"""
web.py — информационная веб-панель sing-box manager.
"""
import io, json, base64, logging
from datetime import datetime
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

import qrcode
from flask import Flask, Blueprint, abort, render_template, Response, jsonify
from flask_httpauth import HTTPBasicAuth

import db, manager
from config import (
    WEB_SECRET_KEY, WEB_HOST, WEB_PORT,
    WEB_ADMIN_USER, WEB_ADMIN_PASS, WEB_PREFIX,
    SERVER_PORT, OBFS_PASSWORD,
)

app = Flask(__name__)
app.secret_key = WEB_SECRET_KEY
auth = HTTPBasicAuth()

# Нормализуем PREFIX: "/" или "/secret/path"
_p = WEB_PREFIX.strip("/")
PREFIX = f"/{_p}" if _p else ""

panel = Blueprint("panel", __name__)


@auth.verify_password
def verify_password(username, password):
    return username == WEB_ADMIN_USER and password == WEB_ADMIN_PASS


def qr_b64(data: str) -> str:
    buf = io.BytesIO()
    qrcode.make(data).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def get_system_stats() -> dict:
    """Читает CPU и память из /proc — без внешних зависимостей."""
    stats = {"cpu_pct": 0.0, "mem_used_mb": 0, "mem_total_mb": 0, "mem_pct": 0.0}
    try:
        # CPU: два снимка /proc/stat с паузой 200 мс
        import time
        def read_cpu():
            with open("/proc/stat") as f:
                line = f.readline()
            vals = list(map(int, line.split()[1:]))
            idle = vals[3]
            total = sum(vals)
            return idle, total
        i1, t1 = read_cpu()
        time.sleep(0.2)
        i2, t2 = read_cpu()
        dt = t2 - t1
        stats["cpu_pct"] = round((1 - (i2 - i1) / dt) * 100, 1) if dt else 0.0
    except Exception:
        pass
    try:
        # Память из /proc/meminfo
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                mem[k.strip()] = int(v.split()[0])  # в кБ
        total_kb    = mem.get("MemTotal", 0)
        avail_kb    = mem.get("MemAvailable", mem.get("MemFree", 0))
        used_kb     = total_kb - avail_kb
        stats["mem_total_mb"] = total_kb // 1024
        stats["mem_used_mb"]  = used_kb  // 1024
        stats["mem_pct"]      = round(used_kb / total_kb * 100, 1) if total_kb else 0.0
    except Exception:
        pass
    return stats


def get_online() -> set:
    try:
        from scheduler import get_online_users
        result = get_online_users()
        if result:
            return result
    except Exception:
        pass
    try:
        return db.get_online_users_db()
    except Exception:
        return set()


# ── Панель ─────────────────────────────────────────────────────────────────

@panel.route("/")
@panel.route("")
@auth.login_required
def index():
    online_set = get_online()
    users   = manager.list_users(online_set=online_set)
    total   = db.get_total_traffic()
    singbox = manager.get_singbox_status()

    for u in users:
        u["hy2_uri"]    = manager.build_hy2_uri(u)
        u["sub_url"]    = manager.build_sub_url(u)
        u["config_url"] = manager.build_config_url(u)
        u["qr_uri_b64"] = qr_b64(u["hy2_uri"])
        u["qr_sub_b64"] = qr_b64(u["sub_url"])
        rx  = u.get("traffic_used_rx", 0)
        tx  = u.get("traffic_used_tx", 0)
        tot = rx + tx
        u["traffic_used_total"] = tot
        u["traffic_used_fmt"]   = manager.fmt_bytes(tot)
        u["traffic_rx_fmt"]     = manager.fmt_bytes(rx)
        u["traffic_tx_fmt"]     = manager.fmt_bytes(tx)
        lim = u.get("traffic_limit_gb", 0)
        u["limit_fmt"]   = f"{lim} GB" if lim else "∞"
        u["expire_fmt"]  = (u.get("expire_at") or "")[:10] or "∞"
        u["online"]      = u["name"] in online_set
        u["online_icon"] = "🟢" if u["online"] else "⚫"
        u["traffic_pct"] = min(100, round(manager.bytes_to_gb(tot) / lim * 100, 1)) if lim else 0

    sys_stats = get_system_stats()
    return render_template(
        "index.html",
        users=users,
        total_rx=manager.fmt_bytes(total["rx"]),
        total_tx=manager.fmt_bytes(total["tx"]),
        total_all=manager.fmt_bytes(total["rx"] + total["tx"]),
        singbox=singbox,
        sys_stats=sys_stats,
        now=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        prefix=PREFIX,
    )


@panel.route("/api/sysinfo")
@auth.login_required
def api_sysinfo():
    """Возвращает CPU, память и статус сервиса — для автообновления без перезагрузки."""
    stats = get_system_stats()
    singbox = manager.get_singbox_status()
    total   = db.get_total_traffic()
    online  = get_online()
    return jsonify({
        "cpu_pct":      stats["cpu_pct"],
        "mem_used_mb":  stats["mem_used_mb"],
        "mem_total_mb": stats["mem_total_mb"],
        "mem_pct":      stats["mem_pct"],
        "singbox_active": singbox["active"],
        "online_count": len(online),
        "total_rx":     manager.fmt_bytes(total["rx"]),
        "total_tx":     manager.fmt_bytes(total["tx"]),
        "now":          datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    })


