"""
super_brain publishers - 真实发布能力

给 dispatcher.py 提供不依赖 Claude Code / MCP 的真实发布接口：
- 微信公众号：换 access_token -> 把封面图上传成永久素材换 thumb_media_id -> 建草稿
- 头条：委托给 toutiao-agent 已经跑通的 PowerShell 脚本，不重新实现 DeepSeek 调用逻辑

边界说明（跟 ship / ops-assistant 的 private.md 一致）：
- 默认只做到"建草稿"，绝不群发。
- publish_wechat_article()（freepublish 对外发布）是唯一的例外：它只在 autopublish 流水线
  的三重闸门（全局主开关 + 渠道 mode='api' + CEO 逐单放行）全部通过时才会被调用，
  不从这里直接触发。其它路径（ops-assistant/ship/inbox）仍然只到草稿为止。

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

import config_store
from paths import CONFIG_PATH, OPC_ROOT, SUPER_BRAIN

logger = logging.getLogger("super_brain.publishers")

# 注意：CONFIG_PATH 是从 paths.py import 的统一权威常量（不再是本模块自拼的一份）。
# 头条草稿这条路径本质是调用 toutiao-agent（兄弟项目）的 PowerShell 脚本——这是它自身的
# 实现方式决定的，只能在 Windows 上跑，不是路径写法能解决的问题。服务器（Linux）上调用
# 这个功能会在 subprocess 那一步明确报错（找不到 powershell），不会是这种诡异的路径拼接错误。
TOUTIAO_SCRIPT = OPC_ROOT / "toutiao-agent" / "Generate-ToutiaoDraft.ps1"
TOUTIAO_DRAFTS_DIR_DEFAULT = OPC_ROOT / "toutiao-agent" / "drafts"
# 公众号草稿的本地预览文件不能挂靠头条那套默认路径——头条的默认值依赖兄弟项目
# toutiao-agent 的目录结构（OPC_ROOT/toutiao-agent/drafts），服务器上这个兄弟项目根本
# 不存在，公众号内容跟它没有任何关系，存进一个语义上完全不相关、服务器上凭空造出来的
# 目录里会让人误以为两者有依赖关系。默认值改成 SUPER_BRAIN 自己下面的目录——不管本机
# 还是服务器，SUPER_BRAIN 都保证指向 super_brain 自己的仓库根目录，不依赖任何兄弟项目
# 是否存在。
WECHAT_DRAFTS_DIR_DEFAULT = SUPER_BRAIN / "wechat_drafts"


class PublishError(Exception):
    """真实发布调用失败时抛出，携带微信/头条返回的原始错误信息，不吞掉细节。"""


def load_config() -> dict:
    """读 config.json（走 config_store 统一实现，缓存读）。缺文件/坏文件统一转成带原因的
    PublishError——微信凭据是硬性必需，调用方不能默默用一份空配置往下走。"""
    try:
        return config_store.read_config_cached(path=CONFIG_PATH)
    except config_store.ConfigReadError as exc:
        if exc.kind == "missing":
            raise PublishError(
                f"找不到 {CONFIG_PATH}。需要包含 WECHAT_APP_ID / WECHAT_APP_KEY / "
                f"WECHAT_DEFAULT_COVER_URL 三个字段，参考 config.json.example（如果有）自己建一份。"
            ) from exc
        raise PublishError(str(exc)) from exc


def get_toutiao_drafts_dir() -> Path:
    """草稿存放目录可以通过 config.json 里的 TOUTIAO_DRAFTS_DIR 迁移到别处（比如打包给客户、
    换电脑时不用改代码）；没配置就用 toutiao-agent 项目自带的 drafts/（历史默认值，兼容老数据）。
    跟微信凭据不同，这个不是硬性必填项，缺配置不该报错，直接兜底。
    """
    config = config_store.read_config_soft(path=CONFIG_PATH, cached=True)
    value = config.get("TOUTIAO_DRAFTS_DIR")
    if value:
        return Path(value)
    return TOUTIAO_DRAFTS_DIR_DEFAULT


def get_wechat_drafts_dir() -> Path:
    """公众号草稿本地预览文件的存放目录——跟 get_toutiao_drafts_dir() 是同样的"可选配置+
    兜底默认值"模式，但默认值跟头条完全独立（见 WECHAT_DRAFTS_DIR_DEFAULT 上面的说明），
    不共用同一个目录、也不共用同一个默认值来源。config.json 里对应字段是 WECHAT_DRAFTS_DIR。
    """
    config = config_store.read_config_soft(path=CONFIG_PATH, cached=True)
    value = config.get("WECHAT_DRAFTS_DIR")
    if value:
        return Path(value)
    return WECHAT_DRAFTS_DIR_DEFAULT


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


def publish_wechat_draft(title: str, content_html: str, cover_url: str | None = None) -> dict:
    """完整流程：换 token -> 上传封面 -> 建草稿。只做到草稿，绝不群发。

    cover_url：封面图公网地址（微信要先下载并上传成永久素材换 thumb_media_id）。
    不传时读 config.json 的 WECHAT_DEFAULT_COVER_URL；调用方（autopublish/imagegen）
    可显式传入自动生成的封面链接——公众号草稿必须有封面，所以必须有其一。"""
    logger.info("开始公众号草稿发布流程")
    config = load_config()
    access_token = wechat_get_access_token(config["WECHAT_APP_ID"], config["WECHAT_APP_KEY"])
    cover = cover_url or config.get("WECHAT_DEFAULT_COVER_URL", "")
    if not cover:
        raise PublishError(
            "公众号草稿需要封面图：config.json 未配 WECHAT_DEFAULT_COVER_URL，调用方也没传 cover_url。"
        )
    thumb_media_id = wechat_upload_thumb(access_token, cover)
    draft_media_id = wechat_create_draft(access_token, thumb_media_id, title, content_html)
    logger.info("公众号草稿发布流程全部完成")
    return {"draft_media_id": draft_media_id, "thumb_media_id": thumb_media_id}


# ---------- 微信：真正对外发布（autopublish 流水线的最后一步） ----------

def publish_wechat_article(draft_media_id: str) -> dict:
    """把草稿箱里的一篇草稿真正发布出去（freepublish/submit，发布后对外可见、可被搜索）。

    这是整个项目第一次触碰"真实对外发布"的接口，调用方必须满足的前提（autopublish.py 的
    三重闸门在调度层保证，这里只做接口本身）：
    - 公众号必须已开通"发布"能力：目前只有认证服务号/已认证的订阅号可用，未认证账号调这个
      接口会报错（45064 之类的错误码）——报错原样抛 PublishError，不吞。
    - 有每日发布次数限制（订阅号/服务号各不同），超限报错原样透传。
    - 一旦成功，内容立即对外可见且不可撤回（只能删除已发布文章），调用前先想清楚。

    返回 {"publish_id": "..."}（后续可以用 publish_id 查发布状态/删除）。
    """
    logger.info(f"微信 freepublish：发布草稿 {draft_media_id}")
    config = load_config()
    access_token = wechat_get_access_token(config["WECHAT_APP_ID"], config["WECHAT_APP_KEY"])
    url = f"https://api.weixin.qq.com/cgi-bin/freepublish/submit?access_token={access_token}"
    body = json.dumps({"media_id": draft_media_id}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "publish_id" not in data:
        logger.error(f"微信 freepublish 失败：{data}")
        raise PublishError(f"公众号发布失败：{data}（常见原因：账号未开通发布能力/超每日次数/草稿不存在）")
    logger.info(f"微信 freepublish 成功：publish_id={data['publish_id']}")
    return {"publish_id": data["publish_id"]}


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
