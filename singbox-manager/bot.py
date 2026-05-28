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
from config import BOT_TOKEN, ADMIN_IDS, WEB_BASE_URL, SERVER_HOST, CLIENT_HOST, DEFAULT_SNI, DEFAULT_INSECURE, HTTPS_PROXY

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
    host = manager._get_user_host(u)
    if host != SERVER_HOST:
        lines.append(f"  Сервер: <code>{host}</code>")
    if u.get("sni"):
        lines.append(f"  SNI: <code>{u['sni']}</code>")
    if u.get("allow_insecure"):
        lines.append("  🔓 Разрешены небезопасные сертификаты")
    if u.get("notes"):
        lines.append(f"  Заметка: {u['notes']}")
    return "\n".join(lines)

def fmt_edit_card(u: dict) -> str:
    """Карточка пользователя для меню /edit."""
    total = u.get("traffic_used_rx", 0) + u.get("traffic_used_tx", 0)
    limit = f"{u['traffic_limit_gb']} GB" if u.get("traffic_limit_gb") else "∞"
    exp = (u.get("expire_at") or "")[:10] or "∞"
    host = manager._get_user_host(u)
    lines = [
        f"✏️ <b>Редактирование: {u['name']}</b>",
        f"  Статус: <code>{u['status']}</code>",
        f"  Сервер: <code>{host}</code>",
        f"  Email: {u.get('email') or '—'}",
        f"  Трафик: {manager.fmt_bytes(total)} / {limit}",
        f"  Истекает: {exp}",
    ]
    if u.get("sni"):
        lines.append(f"  SNI: <code>{u['sni']}</code>")
    if u.get("allow_insecure"):
        lines.append("  🔓 Небезопасные сертификаты: ВКЛ")
    else:
        lines.append("  🔒 Небезопасные сертификаты: ВЫКЛ")
    if u.get("notes"):
        lines.append(f"  Заметка: {u['notes']}")
    return "\n".join(lines)