@panel.route("/api/users")
@auth.login_required
def api_users():
    online_set = get_online()
    users = manager.list_users(online_set=online_set)
    return jsonify({
        "users": [{
            "id": u["id"], "name": u["name"], "status": u["status"],
            "email": u.get("email"), "online": u.get("online", False),
            "traffic_used_rx": u.get("traffic_used_rx", 0),
            "traffic_used_tx": u.get("traffic_used_tx", 0),
            "traffic_limit_gb": u.get("traffic_limit_gb", 0),
            "expire_at": u.get("expire_at"), "created_at": u.get("created_at"),
            "expired": u.get("expired"), "over_limit": u.get("over_limit"),
        } for u in users],
        "total": db.get_total_traffic(),
    })


@panel.route("/api/user/suspend/<name>", methods=["POST"])
@auth.login_required
def api_suspend_user(name):
    u = db.get_user_by_name(name)
    if not u or u["status"] == "deleted":
        return jsonify({"error": "not found"}), 404
    try:
        manager.suspend_user(name)
        return jsonify({"ok": True, "name": name, "status": "suspended"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@panel.route("/api/user/activate/<name>", methods=["POST"])
@auth.login_required
def api_activate_user(name):
    u = db.get_user_by_name(name)
    if not u or u["status"] == "deleted":
        return jsonify({"error": "not found"}), 404
    try:
        manager.activate_user(name)
        return jsonify({"ok": True, "name": name, "status": "active"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@panel.route("/api/reset_traffic/<name>", methods=["POST"])
@auth.login_required
def api_reset_traffic(name):
    if not db.get_user_by_name(name):
        return jsonify({"error": "not found"}), 404
    db.reset_traffic(name)
    # Сбрасываем кэш prev_stats — иначе scheduler через 60 сек
    # восстановит трафик из старых данных Clash API
    try:
        import traffic_collector
        traffic_collector.reset_user_stats(name)
    except Exception as e:
        logging.warning("reset_user_stats failed: %s", e)
    return jsonify({"ok": True, "name": name})


# ── Подписка и конфиг — без PREFIX и без auth (токен = защита) ─────────────

@app.route("/sub/<token>")
def subscription(token):
    u = db.get_user_by_token(token)
    if not u or u["status"] == "deleted":
        abort(404)
    payload = base64.b64encode(manager.build_hy2_uri(u).encode()).decode()
    return Response(payload, mimetype="text/plain",
                    headers={"profile-title": u["name"], "profile-update-interval": "24"})


@app.route("/config/<token>")
def config_file(token):
    u = db.get_user_by_token(token)
    if not u or u["status"] == "deleted":
        abort(404)
    h = manager._get_user_host(u)
    cfg = {
        "log": {"level": "info"},
        "inbounds": [{"type": "tun", "tag": "tun-in",
            "address": ["172.19.0.1/30", "fdfe:dcba:9876::1/126"],
            "auto_route": True, "strict_route": True, "stack": "system", "sniff": True}],
        "outbounds": [
            {"type": "hysteria2", "tag": "proxy", "server": h,
             "server_port": SERVER_PORT, "password": u["password"],
             **({"obfs": {"type": "salamander", "password": OBFS_PASSWORD}} if OBFS_PASSWORD else {}),
             "tls": {"enabled": True, "server_name": h, "insecure": False}},
            {"type": "direct", "tag": "direct"},
            {"type": "block",  "tag": "block"},
            {"type": "dns",    "tag": "dns-out"},
        ],
        "route": {"rules": [{"protocol": "dns", "outbound": "dns-out"},
                             {"ip_is_private": True, "outbound": "direct"}],
                  "final": "proxy", "auto_detect_interface": True},
    }
    return Response(json.dumps(cfg, indent=2, ensure_ascii=False),
                    mimetype="application/json",
                    headers={"Content-Disposition": f"attachment; filename=singbox_{u['name']}.json"})


# Регистрируем blueprint с PREFIX
app.register_blueprint(panel, url_prefix=PREFIX if PREFIX else "/")

def _start_scheduler():
    """
    Запускает scheduler уже внутри рабочего процесса Flask.
    Потоки запущенные до app.run() теряются при внутреннем fork werkzeug.
    Timer 0.5s гарантирует что app.run() уже захватил управление.
    """
    import os
    try:
        import scheduler
        scheduler.start()
        logging.getLogger(__name__).info("scheduler запущен")
    except Exception as e:
        logging.getLogger(__name__).error("scheduler ошибка: %s", e)


if __name__ == "__main__":
    import threading
    db.init_db()
    timer = threading.Timer(0.5, _start_scheduler)
    timer.daemon = True
    timer.start()
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False)
