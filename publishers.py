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
import mimetypes
import subprocess
import urllib.request
import uuid
from pathlib import Path

SUPER_BRAIN = Path(r"G:\code\super_brain")
CONFIG_PATH = SUPER_BRAIN / "config.json"
TOUTIAO_SCRIPT = Path(r"G:\code\toutiao-agent\Generate-ToutiaoDraft.ps1")
TOUTIAO_DRAFTS_DIR = Path(r"G:\code\toutiao-agent\drafts")


class PublishError(Exception):
    """真实发布调用失败时抛出，携带微信/头条返回的原始错误信息，不吞掉细节。"""


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise PublishError(
            f"找不到 {CONFIG_PATH}。需要包含 WECHAT_APP_ID / WECHAT_APP_KEY / "
            f"WECHAT_DEFAULT_COVER_URL 三个字段，参考 config.json.example（如果有）自己建一份。"
        )
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))


# ---------- 微信 ----------

def wechat_get_access_token(appid: str, appkey: str) -> str:
    url = (
        "https://api.weixin.qq.com/cgi-bin/token"
        f"?grant_type=client_credential&appid={appid}&secret={appkey}"
    )
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "access_token" not in data:
        raise PublishError(f"获取微信 access_token 失败：{data}")
    return data["access_token"]


def wechat_upload_thumb(access_token: str, image_url: str) -> str:
    """下载封面图，上传成微信永久素材，返回 thumb_media_id。"""
    with urllib.request.urlopen(image_url, timeout=30) as resp:
        image_bytes = resp.read()
        content_type = resp.headers.get_content_type() or "image/png"

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
        raise PublishError(f"上传微信封面图失败：{data}")
    return data["media_id"]


def wechat_create_draft(access_token: str, thumb_media_id: str, title: str, content_html: str) -> str:
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
        raise PublishError(f"创建微信草稿失败：{data}")
    return data["media_id"]


def publish_wechat_draft(title: str, content_html: str) -> dict:
    """完整流程：换 token -> 上传封面 -> 建草稿。只做到草稿，绝不群发。"""
    config = load_config()
    access_token = wechat_get_access_token(config["WECHAT_APP_ID"], config["WECHAT_APP_KEY"])
    thumb_media_id = wechat_upload_thumb(access_token, config["WECHAT_DEFAULT_COVER_URL"])
    draft_media_id = wechat_create_draft(access_token, thumb_media_id, title, content_html)
    return {"draft_media_id": draft_media_id, "thumb_media_id": thumb_media_id}


# ---------- 头条 ----------

def publish_toutiao_draft(date: str) -> Path | None:
    """委托给 toutiao-agent 已经跑通的 PowerShell 脚本，不重新实现它的 DeepSeek 调用逻辑。
    只生成草稿文件，头条没有官方发布 API，这里到此为止。

    返回 None 表示"当天没有 opc 素材，安全跳过"——这不是失败，调用方不应该当错误处理。
    """
    if not TOUTIAO_SCRIPT.exists():
        raise PublishError(f"找不到 {TOUTIAO_SCRIPT}")

    opc_path = Path(r"G:\code") / f"opc_{date}.md"
    if not opc_path.exists():
        return None

    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(TOUTIAO_SCRIPT), "-Date", date],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise PublishError(f"头条草稿生成失败（exit {result.returncode}）：{result.stderr or result.stdout}")
    draft_path = TOUTIAO_DRAFTS_DIR / f"toutiao_{date}.md"
    if not draft_path.exists():
        raise PublishError(f"脚本退出码是 0，但没找到预期的草稿文件 {draft_path}（opc 素材确实存在，属于真实异常）")
    return draft_path
