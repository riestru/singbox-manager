"""
bot.py — Telegram-бот для управления sing-box.
aiogram >= 3.7
"""
import asyncio, io, logging, re
from typing import Any, Awaitable, Callable
import qrcode
from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand, BufferedInputFile, CallbackQuery,
    InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject,
)
import db, manager
from config import BOT_TOKEN, ADMIN_IDS, WEB_BASE_URL, SERVER_HOST, HTTPS_PROXY

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
router = Router()


# ── Middleware ─────────────────────────────────────────────────────────────

class AdminMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict) -> Any:
        user = data.get("event_from_user")
        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            return await handler(event, data)
        if user and user.id not in ADMIN_IDS:
            if isinstance(event, Message):
                await event.answer("⛔ Нет доступа.")
            return
        return await handler(event, data)


# ── Утилиты ────────────────────────────────────────────────────────────────

def qr(data: str) -> BufferedInputFile:
    buf = io.BytesIO()
    qrcode.make(data).save(buf, format="PNG")
    return BufferedInputFile(buf.getvalue(), filename="qr.png")

def status_icon(u: dict) -> str:
    if u.get("online"):
        return "🟢"
    if u["status"] == "suspended":
        return "⏸"
    if u.get("expired") or u.get("over_limit"):
        return "🔴"
    return "⚫"

def fmt_short(u: dict) -> str:
    icon = status_icon(u)
    limit = f"{u['traffic_limit_gb']} GB" if u.get("traffic_limit_gb") else "∞"
    used = manager.bytes_to_gb(u.get("traffic_used_rx", 0) + u.get("traffic_used_tx", 0))
    exp = (u.get("expire_at") or "")[:10] or "∞"
    return f"{icon}  <b>{u['name']}</b> | {used:.2f}/{limit} GB | до {exp}"

def fmt_detail(u: dict) -> str:
    total = u.get("traffic_used_rx", 0) + u.get("traffic_used_tx", 0)
    limit = f"{u['traffic_limit_gb']} GB" if u.get("traffic_limit_gb") else "∞"
    exp = (u.get("expire_at") or "")[:10] or "∞"
    online_str = "🟢 Online" if u.get("online") else "⚫ Offline"
    lines = [
        f"{status_icon(u)}  <b>{u['name']}</b>  {online_str}",
        f"  Статус: <code>{u['status']}</code>",
        f"  Email: {u.get('email') or '—'}",
        f"  Трафик: {manager.fmt_bytes(total)} / {limit}",
        f"    ↓ {manager.fmt_bytes(u.get('traffic_used_rx',0))}   "
        f"↑ {manager.fmt_bytes(u.get('traffic_used_tx',0))}",
        f"  Истекает: {exp}",
        f"  Создан: {(u.get('created_at') or '')[:10]}",
    ]
    # НОВОЕ: отображаем SNI и insecure
    if u.get("sni"):
        lines.append(f"  SNI: <code>{u['sni']}</code>")
    if u.get("allow_insecure"):
        lines.append("  🔓 Разрешены небезопасные сертификаты")
    if u.get("notes"):
        lines.append(f"  Заметка: {u['notes']}")
    return "\n".join(lines)

def get_online() -> set[str]:
    try:
        from scheduler import get_online_users
        result = get_online_users()
        if result:
            return result
    except Exception:
        pass
    # Запасной вариант: читаем из БД напрямую
    try:
        return db.get_online_users_db()
    except Exception:
        return set()


# ── FSM ────────────────────────────────────────────────────────────────────

class AddUser(StatesGroup):
    name = State()
    server_host = State()
    email = State()
    traffic_limit = State()
    expire_days = State()
    notes = State()
    sni = State()              # ← НОВОЕ
    allow_insecure = State()   # ← НОВОЕ
    offer_email = State()

class StopUser(StatesGroup):
    confirm = State()

class DeleteUser(StatesGroup):
    confirm = State()

class SendEmail(StatesGroup):
    choose_email = State()


# ── /cancel ────────────────────────────────────────────────────────────────

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer("Нет активного действия для отмены.")
        return
    await state.clear()
    await message.answer("❌ Действие отменено.")


