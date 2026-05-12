#!/bin/bash
# =============================================================================
# singbox-manager-install.sh
# Полная установка sing-box + singbox-manager на Ubuntu 24.04
# Использование: bash singbox-manager-install.sh
# =============================================================================
set -euo pipefail

# ── Цвета для вывода ──────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERR]${NC}  $*"; exit 1; }
step()    { echo -e "\n${BOLD}══ $* ══${NC}"; }

# ── Константы ─────────────────────────────────────────────────────────────
APP_DIR="/opt/singbox-manager"
SERVICE_USER="singboxmgr"
SINGBOX_VERSION="1.13.2"
# Для автоопределения последней версии sing-box раскомментируйте:
# SINGBOX_VERSION=$(curl -s https://api.github.com/repos/SagerNet/sing-box/releases/latest \
#   | grep '"tag_name"' | sed 's/.*"v\([^"]*\)".*/\1/')
SINGBOX_BIN="/usr/local/bin/sing-box"
SINGBOX_CONFIG="/etc/sing-box/config.json"
CERTS_DIR="/etc/ssl/singbox-manager"
MANAGER_ZIP_URL="https://github.com/riestru/singbox-manager/releases/latest/download/singbox-manager.zip"

# ── Проверка root ─────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && error "Запускайте от root: sudo bash singbox-manager-install.sh"

echo -e "${BOLD}"
echo "╔══════════════════════════════════════════════════╗"
echo "║        Sing-box Manager — Установщик            ║"
echo "║        Ubuntu 24.04 / sing-box ${SINGBOX_VERSION}          ║"
echo "╚══════════════════════════════════════════════════╝"
echo -e "${NC}"

# =============================================================================
# ШАГ 1: Запрос параметров
# =============================================================================
step "Параметры установки"

# Домен
echo -e "\n${BOLD}Домен${NC} (например: vpn.example.com)"
echo "Если домена нет — нажмите Enter (будет самоподписанный сертификат)"
read -rp "Домен: " DOMAIN
DOMAIN="${DOMAIN// /}"

# Email (для acme.sh, только если домен задан)
if [[ -n "$DOMAIN" ]]; then
    echo -e "\n${BOLD}Email${NC} для Let's Encrypt (acme.sh):"
    while true; do
        read -rp "Email: " ACME_EMAIL
        [[ "$ACME_EMAIL" =~ ^[^@]+@[^@]+\.[^@]+$ ]] && break
        warn "Некорректный email, попробуйте снова"
    done
fi

# WEB_PREFIX — секретный путь к панели
echo -e "\n${BOLD}Секретный путь к веб-панели${NC} (WEB_PREFIX)"
echo "Генерируется автоматически. Можно изменить или оставить пустым (панель на /)."
AUTO_PREFIX=$(openssl rand -base64 8 | tr '/+=' '_-x' | head -c 12)
echo -e "Сгенерированный: ${CYAN}${AUTO_PREFIX}${NC}"
read -rp "Нажмите Enter чтобы использовать его, или введите свой (пусто = без префикса): " CUSTOM_PREFIX
if [[ -z "$CUSTOM_PREFIX" ]]; then
    WEB_PREFIX="$AUTO_PREFIX"
    info "Используется: ${WEB_PREFIX}"
elif [[ "$CUSTOM_PREFIX" == " " ]]; then
    WEB_PREFIX=""
    warn "Панель будет доступна на / без защиты путём"
else
    WEB_PREFIX="$CUSTOM_PREFIX"
    info "Используется: ${WEB_PREFIX}"
fi

# Генерация паролей
OBFS_PASSWORD=$(openssl rand -base64 16 | tr '/+' '_-')
WEB_SECRET_KEY=$(openssl rand -hex 24)
WEB_ADMIN_PASS=$(openssl rand -base64 12 | tr '/+=' '_-x')

echo -e "\n${BOLD}Параметры установки:${NC}"
echo "  Домен:          ${DOMAIN:-'нет (самоподписанный сертификат)'}"
echo "  OBFS пароль:    ${OBFS_PASSWORD}"
echo "  WEB_PREFIX:     ${WEB_PREFIX:-'(пусто — панель на /)'}"
echo "  WEB_ADMIN_PASS: ${WEB_ADMIN_PASS}"
echo ""
read -rp "Продолжить? [Enter / Ctrl+C для отмены]: "

# =============================================================================
# ШАГ 2: Системные пакеты
# =============================================================================
step "Системные пакеты"
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    nginx curl wget unzip socat sqlite3
success "Пакеты установлены"

