"""
clash_traffic.py — трафик и онлайн-статус через Clash API + лог sing-box.

Sing-box 1.11+ не пишет metadata.user в Clash API /connections.
Решение: парсим лог sing-box (работает на уровне INFO).

Формат лога (два события с одинаковым connection ID):
  INFO [3742399784 0ms] inbound/hysteria2[hysteria-in]: inbound connection from 185.69.185.1:61618
  INFO [3742399784 0ms] inbound/hysteria2[hysteria-in]: [TestUser] inbound connection to fonts.googleapis.com:443

Алгоритм:
  1. Читаем хвост лога, строим карту conn_id → {ip, username}
  2. Финальная карта: sourceIP → username (берём последнее известное имя для IP)
  3. Матчим активные соединения из Clash API по sourceIP
"""
import logging
import re
import urllib.request
import json
from collections import defaultdict
from datetime import datetime, timedelta

from config import CLASH_API_URL, SINGBOX_LOG_PATH

log = logging.getLogger(__name__)

# Кэш: sourceIP → (username, обновлено)
# Живёт 30 минут — перекрывает время между переподключениями
_ip_user_cache: dict[str, tuple[str, datetime]] = {}
_IP_CACHE_TTL = timedelta(minutes=30)

# Паттерны для парсинга лога
# "from IP:PORT" — строка с IP
_RE_FROM = re.compile(
    r'\[(\d+)\s+\d+ms\].*?inbound connection from (\d{1,3}(?:\.\d{1,3}){3}):\d+'
)
# "[Username] inbound connection to" — строка с именем
_RE_USER = re.compile(
    r'\[(\d+)\s+\d+ms\].*?\[([^\]]+)\] inbound connection to '
)


def _get(path: str, timeout: int = 5) -> dict | None:
    try:
        url = CLASH_API_URL.rstrip("/") + path
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        log.debug(f"[clash] {path}: {e}")
        return None


def is_available() -> bool:
    return _get("/version") is not None


def _build_ip_user_map(lines: int = 5000) -> dict[str, str]:
    """
    Читает хвост лога sing-box и строит карту {sourceIP: username}.

    Алгоритм:
      - Проход 1: собираем conn_id → ip  (строки "from IP:PORT")
      - Проход 2: собираем conn_id → username  (строки "[Name] inbound connection to")
      - Итог: ip → username через общий conn_id
    """
    id_to_ip:   dict[str, str] = {}
    id_to_user: dict[str, str] = {}

    try:
        with open(SINGBOX_LOG_PATH, "r", errors="replace") as f:
            all_lines = f.readlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
    except FileNotFoundError:
        log.warning(f"[clash] лог не найден: {SINGBOX_LOG_PATH}")
        return {}
    except Exception as e:
        log.debug(f"[clash] ошибка чтения лога: {e}")
        return {}

    for line in tail:
        if "inbound connection" not in line:
            continue
        m = _RE_FROM.search(line)
        if m:
            id_to_ip[m.group(1)] = m.group(2)
            continue
        m = _RE_USER.search(line)
        if m:
            id_to_user[m.group(1)] = m.group(2)

    # Собираем итоговую карту ip → username
    ip_user: dict[str, str] = {}
    for conn_id, ip in id_to_ip.items():
        user = id_to_user.get(conn_id)
        if user:
            ip_user[ip] = user

    if ip_user:
        log.debug(f"[clash] из лога: {ip_user}")
    else:
        log.debug("[clash] IP→user из лога не получено (нет совпадений)")

    return ip_user


def _get_connections_with_users() -> list[dict]:
    """Соединения Clash API с полем resolved_user."""
    data = _get("/connections")
    if not data:
        return []

    ip_user_map = _build_ip_user_map()
    now = datetime.utcnow()
    result = []

    for conn in data.get("connections", []):
        meta = conn.get("metadata", {})
        src_ip = meta.get("sourceIP", "")

        # Метод 1: metadata.user (sing-box ≤ 1.10)
        user = (meta.get("user") or "").strip()

        # Метод 2: лог sing-box (основной для 1.11+)
        if not user and src_ip:
            user = ip_user_map.get(src_ip, "")
            if user:
                _ip_user_cache[src_ip] = (user, now)

        # Метод 3: IP-кэш (если в последнем хвосте лога этот IP не встретился)
        if not user and src_ip:
            cached = _ip_user_cache.get(src_ip)
            if cached and (now - cached[1]) < _IP_CACHE_TTL:
                user = cached[0]
                log.debug(f"[clash] {src_ip} → {user} (кэш)")

        if not user:
            log.debug(
                f"[clash] user не определён: ip={src_ip} "
                f"type={meta.get('type')} "
                f"up={conn.get('upload')} down={conn.get('download')}"
            )

        conn["resolved_user"] = user
        result.append(conn)

    return result


# ── Публичный API ──────────────────────────────────────────────────────────

def get_online_users() -> set[str]:
    conns = _get_connections_with_users()
    return {c["resolved_user"] for c in conns if c.get("resolved_user")}


def get_traffic_by_user() -> dict[str, dict]:
    conns = _get_connections_with_users()
    traffic: dict[str, dict] = defaultdict(lambda: {"upload": 0, "download": 0})
    for conn in conns:
        user = conn.get("resolved_user") or "_unknown"
        traffic[user]["upload"]   += conn.get("upload", 0)
        traffic[user]["download"] += conn.get("download", 0)
    return dict(traffic)


def get_total_traffic_counters() -> tuple[int, int]:
    """(downloadTotal, uploadTotal) — суммарно с момента старта sing-box."""
    data = _get("/connections")
    if not data:
        return 0, 0
    return data.get("downloadTotal", 0), data.get("uploadTotal", 0)
