"""
manager.py — управление пользователями sing-box.
"""
import json
import os
import secrets
import subprocess
import base64
from datetime import datetime, timedelta

import db
from config import (
    SINGBOX_CONFIG_PATH, SINGBOX_SERVICE,
    SERVER_HOST, SERVER_PORT, OBFS_PASSWORD,
    DEFAULT_TRAFFIC_LIMIT_GB, DEFAULT_EXPIRE_DAYS,
    WEB_BASE_URL, SUB_URL_PREFIX,
)


# ── Утилиты ────────────────────────────────────────────────────────────────

def gen_password() -> str:
    return base64.b64encode(secrets.token_bytes(16)).decode()

def gen_sub_token() -> str:
    return secrets.token_urlsafe(24)

def bytes_to_gb(b: int) -> float:
    return round(b / (1024 ** 3), 3)

def gb_to_bytes(gb: float) -> int:
    return int(gb * (1024 ** 3))

def fmt_bytes(b: int) -> str:
    if b < 1024:       return f"{b} B"
    if b < 1024**2:    return f"{b/1024:.1f} KB"
    if b < 1024**3:    return f"{b/1024**2:.1f} MB"
    return f"{b/1024**3:.2f} GB"


# ── Конфигурация sing-box ──────────────────────────────────────────────────

def read_config() -> dict:
    with open(SINGBOX_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def write_config(cfg: dict):
    tmp = SINGBOX_CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, SINGBOX_CONFIG_PATH)

def restart_singbox() -> tuple[bool, str]:
    """Перезапускает sing-box через systemctl restart."""
    try:
        r = subprocess.run(
            ["systemctl", "restart", SINGBOX_SERVICE],
            capture_output=True, text=True, timeout=25
        )
        if r.returncode == 0:
            return True, "restarted"
        return False, r.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, str(e)

# Оставляем псевдоним для обратной совместимости
reload_singbox = restart_singbox

def get_inbound(cfg: dict) -> dict:
    for inb in cfg.get("inbounds", []):
        if inb.get("type") == "hysteria2":
            return inb
    raise ValueError("hysteria2 inbound not found in config")

def ensure_clash_api(cfg: dict):
    """Добавляет секцию Clash API в конфиг если её нет."""
    from config import CLASH_API_URL
    host_port = CLASH_API_URL.replace("http://", "")
    if "experimental" not in cfg:
        cfg["experimental"] = {}
    if "clash_api" not in cfg["experimental"]:
        cfg["experimental"]["clash_api"] = {
            "external_controller": host_port,
            "secret": ""
        }
        return True   # конфиг изменён
    return False

def sync_config_from_db():
    """Синхронизирует users в config.json из БД."""
    users = db.get_all_users()
    active_users = [
        {"name": u["name"], "password": u["password"]}
        for u in users
        if u["status"] == "active" and not is_expired(u)
    ]
    cfg = read_config()
    inb = get_inbound(cfg)
    inb["users"] = active_users
    write_config(cfg)
    return active_users

def enable_clash_api():
    """Включает Clash API в config.json и перезапускает sing-box."""
    cfg = read_config()
    changed = ensure_clash_api(cfg)
    if changed:
        write_config(cfg)
        return restart_singbox()
    return True, "already enabled"


# ── Проверки ──────────────────────────────────────────────────────────────

def is_expired(user: dict) -> bool:
    if not user.get("expire_at"):
        return False
    try:
        return datetime.utcnow() > datetime.fromisoformat(user["expire_at"])
    except Exception:
        return False

def is_over_limit(user: dict) -> bool:
    limit_gb = user.get("traffic_limit_gb", 0)
    if not limit_gb:
        return False
    used = user.get("traffic_used_rx", 0) + user.get("traffic_used_tx", 0)
    return used >= gb_to_bytes(limit_gb)


# ── CRUD пользователей ────────────────────────────────────────────────────

def add_user(name: str, email: str = None, server_host: str = None,
             traffic_limit_gb: float = None, expire_days: int = None,
             notes: str = None) -> dict:
    if db.get_user_by_name(name):
        raise ValueError(f"Пользователь «{name}» уже существует")

    password      = gen_password()
    sub_token     = gen_sub_token()
    traffic_limit_gb = traffic_limit_gb if traffic_limit_gb is not None else DEFAULT_TRAFFIC_LIMIT_GB
    expire_days      = expire_days if expire_days is not None else DEFAULT_EXPIRE_DAYS
    expire_at     = None
    if expire_days:
        expire_at = (datetime.utcnow() + timedelta(days=expire_days)).isoformat()

    # server_host хранится в notes-extended или отдельном поле;
    # пока храним в поле notes если задан отдельно
    full_notes = notes or ""
    if server_host and server_host != SERVER_HOST:
        full_notes = f"[host:{server_host}] {full_notes}".strip()

    user = db.add_user(
        name=name, password=password, email=email,
        traffic_limit_gb=traffic_limit_gb, expire_at=expire_at,
        sub_token=sub_token,
        notes=full_notes or None,
    )
    # Сохраняем server_host для URI
    user["_server_host"] = server_host or SERVER_HOST
    sync_config_from_db()
    restart_singbox()
    return user

