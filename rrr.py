import os
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import sqlite3
from sqlite3 import Connection
from contextlib import contextmanager

import json
import math
import random
import asyncio
import uuid

from telegram import (
    Update, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
    ReplyKeyboardMarkup
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters
)
from telegram.constants import ParseMode

BOT_TOKEN = ""

ADMIN_IDS = []
DB_PATH = "nearby_bot.db"

# Настройки
MAX_PHOTOS = 3
MAX_BIO_LENGTH = 500
DEFAULT_SEARCH_RADIUS_KM = 10
CHAT_DURATION_HOURS = 24
LIKES_PER_DAY_FREE = 1000000

# Теги (интересы)
TAGS = [
    "Кофе", "Игры", "Походы", "IT", "Искусство", 
    "Спорт", "Кино", "Музыка",
    "Еда", "Фотография", "Авто"
]

# Состояния FSM
class States:
    REG_PHOTO = 1
    REG_NAME_AGE = 2
    REG_GENDER = 3
    REG_CITY = 4
    REG_BIO = 5
    REG_INTERESTS = 6
    REG_GOAL = 7
    REG_SEARCH_SETTINGS = 8
    EDIT_PROFILE = 9

# ==================== БАЗА ДАННЫХ ====================
class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.init_db()
    
    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
    
    def init_db(self):
        with self.get_connection() as conn:
            # Пользователи
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    username TEXT,
                    full_name TEXT,
                    age INTEGER,
                    city TEXT,
                    latitude REAL,
                    longitude REAL,
                    bio TEXT,
                    interests TEXT,  -- JSON список
                    goal TEXT,
                    gender TEXT,
                    search_gender TEXT DEFAULT 'any',
                    search_age_min INTEGER DEFAULT 18,
                    search_age_max INTEGER DEFAULT 45,
                    search_radius INTEGER DEFAULT 50,
                    photos TEXT,  -- JSON список file_id
                    likes_today INTEGER DEFAULT 0,
                    likes_reset_date TEXT,
                    is_active BOOLEAN DEFAULT 1,
                    is_premium BOOLEAN DEFAULT 0,
                    is_banned BOOLEAN DEFAULT 0,
                    last_seen TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Лайки
            conn.execute("""
                CREATE TABLE IF NOT EXISTS likes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_user_id INTEGER NOT NULL,
                    to_user_id INTEGER NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (from_user_id) REFERENCES users(id),
                    FOREIGN KEY (to_user_id) REFERENCES users(id),
                    UNIQUE(from_user_id, to_user_id)
                )
            """)
            
            # Мэтчи (чаты)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS matches (
                    id TEXT PRIMARY KEY,
                    user1_id INTEGER NOT NULL,
                    user2_id INTEGER NOT NULL,
                    chat_expires_at TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT 1,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user1_id) REFERENCES users(id),
                    FOREIGN KEY (user2_id) REFERENCES users(id)
                )
            """)
            
            # Сообщения
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id TEXT NOT NULL,
                    sender_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (match_id) REFERENCES matches(id),
                    FOREIGN KEY (sender_id) REFERENCES users(id)
                )
            """)
            
            # Жалобы
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reporter_id INTEGER NOT NULL,
                    reported_user_id INTEGER NOT NULL,
                    reason TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (reporter_id) REFERENCES users(id),
                    FOREIGN KEY (reported_user_id) REFERENCES users(id)
                )
            """)
            
            # Индексы
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_telegram ON users(telegram_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active)")
    
    def get_user_by_telegram_id(self, telegram_id: int) -> Optional[Dict]:
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            row = cursor.fetchone()
            if row:
                user = dict(row)
                # Парсим JSON поля
                for field in ['interests', 'photos']:
                    if user[field]:
                        try:
                            user[field] = json.loads(user[field])
                        except:
                            user[field] = []
                    else:
                        user[field] = []
                return user
            return None
    
    def create_user(self, user_data: Dict) -> Optional[Dict]:
        with self.get_connection() as conn:
            # Преобразуем списки в JSON
            data_to_insert = user_data.copy()
            for field in ['interests', 'photos']:
                if field in data_to_insert and isinstance(data_to_insert[field], list):
                    data_to_insert[field] = json.dumps(data_to_insert[field], ensure_ascii=False)
            
            fields = list(data_to_insert.keys())
            placeholders = ['?' for _ in fields]
            
            sql = f"""
                INSERT INTO users ({', '.join(fields)})
                VALUES ({', '.join(placeholders)})
            """
            
            try:
                cursor = conn.execute(sql, list(data_to_insert.values()))
                user_id = cursor.lastrowid
                
                # Получаем созданного пользователя
                cursor = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
                row = cursor.fetchone()
                if row:
                    user = dict(row)
                    # Парсим JSON поля
                    for field in ['interests', 'photos']:
                        if user[field]:
                            try:
                                user[field] = json.loads(user[field])
                            except:
                                user[field] = []
                        else:
                            user[field] = []
                    return user
            except Exception as e:
                logging.error(f"Error creating user: {e}")
            return None
    
    def update_user(self, telegram_id: int, updates: Dict) -> bool:
        with self.get_connection() as conn:
            # Преобразуем списки в JSON
            data_to_update = updates.copy()
            for field in ['interests', 'photos']:
                if field in data_to_update and isinstance(data_to_update[field], list):
                    data_to_update[field] = json.dumps(data_to_update[field], ensure_ascii=False)
            
            set_clause = ', '.join([f"{key} = ?" for key in data_to_update.keys()])
            values = list(data_to_update.values()) + [telegram_id]
            
            sql = f"UPDATE users SET {set_clause} WHERE telegram_id = ?"
            cursor = conn.execute(sql, values)
            return cursor.rowcount > 0
    
    def reset_daily_likes_if_needed(self, user_id: int):
        with self.get_connection() as conn:
            user = self.get_user_by_telegram_id(user_id)
            if not user:
                return
            
            today = datetime.now().strftime("%Y-%m-%d")
            if user.get('likes_reset_date') != today:
                conn.execute(
                    "UPDATE users SET likes_today = 0, likes_reset_date = ? WHERE telegram_id = ?",
                    (today, user_id)
                )
    
    def get_next_profile(self, current_user_id: int) -> Optional[Dict]:
        """Получить следующую анкету для показа"""
        with self.get_connection() as conn:
            user = self.get_user_by_telegram_id(current_user_id)
            if not user:
                return None
            
            user_id_db = user['id']
            
            # Базовый запрос
            query = """
                SELECT u.* FROM users u
                WHERE u.telegram_id != ?
                AND u.is_active = 1
                AND u.is_banned = 0
                AND u.age BETWEEN ? AND ?
                AND NOT EXISTS (
                    SELECT 1 FROM likes l 
                    WHERE l.from_user_id = ?
                    AND l.to_user_id = u.id
                )
                AND NOT EXISTS (
                    SELECT 1 FROM matches m 
                    WHERE (m.user1_id = ? AND m.user2_id = u.id)
                    OR (m.user2_id = ? AND m.user1_id = u.id)
                    AND m.is_active = 1
                )
                AND (
                    ? = 'any' OR u.gender = ?
                )
                ORDER BY u.last_seen DESC 
                LIMIT 1
            """
            
            search_gender = user.get('search_gender', 'any')
            search_age_min = user.get('search_age_min', 18)
            search_age_max = user.get('search_age_max', 45)
            
            params = [
                current_user_id,  # telegram_id != ?
                search_age_min, search_age_max,  # age BETWEEN
                user_id_db,  # для первого подзапроса
                user_id_db, user_id_db,  # для второго подзапроса
                search_gender, search_gender  # фильтр по полу
            ]
            
            cursor = conn.execute(query, params)
            row = cursor.fetchone()
            
            if row:
                profile = dict(row)
                # Парсим JSON поля
                for field in ['interests', 'photos']:
                    if profile[field]:
                        try:
                            profile[field] = json.loads(profile[field])
                        except:
                            profile[field] = []
                    else:
                        profile[field] = []
                return profile
            
            return None
    
    def create_like(self, from_user_id: int, to_user_id: int) -> bool:
        """Создать лайк и проверить на взаимность"""
        with self.get_connection() as conn:
            # Получаем ID пользователей
            from_user = self.get_user_by_telegram_id(from_user_id)
            to_user = self.get_user_by_telegram_id(to_user_id)
            
            if not from_user or not to_user:
                return False
            
            # Проверяем лимит лайков
            today = datetime.now().strftime("%Y-%m-%d")
            if from_user.get('likes_reset_date') != today:
                conn.execute(
                    "UPDATE users SET likes_today = 0, likes_reset_date = ? WHERE telegram_id = ?",
                    (today, from_user_id)
                )
                from_user['likes_today'] = 0
            
            likes_limit = LIKES_PER_DAY_FREE if not from_user['is_premium'] else 9999
            if from_user['likes_today'] >= likes_limit:
                return False  # Лимит исчерпан
            
            # Создаем лайк
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO likes (from_user_id, to_user_id) VALUES (?, ?)",
                    (from_user['id'], to_user['id'])
                )
                
                # Увеличиваем счетчик лайков за сегодня
                conn.execute(
                    "UPDATE users SET likes_today = likes_today + 1 WHERE telegram_id = ?",
                    (from_user_id,)
                )
                
                # Проверяем на взаимность
                cursor = conn.execute("""
                    SELECT 1 FROM likes 
                    WHERE from_user_id = ? AND to_user_id = ?
                """, (to_user['id'], from_user['id']))
                
                mutual = cursor.fetchone() is not None
                
                # Если взаимность - создаем мэтч
                if mutual:
                    match_id = str(uuid.uuid4())
                    expires_at = (datetime.now() + timedelta(hours=CHAT_DURATION_HOURS)).isoformat()
                    
                    conn.execute("""
                        INSERT INTO matches (id, user1_id, user2_id, chat_expires_at)
                        VALUES (?, ?, ?, ?)
                    """, (match_id, from_user['id'], to_user['id'], expires_at))
                    
                    # Удаляем взаимные лайки
                    conn.execute("""
                        DELETE FROM likes 
                        WHERE (from_user_id = ? AND to_user_id = ?)
                        OR (from_user_id = ? AND to_user_id = ?)
                    """, (from_user['id'], to_user['id'], to_user['id'], from_user['id']))
                
                return mutual
                
            except Exception as e:
                logging.error(f"Error creating like: {e}")
                return False
    
    def get_active_matches(self, user_id: int) -> List[Dict]:
        """Получить активные мэтчи пользователя"""
        with self.get_connection() as conn:
            user = self.get_user_by_telegram_id(user_id)
            if not user:
                return []
            
            cursor = conn.execute("""
                SELECT m.*, 
                       CASE 
                           WHEN m.user1_id = ? THEN u2.telegram_id
                           ELSE u1.telegram_id
                       END as partner_telegram_id,
                       CASE 
                           WHEN m.user1_id = ? THEN u2.full_name
                           ELSE u1.full_name
                       END as partner_name
                FROM matches m
                LEFT JOIN users u1 ON m.user1_id = u1.id
                LEFT JOIN users u2 ON m.user2_id = u2.id
                WHERE (m.user1_id = ? OR m.user2_id = ?)
                AND m.is_active = 1
                AND datetime(m.chat_expires_at) > datetime('now')
                ORDER BY m.chat_expires_at DESC
            """, (user['id'], user['id'], user['id'], user['id']))
            
            rows = cursor.fetchall()
            matches = []
            for row in rows:
                match = dict(row)
                matches.append(match)
            return matches

db = Database(DB_PATH)

# ==================== УТИЛИТЫ ====================
def calculate_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Рассчитать расстояние между двумя точками (км)"""
    if not all([lat1, lon1, lat2, lon2]):
        return 0
    
    # Формула Хаверсина
    R = 6371  # Радиус Земли в км
    
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    
    a = math.sin(delta_lat/2)**2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    
    return R * c

