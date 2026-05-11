#!/bin/bash
# install.sh — Установка sing-box manager на Ubuntu 24.04
# Запускать от root: bash install.sh

set -e

APP_DIR="/opt/singbox-manager"
SERVICE_USER="singboxmgr"
PYTHON="python3"

echo "=== Sing-box Manager Installer ==="

# 1. Зависимости
echo "[1/7] Установка системных пакетов..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git curl

# 2. Системный пользователь
echo "[2/7] Создание пользователя $SERVICE_USER..."
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd -r -s /bin/false "$SERVICE_USER"
fi
# Разрешаем управлять sing-box через sudo без пароля
echo "$SERVICE_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl reload sing-box, /usr/bin/systemctl restart sing-box, /usr/bin/systemctl is-active sing-box, /usr/bin/systemctl show sing-box" \
  > /etc/sudoers.d/singboxmgr
chmod 440 /etc/sudoers.d/singboxmgr

# 3. Копирование файлов
echo "[3/7] Копирование файлов приложения..."
mkdir -p "$APP_DIR/templates"
cp -r ./* "$APP_DIR/" 2>/dev/null || true
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
chmod 755 "$APP_DIR"

# Права на конфиг sing-box
chown "$SERVICE_USER:$SERVICE_USER" /etc/sing-box/config.json
chmod 660 /etc/sing-box/config.json

# 4. Python venv + зависимости
echo "[4/7] Установка Python зависимостей..."
$PYTHON -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

# 5. .env файл (если не существует)
echo "[5/7] Настройка .env..."
ENV_FILE="$APP_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" << 'EOF'
BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
ADMIN_IDS=123456789
SERVER_HOST=your.domain.com
OBFS_PASSWORD=your_obfs_password_from_config
WEB_BASE_URL=http://your.domain.com:5000
WEB_SECRET_KEY=change_this_to_random_string

# SMTP (опционально)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your@gmail.com
SMTP_PASS=your_app_password
SMTP_FROM=VPN <your@gmail.com>
EOF
    chown "$SERVICE_USER:$SERVICE_USER" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "  ⚠️  Заполните $ENV_FILE перед запуском!"
fi

# 6. Systemd unit: Telegram Bot
echo "[6/7] Создание systemd сервисов..."
cat > /etc/systemd/system/singbox-bot.service << EOF
[Unit]
Description=Sing-box Telegram Bot
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/venv/bin/python bot.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# Systemd unit: Web Panel
cat > /etc/systemd/system/singbox-web.service << EOF
[Unit]
Description=Sing-box Web Panel
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/venv/bin/python web.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# 7. Инициализация БД и перезапуск
echo "[7/7] Инициализация базы данных..."
cd "$APP_DIR"
sudo -u "$SERVICE_USER" "$APP_DIR/venv/bin/python" -c "import db; db.init_db(); print('DB OK')"

systemctl daemon-reload
systemctl enable singbox-bot singbox-web
echo ""
echo "=== Установка завершена! ==="
echo ""
echo "  1. Отредактируйте $ENV_FILE"
echo "  2. Запустите сервисы:"
echo "       systemctl start singbox-bot"
echo "       systemctl start singbox-web"
echo ""
echo "  Логи бота:  journalctl -u singbox-bot -f"
echo "  Логи веба:  journalctl -u singbox-web -f"
echo ""
echo "  Web-панель: http://YOUR_SERVER:5000 (admin / change_me_password)"
echo "  Пароль веб-панели: измените WEB_USERS в web.py"
