import os

# === Telegram ===
BOT_TOKEN  = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_IDS  = list(map(int, os.getenv("ADMIN_IDS", "123456789").split(",")))

# Прокси для Telegram бота (если Telegram заблокирован на сервере)
# Форматы: socks5://user:pass@host:port  или  http://host:port  или  пусто
HTTPS_PROXY = os.getenv("HTTPS_PROXY", "")

# === Sing-box ===
SINGBOX_CONFIG_PATH = "/etc/sing-box/config.json"
SINGBOX_SERVICE     = "sing-box"
SERVER_HOST         = os.getenv("SERVER_HOST", "your.server.domain")
SERVER_PORT         = int(os.getenv("SERVER_PORT", "443"))
OBFS_PASSWORD       = os.getenv("OBFS_PASSWORD", "")

# Clash API — включается добавлением секции experimental в config.json
CLASH_API_URL = os.getenv("CLASH_API_URL", "http://127.0.0.1:9090")

# === База данных ===
DB_PATH = "/opt/singbox-manager/users.db"

# === Web ===
WEB_HOST        = "127.0.0.1"
WEB_PORT        = int(os.getenv("WEB_PORT", "5000"))
WEB_SECRET_KEY  = os.getenv("WEB_SECRET_KEY", "change_this_secret_key_please")
WEB_BASE_URL    = os.getenv("WEB_BASE_URL", "http://your.server.domain:5000")
WEB_ADMIN_USER  = os.getenv("WEB_ADMIN_USER", "admin")
WEB_ADMIN_PASS  = os.getenv("WEB_ADMIN_PASS", "change_me")
# Секретный префикс пути к панели (оставьте пустым чтобы открывалась по /)
# Пример: WEB_PREFIX=/drLw/OVGkbM=  → панель на https://domain/drLw/OVGkbM=/
WEB_PREFIX      = os.getenv("WEB_PREFIX", "")

# Префикс URL подписки (если хотите проксировать через другой домен)
# Пример: SUB_URL_PREFIX=https://proxy.example.com/?url=
# Тогда ссылка подписки будет: SUB_URL_PREFIX + основной_url
SUB_URL_PREFIX  = os.getenv("SUB_URL_PREFIX", "")

# === Email ===
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.mail.ru")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_SSL  = os.getenv("SMTP_SSL", "true").lower() == "true"   # true=SSL/465, false=STARTTLS/587
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
# SMTP_FROM — строка «От кого» которую видит получатель в поле «От»
# Пример: "VPN Service <vpn@mail.ru>"  или просто "vpn@mail.ru"
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)

# === Лимиты по умолчанию ===
DEFAULT_TRAFFIC_LIMIT_GB = 0
DEFAULT_EXPIRE_DAYS       = 0

# === Логи ===
SINGBOX_LOG_PATH = "/var/log/sing-box/sing-box.log"
