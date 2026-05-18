#!/bin/bash
# =============================================================================
# singbox-manager-install.sh
# Полная установка sing-box + singbox-manager на Ubuntu 24.04
#
# Способы запуска:
#   bash singbox-manager-install.sh
#   curl -fsSL https://raw.githubusercontent.com/riestru/singbox-manager/main/singbox-manager-install.sh -o install.sh && bash install.sh
#
# ВАЖНО: не запускайте через  curl ... | bash  — интерактивный ввод не работает.
# =============================================================================
set -euo pipefail

# ── Цвета ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERR]${NC}  $*"; exit 1; }
step()    { echo -e "\n${BOLD}══ $* ══${NC}"; }

# Читаем ввод всегда из /dev/tty — работает и при pipe, и при прямом запуске
tty_read() {
    local prompt="$1" varname="$2"
    local val
    read -rp "$prompt" val </dev/tty
    printf -v "$varname" '%s' "$val"
}

# ── Константы ─────────────────────────────────────────────────────────────
APP_DIR="/opt/singbox-manager"
SERVICE_USER="singboxmgr"
SINGBOX_VERSION="1.13.2"
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
# ШАГ 0: Pre-flight проверки
# =============================================================================
step "Pre-flight проверки"

PREFLIGHT_OK=true

# ── Проверка занятости портов ─────────────────────────────────────────────

check_port_tcp() {
    local port="$1"
    local owner
    owner=$(ss -tlnp "sport = :${port}" 2>/dev/null | awk 'NR>1 {print $NF}' | grep -oP 'users:\(\("\K[^"]+' | head -1 || true)
    if ss -tlnp "sport = :${port}" 2>/dev/null | grep -q ":${port}"; then
        warn "TCP порт ${port} занят (процесс: ${owner:-неизвестен})"
        echo false
    else
        echo true
    fi
}

check_port_udp() {
    local port="$1"
    local owner
    owner=$(ss -ulnp "sport = :${port}" 2>/dev/null | awk 'NR>1 {print $NF}' | grep -oP 'users:\(\("\K[^"]+' | head -1 || true)
    if ss -ulnp "sport = :${port}" 2>/dev/null | grep -q ":${port}"; then
        warn "UDP порт ${port} занят (процесс: ${owner:-неизвестен})"
        echo false
    else
        echo true
    fi
}

PORT80_FREE=$(check_port_tcp 80)
PORT443_TCP_FREE=$(check_port_tcp 443)
PORT443_UDP_FREE=$(check_port_udp 443)

# ── Проверка nginx ────────────────────────────────────────────────────────

NGINX_INSTALLED=false
NGINX_ACTIVE=false
NGINX_WILL_CONFLICT=false

if command -v nginx &>/dev/null; then
    NGINX_INSTALLED=true
    if systemctl is-active --quiet nginx 2>/dev/null; then
        NGINX_ACTIVE=true
        # nginx занимает порт 80/443 — это нормально для нашего сценария,
        # мы будем его конфигурировать. Но если порт занят чем-то другим — проблема.
        info "nginx уже установлен и запущен"
    else
        info "nginx установлен, но не запущен"
    fi
else
    info "nginx не установлен — будет установлен"
fi

# ── Проверка конфликта портов (не nginx) ─────────────────────────────────
# Порт 80: если занят не nginx — это проблема
if [[ "$PORT80_FREE" == "false" ]]; then
    PORT80_PROC=$(ss -tlnp "sport = :80" 2>/dev/null | awk 'NR>1 {print $NF}' | grep -oP 'users:\(\("\K[^"]+' | head -1 || true)
    if [[ "$PORT80_PROC" != "nginx" ]] && [[ "$NGINX_ACTIVE" == "false" ]]; then
        warn "TCP 80 занят процессом '${PORT80_PROC:-?}' — не nginx. Это может помешать выпуску сертификата."
        PREFLIGHT_OK=false
    else
        info "TCP 80 занят nginx (штатно, будет перенастроен)"
    fi
fi

