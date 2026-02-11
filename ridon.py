import os
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
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

MAX_PHOTOS = 3
MAX_BIO_LENGTH = 500
DEFAULT_SEARCH_RADIUS_KM = 50
CHAT_DURATION_HOURS = 24
LIKES_PER_DAY_FREE = 20

class States:
    REG_PHOTO = 1
    REG_NAME_AGE = 2
    REG_GENDER = 3
    REG_CITY = 4
    REG_BIO = 5
    EDIT_PROFILE = 7
    EDIT_NAME_AGE = 8
    EDIT_BIO = 9
    EDIT_PHOTO = 10
    EDIT_CITY = 11

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
                    gender TEXT,
                    photos TEXT,  -- JSON список file_id
                    likes_today INTEGER DEFAULT 0,
                    likes_reset_date TEXT,
                    is_active BOOLEAN DEFAULT 1,
                    is_banned BOOLEAN DEFAULT 0,
                    last_seen TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            
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
            
            
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_telegram ON users(telegram_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active)")
    
    def get_user_by_telegram_id(self, telegram_id: int) -> Optional[Dict]:
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            row = cursor.fetchone()
            if row:
                user = dict(row)
                
                # Десериализация photos
                if user['photos']:
                    try:
                        user['photos'] = json.loads(user['photos'])
                    except:
                        user['photos'] = []
                else:
                    user['photos'] = []
                return user
            return None
    
    def create_user(self, user_data: Dict) -> Optional[Dict]:
        with self.get_connection() as conn:
            
            data_to_insert = user_data.copy()
            # Сериализация photos
            if 'photos' in data_to_insert and isinstance(data_to_insert['photos'], list):
                data_to_insert['photos'] = json.dumps(data_to_insert['photos'], ensure_ascii=False)
            
            fields = list(data_to_insert.keys())
            placeholders = ['?' for _ in fields]
            
            sql = f"""
                INSERT INTO users ({', '.join(fields)})
                VALUES ({', '.join(placeholders)})
            """
            
            try:
                cursor = conn.execute(sql, list(data_to_insert.values()))
                user_id = cursor.lastrowid
                
                
                cursor = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
                row = cursor.fetchone()
                if row:
                    user = dict(row)
                    
                    # Десериализация photos
                    if user['photos']:
                        try:
                            user['photos'] = json.loads(user['photos'])
                        except:
                            user['photos'] = []
                    else:
                        user['photos'] = []
                    return user
            except Exception as e:
                logging.error(f"Error creating user: {e}")
            return None
    
    def update_user(self, telegram_id: int, updates: Dict) -> bool:
        with self.get_connection() as conn:
            
            data_to_update = updates.copy()
            # Сериализация photos
            if 'photos' in data_to_update and isinstance(data_to_update['photos'], list):
                data_to_update['photos'] = json.dumps(data_to_update['photos'], ensure_ascii=False)
            
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
            
            
            query = """
                SELECT u.* FROM users u
                WHERE u.telegram_id != ?
                AND u.is_active = 1
                AND u.is_banned = 0
                AND NOT EXISTS (
                    SELECT 1 FROM likes l 
                    WHERE l.from_user_id = ?
                    AND l.to_user_id = u.id
                )
                ORDER BY RANDOM()
                LIMIT 1
            """
            
            params = [
                current_user_id,  
                user_id_db
            ]
            
            cursor = conn.execute(query, params)
            row = cursor.fetchone()
            
            if row:
                profile = dict(row)
               
                # Десериализация photos
                if profile['photos']:
                    try:
                        profile['photos'] = json.loads(profile['photos'])
                    except:
                        profile['photos'] = []
                else:
                    profile['photos'] = []
                return profile
            
            return None
    
    def create_like(self, from_user_id: int, to_user_id: int) -> Tuple[bool, Optional[Dict]]:
        """Создать лайк и проверить на взаимность, возвращает (взаимный, данные пользователя)"""
        with self.get_connection() as conn:
           
            from_user = self.get_user_by_telegram_id(from_user_id)
            to_user = self.get_user_by_telegram_id(to_user_id)
            
            if not from_user or not to_user:
                return False, None
            
           
            today = datetime.now().strftime("%Y-%m-%d")
            if from_user.get('likes_reset_date') != today:
                conn.execute(
                    "UPDATE users SET likes_today = 0, likes_reset_date = ? WHERE telegram_id = ?",
                    (today, from_user_id)
                )
                from_user['likes_today'] = 0
            
            likes_limit = LIKES_PER_DAY_FREE
            if from_user['likes_today'] >= likes_limit:
                return False, None
            
           
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO likes (from_user_id, to_user_id) VALUES (?, ?)",
                    (from_user['id'], to_user['id'])
                )
                
                
                conn.execute(
                    "UPDATE users SET likes_today = likes_today + 1 WHERE telegram_id = ?",
                    (from_user_id,)
                )
                
                
                cursor = conn.execute("""
                    SELECT 1 FROM likes 
                    WHERE from_user_id = ? AND to_user_id = ?
                """, (to_user['id'], from_user['id']))
                
                mutual = cursor.fetchone() is not None
                
                if mutual:
                    return True, to_user
                else:
                    return False, None
                
            except Exception as e:
                logging.error(f"Error creating like: {e}")
                return False, None
    
    def get_users_who_liked_me(self, telegram_id: int) -> List[Dict]:
        """Получить список пользователей, которые лайкнули меня"""
        with self.get_connection() as conn:
            user = self.get_user_by_telegram_id(telegram_id)
            if not user:
                return []
            
            query = """
                SELECT u.* FROM users u
                JOIN likes l ON l.from_user_id = u.id
                WHERE l.to_user_id = ?
                AND u.is_active = 1
                AND u.is_banned = 0
                ORDER BY l.created_at DESC
            """
            
            cursor = conn.execute(query, (user['id'],))
            rows = cursor.fetchall()
            
            profiles = []
            for row in rows:
                profile = dict(row)
                if profile['photos']:
                    try:
                        profile['photos'] = json.loads(profile['photos'])
                    except:
                        profile['photos'] = []
                else:
                    profile['photos'] = []
                profiles.append(profile)
            
            return profiles
    
    def get_mutual_likes(self, telegram_id: int) -> List[Dict]:
        """Получить список взаимных лайков"""
        with self.get_connection() as conn:
            user = self.get_user_by_telegram_id(telegram_id)
            if not user:
                return []
            
            query = """
                SELECT u.* FROM users u
                JOIN likes l1 ON l1.from_user_id = u.id
                JOIN likes l2 ON l2.from_user_id = ? AND l2.to_user_id = u.id
                WHERE l1.to_user_id = ?
                AND u.is_active = 1
                AND u.is_banned = 0
                ORDER BY l1.created_at DESC
            """
            
            cursor = conn.execute(query, (user['id'], user['id']))
            rows = cursor.fetchall()
            
            profiles = []
            for row in rows:
                profile = dict(row)
                if profile['photos']:
                    try:
                        profile['photos'] = json.loads(profile['photos'])
                    except:
                        profile['photos'] = []
                else:
                    profile['photos'] = []
                profiles.append(profile)
            
            return profiles