def edit_menu_kb(name: str) -> InlineKeyboardMarkup:
    """Клавиатура меню редактирования."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🌐 Адрес сервера", callback_data=f"edit_host:{name}"),
            InlineKeyboardButton(text="🔐 SNI",           callback_data=f"edit_sni:{name}"),
        ],
        [
            InlineKeyboardButton(text="📧 Email",         callback_data=f"edit_email:{name}"),
            InlineKeyboardButton(text="📊 Лимит",         callback_data=f"edit_limit:{name}"),
        ],
        [
            InlineKeyboardButton(text="📅 Срок",          callback_data=f"edit_expire:{name}"),
            InlineKeyboardButton(text="📝 Заметка",       callback_data=f"edit_notes:{name}"),
        ],
        [
            InlineKeyboardButton(text="🔓 Insecure",      callback_data=f"edit_insecure:{name}"),
            InlineKeyboardButton(text="🔄 Сбросить трафик", callback_data=f"edit_reset_traffic:{name}"),
        ],
        [
            InlineKeyboardButton(text="🔑 Новый пароль",  callback_data=f"edit_reset_password:{name}"),
            InlineKeyboardButton(text="🔗 Отозвать подписку", callback_data=f"edit_revoke_sub:{name}"),
        ],
        [
            InlineKeyboardButton(text="❌ Закрыть",       callback_data="edit_close"),
        ],
    ])

def get_online() -> set[str]:
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


# ── FSM ────────────────────────────────────────────────────────────────────

class AddUser(StatesGroup):
    name = State()
    server_host = State()
    email = State()
    traffic_limit = State()
    expire_days = State()
    notes = State()
    sni = State()
    allow_insecure = State()
    offer_email = State()

class EditUser(StatesGroup):
    # Каждое поле — отдельное состояние ожидания ввода
    host    = State()
    sni     = State()
    email   = State()
    limit   = State()
    expire  = State()
    notes   = State()

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
        "/edit [имя] — редактировать пользователя\n"
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
        " <b>/edit [имя]</b> — редактировать параметры пользователя через меню\n"
        " <b>/cancel</b> — отменить текущий ввод\n"
        " <b>/users</b> — список всех пользователей\n"
        " <b>/info [имя]</b> — детальная информация\n"
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


# ── /edit — FSM ────────────────────────────────────────────────────────────

@router.message(Command("edit"))
async def cmd_edit(message: Message, state: FSMContext):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /edit [имя]")
        return
    name = parts[1].strip()
    u = db.get_user_by_name(name)
    if not u or u["status"] == "deleted":
        await message.answer(f"Пользователь «{name}» не найден.")
        return
    await state.update_data(edit_name=name)
    await message.answer(fmt_edit_card(u), reply_markup=edit_menu_kb(name))


async def _refresh_edit_menu(call: CallbackQuery, name: str):
    """Обновляет карточку и меню после изменения."""
    u = db.get_user_by_name(name)
    if not u:
        await call.message.edit_text("Пользователь не найден.")
        return
    try:
        await call.message.edit_text(fmt_edit_card(u), reply_markup=edit_menu_kb(name))
    except Exception:
        pass  # текст не изменился — Telegram вернёт ошибку, игнорируем


# ── Кнопки меню /edit ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("edit_host:"))
async def cb_edit_host(call: CallbackQuery, state: FSMContext):
    name = call.data.split(":", 1)[1]
    await state.update_data(edit_name=name)
    await state.set_state(EditUser.host)
    u = db.get_user_by_name(name)
    current = manager._get_user_host(u)
    await call.message.answer(
        f"🌐 <b>Адрес сервера</b> для <b>{name}</b>\n"
        f"Сейчас: <code>{current}</code>\n\n"
        f"Введите новый адрес или /skip чтобы сбросить на дефолт (<code>{SERVER_HOST}</code>):\n\n"
        "/cancel — отмена"
    )
    await call.answer()


@router.message(EditUser.host)
async def edit_host_input(message: Message, state: FSMContext):
    data = await state.get_data()
    name = data["edit_name"]
    text = message.text.strip()
    if text.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    new_host = None if text.lower() == "/skip" else text
    try:
        manager.set_server_host(name, new_host or SERVER_HOST)
        await state.clear()
        u = db.get_user_by_name(name)
        effective = manager._get_user_host(u)
        await message.answer(
            f"✅ Адрес сервера для <b>{name}</b> изменён: <code>{effective}</code>"
        )
        await message.answer(fmt_edit_card(u), reply_markup=edit_menu_kb(name))
    except Exception as e:
        await state.clear()
        await message.answer(f"❌ Ошибка: {e}")
        u = db.get_user_by_name(name)
        if u:
            await message.answer(fmt_edit_card(u), reply_markup=edit_menu_kb(name))


@router.callback_query(F.data.startswith("edit_sni:"))
async def cb_edit_sni(call: CallbackQuery, state: FSMContext):
    name = call.data.split(":", 1)[1]
    await state.update_data(edit_name=name)
    await state.set_state(EditUser.sni)
    u = db.get_user_by_name(name)
    current = u.get("sni") or "—"
    await call.message.answer(
        f"🔐 <b>SNI</b> для <b>{name}</b>\n"
        f"Сейчас: <code>{current}</code>\n\n"
        "Введите новый SNI, /skip чтобы очистить, /cancel — отмена:"
    )
    await call.answer()


@router.message(EditUser.sni)
async def edit_sni_input(message: Message, state: FSMContext):
    data = await state.get_data()
    name = data["edit_name"]
    text = message.text.strip()
    if text.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    new_sni = None if text.lower() == "/skip" else text
    try:
        manager.set_sni(name, new_sni or "")
        await state.clear()
        u = db.get_user_by_name(name)
        val = u.get("sni") or "—"
        await message.answer(f"✅ SNI для <b>{name}</b>: <code>{val}</code>")
        await message.answer(fmt_edit_card(u), reply_markup=edit_menu_kb(name))
    except Exception as e:
        await state.clear()
        await message.answer(f"❌ Ошибка: {e}")
        u = db.get_user_by_name(name)
        if u:
            await message.answer(fmt_edit_card(u), reply_markup=edit_menu_kb(name))


@router.callback_query(F.data.startswith("edit_email:"))
async def cb_edit_email(call: CallbackQuery, state: FSMContext):
    name = call.data.split(":", 1)[1]
    await state.update_data(edit_name=name)
    await state.set_state(EditUser.email)
    u = db.get_user_by_name(name)
    current = u.get("email") or "—"
    await call.message.answer(
        f"📧 <b>Email</b> для <b>{name}</b>\n"
        f"Сейчас: <code>{current}</code>\n\n"
        "Введите новый email, /skip чтобы очистить, /cancel — отмена:"
    )
    await call.answer()


@router.message(EditUser.email)
async def edit_email_input(message: Message, state: FSMContext):
    data = await state.get_data()
    name = data["edit_name"]
    text = message.text.strip()
    if text.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    new_email = None if text.lower() == "/skip" else text
    try:
        manager.set_email(name, new_email or "")
        await state.clear()
        u = db.get_user_by_name(name)
        val = u.get("email") or "—"
        await message.answer(f"✅ Email для <b>{name}</b>: <code>{val}</code>")
        await message.answer(fmt_edit_card(u), reply_markup=edit_menu_kb(name))
    except Exception as e:
        await state.clear()
        await message.answer(f"❌ Ошибка: {e}")
        u = db.get_user_by_name(name)
        if u:
            await message.answer(fmt_edit_card(u), reply_markup=edit_menu_kb(name))


@router.callback_query(F.data.startswith("edit_limit:"))
async def cb_edit_limit(call: CallbackQuery, state: FSMContext):
    name = call.data.split(":", 1)[1]
    await state.update_data(edit_name=name)
    await state.set_state(EditUser.limit)
    u = db.get_user_by_name(name)
    current = f"{u.get('traffic_limit_gb', 0)} GB" if u.get("traffic_limit_gb") else "∞"
    await call.message.answer(
        f"📊 <b>Лимит трафика</b> для <b>{name}</b>\n"
        f"Сейчас: <code>{current}</code>\n\n"
        "Введите новый лимит в ГБ (0 = без лимита), /cancel — отмена:"
    )
    await call.answer()


@router.message(EditUser.limit)
async def edit_limit_input(message: Message, state: FSMContext):
    data = await state.get_data()
    name = data["edit_name"]
    text = message.text.strip()
    if text.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    try:
        gb = float(text)
        if gb < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите число ≥ 0 (например: 50 или 0.5):")
        return
    try:
        manager.set_traffic_limit(name, gb)
        await state.clear()
        u = db.get_user_by_name(name)
        val = f"{gb} GB" if gb else "∞"
        await message.answer(f"✅ Лимит для <b>{name}</b>: {val}")
        await message.answer(fmt_edit_card(u), reply_markup=edit_menu_kb(name))
    except Exception as e:
        await state.clear()
        await message.answer(f"❌ Ошибка: {e}")
        u = db.get_user_by_name(name)
        if u:
            await message.answer(fmt_edit_card(u), reply_markup=edit_menu_kb(name))


@router.callback_query(F.data.startswith("edit_expire:"))
async def cb_edit_expire(call: CallbackQuery, state: FSMContext):
    name = call.data.split(":", 1)[1]
    await state.update_data(edit_name=name)
    await state.set_state(EditUser.expire)
    u = db.get_user_by_name(name)
    current = (u.get("expire_at") or "")[:10] or "∞"
    await call.message.answer(
        f"📅 <b>Срок действия</b> для <b>{name}</b>\n"
        f"Сейчас истекает: <code>{current}</code>\n\n"
        "Введите количество дней от сегодня (0 = без срока), /cancel — отмена:"
    )
    await call.answer()


@router.message(EditUser.expire)
async def edit_expire_input(message: Message, state: FSMContext):
    data = await state.get_data()
    name = data["edit_name"]
    text = message.text.strip()
    if text.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    try:
        days = int(text)
        if days < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое число ≥ 0:")
        return
    try:
        manager.set_expire(name, days)
        await state.clear()
        u = db.get_user_by_name(name)
        val = (u.get("expire_at") or "")[:10] or "∞"
        await message.answer(f"✅ Срок для <b>{name}</b>: {val}")
        await message.answer(fmt_edit_card(u), reply_markup=edit_menu_kb(name))
    except Exception as e:
        await state.clear()
        await message.answer(f"❌ Ошибка: {e}")
        u = db.get_user_by_name(name)
        if u:
            await message.answer(fmt_edit_card(u), reply_markup=edit_menu_kb(name))


@router.callback_query(F.data.startswith("edit_notes:"))
async def cb_edit_notes(call: CallbackQuery, state: FSMContext):
    name = call.data.split(":", 1)[1]
    await state.update_data(edit_name=name)
    await state.set_state(EditUser.notes)
    u = db.get_user_by_name(name)
    current = u.get("notes") or "—"
    await call.message.answer(
        f"📝 <b>Заметка</b> для <b>{name}</b>\n"
        f"Сейчас: <code>{current}</code>\n\n"
        "Введите новую заметку, /skip чтобы очистить, /cancel — отмена:"
    )
    await call.answer()


@router.message(EditUser.notes)
async def edit_notes_input(message: Message, state: FSMContext):
    data = await state.get_data()
    name = data["edit_name"]
    text = message.text.strip()
    if text.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    new_notes = None if text.lower() == "/skip" else text
    try:
        db.update_user_field(name, "notes", new_notes)
        await state.clear()
        u = db.get_user_by_name(name)
        val = u.get("notes") or "—"
        await message.answer(f"✅ Заметка для <b>{name}</b>: {val}")
        await message.answer(fmt_edit_card(u), reply_markup=edit_menu_kb(name))
    except Exception as e:
        await state.clear()
        await message.answer(f"❌ Ошибка: {e}")
        u = db.get_user_by_name(name)
        if u:
            await message.answer(fmt_edit_card(u), reply_markup=edit_menu_kb(name))


@router.callback_query(F.data.startswith("edit_insecure:"))
async def cb_edit_insecure(call: CallbackQuery, state: FSMContext):
    name = call.data.split(":", 1)[1]
    u = db.get_user_by_name(name)
    current = bool(u.get("allow_insecure", 0))
    new_val = not current
    try:
        manager.set_allow_insecure(name, new_val)
        status_str = "ВКЛ 🔓" if new_val else "ВЫКЛ 🔒"
        await call.answer(f"Небезопасные сертификаты: {status_str}", show_alert=False)
        await _refresh_edit_menu(call, name)
    except Exception as e:
        await call.answer(f"❌ Ошибка: {e}", show_alert=True)


@router.callback_query(F.data.startswith("edit_reset_traffic:"))
async def cb_edit_reset_traffic(call: CallbackQuery, state: FSMContext):
    name = call.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, сбросить", callback_data=f"edit_reset_confirm:{name}"),
        InlineKeyboardButton(text="❌ Отмена",       callback_data=f"edit_reset_cancel:{name}"),
    ]])
    await call.message.answer(
        f"🔄 Сбросить трафик для <b>{name}</b>? Действие необратимо.",
        reply_markup=kb
    )
    await call.answer()


@router.callback_query(F.data.startswith("edit_reset_confirm:"))
async def cb_edit_reset_confirm(call: CallbackQuery, state: FSMContext):
    name = call.data.split(":", 1)[1]
    try:
        db.reset_traffic(name)
        try:
            import traffic_collector
            traffic_collector.reset_user_stats(name)
        except Exception as e:
            log.warning("reset_user_stats failed: %s", e)
        await call.message.edit_text(f"✅ Трафик для <b>{name}</b> сброшен.")
        u = db.get_user_by_name(name)
        await call.message.answer(fmt_edit_card(u), reply_markup=edit_menu_kb(name))
    except Exception as e:
        await call.message.edit_text(f"❌ Ошибка сброса трафика: {e}")
        u = db.get_user_by_name(name)
        if u:
            await call.message.answer(fmt_edit_card(u), reply_markup=edit_menu_kb(name))
    await call.answer()


@router.callback_query(F.data.startswith("edit_reset_cancel:"))
async def cb_edit_reset_cancel(call: CallbackQuery, state: FSMContext):
    name = call.data.split(":", 1)[1]
    await call.message.edit_text("Сброс трафика отменён.")
    u = db.get_user_by_name(name)
    await call.message.answer(fmt_edit_card(u), reply_markup=edit_menu_kb(name))
    await call.answer()


# ── Новый пароль ───────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("edit_reset_password:"))
async def cb_edit_reset_password(call: CallbackQuery):
    name = call.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, сменить", callback_data=f"edit_reset_password_confirm:{name}"),
        InlineKeyboardButton(text="❌ Отмена",      callback_data=f"edit_reset_password_cancel:{name}"),
    ]])
    await call.message.answer(
        f"🔑 Сгенерировать новый пароль для <b>{name}</b>?\n"
        "Старый URI перестанет работать. Клиент получит новый URI через подписку.",
        reply_markup=kb
    )
    await call.answer()


@router.callback_query(F.data.startswith("edit_reset_password_confirm:"))
async def cb_edit_reset_password_confirm(call: CallbackQuery):
    name = call.data.split(":", 1)[1]
    try:
        new_pwd = manager.reset_password(name)
        u = db.get_user_by_name(name)
        new_uri = manager.build_hy2_uri(u)
        await call.message.edit_text(
            f"✅ Пароль для <b>{name}</b> обновлён.\n\n"
            f"Новый URI:\n<code>{new_uri}</code>"
        )
        await call.message.answer(fmt_edit_card(u), reply_markup=edit_menu_kb(name))
    except Exception as e:
        await call.message.edit_text(f"❌ Ошибка смены пароля: {e}")
        u = db.get_user_by_name(name)
        if u:
            await call.message.answer(fmt_edit_card(u), reply_markup=edit_menu_kb(name))
    await call.answer()


@router.callback_query(F.data.startswith("edit_reset_password_cancel:"))
async def cb_edit_reset_password_cancel(call: CallbackQuery):
    name = call.data.split(":", 1)[1]
    await call.message.edit_text("Смена пароля отменена.")
    u = db.get_user_by_name(name)
    if u:
        await call.message.answer(fmt_edit_card(u), reply_markup=edit_menu_kb(name))
    await call.answer()


# ── Отозвать подписку ──────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("edit_revoke_sub:"))
async def cb_edit_revoke_sub(call: CallbackQuery):
    name = call.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, отозвать", callback_data=f"edit_revoke_sub_confirm:{name}"),
        InlineKeyboardButton(text="❌ Отмена",       callback_data=f"edit_revoke_sub_cancel:{name}"),
    ]])
    await call.message.answer(
        f"🔗 Отозвать ссылку подписки для <b>{name}</b>?\n"
        "Старая ссылка сразу перестанет работать. Новую можно получить через /info или /qr.",
        reply_markup=kb
    )
    await call.answer()


@router.callback_query(F.data.startswith("edit_revoke_sub_confirm:"))
async def cb_edit_revoke_sub_confirm(call: CallbackQuery):
    name = call.data.split(":", 1)[1]
    try:
        manager.revoke_sub_token(name)
        u = db.get_user_by_name(name)
        new_sub = manager.build_sub_url(u)
        await call.message.edit_text(
            f"✅ Ссылка подписки для <b>{name}</b> отозвана.\n\n"
            f"Новая ссылка:\n{new_sub}"
        )
        await call.message.answer(fmt_edit_card(u), reply_markup=edit_menu_kb(name))
    except Exception as e:
        await call.message.edit_text(f"❌ Ошибка отзыва подписки: {e}")
        u = db.get_user_by_name(name)
        if u:
            await call.message.answer(fmt_edit_card(u), reply_markup=edit_menu_kb(name))
    await call.answer()


@router.callback_query(F.data.startswith("edit_revoke_sub_cancel:"))
async def cb_edit_revoke_sub_cancel(call: CallbackQuery):
    name = call.data.split(":", 1)[1]
    await call.message.edit_text("Отзыв подписки отменён.")
    u = db.get_user_by_name(name)
    if u:
        await call.message.answer(fmt_edit_card(u), reply_markup=edit_menu_kb(name))
    await call.answer()


@router.callback_query(F.data == "edit_close")
async def cb_edit_close(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Редактирование закрыто.")
    await call.answer()


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
        f"По умолчанию: <code>{CLIENT_HOST}</code>\n\n"
        f"Введите адрес или /skip для использования дефолтного:"
    )

@router.message(AddUser.server_host)
async def add_server_host(message: Message, state: FSMContext):
    text = message.text.strip()
    host = CLIENT_HOST if text.lower() == "/skip" else text
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
    await state.set_state(AddUser.sni)
    _sni_default_display = DEFAULT_SNI if DEFAULT_SNI else "не задан (клиент использует хост из URI)"
    await message.answer(
        "🔐 <b>SNI</b> (Server Name Indication)\n"
        "Обычно совпадает с адресом сервера. Укажите, если используете CDN/прокси.\n"
        f"По умолчанию: <code>{_sni_default_display}</code>\n\n"
        "Введите SNI или /skip для использования дефолтного:"
    )

@router.message(AddUser.sni)
async def add_sni(message: Message, state: FSMContext):
    text = message.text.strip()
    if text.lower() == "/skip" or not text:
        sni = DEFAULT_SNI if DEFAULT_SNI else None
    else:
        sni = text
    await state.update_data(sni=sni)

    await state.set_state(AddUser.allow_insecure)
    if DEFAULT_INSECURE:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да (по умолчанию)", callback_data="insecure_yes"),
            InlineKeyboardButton(text="❌ Нет", callback_data="insecure_no"),
        ]])
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да, разрешить", callback_data="insecure_yes"),
            InlineKeyboardButton(text="❌ Нет (по умолчанию)", callback_data="insecure_no"),
        ]])
    await message.answer(
        "🔓 <b>Разрешить небезопасные сертификаты?</b>\n"
        "Включите, только если используете самоподписанный сертификат.\n"
        f"⚠️ По умолчанию из .env: {'ДА (insecure=1)' if DEFAULT_INSECURE else 'НЕТ (insecure=0)'}",
        reply_markup=kb
    )

@router.callback_query(F.data.in_({"insecure_yes", "insecure_no"}))
async def add_allow_insecure(call: CallbackQuery, state: FSMContext):
    allow_insecure = call.data == "insecure_yes"
    await state.update_data(allow_insecure=allow_insecure)

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

    hy2 = manager.build_hy2_uri(user)
    sub = manager.build_sub_url(user)
    lim = f"{user['traffic_limit_gb']} GB" if user.get("traffic_limit_gb") else "∞"
    exp = data.get("expire_days", 0)
    sni_str = f"  SNI: <code>{data.get('sni') or 'по умолчанию'}</code>\n" if data.get("sni") else ""
    insecure_str = "🔓 Разрешены небезопасные сертификаты\n" if data.get("allow_insecure") else ""

    await call.message.edit_text(
        f"✅ Пользователь <b>{user['name']}</b> создан!\n\n"
        f"  Сервер: <code>{data.get('server_host', CLIENT_HOST)}</code>\n"
        f"{sni_str}"
        f"{insecure_str}"
        f"  Трафик: {lim}  |  Срок: {f'{exp} дн.' if exp else '∞'}\n"
        f"  Email: {user.get('email') or '—'}\n\n"
        f"<code>{hy2}</code>\n\n"
        f"🔗 Подписка: {sub}"
    )
    await call.message.answer_photo(qr(hy2), caption="QR — URI подключения")
    await call.message.answer_photo(qr(sub), caption="QR — ссылка подписки")

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
    import html as _html
    from email_sender import send_config_email
    hy2 = manager.build_hy2_uri(u)
    sub = manager.build_sub_url(u)
    cfg = manager.build_config_url(u)
    await message.answer(f"📧 Отправляю на {email}…")
    ok, err = send_config_email(email, u["name"], hy2, sub, cfg)
    if ok:
        await message.answer(f"✅ Конфигурация отправлена на {email}")
    else:
        safe_err = _html.escape(str(err))
        await message.answer(f"❌ Ошибка отправки:\n<code>{safe_err}</code>\n\nПроверьте SMTP настройки в .env")


# ── Запуск ────────────────────────────────────────────────────────────────

async def set_commands(bot: Bot):
    await bot.set_my_commands([
        BotCommand(command="start",         description="Главное меню"),
        BotCommand(command="help",          description="Справка"),
        BotCommand(command="add",           description="Добавить пользователя"),
        BotCommand(command="edit",          description="Редактировать: /edit [имя]"),
        BotCommand(command="users",         description="Список пользователей"),
        BotCommand(command="info",          description="Инфо: /info [имя]"),
        BotCommand(command="stop",          description="Приостановить: /stop [имя]"),
        BotCommand(command="start_user",    description="Возобновить: /start_user [имя]"),
        BotCommand(command="delete",        description="Удалить: /delete [имя]"),
        BotCommand(command="qr",            description="QR-коды: /qr [имя]"),
        BotCommand(command="reset_traffic", description="Сброс трафика: /reset_traffic [имя]"),
        BotCommand(command="set_limit",     description="Лимит: /set_limit [имя] [GB]"),
        BotCommand(command="set_expire",    description="Срок: /set_expire [имя] [дни]"),
        BotCommand(command="send_email",    description="Email конфиг: /send_email [имя]"),
        BotCommand(command="status",        description="Статус сервиса"),
        BotCommand(command="cancel",        description="Отменить текущее действие"),
    ])

async def main():
    db.init_db()

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
