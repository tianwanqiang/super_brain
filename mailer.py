"""
super_brain mailer - 审批/审核通知邮件（stdlib smtplib，无新依赖）

P4 工作流需要"邮件发链接 → 点链接进管理界面审核"。邮箱不是核心功能，配置缺省时所有函数
优雅降级（返回 False，不报错、不阻塞工作流）——没配邮箱的人直接在 /admin/workflows 里审核。

config.json 的 MAIL 段（可选，整段缺失=不发邮件）：
{
  "MAIL": {
    "smtp_host": "smtp.example.com",
    "smtp_port": 465,            // 465=SSL；空或 587=STARTTLS；25=明文
    "username": "you@example.com",
    "password": "...",
    "from_addr": "you@example.com",
    "to_addr": "boss@example.com",
    "public_base_url": "http://服务器IP:5151"   // 用于拼审核链接（nginx/HTTPS 就填外网地址）
  }
}
"""
import logging
import os
import smtplib
import ssl
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

import config_store
from paths import CONFIG_PATH

logger = logging.getLogger("super_brain.mailer")

_last_error: str = ""


def last_error() -> str:
    """最近一次发送失败的原因（给 UI 展示用；成功/未尝试过为空）。"""
    return _last_error


def mail_settings() -> dict | None:
    """读 MAIL 段；未配置/缺关键字段返回 None（调用方优雅跳过，不报错）。"""
    config = config_store.read_config_soft(path=CONFIG_PATH, cached=True)
    section = config.get("MAIL") or {}
    host = (section.get("smtp_host") or "").strip()
    to_addr = (section.get("to_addr") or "").strip()
    from_addr = (section.get("from_addr") or section.get("username") or "").strip()
    if not host or not to_addr or not from_addr:
        return None
    return {
        "host": host,
        "port": int(section.get("smtp_port") or 465),
        "username": (section.get("username") or "").strip(),
        "password": section.get("password") or "",
        "from_addr": from_addr,
        "to_addr": to_addr,
        "public_base_url": (section.get("public_base_url") or "http://127.0.0.1:5151").rstrip("/"),
    }


def review_url(settings: dict, path: str) -> str:
    return f"{settings['public_base_url']}{path}"


def send_mail(settings: dict, subject: str, html: str) -> bool:
    """发一封 HTML 邮件。失败记日志返回 False（工作流继续，人还可以直接在后台审）。
    端口策略：按配置先试；465(SSL) 失败自动补试 587(STARTTLS)，反之亦然——很多云服务器/
    办公网络会拦 465 或只放行 587，同一 host 换模式成功率更高。"""
    global _last_error
    _last_error = ""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False  # 测试环境绝不真发邮件（mail_settings 可能读到真实配置）
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(("super_brain", settings["from_addr"]))
    msg["To"] = settings["to_addr"]
    port = int(settings.get("port") or 465)
    primary_ssl = port == 465
    attempts: list[tuple[int, bool]] = [(port, primary_ssl)]
    fallback = (465, True) if not primary_ssl else (587, False)
    if fallback[0] != port:
        attempts.append(fallback)
    errors: list[str] = []
    for try_port, use_ssl in attempts:
        try:
            if use_ssl:
                with smtplib.SMTP_SSL(settings["host"], try_port, timeout=30,
                                      context=ssl.create_default_context()) as smtp:
                    _login(smtp, settings)
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(settings["host"], try_port, timeout=30) as smtp:
                    if try_port == 587:
                        smtp.starttls(context=ssl.create_default_context())
                    _login(smtp, settings)
                    smtp.send_message(msg)
            logger.info(f"审核邮件已发送：{settings['to_addr']} <- {subject}（端口 {try_port}）")
            return True
        except Exception as exc:  # 记录本次尝试失败，继续下一候选端口
            errors.append(f"端口 {try_port}: {exc}")
            logger.warning(f"邮件尝试失败（{try_port}）：{exc}")
    logger.warning("审核邮件全部端口尝试失败（不影响工作流，请直接在后台审核）：%s", " | ".join(errors))
    _last_error = "；".join(errors)
    return False


def _login(smtp, settings: dict) -> None:
    if settings["username"] and settings["password"]:
        smtp.login(settings["username"], settings["password"])