db = Database(DB_PATH)


def calculate_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Рассчитать расстояние между двумя точками (км)"""
    if not all([lat1, lon1, lat2, lon2]):
        return 0
    
    
    R = 6371  
    
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    
    a = math.sin(delta_lat/2)**2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    
    return R * c


def get_quick_actions_keyboard():
    """Быстрые кнопки для главного меню"""
    return ReplyKeyboardMarkup([
        ["👀 Смотреть анкеты", "📊 Мой профиль"],
        ["❤️ Кто меня лайкнул", "🆘 Помощь"]
    ], resize_keyboard=True, one_time_keyboard=False)

def get_profile_quick_actions():
    """Быстрые кнопки для профиля"""
    return ReplyKeyboardMarkup([
        ["✏️ Редактировать профиль", "❤️ Кто меня лайкнул"],
        ["🔙 Назад в меню"]
    ], resize_keyboard=True)

def get_browse_quick_actions():
    """Быстрые кнопки для просмотра анкет"""
    return ReplyKeyboardMarkup([
        ["❤️ Лайк", "➡️ Дальше"],
        ["🚫 Пожаловаться", "🔙 В меню"]
    ], resize_keyboard=True)

def get_gender_keyboard():
    """Клавиатура для выбора пола"""
    return ReplyKeyboardMarkup([
        ["👨 МУЖЧИНА", "👩 ЖЕНЩИНА"]
    ], resize_keyboard=True, one_time_keyboard=True)

def get_edit_profile_keyboard():
    """Клавиатура для редактирования профиля"""
    return ReplyKeyboardMarkup([
        ["✏️ Имя и возраст", "📝 О себе"],
        ["📸 Фото", "📍 Город"],
        ["🔙 К моему профилю"]
    ], resize_keyboard=True, one_time_keyboard=True)

def get_back_to_profile_keyboard():
    """Кнопка возврата к профилю"""
    return ReplyKeyboardMarkup([
        ["🔙 К моему профилю"]
    ], resize_keyboard=True)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    db_user = db.get_user_by_telegram_id(user.id)
    
    if db_user:
        
        reply_markup = get_quick_actions_keyboard()
        
        await update.message.reply_text(
            f"🔥 *С возвращением, {db_user['full_name'] or user.first_name}!*\n\n"
            "Используй быстрые кнопки ниже для навигации:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        
        return States.REG_PHOTO
    else:
        
        await update.message.reply_text(
            "🔥 *Добро пожаловать в РЯДОМ!*\n\n"
            "Знакомства рядом с тобой • Быстро • Безопасно • Интересно\n\n"
            "📝 Регистрация займет всего 2 минуты!\n\n"
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
        
        reply_markup = get_gender_keyboard()
        
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
    gender_text = update.message.text
    
    gender_map = {
        '👨 МУЖЧИНА': 'male',
        '👩 ЖЕНЩИНА': 'female'
    }
    
    if gender_text not in gender_map:
        reply_markup = get_gender_keyboard()
        await update.message.reply_text(
            "❌ Пожалуйста, выбери пол из предложенных вариантов:",
            reply_markup=reply_markup
        )
        return States.REG_GENDER
    
    if 'registration' not in context.user_data:
        context.user_data['registration'] = {}
    
    context.user_data['registration']['gender'] = gender_map[gender_text]
    
    
    reply_markup = ReplyKeyboardMarkup([
        ["📍 Отправить геолокацию"],
        ["🏙️ Ввести вручную"]
    ], resize_keyboard=True, one_time_keyboard=True)
    
    await update.message.reply_text(
        "📍 *ШАГ 4: ГОРОД*\n\n"
        "Отправь свой город или геолокацию:\n\n"
        "⚡️ Можно отправить геолокацию кнопкой ниже\n"
        "📍 Или просто напиши название города",
        parse_mode=ParseMode.MARKDOWN,
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
    
    
    reg_data = context.user_data['registration']
    
    user_data = {
        'telegram_id': update.effective_user.id,
        'username': update.effective_user.username,
        'full_name': reg_data.get('name', update.effective_user.full_name),
        'age': reg_data.get('age'),
        'city': reg_data.get('city', 'Не указан'),
        'bio': reg_data.get('bio', ''),
        'gender': reg_data.get('gender', 'male'),
        'photos': reg_data.get('photos', []),
        'last_seen': datetime.now().isoformat(),
        'likes_reset_date': datetime.now().strftime("%Y-%m-%d")
    }
    
    
    if 'latitude' in reg_data:
        user_data['latitude'] = reg_data['latitude']
    if 'longitude' in reg_data:
        user_data['longitude'] = reg_data['longitude']
    
    try:
        db_user = db.create_user(user_data)
        
        if db_user:
            
            context.user_data.pop('registration', None)
            
            
            reply_markup = get_quick_actions_keyboard()
            
            await update.message.reply_text(
                f"🎉 *РЕГИСТРАЦИЯ ЗАВЕРШЕНА!*\n\n"
                f"🔥 Добро пожаловать, {user_data['full_name']}!\n\n"
                f"📊 *ТВОЙ ПРОФИЛЬ:*\n"
                f"• 👤 {user_data['full_name']}, {user_data['age']}\n"
                f"• 📍 {user_data['city']}\n\n"
                f"⚡️ *СТАТИСТИКА:*\n"
                f"• ❤️ {LIKES_PER_DAY_FREE} лайков в день\n\n"
                f"📍 *Совет:* Заполни профиль подробнее в разделе 'Мой профиль'",
                parse_mode=ParseMode.MARKDOWN
            )
            
            
            await update.message.reply_text(
                "🎯 *Используй быстрые кнопки для навигации:*",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text(
                "❌ *Ошибка при создании профиля*\n\n"
                "Попробуй снова: /start",
                parse_mode=ParseMode.MARKDOWN
            )
        
        return ConversationHandler.END
        
    except Exception as e:
        logging.error(f"Error creating user: {e}")
        await update.message.reply_text(
            "❌ *Ошибка при регистрации*\n\n"
            "Попробуй снова: /start",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END

async def browse_profiles_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для просмотра анкет"""
    user = db.get_user_by_telegram_id(update.effective_user.id)
    if not user:
        await update.message.reply_text("❌ Сначала зарегистрируйся: /start")
        return
    
    
    await show_next_profile(update, context)

