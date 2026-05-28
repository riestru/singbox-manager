"""
migrate_existing.py — импортирует существующих пользователей из config.json в БД.
Запускать один раз после установки или при переезде на новый сервер.

При импорте сверяет и при необходимости обновляет:
  - адрес подключения клиента (CLIENT_HOST → SERVER_HOST → оставить как есть)
  - SNI                        (DEFAULT_SNI → SERVER_HOST → оставить как есть)
  - allow_insecure             (DEFAULT_INSECURE → false по умолчанию)
  - OBFS пароль берётся из .env автоматически при построении URI (не хранится в БД)

Дополнительно при каждом запуске выполняется миграция устаревшего
формата хранения адреса сервера: если в поле notes есть "[host:адрес]",
значение переносится в поле server_host, а костыль из notes удаляется.
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

# ── Загружаем .env вручную ДО импорта config ──────────────────────────────
def _load_dotenv(path: str):
    """Читает .env и устанавливает переменные в os.environ (только если не заданы)."""
    if not os.path.exists(path):
        print(f"[warn] .env не найден: {path}")
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if "#" in value and not (value.startswith('"') or value.startswith("'")):
                value = value[:value.index("#")].strip()
            if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
                value = value[1:-1]
            if key not in os.environ:
                os.environ[key] = value

_ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
_load_dotenv(_ENV_PATH)
# ──────────────────────────────────────────────────────────────────────────

import db
import manager
from config import (
    SINGBOX_CONFIG_PATH,
    SERVER_HOST,
    CLIENT_HOST,
    DEFAULT_SNI,
    DEFAULT_INSECURE,
    OBFS_PASSWORD,
    SERVER_PORT,
    WEB_BASE_URL,
)


def _resolve_target_host() -> str | None:
    if CLIENT_HOST and CLIENT_HOST != "your.server.domain":
        return CLIENT_HOST
    if SERVER_HOST and SERVER_HOST != "your.server.domain":
        return SERVER_HOST
    return None


def _resolve_target_sni() -> str | None:
    if DEFAULT_SNI:
        return DEFAULT_SNI
    if SERVER_HOST and SERVER_HOST != "your.server.domain":
        return SERVER_HOST
    return None


def _migrate_notes_host(users: list[dict]) -> int:
    """
    Переносит [host:...] из поля notes в поле server_host для всех пользователей.
    Очищает костыль из notes.
    Возвращает количество обновлённых записей.
    """
    migrated = 0
    for u in users:
        notes = u.get("notes") or ""
        if not notes.startswith("[host:"):
            continue
        try:
            end = notes.index("]")
        except ValueError:
            continue
        host_value = notes[6:end].strip()
        tail = notes[end + 1:].strip()

        # Записываем в server_host (None если совпадает с SERVER_HOST — дефолт)
        effective_host = host_value if host_value != SERVER_HOST else None
        db.update_user_field(u["name"], "server_host", effective_host)
        # Очищаем notes: оставляем только хвост без [host:...]
        db.update_user_field(u["name"], "notes", tail or None)

        print(
            f"  [ПЕРЕНОС HOST] {u['name']}: "
            f"notes[host:{host_value}] → server_host={effective_host or '(дефолт)'}"
        )
        migrated += 1
    return migrated


def migrate():
    db.init_db()

    # ── Сначала мигрируем старый формат [host:...] → server_host ──────────
    print("=" * 60)
    print("Проверка устаревшего формата [host:...] в notes...")
    existing_users = db.get_all_users(include_deleted=True)
    notes_migrated = _migrate_notes_host(existing_users)
    if notes_migrated:
        print(f"Перенесено {notes_migrated} записей host из notes в server_host.")
    else:
        print("Устаревших записей [host:...] не найдено.")
    print()

    # ── Затем импортируем пользователей из config.json ─────────────────────
    with open(SINGBOX_CONFIG_PATH, "r") as f:
        cfg = json.load(f)

    inbound = manager.get_inbound(cfg)
    users_in_cfg = inbound.get("users", [])

    if not users_in_cfg:
        print("Пользователей в конфиге не найдено.")
        return

    target_host     = _resolve_target_host()
    target_sni      = _resolve_target_sni()
    target_insecure = DEFAULT_INSECURE

    print("=" * 60)
    print("Параметры миграции из .env:")
    print(f"  Адрес подключения (CLIENT_HOST):      {target_host or '— не задан, оставляем как есть'}")
    print(f"  SNI по умолчанию  (DEFAULT_SNI):      {target_sni  or '— не задан, оставляем как есть'}")
    print(f"  Insecure          (DEFAULT_INSECURE):  {'true (insecure=1)' if target_insecure else 'false (insecure=0)'}")
    print(f"  OBFS пароль      (OBFS_PASSWORD):     {'задан (' + OBFS_PASSWORD[:4] + '...)' if OBFS_PASSWORD else '— не задан'}")
    print(f"  Порт сервера      (SERVER_PORT):      {SERVER_PORT}")
    print(f"  Веб-панель        (WEB_BASE_URL):     {WEB_BASE_URL}")
    print("=" * 60)
    print(f"Найдено {len(users_in_cfg)} пользователей в конфиге.")
    print()

    imported = 0
    skipped  = 0
    updated  = 0

    for u in users_in_cfg:
        name     = u.get("name", "").strip()
        password = u.get("password", "").strip()

        if not name or not password:
            print(f"  [ПРОПУСК] некорректная запись: {u}")
            continue

        existing = db.get_user_by_name(name)

        if existing:
            changed = []

            # Проверяем server_host
            if target_host is not None:
                current_host = manager._get_user_host(existing)
                if current_host != target_host:
                    effective = target_host if target_host != SERVER_HOST else None
                    db.update_user_field(name, "server_host", effective)
                    changed.append(f"server_host: {current_host} → {target_host}")

            # Проверяем SNI
            if target_sni is not None:
                current_sni = existing.get("sni") or ""
                if current_sni != target_sni:
                    db.update_user_field(name, "sni", target_sni)
                    changed.append(f"SNI: '{current_sni or '(пусто)'}' → '{target_sni}'")

            # Проверяем allow_insecure
            current_insecure = bool(existing.get("allow_insecure", 0))
            if current_insecure != target_insecure:
                db.update_user_field(name, "allow_insecure", 1 if target_insecure else 0)
                changed.append(f"insecure: {int(current_insecure)} → {int(target_insecure)}")

            if changed:
                print(f"  [ОБНОВЛЁН] {name}: {', '.join(changed)}")
                updated += 1
            else:
                print(f"  [БЕЗ ИЗМЕНЕНИЙ] {name}: уже в БД, все параметры совпадают")
                skipped += 1
            continue

        # ── Новый пользователь ─────────────────────────────────────────────
        sub_token = manager.gen_sub_token()

        # server_host: None если совпадает с SERVER_HOST (дефолт)
        effective_host = (
            target_host if (target_host and target_host != SERVER_HOST) else None
        )

        db.add_user(
            name=name,
            password=password,
            sub_token=sub_token,
            server_host=effective_host,
            sni=target_sni,
            allow_insecure=target_insecure,
        )

        new_user = db.get_user_by_name(name)
        hy2_uri  = manager.build_hy2_uri(new_user) if new_user else "—"

        print(f"  [ИМПОРТИРОВАН] {name}")
        print(f"    server_host : {target_host or SERVER_HOST}")
        print(f"    SNI         : {target_sni or '(не задан — клиент использует host)'}")
        print(f"    insecure    : {int(target_insecure)}")
        print(f"    URI         : {hy2_uri}")
        imported += 1

    print()
    print("=" * 60)
    print(f"Готово! Импортировано: {imported}, обновлено: {updated}, без изменений: {skipped}")
    print()
    print("Проверьте пользователей: /users в боте или на веб-панели.")
    if imported > 0 or updated > 0:
        print()
        print("OBFS пароль в URI берётся из OBFS_PASSWORD в .env автоматически.")
        print("Подписки и конфиги также обновятся автоматически — они строятся динамически.")


if __name__ == "__main__":
    migrate()