# ── /start ─────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Это служебный бот. Доступ ограничен.")
        return
    await message.answer(
        "👋  <b>Sing-box Manager</b>\n\n"
        "/users — список пользователей\n"
        "/add — добавить пользователя\n"
        "/stop [имя] — приостановить\n"
        "/start_user [имя] — возобновить\n"
        "/delete [имя] — удалить\n"
        "/info [имя] — подробности\n"
        "/qr [имя] — QR-коды\n"
        "/reset_traffic [имя] — сбросить трафик\n"
        "/set_limit [имя] [GB] — лимит трафика\n"
        "/set_expire [имя] [дни] — срок действия\n"
        "/send_email [имя] — конфиг на email\n"
        "/status — статус сервиса\n"
        "/cancel — отменить текущее действие\n"
        "/help — справка\n\n"
        f"🌐 Web: {WEB_BASE_URL}"
    )


# ── /help ──────────────────────────────────────────────────────────────────

@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        " <b>📖 Справка</b>\n\n"
        " <b>/add</b> — добавить пользователя пошагово (включая SNI и insecure)\n"
        " <b>/cancel</b> — отменить текущий ввод\n"
        " <b>/users</b> — список всех пользователей\n"
        " <b>/info [имя]</b> — детальная информация (с показом SNI/инсикьюр)\n"
        " <b>/stop [имя]</b> — приостановить (с подтверждением)\n"
        " <b>/start_user [имя]</b> — возобновить\n"
        " <b>/delete [имя]</b> — удалить (с подтверждением)\n"
        " <b>/qr [имя]</b> — QR URI и подписки\n"
        " <b>/reset_traffic [имя]</b> — сбросить счётчик трафика\n"
        " <b>/set_limit [имя] [GB]</b> — лимит (0 = ∞)\n"
        " <b>/set_expire [имя] [дни]</b> — срок (0 = ∞)\n"
        " <b>/send_email [имя]</b> — конфиг на email\n"
        " <b>/status</b> — статус sing-box\n\n"
        "🟢 Online  ⚫ Offline  ⏸ Пауза  🔴 Лимит/истёк"
    )


# ── /status ────────────────────────────────────────────────────────────────

@router.message(Command("status"))
async def cmd_status(message: Message):
    s = manager.get_singbox_status()
    icon = "🟢" if s["active"] else "🔴"
    total = db.get_total_traffic()
    users = db.get_all_users()
    active = sum(1 for u in users if u["status"] == "active")
    online = len(get_online())
    from clash_traffic import is_available
    clash_str = "✅ активен" if is_available() else "❌ недоступен (трафик не считается)"
    await message.answer(
        f"{icon}  <b>sing-box</b>: {s['status']}\n"
        f"  PID: <code>{s['pid']}</code>  |  С: {s['since']}\n\n"
        f"👥 Всего: {len(users)}  Активных: {active}  Online: {online}\n"
        f"📊 Трафик: ↓{manager.fmt_bytes(total['rx'])} ↑{manager.fmt_bytes(total['tx'])}\n"
        f"🔌 Clash API: {clash_str}"
    )


# ── /users ─────────────────────────────────────────────────────────────────

@router.message(Command("users"))
async def cmd_users(message: Message):
    online = get_online()
    users = manager.list_users(online_set=online)
    if not users:
        await message.answer("Пользователей нет.")
        return
    lines = ["<b>👥 Пользователи:</b>  🟢Online ⚫Offline ⏸Пауза\n"]
    for u in users:
        lines.append(fmt_short(u))
    await message.answer("\n".join(lines))


# ── /info ──────────────────────────────────────────────────────────────────

@router.message(Command("info"))
async def cmd_info(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /info [имя]")
        return
    name = parts[1].strip()
    u = db.get_user_by_name(name)
    if not u:
        await message.answer(f"Пользователь «{name}» не найден.")
        return
    online = get_online()
    u["expired"] = manager.is_expired(u)
    u["over_limit"] = manager.is_over_limit(u)
    u["online"] = name in online
    hy2 = manager.build_hy2_uri(u)
    sub = manager.build_sub_url(u)
    await message.answer(fmt_detail(u) + f"\n\n<code>{hy2}</code>\n\n🔗 {sub}")


# ── /add — FSM ─────────────────────────────────────────────────────────────

@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext):
    await state.set_state(AddUser.name)
    await message.answer(
        "👤 Введите <b>имя</b> нового пользователя\n"
        "(латиница/цифры/_ , 2–32 символа)\n\n"
        "Для отмены: /cancel"
    )

