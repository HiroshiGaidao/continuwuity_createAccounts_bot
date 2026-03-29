# bot.py
import asyncio
import telebot
from telebot import types
import secrets
import string
import re
import os
import logging
from logging.handlers import RotatingFileHandler
from aiohttp import ClientSession
from nio import AsyncClient, RoomMessageText, MatrixRoom, LoginResponse
from config import CONFIG
from database import (
    add_user, get_user, ban_user, unban_user, is_user_banned,
    add_matrix_account, get_user_accounts, hard_delete_matrix_account,
    get_all_users, get_stats, log_action, search_users, get_logs
)

# ================= НАСТРОЙКА ЛОГИРОВАНИЯ =================
os.makedirs(CONFIG["LOG_PATH"], exist_ok=True)
os.chmod(CONFIG["LOG_PATH"], 0o755)

logger = logging.getLogger('MatrixRegBot')
logger.setLevel(getattr(logging, CONFIG["LOG_LEVEL"].upper(), logging.INFO))

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
console_handler.setFormatter(console_formatter)

file_handler = RotatingFileHandler(
    os.path.join(CONFIG["LOG_PATH"], 'bot.log'),
    maxBytes=CONFIG["LOG_MAX_BYTES"],
    backupCount=CONFIG["LOG_BACKUP_COUNT"],
    encoding='utf-8'
)
file_handler.setLevel(logging.DEBUG)
file_formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
file_handler.setFormatter(file_formatter)

logger.addHandler(console_handler)
logger.addHandler(file_handler)

telebot_logger = logging.getLogger('telebot')
telebot_logger.setLevel(logging.WARNING)
telebot_logger.addHandler(file_handler)

# ================= КОНСТАНТЫ =================
MAX_ACCOUNTS_PER_USER = 3
# Ссылки
TELEGRAPH_GUIDE = "https://telegra.ph/Matrix-Guide-01-01"  # Замените на вашу инструкцию
SERVER_INFO = f"🌐 **Наш сервер:** `{CONFIG['MATRIX_DOMAIN']}`\n\n✅ Работает стабильно\n🔒 Шифрование включено\n🌍 Федерация активна"

# Главное меню
MAIN_MENU_TEXT = """
👋 **Добро пожаловать в Matrix Registration Bot!**

Выберите действие:
"""

HELP_TEXT = """
📖 **Справка по боту**

🔹 **Доступные команды:**
• /start — Главное меню
• /register — Регистрация нового аккаунта
• /myaccounts — Мои аккаунты Matrix
• /resetpassword — Сбросить пароль
• /help — Эта справка

🔹 **Возможности:**
✅ Регистрация аккаунтов
✅ Управление паролями
✅ Несколько аккаунтов на пользователя

🔹 **Ограничения:**
• Максимум 3 аккаунта
• Логин: 3-20 символов (латиница + цифры)

❓ **Вопросы?** Свяжитесь с администратором.
"""

# ================= ВАЛИДАЦИЯ =================

def sanitize_username(username: str) -> str:
    username = username.strip().lower()
    username = re.sub(r'[^a-z0-9]', '', username)
    return username[:20]

def validate_username(username: str) -> tuple:
    if not username:
        return False, "❌ Логин не может быть пустым"
    if len(username) < 3:
        return False, "❌ Логин должен быть не менее 3 символов"
    if len(username) > 20:
        return False, "❌ Логин не должен превышать 20 символов"
    if not re.match(r'^[a-z][a-z0-9]{2,19}$', username):
        return False, "❌ Логин должен начинаться с буквы"
    forbidden = ['admin', 'root', 'system', 'matrix', 'bot', 'support', 'help', 'moderator']
    if username.lower() in forbidden:
        return False, f"❌ Логин '{username}' зарезервирован"
    return True, ""

def sanitize_input(text: str, max_length: int = 500) -> str:
    if not text:
        return ""
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    return text.strip()[:max_length]

