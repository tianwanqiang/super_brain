"""
super_brain imagegen - 公众号封面图生成与公网链接（MVP）

背景：微信公众号草稿/发布需要"一张公网可访问的预览图"（封面 thumb 必须上传成微信素材）。
这套提供：
1. 生图：纯 Python 生成封面 PNG（无外部依赖，900x383，公众号图文推荐比例；颜色由标题
   确定性派生，保证同样标题每次生成一致）。之后想接真实生图服务（聚合 API）时，在这里加
   provider，把"确定性画布"换成服务返回的图片/URL 即可，调用方接口不变。
2. 落盘：图片写到 `SUPER_BRAIN/static/covers/` —— Flask 默认 static 目录，天然以
   `/static/covers/<文件名>` 对外可访问（本地 http://127.0.0.1:5151 与服务器都行）。
3. 下载链接：`public_url_for()` 用配置里的公网地址拼完整链接，微信侧 fetch 时可达。

选择规则（保证改动最小、兼容老配置）：
- config.json 里若已配置 WECHAT_DEFAULT_COVER_URL（非空）→ 沿用老链接（不打扰现有账号）；
- 未配置 → 自动生成本地封面，并给出公网链接（公网地址取 PUBLIC_BASE_URL，其次
  MAIL.public_base_url，都没有则给 127.0.0.1 并告警：微信拿不到本地地址）。
"""
import hashlib
import struct
import zlib
from log_setup import Clock
from pathlib import Path

import config_store
from paths import SUPER_BRAIN

# 封面画布：公众号图文封面推荐比例约 2.35:1
COVER_WIDTH = 900
COVER_HEIGHT = 383

# 存放目录：放在 Flask 默认 static 下，天然可被 /static/... 访问
COVERS_DIR = SUPER_BRAIN / "static" / "covers"

PUBLIC_BASE_URL_FIELD = "PUBLIC_BASE_URL"


# ---------- 纯 Python PNG 编码（MVP 占位画布，不引入 Pillow） ----------

def _write_png(path: Path, width: int, height: int, pixels: bytes) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + pixels[y * width * 3:(y + 1) * width * 3] for y in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    path.write_bytes(png)


def _title_colors(title: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    digest = hashlib.sha256((title or "super_brain").encode("utf-8")).digest()
    top = (digest[0] % 200 + 30, digest[1] % 200 + 30, digest[2] % 200 + 30)
    bottom = (digest[3] % 160 + 20, digest[4] % 160 + 20, digest[5] % 160 + 20)
    return top, bottom


def generate_cover(title: str = "", slug: str = "") -> Path:
    """生成一张封面 PNG 并落盘到 static/covers/，返回本地文件路径。"""
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in (slug or title or "cover") if c.isalnum() or c in "-_")[:40] or "cover"
    filename = f"cover_{Clock.now():%Y%m%d_%H%M%S}_{safe}.png"
    path = COVERS_DIR / filename

    top, bottom = _title_colors(title)
    pixels = bytearray()
    for y in range(COVER_HEIGHT):
        t = y / max(1, COVER_HEIGHT - 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        pixels.extend(bytes([r, g, b]) * COVER_WIDTH)
    _write_png(path, COVER_WIDTH, COVER_HEIGHT, bytes(pixels))
    return path


def public_base_url(config: dict | None = None) -> str:
    """公网可达地址：PUBLIC_BASE_URL > MAIL.public_base_url > 本机默认（并提示）。"""
    config = config if config is not None else config_store.read_config_soft(cached=True)
    value = (config.get(PUBLIC_BASE_URL_FIELD) or "").strip() \
        or ((config.get("MAIL") or {}).get("public_base_url") or "").strip()
    if not value:
        value = "http://127.0.0.1:5151"
    return value.rstrip("/")


def public_url_for(local_path: Path, config: dict | None = None) -> str:
    """把 static/covers/ 下的本地图片转成公网可访问的下载链接。"""
    name = Path(local_path).name
    return f"{public_base_url(config)}/static/covers/{name}"


def ensure_wechat_cover(title: str = "") -> tuple[Path | None, str | None]:
    """给微信草稿/发布选封面：
    1) config 已配 WECHAT_DEFAULT_COVER_URL → 直接用（返回 (None, url)）；
    2) 否则自动生成本地封面，返回 (本地路径, 公网链接)。
    返回的 URL 用于微信接口拉图/上传素材；本地路径供预览/入库。"""
    config = config_store.read_config_soft(cached=True)
    configured = (config.get("WECHAT_DEFAULT_COVER_URL") or "").strip()
    if configured:
        return None, configured
    path = generate_cover(title)
    return path, public_url_for(path, config)