@router.message(AddUser.name)
async def add_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not re.match(r'^[a-zA-Z0-9_-]{2,32}$', name):
        await message.answer("❌ Имя: 2–32 символа, только латиница/цифры/_/-")
        return
    if db.get_user_by_name(name):
        await message.answer(f"❌ «{name}» уже существует. Введите другое имя:")
        return
    await state.update_data(name=name)
    await state.set_state(AddUser.server_host)
    await message.answer(
        f"🌐 Адрес сервера для подключения клиента\n"
        f"(домен или IP)\n\n"
        f"По умолчанию: <code>{SERVER_HOST}</code>\n\n"
        f"Введите адрес или /skip для использования дефолтного:"
    )

@router.message(AddUser.server_host)
async def add_server_host(message: Message, state: FSMContext):
    text = message.text.strip()
    host = SERVER_HOST if text.lower() == "/skip" else text
    await state.update_data(server_host=host)
    await state.set_state(AddUser.email)
    await message.answer("📧 Email клиента (для отправки конфига)\nВведите email или /skip:")

@router.message(AddUser.email)
async def add_email(message: Message, state: FSMContext):
    text = message.text.strip()
    email = None if text.lower() == "/skip" else text
    await state.update_data(email=email)
    await state.set_state(AddUser.traffic_limit)
    await message.answer("📊 Лимит трафика в ГБ (0 = без лимита)\nВведите число или /skip:")

@router.message(AddUser.traffic_limit)
async def add_traffic(message: Message, state: FSMContext):
    text = message.text.strip()
    try:
        limit = 0.0 if text.lower() == "/skip" else float(text)
        if limit < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите число ≥ 0 или /skip")
        return
    await state.update_data(traffic_limit=limit)
    await state.set_state(AddUser.expire_days)
    await message.answer("📅 Срок действия в днях (0 = без срока)\nВведите число или /skip:")

@router.message(AddUser.expire_days)
async def add_expire(message: Message, state: FSMContext):
    text = message.text.strip()
    try:
        days = 0 if text.lower() == "/skip" else int(text)
        if days < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое число ≥ 0 или /skip")
        return
    await state.update_data(expire_days=days)
    await state.set_state(AddUser.notes)
    await message.answer("📝 Заметка (ФИО, описание) или /skip:")

@router.message(AddUser.notes)
async def add_notes(message: Message, state: FSMContext):
    text = message.text.strip()
    notes = None if text.lower() == "/skip" else text
    await state.update_data(notes=notes)
    # Переходим к запросу SNI
    await state.set_state(AddUser.sni)
    await message.answer(
        "🔐 <b>SNI</b> (Server Name Indication)\n"
        "Обычно совпадает с адресом сервера. Укажите, если используете CDN/прокси.\n"
        f"По умолчанию: <code>{SERVER_HOST}</code>\n\n"
        "Введите SNI или /skip для использования дефолтного:"
    )

@router.message(AddUser.sni)
async def add_sni(message: Message, state: FSMContext):
    text = message.text.strip()
    # Если /skip или пусто — не сохраняем (будет использоваться host по умолчанию)
    sni = None if text.lower() == "/skip" or not text else text.strip()
    await state.update_data(sni=sni)
    
    await state.set_state(AddUser.allow_insecure)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, разрешить", callback_data="insecure_yes"),
        InlineKeyboardButton(text="❌ Нет (рекомендуется)", callback_data="insecure_no"),
    ]])
    await message.answer(
        "🔓 <b>Разрешить небезопасные сертификаты?</b>\n"
        "Включите, только если используете самоподписанный сертификат.\n"
        "⚠️ Для продакшена рекомендуется: НЕТ",
        reply_markup=kb
    )

