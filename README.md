# Sing-box Manager

Система управления пользователями [sing-box](https://github.com/SagerNet/sing-box) (протокол Hysteria2) через Telegram-бот и веб-панель.

**Стек:** Python 3.11+ · aiogram 3.7 · Flask · SQLite · nginx

---

## Возможности

- Добавление, удаление, приостановка пользователей через Telegram-бот
- Лимиты трафика и срок действия аккаунтов
- QR-коды для URI подключения и подписки
- Отправка конфигурации клиенту на email
- Веб-панель с трафиком, статусом и QR-кодами
- Ссылки подписки (совместимы с NekoBox, Hiddify, sing-box)
- Онлайн-статус клиентов через парсинг лога sing-box
- Секретный путь к веб-панели (WEB_PREFIX)
- Поддержка кастомного домена и SNI для каждого пользователя

---

## Способ 1 — Автоматическая установка (рекомендуется)

Один скрипт устанавливает всё: sing-box, сертификат, nginx, singbox-manager.

```bash
curl -fsSL https://raw.githubusercontent.com/riestru/singbox-manager/main/singbox-manager-install.sh | bash
```

Или скачайте и запустите:

```bash
wget https://raw.githubusercontent.com/riestru/singbox-manager/main/singbox-manager-install.sh
bash singbox-manager-install.sh
```

Скрипт спросит:
- Домен (или Enter для самоподписанного сертификата)
- Email для Let's Encrypt (если домен указан)
- WEB_PREFIX (секретный путь, генерируется автоматически)

После установки заполните `/opt/singbox-manager/.env`:

```bash
nano /opt/singbox-manager/.env
```

Обязательно укажите:
```env
BOT_TOKEN=токен_от_BotFather
ADMIN_IDS=ваш_telegram_id    # узнать у @userinfobot
```

Остальное заполнено автоматически. Запустите сервисы:

```bash
systemctl restart singbox-bot singbox-web
```

---

## Способ 2 — Клонирование репозитория

### 2.1 Клонировать и настроить

```bash
git clone https://github.com/riestru/singbox-manager.git
cd singbox-manager/singbox-manager
```

### 2.2 Установить Python зависимости

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 2.3 Настроить .env

```bash
cp .env.example .env
nano .env
```

Минимальный набор:
```env
BOT_TOKEN=ваш_токен
ADMIN_IDS=ваш_telegram_id
SERVER_HOST=vpn.example.com
OBFS_PASSWORD=пароль_из_sing-box_config.json
WEB_BASE_URL=https://vpn.example.com
WEB_ADMIN_PASS=придумайте_пароль
WEB_PREFIX=секретный_путь
```

### 2.4 Инициализировать БД

```bash
./venv/bin/python -c "import db; db.init_db()"
```

### 2.5 Создать/отредактировать /etc/sudoers.d/singboxmgr

Выполнить `sudo visudo -f /etc/sudoers.d/singboxmgr`:

Удалить всё содержимое и вставить строго эти строки (начинать с самого края, без пробелов/табуляции слева):

```ini
Defaults:singboxmgr !use_pty
Defaults:singboxmgr !authenticate
singboxmgr ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart sing-box, /usr/bin/systemctl is-active sing-box, /usr/bin/systemctl show sing-box *
```

### 2.6 Создать systemd сервисы

Создайте `/etc/systemd/system/singbox-bot.service`:

```ini
[Unit]
Description=Sing-box Telegram Bot
After=network.target

[Service]
Type=simple
User=singboxmgr
WorkingDirectory=/opt/singbox-manager
EnvironmentFile=/opt/singbox-manager/.env
ExecStart=/opt/singbox-manager/venv/bin/python bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Аналогично `singbox-web.service` (замените `bot.py` на `web.py`).

```bash
systemctl daemon-reload
systemctl enable --now singbox-bot singbox-web
```

### 2.7 Импорт существующих пользователей из config.json

Если в sing-box уже есть пользователи:

```bash
./venv/bin/python migrate_existing.py
```

---

## Структура файлов

```
singbox-manager/
├── bot.py                  # Telegram-бот (aiogram 3.7)
├── web.py                  # Веб-панель (Flask)
├── manager.py              # Управление config.json + рестарт sing-box
├── db.py                   # SQLite: все операции с пользователями
├── scheduler.py            # Фоновые задачи: лимиты, онлайн-статус
├── log_parser.py           # Парсинг лога sing-box: онлайн + IP↔user
├── traffic_collector.py    # Сбор трафика через Clash API + лог
├── clash_traffic.py        # Клиент Clash API
├── email_sender.py         # Отправка конфига на email (SSL/STARTTLS)
├── config.py               # Настройки (читает из .env)
├── migrate_existing.py     # Импорт пользователей из config.json
├── requirements.txt
├── .env.example
└── templates/
    └── index.html          # Веб-панель UI
```

---

## Команды Telegram-бота

| Команда | Описание |
|---|---|
| `/start` | Главное меню |
| `/help` | Справка |
| `/add` | Добавить пользователя (пошагово) |
| `/edit [имя` | Редактировать пользователя (своё меню) |
| `/users` | Список пользователей |
| `/info [имя]` | Детальная информация + URI |
| `/stop [имя]` | Приостановить (с подтверждением) |
| `/start_user [имя]` | Возобновить |
| `/delete [имя]` | Удалить (с подтверждением) |
| `/qr [имя]` | QR URI и подписки |
| `/reset_traffic [имя]` | Сбросить счётчик трафика |
| `/set_limit [имя] [GB]` | Лимит трафика (0 = ∞) |
| `/set_expire [имя] [дни]` | Срок действия (0 = ∞) |
| `/send_email [имя]` | Отправить конфиг на email |
| `/set_ip [имя] [IP]` | Задать IP для online-статуса |
| `/status` | Статус sing-box и Clash API |
| `/cancel` | Отменить текущий ввод |

---

## Веб-панель

- Адрес: `https://домен/WEB_PREFIX/`
- Авторизация: HTTP Basic Auth (логин `admin`, пароль из `.env`)
- Показывает: статус сервиса, суммарный трафик, список пользователей с онлайн-статусом, QR-коды, ссылки на подписку и конфиг
- Кнопка 🔄 сброса трафика для каждого пользователя

---

## Трафик и онлайн-статус

**Онлайн-статус** определяется через парсинг лога `/var/log/sing-box/sing-box.log`. Пользователь считается онлайн если появлялся в логе за последние 2 минуты.

**Трафик по пользователям** — через Clash API + маппинг IP из лога. sing-box 1.13.x не передаёт имя пользователя в Clash API metadata, поэтому IP клиента из лога сопоставляется с трафиком соединения в Clash API.

Clash API включён в конфиге по умолчанию:
```json
"experimental": {
  "clash_api": {
    "external_controller": "127.0.0.1:9090",
    "secret": ""
  }
}
```

---

## Обновление

### Через скрипт:
```bash
curl -fsSL https://github.com/riestru/singbox-manager/releases/latest/download/singbox-manager.zip \
  -o /tmp/update.zip
cd /tmp && unzip -o update.zip
cp singbox-manager/*.py /opt/singbox-manager/
cp singbox-manager/templates/index.html /opt/singbox-manager/templates/
systemctl restart singbox-bot singbox-web
```

### Через git (способ 2):
```bash
cd /path/to/singbox-manager
git pull
cp singbox-manager/*.py /opt/singbox-manager/
systemctl restart singbox-bot singbox-web
```

---

## Полезные команды

```bash
# Статус сервисов
systemctl status sing-box singbox-bot singbox-web

# Логи в реальном времени
journalctl -u singbox-bot -f
journalctl -u singbox-web -f
journalctl -u sing-box -f

# Проверка Clash API
curl -s http://127.0.0.1:9090/version

# Активные соединения
curl -s http://127.0.0.1:9090/connections | python3 -m json.tool

# БД пользователей
sqlite3 /opt/singbox-manager/users.db "SELECT name, status, sub_token FROM users;"
```

---

## .env — все параметры

```env
# ── Telegram бот ───────────────────────────────────────────────────────────
BOT_TOKEN=
# ваш Telegram ID (узнать у @userinfobot)
ADMIN_IDS=123456789

# Прокси для Telegram бота (если Telegram заблокирован провайдером)
# Оставьте пустым если прокси не нужен
# Форматы:
#   HTTPS_PROXY=socks5://user:password@proxy_host:1080
#   HTTPS_PROXY=http://proxy_host:3128
HTTPS_PROXY=

# ── Sing-box сервер ────────────────────────────────────────────────────────
# Реальный адрес сервера — используется внутри системы (TLS-сертификат и т.п.)
# При переезде: обязательно обновите на адрес нового сервера
SERVER_HOST=your_domain_or_IP

# Порт sing-box (hysteria2). По умолчанию 443.
# При переезде: обновите если порт изменился
SERVER_PORT=443

# OBFS пароль — должен совпадать с obfs.password в /etc/sing-box/config.json
# При переезде: если пароль изменился — обновите здесь.
#   Новые URI клиентов будут строиться с новым паролем автоматически.
#   Старым клиентам нужно будет обновить конфиг.
OBFS_PASSWORD=ваш_obfs_пароль_из_config.json

# ── Адреса для клиентов (при переезде особенно важны) ─────────────────────

# Адрес для клиентских подключений (домен или IP в URI пользователя).
# Используйте, если клиентский адрес отличается от SERVER_HOST
# (например: другой домен, CDN, обратный прокси).
# Если оставить пустым — используется SERVER_HOST.
# При переезде: обновите на новый адрес/домен.
#   migrate_existing.py при запуске обновит адрес у всех существующих клиентов.
CLIENT_HOST=

# SNI по умолчанию для новых пользователей.
# Используйте, если SNI отличается от CLIENT_HOST
# (например: реальный домен при использовании CDN).
# Если оставить пустым — SNI не прописывается явно в URI
# (клиент использует хост из URI как SNI — стандартное поведение).
# При переезде: обновите если SNI изменился.
#   migrate_existing.py при запуске обновит SNI у всех существующих клиентов.
DEFAULT_SNI=

# Разрешить небезопасные сертификаты по умолчанию (insecure=1 в URI).
# true  — для самоподписанных сертификатов
# false — для Let's Encrypt и других доверенных сертификатов (рекомендуется)
# При переезде: migrate_existing.py обновит allow_insecure у всех клиентов.
DEFAULT_INSECURE=false

# ── Web-панель ─────────────────────────────────────────────────────────────
# При переезде: обновите на адрес нового сервера
WEB_BASE_URL=https://your_domain
WEB_SECRET_KEY=случайная_строка_минимум_32_символа
WEB_PORT=5000

# Логин/пароль для входа в панель
WEB_ADMIN_USER=admin
WEB_ADMIN_PASS=придумайте_сложный_пароль

# Секретный путь к панели (опционально, для безопасности)
# Если задан — панель открывается по https://domain/ВАШ_ПУТЬ/
# Если пусто — панель на https://domain/
# Пример: WEB_PREFIX=drLw/OVGkbM=
WEB_PREFIX=

# ── Подписка ───────────────────────────────────────────────────────────────
# Если хотите проксировать ссылку подписки через другой домен:
# SUB_URL_PREFIX=https://proxy.example.com/?url=
# Тогда ссылка будет: SUB_URL_PREFIX + основной_url
# Если пусто — ссылка подписки ведёт напрямую на WEB_BASE_URL
SUB_URL_PREFIX=

# ── Email (опционально) ────────────────────────────────────────────────────
# SMTP_FROM — строка "От кого" в письме.
# ВАЖНО: имя отправителя ОБЯЗАТЕЛЬНО в кавычках если содержит пробелы:
#   "Моя VPN" <user@mail.ru>   ← правильно
#   Моя VPN <user@mail.ru>     ← неправильно, mail.ru отклонит
# Порт 465 → SSL (mail.ru), порт 587 → STARTTLS (gmail, yandex)
SMTP_HOST=smtp.mail.ru
SMTP_PORT=465
SMTP_USER=ваш@mail.ru
SMTP_PASS=пароль_приложения
SMTP_FROM=<ваш@mail.ru>
```