async def show_next_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать следующую анкету"""
    user = db.get_user_by_telegram_id(update.effective_user.id)
    if not user:
        return
    
    
    db.reset_daily_likes_if_needed(user['telegram_id'])
    
    
    profile = db.get_next_profile(user['telegram_id'])
    
    if not profile:
        reply_markup = get_browse_quick_actions()
        await update.message.reply_text(
            "😔 *ПОКА НЕТ ПОДХОДЯЩИХ АНКЕТ*\n\n"
            "⚡️ *Попробуй:*\n"
            "• Зайти позже\n\n"
            "🔥 Новые пользователи появляются каждый день!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
        return
    
    
    caption = f"🔥 *{profile['full_name']}, {profile['age']}*\n"
    
    if profile['city']:
        caption += f"📍 {profile['city']}\n"
    
    if profile['bio']:
        bio_preview = profile['bio'][:100] + "..." if len(profile['bio']) > 100 else profile['bio']
        caption += f"\n📝 {bio_preview}\n"
    
    
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
    
    reply_markup = get_browse_quick_actions()
    
    
    context.user_data['current_profile_id'] = profile['telegram_id']
    
    
    if profile['photos']:
        photo = profile['photos'][0]
        
        await update.message.reply_photo(
            photo=photo,
            caption=caption,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            caption,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )

async def handle_like_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка лайка"""
    user = db.get_user_by_telegram_id(update.effective_user.id)
    if not user:
        return
    
    target_user_id = context.user_data.get('current_profile_id')
    if not target_user_id:
        await update.message.reply_text("❌ Не найдена текущая анкета.")
        return
    
    
    is_mutual, liked_user = db.create_like(user['telegram_id'], target_user_id)
    
    if is_mutual and liked_user:
        
        username = liked_user.get('username')
        user_link = f"@{username}" if username else f"tg://user?id={liked_user['telegram_id']}"
        
        await update.message.reply_text(
            f"🎉 *ЕСТЬ ВЗАИМНАЯ СИМПАТИЯ!*\n\n"
            f"🔥 *{liked_user['full_name']}, {liked_user['age']}* тоже лайкнул(а) тебя!\n\n"
            f"💬 *Начни общение:* {user_link}\n\n"
            f"⚡️ *Совет:* Представься и начни интересный разговор!",
            parse_mode=ParseMode.MARKDOWN
        )
    elif liked_user:
        
        await update.message.reply_text(
            "✅ *ЛАЙК ОТПРАВЛЕН!*\n\n"
            f"Ждем ответа от *{liked_user['full_name']}*...",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            "❌ *Не удалось отправить лайк.*\n"
            "Возможно, достигнут лимит лайков на сегодня.",
            parse_mode=ParseMode.MARKDOWN
        )
    
    
    await asyncio.sleep(1)
    await show_next_profile(update, context)