@router.callback_query(F.data.in_({"insecure_yes", "insecure_no"}))
async def add_allow_insecure(call: CallbackQuery, state: FSMContext):
    allow_insecure = call.data == "insecure_yes"
    await state.update_data(allow_insecure=allow_insecure)
    
    # Все данные собраны — создаём пользователя
    data = await state.get_data()
    try:
        user = manager.add_user(
            name=data["name"],
            email=data.get("email"),
            server_host=data.get("server_host"),
            traffic_limit_gb=data.get("traffic_limit", 0),
            expire_days=data.get("expire_days", 0),
            notes=data.get("notes"),
            sni=data.get("sni"),
            allow_insecure=data.get("allow_insecure", False),
        )
    except Exception as e:
        await state.clear()
        await call.message.edit_text(f"❌ Ошибка создания: {e}")
        return
    
    # Формируем ответ с учётом новых параметров
    hy2 = manager.build_hy2_uri(user)
    sub = manager.build_sub_url(user)
    lim = f"{user['traffic_limit_gb']} GB" if user.get("traffic_limit_gb") else "∞"
    exp = data.get("expire_days", 0)
    sni_str = f"  SNI: <code>{data.get('sni') or 'по умолчанию'}</code>\n" if data.get("sni") else ""
    insecure_str = "🔓 Разрешены небезопасные сертификаты\n" if data.get("allow_insecure") else ""
    
    await call.message.edit_text(
        f"✅ Пользователь <b>{user['name']}</b> создан!\n\n"
        f"  Сервер: <code>{data.get('server_host', SERVER_HOST)}</code>\n"
        f"{sni_str}"
        f"{insecure_str}"
        f"  Трафик: {lim}  |  Срок: {f'{exp} дн.' if exp else '∞'}\n"
        f"  Email: {user.get('email') or '—'}\n\n"
        f"<code>{hy2}</code>\n\n"
        f"🔗 Подписка: {sub}"
    )
    await call.message.answer_photo(qr(hy2), caption="QR — URI подключения")
    await call.message.answer_photo(qr(sub), caption="QR — ссылка подписки")
    
    # Предлагаем отправить email (как было)
    await state.update_data(created_user=user["name"])
    await state.set_state(AddUser.offer_email)
    user_email = user.get("email")
    if user_email:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"✉️ Отправить на {user_email}",
                                 callback_data=f"email_yes:{user['name']}"),
            InlineKeyboardButton(text="⏭ Пропустить", callback_data="email_skip"),
        ]])
        await call.message.answer("Отправить конфигурацию клиенту на email?", reply_markup=kb)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✉️ Ввести email и отправить",
                                 callback_data=f"email_enter:{user['name']}"),
            InlineKeyboardButton(text="⏭ Пропустить", callback_data="email_skip"),
        ]])
        await call.message.answer("Email не указан. Отправить конфигурацию?", reply_markup=kb)

@router.callback_query(F.data.startswith("email_yes:"))
async def cb_email_yes(call: CallbackQuery, state: FSMContext):
    name = call.data.split(":", 1)[1]
    u = db.get_user_by_name(name)
    await state.clear()
    await call.message.edit_text(f"📧 Отправляю на {u['email']}…")
    await _do_send_email(call.message, u, u["email"])

@router.callback_query(F.data.startswith("email_enter:"))
async def cb_email_enter(call: CallbackQuery, state: FSMContext):
    name = call.data.split(":", 1)[1]
    await state.update_data(target_name=name)
    await state.set_state(SendEmail.choose_email)
    await call.message.edit_text("Введите email для отправки конфигурации:")

@router.callback_query(F.data == "email_skip")
async def cb_email_skip(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Пропущено. Отправить позже: /send_email [имя]")


# ── /stop ─────────────────────────────────────────────────────────────────

@router.message(Command("stop"))
async def cmd_stop(message: Message, state: FSMContext):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /stop [имя]")
        return
    name = parts[1].strip()
    u = db.get_user_by_name(name)
    if not u or u["status"] == "deleted":
        await message.answer(f"Пользователь «{name}» не найден.")
        return
    if u["status"] == "suspended":
        await message.answer(f"«{name}» уже приостановлен.")
        return
    await state.set_state(StopUser.confirm)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, приостановить", callback_data=f"stop_yes:{name}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="action_cancel"),
    ]])
    await message.answer(f"⏸ Приостановить <b>{name}</b>?", reply_markup=kb)