# ==================== БЫСТРЫЕ КНОПКИ (QUICK ACTIONS) ====================
def get_quick_actions_keyboard():
    """Быстрые кнопки для главного меню"""
    return ReplyKeyboardMarkup([
        ["👀 Смотреть анкеты", "📊 Мой профиль"],
        ["💬 Мои чаты", "⚙️ Настройки"],
        ["🌟 Премиум", "🆘 Помощь"]
    ], resize_keyboard=True, one_time_keyboard=False)

def get_profile_quick_actions():
    """Быстрые кнопки для профиля"""
    return ReplyKeyboardMarkup([
        ["✏️ Редактировать", "⚙️ Настройки поиска"],
        ["🔙 Назад в меню"]
    ], resize_keyboard=True)

def get_browse_quick_actions():
    """Быстрые кнопки для просмотра анкет"""
    return ReplyKeyboardMarkup([
        ["❤️ Лайк", "➡️ Дальше"],
        ["🚫 Пожаловаться", "🔙 В меню"]
    ], resize_keyboard=True)

def get_chats_quick_actions():
    """Быстрые кнопки для чатов"""
    return ReplyKeyboardMarkup([
        ["📝 Написать сообщение", "🔄 Обновить список"],
        ["🔙 В меню"]
    ], resize_keyboard=True)

