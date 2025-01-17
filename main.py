
import os
import psycopg2
import yt_dlp
import telegram
import asyncio
import ssl
import vk_api
import urllib.parse
import logging
from datetime import datetime
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
)
import aiohttp
import requests

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# Главные администраторы (их ID прописаны в коде)
ADMIN_CHAT_IDS = [1276928573, 332786197, 1786980999, 228845914]  # Замените на реальные ID главных админов

# VK App ID (замените на свой)
VK_CLIENT_ID = "52875762"  # Укажите здесь ваш ID приложения VK

# Состояния для ConversationHandler
AUTHORIZATION = 0
WAITING_GROUP_TOKEN = 1
WAITING_GROUP_ID = 2
WAITING_ADMIN_ID = 3
WAITING_REMOVE_ADMIN_ID = 4
WAITING_GROUP_REMOVE_ID = 5
SELECTING_TARIFF = 6
TARIFF_DETAILS = 7

# Переменные окружения для ограничений скачиваний
REGULAR_DAILY_LIMIT = int(os.getenv("REGULAR_DAILY_LIMIT"))
ADMIN_DAILY_LIMIT = int(os.getenv("ADMIN_DAILY_LIMIT"))

# Строка подключения к PostgreSQL
DATABASE_URL = os.getenv("DATABASE_URL")

# Токен бота
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Функция для получения подключения к базе данных
def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

