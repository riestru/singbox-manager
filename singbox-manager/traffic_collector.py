"""
traffic_collector.py — сбор трафика через Clash API sing-box.
Читает логи напрямую из файла /var/log/sing-box/sing-box.log,
сопоставляя conn_id между строками "from IP:PORT" и "[Username] inbound".
"""
import logging
import urllib.request
import urllib.error
import json
import re
import time
import os
from threading import Thread, Lock
import db
from config import CLASH_API_URL, SINGBOX_LOG_PATH

log = logging.getLogger(__name__)

# Кэш: имя → (rx, tx) на прошлом опросе
_prev_stats: dict[str, tuple[int, int]] = {}

# Карта "IP:port" → username (потокобезопасная)
_session_to_user: dict[str, str] = {}
_session_lock = Lock()

# Карта conn_id → временные данные для сопоставления
_conn_cache: dict[int, dict] = {}
_conn_lock = Lock()
_CONN_CACHE_TTL = 120  # секунды

# Позиция в файле логов
_log_file_position = 0
_log_file_inode = None

# Флаг и поток для logs-listener
_logs_thread: Thread | None = None
_logs_running = False


def _parse_log_line(line: str) -> dict | None:
    """
    Парсит строку лога sing-box:
    "+0500 2026-05-06 02:35:22 INFO [877358256 1ms] inbound/hysteria2[hysteria-in]: [Adelya] inbound connection to host:443"
    """
    try:
        line_stripped = line.strip()
        if not line_stripped:
            return None
        
        # Извлекаем conn_id: [1234567890 0ms]
        conn_match = re.search(r'\[(\d+)\s+\d+ms\]', line_stripped)
        if not conn_match:
            return None
        conn_id = int(conn_match.group(1))
        
        result = {"conn_id": conn_id}
        
        # Ищем source IP:port
        ip_match = re.search(r'from\s+([\d.]+):(\d+)', line_stripped)
        if ip_match:
            result["source_ip"] = ip_match.group(1)
            result["source_port"] = ip_match.group(2)
        
        # Ищем username в квадратных скобках
        user_match = re.search(r'\]:\s*\[([^\]]+)\]\s+inbound', line_stripped)
        if user_match:
            result["username"] = user_match.group(1).strip()
        
        return result if len(result) > 1 else None
    except Exception:
        return None


def _process_parsed(parsed: dict):
    """Обновляет кэш conn_id и при полном совпадении добавляет в _session_to_user."""
    conn_id = parsed.get("conn_id")
    if not conn_id:
        return
    
    now = time.time()
    
    with _conn_lock:
        entry = _conn_cache.get(conn_id, {})
        entry["updated"] = now
        
        if "source_ip" in parsed and "source_port" in parsed:
            entry["source_ip"] = parsed["source_ip"]
            entry["source_port"] = parsed["source_port"]
        if "username" in parsed:
            entry["username"] = parsed["username"]
        
        _conn_cache[conn_id] = entry
        
        # Если есть и IP и username — сохраняем в основной кэш
        if entry.get("source_ip") and entry.get("source_port") and entry.get("username"):
            session_key = f"{entry['source_ip']}:{entry['source_port']}"
            username = entry["username"]
            
            with _session_lock:
                _session_to_user[session_key] = username
            
            del _conn_cache[conn_id]
        
        # Чистим старые записи
        expired = [cid for cid, e in _conn_cache.items() 
                   if now - e.get("updated", 0) > _CONN_CACHE_TTL]
        for cid in expired:
            del _conn_cache[cid]
        
        # Ограничиваем размер кэша
        if len(_conn_cache) > 5000:
            oldest = sorted(_conn_cache.items(), key=lambda x: x[1].get("updated", 0))[:2500]
            for cid, _ in oldest:
                del _conn_cache[cid]


def _read_log_file():
    """Читает новые строки из файла логов sing-box с обработкой rotation."""
    global _log_file_position, _log_file_inode
    
    if not os.path.exists(SINGBOX_LOG_PATH):
        return []
    
    try:
        stat = os.stat(SINGBOX_LOG_PATH)
        current_inode = stat.st_ino
        
        if _log_file_inode is not None and _log_file_inode != current_inode:
            _log_file_position = 0
        _log_file_inode = current_inode
        
        with open(SINGBOX_LOG_PATH, 'r', encoding='utf-8', errors='ignore') as f:
            if _log_file_position > stat.st_size:
                _log_file_position = 0
            f.seek(_log_file_position)
            lines = f.readlines()
            _log_file_position = f.tell()
            return lines
    except Exception:
        return []