# ================= ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ =================
user_states = {}
pending_requests = {}
tg_bot = telebot.TeleBot(CONFIG["TG_BOT_TOKEN"])

matrix_client = None
matrix_ready = asyncio.Event()
matrix_loop = None

# ================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================

def run_async_on_matrix_loop(coro):
    global matrix_loop
    if matrix_loop is None:
        logger.error("❌ Matrix loop ещё не инициализирован!")
        return None
    future = asyncio.run_coroutine_threadsafe(coro, matrix_loop)
    try:
        return future.result(timeout=60.0)
    except asyncio.TimeoutError:
        logger.error("❌ Timeout при выполнении async-операции")
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка выполнения async: {e}", exc_info=True)
        return None

# ================= КЛАВИАТУРЫ =================

def main_menu_keyboard():
    """Главное меню"""
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📝 Регистрация", callback_data="menu_register"),
        types.InlineKeyboardButton("📋 Мои аккаунты", callback_data="menu_accounts")
    )
    markup.add(
        # types.InlineKeyboardButton("🔑 Сброс пароля", callback_data="menu_resetpwd"),
        types.InlineKeyboardButton("📖 Помощь", callback_data="menu_help")
    )
    return markup

def back_keyboard(back_callback="menu_main"):
    """Кнопка назад"""
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data=back_callback))
    return markup

def accounts_keyboard(accounts):
    """Клавиатура со списком аккаунтов"""
    markup = types.InlineKeyboardMarkup()
    for acc in accounts:
        markup.add(types.InlineKeyboardButton(
            f"🔑 Сбросить пароль от {acc['matrix_username']}",
            callback_data=f"resetpwd_{acc['matrix_username']}"
        ))
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    return markup

# ================= MATRIX ФУНКЦИИ =================

async def init_matrix_client():
    global matrix_client, matrix_loop
    
    logger.info("🔄 Инициализация Matrix клиента...")
    
    try:
        matrix_loop = asyncio.get_running_loop()
        matrix_client = AsyncClient(
            CONFIG["MATRIX_SERVER_URL"],
            CONFIG["MATRIX_BOT_USER"],
            store_path=CONFIG["MATRIX_STORE_PATH"],
        )
        
        async def on_message(room: MatrixRoom, message: RoomMessageText):
            pass
        
        matrix_client.add_event_callback(on_message, RoomMessageText)
        
        resp = await matrix_client.login(CONFIG["MATRIX_BOT_PASSWORD"])
        
        if isinstance(resp, LoginResponse):
            logger.info(f"✅ Matrix bot logged in: {resp.user_id}")
            matrix_ready.set()
        else:
            logger.error(f"❌ Matrix login failed: {resp}")
        
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации Matrix: {e}", exc_info=True)
    
    asyncio.create_task(matrix_sync_loop())

async def matrix_sync_loop():
    sync_count = 0
    while True:
        try:
            sync_count += 1
            await matrix_client.sync(timeout=30000)
            if sync_count % 10 == 0:
                logger.info(f"📊 Sync: {sync_count} циклов")
        except Exception as e:
            logger.error(f"❌ Matrix sync error: {e}", exc_info=True)
            await asyncio.sleep(5)

async def send_admin_command_async(command: str) -> bool:
    logger.info(f"📤 Команда Matrix: {command}")
    
    try:
        await asyncio.wait_for(matrix_ready.wait(), timeout=30.0)
        
        if matrix_client is None or not matrix_client.logged_in:
            return False
        
        resp = await matrix_client.room_send(
            room_id=CONFIG["MATRIX_ADMIN_ROOM_ID"],
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": command}
        )
        
        if hasattr(resp, 'event_id'):
            logger.info(f"✅ Отправлено: {resp.event_id}")
        
        await asyncio.sleep(1)
        return True
        
    except Exception as e:
        logger.error(f"❌ Ошибка отправки: {e}", exc_info=True)
        return False

def send_admin_command(command: str) -> bool:
    return run_async_on_matrix_loop(send_admin_command_async(command))