# =============================================================================
# ШАГ 3: Sing-box
# =============================================================================
step "Установка sing-box ${SINGBOX_VERSION}"

SINGBOX_TGZ="sing-box-${SINGBOX_VERSION}-linux-amd64.tar.gz"
SINGBOX_URL="https://github.com/SagerNet/sing-box/releases/download/v${SINGBOX_VERSION}/${SINGBOX_TGZ}"

cd /tmp
if [[ ! -f "$SINGBOX_TGZ" ]]; then
    info "Скачиваем sing-box..."
    wget -q --show-progress "$SINGBOX_URL" -O "$SINGBOX_TGZ"
fi
tar -xzf "$SINGBOX_TGZ"
cp "/tmp/sing-box-${SINGBOX_VERSION}-linux-amd64/sing-box" "$SINGBOX_BIN"
chmod +x "$SINGBOX_BIN"
rm -rf "/tmp/sing-box-${SINGBOX_VERSION}-linux-amd64" "/tmp/$SINGBOX_TGZ"
success "sing-box ${SINGBOX_VERSION} установлен → ${SINGBOX_BIN}"

# Systemd unit для sing-box
cat > /etc/systemd/system/sing-box.service << 'EOF'
[Unit]
Description=Sing-box
After=network.target

[Service]
ExecStart=/usr/local/bin/sing-box run -c /etc/sing-box/config.json
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
success "systemd unit sing-box создан"

# =============================================================================
# ШАГ 4: TLS сертификат
# =============================================================================
step "TLS сертификат"
mkdir -p "$CERTS_DIR" /var/log/sing-box /var/www/html

if [[ -n "$DOMAIN" ]]; then
    info "Получаем сертификат для ${DOMAIN} через acme.sh..."

    # Временный nginx для acme challenge
    cat > /etc/nginx/sites-available/acme-temp << NGINXEOF
server {
    listen 80;
    server_name ${DOMAIN};
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 444; }
}
NGINXEOF
    rm -f /etc/nginx/sites-enabled/default
    ln -sf /etc/nginx/sites-available/acme-temp /etc/nginx/sites-enabled/acme-temp
    nginx -t -q && systemctl restart nginx

    # acme.sh
    if [[ ! -f ~/.acme.sh/acme.sh ]]; then
        curl -fsSL https://get.acme.sh | sh -s email="$ACME_EMAIL" --no-profile
    fi
    # shellcheck source=/dev/null
    . ~/.acme.sh/acme.sh.env 2>/dev/null || export PATH="$HOME/.acme.sh:$PATH"

    ~/.acme.sh/acme.sh --issue -d "$DOMAIN" --webroot /var/www/html --quiet \
      || error "Не удалось получить сертификат для ${DOMAIN}. Проверьте DNS и порт 80."

    ~/.acme.sh/acme.sh --install-cert -d "$DOMAIN" \
        --cert-file      "${CERTS_DIR}/cert.pem" \
        --key-file       "${CERTS_DIR}/key.pem" \
        --fullchain-file "${CERTS_DIR}/fullchain.pem" \
        --reloadcmd      "systemctl reload nginx" \
        --quiet

    rm -f /etc/nginx/sites-enabled/acme-temp /etc/nginx/sites-available/acme-temp
    success "Сертификат получен → ${CERTS_DIR}"
    CERT_PATH="${CERTS_DIR}/fullchain.pem"
    KEY_PATH="${CERTS_DIR}/key.pem"
else
    warn "Домен не задан — генерируем самоподписанный сертификат"
    openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
        -keyout "${CERTS_DIR}/key.pem" \
        -out    "${CERTS_DIR}/fullchain.pem" \
        -subj   "/CN=sing-box-vpn" -quiet
    CERT_PATH="${CERTS_DIR}/fullchain.pem"
    KEY_PATH="${CERTS_DIR}/key.pem"
    success "Самоподписанный сертификат создан"
