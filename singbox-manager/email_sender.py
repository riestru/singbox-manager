"""
email_sender.py — отправка конфигурации на email.
Автоматически выбирает SSL (порт 465) или STARTTLS (порт 587).
"""
import smtplib, io, logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

import qrcode
from config import SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM, SERVER_HOST

log = logging.getLogger(__name__)


def _qr_png(data: str) -> bytes:
    buf = io.BytesIO()
    qrcode.make(data).save(buf, format="PNG")
    return buf.getvalue()


def send_config_email(to_email: str, user_name: str,
                      hy2_uri: str, sub_url: str, config_url: str) -> tuple[bool, str]:
    """Возвращает (True, "") при успехе или (False, "описание ошибки")."""
    if not SMTP_USER or not SMTP_PASS:
        return False, "SMTP_USER или SMTP_PASS не заданы в .env"

    sender = SMTP_FROM or SMTP_USER

    msg = MIMEMultipart("related")
    msg["Subject"] = f"VPN конфигурация — {user_name}"
    msg["From"]    = sender
    msg["To"]      = to_email

    html = f"""
    <html><body>
    <h2>Ваша VPN конфигурация</h2>
    <p><b>Сервер:</b> {SERVER_HOST} &nbsp; <b>Пользователь:</b> {user_name}</p>

    <h3>URI подключения</h3>
    <p style="word-break:break-all;font-family:monospace;background:#f4f4f4;padding:10px;">{hy2_uri}</p>
    <p><img src="cid:qr_uri" width="200" height="200"/></p>

    <h3>Ссылка на подписку</h3>
    <p><a href="{sub_url}">{sub_url}</a></p>
    <p><img src="cid:qr_sub" width="200" height="200"/></p>

    <h3>Файл конфигурации sing-box</h3>
    <p><a href="{config_url}">Скачать singbox JSON</a></p>

    <hr/>
    <p style="color:#888;font-size:12px;">
      Клиенты: sing-box, NekoBox, Hiddify — совместимы с Hysteria2.
    </p>
    </body></html>
    """
    msg.attach(MIMEText(html, "html", "utf-8"))

    for cid, data in [("qr_uri", hy2_uri), ("qr_sub", sub_url)]:
        img = MIMEImage(_qr_png(data), _subtype="png")
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
        msg.attach(img)

    try:
        use_ssl = SMTP_PORT == 465
        if use_ssl:
            # Порт 465 — SSL с самого начала (mail.ru, некоторые другие)
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as s:
                s.login(SMTP_USER, SMTP_PASS)
                s.sendmail(sender, [to_email], msg.as_bytes())
        else:
            # Порт 587 (или любой другой) — STARTTLS (gmail, яндекс и др.)
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
                s.ehlo()
                s.starttls()
                s.login(SMTP_USER, SMTP_PASS)
                s.sendmail(sender, [to_email], msg.as_bytes())
        log.info(f"[email] sent to {to_email} via {'SSL' if use_ssl else 'STARTTLS'}")
        return True, ""
    except Exception as e:
        log.error(f"[email] {e}")
        return False, str(e)