async def check_username_available_async(username: str) -> bool:
    url = f"{CONFIG['MATRIX_SERVER_URL']}/_matrix/client/v3/register/available"
    params = {"username": username}
    try:
        async with ClientSession() as session:
            async with session.get(url, params=params) as resp:
                return resp.status == 200
    except Exception as e:
        logger.error(f"❌ Ошибка проверки: {e}", exc_info=True)
        return None

def check_username_available(username: str) -> bool:
    return run_async_on_matrix_loop(check_username_available_async(username))

def generate_secure_password(length=16):
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return ''.join(secrets.choice(alphabet) for _ in range(length))

# ================= TELEGRAM ОБРАБОТЧИКИ =================

@tg_bot.message_handler(commands=['start'])
def start_command(message):
    logger.info(f"📱 /start от {message.chat.id}")
    
    username = sanitize_input(message.from_user.username or "")
    first_name = sanitize_input(message.from_user.first_name or "")
    
    add_user(message.chat.id, username, first_name)
    
    if is_user_banned(message.chat.id):
        tg_bot.send_message(message.chat.id, "❌ Ваш доступ заблокирован.")
        return
    
    tg_bot.send_message(
        message.chat.id,
        MAIN_MENU_TEXT,
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )

@tg_bot.message_handler(commands=['help'])
def help_command(message):
    """Команда /help - показывает справку"""
    logger.info(f"📱 /help от {message.chat.id}")
    
    if is_user_banned(message.chat.id):
        return
    
    tg_bot.send_message(
        message.chat.id,
        HELP_TEXT,
        reply_markup=back_keyboard("menu_main"),
        parse_mode="Markdown"
    )

@tg_bot.message_handler(commands=['register'])
def register_command(message):
    logger.info(f"📱 /register от {message.chat.id}")
    
    if is_user_banned(message.chat.id):
        tg_bot.send_message(message.chat.id, "❌ Доступ заблокирован.")
        return
    
    accounts = get_user_accounts(message.chat.id)
    
    if len(accounts) >= MAX_ACCOUNTS_PER_USER:
        tg_bot.send_message(message.chat.id, f"❌ Достигнут лимит аккаунтов ({MAX_ACCOUNTS_PER_USER}).")
        return
    
    user_states[message.chat.id] = "waiting_username"
    tg_bot.send_message(
        message.chat.id,
        "📝 Введите желаемый **логин** (3-20 символов, латиница + цифры):\n\nПример: `alex`",
        reply_markup=back_keyboard("menu_main"),
        parse_mode="Markdown"
    )

@tg_bot.message_handler(commands=['myaccounts'])
def my_accounts_command(message):
    logger.info(f"📱 /myaccounts от {message.chat.id}")
    
    if is_user_banned(message.chat.id):
        tg_bot.send_message(message.chat.id, "❌ Доступ заблокирован.")
        return
    
    accounts = get_user_accounts(message.chat.id)
    
    if not accounts:
        tg_bot.send_message(
            message.chat.id,
            "📭 У вас пока нет аккаунтов.\n\nИспользуйте меню для регистрации.",
            reply_markup=back_keyboard("menu_main"),
            parse_mode="Markdown"
        )
        return
    
    msg = "📋 **Ваши аккаунты Matrix:**\n\n"
    for acc in accounts:
        msg += f"• `{acc['matrix_full_id']}`\n"
    
    tg_bot.send_message(
        message.chat.id,
        msg,
        reply_markup=accounts_keyboard(accounts),
        parse_mode="Markdown"
    )