@router.callback_query(F.data.startswith("stop_yes:"))
async def cb_stop_yes(call: CallbackQuery, state: FSMContext):
    name = call.data.split(":", 1)[1]
    try:
        manager.suspend_user(name)
        await call.message.edit_text(f"⏸ <b>{name}</b> приостановлен.")
    except Exception as e:
        await call.message.edit_text(f"❌ Ошибка: {e}")
    await state.clear()


# ── /start_user ───────────────────────────────────────────────────────────

@router.message(Command("start_user"))
async def cmd_start_user(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /start_user [имя]")
        return
    name = parts[1].strip()
    try:
        manager.activate_user(name)
        await message.answer(f"▶️ <b>{name}</b> активирован.")
    except Exception as e:
        await message.answer(f"❌ {e}")


# ── /delete ───────────────────────────────────────────────────────────────

@router.message(Command("delete"))
async def cmd_delete(message: Message, state: FSMContext):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /delete [имя]")
        return
    name = parts[1].strip()
    u = db.get_user_by_name(name)
    if not u or u["status"] == "deleted":
        await message.answer(f"Пользователь «{name}» не найден.")
        return
    await state.set_state(DeleteUser.confirm)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"del_yes:{name}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="action_cancel"),
    ]])
    await message.answer(f"⚠️ Удалить <b>{name}</b>? Это необратимо!", reply_markup=kb)

@router.callback_query(F.data.startswith("del_yes:"))
async def cb_del_yes(call: CallbackQuery, state: FSMContext):
    name = call.data.split(":", 1)[1]
    try:
        manager.delete_user(name)
        await call.message.edit_text(f"🗑 <b>{name}</b> удалён.")
    except Exception as e:
        await call.message.edit_text(f"❌ Ошибка: {e}")
    await state.clear()