async def handle_next_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка перехода к следующей анкете"""
    await update.message.reply_text("🔄 Ищем следующую анкету...")
    await show_next_profile(update, context)

async def handle_report_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка жалобы"""
    await update.message.reply_text(
        "🚫 *ЖАЛОБА*\n\n"
        "Опиши причину жалобы на этого пользователя:"
    )
    
    context.user_data['reporting'] = True
    context.user_data['reported_user_id'] = context.user_data.get('current_profile_id')

async def handle_report_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текста жалобы"""
    if context.user_data.get('reporting'):
        reason = update.message.text
        reported_user_id = context.user_data.get('reported_user_id')
        
        if reported_user_id:
            with db.get_connection() as conn:
                reporter = db.get_user_by_telegram_id(update.effective_user.id)
                reported = db.get_user_by_telegram_id(reported_user_id)
                
                if reporter and reported:
                    conn.execute(
                        "INSERT INTO reports (reporter_id, reported_user_id, reason) VALUES (?, ?, ?)",
                        (reporter['id'], reported['id'], reason)
                    )
                    
                    await update.message.reply_text(
                        "✅ *Жалоба отправлена администраторам.*\n\n"
                        "Спасибо за помощь в поддержании сообщества!"
                    )
        
        context.user_data.pop('reporting', None)
        context.user_data.pop('reported_user_id', None)
        
        
        await show_next_profile(update, context)

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда просмотра профиля"""
    user = db.get_user_by_telegram_id(update.effective_user.id)
    if not user:
        await update.message.reply_text("❌ Сначала зарегистрируйся: /start")
        return
    
    
    text = f"📊 *ТВОЙ ПРОФИЛЬ*\n\n"
    text += f"🔥 *{user['full_name']}, {user['age']}*\n"
    text += f"📍 {user['city'] or 'Город не указан'}\n"
    
    if user['bio']:
        text += f"\n*О СЕБЕ:*\n{user['bio']}\n\n"
    
    
    likes_today = user.get('likes_today', 0)
    likes_limit = LIKES_PER_DAY_FREE
    
    
    users_who_liked_me = db.get_users_who_liked_me(user['telegram_id'])
    
    text += f"⚡️ *СТАТИСТИКА:*\n"
    text += f"• ❤️ Лайков сегодня: {likes_today}/{likes_limit}\n"
    text += f"• 💌 Тебя лайкнули: {len(users_who_liked_me)} чел.\n"
    text += f"• 🔥 Активен: {'✅ ДА' if user['is_active'] else '❌ НЕТ'}\n\n"
    
    reply_markup = get_profile_quick_actions()
    
    
    if user['photos']:
        photo = user['photos'][0]
        
        await update.message.reply_photo(
            photo=photo,
            caption=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )

