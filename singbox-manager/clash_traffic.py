"""
clash_traffic.py — получение трафика и онлайн-статуса через Clash API sing-box.

Как включить Clash API в sing-box:
Добавить в /etc/sing-box/config.json:
  "experimental": {
    "clash_api": {
      "external_controller": "127.0.0.1:9090",
      "secret": ""
    }
  }
Затем: systemctl restart sing-box
"""
import logging
import urllib.request
import urllib.error
import json
from collections import defaultdict

from config import CLASH_API_URL

log = logging.getLogger(__name__)


def _get(path: str, timeout: int = 5) -> dict | None:
    try:
        url = CLASH_API_URL.rstrip("/") + path
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        log.debug(f"[clash] {path}: {e}")
        return None


def is_available() -> bool:
    """Проверяет доступность Clash API."""
    return _get("/version") is not None


def get_online_users() -> set[str]:
    """
    Возвращает множество имён пользователей с активными соединениями.
    Clash API /connections содержит поле metadata.user для hysteria2.
    """
    data = _get("/connections")
    if not data:
        return set()
    online = set()
    for conn in data.get("connections", []):
        meta = conn.get("metadata", {})
        # sing-box пишет имя пользователя hysteria2 только в metadata.user.
        # Fallback на sourceIP убран: IP не совпадёт ни с одним username в БД.
        user = meta.get("user", "").strip()
        if user:
            online.add(user)
    return online


def get_traffic_by_user() -> dict[str, dict]:
    """
    Возвращает словарь {username: {upload: bytes, download: bytes}}
    из активных соединений Clash API.

    Внимание: Clash API показывает только АКТИВНЫЕ соединения.
    Для накопленного трафика нужно периодически суммировать и сохранять в БД.
    """
    data = _get("/connections")
    if not data:
        return {}
    traffic = defaultdict(lambda: {"upload": 0, "download": 0})
    for conn in data.get("connections", []):
        meta = conn.get("metadata", {})
        # Только metadata.user — это имя из конфига sing-box hysteria2.
        # sourceIP не используем: он не матчится с username в БД.
        user = meta.get("user", "").strip()
        if user:
            traffic[user]["upload"]   += conn.get("upload", 0)
            traffic[user]["download"] += conn.get("download", 0)
    return dict(traffic)