# Порт UDP 443: должен быть свободен для sing-box (hysteria2)
if [[ "$PORT443_UDP_FREE" == "false" ]]; then
    PORT443U_PROC=$(ss -ulnp "sport = :443" 2>/dev/null | awk 'NR>1 {print $NF}' | grep -oP 'users:\(\("\K[^"]+' | head -1 || true)
    # sing-box уже запущен — он будет остановлен и перезапущен, это нормально
    if [[ "$PORT443U_PROC" == "sing-box" ]]; then
        info "UDP 443 занят sing-box (будет перезапущен)"
    else
        warn "UDP 443 занят процессом '${PORT443U_PROC:-?}'. Sing-box (hysteria2) не сможет запуститься!"
        PREFLIGHT_OK=false
    fi
fi

# Порт TCP 443: nginx будет его слушать, если nginx активен — OK
if [[ "$PORT443_TCP_FREE" == "false" ]]; then
    PORT443T_PROC=$(ss -tlnp "sport = :443" 2>/dev/null | awk 'NR>1 {print $NF}' | grep -oP 'users:\(\("\K[^"]+' | head -1 || true)
    if [[ "$PORT443T_PROC" == "nginx" ]]; then
        info "TCP 443 занят nginx (штатно, будет перенастроен)"
    else
        warn "TCP 443 занят процессом '${PORT443T_PROC:-?}'. Nginx не сможет запуститься на 443!"
        PREFLIGHT_OK=false
    fi
fi

# ── Проверка уже существующих компонентов (повторная установка) ────────────

REINSTALL=false

if [[ -f "$SINGBOX_CONFIG" ]]; then
    warn "Найден существующий config.json: ${SINGBOX_CONFIG}"
    warn "При продолжении он будет ПЕРЕЗАПИСАН. Сохраните пользователей!"
    REINSTALL=true
fi

if [[ -d "$APP_DIR" ]]; then
    warn "Директория ${APP_DIR} уже существует — это повторная установка"
    REINSTALL=true
fi

if [[ -d "$HOME/.acme.sh" ]]; then
    # Проверяем, работоспособен ли acme.sh
    if [[ ! -x "$HOME/.acme.sh/acme.sh" ]]; then
        warn "acme.sh найден в ${HOME}/.acme.sh, но повреждён (нет исполняемого файла)"
        warn "Будет выполнена принудительная переустановка acme.sh"
        ACME_BROKEN=true
    else
        info "acme.sh уже установлен и работоспособен"
        ACME_BROKEN=false
    fi
else
    ACME_BROKEN=false
fi

# ── Итог pre-flight ───────────────────────────────────────────────────────

echo ""
if [[ "$PREFLIGHT_OK" == "false" ]]; then
    echo -e "${RED}${BOLD}Обнаружены критические проблемы (см. предупреждения выше).${NC}"
    echo -e "Рекомендуется устранить их до продолжения."
    echo ""
    tty_read "Продолжить несмотря на предупреждения? [yes/N]: " _force_continue
    if [[ "${_force_continue,,}" != "yes" ]]; then
        error "Установка прервана пользователем."
    fi
    warn "Продолжаем по запросу пользователя..."
else
    if [[ "$REINSTALL" == "true" ]]; then
        echo -e "${YELLOW}${BOLD}Обнаружена предыдущая установка. Будет выполнена переустановка.${NC}"
    else
        success "Pre-flight проверки пройдены"
    fi
fi

# =============================================================================
# ШАГ 1: Запрос параметров
# =============================================================================
step "Параметры установки"

# Домен
echo -e "\n${BOLD}Домен${NC} (например: vpn.example.com)"
echo "Если домена нет — нажмите Enter (будет самоподписанный сертификат)"
tty_read "Домен: " DOMAIN
DOMAIN="${DOMAIN// /}"

# Email (только если домен задан)
ACME_EMAIL=""
if [[ -n "$DOMAIN" ]]; then
    echo -e "\n${BOLD}Email${NC} для Let's Encrypt (acme.sh):"
    while true; do
        tty_read "Email: " ACME_EMAIL
        [[ "$ACME_EMAIL" =~ ^[^@]+@[^@]+\.[^@]+$ ]] && break
        warn "Некорректный email, попробуйте снова"
    done
fi