async def start_edit_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало редактирования профиля"""
    user = db.get_user_by_telegram_id(update.effective_user.id)
    if not user:
        return
    
    reply_markup = get_edit_profile_keyboard()
    await update.message.reply_text(
        "✏️ *РЕДАКТИРОВАНИЕ ПРОФИЛЯ*\n\n"
        "Выбери, что хочешь изменить:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )
    
    return States.EDIT_PROFILE

async def handle_edit_name_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка редактирования имени и возраста"""
    await update.message.reply_text(
        "✏️ *ИЗМЕНЕНИЕ ИМЕНИ И ВОЗРАСТА*\n\n"
        "Введи свое имя и возраст:\n"
        "*Пример: Иван 25* или *Анна 22*\n\n"
        "⚡️ Пиши как в примере выше",
        parse_mode=ParseMode.MARKDOWN
    )
    
    return States.EDIT_NAME_AGE

async def handle_edit_name_age_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ввода нового имени и возраста"""
    try:
        text = update.message.text.strip()
        parts = text.split()
        
        if len(parts) < 2:
            raise ValueError
        
        name = ' '.join(parts[:-1])
        age = int(parts[-1])
        
        if not 18 <= age <= 100:
            await update.message.reply_text("❌ Возраст должен быть от 18 до 100 лет.")
            return States.EDIT_NAME_AGE
        
        
        db.update_user(update.effective_user.id, {
            'full_name': name,
            'age': age
        })
        
        await update.message.reply_text(
            "✅ *Имя и возраст успешно обновлены!*",
            parse_mode=ParseMode.MARKDOWN
        )
        
        reply_markup = get_edit_profile_keyboard()
        await update.message.reply_text(
            "✏️ *РЕДАКТИРОВАНИЕ ПРОФИЛЯ*\n\n"
            "Выбери, что хочешь изменить:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
        
        return States.EDIT_PROFILE
        
    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ *Неверный формат!*\n\n"
            "Пожалуйста, введи в формате: *Имя Возраст*\n"
            "Пример: *Анна 24* или *Иван Петров 30*",
            parse_mode=ParseMode.MARKDOWN
        )
        return States.EDIT_NAME_AGE

async def handle_edit_bio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка редактирования информации о себе"""
    await update.message.reply_text(
        "📝 *ИЗМЕНЕНИЕ ИНФОРМАЦИИ О СЕБЕ*\n\n"
        "Расскажи коротко о себе:\n\n"
        "⚡️ *Примеры:*\n"
        "• Люблю путешествия, кино и кофе\n"
        "• IT-специалист, увлекаюсь спортом\n"
        "• Ищу интересного собеседника\n\n"
        "📍 Пиши кратко, но информативно (до 500 символов)",
        parse_mode=ParseMode.MARKDOWN
    )
    
    return States.EDIT_BIO