@tg_bot.message_handler(commands=['resetpassword'])
def reset_password_command(message):
    logger.info(f"📱 /resetpassword от {message.chat.id}")
    
    if is_user_banned(message.chat.id):
        tg_bot.send_message(message.chat.id, "❌ Доступ заблокирован.")
        return
    
    accounts = get_user_accounts(message.chat.id)
    
    if not accounts:
        tg_bot.send_message(
            message.chat.id,
            "❌ У вас нет аккаунтов для сброса пароля.",
            reply_markup=back_keyboard("menu_main"),
            parse_mode="Markdown"
        )
        return
    
    if len(accounts) == 1:
        reset_password_for_user(message.chat.id, accounts[0]['matrix_username'])
    else:
        msg = "🔑 **Выберите аккаунт:**\n\n"
        for acc in accounts:
            msg += f"• `{acc['matrix_full_id']}`\n"
        
        tg_bot.send_message(
            message.chat.id,
            msg,
            reply_markup=accounts_keyboard(accounts),
            parse_mode="Markdown"
        )

# ================= CALLBACK ОБРАБОТЧИКИ =================

@tg_bot.callback_query_handler(func=lambda call: call.data == "menu_main")
def menu_main_callback(call):
    """Возврат в главное меню"""
    logger.info(f"📱 Callback: menu_main от {call.from_user.id}")
    
    tg_bot.edit_message_text(
        MAIN_MENU_TEXT,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )
    tg_bot.answer_callback_query(call.id)

@tg_bot.callback_query_handler(func=lambda call: call.data == "menu_help")
def menu_help_callback(call):
    """Хелп из меню - показывает общую справку"""
    logger.info(f"📱 Callback: menu_help от {call.from_user.id}")
    
    tg_bot.edit_message_text(
        HELP_TEXT,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=back_keyboard("menu_main"),
        parse_mode="Markdown"
    )
    tg_bot.answer_callback_query(call.id)

@tg_bot.callback_query_handler(func=lambda call: call.data == "menu_register")
def menu_register_callback(call):
    """Регистрация из меню"""
    logger.info(f"📱 Callback: menu_register от {call.from_user.id}")
    
    if is_user_banned(call.from_user.id):
        tg_bot.answer_callback_query(call.id, "❌ Доступ заблокирован", show_alert=True)
        return
    
    accounts = get_user_accounts(call.from_user.id)
    
    if len(accounts) >= MAX_ACCOUNTS_PER_USER:
        tg_bot.answer_callback_query(call.id, f"❌ Лимит аккаунтов ({MAX_ACCOUNTS_PER_USER})", show_alert=True)
        return
    
    user_states[call.from_user.id] = "waiting_username"
    tg_bot.edit_message_text(
        "📝 Введите желаемый **логин** (3-20 символов):\n\nПример: `alex`",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=back_keyboard("menu_main"),
        parse_mode="Markdown"
    )
    tg_bot.answer_callback_query(call.id)

@tg_bot.callback_query_handler(func=lambda call: call.data == "menu_accounts")
def menu_accounts_callback(call):
    """Мои аккаунты из меню"""
    logger.info(f"📱 Callback: menu_accounts от {call.from_user.id}")
    
    accounts = get_user_accounts(call.from_user.id)
    
    if not accounts:
        tg_bot.edit_message_text(
            "📭 У вас пока нет аккаунтов.\n\nИспользуйте меню для регистрации.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=back_keyboard("menu_main"),
            parse_mode="Markdown"
        )
        return
    
    msg = "📋 **Ваши аккаунты Matrix:**\n\n"
    for acc in accounts:
        msg += f"• `{acc['matrix_full_id']}`\n"
    
    tg_bot.edit_message_text(
        msg,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=accounts_keyboard(accounts),
        parse_mode="Markdown"
    )
    tg_bot.answer_callback_query(call.id)

@tg_bot.callback_query_handler(func=lambda call: call.data == "menu_resetpwd")
def menu_resetpwd_callback(call):
    """Сброс пароля из меню"""
    logger.info(f"📱 Callback: menu_resetpwd от {call.from_user.id}")
    
    accounts = get_user_accounts(call.from_user.id)
    
    if not accounts:
        tg_bot.answer_callback_query(call.id, "❌ Нет аккаунтов", show_alert=True)
        return
    
    if len(accounts) == 1:
        reset_password_for_user(call.from_user.id, accounts[0]['matrix_username'])
        tg_bot.answer_callback_query(call.id)
    else:
        msg = "🔑 **Выберите аккаунт:**\n\n"
        for acc in accounts:
            msg += f"• `{acc['matrix_full_id']}`\n"
        
        tg_bot.edit_message_text(
            msg,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=accounts_keyboard(accounts),
            parse_mode="Markdown"
        )
        tg_bot.answer_callback_query(call.id)