# WEB_PREFIX
echo -e "\n${BOLD}Секретный путь к веб-панели${NC} (WEB_PREFIX)"
echo "Защищает панель — она будет доступна только по https://домен/PREFIX/"
AUTO_PREFIX=$(openssl rand -base64 8 | tr -dc 'a-zA-Z0-9' | head -c 12)
echo -e "Сгенерированный: ${CYAN}${AUTO_PREFIX}${NC}"
echo "Нажмите Enter чтобы использовать его, введите свой, или '-' для отключения (панель на /):"
tty_read "WEB_PREFIX: " CUSTOM_PREFIX

if [[ -z "$CUSTOM_PREFIX" ]]; then
    WEB_PREFIX="$AUTO_PREFIX"
    info "Используется: ${WEB_PREFIX}"
elif [[ "$CUSTOM_PREFIX" == "-" ]]; then
    WEB_PREFIX=""
    warn "Панель будет доступна на / без защиты путём"
else
    WEB_PREFIX="$CUSTOM_PREFIX"
    info "Используется: ${WEB_PREFIX}"
fi

# Генерация паролей
OBFS_PASSWORD=$(openssl rand -base64 16 | tr -dc 'a-zA-Z0-9+/' | head -c 22)
WEB_SECRET_KEY=$(openssl rand -hex 24)
WEB_ADMIN_PASS=$(openssl rand -base64 12 | tr -dc 'a-zA-Z0-9' | head -c 16)

echo -e "\n${BOLD}Параметры установки:${NC}"
echo "  Домен:          ${DOMAIN:-'нет (самоподписанный сертификат)'}"
echo "  OBFS пароль:    ${OBFS_PASSWORD}"
echo "  WEB_PREFIX:     ${WEB_PREFIX:-'(пусто — панель на /)'}"
echo "  WEB_ADMIN_PASS: ${WEB_ADMIN_PASS}"
echo ""
tty_read "Продолжить? [Enter / Ctrl+C для отмены]: " _confirm

# =============================================================================
# ШАГ 2: Системные пакеты
# =============================================================================
step "Системные пакеты"
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    nginx curl wget unzip socat sqlite3 openssl cron
success "Пакеты установлены"

# Убеждаемся, что cron запущен (нужен для acme.sh авторенью)
systemctl enable cron --quiet 2>/dev/null || true
systemctl start cron 2>/dev/null || true

# =============================================================================
# ШАГ 3: Sing-box
# =============================================================================
step "Установка sing-box ${SINGBOX_VERSION}"

# Останавливаем sing-box если запущен
if systemctl is-active --quiet sing-box 2>/dev/null; then
    info "Останавливаем существующий sing-box..."
    systemctl stop sing-box
fi

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
success "sing-box ${SINGBOX_VERSION} → ${SINGBOX_BIN}"

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

    # ── Останавливаем nginx чтобы освободить порт 80 для acme challenge ──
    # (или настраиваем временный конфиг — второй способ надёжнее)
    if systemctl is-active --quiet nginx 2>/dev/null; then
        info "nginx активен — настраиваем временный конфиг для acme challenge"
        rm -f /etc/nginx/sites-enabled/default
        cat > /etc/nginx/sites-available/acme-temp << NGINXEOF
server {
    listen 80;
    server_name ${DOMAIN};
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 444; }
}
NGINXEOF
        # Убираем все конфиги кроме временного
        find /etc/nginx/sites-enabled/ -type l -not -name 'acme-temp' -delete 2>/dev/null || true
        ln -sf /etc/nginx/sites-available/acme-temp /etc/nginx/sites-enabled/acme-temp
        nginx -t -q && systemctl reload nginx
    else
        # nginx не запущен — остановим его чтобы порт 80 был свободен,
        # и запустим временный конфиг
        systemctl stop nginx 2>/dev/null || true
        rm -f /etc/nginx/sites-enabled/default
        cat > /etc/nginx/sites-available/acme-temp << NGINXEOF
