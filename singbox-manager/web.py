"""
web.py — информационная веб-панель sing-box manager.
"""
import io, json, base64
from datetime import datetime

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

    return render_template(
        "index.html",
        users=users,
        total_rx=manager.fmt_bytes(total["rx"]),
        total_tx=manager.fmt_bytes(total["tx"]),
        total_all=manager.fmt_bytes(total["rx"] + total["tx"]),
        singbox=singbox,
        now=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        prefix=PREFIX,
    )


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


@panel.route("/api/reset_traffic/<name>", methods=["POST"])
@auth.login_required
def api_reset_traffic(name):
    if not db.get_user_by_name(name):
        return jsonify({"error": "not found"}), 404
    db.reset_traffic(name)
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

if __name__ == "__main__":
    db.init_db()
    try:
        import scheduler
        scheduler.start()
    except Exception as e:
        print(f"[web] scheduler: {e}")
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False)