@tg_bot.callback_query_handler(func=lambda call: call.data.startswith("resetpwd_"))
def reset_password_callback(call):
    logger.info(f"📱 Callback: resetpwd от {call.from_user.id}")
    
    if call.message.chat.id != call.from_user.id:
        tg_bot.answer_callback_query(call.id, "⛔ Это не ваш запрос", show_alert=True)
        return
    
    username = call.data.split("_")[1]
    reset_password_for_user(call.message.chat.id, username)
    tg_bot.answer_callback_query(call.id)

def reset_password_for_user(tg_chat_id: int, matrix_username: str):
    logger.info(f"🔑 Сброс пароля: {matrix_username}")
    
    new_password = generate_secure_password()
    full_user_id = f"@{matrix_username}:{CONFIG['MATRIX_DOMAIN']}"
    command = f"!admin users password {full_user_id} {new_password}"
    
    success = send_admin_command(command)
    
    if success:
        msg = (
            f"🔑 **Пароль сброшен!**\n\n"
            f"🆔 Аккаунт: `{full_user_id}`\n"
            f"🔕 Новый пароль: `{new_password}`\n\n"
            f"⚠️ Сохраните пароль!"
        )
        tg_bot.send_message(tg_chat_id, msg, parse_mode="Markdown")
        log_action(tg_chat_id, "password_reset", matrix_username)
    else:
        tg_bot.send_message(tg_chat_id, "❌ Ошибка сброса пароля.")

@tg_bot.message_handler(func=lambda m: user_states.get(m.chat.id) == "waiting_username")
def handle_username(message):
    logger.info(f"📱 Логин от {message.chat.id}: {message.text.strip()}")
    
    if is_user_banned(message.chat.id):
        user_states.pop(message.chat.id, None)
        tg_bot.send_message(message.chat.id, "❌ Доступ заблокирован.")
        return
    
    raw_username = sanitize_input(message.text.strip())
    sanitized_username = sanitize_username(raw_username)
    
    is_valid, error_msg = validate_username(sanitized_username)
    if not is_valid:
        tg_bot.send_message(message.chat.id, error_msg, reply_markup=back_keyboard("menu_main"))
        return
    
    tg_bot.send_message(message.chat.id, "🔍 Проверяю доступность...")
    
    available = check_username_available(sanitized_username)
    
    if available is None:
        tg_bot.send_message(message.chat.id, "⚠️ Ошибка проверки.", reply_markup=back_keyboard("menu_main"))
        user_states.pop(message.chat.id, None)
        return
    
    if not available:
        tg_bot.send_message(message.chat.id, "❌ Логин занят.", reply_markup=back_keyboard("menu_main"))
        return

    user_states.pop(message.chat.id, None)
    
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve_{message.chat.id}_{sanitized_username}"),
        types.InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{message.chat.id}_{sanitized_username}")
    )
    
    admin_text = (
        f"🔔 **Новая заявка**\n\n"
        f"👤 Пользователь: `{sanitize_input(message.from_user.first_name or '')}`\n"
        f"🆔 Telegram: `{message.chat.id}`\n"
        f"📝 Логин: `@{sanitized_username}:{CONFIG['MATRIX_DOMAIN']}`"
    )
    
    sent = tg_bot.send_message(CONFIG["ADMIN_CHAT_ID"], admin_text, reply_markup=markup, parse_mode="Markdown")
    pending_requests[sent.message_id] = {"user_chat_id": message.chat.id, "username": sanitized_username}
    
    tg_bot.send_message(
        message.chat.id,
        "✅ Заявка отправлена. Ожидайте подтверждения.",
        reply_markup=back_keyboard("menu_main")
    )