server {
    listen 80;
    server_name ${DOMAIN};
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 444; }
}
NGINXEOF
        ln -sf /etc/nginx/sites-available/acme-temp /etc/nginx/sites-enabled/acme-temp
        nginx -t -q && systemctl start nginx
    fi

    # ── Проверяем доступность порта 80 снаружи ───────────────────────────
    sleep 1
    if ! ss -tlnp "sport = :80" 2>/dev/null | grep -q ":80"; then
        error "Порт 80 не слушается после запуска nginx. Проверьте конфигурацию."
    fi
    info "Порт 80 доступен — продолжаем"

    # ── Установка / переустановка acme.sh ────────────────────────────────
    _install_acme() {
        info "Устанавливаем acme.sh (с --force)..."
        curl -fsSL https://get.acme.sh | sh -s email="$ACME_EMAIL" --no-profile --force
    }

    if [[ "${ACME_BROKEN:-false}" == "true" ]]; then
        # Папка битая — удаляем и ставим заново
        warn "Удаляем повреждённую установку acme.sh..."
        rm -rf "$HOME/.acme.sh"
        _install_acme
    elif [[ ! -f "$HOME/.acme.sh/acme.sh" ]]; then
        # Просто нет — устанавливаем
        _install_acme
    else
        # Проверяем работоспособность
        if ! "$HOME/.acme.sh/acme.sh" --version &>/dev/null; then
            warn "acme.sh установлен, но не отвечает на --version. Переустанавливаем..."
            rm -rf "$HOME/.acme.sh"
            _install_acme
        else
            info "acme.sh уже работоспособен, переустановка не нужна"
        fi
    fi

    export PATH="$HOME/.acme.sh:$PATH"

    # Финальная проверка
    if [[ ! -x "$HOME/.acme.sh/acme.sh" ]]; then
        error "acme.sh так и не установился. Проверьте сеть и права доступа."
    fi

    # ── Выпуск сертификата ────────────────────────────────────────────────
    # --force переиздаёт даже если сертификат уже есть (при повторной установке)
    "$HOME/.acme.sh/acme.sh" --issue -d "$DOMAIN" --webroot /var/www/html --force \
      || error "Не удалось получить сертификат для ${DOMAIN}. Проверьте DNS (dig ${DOMAIN} +short) и порт 80."

    "$HOME/.acme.sh/acme.sh" --install-cert -d "$DOMAIN" \
        --cert-file      "${CERTS_DIR}/cert.pem" \
        --key-file       "${CERTS_DIR}/key.pem" \
        --fullchain-file "${CERTS_DIR}/fullchain.pem" \
        --reloadcmd      "systemctl reload nginx"

    # Убираем временный конфиг acme
    rm -f /etc/nginx/sites-enabled/acme-temp /etc/nginx/sites-available/acme-temp
    success "Сертификат получен → ${CERTS_DIR}"
    CERT_PATH="${CERTS_DIR}/fullchain.pem"
    KEY_PATH="${CERTS_DIR}/key.pem"
else
    warn "Домен не задан — генерируем самоподписанный сертификат (для теста)"
    openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
        -keyout "${CERTS_DIR}/key.pem" \
        -out    "${CERTS_DIR}/fullchain.pem" \
        -subj   "/CN=sing-box-vpn" 2>/dev/null
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
success "config.json создан (OBFS: ${OBFS_PASSWORD})"

# =============================================================================
# ШАГ 6: Установка singbox-manager
# =============================================================================
step "Установка singbox-manager"

# Останавливаем сервисы если запущены
for svc in singbox-bot singbox-web; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        info "Останавливаем ${svc}..."
        systemctl stop "$svc"
    fi
done

# Системный пользователь
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd -r -s /bin/false "$SERVICE_USER"
    success "Пользователь ${SERVICE_USER} создан"
else
    info "Пользователь ${SERVICE_USER} уже существует"
fi

# Скачиваем и распаковываем
info "Скачиваем singbox-manager..."
curl -fsSL -o /tmp/singbox-manager.zip "$MANAGER_ZIP_URL"
unzip -qo /tmp/singbox-manager.zip -d /opt
rm -f /tmp/singbox-manager.zip
mkdir -p "${APP_DIR}/templates"
success "Файлы распакованы → ${APP_DIR}"

# Python venv
info "Устанавливаем Python зависимости..."
python3 -m venv "${APP_DIR}/venv"
"${APP_DIR}/venv/bin/pip" install -q --upgrade pip
"${APP_DIR}/venv/bin/pip" install -q -r "${APP_DIR}/requirements.txt"
success "Python venv готов"

