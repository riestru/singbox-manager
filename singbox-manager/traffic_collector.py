"""
traffic_collector.py — сбор трафика через Clash API sing-box.

Как включить Clash API в /etc/sing-box/config.json:
добавить секцию "experimental" на верхний уровень:

  "experimental": {
    "clash_api": {
      "external_controller": "127.0.0.1:9090",
      "secret": ""
    }
  }

После этого перезапустить sing-box:
  systemctl restart sing-box
"""
import logging
import urllib.request
import urllib.error
import json
from datetime import datetime

import db
from config import CLASH_API_URL

log = logging.getLogger(__name__)

# Кэш: имя → (rx, tx) на прошлом опросе — для подсчёта дельты
_prev_stats: dict[str, tuple[int, int]] = {}


def fetch_clash_traffic() -> dict[str, tuple[int, int]]:
    """
    Запрашивает статистику соединений из Clash API.
    Возвращает {username: (rx_bytes, tx_bytes)} суммарно по всем соединениям.
    """
    try:
        url = f"{CLASH_API_URL}/connections"
        req = urllib.request.Request(url, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as e:
        log.debug(f"[traffic] Clash API недоступен: {e}")
        return {}
    except Exception as e:
        log.warning(f"[traffic] Ошибка запроса: {e}")
        return {}

    result: dict[str, tuple[int, int]] = {}
    for conn in data.get("connections", []):
        # sing-box пишет имя пользователя в metadata.user
        user = (conn.get("metadata") or {}).get("user", "")
        if not user:
            continue
        rx = conn.get("download", 0)
        tx = conn.get("upload", 0)
        if user in result:
            result[user] = (result[user][0] + rx, result[user][1] + tx)
        else:
            result[user] = (rx, tx)
    return result


def collect_and_save():
    """
    Вызывать периодически (каждые 30–60 сек).
    Считает дельту трафика с прошлого вызова и добавляет в БД.
    """
    global _prev_stats
    current = fetch_clash_traffic()
    if not current:
        return

    for username, (rx, tx) in current.items():
        prev_rx, prev_tx = _prev_stats.get(username, (0, 0))
        # Если текущее значение меньше предыдущего — соединение переоткрылось,
        # считаем весь текущий трафик новым.
        delta_rx = rx if rx < prev_rx else (rx - prev_rx)
        delta_tx = tx if tx < prev_tx else (tx - prev_tx)
        if delta_rx > 0 or delta_tx > 0:
            db.update_traffic(username, delta_rx, delta_tx)
            log.debug(f"[traffic] {username}: +{delta_rx}rx +{delta_tx}tx")

    _prev_stats = current


def get_online_users() -> set[str]:
    """
    Возвращает множество имён пользователей с активными соединениями прямо сейчас.
    """
    current = fetch_clash_traffic()
    return set(current.keys())


def check_clash_api() -> tuple[bool, str]:
    """Проверяет доступность Clash API. Возвращает (ok, сообщение)."""
    try:
        url = f"{CLASH_API_URL}/version"
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read())
        return True, data.get("version", "unknown")
    except Exception as e:
        return False, str(e)