async def handle_edit_bio_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ввода новой информации о себе"""
    bio = update.message.text.strip()
    
    if len(bio) > MAX_BIO_LENGTH:
        await update.message.reply_text(f"❌ Слишком длинно! Максимум {MAX_BIO_LENGTH} символов.")
        return States.EDIT_BIO
    
    
    db.update_user(update.effective_user.id, {
        'bio': bio
    })
    
    await update.message.reply_text(
        "✅ *Информация о себе успешно обновлена!*",
        parse_mode=ParseMode.MARKDOWN
    )
    
    reply_markup = get_edit_profile_keyboard()
    await update.message.reply_text(
        "✏️ *РЕДАКТИРОВАНИЕ ПРОФИЛЯ*\n\n"
        "Выбери, что хочешь изменить:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )
    
    return States.EDIT_PROFILE

async def handle_edit_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка редактирования фото"""
    await update.message.reply_text(
        "📸 *ИЗМЕНЕНИЕ ФОТО*\n\n"
        "Отправь новое фото (лицо должно быть хорошо видно):",
        parse_mode=ParseMode.MARKDOWN
    )
    
    return States.EDIT_PHOTO

async def handle_edit_photo_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нового фото"""
    if not update.message.photo:
        await update.message.reply_text("📸 Пожалуйста, отправь фото.")
        return States.EDIT_PHOTO
    
    
    photo_file = await update.message.photo[-1].get_file()
    
    
    user = db.get_user_by_telegram_id(update.effective_user.id)
    if user:
        photos = [photo_file.file_id]  
    else:
        photos = [photo_file.file_id]
    
    
    db.update_user(update.effective_user.id, {
        'photos': photos
    })
    
    await update.message.reply_text(
        "✅ *Фото успешно обновлено!*",
        parse_mode=ParseMode.MARKDOWN
    )
    
    reply_markup = get_edit_profile_keyboard()
    await update.message.reply_text(
        "✏️ *РЕДАКТИРОВАНИЕ ПРОФИЛЯ*\n\n"
        "Выбери, что хочешь изменить:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )
    
    return States.EDIT_PROFILE

async def handle_edit_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка редактирования города"""
    reply_markup = ReplyKeyboardMarkup([
        ["📍 Отправить геолокацию"],
        ["🏙️ Ввести вручную"]
    ], resize_keyboard=True, one_time_keyboard=True)
    
    await update.message.reply_text(
        "📍 *ИЗМЕНЕНИЕ ГОРОДА*\n\n"
        "Отправь свой город или геолокацию:\n\n"
        "⚡️ Можно отправить геолокацию кнопкой ниже\n"
        "📍 Или просто напиши название города",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )
    
    return States.EDIT_CITY