def _logs_listener():
    """Фоновый поток: читает файл логов и поддерживает карту session→username."""
    global _logs_running
    
    log.info(f"[traffic] logs-listener started, watching {SINGBOX_LOG_PATH}")
    
    while _logs_running:
        try:
            lines = _read_log_file()
            for line in lines:
                if not _logs_running:
                    break
                parsed = _parse_log_line(line)
                if parsed:
                    _process_parsed(parsed)
            time.sleep(0.3)
        except Exception as e:
            log.warning(f"[traffic] logs-listener error: {e}")
            if _logs_running:
                time.sleep(2)


def start_logs_listener():
    """Публичный API: запускает фоновый поток logs-listener (идемпотентный)."""
    global _logs_thread, _logs_running
    if _logs_thread and _logs_thread.is_alive():
        return
    _logs_running = True
    _logs_thread = Thread(target=_logs_listener, daemon=True, name="logs-listener")
    _logs_thread.start()
    log.info("[traffic] logs-listener started")


def stop_logs_listener():
    """Публичный API: останавливает фоновый поток logs-listener."""
    global _logs_running
    _logs_running = False


def fetch_clash_traffic() -> dict[str, tuple[int, int]]:
    """Запрашивает статистику соединений из Clash API."""
    try:
        url = f"{CLASH_API_URL}/connections"
        req = urllib.request.Request(url, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError:
        return {}
    except Exception:
        return {}
    
    result: dict[str, tuple[int, int]] = {}
    for conn in data.get("connections", []):
        meta = conn.get("metadata") or {}
        user = meta.get("user", "").strip()
        
        if not user:
            source_ip = meta.get("sourceIP", "").strip()
            source_port = meta.get("sourcePort", "").strip()
            if source_ip and source_port:
                session_key = f"{source_ip}:{source_port}"
                with _session_lock:
                    user = _session_to_user.get(session_key, "")
        
        if not user:
            continue
            
        rx = conn.get("download", 0) or 0
        tx = conn.get("upload", 0) or 0
        
        if user in result:
            result[user] = (result[user][0] + rx, result[user][1] + tx)
        else:
            result[user] = (rx, tx)
    
    return result


def collect_and_save():
    """Считает дельту трафика и добавляет в БД."""
    global _prev_stats
    current = fetch_clash_traffic()

    if not current:
        return

    total_delta_rx = 0
    total_delta_tx = 0

    for username, (rx, tx) in current.items():
        prev_rx, prev_tx = _prev_stats.get(username, (0, 0))
        delta_rx = rx if rx < prev_rx else (rx - prev_rx)
        delta_tx = tx if tx < prev_tx else (tx - prev_tx)

        if delta_rx > 0 or delta_tx > 0:
            db.update_traffic(username, delta_rx, delta_tx)
            total_delta_rx += delta_rx
            total_delta_tx += delta_tx
            log.info(f"[traffic] {username}: +{delta_rx}rx +{delta_tx}tx")

    if total_delta_rx > 0 or total_delta_tx > 0:
        try:
            db.add_server_traffic(total_delta_rx, total_delta_tx)
        except Exception as e:
            log.warning(f"[traffic] server_stats update failed: {e}")

    _prev_stats = current


def reset_user_stats(name: str):
    """
    Сбрасывает кэш _prev_stats для одного пользователя.
    Нужно вызывать при сбросе трафика в БД — иначе scheduler
    при следующем опросе восстановит старые значения обратно.
    """
    global _prev_stats
    if name in _prev_stats:
        del _prev_stats[name]
        log.info(f"[traffic] prev_stats сброшен для {name}")


def get_online_users() -> set[str]:
    """Возвращает множество имён пользователей с активными соединениями."""
    current = fetch_clash_traffic()
    return set(current.keys())


def check_clash_api() -> tuple[bool, str]:
    """Проверяет доступность Clash API."""
    try:
        url = f"{CLASH_API_URL}/version"
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read())
        return True, data.get("version", "unknown")
    except Exception as e:
        return False, str(e)


# Автозапуск logs-listener при импорте
start_logs_listener()
