"""
scheduler.py — фоновые задачи:
- сбор трафика из Clash API → запись в БД
- проверка лимитов и истечения срока
- определение online-статуса
Запускается в фоновом потоке из web.py и bot.py.
"""
import logging
import time
from threading import Thread, Lock
import db
import manager
import traffic_collector  # Используем новый модуль для трафика и онлайна

log = logging.getLogger(__name__)

# Кэш online-пользователей — обновляется каждые 30 сек
_online_users: set[str] = set()
_online_lock = Lock()

# Флаг: Clash API доступен
_clash_available: bool = False


def get_online_users() -> set[str]:
    with _online_lock:
        cached = set(_online_users)
    # Если кэш пуст, читаем из БД (актуально для бота, если web-процесс умер)
    if not cached:
        try:
            return db.get_online_users_db()
        except Exception:
            pass
    return cached


def _update_online():
    global _online_users, _clash_available
    # ИСПРАВЛЕНИЕ: используем traffic_collector вместо clash_traffic
    online = traffic_collector.get_online_users()
    _clash_available = True
    
    with _online_lock:
        _online_users = online
        
    # Сохраняем в БД — чтобы второй процесс (бот или веб) тоже мог читать
    try:
        db.set_online_users(online)
    except Exception as e:
        log.debug(f"[scheduler] online DB write: {e}")


def _collect_traffic():
    """Собирает дельту трафика из Clash API и записывает в БД."""
    traffic_collector.collect_and_save()


def _check_limits():
    """Приостанавливает пользователей с истёкшим сроком или лимитом."""
    users = db.get_all_users()
    changed = False
    for u in users:
        if u["status"] != "active":
            continue
        if manager.is_expired(u) or manager.is_over_limit(u):
            log.info(f"[scheduler] Suspending {u['name']}")
            db.update_user_status(u["name"], "suspended")
            changed = True
    if changed:
        manager.sync_config_from_db()
        manager.restart_singbox()


def _loop(traffic_interval: int, limits_interval: int):
    traffic_counter = 0
    limits_counter = 0
    
    # Первичная проверка доступности API
    try:
        traffic_collector.check_clash_api()
        _clash_available = True
        log.info("[scheduler] Clash API доступен — сбор трафика и онлайна включён")
    except Exception:
        log.warning(
            "[scheduler] Clash API недоступен. Трафик и онлайн не будут обновляться.\n"
            "  Добавьте в /etc/sing-box/config.json:\n"
            '   "experimental": {"clash_api": {"external_controller": "127.0.0.1:9090"}}\n'
            "  и перезапустите sing-box."
        )

    while True:
        time.sleep(30)
        traffic_counter += 30
        limits_counter += 30

        # Онлайн-статус — каждые 30 сек
        try:
            _update_online()
        except Exception as e:
            log.debug(f"[scheduler] online update: {e}")

        # Трафик — каждые N секунд
        if traffic_counter >= traffic_interval:
            traffic_counter = 0
            try:
                _collect_traffic()
            except Exception as e:
                log.debug(f"[scheduler] traffic collect: {e}")

        # Лимиты — каждые N секунд
        if limits_counter >= limits_interval:
            limits_counter = 0
            try:
                _check_limits()
            except Exception as e:
                log.error(f"[scheduler] limits check: {e}")


def start(traffic_interval: int = 60, limits_interval: int = 300) -> Thread:
    """Запускает планировщик в фоновом потоке."""
    t = Thread(
        target=_loop,
        args=(traffic_interval, limits_interval),
        daemon=True,
        name="scheduler",
    )
    t.start()
    log.info(f"[scheduler] started (traffic={traffic_interval}s, limits={limits_interval}s)")
    return t