# ================= АДМИН ПАНЕЛЬ =================

@tg_bot.message_handler(commands=['admin'])
def admin_panel(message):
    logger.info(f"📱 /admin от {message.chat.id}")
    
    if message.chat.id != CONFIG["ADMIN_CHAT_ID"]:
        return
    
    stats = get_stats()
    
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("👥 Пользователи", callback_data="admin_users"),
        types.InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")
    )
    markup.add(
        types.InlineKeyboardButton("🔍 Поиск", callback_data="admin_search"),
        types.InlineKeyboardButton("📜 Логи", callback_data="admin_logs")
    )
    markup.add(
        types.InlineKeyboardButton("💾 Скачать логи", callback_data="admin_download_logs")
    )
    
    msg = (
        f"🛡 **Админ-панель**\n\n"
        f"📊 **Статистика:**\n"
        f"• Всего: {stats['total']}\n"
        f"• Активных: {stats['active']}\n"
        f"• Забаненных: {stats['banned']}\n"
        f"• Аккаунтов: {stats['accounts']}"
    )
    
    tg_bot.send_message(message.chat.id, msg, reply_markup=markup, parse_mode="Markdown")

@tg_bot.callback_query_handler(func=lambda call: call.data.startswith("admin_"))
def admin_callback(call):
    if call.message.chat.id != CONFIG["ADMIN_CHAT_ID"]:
        tg_bot.answer_callback_query(call.id, "⛔ Нет прав")
        return
    
    action = call.data.split("_")[1]
    
    if action == "users":
        users = get_all_users(limit=10)
        msg = "👥 **Пользователи:**\n\n"
        markup = types.InlineKeyboardMarkup()
        
        for user in users:
            emoji = "🟢" if user['status'] == 'active' else "🔴"
            msg += f"{emoji} `{user['tg_chat_id']}` — {user['first_name']}\n"
            markup.add(types.InlineKeyboardButton(
                f"⚙️ {user['tg_chat_id']}",
                callback_data=f"admin_user_{user['tg_chat_id']}"
            ))
        markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_main"))
        
        tg_bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")
    
    elif action == "stats":
        stats = get_stats()
        msg = (
            f"📊 **Статистика:**\n\n"
            f"• Всего: {stats['total']}\n"
            f"• Активных: {stats['active']}\n"
            f"• Забаненных: {stats['banned']}\n"
            f"• Аккаунтов: {stats['accounts']}"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_main"))
        tg_bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")
    
    elif action == "search":
        tg_bot.send_message(call.message.chat.id, "🔍 Введите запрос:")
        user_states[call.message.chat.id] = "admin_searching"
    
    elif action == "logs":
        logs = get_logs(days=7, limit=50)
        msg = "📜 **Логи (7 дней):**\n\n"
        for log in logs:
            msg += f"• `{log['action']}`: {log['details']}\n"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_main"))
        tg_bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")
    
    elif action == "download_logs":
        log_file = os.path.join(CONFIG["LOG_PATH"], 'bot.log')
        if os.path.exists(log_file):
            with open(log_file, 'rb') as f:
                tg_bot.send_document(call.message.chat.id, f, caption="📄 Логи")
    
    elif action == "main":
        stats = get_stats()
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("👥 Пользователи", callback_data="admin_users"),
            types.InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")
        )
        markup.add(
            types.InlineKeyboardButton("🔍 Поиск", callback_data="admin_search"),
            types.InlineKeyboardButton("📜 Логи", callback_data="admin_logs")
        )
        markup.add(
            types.InlineKeyboardButton("💾 Скачать логи", callback_data="admin_download_logs")
        )
        msg = (
            f"🛡 **Админ-панель**\n\n"
            f"• Всего: {stats['total']}\n"
            f"• Активных: {stats['active']}\n"
            f"• Забаненных: {stats['banned']}"
        )
        tg_bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")
    
    elif action == "user":
        tg_chat_id = int(call.data.split("_")[2])
        user = get_user(tg_chat_id)
        accounts = get_user_accounts(tg_chat_id)
        
        if not user:
            return
        
        msg = (
            f"👤 **Пользователь**\n\n"
            f"🆔 `{user['tg_chat_id']}`\n"
            f"👤 {user['first_name']}\n"
            f"📊 Аккаунтов: {len(accounts)}\n"
            f"🚫 Статус: `{user['status']}`\n\n"
        )
        
        if accounts:
            msg += "📋 **Аккаунты:**\n"
            for acc in accounts:
                msg += f"• `{acc['matrix_full_id']}`\n"
        
        markup = types.InlineKeyboardMarkup()
        for acc in accounts:
            markup.add(types.InlineKeyboardButton(
                f"🗑️ {acc['matrix_username']}",
                callback_data=f"admin_delete_{tg_chat_id}_{acc['matrix_username']}"
            ))
        
        if user['status'] == 'active':
            markup.add(types.InlineKeyboardButton("🔴 Забанить", callback_data=f"admin_ban_{tg_chat_id}"))
        else:
            markup.add(types.InlineKeyboardButton("🟢 Разбанить", callback_data=f"admin_unban_{tg_chat_id}"))
        markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_users"))
        
        tg_bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")
    
    elif action == "ban" or action == "unban":
        tg_chat_id = int(call.data.split("_")[2])
        
        if action == "ban":
            ban_user(tg_chat_id, "Забанен")
            tg_bot.send_message(tg_chat_id, "❌ Доступ заблокирован.")
        else:
            unban_user(tg_chat_id)
            tg_bot.send_message(tg_chat_id, "✅ Доступ восстановлен.")
        
        log_action(CONFIG["ADMIN_CHAT_ID"], f"admin_{action}", f"User {tg_chat_id}")
        
        user = get_user(tg_chat_id)
        msg = f"✅ {'Забанен' if action == 'ban' else 'Разбанен'}: `{user['first_name']}`"
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_users"))
        tg_bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")
    
    elif action == "delete":
        parts = call.data.split("_")
        tg_chat_id = int(parts[2])
        matrix_username = parts[3]
        full_user_id = f"@{matrix_username}:{CONFIG['MATRIX_DOMAIN']}"
        
        command = f"!admin users deactivate {full_user_id}"
        success = send_admin_command(command)
        
        if success:
            hard_delete_matrix_account(tg_chat_id, matrix_username)
            
            try:
                tg_bot.send_message(tg_chat_id, f"⚠️ Аккаунт `{full_user_id}` удалён администратором.")
            except:
                pass
            
            tg_bot.answer_callback_query(call.id, f"✅ {matrix_username} удалён")
            
            # Обновляем карточку
            user = get_user(tg_chat_id)
            accounts = get_user_accounts(tg_chat_id)
            
            msg = f"✅ **Удалён:** `{matrix_username}`\n\n📊 Осталось: {len(accounts)}"
            
            markup = types.InlineKeyboardMarkup()
            for acc in accounts:
                markup.add(types.InlineKeyboardButton(
                    f"🗑️ {acc['matrix_username']}",
                    callback_data=f"admin_delete_{tg_chat_id}_{acc['matrix_username']}"
                ))
            markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_users"))
            
            tg_bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")
        else:
            tg_bot.answer_callback_query(call.id, "❌ Ошибка", show_alert=True)
    
    tg_bot.answer_callback_query(call.id)