# ==================== HANDLERS ====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    db_user = db.get_user_by_telegram_id(user.id)
    
    if db_user:
        # Пользователь уже зарегистрирован
        reply_markup = get_quick_actions_keyboard()
        
        # Дополнительно отправляем inline-кнопки для быстрого доступа
        inline_keyboard = [
            [InlineKeyboardButton("🔥 НАЧАТЬ ПРОСМОТР", callback_data="browse")],
            [InlineKeyboardButton("👤 МОЙ ПРОФИЛЬ", callback_data="profile"),
             InlineKeyboardButton("💬 МОИ ЧАТЫ", callback_data="chats")],
            [InlineKeyboardButton("⚡️ БЫСТРЫЙ ПОИСК", callback_data="quick_search"),
             InlineKeyboardButton("📍 РЯДОМ СЕЙЧАС", callback_data="nearby_now")]
        ]
        
        inline_markup = InlineKeyboardMarkup(inline_keyboard)
        
        # Отправляем основное сообщение с быстрыми кнопками
        await update.message.reply_text(
            f"🔥 *С возвращением, {db_user['full_name'] or user.first_name}!*\n\n"
            "Используй быстрые кнопки ниже или команды:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        
        # Отправляем дополнительное сообщение с inline-кнопками
        await update.message.reply_text(
            "🎯 *Быстрые действия:*",
            reply_markup=inline_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        
        return States.REG_PHOTO
    else:
        # Новая регистрация
        keyboard = [[InlineKeyboardButton("🚀 НАЧАТЬ РЕГИСТРАЦИЮ", callback_data="start_registration")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "🔥 *Добро пожаловать в РЯДОМ!*\n\n"
            "Знакомства рядом с тобой • Быстро • Безопасно • Интересно\n\n"
            "📝 Регистрация займет всего 2 минуты!",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        return States.REG_PHOTO

async def quick_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Быстрый поиск анкет"""
    query = update.callback_query
    await query.answer()
    
    user = db.get_user_by_telegram_id(update.effective_user.id)
    if not user:
        await query.edit_message_text("Пожалуйста, сначала зарегистрируйтесь /start")
        return
    
    # Показываем ближайшие анкеты
    await browse_profiles_callback(update, context)

async def nearby_now_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать кто рядом сейчас"""
    query = update.callback_query
    await query.answer()
    
    user = db.get_user_by_telegram_id(update.effective_user.id)
    if not user:
        await query.edit_message_text("Пожалуйста, сначала зарегистрируйтесь /start")
        return
    
    if not user['is_premium']:
        keyboard = [
            [InlineKeyboardButton("🌟 АКТИВИРОВАТЬ ПРЕМИУМ", callback_data="activate_premium")],
            [InlineKeyboardButton("👀 СМОТРЕТЬ ОБЫЧНЫЕ АНКЕТЫ", callback_data="browse")]
        ]
        
        await query.edit_message_text(
            "📍 *РЯДОМ СЕЙЧАС*\n\n"
            "Эта функция доступна только для премиум-пользователей!\n\n"
            "🌟 *Преимущества премиума:*\n"
            "• Видеть кто онлайн рядом прямо сейчас\n"
            "• Неограниченное количество лайков\n"
            "• Приоритет в показе анкет\n"
            "• Расширенные фильтры поиска\n\n"
            "Активируйте премиум для доступа к этой функции!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        # Здесь будет логика показа пользователей онлайн рядом
        await query.edit_message_text(
            "📍 *Ищу пользователей рядом...*\n\n"
            "Эта функция в разработке. Скоро будет доступна!",
            parse_mode=ParseMode.MARKDOWN
        )

async def start_registration_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало регистрации"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "📸 *ШАГ 1: ФОТО*\n\n"
        "Отправь свое фото (лицо должно быть хорошо видно):\n\n"
        "⚡️ Совет: Используй свежее и качественное фото\n"
        "⚠️ Фото проходит автоматическую модерацию",
        parse_mode=ParseMode.MARKDOWN
    )
    
    return States.REG_PHOTO

async def handle_registration_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка фото при регистрации"""
    if not update.message.photo:
        await update.message.reply_text("📸 Пожалуйста, отправь фото.")
        return States.REG_PHOTO
    
    # Сохраняем file_id фото
    photo_file = await update.message.photo[-1].get_file()
    context.user_data['registration'] = {
        'photos': [photo_file.file_id],
        'step': 1
    }
    
    await update.message.reply_text(
        "✅ *Фото принято!*\n\n"
        "👤 *ШАГ 2: ИМЯ И ВОЗРАСТ*\n\n"
        "Введи свое имя и возраст:\n"
        "*Пример: Иван 25* или *Анна 22*\n\n"
        "⚡️ Пиши как в примере выше",
        parse_mode=ParseMode.MARKDOWN
    )
    
    return States.REG_NAME_AGE

async def handle_registration_name_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка имени и возраста"""
    try:
        text = update.message.text.strip()
        parts = text.split()
        
        if len(parts) < 2:
            raise ValueError
        
        name = ' '.join(parts[:-1])
        age = int(parts[-1])
        
        if not 18 <= age <= 100:
            await update.message.reply_text("❌ Возраст должен быть от 18 до 100 лет.")
            return States.REG_NAME_AGE
        
        if 'registration' not in context.user_data:
            context.user_data['registration'] = {}
        
        context.user_data['registration']['name'] = name
        context.user_data['registration']['age'] = age
        
        # Клавиатура для выбора пола
        keyboard = [
            [
                InlineKeyboardButton("👨 МУЖЧИНА", callback_data="gender_male"),
                InlineKeyboardButton("👩 ЖЕНЩИНА", callback_data="gender_female")
            ],
            [InlineKeyboardButton("👤 ДРУГОЕ", callback_data="gender_other")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "👫 *ШАГ 3: ПОЛ*\n\n"
            "Выбери свой пол:\n\n"
            "⚡️ Это поможет нам лучше подбирать анкеты",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
        
        return States.REG_GENDER
        
    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ *Неверный формат!*\n\n"
            "Пожалуйста, введи в формате: *Имя Возраст*\n"
            "Пример: *Анна 24* или *Иван Петров 30*",
            parse_mode=ParseMode.MARKDOWN
        )
        return States.REG_NAME_AGE

async def handle_registration_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора пола"""
    query = update.callback_query
    await query.answer()
    
    gender_map = {
        'gender_male': 'male',
        'gender_female': 'female',
        'gender_other': 'other'
    }
    
    if 'registration' not in context.user_data:
        context.user_data['registration'] = {}
    
    context.user_data['registration']['gender'] = gender_map[query.data]
    
    # Быстрые кнопки для отправки города
    reply_markup = ReplyKeyboardMarkup([
        ["📍 Отправить геолокацию"],
        ["🏙️ Ввести вручную"]
    ], resize_keyboard=True, one_time_keyboard=True)
    
    await query.edit_message_text(
        "📍 *ШАГ 4: ГОРОД*\n\n"
        "Отправь свой город или геолокацию:\n\n"
        "⚡️ Можно отправить геолокацию кнопкой ниже\n"
        "📍 Или просто напиши название города",
        parse_mode=ParseMode.MARKDOWN
    )
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Выбери способ:",
        reply_markup=reply_markup
    )
    
    return States.REG_CITY

async def handle_registration_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка города"""
    city = None
    
    if update.message.text == "📍 Отправить геолокацию":
        await update.message.reply_text(
            "📍 Нажми на скрепку 📎 и выбери 'Геопозиция'",
            reply_markup=ReplyKeyboardRemove()
        )
        return States.REG_CITY
    elif update.message.text == "🏙️ Ввести вручную":
        await update.message.reply_text(
            "🏙️ Напиши название своего города:",
            reply_markup=ReplyKeyboardRemove()
        )
        return States.REG_CITY
    elif update.message.text:
        city = update.message.text.strip()
    elif update.message.location:
        # Сохраняем координаты
        latitude = update.message.location.latitude
        longitude = update.message.location.longitude
        if 'registration' not in context.user_data:
            context.user_data['registration'] = {}
        context.user_data['registration']['latitude'] = latitude
        context.user_data['registration']['longitude'] = longitude
        city = "Город по геолокации"
    
    if not city:
        await update.message.reply_text("Пожалуйста, отправь название города или геолокацию.")
        return States.REG_CITY
    
    if 'registration' not in context.user_data:
        context.user_data['registration'] = {}
    
    context.user_data['registration']['city'] = city
    
    await update.message.reply_text(
        "📝 *ШАГ 5: О СЕБЕ*\n\n"
        "Расскажи коротко о себе:\n\n"
        "⚡️ *Примеры:*\n"
        "• Люблю путешествия, кино и кофе\n"
        "• IT-специалист, увлекаюсь спортом\n"
        "• Ищу интересного собеседника\n\n"
        "📍 Пиши кратко, но информативно (до 500 символов)",
        parse_mode=ParseMode.MARKDOWN
    )
    
    return States.REG_BIO

async def handle_registration_bio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка информации о себе"""
    bio = update.message.text.strip()
    
    if len(bio) > MAX_BIO_LENGTH:
        await update.message.reply_text(f"❌ Слишком длинно! Максимум {MAX_BIO_LENGTH} символов.")
        return States.REG_BIO
    
    if 'registration' not in context.user_data:
        context.user_data['registration'] = {}
    
    context.user_data['registration']['bio'] = bio
    
    # Создаем клавиатуру с тегами (интересами) - улучшенная версия
    keyboard = []
    row = []
    # Первые 6 самых популярных тегов
    popular_tags = ["Кофе", "Путешествия", "Спорт", "Кино", "Музыка", "Игры"]
    
    for i, tag in enumerate(popular_tags, 1):
        row.append(InlineKeyboardButton(tag, callback_data=f"tag_{tag}"))
        if i % 3 == 0:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("📋 ВСЕ ТЕГИ", callback_data="all_tags")])
    keyboard.append([InlineKeyboardButton("✅ ГОТОВО", callback_data="tags_done")])
    
    await update.message.reply_text(
        "🏷️ *ШАГ 6: ИНТЕРЕСЫ*\n\n"
        "Выбери до 5 тегов, которые тебе интересны:\n\n"
        f"🎯 Выбрано: {len(context.user_data['registration'].get('interests', []))}/5\n\n"
        "⚡️ *Популярные теги:*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    return States.REG_INTERESTS

async def show_all_tags_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать все теги"""
    query = update.callback_query
    await query.answer()
    
    keyboard = []
    row = []
    for i, tag in enumerate(TAGS, 1):
        row.append(InlineKeyboardButton(tag, callback_data=f"tag_{tag}"))
        if i % 3 == 0:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("🔙 НАЗАД", callback_data="back_to_popular")])
    keyboard.append([InlineKeyboardButton("✅ ГОТОВО", callback_data="tags_done")])
    
    interests = context.user_data['registration'].get('interests', [])
    
    await query.edit_message_text(
        "🏷️ *ВСЕ ТЕГИ*\n\n"
        "Выбери до 5 тегов, которые тебе интересны:\n\n"
        f"🎯 Выбрано: {len(interests)}/5\n"
        "📍 Выбранные: " + (', '.join(interests) if interests else "пока нет"),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    return States.REG_INTERESTS

async def back_to_popular_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вернуться к популярным тегам"""
    query = update.callback_query
    await query.answer()
    
    return await handle_registration_bio(update, context)

async def handle_registration_interests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора интересов"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "all_tags":
        return await show_all_tags_callback(update, context)
    
    elif query.data == "back_to_popular":
        return await back_to_popular_callback(update, context)
    
    elif query.data == "tags_done":
        interests = context.user_data['registration'].get('interests', [])
        if not interests:
            await query.answer("❌ Выбери хотя бы один интерес!", show_alert=True)
            return States.REG_INTERESTS
        
        # Клавиатура для выбора цели знакомства
        keyboard = [
            [InlineKeyboardButton("💑 ОТНОШЕНИЯ", callback_data="goal_relationship")],
            [InlineKeyboardButton("👥 ДРУЖБА", callback_data="goal_friendship")],
            [InlineKeyboardButton("💬 ОБЩЕНИЕ", callback_data="goal_chat")],
            [InlineKeyboardButton("🎉 НЕВАЖНО", callback_data="goal_all")]
        ]
        
        await query.edit_message_text(
            "🎯 *ПОСЛЕДНИЙ ШАГ: ЦЕЛЬ*\n\n"
            "Что ты ищешь?\n\n"
            "⚡️ *Варианты:*\n"
            "• 💑 Отношения - для серьезных знакомств\n"
            "• 👥 Дружба - найти друзей и компанию\n"
            "• 💬 Общение - просто пообщаться\n"
            "• 🎉 Неважно - открыт ко всему",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
        return States.REG_GOAL
    
    else:
        # Добавление/удаление тега
        tag = query.data.replace("tag_", "")
        if 'registration' not in context.user_data:
            context.user_data['registration'] = {}
        
        interests = context.user_data['registration'].get('interests', [])
        
        if tag in interests:
            interests.remove(tag)
        elif len(interests) < 5:
            interests.append(tag)
        else:
            await query.answer("❌ Максимум 5 тегов!", show_alert=True)
            return States.REG_INTERESTS
        
        context.user_data['registration']['interests'] = interests
        
        # Определяем, показывать популярные или все теги
        if "all_tags" in query.message.text:
            # Показываем все теги
            keyboard = []
            row = []
            for i, t in enumerate(TAGS, 1):
                button_text = f"✅ {t}" if t in interests else t
                row.append(InlineKeyboardButton(button_text, callback_data=f"tag_{t}"))
                if i % 3 == 0:
                    keyboard.append(row)
                    row = []
            if row:
                keyboard.append(row)
            
            keyboard.append([InlineKeyboardButton("🔙 НАЗАД", callback_data="back_to_popular")])
            keyboard.append([InlineKeyboardButton("✅ ГОТОВО", callback_data="tags_done")])
            
            await query.edit_message_text(
                "🏷️ *ВСЕ ТЕГИ*\n\n"
                "Выбери до 5 тегов, которые тебе интересны:\n\n"
                f"🎯 Выбрано: {len(interests)}/5\n"
                "📍 Выбранные: " + (', '.join(interests) if interests else "пока нет"),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            # Показываем популярные теги
            popular_tags = ["Кофе", "Путешествия", "Спорт", "Кино", "Музыка", "Игры"]
            keyboard = []
            row = []
            for i, t in enumerate(popular_tags, 1):
                button_text = f"✅ {t}" if t in interests else t
                row.append(InlineKeyboardButton(button_text, callback_data=f"tag_{t}"))
                if i % 3 == 0:
                    keyboard.append(row)
                    row = []
            if row:
                keyboard.append(row)
            
            keyboard.append([InlineKeyboardButton("📋 ВСЕ ТЕГИ", callback_data="all_tags")])
            keyboard.append([InlineKeyboardButton("✅ ГОТОВО", callback_data="tags_done")])
            
            await query.edit_message_text(
                "🏷️ *ШАГ 6: ИНТЕРЕСЫ*\n\n"
                "Выбери до 5 тегов, которые тебе интересны:\n\n"
                f"🎯 Выбрано: {len(interests)}/5\n"
                "📍 Выбранные: " + (', '.join(interests) if interests else "пока нет"),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        return States.REG_INTERESTS

async def handle_registration_goal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора цели"""
    query = update.callback_query
    await query.answer()
    
    goal_map = {
        'goal_relationship': 'relationship',
        'goal_friendship': 'friendship',
        'goal_chat': 'chat',
        'goal_all': 'all'
    }
    
    if 'registration' not in context.user_data:
        context.user_data['registration'] = {}
    
    context.user_data['registration']['goal'] = goal_map[query.data]
    
    # Создаем пользователя в БД
    reg_data = context.user_data['registration']
    
    user_data = {
        'telegram_id': update.effective_user.id,
        'username': update.effective_user.username,
        'full_name': reg_data.get('name', update.effective_user.full_name),
        'age': reg_data.get('age'),
        'city': reg_data.get('city', 'Не указан'),
        'bio': reg_data.get('bio', ''),
        'interests': reg_data.get('interests', []),
        'goal': reg_data.get('goal', 'all'),
        'gender': reg_data.get('gender', 'other'),
        'search_gender': 'any',
        'search_age_min': 18,
        'search_age_max': 45,
        'search_radius': DEFAULT_SEARCH_RADIUS_KM,
        'photos': reg_data.get('photos', []),
        'last_seen': datetime.now().isoformat(),
        'likes_reset_date': datetime.now().strftime("%Y-%m-%d")
    }
    
    # Добавляем координаты если есть
    if 'latitude' in reg_data:
        user_data['latitude'] = reg_data['latitude']
    if 'longitude' in reg_data:
        user_data['longitude'] = reg_data['longitude']
    
    try:
        db_user = db.create_user(user_data)
        
        if db_user:
            # Очищаем временные данные
            context.user_data.pop('registration', None)
            
            # Быстрые кнопки для нового пользователя
            reply_markup = get_quick_actions_keyboard()
            
            # Inline-кнопки для быстрого старта
            inline_keyboard = [
                [InlineKeyboardButton("🔥 НАЧАТЬ ПРОСМОТР", callback_data="browse")],
                [InlineKeyboardButton("👤 ПОСМОТРЕТЬ ПРОФИЛЬ", callback_data="profile")],
                [InlineKeyboardButton("⚡️ БЫСТРЫЙ ПОИСК", callback_data="quick_search")]
            ]
            inline_markup = InlineKeyboardMarkup(inline_keyboard)
            
            await query.edit_message_text(
                f"🎉 *РЕГИСТРАЦИЯ ЗАВЕРШЕНА!*\n\n"
                f"🔥 Добро пожаловать, {user_data['full_name']}!\n\n"
                f"📊 *ТВОЙ ПРОФИЛЬ:*\n"
                f"• 👤 {user_data['full_name']}, {user_data['age']}\n"
                f"• 📍 {user_data['city']}\n"
                f"• 🎯 {user_data['goal']}\n"
                f"• 🏷️ {', '.join(user_data['interests'][:3])}\n\n"
                f"⚡️ *СТАТИСТИКА:*\n"
                f"• ❤️ {LIKES_PER_DAY_FREE} лайков в день\n"
                f"• 🌟 Премиум: ❌ (не активирован)\n\n"
                f"📍 *Совет:* Заполни профиль подробнее в разделе 'Мой профиль'",
                parse_mode=ParseMode.MARKDOWN
            )
            
            # Отправляем быстрые кнопки
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="🎯 *Используй быстрые кнопки для навигации:*",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
            
            # Отправляем inline-кнопки
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚡️ *Быстрый старт:*",
                reply_markup=inline_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await query.edit_message_text(
                "❌ *Ошибка при создании профиля*\n\n"
                "Попробуй снова: /start",
                parse_mode=ParseMode.MARKDOWN
            )
        
        return ConversationHandler.END
        
    except Exception as e:
        logging.error(f"Error creating user: {e}")
        await query.edit_message_text(
            "❌ *Ошибка при регистрации*\n\n"
            "Попробуй снова: /start",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END

async def browse_profiles_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показ анкет"""
    query = update.callback_query if hasattr(update, 'callback_query') else None
    
    if query:
        await query.answer()
        chat_id = update.effective_chat.id
    else:
        chat_id = update.effective_chat.id
    
    user = db.get_user_by_telegram_id(update.effective_user.id)
    if not user:
        if query:
            await query.edit_message_text("❌ Сначала зарегистрируйся: /start")
        else:
            await update.message.reply_text("❌ Сначала зарегистрируйся: /start")
        return
    
    # Сбрасываем дневные лайки если нужно
    db.reset_daily_likes_if_needed(user['telegram_id'])
    
    # Получаем следующую анкету
    profile = db.get_next_profile(user['telegram_id'])
    
    if not profile:
        if query:
            await query.edit_message_text(
                "😔 *ПОКА НЕТ ПОДХОДЯЩИХ АНКЕТ*\n\n"
                "⚡️ *Попробуй:*\n"
                "• Расширить радиус поиска (/settings)\n"
                "• Изменить критерии поиска\n"
                "• Зайти позже\n\n"
                "🔥 Новые пользователи появляются каждый день!",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text(
                "😔 *ПОКА НЕТ ПОДХОДЯЩИХ АНКЕТ*\n\n"
                "⚡️ *Попробуй:*\n"
                "• Расширить радиус поиска (/settings)\n"
                "• Изменить критерии поиска\n"
                "• Зайти позже\n\n"
                "🔥 Новые пользователи появляются каждый день!",
                parse_mode=ParseMode.MARKDOWN
            )
        return
    
    # Формируем подпись к фото
    caption = f"🔥 *{profile['full_name']}, {profile['age']}*\n"
    
    if profile['city']:
        caption += f"📍 {profile['city']}\n"
    
    if profile['bio']:
        bio_preview = profile['bio'][:100] + "..." if len(profile['bio']) > 100 else profile['bio']
        caption += f"\n📝 {bio_preview}\n"
    
    # Общие интересы
    user_interests = user['interests']
    profile_interests = profile['interests']
    common_interests = set(user_interests).intersection(set(profile_interests))
    
    if common_interests:
        caption += f"\n🎯 *Общие интересы:* {', '.join(list(common_interests)[:3])}\n"
    
    # Расстояние (если есть координаты)
    if user.get('latitude') and profile.get('latitude'):
        distance = calculate_distance(
            user['latitude'], user['longitude'],
            profile['latitude'], profile['longitude']
        )
        if distance > 0:
            if distance < 1:
                caption += f"\n📍 *Менее 1 км от тебя*"
            else:
                caption += f"\n📍 *Около {int(distance)} км от тебя*"
    
    # Inline-кнопки для быстрых действий (как в Tinder/Badoo)
    keyboard = [
        [
            InlineKeyboardButton("❤️ ЛАЙК", callback_data=f"like_{profile['telegram_id']}"),
            InlineKeyboardButton("💌 СУПЕРЛАЙК", callback_data=f"superlike_{profile['telegram_id']}"),
        ],
        [
            InlineKeyboardButton("➡️ ДАЛЬШЕ", callback_data="next_profile"),
            InlineKeyboardButton("👎 ПРОПУСТИТЬ", callback_data=f"skip_{profile['telegram_id']}"),
        ],
        [
            InlineKeyboardButton("🚫 ПОЖАЛОВАТЬСЯ", callback_data=f"report_{profile['telegram_id']}"),
            InlineKeyboardButton("⭐ В ИЗБРАННОЕ", callback_data=f"favorite_{profile['telegram_id']}"),
        ],
        [InlineKeyboardButton("🔙 В МЕНЮ", callback_data="main_menu")]
    ]
    
    # Быстрые кнопки клавиатуры
    reply_markup = get_browse_quick_actions()
    
    # Отправляем фото или текст
    if profile['photos']:
        photo = profile['photos'][0]
        
        try:
            if query:
                await query.message.delete()
        except:
            pass
        
        # Отправляем фото с подписью и inline-кнопками
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=photo,
            caption=caption,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
        # Отправляем быстрые кнопки отдельным сообщением
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚡️ *Используй быстрые кнопки:*",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        if query:
            await query.edit_message_text(
                caption,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text(
                caption,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            
            # Отправляем быстрые кнопки
            await update.message.reply_text(
                "⚡️ *Используй быстрые кнопки:*",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
    
    # Сохраняем ID текущей анкеты
    context.user_data['last_profile_id'] = profile['telegram_id']

# Обработчики для новых кнопок
async def handle_superlike(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка суперлайка"""
    query = update.callback_query
    await query.answer("⭐ Суперлайк отправлен!", show_alert=True)
    
    # Пока что суперлайк работает как обычный лайк
    target_user_id = int(query.data.split("_")[1])
    current_user_id = update.effective_user.id
    
    # Создаем лайк
    is_mutual = db.create_like(current_user_id, target_user_id)
    
    if is_mutual:
        await query.edit_message_caption(
            caption=query.message.caption + "\n\n🎉 *Есть взаимная симпатия!*",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await query.edit_message_caption(
            caption=query.message.caption + "\n\n⭐ *Суперлайк отправлен!*",
            parse_mode=ParseMode.MARKDOWN
        )
    
    # Показываем следующую анкету через 2 секунды
    await asyncio.sleep(2)
    await browse_profiles_callback(update, context)

async def handle_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка пропуска"""
    query = update.callback_query
    await query.answer("👎 Пропущено")
    
    # Показываем следующую анкету
    await browse_profiles_callback(update, context)

async def handle_favorite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка добавления в избранное"""
    query = update.callback_query
    await query.answer("⭐ Добавлено в избранное!", show_alert=True)
    
    # Показываем следующую анкету
    await browse_profiles_callback(update, context)

async def handle_like(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка лайка"""
    query = update.callback_query
    await query.answer()
    
    target_user_id = int(query.data.split("_")[1])
    current_user_id = update.effective_user.id
    
    # Создаем лайк
    is_mutual = db.create_like(current_user_id, target_user_id)
    
    if is_mutual:
        # Уведомляем о мэтче
        current_user = db.get_user_by_telegram_id(current_user_id)
        target_user = db.get_user_by_telegram_id(target_user_id)
        
        # Уведомляем текущего пользователя
        try:
            # Проверяем, есть ли у сообщения caption (фото) или text
            if query.message.caption:
                await query.edit_message_caption(
                    caption=query.message.caption + "\n\n🎉 *ЕСТЬ ВЗАИМНАЯ СИМПАТИЯ!*",
                    parse_mode=ParseMode.MARKDOWN
                )
            elif query.message.text:
                await query.edit_message_text(
                    text=query.message.text + "\n\n🎉 *ЕСТЬ ВЗАИМНАЯ СИМПАТИЯ!*",
                    parse_mode=ParseMode.MARKDOWN
                )
        except Exception as e:
            logging.error(f"Error editing message: {e}")
        
        # Уведомляем другого пользователя
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=f"🎉 *ЕСТЬ ВЗАИМНАЯ СИМПАТИЯ С {current_user['full_name']}!*\n\n"
                     f"🔥 Перейди в 'Мои чаты' чтобы начать общение!"
            )
        except:
            pass
        
        # Показываем следующую анкету через 3 секунды
        await asyncio.sleep(3)
        await browse_profiles_callback(update, context)
    else:
        try:
            if query.message.caption:
                await query.edit_message_caption(
                    caption=query.message.caption + "\n\n✅ *ЛАЙК ОТПРАВЛЕН!*",
                    parse_mode=ParseMode.MARKDOWN
                )
            elif query.message.text:
                await query.edit_message_text(
                    text=query.message.text + "\n\n✅ *ЛАЙК ОТПРАВЛЕН!*",
                    parse_mode=ParseMode.MARKDOWN
                )
        except Exception as e:
            logging.error(f"Error editing message: {e}")
        
        # Показываем следующую анкету через 2 секунды
        await asyncio.sleep(2)
        await browse_profiles_callback(update, context)

async def next_profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Следующая анкета"""
    query = update.callback_query
    await query.answer("🔄 Ищем...")
    
    await browse_profiles_callback(update, context)

async def profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Просмотр своего профиля"""
    query = update.callback_query
    await query.answer()
    
    user = db.get_user_by_telegram_id(update.effective_user.id)
    if not user:
        await query.edit_message_text("❌ Сначала зарегистрируйся: /start")
        return
    
    # Формируем текст профиля
    text = f"📊 *ТВОЙ ПРОФИЛЬ*\n\n"
    text += f"🔥 *{user['full_name']}, {user['age']}*\n"
    text += f"📍 {user['city'] or 'Город не указан'}\n"
    text += f"🎯 Цель: {user['goal'] or 'Не указана'}\n\n"
    
    if user['bio']:
        text += f"*О СЕБЕ:*\n{user['bio']}\n\n"
    
    interests = user['interests']
    if interests:
        text += f"*ИНТЕРЕСЫ:* {', '.join(interests)}\n\n"
    
    # Статистика
    likes_today = user.get('likes_today', 0)
    likes_limit = LIKES_PER_DAY_FREE if not user['is_premium'] else "∞"
    
    text += f"⚡️ *СТАТИСТИКА:*\n"
    text += f"• ❤️ Лайков сегодня: {likes_today}/{likes_limit}\n"
    text += f"• 🌟 Премиум: {'✅ АКТИВЕН' if user['is_premium'] else '❌ НЕ АКТИВЕН'}\n"
    text += f"• 🔥 Активен: {'✅ ДА' if user['is_active'] else '❌ НЕТ'}\n\n"
    
    # Inline-кнопки для профиля
    keyboard = [
        [
            InlineKeyboardButton("✏️ РЕДАКТИРОВАТЬ", callback_data="edit_profile"),
            InlineKeyboardButton("⚙️ НАСТРОЙКИ", callback_data="settings")
        ],
        [
            InlineKeyboardButton("🌟 ПРЕМИУМ", callback_data="premium_info"),
            InlineKeyboardButton("📊 СТАТИСТИКА", callback_data="stats_info")
        ],
        [InlineKeyboardButton("🔙 В МЕНЮ", callback_data="main_menu")]
    ]
    
    # Быстрые кнопки
    reply_markup = get_profile_quick_actions()
    
    # Если есть фото, отправляем с фото
    if user['photos']:
        photo = user['photos'][0]
        
        try:
            await query.message.delete()
        except:
            pass
        
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=photo,
            caption=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
        # Отправляем быстрые кнопки
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚡️ *Быстрые действия:*",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
        # Отправляем быстрые кнопки
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚡️ *Быстрые действия:*",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

async def chats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список активных чатов"""
    query = update.callback_query
    await query.answer()
    
    user = db.get_user_by_telegram_id(update.effective_user.id)
    if not user:
        await query.edit_message_text("❌ Сначала зарегистрируйся: /start")
        return
    
    matches = db.get_active_matches(user['telegram_id'])
    
    if not matches:
        inline_keyboard = [
            [InlineKeyboardButton("🔥 НАЧАТЬ ПРОСМОТР", callback_data="browse")],
            [InlineKeyboardButton("⚡️ БЫСТРЫЙ ПОИСК", callback_data="quick_search")]
        ]
        
        await query.edit_message_text(
            "💬 *У ТЕБЯ ПОКА НЕТ АКТИВНЫХ ЧАТОВ*\n\n"
            "⚡️ *Совет:* Начни просмотр анкет и ставь ❤️\n"
            "При взаимной симпатии откроется чат на 24 часа!\n\n"
            "🔥 Новые знакомства ждут тебя!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(inline_keyboard)
        )
        
        # Быстрые кнопки
        reply_markup = get_chats_quick_actions()
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚡️ *Быстрые действия:*",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    text = "💬 *ТВОИ АКТИВНЫЕ ЧАТЫ:*\n\n"
    keyboard = []
    
    for match in matches:
        partner_name = match.get('partner_name', 'Пользователь')
        partner_id = match.get('partner_telegram_id')
        
        # Рассчитываем оставшееся время
        expires_at_str = match['chat_expires_at']
        if 'Z' in expires_at_str:
            expires_at = datetime.fromisoformat(expires_at_str.replace('Z', '+00:00'))
        else:
            expires_at = datetime.fromisoformat(expires_at_str)
        
        time_left = expires_at - datetime.now()
        hours_left = max(0, int(time_left.total_seconds() // 3600))
        minutes_left = max(0, int((time_left.total_seconds() % 3600) // 60))
        
        text += f"• 💬 {partner_name} - {hours_left}ч {minutes_left}м осталось\n"
        keyboard.append([InlineKeyboardButton(
            f"💬 {partner_name} ({hours_left}ч)", 
            callback_data=f"chat_{match['id']}"
        )])
    
    keyboard.append([InlineKeyboardButton("🔄 ОБНОВИТЬ", callback_data="chats")])
    keyboard.append([InlineKeyboardButton("🔙 В МЕНЮ", callback_data="main_menu")])
    
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    # Быстрые кнопки
    reply_markup = get_chats_quick_actions()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="⚡️ *Быстрые действия:*",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главное меню"""
    query = update.callback_query
    await query.answer()
    
    user = db.get_user_by_telegram_id(update.effective_user.id)
    
    # Быстрые кнопки
    reply_markup = get_quick_actions_keyboard()
    
    if user:
        # Inline-кнопки для быстрого доступа
        inline_keyboard = [
            [InlineKeyboardButton("🔥 НАЧАТЬ ПРОСМОТР", callback_data="browse")],
            [InlineKeyboardButton("👤 МОЙ ПРОФИЛЬ", callback_data="profile"),
             InlineKeyboardButton("💬 МОИ ЧАТЫ", callback_data="chats")],
            [InlineKeyboardButton("⚡️ БЫСТРЫЙ ПОИСК", callback_data="quick_search"),
             InlineKeyboardButton("📍 РЯДОМ СЕЙЧАС", callback_data="nearby_now")],
            [InlineKeyboardButton("🌟 ПРЕМИУМ", callback_data="premium_info"),
             InlineKeyboardButton("🆘 ПОМОЩЬ", callback_data="help_callback")]
        ]
        
        inline_markup = InlineKeyboardMarkup(inline_keyboard)
        
        await query.edit_message_text(
            f"🔥 *ГЛАВНОЕ МЕНЮ*\n\n"
            f"Привет, {user['full_name'] or 'друг'}!\n\n"
            f"⚡️ *Статус:* {'🌟 ПРЕМИУМ' if user['is_premium'] else '⚡️ БАЗОВЫЙ'}\n"
            f"❤️ Лайков сегодня: {user.get('likes_today', 0)}/{LIKES_PER_DAY_FREE}\n\n"
            f"🎯 *Что делаем?*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=inline_markup
        )
        
        # Отправляем быстрые кнопки
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚡️ *Используй быстрые кнопки для навигации:*",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        keyboard = [[InlineKeyboardButton("🚀 НАЧАТЬ РЕГИСТРАЦИЮ", callback_data="start_registration")]]
        await query.edit_message_text(
            "🔥 *Добро пожаловать в РЯДОМ!*\n\n"
            "Знакомства рядом с тобой • Быстро • Безопасно • Интересно\n\n"
            "📝 Начни регистрацию прямо сейчас!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Быстрый вызов помощи"""
    query = update.callback_query
    await query.answer()
    
    await help_command(update, context)

async def premium_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Информация о премиуме"""
    query = update.callback_query
    await query.answer()
    
    user = db.get_user_by_telegram_id(update.effective_user.id)
    
    keyboard = [
        [InlineKeyboardButton("🌟 АКТИВИРОВАТЬ ПРЕМИУМ", callback_data="activate_premium")],
        [InlineKeyboardButton("🔙 НАЗАД", callback_data="main_menu")]
    ]
    
    text = "🌟 *ПРЕМИУМ ПОДПИСКА*\n\n"
    text += "⚡️ *Преимущества:*\n"
    text += "• ❤️ Неограниченное количество лайков\n"
    text += "• 📍 Видеть кто онлайн рядом прямо сейчас\n"
    text += "• 🚀 Приоритет в показе твоей анкеты\n"
    text += "• 🔍 Расширенные фильтры поиска\n"
    text += "• ⭐ Суперлайки (выделят твой лайк)\n"
    text += "• 💬 Продление чатов до 72 часов\n\n"
    
    if user and user['is_premium']:
        text += "✅ *Твой статус:* ПРЕМИУМ АКТИВЕН\n"
        text += "🎉 Ты пользуешься всеми преимуществами!\n"
    else:
        text += "❌ *Твой статус:* БАЗОВЫЙ\n"
        text += "🔥 Активируй премиум для полного доступа!\n\n"
        text += "💎 *Стоимость:* 299₽/месяц\n"
    
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def activate_premium_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Активация премиума"""
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("✅ ПОДТВЕРДИТЬ ОПЛАТУ", callback_data="confirm_payment")],
        [InlineKeyboardButton("🔙 НАЗАД", callback_data="premium_info")]
    ]
    
    await query.edit_message_text(
        "💎 *АКТИВАЦИЯ ПРЕМИУМА*\n\n"
        "⚡️ *Для активации:*\n"
        "1. Переведи 299₽ на карту:\n"
        "   `2200 7001 2345 6789`\n"
        "2. Укажи в комментарии свой ID:\n"
        f"   `{update.effective_user.id}`\n"
        "3. Нажми 'Подтвердить оплату'\n\n"
        "📍 *После подтверждения оплаты:*\n"
        "• Премиум активируется в течение 5 минут\n"
        "• Ты получишь уведомление\n"
        "• Все функции станут доступны сразу",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /help"""
    help_text = """
    🤖 *БОТ ДЛЯ ЗНАКОМСТВ «РЯДОМ»*

    ⚡️ *ОСНОВНЫЕ КОМАНДЫ:*
    /start - Главное меню
    /profile - Мой профиль
    /browse - Начать просмотр анкет
    /chats - Мои активные чаты
    /help - Эта справка

    🎯 *КАК ЭТО РАБОТАЕТ?*
    1. 📝 Заполни профиль (/start)
    2. 👀 Смотри анкеты и ставь ❤️
    3. 💬 При взаимной симпатии открывается чат на 24 часа
    4. 🔥 Общайся и обменивайся контактами!

    ⭐ *ПРЕИМУЩЕСТВА:*
    • 📍 Геолокационный поиск
    • 🎯 Подбор по интересам
    • 🔒 Безопасные чаты
    • ⚡️ Быстрые знакомства

    ⚠️ *ПРАВИЛА:*
    • 🙏 Будь вежлив и уважителен
    • 🚫 Не спамь
    • 🔒 Не передавай личные данные сразу
    • 📢 Сообщай о нарушениях

    📞 *ПОДДЕРЖКА:* @Tseerber
    """
    
    if hasattr(update, 'callback_query'):
        await update.callback_query.edit_message_text(help_text, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отмена текущего действия"""
    await update.message.reply_text(
        "❌ Действие отменено.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    logging.error(f"Exception while handling an update: {context.error}")
    
    try:
        await context.bot.send_message(
            chat_id=update.effective_chat.id if update else None,
            text="❌ Произошла ошибка. Пожалуйста, попробуй снова."
        )
    except:
        pass

# Обработчик текстовых сообщений для быстрых кнопок
async def handle_quick_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка быстрых кнопок"""
    text = update.message.text
    
    if text == "👀 Смотреть анкеты":
        await browse_profiles_callback(update, context)
    elif text == "📊 Мой профиль":
        # Создаем callback query для профиля
        class FakeQuery:
            def __init__(self):
                self.data = "profile"
                self.message = update.message
                self.from_user = update.effective_user
            
            async def answer(self, *args, **kwargs):
                pass
            
            async def edit_message_text(self, *args, **kwargs):
                await update.message.reply_text(*args, **kwargs)
        
        fake_query = FakeQuery()
        fake_update = Update(update.update_id, message=update.message)
        fake_update.callback_query = fake_query
        
        await profile_callback(fake_update, context)
    elif text == "💬 Мои чаты":
        # Аналогично для чатов
        class FakeQuery:
            def __init__(self):
                self.data = "chats"
                self.message = update.message
                self.from_user = update.effective_user
            
            async def answer(self, *args, **kwargs):
                pass
            
            async def edit_message_text(self, *args, **kwargs):
                await update.message.reply_text(*args, **kwargs)
        
        fake_query = FakeQuery()
        fake_update = Update(update.update_id, message=update.message)
        fake_update.callback_query = fake_query
        
        await chats_callback(fake_update, context)
    elif text == "⚙️ Настройки":
        await update.message.reply_text("⚙️ *Настройки*\n\nЭта функция в разработке. Скоро будет доступна!", parse_mode=ParseMode.MARKDOWN)
    elif text == "🌟 Премиум":
        await update.message.reply_text("🌟 *Премиум*\n\nДля активации премиума используй inline-кнопки в меню.", parse_mode=ParseMode.MARKDOWN)
    elif text == "🆘 Помощь":
        await help_command(update, context)
    elif text == "❤️ Лайк":
        await update.message.reply_text("❤️ Используй inline-кнопки под анкетой для лайков!", parse_mode=ParseMode.MARKDOWN)
    elif text == "➡️ Дальше":
        await update.message.reply_text("➡️ Используй inline-кнопки под анкетой для навигации!", parse_mode=ParseMode.MARKDOWN)
    elif text == "🔙 В меню" or text == "🔙 Назад в меню":
        await update.message.reply_text("⚡️ Возвращаю в меню...", reply_markup=get_quick_actions_keyboard())
        await main_menu_callback(update, context)
    elif text == "✏️ Редактировать":
        await update.message.reply_text("✏️ *Редактирование профиля*\n\nЭта функция в разработке. Скоро будет доступна!", parse_mode=ParseMode.MARKDOWN)
    elif text == "⚙️ Настройки поиска":
        await update.message.reply_text("⚙️ *Настройки поиска*\n\nЭта функция в разработке. Скоро будет доступна!", parse_mode=ParseMode.MARKDOWN)

# ==================== MAIN ====================
def main():
    """Запуск бота"""
    # Настройка логирования

    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    
    # Проверка токена
    if BOT_TOKEN == "ВСТАВЬТЕ_ВАШ_ТОКЕН_ЗДЕСЬ":
        print("❌ ОШИБКА: Вы не указали токен бота!")
        print("📝 Получите токен у @BotFather в Telegram")
        print("🔧 Замените строку: BOT_TOKEN = 'ВСТАВЬТЕ_ВАШ_ТОКЕН_ЗДЕСЬ'")
        print("   на ваш реальный токен, например:")
        print("   BOT_TOKEN = '8524498297:AAE07uhhKek7jg7gwNyMeGHA_oDJCgWXvns'")
        return
    
    # Создание приложения
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Добавляем обработчик ошибок
    application.add_error_handler(error_handler)
    
    # Обработчик регистрации
    registration_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_registration_callback, pattern="^start_registration$")
        ],
        states={
            States.REG_PHOTO: [
                MessageHandler(filters.PHOTO, handle_registration_photo)
            ],
            States.REG_NAME_AGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_registration_name_age)
            ],
            States.REG_GENDER: [
                CallbackQueryHandler(handle_registration_gender, pattern="^gender_")
            ],
            States.REG_CITY: [
                MessageHandler(filters.TEXT | filters.LOCATION, handle_registration_city)
            ],
            States.REG_BIO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_registration_bio)
            ],
            States.REG_INTERESTS: [
                CallbackQueryHandler(handle_registration_interests, pattern="^(tag_|tags_done|all_tags|back_to_popular)")
            ],
            States.REG_GOAL: [
                CallbackQueryHandler(handle_registration_goal, pattern="^goal_")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )
    
    # Команды
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(registration_handler)
    
    # Обработчик быстрых кнопок
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_quick_buttons))
    
    # Callback-запросы
    application.add_handler(CallbackQueryHandler(browse_profiles_callback, pattern="^browse$"))
    application.add_handler(CallbackQueryHandler(profile_callback, pattern="^profile$"))
    application.add_handler(CallbackQueryHandler(chats_callback, pattern="^chats$"))
    application.add_handler(CallbackQueryHandler(main_menu_callback, pattern="^main_menu$"))
    application.add_handler(CallbackQueryHandler(next_profile_callback, pattern="^next_profile$"))
    application.add_handler(CallbackQueryHandler(handle_like, pattern="^like_"))
    application.add_handler(CallbackQueryHandler(handle_superlike, pattern="^superlike_"))
    application.add_handler(CallbackQueryHandler(handle_skip, pattern="^skip_"))
    application.add_handler(CallbackQueryHandler(handle_favorite, pattern="^favorite_"))
    application.add_handler(CallbackQueryHandler(quick_search_callback, pattern="^quick_search$"))
    application.add_handler(CallbackQueryHandler(nearby_now_callback, pattern="^nearby_now$"))
    application.add_handler(CallbackQueryHandler(help_callback, pattern="^help_callback$"))
    application.add_handler(CallbackQueryHandler(premium_info_callback, pattern="^premium_info$"))
    application.add_handler(CallbackQueryHandler(activate_premium_callback, pattern="^activate_premium$"))
    
    # Запуск бота
    print("работает")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
