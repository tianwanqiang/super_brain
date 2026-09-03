"""
super_brain publishers - 真实发布能力

给 dispatcher.py 提供不依赖 Claude Code / MCP 的真实发布接口：
- 微信公众号：换 access_token -> 把封面图上传成永久素材换 thumb_media_id -> 建草稿
- 头条：委托给 toutiao-agent 已经跑通的 PowerShell 脚本，不重新实现 DeepSeek 调用逻辑

明确边界（跟 ship / ops-assistant 的 private.md 一致）：
- 只做到"建草稿"，绝不调用微信的群发/发布接口
- 头条本来就没有官方发布 API，只到"生成草稿文件"为止

凭据来自 config.json（已 gitignore，不进公开仓库）。
"""
import json
import logging
import mimetypes
import shutil
import subprocess
import urllib.request
import uuid
from pathlib import Path

from paths import OPC_ROOT, SUPER_BRAIN

logger = logging.getLogger("super_brain.publishers")

CONFIG_PATH = SUPER_BRAIN / "config.json"
# 头条草稿这条路径本质是调用 toutiao-agent（兄弟项目）的 PowerShell 脚本——这是它自身的
# 实现方式决定的，只能在 Windows 上跑，不是路径写法能解决的问题。服务器（Linux）上调用
# 这个功能会在 subprocess 那一步明确报错（找不到 powershell），不会是这种诡异的路径拼接错误。
TOUTIAO_SCRIPT = OPC_ROOT / "toutiao-agent" / "Generate-ToutiaoDraft.ps1"
TOUTIAO_DRAFTS_DIR_DEFAULT = OPC_ROOT / "toutiao-agent" / "drafts"


class PublishError(Exception):
    """真实发布调用失败时抛出，携带微信/头条返回的原始错误信息，不吞掉细节。"""


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise PublishError(
            f"找不到 {CONFIG_PATH}。需要包含 WECHAT_APP_ID / WECHAT_APP_KEY / "
            f"WECHAT_DEFAULT_COVER_URL 三个字段，参考 config.json.example（如果有）自己建一份。"
        )
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))


def get_toutiao_drafts_dir() -> Path:
    """草稿存放目录可以通过 config.json 里的 TOUTIAO_DRAFTS_DIR 迁移到别处（比如打包给客户、
    换电脑时不用改代码）；没配置就用 toutiao-agent 项目自带的 drafts/（历史默认值，兼容老数据）。
    跟微信凭据不同，这个不是硬性必填项，缺配置不该报错，直接兜底。
    """
    if CONFIG_PATH.exists():
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            value = config.get("TOUTIAO_DRAFTS_DIR")
            if value:
                return Path(value)
        except (json.JSONDecodeError, OSError):
            logger.warning("读取 config.json 里的 TOUTIAO_DRAFTS_DIR 失败，用默认目录兜底")
    return TOUTIAO_DRAFTS_DIR_DEFAULT


# ---------- 微信 ----------

def wechat_get_access_token(appid: str, appkey: str) -> str:
    logger.info(f"微信 step 1/3：换 access_token（appid={appid}）")
    url = (
        "https://api.weixin.qq.com/cgi-bin/token"
        f"?grant_type=client_credential&appid={appid}&secret={appkey}"
    )
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "access_token" not in data:
        logger.error(f"微信 step 1/3 失败：{data}")
        raise PublishError(f"获取微信 access_token 失败：{data}")
    logger.info("微信 step 1/3 成功")
    return data["access_token"]


def wechat_upload_thumb(access_token: str, image_url: str) -> str:
    """下载封面图，上传成微信永久素材，返回 thumb_media_id。"""
    logger.info(f"微信 step 2/3：下载封面图并上传成永久素材（源：{image_url}）")
    with urllib.request.urlopen(image_url, timeout=30) as resp:
        image_bytes = resp.read()
        content_type = resp.headers.get_content_type() or "image/png"
    logger.debug(f"封面图下载完成，{len(image_bytes)} 字节，content-type={content_type}")

    ext = mimetypes.guess_extension(content_type) or ".png"
    boundary = uuid.uuid4().hex
    filename = f"cover{ext}"

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="media"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8") + image_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

    url = f"https://api.weixin.qq.com/cgi-bin/material/add_material?access_token={access_token}&type=image"
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "media_id" not in data:
        logger.error(f"微信 step 2/3 失败：{data}")
        raise PublishError(f"上传微信封面图失败：{data}")
    logger.info(f"微信 step 2/3 成功，media_id={data['media_id']}")
    return data["media_id"]


