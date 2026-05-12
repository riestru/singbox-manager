"""
migrate_existing.py — импортирует существующих пользователей из config.json в БД.
Запускать один раз после установки.
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

import db
import manager
from config import SINGBOX_CONFIG_PATH


def migrate():
    db.init_db()
    with open(SINGBOX_CONFIG_PATH, "r") as f:
        cfg = json.load(f)

    inbound = manager.get_inbound(cfg)
    users_in_cfg = inbound.get("users", [])

    if not users_in_cfg:
        print("Пользователей в конфиге не найдено.")
        return

    print(f"Найдено {len(users_in_cfg)} пользователей в конфиге.")
    imported = 0
    skipped = 0

    for u in users_in_cfg:
        name = u.get("name", "").strip()
        password = u.get("password", "").strip()
        if not name or not password:
            print(f"  Пропуск: некорректная запись {u}")
            continue
        if db.get_user_by_name(name):
            print(f"  Пропуск (уже есть): {name}")
            skipped += 1
            continue
        sub_token = manager.gen_sub_token()
        db.add_user(
            name=name,
            password=password,
            sub_token=sub_token,
        )
        print(f"  Импортирован: {name}")
        imported += 1

    print(f"\nГотово! Импортировано: {imported}, пропущено: {skipped}")
    print("Теперь проверьте базу командой: /users в боте или на веб-панели.")


if __name__ == "__main__":
    migrate()