@router.callback_query(F.data == "action_cancel")
async def cb_action_cancel(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("Отменено.")
    await state.clear()


# ── /qr ───────────────────────────────────────────────────────────────────

@router.message(Command("qr"))
async def cmd_qr(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /qr [имя]")
        return
    name = parts[1].strip()
    u = db.get_user_by_name(name)
    if not u:
        await message.answer(f"Пользователь «{name}» не найден.")
        return
    hy2 = manager.build_hy2_uri(u)
    sub = manager.build_sub_url(u)
    await message.answer_photo(qr(hy2), caption=f"🔑 URI:\n<code>{hy2}</code>")
    await message.answer_photo(qr(sub), caption=f"📡 Подписка: {sub}")


# ── /reset_traffic ────────────────────────────────────────────────────────

@router.message(Command("reset_traffic"))
async def cmd_reset_traffic(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /reset_traffic [имя]")
        return
    name = parts[1].strip()
    if not db.get_user_by_name(name):
        await message.answer(f"Пользователь «{name}» не найден.")
        return
    db.reset_traffic(name)
    await message.answer(f"🔄 Трафик для <b>{name}</b> сброшен.")


# ── /set_limit ────────────────────────────────────────────────────────────

@router.message(Command("set_limit"))
async def cmd_set_limit(message: Message):
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("Использование: /set_limit [имя] [GB]\nПример: /set_limit user1 50")
        return
    name, gb_str = parts[1], parts[2]
    try:
        gb = float(gb_str)
    except ValueError:
        await message.answer("❌ GB должно быть числом")
        return
    if not db.get_user_by_name(name):
        await message.answer(f"Пользователь «{name}» не найден.")
        return
    manager.set_traffic_limit(name, gb)
    await message.answer(f"📊 Лимит для <b>{name}</b>: {f'{gb} GB' if gb else '∞'}")


# ── /set_expire ───────────────────────────────────────────────────────────

@router.message(Command("set_expire"))
async def cmd_set_expire(message: Message):
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("Использование: /set_expire [имя] [дни]\nПример: /set_expire user1 30")
        return
    name, days_str = parts[1], parts[2]
    try:
        days = int(days_str)
    except ValueError:
        await message.answer("❌ Дни должны быть целым числом")
        return
    if not db.get_user_by_name(name):
        await message.answer(f"Пользователь «{name}» не найден.")
        return
    manager.set_expire(name, days)
    await message.answer(f"📅 Срок для <b>{name}</b>: {f'{days} дн.' if days else '∞'}")


# ── /send_email ───────────────────────────────────────────────────────────

@router.message(Command("send_email"))
async def cmd_send_email(message: Message, state: FSMContext):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /send_email [имя]")
        return
    name = parts[1].strip()
    u = db.get_user_by_name(name)
    if not u:
        await message.answer(f"Пользователь «{name}» не найден.")
        return
    if u.get("email"):
        await _do_send_email(message, u, u["email"])
    else:
        await state.update_data(target_name=name)
        await state.set_state(SendEmail.choose_email)
        await message.answer(f"У <b>{name}</b> нет email.\nВведите адрес (или /cancel):")

@router.message(SendEmail.choose_email)
async def send_email_manual(message: Message, state: FSMContext):
    email = message.text.strip()
    data = await state.get_data()
    await state.clear()
    u = db.get_user_by_name(data["target_name"])
    if not u:
        await message.answer("Пользователь не найден.")
        return
    await _do_send_email(message, u, email)

async def _do_send_email(message: Message, u: dict, email: str):
    from email_sender import send_config_email
    hy2 = manager.build_hy2_uri(u)
    sub = manager.build_sub_url(u)
    cfg = manager.build_config_url(u)
    await message.answer(f"📧 Отправляю на {email}…")
    ok, err = send_config_email(email, u["name"], hy2, sub, cfg)
    if ok:
        await message.answer(f"✅ Конфигурация отправлена на {email}")
    else:
        await message.answer(f"❌ Ошибка отправки: {err}\n\nПроверьте SMTP настройки в .env")


# ── Запуск ────────────────────────────────────────────────────────────────

async def set_commands(bot: Bot):
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="help", description="Справка"),
        BotCommand(command="add", description="Добавить пользователя (с SNI/инсикьюр)"),
        BotCommand(command="users", description="Список пользователей"),
        BotCommand(command="info", description="Инфо: /info [имя]"),
        BotCommand(command="stop", description="Приостановить: /stop [имя]"),
        BotCommand(command="start_user", description="Возобновить: /start_user [имя]"),
        BotCommand(command="delete", description="Удалить: /delete [имя]"),
        BotCommand(command="qr", description="QR-коды: /qr [имя]"),
        BotCommand(command="reset_traffic", description="Сброс трафика: /reset_traffic [имя]"),
        BotCommand(command="set_limit", description="Лимит: /set_limit [имя] [GB]"),
        BotCommand(command="set_expire", description="Срок: /set_expire [имя] [дни]"),
        BotCommand(command="send_email", description="Email конфиг: /send_email [имя]"),
        BotCommand(command="status", description="Статус сервиса"),
        BotCommand(command="cancel", description="Отменить текущее действие"),
    ])

async def main():
    db.init_db()
    # Scheduler (сбор трафика) запускается ТОЛЬКО в web.py.
    # Бот читает трафик и онлайн-статус из БД через db.get_online_users_db().

    # Прокси: если HTTPS_PROXY задан в .env — используем его для подключения к Telegram
    if HTTPS_PROXY:
        from aiohttp import ClientSession
        from aiogram.client.session.aiohttp import AiohttpSession
        session = AiohttpSession(proxy=HTTPS_PROXY)
        bot = Bot(token=BOT_TOKEN,
                  default=DefaultBotProperties(parse_mode=ParseMode.HTML),
                  session=session)
        log.info(f"Bot using proxy: {HTTPS_PROXY}")
    else:
        bot = Bot(token=BOT_TOKEN,
                  default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        log.info("Bot connecting directly")

    dp = Dispatcher(storage=MemoryStorage())
    dp.message.middleware(AdminMiddleware())
    dp.callback_query.middleware(AdminMiddleware())
    dp.include_router(router)
    await set_commands(bot)
    log.info("Bot started")
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