def suspend_user(name: str) -> bool:
    if not db.get_user_by_name(name):
        raise ValueError(f"Пользователь «{name}» не найден")
    db.update_user_status(name, "suspended")
    sync_config_from_db()
    ok, msg = restart_singbox()
    return ok

def activate_user(name: str) -> bool:
    if not db.get_user_by_name(name):
        raise ValueError(f"Пользователь «{name}» не найден")
    db.update_user_status(name, "active")
    sync_config_from_db()
    ok, _ = restart_singbox()
    return ok

def delete_user(name: str) -> bool:
    if not db.get_user_by_name(name):
        raise ValueError(f"Пользователь «{name}» не найден")
    db.update_user_status(name, "deleted")
    sync_config_from_db()
    ok, _ = restart_singbox()
    return ok

def set_traffic_limit(name: str, limit_gb: float):
    db.update_user_field(name, "traffic_limit_gb", limit_gb)

def set_expire(name: str, days: int):
    if days <= 0:
        db.update_user_field(name, "expire_at", None)
    else:
        expire_at = (datetime.utcnow() + timedelta(days=days)).isoformat()
        db.update_user_field(name, "expire_at", expire_at)

def set_email(name: str, email: str):
    db.update_user_field(name, "email", email)

def reset_traffic(name: str):
    db.reset_traffic(name)

def list_users(online_set: set = None) -> list[dict]:
    users = db.get_all_users()
    result = []
    for u in users:
        u["expired"]            = is_expired(u)
        u["over_limit"]         = is_over_limit(u)
        u["traffic_used_total"] = u["traffic_used_rx"] + u["traffic_used_tx"]
        u["traffic_used_gb"]    = bytes_to_gb(u["traffic_used_total"])
        u["online"]             = u["name"] in online_set if online_set is not None else False
        result.append(u)
    return result


# ── URI и ссылки ──────────────────────────────────────────────────────────

def _get_user_host(user: dict) -> str:
    """Извлекает host из заметки [host:...] или берёт дефолт."""
    notes = user.get("notes") or ""
    if notes.startswith("[host:"):
        end = notes.index("]")
        return notes[6:end]
    return SERVER_HOST

def build_hy2_uri(user: dict, host: str = None) -> str:
    h = host or _get_user_host(user)
    obfs = f"&obfs=salamander&obfs-password={OBFS_PASSWORD}" if OBFS_PASSWORD else ""
    return (
        f"hysteria2://{user['password']}@{h}:{SERVER_PORT}"
        f"?insecure=0{obfs}"
        f"#{user['name']}"
    )

def build_sub_url(user: dict) -> str:
    base = f"{WEB_BASE_URL}/sub/{user['sub_token']}"
    if SUB_URL_PREFIX:
        return SUB_URL_PREFIX + base
    return base

def build_config_url(user: dict) -> str:
    return f"{WEB_BASE_URL}/config/{user['sub_token']}"


# ── Мониторинг ────────────────────────────────────────────────────────────

def check_expired_users():
    users = db.get_all_users()
    changed = False
    for u in users:
        if u["status"] != "active":
            continue
        if is_expired(u) or is_over_limit(u):
            db.update_user_status(u["name"], "suspended")
            changed = True
    if changed:
        sync_config_from_db()
        restart_singbox()

def get_singbox_status() -> dict:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", SINGBOX_SERVICE],
            capture_output=True, text=True, timeout=5
        )
        active = r.stdout.strip() == "active"
        r2 = subprocess.run(
            ["systemctl", "show", SINGBOX_SERVICE,
             "--property=ActiveEnterTimestamp,MainPID"],
            capture_output=True, text=True, timeout=5
        )
        info = dict(
            line.split("=", 1) for line in r2.stdout.strip().splitlines() if "=" in line
        )
        return {
            "active": active,
            "status": r.stdout.strip(),
            "pid":    info.get("MainPID", "?"),
            "since":  info.get("ActiveEnterTimestamp", "?"),
        }
    except Exception as e:
        return {"active": False, "status": "error", "pid": "?", "since": str(e)}