# .env
ENV_FILE="${APP_DIR}/.env"
[[ -f "${APP_DIR}/.env.example" ]] && cp "${APP_DIR}/.env.example" "$ENV_FILE" || touch "$ENV_FILE"

_set_env() {
    local key="$1" val="$2"
    if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
    else
        echo "${key}=${val}" >> "$ENV_FILE"
    fi
}

_set_env "OBFS_PASSWORD"  "$OBFS_PASSWORD"
_set_env "WEB_PREFIX"     "$WEB_PREFIX"
_set_env "WEB_SECRET_KEY" "$WEB_SECRET_KEY"
_set_env "WEB_ADMIN_PASS" "$WEB_ADMIN_PASS"
if [[ -n "$DOMAIN" ]]; then
    _set_env "SERVER_HOST"  "$DOMAIN"
    _set_env "WEB_BASE_URL" "https://${DOMAIN}"
fi

chmod 600 "$ENV_FILE"

# Права для singboxmgr
chown "${SERVICE_USER}:${SERVICE_USER}" /etc/sing-box/ /etc/sing-box/config.json
chmod 750 /etc/sing-box/
chmod 640 /etc/sing-box/config.json
chown -R "${SERVICE_USER}:${SERVICE_USER}" "$APP_DIR"
chmod 755 "$APP_DIR"

# sudoers
cat > /etc/sudoers.d/singboxmgr << SUDOEOF
Defaults:${SERVICE_USER} !use_pty
${SERVICE_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart sing-box, /usr/bin/systemctl is-active sing-box, /usr/bin/systemctl show sing-box
SUDOEOF
chmod 440 /etc/sudoers.d/singboxmgr
success "Права настроены"

# Systemd сервисы
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
    -c "import sys; sys.path.insert(0,'${APP_DIR}'); import db; db.init_db(); print('DB OK')"

systemctl daemon-reload
systemctl enable singbox-bot singbox-web sing-box
success "Systemd сервисы созданы и включены"

# =============================================================================
# ШАГ 7: Nginx
# =============================================================================
step "Настройка nginx"
rm -f /etc/nginx/sites-enabled/default

# Формируем блок location для панели
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

if [[ -n "$DOMAIN" ]]; then
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
else
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
fi

ln -sf /etc/nginx/sites-available/singbox-manager \
       /etc/nginx/sites-enabled/singbox-manager
nginx -t && systemctl restart nginx
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
    PANEL_URL="https://${DOMAIN}/${WEB_PREFIX:+${WEB_PREFIX}/}"
else
    SERVER_IP=$(hostname -I | awk '{print $1}')
    PANEL_URL="http://${SERVER_IP}/${WEB_PREFIX:+${WEB_PREFIX}/}"
fi

echo -e "${BOLD}Сохраните эти данные:${NC}"
echo "┌──────────────────────────────────────────────────────"
echo "│  OBFS пароль:    ${OBFS_PASSWORD}"
echo "│  WEB_PREFIX:     ${WEB_PREFIX:-'(без префикса)'}"
echo "│  Веб-панель:     ${PANEL_URL}"
echo "│  Логин панели:   admin"
echo "│  Пароль панели:  ${WEB_ADMIN_PASS}"
echo "└──────────────────────────────────────────────────────"
echo ""
echo -e "${YELLOW}${BOLD}Обязательно заполните .env:${NC}"
echo "  nano ${APP_DIR}/.env"
echo "  → Заполните: BOT_TOKEN, ADMIN_IDS"
echo "  → OBFS_PASSWORD, WEB_PREFIX, WEB_ADMIN_PASS — уже заполнены"
echo ""
echo -e "${BOLD}Полезные команды:${NC}"
echo "  systemctl status sing-box singbox-bot singbox-web"
echo "  journalctl -u singbox-bot -f"
echo "  journalctl -u singbox-web -f"
echo "  journalctl -u sing-box -f"
echo ""
echo "  # Если в sing-box уже были пользователи — импортировать в БД:"
echo "  ${APP_DIR}/venv/bin/python ${APP_DIR}/migrate_existing.py"
echo ""