fi
chmod 640 "${CERTS_DIR}"/*.pem

# =============================================================================
# ШАГ 5: Конфигурация sing-box
# =============================================================================
step "Конфигурация sing-box"
mkdir -p /etc/sing-box

cat > "$SINGBOX_CONFIG" << CFGEOF
{
  "log": {
    "level": "info",
    "output": "/var/log/sing-box/sing-box.log",
    "timestamp": true
  },
  "inbounds": [
    {
      "type": "hysteria2",
      "tag": "hysteria-in",
      "listen": "0.0.0.0",
      "listen_port": 443,
      "sniff": true,
      "sniff_override_destination": false,
      "ignore_client_bandwidth": false,
      "users": [],
      "obfs": {
        "type": "salamander",
        "password": "${OBFS_PASSWORD}"
      },
      "tls": {
        "enabled": true,
        "certificate_path": "${CERT_PATH}",
        "key_path": "${KEY_PATH}"
      }
    }
  ],
  "outbounds": [
    { "type": "direct", "tag": "direct" },
    { "type": "block",  "tag": "block"  }
  ],
  "route": {
    "rules": [
      { "ip_is_private": true, "outbound": "block" }
    ],
    "final": "direct"
  },
  "experimental": {
    "clash_api": {
      "external_controller": "127.0.0.1:9090",
      "secret": ""
    }
  }
}
CFGEOF
success "config.json создан"

# =============================================================================
# ШАГ 6: Установка singbox-manager
# =============================================================================
step "Установка singbox-manager"

# Системный пользователь
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd -r -s /bin/false "$SERVICE_USER"
    success "Пользователь ${SERVICE_USER} создан"
fi

# Скачиваем и распаковываем
info "Скачиваем singbox-manager..."
cd /opt
curl -fsSL -o /tmp/singbox-manager.zip "$MANAGER_ZIP_URL"
unzip -qo /tmp/singbox-manager.zip -d /opt
rm -f /tmp/singbox-manager.zip
mkdir -p "${APP_DIR}/templates"

# Python venv
info "Устанавливаем Python зависимости..."
python3 -m venv "${APP_DIR}/venv"
"${APP_DIR}/venv/bin/pip" install -q --upgrade pip
"${APP_DIR}/venv/bin/pip" install -q -r "${APP_DIR}/requirements.txt"
success "Python venv готов"

# .env
ENV_FILE="${APP_DIR}/.env"
if [[ -f "${APP_DIR}/.env.example" ]]; then
    cp "${APP_DIR}/.env.example" "$ENV_FILE"
else
    touch "$ENV_FILE"
fi

# Заполняем известные значения автоматически
_set_env() {
    local key="$1" val="$2"
    if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
    else
        echo "${key}=${val}" >> "$ENV_FILE"
    fi
}

_set_env "OBFS_PASSWORD"    "$OBFS_PASSWORD"
_set_env "WEB_PREFIX"       "$WEB_PREFIX"
_set_env "WEB_SECRET_KEY"   "$WEB_SECRET_KEY"
_set_env "WEB_ADMIN_PASS"   "$WEB_ADMIN_PASS"
if [[ -n "$DOMAIN" ]]; then
    _set_env "SERVER_HOST"  "$DOMAIN"
    _set_env "WEB_BASE_URL" "https://${DOMAIN}"
fi

chmod 600 "$ENV_FILE"
chown "$SERVICE_USER:$SERVICE_USER" "$ENV_FILE"

# Права для singboxmgr на sing-box конфиг
chown "${SERVICE_USER}:${SERVICE_USER}" /etc/sing-box/ /etc/sing-box/config.json
chmod 750 /etc/sing-box/
chmod 640 /etc/sing-box/config.json
chown -R "${SERVICE_USER}:${SERVICE_USER}" "$APP_DIR"
chmod 755 "$APP_DIR"

# sudoers для управления sing-box без пароля
cat > /etc/sudoers.d/singboxmgr << SUDOEOF
${SERVICE_USER} ALL=(ALL) NOPASSWD: \\
  /usr/bin/systemctl restart sing-box, \\
  /usr/bin/systemctl is-active sing-box, \\
  /usr/bin/systemctl show sing-box
SUDOEOF
chmod 440 /etc/sudoers.d/singboxmgr
success "Права настроены"

# Systemd для бота и веб-панели
cat > /etc/systemd/system/singbox-bot.service << SVCEOF
[Unit]
Description=Sing-box Telegram Bot
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_DIR}/venv/bin/python bot.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF

cat > /etc/systemd/system/singbox-web.service << SVCEOF
[Unit]
Description=Sing-box Web Panel
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_DIR}/venv/bin/python web.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF

# Инициализация БД
sudo -u "$SERVICE_USER" "${APP_DIR}/venv/bin/python" \
    -c "import db; db.init_db(); print('DB OK')"

systemctl daemon-reload
systemctl enable singbox-bot singbox-web sing-box
success "Systemd сервисы созданы и включены"

# =============================================================================
# ШАГ 7: Nginx
# =============================================================================
step "Настройка nginx"
rm -f /etc/nginx/sites-enabled/default

if [[ -n "$DOMAIN" ]]; then
    # HTTPS с доменом
    if [[ -n "$WEB_PREFIX" ]]; then
        PANEL_LOCATION="
    location /${WEB_PREFIX}/ {
        proxy_pass         http://127.0.0.1:5000/${WEB_PREFIX}/;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
    }
    location / { return 404; }"
    else
        PANEL_LOCATION="
    location / {
        proxy_pass         http://127.0.0.1:5000/;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
    }"
    fi

    cat > /etc/nginx/sites-available/singbox-manager << NGINXEOF
server {
    listen 80;
    server_name ${DOMAIN};
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 301 https://\$host\$request_uri; }
}

server {
    listen 443 ssl;
    server_name ${DOMAIN};

    ssl_certificate     ${CERTS_DIR}/fullchain.pem;
    ssl_certificate_key ${CERTS_DIR}/key.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    location /sub/ {
        proxy_pass       http://127.0.0.1:5000/sub/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
    location /config/ {
        proxy_pass       http://127.0.0.1:5000/config/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
${PANEL_LOCATION}
}
NGINXEOF
    ln -sf /etc/nginx/sites-available/singbox-manager \
           /etc/nginx/sites-enabled/singbox-manager

else
    # HTTP без домена (самоподписанный, только для теста)
    if [[ -n "$WEB_PREFIX" ]]; then
        PANEL_LOCATION="
    location /${WEB_PREFIX}/ {
        proxy_pass         http://127.0.0.1:5000/${WEB_PREFIX}/;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
    }
    location / { return 404; }"
    else
        PANEL_LOCATION="
    location / {
        proxy_pass         http://127.0.0.1:5000/;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
    }"
    fi

    cat > /etc/nginx/sites-available/singbox-manager << NGINXEOF
server {
    listen 80 default_server;
    server_name _;

    location /sub/ {
        proxy_pass       http://127.0.0.1:5000/sub/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
    location /config/ {
        proxy_pass       http://127.0.0.1:5000/config/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
${PANEL_LOCATION}
}
NGINXEOF
    ln -sf /etc/nginx/sites-available/singbox-manager \
           /etc/nginx/sites-enabled/singbox-manager
fi

nginx -t -q && systemctl restart nginx
success "Nginx настроен"

# =============================================================================
# ШАГ 8: Запуск
# =============================================================================
step "Запуск сервисов"
systemctl restart sing-box
sleep 2
systemctl restart singbox-web
sleep 1
systemctl restart singbox-bot

# =============================================================================
# Итог
# =============================================================================
echo ""
echo -e "${GREEN}${BOLD}"
echo "╔══════════════════════════════════════════════════╗"
echo "║            Установка завершена!                  ║"
echo "╚══════════════════════════════════════════════════╝"
echo -e "${NC}"

if [[ -n "$DOMAIN" ]]; then
    if [[ -n "$WEB_PREFIX" ]]; then
        PANEL_URL="https://${DOMAIN}/${WEB_PREFIX}/"
    else
        PANEL_URL="https://${DOMAIN}/"
    fi
else
    SERVER_IP=$(hostname -I | awk '{print $1}')
    if [[ -n "$WEB_PREFIX" ]]; then
        PANEL_URL="http://${SERVER_IP}/${WEB_PREFIX}/"
    else
        PANEL_URL="http://${SERVER_IP}/"
    fi
fi

echo -e "${BOLD}Сохраните эти данные:${NC}"
echo "┌─────────────────────────────────────────────────────────"
echo "│  OBFS пароль:       ${OBFS_PASSWORD}"
echo "│  WEB_PREFIX:        ${WEB_PREFIX:-'(без префикса)'}"
echo "│  Веб-панель:        ${PANEL_URL}"
echo "│  Логин панели:      admin"
echo "│  Пароль панели:     ${WEB_ADMIN_PASS}"
echo "└─────────────────────────────────────────────────────────"
echo ""
echo -e "${YELLOW}${BOLD}Обязательно заполните .env:${NC}"
echo "  nano ${APP_DIR}/.env"
echo "  Заполните: BOT_TOKEN, ADMIN_IDS"
echo "  OBFS_PASSWORD, WEB_PREFIX и WEB_ADMIN_PASS уже заполнены."
echo ""
echo -e "${BOLD}Полезные команды:${NC}"
echo "  systemctl status sing-box singbox-bot singbox-web"
echo "  journalctl -u singbox-bot -f"
echo "  journalctl -u singbox-web -f"
echo "  journalctl -u sing-box -f"
echo ""
echo "  # Если в config.json уже были пользователи — импортировать:"
echo "  ${APP_DIR}/venv/bin/python ${APP_DIR}/migrate_existing.py"
echo ""
