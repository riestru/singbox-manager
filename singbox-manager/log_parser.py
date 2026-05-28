"""
log_parser.py — парсинг лога sing-box для определения:
  1. Online-статуса (пользователь активен если был в логе < N минут назад)
  2. Текущего IP пользователя (для сопоставления с Clash API трафиком)

Формат строк лога sing-box:
  +0500 2026-05-05 02:29:25 INFO [674989939 0ms] inbound/hysteria2[hysteria-in]: [TestUser] inbound connection to ads.mozilla.org:443
  +0500 2026-05-05 02:29:38 INFO [4264346439 0ms] inbound/hysteria2[hysteria-in]: inbound connection from 185.69.185.1:61623
"""
import re
import os
import logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict

from config import SINGBOX_LOG_PATH

log = logging.getLogger(__name__)

# Паттерны
_RE_TIMESTAMP = re.compile(
    r'[+-]\d{4}\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})'
)
_RE_USER = re.compile(
    r'inbound/hysteria2\[.*?\]:\s+\[(\w+)\]\s+inbound connection'
)
_RE_IP_FROM = re.compile(
    r'inbound/hysteria2\[.*?\]:\s+inbound connection from\s+([\d\.]+):\d+'
)

# Сколько байт читаем с конца лога (последние ~500 строк)
_TAIL_BYTES = 64 * 1024  # 64 KB


def _read_tail(path: str, n_bytes: int) -> str:
    """Читает последние n_bytes из файла."""
    try:
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            if size > n_bytes:
                f.seek(size - n_bytes)
                f.readline()  # пропускаем неполную первую строку
            return f.read()
    except Exception as e:
        log.warning(f"[log_parser] cannot read {path}: {e}")
        return ""


def parse_log(online_window_seconds: int = 120) -> tuple[set[str], dict[str, str]]:
    """
    Парсит хвост лога sing-box.
    
    Возвращает:
      online_users: set[str]  — имена пользователей активных за последние N секунд
      ip_to_user:   dict[str, str] — {ip: username} последний известный IP каждого
    
    Алгоритм: проходим строки парами — строка с IP идёт перед строкой с [Username].
    Запоминаем последний IP и последнее время активности для каждого пользователя.
    """
    content = _read_tail(SINGBOX_LOG_PATH, _TAIL_BYTES)
    if not content:
        return set(), {}

    lines = content.splitlines()
    now_utc = datetime.utcnow()
    threshold = now_utc - timedelta(seconds=online_window_seconds)

    # {username: last_seen_dt}
    last_seen: dict[str, datetime] = {}
    # {username: last_ip}
    ip_to_user: dict[str, str] = {}
    user_to_ip: dict[str, str] = {}

    last_ip: str = ""
    last_ts:  datetime = None

    for line in lines:
        # Пробуем извлечь timestamp
        ts_m = _RE_TIMESTAMP.search(line)
        if ts_m:
            try:
                last_ts = datetime.strptime(ts_m.group(1), "%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

        # Строка с IP: "inbound connection from X.X.X.X:port"
        ip_m = _RE_IP_FROM.search(line)
        if ip_m:
            last_ip = ip_m.group(1)
            continue

        # Строка с username: "[User] inbound connection to ..."
        user_m = _RE_USER.search(line)
        if user_m and last_ts:
            username = user_m.group(1)
            # Обновляем время последней активности
            if username not in last_seen or last_seen[username] < last_ts:
                last_seen[username] = last_ts
            # Обновляем IP если есть свежий
            if last_ip:
                user_to_ip[username] = last_ip
                ip_to_user[last_ip]  = username
            last_ip = ""  # сбрасываем — IP уже привязан

    # Определяем кто онлайн
    online = {
        user for user, ts in last_seen.items()
        if ts >= threshold
    }

    return online, ip_to_user


def get_online_users(online_window_seconds: int = 120) -> set[str]:
    online, _ = parse_log(online_window_seconds)
    return online


def get_ip_user_map() -> dict[str, str]:
    """Возвращает {ip: username} из лога — для сопоставления с Clash API."""
    _, ip_to_user = parse_log()
    return ip_to_user