async def handle_edit_city_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ввода нового города"""
    city = None
    updates = {}
    
    if update.message.text == "📍 Отправить геолокацию":
        await update.message.reply_text(
            "📍 Нажми на скрепку 📎 и выбери 'Геопозиция'",
            reply_markup=ReplyKeyboardRemove()
        )
        return States.EDIT_CITY
    elif update.message.text == "🏙️ Ввести вручную":
        await update.message.reply_text(
            "🏙️ Напиши название своего города:",
            reply_markup=ReplyKeyboardRemove()
        )
        return States.EDIT_CITY
    elif update.message.text:
        city = update.message.text.strip()
        updates['city'] = city
    elif update.message.location:
        
        latitude = update.message.location.latitude
        longitude = update.message.location.longitude
        updates['latitude'] = latitude
        updates['longitude'] = longitude
        updates['city'] = "Город по геолокации"
    
    if not updates:
        await update.message.reply_text("Пожалуйста, отправь название города или геолокацию.")
        return States.EDIT_CITY
    
    
    db.update_user(update.effective_user.id, updates)
    
    await update.message.reply_text(
        "✅ *Город успешно обновлен!*",
        parse_mode=ParseMode.MARKDOWN
    )
    
    reply_markup = get_edit_profile_keyboard()
    await update.message.reply_text(
        "✏️ *РЕДАКТИРОВАНИЕ ПРОФИЛЯ*\n\n"
        "Выбери, что хочешь изменить:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )
    
    return States.EDIT_PROFILE

async def show_who_liked_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать, кто лайкнул меня"""
    user = db.get_user_by_telegram_id(update.effective_user.id)
    if not user:
        await update.message.reply_text("❌ Сначала зарегистрируйся: /start")
        return
    
    
    users_who_liked_me = db.get_users_who_liked_me(user['telegram_id'])
    
    if not users_who_liked_me:
        await update.message.reply_text(
            "💔 *ПОКА НИКТО ТЕБЯ НЕ ЛАЙКНУЛ*\n\n"
            "⚡️ *Советы:*\n"
            "• Добавь качественное фото\n"
            "• Заполни информацию о себе\n"
            "• Будь активнее - лайкай других\n"
            "• Прояви себя интересным собеседником",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    
    text = f"❤️ *ТЕБЯ ЛАЙКНУЛИ: {len(users_who_liked_me)} ЧЕЛ.*\n\n"
    
    for i, profile in enumerate(users_who_liked_me[:10], 1):
        username_link = f"@{profile['username']}" if profile.get('username') else f"tg://user?id={profile['telegram_id']}"
        text += f"{i}. *{profile['full_name']}, {profile['age']}*\n"
        if profile['city']:
            text += f"📍 {profile['city']}\n"
        if profile['bio']:
            bio_preview = profile['bio'][:50] + "..." if len(profile['bio']) > 50 else profile['bio']
            text += f"📝 {bio_preview}\n"
        text += f"💬 {username_link}\n\n"
    
    if len(users_who_liked_me) > 10:
        text += f"... и еще {len(users_who_liked_me) - 10} чел.\n\n"
    
    text += "⚡️ *Как начать общение:*\n"
    text += "1. Перейди в '👀 Смотреть анкеты'\n"
    text += "2. Лайкни человека взаимно\n"
    text += "3. При взаимном лайке сможете общаться!\n"
    
    
    reply_markup = get_back_to_profile_keyboard()
    
    if users_who_liked_me and users_who_liked_me[0]['photos']:
        photo = users_who_liked_me[0]['photos'][0]
        
        await update.message.reply_photo(
            photo=photo,
            caption=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )

async def main_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат в главное меню"""
    user = db.get_user_by_telegram_id(update.effective_user.id)
    
    reply_markup = get_quick_actions_keyboard()
    
    if user:
        
        users_who_liked_me = db.get_users_who_liked_me(user['telegram_id'])
        
        welcome_text = f"🔥 *ГЛАВНОЕ МЕНЮ*\n\n"
        welcome_text += f"Привет, {user['full_name'] or 'друг'}!\n\n"
        welcome_text += f"⚡️ *Статус:* БАЗОВЫЙ\n"
        welcome_text += f"❤️ Лайков сегодня: {user.get('likes_today', 0)}/{LIKES_PER_DAY_FREE}\n"
        welcome_text += f"💌 Тебя лайкнули: {len(users_who_liked_me)} чел.\n\n"
        welcome_text += f"🎯 *Что делаем?*"
        
        await update.message.reply_text(
            welcome_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            "🔥 *Добро пожаловать в РЯДОМ!*\n\n"
            "Знакомства рядом с тобой • Быстро • Безопасно • Интересно\n\n"
            "📝 Начни регистрацию прямо сейчас!\n\n"
            "Напиши /start для начала регистрации"
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /help"""
    help_text = """
    🤖 *БОТ ДЛЯ ЗНАКОМСТВ «РЯДОМ»*

    ⚡️ *ОСНОВНЫЕ КОМАНДЫ:*
    /start - Главное меню
    /profile - Мой профиль
    /browse - Начать просмотр анкет
    /help - Эта справка

    🎯 *КАК ЭТО РАБОТАЕТ?*
    1. 📝 Заполни профиль (/start)
    2. 👀 Смотри анкеты и ставь ❤️
    3. 🔥 При взаимной симпатии можете начать общение!
    4. 💌 Смотри кто тебя лайкнул в разделе "Кто меня лайкнул"

    ⭐ *ПРЕИМУЩЕСТВА:*
    • 📍 Геолокационный поиск
    • ⚡️ Быстрые знакомства
    • ✏️ Редактирование профиля
    • 💌 Уведомления о лайках

    ⚠️ *ПРАВИЛА:*
    • 🙏 Будь вежлив и уважителен
    • 🚫 Не спамь
    • 🔒 Не передавай личные данные сразу
    • 📢 Сообщай о нарушениях

    📞 *ПОДДЕРЖКА:* @w33RY
    """
    
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


async def handle_quick_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка быстрых кнопок"""
    text = update.message.text
    
    if text == "👀 Смотреть анкеты":
        await browse_profiles_command(update, context)
    elif text == "📊 Мой профиль":
        await profile_command(update, context)
    elif text == "❤️ Кто меня лайкнул":
        await show_who_liked_me(update, context)
    elif text == "🆘 Помощь":
        await help_command(update, context)
    elif text == "❤️ Лайк":
        await handle_like_action(update, context)
    elif text == "➡️ Дальше":
        await handle_next_action(update, context)
    elif text == "🚫 Пожаловаться":
        await handle_report_action(update, context)
    elif text == "🔙 В меню":
        await update.message.reply_text("⚡️ Возвращаю в меню...", reply_markup=get_quick_actions_keyboard())
        await main_menu_command(update, context)
    elif text == "🔙 Назад в меню":
        await update.message.reply_text("⚡️ Возвращаю в меню...", reply_markup=get_quick_actions_keyboard())
        await main_menu_command(update, context)
    elif text == "✏️ Редактировать профиль":
        await start_edit_profile(update, context)
    elif text == "✏️ Имя и возраст":
        await handle_edit_name_age(update, context)
    elif text == "📝 О себе":
        await handle_edit_bio(update, context)
    elif text == "📸 Фото":
        await handle_edit_photo(update, context)
    elif text == "📍 Город":
        await handle_edit_city(update, context)
    elif text == "🔙 К моему профилю":
        await profile_command(update, context)
    else:
        
        if context.user_data.get('reporting'):
            await handle_report_text(update, context)
        else:
            
            await update.message.reply_text(
                "Используй кнопки меню для навигации или команды:\n"
                "/start - Главное меню\n"
                "/help - Помощь"
            )


def main():
    """Запуск бота"""
    
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    
    
    if BOT_TOKEN == "ВСТАВЬТЕ_ВАШ_ТОКЕН_ЗДЕСЬ":
        print("❌ ОШИБКА: Вы не указали токен бота!")
        print("📝 Получите токен у @BotFather в Telegram")
        print("🔧 Замените строку: BOT_TOKEN = 'ВСТАВЬТЕ_ВАШ_ТОКЕН_ЗДЕСЬ'")
        print("   на ваш реальный токен, например:")
        print("   BOT_TOKEN = '8524498297:AAE07uhhKek7jg7gwNyMeGHA_oDJCgWXvns'")
        return
    
    
    application = Application.builder().token(BOT_TOKEN).build()
    
   
    application.add_error_handler(error_handler)
    
    
    registration_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_command)
        ],
        states={
            States.REG_PHOTO: [
                MessageHandler(filters.PHOTO, handle_registration_photo)
            ],
            States.REG_NAME_AGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_registration_name_age)
            ],
            States.REG_GENDER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_registration_gender)
            ],
            States.REG_CITY: [
                MessageHandler(filters.TEXT | filters.LOCATION, handle_registration_city)
            ],
            States.REG_BIO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_registration_bio)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )
    
    
    edit_profile_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & filters.Regex("^✏️ Редактировать профиль$"), start_edit_profile)
        ],
        states={
            States.EDIT_PROFILE: [
                MessageHandler(filters.TEXT & filters.Regex("^✏️ Имя и возраст$"), handle_edit_name_age),
                MessageHandler(filters.TEXT & filters.Regex("^📝 О себе$"), handle_edit_bio),
                MessageHandler(filters.TEXT & filters.Regex("^📸 Фото$"), handle_edit_photo),
                MessageHandler(filters.TEXT & filters.Regex("^📍 Город$"), handle_edit_city),
                MessageHandler(filters.TEXT & filters.Regex("^🔙 К моему профилю$"), profile_command),
            ],
            States.EDIT_NAME_AGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_name_age_input)
            ],
            States.EDIT_BIO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_bio_input)
            ],
            States.EDIT_PHOTO: [
                MessageHandler(filters.PHOTO, handle_edit_photo_input)
            ],
            States.EDIT_CITY: [
                MessageHandler(filters.TEXT | filters.LOCATION, handle_edit_city_input)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )
    
    
    application.add_handler(registration_handler)
    application.add_handler(edit_profile_handler)
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("profile", profile_command))
    application.add_handler(CommandHandler("browse", browse_profiles_command))
    application.add_handler(CommandHandler("start", main_menu_command))
    
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_quick_buttons))
    
    
    print("БОТ ЗАПУЩЕН")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