# Создание базы данных
def setup_database():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS admins
        (
            chat_id BIGINT PRIMARY KEY,
            user_token TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS groups
        (
            group_id TEXT PRIMARY KEY,
            token TEXT,
            name TEXT,
            admin_chat_id BIGINT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS video_downloads
        (
            user_id BIGINT,
            date DATE,
            count INTEGER,
            PRIMARY KEY (user_id, date)
        )
        """
    )
    conn.commit()
    conn.close()

# Функции работы с администраторами
def get_admin_token(chat_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_token FROM admins WHERE chat_id=%s", (chat_id,))
    result = c.fetchone()
    conn.close()
    if result:
        return result[0]
    else:
        return None

def update_admin_token(chat_id, user_token):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO admins (chat_id, user_token)
        VALUES (%s, %s)
        ON CONFLICT (chat_id) DO UPDATE SET user_token = EXCLUDED.user_token
        """,
        (chat_id, user_token),
    )
    conn.commit()
    conn.close()

def is_admin(chat_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM admins WHERE chat_id=%s", (chat_id,))
    result = c.fetchone()
    conn.close()
    return result is not None

def add_admin_to_db(chat_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO admins (chat_id) VALUES (%s) ON CONFLICT DO NOTHING",
        (chat_id,),
    )
    conn.commit()
    conn.close()

def remove_admin_from_db(chat_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM admins WHERE chat_id=%s", (chat_id,))
    conn.commit()
    conn.close()

def get_admins():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT chat_id FROM admins")
    admins = c.fetchall()
    conn.close()
    return [admin[0] for admin in admins]

# Функции работы с группами
def add_group_to_db(group_id, token, name, admin_chat_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO groups (group_id, token, name, admin_chat_id)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (group_id) DO UPDATE SET token = EXCLUDED.token, name = EXCLUDED.name, admin_chat_id = EXCLUDED.admin_chat_id
        """,
        (group_id, token, name, admin_chat_id),
    )
    conn.commit()
    conn.close()

def get_groups(admin_chat_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM groups WHERE admin_chat_id=%s", (admin_chat_id,)
    )
    groups = c.fetchall()
    conn.close()
    return groups

def remove_group_from_db(group_id, admin_chat_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "DELETE FROM groups WHERE group_id=%s AND admin_chat_id=%s",
        (group_id, admin_chat_id),
    )
    conn.commit()
    conn.close()

# Функции для отслеживания скачиваний видео
def get_daily_download_count(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    today = datetime.utcnow().date()
    c.execute(
        "SELECT count FROM video_downloads WHERE user_id=%s AND date=%s",
        (user_id, today),
    )
    result = c.fetchone()
    conn.close()
    if result:
        return result[0]
    else:
        return 0

def increment_daily_download_count(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    today = datetime.utcnow().date()
    current_count = get_daily_download_count(user_id)
    if current_count == 0:
        c.execute(
            """
            INSERT INTO video_downloads (user_id, date, count)
            VALUES (%s, %s, %s)
            """,
            (user_id, today, 1),
        )
    else:
        c.execute(
            """
            UPDATE video_downloads
            SET count = count + 1
            WHERE user_id=%s AND date=%s
            """,
            (user_id, today),
        )
    conn.commit()
    conn.close()

def get_download_limit(chat_id):
    if chat_id in ADMIN_CHAT_IDS or is_admin(chat_id):
        return ADMIN_DAILY_LIMIT
    else:
        return REGULAR_DAILY_LIMIT

# Функция для повторных попыток отправки сообщения
async def send_message_with_retry(
    update, text, reply_markup=None, max_retries=3
):
    for attempt in range(max_retries):
        try:
            if reply_markup:
                return await update.message.reply_text(
                    text, reply_markup=reply_markup
                )
            else:
                return await update.message.reply_text(text)
        except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
            if attempt == max_retries - 1:
                raise e
            await asyncio.sleep(1)

# Генерация ссылки для авторизации VK
def get_vk_auth_url():
    SCOPE = "video,wall,groups,offline"
    return f"https://oauth.vk.com/authorize?client_id={VK_CLIENT_ID}&display=page&redirect_uri=https://oauth.vk.com/blank.html&scope={SCOPE}&response_type=token&v=5.199"

# Извлечение access_token из ссылки
def extract_access_token_from_url(url):
    parsed = urllib.parse.urlparse(url)
    fragment_params = urllib.parse.parse_qs(parsed.fragment)
    return fragment_params.get("access_token", [None])[0]

# Список тарифов
tariffs = [
    {"name": "Новичок", "cost": "900руб", "videos": 500},
    {"name": "Любитель", "cost": "4000руб", "videos": 1000},
    {"name": "Профи", "cost": "7500руб", "videos": 2000},
    {"name": "Бизнес", "cost": "14500руб", "videos": 3000},
]

# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # Проверяем, является ли пользователь администратором
    if chat_id in ADMIN_CHAT_IDS or is_admin(chat_id):
        # Проверяем, есть ли у администратора user_token
        user_token = get_admin_token(chat_id)
        if not user_token:
            # Отправляем ссылку для авторизации
            auth_url = get_vk_auth_url()
            await send_message_with_retry(
                update,
                f"Вы не авторизованы.\nПожалуйста, перейдите по ссылке для авторизации:\n{auth_url}\n"
                "После авторизации скопируйте всю ссылку из адресной строки и отправьте мне.",
            )
            return AUTHORIZATION
        else:
            # Отправляем главное меню
            keyboard = [
                [KeyboardButton("Добавить группу"), KeyboardButton("Мои группы")],
                [KeyboardButton("Удалить группу")],
            ]
            if chat_id in ADMIN_CHAT_IDS:
                keyboard.append(
                    [KeyboardButton("Добавить администратора"), KeyboardButton("Удалить администратора")]
                )
                keyboard.append([KeyboardButton("Администраторы")])
            reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            await send_message_with_retry(
                update,
                "Привет! Я помогу скачивать видео из TikTok, YouTube, VK клипы и публиковать видео в группы ВКонтакте.",
                reply_markup=reply_markup,
            )
            return ConversationHandler.END
    else:
        # Отправляем сообщение о необходимости покупки тарифа
        keyboard = [
            [InlineKeyboardButton(tariff["name"], callback_data=f"tariff_{i}")]
            for i, tariff in enumerate(tariffs)
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await send_message_with_retry(
            update,
            "Для доступа к боту необходимо купить тариф.",
            reply_markup=reply_markup,
        )
        return SELECTING_TARIFF

# Обработка ссылки авторизации
async def handle_authorization(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    url = update.message.text.strip()
    # Извлекаем access_token из ссылки
    access_token = extract_access_token_from_url(url)
    if access_token:
        # Добавляем администратора в базу данных, если его нет
        if not is_admin(chat_id):
            add_admin_to_db(chat_id)
        # Сохраняем access_token в базе данных
        update_admin_token(chat_id, access_token)
        await send_message_with_retry(update, "✅ Вы успешно авторизовались!")
        # Отправляем главное меню
        await start(update, context)
        return ConversationHandler.END
    else:
        await send_message_with_retry(
            update,
            "Не удалось извлечь access_token. Убедитесь, что вы отправили полную ссылку из адресной строки после авторизации.",
        )
        return AUTHORIZATION

# Команда 'Добавить группу'
async def add_group_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in ADMIN_CHAT_IDS or is_admin(chat_id):
        reply_markup = ReplyKeyboardMarkup(
            [[KeyboardButton("Отмена")]], resize_keyboard=True, one_time_keyboard=True
        )
        await send_message_with_retry(
            update,
            "Пожалуйста, отправьте токен группы ВКонтакте:",
            reply_markup=reply_markup,
        )
        return WAITING_GROUP_TOKEN
    else:
        await send_message_with_retry(
            update, "Этот бот доступен только для администраторов."
        )
        return ConversationHandler.END

async def group_token_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["group_token"] = text
    reply_markup = ReplyKeyboardMarkup(
        [[KeyboardButton("Отмена")]], resize_keyboard=True, one_time_keyboard=True
    )
    await send_message_with_retry(
        update, "Теперь отправьте ID группы (без минуса):", reply_markup=reply_markup
    )
    return WAITING_GROUP_ID

async def group_id_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    group_id = f"-{text}"
    group_token = context.user_data["group_token"]

    try:
        vk_session = vk_api.VkApi(token=group_token)
        vk = vk_session.get_api()
        group_info = vk.groups.getById(group_id=group_id[1:])[0]

        add_group_to_db(group_id, group_token, group_info["name"], chat_id)

        await send_message_with_retry(
            update, f'✅ Группа "{group_info["name"]}" успешно добавлена!'
        )
    except Exception as e:
        await send_message_with_retry(
            update, f"❌ Ошибка при добавлении группы: {str(e)}"
        )

    await start(update, context)
    return ConversationHandler.END

# Команда 'Мои группы'
async def show_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in ADMIN_CHAT_IDS or is_admin(chat_id):
        groups = get_groups(chat_id)
        if not groups:
            await send_message_with_retry(update, "Нет добавленных групп.")
            return

        message = "Добавленные группы:\n\n"
        for i, group in enumerate(groups, 1):
            message += f"{i}. {group[2]} (ID: {group[0]})\n"

        await send_message_with_retry(update, message)
    else:
        await send_message_with_retry(
            update, "Этот бот доступен только для администраторов."
        )

# Команда 'Добавить администратора'
async def add_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in ADMIN_CHAT_IDS:
        reply_markup = ReplyKeyboardMarkup(
            [[KeyboardButton("Отмена")]], resize_keyboard=True, one_time_keyboard=True
        )
        await send_message_with_retry(
            update,
            "Пожалуйста, отправьте ID пользователя, которого вы хотите добавить в качестве администратора:",
            reply_markup=reply_markup,
        )
        return WAITING_ADMIN_ID
    else:
        await send_message_with_retry(
            update, "Команда доступна только для главных администраторов."
        )
        return ConversationHandler.END

async def add_admin_id_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        new_admin_chat_id = int(text)
        if new_admin_chat_id not in ADMIN_CHAT_IDS and not is_admin(new_admin_chat_id):
            add_admin_to_db(new_admin_chat_id)
            await send_message_with_retry(
                update,
                f"✅ Пользователь с ID {new_admin_chat_id} добавлен в качестве администратора!",
            )
        else:
            await send_message_with_retry(
                update,
                f"❌ Пользователь с ID {new_admin_chat_id} уже является администратором.",
            )
    except ValueError:
        await send_message_with_retry(
            update, "Неверный формат ID пользователя. Введите число."
        )
        return WAITING_ADMIN_ID
    await start(update, context)
    return ConversationHandler.END

# Команда 'Удалить администратора'
async def remove_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in ADMIN_CHAT_IDS:
        admins = get_admins()
        if not admins:
            await send_message_with_retry(update, "Нет добавленных администраторов.")
            return ConversationHandler.END

        message = "Добавленные администраторы:\n\n"
        for i, admin in enumerate(admins, 1):
            message += f"{i}. ID: {admin}\n"

        reply_markup = ReplyKeyboardMarkup(
            [[KeyboardButton("Отмена")]], resize_keyboard=True, one_time_keyboard=True
        )

        await send_message_with_retry(
            update,
            f"{message}\nПожалуйста, отправьте ID администратора, которого вы хотите удалить:",
            reply_markup=reply_markup,
        )
        return WAITING_REMOVE_ADMIN_ID
    else:
        await send_message_with_retry(
            update, "Команда доступна только для главных администраторов."
        )
        return ConversationHandler.END

async def remove_admin_id_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        admin_chat_id = int(text)
        if is_admin(admin_chat_id):
            remove_admin_from_db(admin_chat_id)
            await send_message_with_retry(
                update,
                f"✅ Пользователь с ID {admin_chat_id} удален из списка администраторов!",
            )
        else:
            await send_message_with_retry(
                update,
                f"❌ Пользователь с ID {admin_chat_id} не является администратором.",
            )
    except ValueError:
        await send_message_with_retry(
            update, "Неверный формат ID пользователя. Введите число."
        )
        return WAITING_REMOVE_ADMIN_ID
    await start(update, context)
    return ConversationHandler.END

# Команда 'Удалить группу'
async def remove_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in ADMIN_CHAT_IDS or is_admin(chat_id):
        groups = get_groups(chat_id)
        if not groups:
            await send_message_with_retry(update, "Нет добавленных групп.")
            return ConversationHandler.END

        message = "Добавленные группы:\n\n"
        for i, group in enumerate(groups, 1):
            message += f"{i}. {group[2]} (ID: {group[0]})\n"

        reply_markup = ReplyKeyboardMarkup(
            [
                [KeyboardButton(str(i)) for i in range(1, len(groups) + 1)],
                [KeyboardButton("Отмена")],
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        )

        await send_message_with_retry(
            update,
            f"{message}\nПожалуйста, выберите номер группы для удаления или нажмите 'Отмена':",
            reply_markup=reply_markup,
        )
        context.user_data["groups"] = groups
        return WAITING_GROUP_REMOVE_ID
    else:
        await send_message_with_retry(
            update, "Этот бот доступен только для администраторов."
        )
        return ConversationHandler.END

async def group_remove_id_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        group_index = int(text) - 1
        groups = context.user_data.get("groups", [])
        if 0 <= group_index < len(groups):
            group_id = groups[group_index][0]
            remove_group_from_db(group_id, update.effective_chat.id)
            await send_message_with_retry(
                update,
                f"✅ Группа {groups[group_index][2]} (ID: {group_id}) удалена!",
            )
        else:
            await send_message_with_retry(update, "Неверный номер группы.")
            return WAITING_GROUP_REMOVE_ID
    except ValueError:
        await send_message_with_retry(
            update, "Неверный формат номера группы. Введите число."
        )
        return WAITING_GROUP_REMOVE_ID
    await start(update, context)
    return ConversationHandler.END

# Команда 'Администраторы'
async def show_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in ADMIN_CHAT_IDS:
        admins = get_admins()
        if not admins:
            await send_message_with_retry(update, "Нет добавленных администраторов.")
            return

        message = "Добавленные администраторы:\n\n"
        for i, admin in enumerate(admins, 1):
            message += f"{i}. ID: {admin}\n"

        await send_message_with_retry(update, message)
    else:
        await send_message_with_retry(
            update, "Команда доступна только для главных администраторов."
        )

# Обработка сообщений с ссылками на видео
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if chat_id in ADMIN_CHAT_IDS or is_admin(chat_id):
        user_token = get_admin_token(chat_id)
        if not user_token:
            await send_message_with_retry(
                update,
                "Вы не авторизованы. Используйте команду /start, чтобы авторизоваться.",
            )
            return

        groups = get_groups(chat_id)
        if not groups:
            await send_message_with_retry(
                update,
                'Нет добавленных групп. Используйте кнопку "Добавить группу", чтобы добавить группу.',
            )
            return

        # Проверяем, содержит ли сообщение ссылку на видео
        url = update.message.text.strip()
        if any(
            domain in url
            for domain in ["tiktok.com", "youtube.com", "youtu.be", "vk.com", "instagram.com"]
        ):
            # Проверяем лимит скачиваний
            download_limit = get_download_limit(chat_id)
            current_count = get_daily_download_count(user_id)
            if current_count >= download_limit:
                await send_message_with_retry(
                    update,
                    f"❌ Вы достигли ежедневного лимита скачиваний ({download_limit} видео). Попробуйте завтра.",
                )
                return

            # Отправляем сообщение "Идет загрузка..."
            loading_message = await send_message_with_retry(update, "Идет загрузка...")
            try:
                # Создаем директорию для администратора
                admin_video_dir = f"videos/{chat_id}"
                if not os.path.exists(admin_video_dir):
                    os.makedirs(admin_video_dir)

                # Загрузка видео
                ydl_options = {
                    "format": "best",
                    "outtmpl": f"{admin_video_dir}/downloaded_video.%(ext)s",
                    "quiet": True,
                    "socket_timeout": 600,
                    "geo_bypass": True,
                    "geo_bypass_country": "DE",
                }
                with yt_dlp.YoutubeDL(ydl_options) as ydl:
                    result = ydl.extract_info(url, download=True)
                    video_file = ydl.prepare_filename(result)

                await asyncio.sleep(1)
                await loading_message.delete()

                # Публикация видео в группы
                keyboard = []
                row = []
                for i, group in enumerate(groups, 1):
                    row.append(
                        InlineKeyboardButton(group[2], callback_data=f"post_{i-1}")
                    )
                    if len(row) == 2:
                        keyboard.append(row)
                        row = []
                if row:
                    keyboard.append(row)

                reply_markup = InlineKeyboardMarkup(keyboard)

                # Отправляем сообщение с клавиатурой
                await send_message_with_retry(
                    update,
                    "Выберите группу для публикации:",
                    reply_markup=reply_markup,
                )

                # Сохраняем видео и информацию о группах для обработки в button_callback
                context.user_data["video_path"] = video_file
                context.user_data["groups"] = groups
                context.user_data["user_token"] = user_token

                # Устанавливаем таймер для удаления видео
                DELETE_TIMEOUT = 90  # Время в секундах (например, 600 секунд = 10 минут)

                async def delete_video_after_timeout(chat_id, video_path, timeout):
                    try:
                        await asyncio.sleep(timeout)
                        if os.path.exists(video_path):
                            os.remove(video_path)
                            print(
                                f"Видео файл для chat_id {chat_id} удален после тайм-аута."
                            )
                            # Удаляем папку администратора, если она пуста
                            admin_video_dir = os.path.dirname(video_path)
                            try:
                                os.rmdir(admin_video_dir)
                            except OSError:
                                pass  # Папка не пуста
                    except asyncio.CancelledError:
                        # Задача была отменена, ничего не делаем
                        pass

                delete_task = asyncio.create_task(
                    delete_video_after_timeout(chat_id, video_file, DELETE_TIMEOUT)
                )
                context.user_data["delete_task"] = delete_task

                # Увеличиваем счетчик скачиваний
                increment_daily_download_count(user_id)

            except Exception as e:
                await send_message_with_retry(
                    update, f"Ошибка при скачивании видео: {str(e)}"
                )
        else:
            await send_message_with_retry(
                update,
                "Пожалуйста, отправьте ссылку на видео из TikTok, YouTube, VK или Instagram.",
            )
    else:
        keyboard = [
            [InlineKeyboardButton(tariff["name"], callback_data=f"tariff_{i}")]
            for i, tariff in enumerate(tariffs)
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await send_message_with_retry(
            update,
            "Для доступа к боту необходимо купить тариф.",
            reply_markup=reply_markup,
        )

# Функции для работы с VK API
async def get_upload_url(user_token, group_id):

    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(ssl=ssl_context)
    ) as session:
        async with session.post(
            f"https://api.vk.com/method/video.save?access_token={user_token}&group_id={group_id}&&v=5.199"
        ) as resp:
            data = await resp.json()
            if "response" in data:
                if "upload_url" in data["response"]:
                    return data["response"]
                else:
                    return {"error": {"error_msg": "Upload URL not found in response"}}
            elif "error" in data:
                return {"error": data["error"]}
            else:
                return {"error": {"error_msg": "Unknown error occurred"}}

async def post_video(user_token, group_id, video_id, owner_id):
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    try:
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=ssl_context)
        ) as session:
            async with session.post(
                f"https://api.vk.com/method/wall.post?access_token={user_token}&owner_id={-group_id}&from_group=1&attachments=video{owner_id}_{video_id}&v=5.199"
            ) as resp:
                data = await resp.json()
                if "response" in data:
                    return data["response"]
                elif "error" in data:
                    raise Exception(f"VK API error: {data['error']}")
                else:
                    raise Exception("Unknown error occurred during VK post")
    except Exception as e:
        print(f"Ошибка при публикации видео в VK: {e}")
        return {"error": str(e)}

# Обработка нажатий кнопок
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()
        data = query.data
        chat_id = query.message.chat_id

        if data.startswith("post_"):
            # Обработка публикации видео
            user_token = get_admin_token(chat_id)
            if not user_token:
                await query.message.edit_text(
                    "Вы не авторизованы. Используйте команду /start, чтобы авторизоваться."
                )
                return

            group_index = int(data.split("_")[1])
            groups = context.user_data.get("groups", [])
            if not groups:
                await query.message.edit_text("Нет добавленных групп.")
                return
            group = groups[group_index]

            video_path = context.user_data["video_path"]
            group_token = group[1]  # Токен группы
            group_id = int(group[0][1:])  # ID группы без минуса

            try:
                # Получаем URL для загрузки видео
                upload_info = await get_upload_url(user_token, group_id)

                if "error" in upload_info:
                    await query.message.edit_text(
                        f'❌ Ошибка при получении URL для загрузки видео: {upload_info["error"]["error_msg"]}'
                    )
                    return

                # Загружаем видео на сервер ВКонтакте
                with open(video_path, "rb") as video_file:
                    upload_result = requests.post(
                        upload_info["upload_url"], files={"video_file": video_file}
                    )

                if upload_result.status_code == 200:
                    upload_data = upload_result.json()

                    # Публикуем видео в группе
                    post_result = await post_video(
                        user_token,
                        group_id,
                        upload_data["video_id"],
                        upload_data["owner_id"],
                    )
                    if "error" in post_result:
                        await query.message.edit_text(
                            f'✅ Видео успешно опубликовано в группе "{group[2]}"!'
                        )
                    else:
                        await query.message.edit_text(
                            f'✅ Видео успешно опубликовано в группе "{group[2]}"!'
                        )

                    # Удаляем видео из локальной папки
                    if os.path.exists(video_path):
                        os.remove(video_path)
                        # Удаляем папку администратора, если она пуста
                        admin_video_dir = os.path.dirname(video_path)
                        try:
                            os.rmdir(admin_video_dir)
                        except OSError:
                            pass  # Папка не пуста

                    # Отменяем задачу удаления видео по таймеру
                    if "delete_task" in context.user_data:
                        delete_task = context.user_data["delete_task"]
                        delete_task.cancel()

                else:
                    await query.message.edit_text(
                        f"❌ Ошибка при загрузке видео: HTTP {upload_result.status_code} - {upload_result.text}"
                    )

            except Exception as e:
                await query.message.edit_text(f"❌ Ошибка при публикации: {str(e)}")

        elif data.startswith("tariff_"):
            # Обработка выбора тарифа
            tariff_index = int(data[len("tariff_"):])
            tariff = tariffs[tariff_index]
            # Показываем детали тарифа и кнопки 'Оплатить' и 'Назад'
            message = (
                f"Тариф: {tariff['name']}\nСтоимость: {tariff['cost']}\nКоличество видео: {tariff['videos']}"
            )
            keyboard = [
                [
                    InlineKeyboardButton("Оплатить", callback_data="pay"),
                    InlineKeyboardButton("Назад", callback_data="back_to_tariffs"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.message.edit_text(message, reply_markup=reply_markup)

        elif data == "back_to_tariffs":
            # Возвращаемся к списку тарифов
            keyboard = [
                [InlineKeyboardButton(tariff["name"], callback_data=f"tariff_{i}")]
                for i, tariff in enumerate(tariffs)
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.message.edit_text(
                "Для доступа к боту необходимо купить тариф.",
                reply_markup=reply_markup,
            )

        elif data == "pay":
            # Ничего не делаем при нажатии 'Оплатить'
            await query.answer("Оплата пока не реализована.")

        else:
            # Другие данные обратного вызова
            pass

    except telegram.error.BadRequest as e:
        print(f"Error in button_callback: {e}")
    except KeyError as e:
        print(f"Error in button_callback: '{e}'")
    except Exception as e:
        print(f"Error in button_callback: {e}")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)  # Вызов функции start
    return ConversationHandler.END  # Завершение текущего разговора

# Обработка текстовых сообщений (кнопок)
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    if text == "Добавить группу":
        return await add_group_start(update, context)
    elif text == "Мои группы":
        await show_groups(update, context)
    elif text == "Удалить группу":
        return await remove_group(update, context)
    elif text == "Добавить администратора":
        return await add_admin_start(update, context)
    elif text == "Удалить администратора":
        return await remove_admin_start(update, context)
    elif text == "Администраторы":
        await show_admins(update, context)
    elif text == "Отмена":
        await cancel(update, context)
    else:
        await handle_message(update, context)  # Обрабатываем как возможную ссылку на видео

def main():
    # Создаем базу данных при запуске
    setup_database()

    # Настройка бота
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    # Добавление обработчиков
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex("^(Добавить группу)$"), add_group_start),
            MessageHandler(filters.Regex("^(Добавить администратора)$"), add_admin_start),
            MessageHandler(filters.Regex("^(Удалить администратора)$"), remove_admin_start),
            MessageHandler(filters.Regex("^(Удалить группу)$"), remove_group),
        ],
        states={
            AUTHORIZATION: [
                MessageHandler(filters.Regex("^Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_authorization),
            ],
            WAITING_GROUP_TOKEN: [
                MessageHandler(filters.Regex("^Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, group_token_received),
            ],
            WAITING_GROUP_ID: [
                MessageHandler(filters.Regex("^Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, group_id_received),
            ],
            WAITING_ADMIN_ID: [
                MessageHandler(filters.Regex("^Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_admin_id_received),
            ],
            WAITING_REMOVE_ADMIN_ID: [
                MessageHandler(filters.Regex("^Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, remove_admin_id_received),
            ],
            WAITING_GROUP_REMOVE_ID: [
                MessageHandler(filters.Regex("^Отмена$"), cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, group_remove_id_received),
            ],
            SELECTING_TARIFF: [
                CallbackQueryHandler(button_callback)
            ],
            TARIFF_DETAILS: [
                CallbackQueryHandler(button_callback)
            ],
        },
        fallbacks=[
            CommandHandler("start", cancel),
            MessageHandler(filters.Regex("^Отмена$"), cancel),
        ],
    )

    application.add_handler(conv_handler)
    application.add_handler(MessageHandler(filters.Regex("^(Мои группы)$"), show_groups))
    application.add_handler(MessageHandler(filters.Regex("^(Администраторы)$"), show_admins))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message)
    )

    # Запуск бота
    application.run_polling()

if __name__ == "__main__":
    main()