def wechat_create_draft(access_token: str, thumb_media_id: str, title: str, content_html: str) -> str:
    logger.info(f"微信 step 3/3：创建草稿（标题：{title}，正文 {len(content_html)} 字符）")
    url = f"https://api.weixin.qq.com/cgi-bin/draft/add?access_token={access_token}"
    body = json.dumps({
        "articles": [{
            "title": title,
            "author": "",
            "digest": "",
            "content": content_html,
            "content_source_url": "",
            "thumb_media_id": thumb_media_id,
            "need_open_comment": 0,
            "only_fans_can_comment": 0,
        }]
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "media_id" not in data:
        logger.error(f"微信 step 3/3 失败：{data}")
        raise PublishError(f"创建微信草稿失败：{data}")
    logger.info(f"微信 step 3/3 成功，draft_media_id={data['media_id']}")
    return data["media_id"]


def publish_wechat_draft(title: str, content_html: str) -> dict:
    """完整流程：换 token -> 上传封面 -> 建草稿。只做到草稿，绝不群发。"""
    logger.info("开始公众号草稿发布流程")
    config = load_config()
    access_token = wechat_get_access_token(config["WECHAT_APP_ID"], config["WECHAT_APP_KEY"])
    thumb_media_id = wechat_upload_thumb(access_token, config["WECHAT_DEFAULT_COVER_URL"])
    draft_media_id = wechat_create_draft(access_token, thumb_media_id, title, content_html)
    logger.info("公众号草稿发布流程全部完成")
    return {"draft_media_id": draft_media_id, "thumb_media_id": thumb_media_id}


# ---------- 头条 ----------

def publish_toutiao_draft(date: str) -> Path | None:
    """委托给 toutiao-agent 已经跑通的 PowerShell 脚本，不重新实现它的 DeepSeek 调用逻辑。
    只生成草稿文件，头条没有官方发布 API，这里到此为止。

    返回 None 表示"当天没有 opc 素材，安全跳过"——这不是失败，调用方不应该当错误处理。
    """
    if not TOUTIAO_SCRIPT.exists():
        logger.error(f"找不到 {TOUTIAO_SCRIPT}")
        raise PublishError(f"找不到 {TOUTIAO_SCRIPT}")

    opc_path = OPC_ROOT / f"opc_{date}.md"
    if not opc_path.exists():
        logger.info(f"头条：{opc_path} 不存在，安全跳过")
        return None

    logger.info(f"头条：调用 {TOUTIAO_SCRIPT.name} -Date {date}")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(TOUTIAO_SCRIPT), "-Date", date],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        logger.error(f"头条脚本退出码 {result.returncode}：{result.stderr or result.stdout}")
        raise PublishError(f"头条草稿生成失败（exit {result.returncode}）：{result.stderr or result.stdout}")

    # PowerShell 脚本自己写死了输出目录（toutiao-agent\drafts），不知道 super_brain 这边配置了
    # 别的存放目录——脚本内部逻辑不动，这里只是把产出文件统一挪到用户配置的目录，让"存放目录
    # 可配置"这件事对两条生成路径（当天 opc / 会议纪要）都是真的，而不是只对一半路径生效。
    script_output_dir = TOUTIAO_SCRIPT.parent / "drafts"
    draft_path = script_output_dir / f"toutiao_{date}.md"
    if not draft_path.exists():
        logger.error(f"头条脚本退出码是 0，但没找到预期的草稿文件 {draft_path}")
        raise PublishError(f"脚本退出码是 0，但没找到预期的草稿文件 {draft_path}（opc 素材确实存在，属于真实异常）")

    target_dir = get_toutiao_drafts_dir()
    if target_dir.resolve() != script_output_dir.resolve():
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / draft_path.name
        shutil.move(str(draft_path), str(target_path))
        logger.info(f"头条草稿已从脚本默认目录搬到配置目录：{target_path}")
        draft_path = target_path

    logger.info(f"头条草稿生成成功：{draft_path}")
    return draft_path