@tg_bot.message_handler(func=lambda m: user_states.get(m.chat.id) == "admin_searching")
def admin_search_handler(message):
    if message.chat.id != CONFIG["ADMIN_CHAT_ID"]:
        return
    
    query = sanitize_input(message.text.strip())
    users = search_users(query)
    user_states.pop(message.chat.id, None)
    
    if not users:
        tg_bot.send_message(message.chat.id, "🔍 Ничего не найдено.")
        return
    
    msg = f"🔍 **Поиск \"{query}\":**\n\n"
    markup = types.InlineKeyboardMarkup()
    
    for user in users:
        msg += f"• `{user['tg_chat_id']}` — {user['first_name']}\n"
        markup.add(types.InlineKeyboardButton(
            f"👤 {user['tg_chat_id']}",
            callback_data=f"admin_user_{user['tg_chat_id']}"
        ))
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_main"))
    
    tg_bot.send_message(message.chat.id, msg, reply_markup=markup, parse_mode="Markdown")

# ================= РЕГИСТРАЦИЯ (АПРУВ) =================

@tg_bot.callback_query_handler(func=lambda call: call.data.startswith("approve_") or call.data.startswith("reject_"))
def registration_callback(call):
    logger.info(f"📱 Registration: {call.data}")
    
    if call.message.chat.id != CONFIG["ADMIN_CHAT_ID"]:
        tg_bot.answer_callback_query(call.id, "⛔ Нет прав")
        return

    parts = call.data.split("_")
    if len(parts) < 3:
        return
    
    action, user_chat_id, username = parts[0], int(parts[1]), parts[2]
    
    tg_bot.answer_callback_query(call.id, "⏳ Обрабатываю...")
    
    if action == "approve":
        password = generate_secure_password()
        full_user_id = f"@{username}:{CONFIG['MATRIX_DOMAIN']}"
        command = f"!admin users create {full_user_id} {password}"
        
        success = send_admin_command(command)
        
        if success:
            add_matrix_account(user_chat_id, username, full_user_id)
            
            # ✅ Сообщение об успешной регистрации с серверной информацией
            msg = (
                f"🎉 **Аккаунт создан!**\n\n"
                f"🆔 **Логин:** `{full_user_id}`\n"
                f"🔑 **Пароль:** `{password}`\n\n"
                f"⚠️ **Сохраните пароль!**\n\n"
                f"{SERVER_INFO}\n\n"
                f"📖 **Инструкция по подключению:**\n{TELEGRAPH_GUIDE}"
            )
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🏠 В главное меню", callback_data="menu_main"))
            
            try:
                tg_bot.send_message(user_chat_id, msg, reply_markup=markup, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"❌ Не удалось отправить: {e}")
            
            tg_bot.edit_message_text(
                f"✅ `{username}` создан!",
                call.message.chat.id,
                call.message.message_id
            )
            tg_bot.answer_callback_query(call.id, f"✅ {username} создан!")
        else:
            tg_bot.answer_callback_query(call.id, "❌ Ошибка!", show_alert=True)
            
    elif action == "reject":
        try:
            tg_bot.send_message(user_chat_id, "❌ Заявка отклонена.")
        except: pass
        tg_bot.edit_message_text(f"❌ `{username}` отклонена.", call.message.chat.id, call.message.message_id)
        tg_bot.answer_callback_query(call.id, "❌ Отклонено")

# ================= ЗАПУСК =================

def start_matrix_background():
    logger.info("🔄 Запуск Matrix клиента...")
    
    def run_async():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(init_matrix_client())
        loop.run_forever()
    
    import threading
    thread = threading.Thread(target=run_async, daemon=True, name="MatrixAsync")
    thread.start()

if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info("🤖 Запуск бота...")
    logger.info("=" * 50)
    
    for key, value in CONFIG.items():
        if 'TOKEN' in key or 'PASSWORD' in key:
            logger.info(f"🔐 {key}: [СКРЫТО]")
        else:
            logger.info(f"⚙️ {key}: {value}")
    
    os.makedirs(CONFIG["MATRIX_STORE_PATH"], exist_ok=True)
    os.makedirs(os.path.dirname(CONFIG["DB_PATH"]), exist_ok=True)
    
    start_matrix_background()
    
    import time
    time.sleep(3)
    
    logger.info("📱 Telegram bot polling started...")
    print("🤖 Бот запущен!")
    tg_bot.infinity_polling()