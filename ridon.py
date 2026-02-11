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

from telegram import (
    Update, 
    ReplyKeyboardRemove,
    KeyboardButton,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler
)
from telegram.constants import ParseMode


BOT_TOKEN = ""


ADMIN_IDS = []
DB_PATH = ""

MAX_PHOTOS = 3
MAX_BIO_LENGTH = 500
DEFAULT_SEARCH_RADIUS_KM = 50
CHAT_DURATION_HOURS = 24
LIKES_PER_DAY_FREE = 20

class States:
    REG_PHOTO = 1
    REG_NAME = 2
    REG_AGE = 3
    REG_GENDER = 4
    REG_CITY = 5
    REG_INTERESTS = 6
    REG_BIO = 7
    EDIT_PROFILE = 8
    EDIT_NAME = 9
    EDIT_AGE = 10
    EDIT_BIO = 11
    EDIT_PHOTO = 12
    EDIT_CITY = 13
    EDIT_INTERESTS = 14
    SEARCH_SETTINGS = 15

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
            # Основная таблица пользователей
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    full_name TEXT,
                    age INTEGER,
                    gender TEXT CHECK(gender IN ('male', 'female', 'other')),
                    city TEXT,
                    latitude REAL,
                    longitude REAL,
                    bio TEXT,
                    interests TEXT,  -- JSON список интересов
                    search_gender TEXT,  -- Кого ищет
                    search_min_age INTEGER DEFAULT 18,
                    search_max_age INTEGER DEFAULT 100,
                    profile_photos TEXT,  -- JSON список file_id
                    is_active BOOLEAN DEFAULT 1,
                    is_banned BOOLEAN DEFAULT 0,
                    is_premium BOOLEAN DEFAULT 0,
                    likes_given_today INTEGER DEFAULT 0,
                    likes_received_total INTEGER DEFAULT 0,
                    last_like_reset_date TEXT,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Таблица лайков
            conn.execute("""
                CREATE TABLE IF NOT EXISTS likes (
                    like_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_user_id INTEGER NOT NULL,
                    to_user_id INTEGER NOT NULL,
                    is_mutual BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (from_user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                    FOREIGN KEY (to_user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                    UNIQUE(from_user_id, to_user_id)
                )
            """)
            
            # Таблица жалоб
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reports (
                    report_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reporter_id INTEGER NOT NULL,
                    reported_user_id INTEGER NOT NULL,
                    reason TEXT,
                    status TEXT CHECK(status IN ('pending', 'reviewed', 'resolved', 'dismissed')) DEFAULT 'pending',
                    admin_notes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (reporter_id) REFERENCES users(user_id) ON DELETE CASCADE,
                    FOREIGN KEY (reported_user_id) REFERENCES users(user_id) ON DELETE CASCADE
                )
            """)
            
            # Таблица просмотров профилей
            conn.execute("""
                CREATE TABLE IF NOT EXISTS profile_views (
                    view_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    viewer_id INTEGER NOT NULL,
                    viewed_user_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (viewer_id) REFERENCES users(user_id) ON DELETE CASCADE,
                    FOREIGN KEY (viewed_user_id) REFERENCES users(user_id) ON DELETE CASCADE
                )
            """)
            
            # Таблица чатов
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user1_id INTEGER NOT NULL,
                    user2_id INTEGER NOT NULL,
                    is_active BOOLEAN DEFAULT 1,
                    last_message_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user1_id) REFERENCES users(user_id) ON DELETE CASCADE,
                    FOREIGN KEY (user2_id) REFERENCES users(user_id) ON DELETE CASCADE,
                    UNIQUE(user1_id, user2_id)
                )
            """)
            
            # Таблица сообщений администратора
            conn.execute("""
                CREATE TABLE IF NOT EXISTS admin_messages (
                    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id INTEGER NOT NULL,
                    user_id INTEGER,
                    message_text TEXT,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (admin_id) REFERENCES users(user_id),
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE SET NULL
                )
            """)
            
            # Создаем индексы
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_telegram ON users(telegram_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active) WHERE is_active = 1")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_gender ON users(gender)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_city ON users(city)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_age ON users(age)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_likes_from_to ON likes(from_user_id, to_user_id)")
            

    
    def get_user_by_telegram_id(self, telegram_id: int) -> Optional[Dict]:
        """Получить пользователя по telegram_id"""
        with self.get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM users 
                WHERE telegram_id = ?
            """, (telegram_id,))
            row = cursor.fetchone()
            if row:
                user = dict(row)
                # Десериализация
                for field in ['profile_photos', 'interests']:
                    if user[field]:
                        try:
                            user[field] = json.loads(user[field])
                        except:
                            user[field] = []
                    else:
                        user[field] = []
                return user
            return None
    
    def get_next_profile(self, current_user_telegram_id: int) -> Optional[Dict]:
        """Получить следующую анкету с учетом настроек поиска"""
        with self.get_connection() as conn:
            current_user = self.get_user_by_telegram_id(current_user_telegram_id)
            if not current_user:
                return None
            
            # Получаем настройки поиска текущего пользователя
            search_gender = current_user.get('search_gender')
            search_min_age = current_user.get('search_min_age', 18)
            search_max_age = current_user.get('search_max_age', 100)
            
            # Базавый запрос
            query = """
                SELECT u.* 
                FROM users u
                WHERE u.telegram_id != ?
                AND u.is_active = 1
                AND u.is_banned = 0
                AND u.age BETWEEN ? AND ?
                AND NOT EXISTS (
                    SELECT 1 FROM likes l 
                    WHERE l.from_user_id = ? AND l.to_user_id = u.user_id
                )
            """
            params = [current_user_telegram_id, search_min_age, search_max_age, current_user['user_id']]
            
            # Добавляем фильтр по полу если указан
            if search_gender and search_gender != 'all':
                query += " AND u.gender = ?"
                params.append(search_gender)
            
            # Добавляем фильтр по интересам если есть
            if current_user.get('interests'):
                interests = current_user['interests']
                if interests:
                    # Поиск по совпадению интересов (хотя бы один)
                    query += """
                        AND (u.interests IS NOT NULL AND (
                    """
                    for i, interest in enumerate(interests):
                        if i > 0:
                            query += " OR "
                        query += "u.interests LIKE ?"
                        params.append(f'%"{interest}"%')
                    query += "))"
            
            query += " ORDER BY RANDOM() LIMIT 1"
            
            cursor = conn.execute(query, params)
            row = cursor.fetchone()
            
            if row:
                profile = dict(row)
                for field in ['profile_photos', 'interests']:
                    if profile[field]:
                        try:
                            profile[field] = json.loads(profile[field])
                        except:
                            profile[field] = []
                    else:
                        profile[field] = []
                
                # Записываем просмотр
                self.record_profile_view(current_user['user_id'], profile['user_id'])
                
                return profile
            
            return None
    
    def search_by_interests(self, current_user_telegram_id: int, interests: List[str]) -> List[Dict]:
        """Поиск пользователей по интересам"""
        with self.get_connection() as conn:
            current_user = self.get_user_by_telegram_id(current_user_telegram_id)
            if not current_user:
                return []
            
            query = """
                SELECT u.* 
                FROM users u
                WHERE u.telegram_id != ?
                AND u.is_active = 1
                AND u.is_banned = 0
                AND u.interests IS NOT NULL
                AND (
            """
            params = [current_user_telegram_id]
            
            for i, interest in enumerate(interests):
                if i > 0:
                    query += " OR "
                query += "u.interests LIKE ?"
                params.append(f'%"{interest}"%')
            
            query += ") ORDER BY RANDOM() LIMIT 10"
            
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
            
            profiles = []
            for row in rows:
                profile = dict(row)
                for field in ['profile_photos', 'interests']:
                    if profile[field]:
                        try:
                            profile[field] = json.loads(profile[field])
                        except:
                            profile[field] = []
                    else:
                        profile[field] = []
                profiles.append(profile)
            
            return profiles
    
    def update_user(self, telegram_id: int, updates: Dict) -> bool:
        """Обновить данные пользователя"""
        with self.get_connection() as conn:
            data_to_update = updates.copy()
            
            # Сериализация списков
            for field in ['profile_photos', 'interests']:
                if field in data_to_update and isinstance(data_to_update[field], list):
                    data_to_update[field] = json.dumps(data_to_update[field], ensure_ascii=False)
            
            data_to_update['updated_at'] = datetime.now().isoformat()
            
            set_clause = ', '.join([f"{key} = ?" for key in data_to_update.keys()])
            values = list(data_to_update.values()) + [telegram_id]
            
            sql = f"UPDATE users SET {set_clause} WHERE telegram_id = ?"
            cursor = conn.execute(sql, values)
            return cursor.rowcount > 0
    
    def create_user(self, user_data: Dict) -> Optional[Dict]:
        """Создать нового пользователя"""
        with self.get_connection() as conn:
            data_to_insert = user_data.copy()
            
            # Сериализация
            for field in ['profile_photos', 'interests']:
                if field in data_to_insert and isinstance(data_to_insert[field], list):
                    data_to_insert[field] = json.dumps(data_to_insert[field], ensure_ascii=False)
            
            now = datetime.now().isoformat()
            data_to_insert['created_at'] = now
            data_to_insert['updated_at'] = now
            data_to_insert['last_seen'] = now
            
            fields = list(data_to_insert.keys())
            placeholders = ['?' for _ in fields]
            
            sql = f"""
                INSERT INTO users ({', '.join(fields)})
                VALUES ({', '.join(placeholders)})
            """
            
            try:
                cursor = conn.execute(sql, list(data_to_insert.values()))
                user_id = cursor.lastrowid
                
                return self.get_user_by_id(user_id)
            except Exception as e:
                logging.error(f"Error creating user: {e}")
                return None
    
    def get_user_by_id(self, user_id: int) -> Optional[Dict]:
        """Получить пользователя по ID"""
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            if row:
                user = dict(row)
                for field in ['profile_photos', 'interests']:
                    if user[field]:
                        try:
                            user[field] = json.loads(user[field])
                        except:
                            user[field] = []
                    else:
                        user[field] = []
                return user
            return None
    
    def get_users_who_liked_me(self, telegram_id: int) -> List[Dict]:
        """Получить список пользователей, которые лайкнули меня"""
        with self.get_connection() as conn:
            user = self.get_user_by_telegram_id(telegram_id)
            if not user:
                return []
            
            query = """
                SELECT u.* 
                FROM users u
                JOIN likes l ON l.from_user_id = u.user_id
                WHERE l.to_user_id = ?
                AND u.is_active = 1
                AND u.is_banned = 0
                ORDER BY l.created_at DESC
            """
            
            cursor = conn.execute(query, (user['user_id'],))
            rows = cursor.fetchall()
            
            profiles = []
            for row in rows:
                profile = dict(row)
                for field in ['profile_photos', 'interests']:
                    if profile[field]:
                        try:
                            profile[field] = json.loads(profile[field])
                        except:
                            profile[field] = []
                    else:
                        profile[field] = []
                profiles.append(profile)
            
            return profiles
    
    def create_like(self, from_user_telegram_id: int, to_user_telegram_id: int) -> Tuple[bool, Optional[Dict]]:
        """Создать лайк"""
        with self.get_connection() as conn:
            from_user = self.get_user_by_telegram_id(from_user_telegram_id)
            to_user = self.get_user_by_telegram_id(to_user_telegram_id)
            
            if not from_user or not to_user:
                return False, None
            
            # Проверяем лимит
            today = datetime.now().strftime("%Y-%m-%d")
            if from_user.get('last_like_reset_date') != today:
                conn.execute("""
                    UPDATE users 
                    SET likes_given_today = 0, last_like_reset_date = ?
                    WHERE telegram_id = ?
                """, (today, from_user_telegram_id))
                from_user['likes_given_today'] = 0
            
            if from_user.get('likes_given_today', 0) >= LIKES_PER_DAY_FREE and not from_user.get('is_premium', False):
                return False, None
            
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO likes (from_user_id, to_user_id)
                    VALUES (?, ?)
                """, (from_user['user_id'], to_user['user_id']))
                
                conn.execute("""
                    UPDATE users 
                    SET likes_given_today = likes_given_today + 1,
                        updated_at = ?
                    WHERE telegram_id = ?
                """, (datetime.now().isoformat(), from_user_telegram_id))
                
                # Проверяем взаимность
                cursor = conn.execute("""
                    SELECT 1 FROM likes 
                    WHERE from_user_id = ? AND to_user_id = ?
                """, (to_user['user_id'], from_user['user_id']))
                
                is_mutual = cursor.fetchone() is not None
                
                if is_mutual:
                    conn.execute("""
                        UPDATE likes 
                        SET is_mutual = 1 
                        WHERE (from_user_id = ? AND to_user_id = ?)
                           OR (from_user_id = ? AND to_user_id = ?)
                    """, (from_user['user_id'], to_user['user_id'], 
                          to_user['user_id'], from_user['user_id']))
                
                return is_mutual, to_user
                
            except Exception as e:
                logging.error(f"Error creating like: {e}")
                return False, None
    
    def record_profile_view(self, viewer_id: int, viewed_user_id: int):
        """Записать просмотр профиля"""
        with self.get_connection() as conn:
            try:
                conn.execute("""
                    INSERT INTO profile_views (viewer_id, viewed_user_id)
                    VALUES (?, ?)
                """, (viewer_id, viewed_user_id))
            except:
                pass
    
    def get_user_stats(self, telegram_id: int) -> Dict:
        """Получить статистику пользователя"""
        with self.get_connection() as conn:
            user = self.get_user_by_telegram_id(telegram_id)
            if not user:
                return {}
            
            cursor = conn.execute("SELECT COUNT(*) as likes_given FROM likes WHERE from_user_id = ?", (user['user_id'],))
            likes_given = cursor.fetchone()['likes_given']
            
            cursor = conn.execute("SELECT COUNT(*) as likes_received FROM likes WHERE to_user_id = ?", (user['user_id'],))
            likes_received = cursor.fetchone()['likes_received']
            
            cursor = conn.execute("SELECT COUNT(*) as mutual_likes FROM likes WHERE (from_user_id = ? OR to_user_id = ?) AND is_mutual = 1", 
                                 (user['user_id'], user['user_id']))
            mutual_likes = cursor.fetchone()['mutual_likes']
            
            cursor = conn.execute("SELECT COUNT(*) as profile_views FROM profile_views WHERE viewed_user_id = ?", (user['user_id'],))
            profile_views = cursor.fetchone()['profile_views']
            
            return {
                'likes_given': likes_given,
                'likes_received': likes_received,
                'mutual_likes': mutual_likes,
                'profile_views': profile_views,
                'likes_given_today': user.get('likes_given_today', 0),
                'likes_received_total': user.get('likes_received_total', 0)
            }
    
    def reset_daily_likes_if_needed(self, telegram_id: int):
        """Сбросить счетчик лайков"""
        with self.get_connection() as conn:
            user = self.get_user_by_telegram_id(telegram_id)
            if not user:
                return
            
            today = datetime.now().strftime("%Y-%m-%d")
            if user.get('last_like_reset_date') != today:
                conn.execute("""
                    UPDATE users 
                    SET likes_given_today = 0, last_like_reset_date = ?
                    WHERE telegram_id = ?
                """, (today, telegram_id))
    
    def update_last_seen(self, telegram_id: int):
        """Обновить время последней активности"""
        with self.get_connection() as conn:
            conn.execute("""
                UPDATE users 
                SET last_seen = ?, updated_at = ?
                WHERE telegram_id = ?
            """, (datetime.now().isoformat(), datetime.now().isoformat(), telegram_id))
    
    def delete_user(self, telegram_id: int) -> bool:
        """Удалить пользователя"""
        with self.get_connection() as conn:
            cursor = conn.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
            return cursor.rowcount > 0
    
    def ban_user(self, telegram_id: int) -> bool:
        """Забанить пользователя"""
        with self.get_connection() as conn:
            cursor = conn.execute("""
                UPDATE users 
                SET is_banned = 1, is_active = 0, updated_at = ?
                WHERE telegram_id = ?
            """, (datetime.now().isoformat(), telegram_id))
            return cursor.rowcount > 0
    
    def unban_user(self, telegram_id: int) -> bool:
        """Разбанить пользователя"""
        with self.get_connection() as conn:
            cursor = conn.execute("""
                UPDATE users 
                SET is_banned = 0, is_active = 1, updated_at = ?
                WHERE telegram_id = ?
            """, (datetime.now().isoformat(), telegram_id))
            return cursor.rowcount > 0
    
    def get_all_users(self, limit: int = 100) -> List[Dict]:
        """Получить всех пользователей"""
        with self.get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM users 
                ORDER BY created_at DESC 
                LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
            users = []
            for row in rows:
                user = dict(row)
                for field in ['profile_photos', 'interests']:
                    if user[field]:
                        try:
                            user[field] = json.loads(user[field])
                        except:
                            user[field] = []
                    else:
                        user[field] = []
                users.append(user)
            return users
    
    def get_user_count(self) -> Dict:
        """Получить статистику пользователей"""
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) as total FROM users")
            total = cursor.fetchone()['total']
            
            cursor = conn.execute("SELECT COUNT(*) as active FROM users WHERE is_active = 1")
            active = cursor.fetchone()['active']
            
            cursor = conn.execute("SELECT COUNT(*) as banned FROM users WHERE is_banned = 1")
            banned = cursor.fetchone()['banned']
            
            cursor = conn.execute("SELECT COUNT(*) as today FROM users WHERE DATE(created_at) = DATE('now')")
            today = cursor.fetchone()['today']
            
            return {'total': total, 'active': active, 'banned': banned, 'today': today}
    
    def get_pending_reports(self) -> List[Dict]:
        """Получить необработанные жалобы"""
        with self.get_connection() as conn:
            cursor = conn.execute("""
                SELECT r.*, 
                       u1.username as reporter_username,
                       u1.full_name as reporter_name,
                       u2.username as reported_username,
                       u2.full_name as reported_name
                FROM reports r
                JOIN users u1 ON r.reporter_id = u1.user_id
                JOIN users u2 ON r.reported_user_id = u2.user_id
                WHERE r.status = 'pending'
                ORDER BY r.created_at DESC
                LIMIT 50
            """)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    
    def create_report(self, reporter_telegram_id: int, reported_user_telegram_id: int, reason: str) -> bool:
        """Создать жалобу"""
        with self.get_connection() as conn:
            reporter = self.get_user_by_telegram_id(reporter_telegram_id)
            reported = self.get_user_by_telegram_id(reported_user_telegram_id)
            
            if not reporter or not reported:
                return False
            
            try:
                conn.execute("""
                    INSERT INTO reports (reporter_id, reported_user_id, reason)
                    VALUES (?, ?, ?)
                """, (reporter['user_id'], reported['user_id'], reason))
                return True
            except:
                return False
    
    def update_report_status(self, report_id: int, status: str, admin_notes: str = None) -> bool:
        """Обновить статус жалобы"""
        with self.get_connection() as conn:
            cursor = conn.execute("""
                UPDATE reports 
                SET status = ?, admin_notes = ?
                WHERE report_id = ?
            """, (status, admin_notes, report_id))
            return cursor.rowcount > 0
    
    def search_users(self, search_term: str) -> List[Dict]:
        """Поиск пользователей"""
        with self.get_connection() as conn:
            try:
                telegram_id = int(search_term)
                cursor = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            except ValueError:
                cursor = conn.execute("""
                    SELECT * FROM users 
                    WHERE full_name LIKE ? OR username LIKE ?
                    LIMIT 20
                """, (f"%{search_term}%", f"%{search_term}%"))
            
            rows = cursor.fetchall()
            users = []
            for row in rows:
                user = dict(row)
                for field in ['profile_photos', 'interests']:
                    if user[field]:
                        try:
                            user[field] = json.loads(user[field])
                        except:
                            user[field] = []
                    else:
                        user[field] = []
                users.append(user)
            return users
    
    def get_user_profile_completion(self, telegram_id: int) -> Dict:
        """Получить процент заполнения профиля"""
        user = self.get_user_by_telegram_id(telegram_id)
        if not user:
            return {'percentage': 0, 'missing_fields': []}
        
        fields = {
            'profile_photos': bool(user.get('profile_photos')),
            'full_name': bool(user.get('full_name')),
            'age': bool(user.get('age')),
            'gender': bool(user.get('gender')),
            'city': bool(user.get('city')),
            'interests': bool(user.get('interests')),
            'bio': bool(user.get('bio'))
        }
        
        filled_count = sum(1 for field in fields.values() if field)
        total_count = len(fields)
        percentage = int((filled_count / total_count) * 100)
        
        missing_fields = [field for field, filled in fields.items() if not filled]
        
        return {
            'percentage': percentage,
            'missing_fields': missing_fields,
            'filled_count': filled_count,
            'total_count': total_count
        }

db = Database(DB_PATH)


def get_quick_actions_keyboard():
    """Быстрые кнопки"""
    return ReplyKeyboardMarkup([
        ["👀 Смотреть анкеты", "🔍 Поиск по интересам"],
        ["⚙️ Настройки поиска", "📊 Мой профиль"],
        ["❤️ Кто меня лайкнул", "🆘 Помощь"]
    ], resize_keyboard=True)

def get_profile_quick_actions():
    """Кнопки профиля"""
    return ReplyKeyboardMarkup([
        ["✏️ Редактировать профиль", "⚙️ Настройки поиска"],
        ["🗑️ Удалить анкету", "🔙 Главное меню"]
    ], resize_keyboard=True)

def get_browse_quick_actions():
    """Кнопки просмотра"""
    return ReplyKeyboardMarkup([
        ["❤️ Лайк", "➡️ Дальше"],
        ["🚫 Пожаловаться", "🔍 Поиск по интересам"],
        ["🔙 Главное меню"]
    ], resize_keyboard=True)

def get_gender_keyboard():
    """Выбор пола"""
    return ReplyKeyboardMarkup([
        ["👨 Мужчина", "👩 Женщина"],
        ["🔙 Назад"]
    ], resize_keyboard=True)

def get_search_gender_keyboard():
    """Выбор пола для поиска"""
    return ReplyKeyboardMarkup([
        ["👨 Мужчин", "👩 Женщин"],
        ["👫 Всех", "🔙 Назад"]
    ], resize_keyboard=True)

def get_search_settings_keyboard():
    """Настройки поиска"""
    return ReplyKeyboardMarkup([
        ["👫 Кого искать", "🎯 Возраст"],
        ["🔙 Главное меню"]
    ], resize_keyboard=True)

def get_interests_keyboard():
    """Интересы"""
    interests = [
        "🎵 Музыка", "🎬 Кино", "📚 Книги", "🏀 Спорт",
        "✈️ Путешествия", "🍳 Кулинария", "🎨 Искусство",
        "💻 IT", "🐶 Животные", "🌿 Природа", "🎮 Игры",
        "🏋️ Фитнес", "📸 Фото", "🎤 Пение", "💃 Танцы"
    ]
    
    keyboard = []
    for i in range(0, len(interests), 3):
        keyboard.append(interests[i:i+3])
    keyboard.append(["✅ Готово", "🔙 Назад"])
    
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_edit_profile_keyboard():
    """Редактирование профиля"""
    return ReplyKeyboardMarkup([
        ["✏️ Имя", "🎯 Возраст", "📍 Город"],
        ["📸 Фото", "❤️ Интересы", "📝 О себе"],
        ["🔙 К профилю"]
    ], resize_keyboard=True)

def get_admin_keyboard():
    """Админ-панель"""
    return ReplyKeyboardMarkup([
        ["📊 Статистика", "👥 Все пользователи"],
        ["⚠️ Жалобы", "🔍 Найти пользователя"],
        ["🚫 Забанить", "📨 Отправить сообщение"],
        ["🔙 Главное меню"]
    ], resize_keyboard=True)

def get_admin_back_keyboard():
    """Кнопка возврата в админ-меню"""
    return ReplyKeyboardMarkup([
        ["🔙 В админ-меню"]
    ], resize_keyboard=True)

def get_back_to_profile_keyboard():
    """Кнопка возврата к профилю"""
    return ReplyKeyboardMarkup([
        ["🔙 К профилю"]
    ], resize_keyboard=True)

def get_confirm_delete_keyboard():
    """Подтверждение удаления"""
    return ReplyKeyboardMarkup([
        ["✅ Да, удалить", "❌ Нет, отменить"]
    ], resize_keyboard=True)

def is_admin(telegram_id: int) -> bool:
    """Проверка администратора"""
    return telegram_id in ADMIN_IDS


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик /start"""
    user = update.effective_user
    db_user = db.get_user_by_telegram_id(user.id)
    
    if db_user:
        # Если пользователь уже зарегистрирован
        reply_markup = get_quick_actions_keyboard()
        
        await update.message.reply_text(
            f"🔥 *С возвращением, {db_user['full_name'] or user.first_name}!*\n\n"
            "Выбери действие:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        
        db.update_last_seen(user.id)
        return ConversationHandler.END
    else:
        # Начало регистрации
        await update.message.reply_text(
            "🔥 *Добро пожаловать в РЯДОМ!*\n\n"
            "Давай создадим твой профиль. Это займет всего пару минут!\n\n"
            "📸 *ШАГ 1 ИЗ 7: ФОТО*\n\n"
            "Отправь свое фото (лицо должно быть хорошо видно):",
            parse_mode=ParseMode.MARKDOWN
        )
        
        return States.REG_PHOTO


async def handle_registration_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка фото"""
    if not update.message.photo:
        await update.message.reply_text("📸 Пожалуйста, отправь фото.")
        return States.REG_PHOTO
    
    photo_file = await update.message.photo[-1].get_file()
    context.user_data['registration'] = {
        'profile_photos': [photo_file.file_id]
    }
    
    await update.message.reply_text(
        "✅ *Фото принято!*\n\n"
        "👤 *ШАГ 2 ИЗ 7: ИМЯ*\n\n"
        "Как тебя зовут?",
        parse_mode=ParseMode.MARKDOWN
    )
    
    return States.REG_NAME


async def handle_registration_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка имени"""
    name = update.message.text.strip()
    
    if len(name) < 2:
        await update.message.reply_text("❌ Имя должно содержать хотя бы 2 символа.")
        return States.REG_NAME
    
    context.user_data['registration']['full_name'] = name
    
    await update.message.reply_text(
        "✅ *Имя сохранено!*\n\n"
        "🎂 *ШАГ 3 ИЗ 7: ВОЗРАСТ*\n\n"
        "Сколько тебе лет? (от 18 до 100):",
        parse_mode=ParseMode.MARKDOWN
    )
    
    return States.REG_AGE


async def handle_registration_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка возраста"""
    try:
        age = int(update.message.text.strip())
        
        if not 18 <= age <= 100:
            await update.message.reply_text("❌ Возраст должен быть от 18 до 100 лет.")
            return States.REG_AGE
        
        context.user_data['registration']['age'] = age
        
        reply_markup = get_gender_keyboard()
        
        await update.message.reply_text(
            "✅ *Возраст сохранен!*\n\n"
            "👫 *ШАГ 4 ИЗ 7: ПОЛ*\n\n"
            "Выбери свой пол:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
        
        return States.REG_GENDER
        
    except ValueError:
        await update.message.reply_text("❌ Пожалуйста, введи число.")
        return States.REG_AGE


async def handle_registration_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка пола"""
    gender_text = update.message.text
    
    gender_map = {
        '👨 Мужчина': 'male',
        '👩 Женщина': 'female'
    }
    
    if gender_text not in gender_map:
        if gender_text == "🔙 Назад":
            await update.message.reply_text(
                "🎂 *ШАГ 3 ИЗ 7: ВОЗРАСТ*\n\n"
                "Сколько тебе лет? (от 18 до 100):",
                parse_mode=ParseMode.MARKDOWN
            )
            return States.REG_AGE
        reply_markup = get_gender_keyboard()
        await update.message.reply_text("❌ Пожалуйста, выбери пол из предложенных вариантов:", reply_markup=reply_markup)
        return States.REG_GENDER
    
    context.user_data['registration']['gender'] = gender_map[gender_text]
    
    reply_markup = ReplyKeyboardMarkup([
        ["📍 Отправить геолокацию"],
        ["🏙️ Ввести вручную"],
        ["🔙 Назад"]
    ], resize_keyboard=True)
    
    await update.message.reply_text(
        "✅ *Пол сохранен!*\n\n"
        "📍 *ШАГ 5 ИЗ 7: ГОРОД*\n\n"
        "Из какого ты города?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )
    
    return States.REG_CITY


async def handle_registration_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка города"""
    text = update.message.text
    
    if text == "🔙 Назад":
        reply_markup = get_gender_keyboard()
        await update.message.reply_text(
            "👫 *ШАГ 4 ИЗ 7: ПОЛ*\n\n"
            "Выбери свой пол:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
        return States.REG_GENDER
    
    city = None
    updates = {}
    
    if text == "📍 Отправить геолокацию":
        await update.message.reply_text(
            "📍 Нажми на скрепку 📎 и выбери 'Геопозиция'",
            reply_markup=ReplyKeyboardRemove()
        )
        return States.REG_CITY
    elif text == "🏙️ Ввести вручную":
        await update.message.reply_text(
            "🏙️ Напиши название своего города:",
            reply_markup=ReplyKeyboardRemove()
        )
        return States.REG_CITY
    elif text:
        city = text.strip()
        updates['city'] = city
    elif update.message.location:
        latitude = update.message.location.latitude
        longitude = update.message.location.longitude
        updates['latitude'] = latitude
        updates['longitude'] = longitude
        updates['city'] = "Город по геолокации"
    
    if not updates:
        await update.message.reply_text("Пожалуйста, отправь название города или геолокацию.")
        return States.REG_CITY
    
    context.user_data['registration'].update(updates)
    
    reply_markup = get_interests_keyboard()
    
    await update.message.reply_text(
        "✅ *Город сохранен!*\n\n"
        "❤️ *ШАГ 6 ИЗ 7: ИНТЕРЕСЫ*\n\n"
        "Выбери свои интересы (можно несколько):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )
    
    # Инициализируем список интересов
    context.user_data['registration']['interests'] = []
    
    return States.REG_INTERESTS


async def handle_registration_interests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка интересов"""
    text = update.message.text
    
    if text == "🔙 Назад":
        reply_markup = ReplyKeyboardMarkup([
            ["📍 Отправить геолокацию"],
            ["🏙️ Ввести вручную"],
            ["🔙 Назад"]
        ], resize_keyboard=True)
        
        await update.message.reply_text(
            "📍 *ШАГ 5 ИЗ 7: ГОРОД*\n\n"
            "Из какого ты города?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
        return States.REG_CITY
    
    if text == "✅ Готово":
        if not context.user_data['registration'].get('interests'):
            await update.message.reply_text("❌ Выбери хотя бы один интерес.")
            return States.REG_INTERESTS
        
        await update.message.reply_text(
            "✅ *Интересы сохранены!*\n\n"
            "📝 *ШАГ 7 ИЗ 7: О СЕБЕ*\n\n"
            "Расскажи коротко о себе (до 500 символов):\n\n"
            "⚡️ *Пример:*\n"
            "• Люблю путешествия и кино\n"
            "• Работаю в IT\n"
            "• Ищу интересного собеседника",
            parse_mode=ParseMode.MARKDOWN
        )
        
        return States.REG_BIO
    
    # Добавляем/убираем интерес
    interests = context.user_data['registration'].get('interests', [])
    
    # Убираем эмодзи для хранения
    interest_text = text.split(' ', 1)[-1] if ' ' in text else text
    
    if interest_text in interests:
        interests.remove(interest_text)
        await update.message.reply_text(f"❌ {text} - удалено")
    else:
        interests.append(interest_text)
        await update.message.reply_text(f"✅ {text} - добавлено")
    
    context.user_data['registration']['interests'] = interests
    
    # Показываем текущий список
    if interests:
        interests_text = "\n".join([f"• {i}" for i in interests])
        await update.message.reply_text(
            f"📋 *Твои интересы:*\n{interests_text}\n\n"
            f"Выбрано: {len(interests)}\n"
            f"Нажми '✅ Готово' когда закончишь",
            parse_mode=ParseMode.MARKDOWN
        )
    
    return States.REG_INTERESTS


async def handle_registration_bio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка информации о себе"""
    bio = update.message.text.strip()
    
    if len(bio) > MAX_BIO_LENGTH:
        await update.message.reply_text(f"❌ Слишком длинно! Максимум {MAX_BIO_LENGTH} символов.")
        return States.REG_BIO
    
    context.user_data['registration']['bio'] = bio
    
    reg_data = context.user_data['registration']
    user = update.effective_user
    
    user_data = {
        'telegram_id': user.id,
        'username': user.username,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'full_name': reg_data.get('full_name'),
        'age': reg_data.get('age'),
        'gender': reg_data.get('gender'),
        'city': reg_data.get('city', 'Не указан'),
        'interests': reg_data.get('interests', []),
        'bio': reg_data.get('bio', ''),
        'profile_photos': reg_data.get('profile_photos', []),
        'search_gender': 'all',  # По умолчанию ищем всех
        'search_min_age': 18,
        'search_max_age': 100,
        'last_like_reset_date': datetime.now().strftime("%Y-%m-%d")
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
                f"• 📍 {user_data['city']}\n"
                f"• ❤️ Интересы: {', '.join(user_data['interests'][:3])}\n\n"
                f"⚡️ *Доступно:*\n"
                f"• ❤️ {LIKES_PER_DAY_FREE} лайков в день\n"
                f"• 🔍 Ищешь: всех\n"
                f"• 🎯 Возраст: 18-100 лет",
                parse_mode=ParseMode.MARKDOWN
            )
            
            await update.message.reply_text(
                "🎯 *Выбери действие:*",
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
    """Просмотр анкет"""
    user = db.get_user_by_telegram_id(update.effective_user.id)
    if not user:
        await update.message.reply_text("❌ Сначала зарегистрируйся: /start")
        return
    
    db.update_last_seen(user['telegram_id'])
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
            "• Изменить настройки поиска\n"
            "• Расширить возрастной диапазон\n"
            "• Искать по интересам\n"
            "• Зайти позже",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
        return
    
    caption = f"🔥 *{profile['full_name']}, {profile['age']}*\n"
    
    if profile['gender'] == 'male':
        caption += "👨 Мужчина\n"
    elif profile['gender'] == 'female':
        caption += "👩 Женщина\n"
    
    if profile['city']:
        caption += f"📍 {profile['city']}\n"
    
    if profile['interests']:
        interests_str = ', '.join(profile['interests'][:5])
        caption += f"❤️ {interests_str}\n"
    
    if profile['bio']:
        bio_preview = profile['bio'][:100] + "..." if len(profile['bio']) > 100 else profile['bio']
        caption += f"\n📝 {bio_preview}\n"
    
    reply_markup = get_browse_quick_actions()
    
    context.user_data['current_profile_id'] = profile['telegram_id']
    
    if profile['profile_photos']:
        photo = profile['profile_photos'][0]
        
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


async def search_by_interests_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Поиск по интересам"""
    user = db.get_user_by_telegram_id(update.effective_user.id)
    if not user:
        await update.message.reply_text("❌ Сначала зарегистрируйся: /start")
        return
    
    if not user.get('interests'):
        await update.message.reply_text(
            "❌ *У тебя нет указанных интересов*\n\n"
            "Добавь интересы в своем профиле, чтобы искать по ним.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    interests = user['interests']
    profiles = db.search_by_interests(user['telegram_id'], interests[:3])  # Берем первые 3 интереса
    
    if not profiles:
        await update.message.reply_text(
            "😔 *Нет пользователей с такими интересами*\n\n"
            "Попробуй изменить свои интересы или настройки поиска.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Показываем первого пользователя
    profile = profiles[0]
    context.user_data['search_results'] = profiles
    context.user_data['current_search_index'] = 0
    context.user_data['current_profile_id'] = profile['telegram_id']
    
    caption = f"🔍 *ПОИСК ПО ИНТЕРЕСАМ*\n\n"
    caption += f"🔥 *{profile['full_name']}, {profile['age']}*\n"
    
    if profile['gender'] == 'male':
        caption += "👨 Мужчина\n"
    elif profile['gender'] == 'female':
        caption += "👩 Женщина\n"
    
    if profile['city']:
        caption += f"📍 {profile['city']}\n"
    
    if profile['interests']:
        common_interests = set(interests) & set(profile['interests'])
        if common_interests:
            caption += f"❤️ Общие интересы: {', '.join(list(common_interests)[:3])}\n"
    
    if profile['bio']:
        bio_preview = profile['bio'][:100] + "..." if len(profile['bio']) > 100 else profile['bio']
        caption += f"\n📝 {bio_preview}\n"
    
    reply_markup = ReplyKeyboardMarkup([
        ["❤️ Лайк", "➡️ Дальше"],
        ["🚫 Пожаловаться", "🔙 Обычный поиск"],
        ["🔙 Главное меню"]
    ], resize_keyboard=True)
    
    if profile['profile_photos']:
        photo = profile['profile_photos'][0]
        
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
        user_stats = db.get_user_stats(user['telegram_id'])
        
        await update.message.reply_text(
            "✅ *ЛАЙК ОТПРАВЛЕН!*\n\n"
            f"Ждем ответа от *{liked_user['full_name']}*...\n\n"
            f"📊 *Осталось лайков сегодня:* {LIKES_PER_DAY_FREE - user_stats['likes_given_today']}",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            "❌ *Не удалось отправить лайк.*\n"
            "Возможно, достигнут лимит лайков на сегодня.",
            parse_mode=ParseMode.MARKDOWN
        )
    
    await asyncio.sleep(1)
    
    # Определяем какой поиск используется
    if 'search_results' in context.user_data:
        # Поиск по интересам
        await show_next_search_result(update, context)
    else:
        # Обычный поиск
        await show_next_profile(update, context)


async def show_next_search_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать следующий результат поиска"""
    if 'search_results' not in context.user_data:
        await update.message.reply_text("❌ Нет результатов поиска.")
        return
    
    results = context.user_data['search_results']
    current_index = context.user_data.get('current_search_index', 0)
    
    current_index += 1
    if current_index >= len(results):
        current_index = 0
    
    context.user_data['current_search_index'] = current_index
    profile = results[current_index]
    context.user_data['current_profile_id'] = profile['telegram_id']
    
    user = db.get_user_by_telegram_id(update.effective_user.id)
    interests = user.get('interests', []) if user else []
    
    caption = f"🔍 *ПОИСК ПО ИНТЕРЕСАМ*\n\n"
    caption += f"🔥 *{profile['full_name']}, {profile['age']}*\n"
    
    if profile['gender'] == 'male':
        caption += "👨 Мужчина\n"
    elif profile['gender'] == 'female':
        caption += "👩 Женщина\n"
    
    if profile['city']:
        caption += f"📍 {profile['city']}\n"
    
    if profile['interests']:
        common_interests = set(interests) & set(profile['interests'])
        if common_interests:
            caption += f"❤️ Общие интересы: {', '.join(list(common_interests)[:3])}\n"
    
    if profile['bio']:
        bio_preview = profile['bio'][:100] + "..." if len(profile['bio']) > 100 else profile['bio']
        caption += f"\n📝 {bio_preview}\n"
    
    reply_markup = ReplyKeyboardMarkup([
        ["❤️ Лайк", "➡️ Дальше"],
        ["🚫 Пожаловаться", "🔙 Обычный поиск"],
        ["🔙 Главное меню"]
    ], resize_keyboard=True)
    
    if profile['profile_photos']:
        photo = profile['profile_photos'][0]
        
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


async def handle_next_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка 'Дальше'"""
    await update.message.reply_text("🔄 Ищу следующую анкету...")
    
    if 'search_results' in context.user_data:
        await show_next_search_result(update, context)
    else:
        await show_next_profile(update, context)


async def search_settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройки поиска"""
    user = db.get_user_by_telegram_id(update.effective_user.id)
    if not user:
        await update.message.reply_text("❌ Сначала зарегистрируйся: /start")
        return
    
    reply_markup = get_search_settings_keyboard()
    
    await update.message.reply_text(
        f"⚙️ *НАСТРОЙКИ ПОИСКА*\n\n"
        f"👫 *Кого ищешь:* {user.get('search_gender', 'all')}\n"
        f"🎯 *Возраст:* {user.get('search_min_age', 18)}-{user.get('search_max_age', 100)} лет\n\n"
        f"Выбери что изменить:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )
    
    return States.SEARCH_SETTINGS


async def handle_search_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройка пола для поиска"""
    reply_markup = get_search_gender_keyboard()
    
    await update.message.reply_text(
        "👫 *Кого ты ищешь?*\n\n"
        "Выбери вариант:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )
    
    return States.SEARCH_SETTINGS


async def handle_search_gender_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора пола для поиска"""
    text = update.message.text
    
    gender_map = {
        '👨 Мужчин': 'male',
        '👩 Женщин': 'female',
        '👫 Всех': 'all'
    }
    
    if text not in gender_map:
        if text == "🔙 Назад":
            await search_settings_command(update, context)
            return States.SEARCH_SETTINGS
        reply_markup = get_search_gender_keyboard()
        await update.message.reply_text("❌ Пожалуйста, выбери вариант из предложенных:", reply_markup=reply_markup)
        return States.SEARCH_SETTINGS
    
    db.update_user(update.effective_user.id, {'search_gender': gender_map[text]})
    
    await update.message.reply_text(
        f"✅ *Настройки сохранены!*\n\n"
        f"Теперь ты ищешь: {text.lower()}",
        parse_mode=ParseMode.MARKDOWN
    )
    
    await search_settings_command(update, context)
    return States.SEARCH_SETTINGS


async def handle_search_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройка возраста для поиска"""
    await update.message.reply_text(
        "🎯 *НАСТРОЙКА ВОЗРАСТА*\n\n"
        "Введи возрастной диапазон в формате:\n"
        "*18-30* (от 18 до 30 лет)\n\n"
        "Минимальный возраст: 18\n"
        "Максимальный возраст: 100",
        parse_mode=ParseMode.MARKDOWN
    )
    
    return States.SEARCH_SETTINGS


async def handle_search_age_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка возрастного диапазона"""
    try:
        text = update.message.text.strip()
        if '-' not in text:
            raise ValueError
        
        min_age, max_age = map(int, text.split('-'))
        
        if not (18 <= min_age <= 100 and 18 <= max_age <= 100):
            raise ValueError
        
        if min_age > max_age:
            min_age, max_age = max_age, min_age
        
        db.update_user(update.effective_user.id, {
            'search_min_age': min_age,
            'search_max_age': max_age
        })
        
        await update.message.reply_text(
            f"✅ *Настройки сохранены!*\n\n"
            f"Теперь ты ищешь людей в возрасте: *{min_age}-{max_age} лет*",
            parse_mode=ParseMode.MARKDOWN
        )
        
        await search_settings_command(update, context)
        return States.SEARCH_SETTINGS
        
    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ *Неверный формат!*\n\n"
            "Введи возрастной диапазон в формате:\n"
            "*18-30* (от 18 до 30 лет)\n\n"
            "Попробуй еще раз:",
            parse_mode=ParseMode.MARKDOWN
        )
        return States.SEARCH_SETTINGS


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Просмотр профиля"""
    user = db.get_user_by_telegram_id(update.effective_user.id)
    if not user:
        await update.message.reply_text("❌ Сначала зарегистрируйся: /start")
        return
    
    db.update_last_seen(user['telegram_id'])
    
    text = f"📊 *ТВОЙ ПРОФИЛЬ*\n\n"
    text += f"🔥 *{user['full_name']}, {user['age']}*\n"
    
    if user['gender'] == 'male':
        text += "👨 Мужчина\n"
    elif user['gender'] == 'female':
        text += "👩 Женщина\n"
    
    text += f"📍 {user['city'] or 'Город не указан'}\n"
    
    if user['interests']:
        text += f"❤️ {', '.join(user['interests'][:5])}\n"
    
    if user['bio']:
        text += f"\n*О СЕБЕ:*\n{user['bio']}\n\n"
    
    text += f"🔍 *Настройки поиска:*\n"
    text += f"• 👫 Ищешь: {user.get('search_gender', 'all')}\n"
    text += f"• 🎯 Возраст: {user.get('search_min_age', 18)}-{user.get('search_max_age', 100)} лет\n"
    
    reply_markup = get_profile_quick_actions()
    
    if user['profile_photos']:
        photo = user['profile_photos'][0]
        
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
    """Редактирование профиля"""
    reply_markup = get_edit_profile_keyboard()
    
    await update.message.reply_text(
        "✏️ *РЕДАКТИРОВАНИЕ ПРОФИЛЯ*\n\n"
        "Выбери что хочешь изменить:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )
    
    return States.EDIT_PROFILE


async def handle_edit_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактирование имени"""
    await update.message.reply_text(
        "✏️ *ИЗМЕНЕНИЕ ИМЕНИ*\n\n"
        "Введи новое имя:",
        parse_mode=ParseMode.MARKDOWN
    )
    
    return States.EDIT_NAME


async def handle_edit_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нового имени"""
    name = update.message.text.strip()
    
    if len(name) < 2:
        await update.message.reply_text("❌ Имя должно содержать хотя бы 2 символа.")
        return States.EDIT_NAME
    
    db.update_user(update.effective_user.id, {'full_name': name})
    
    await update.message.reply_text(
        "✅ *Имя успешно обновлено!*",
        parse_mode=ParseMode.MARKDOWN
    )
    
    await start_edit_profile(update, context)
    return States.EDIT_PROFILE


async def handle_edit_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактирование возраста"""
    await update.message.reply_text(
        "🎂 *ИЗМЕНЕНИЕ ВОЗРАСТА*\n\n"
        "Введи новый возраст (от 18 до 100):",
        parse_mode=ParseMode.MARKDOWN
    )
    
    return States.EDIT_AGE


async def handle_edit_age_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нового возраста"""
    try:
        age = int(update.message.text.strip())
        
        if not 18 <= age <= 100:
            await update.message.reply_text("❌ Возраст должен быть от 18 до 100 лет.")
            return States.EDIT_AGE
        
        db.update_user(update.effective_user.id, {'age': age})
        
        await update.message.reply_text(
            "✅ *Возраст успешно обновлен!*",
            parse_mode=ParseMode.MARKDOWN
        )
        
        await start_edit_profile(update, context)
        return States.EDIT_PROFILE
        
    except ValueError:
        await update.message.reply_text("❌ Пожалуйста, введи число.")
        return States.EDIT_AGE


async def handle_edit_bio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактирование информации о себе"""
    await update.message.reply_text(
        "📝 *ИЗМЕНЕНИЕ ИНФОРМАЦИИ О СЕБЕ*\n\n"
        "Введи новую информацию о себе (до 500 символов):",
        parse_mode=ParseMode.MARKDOWN
    )
    
    return States.EDIT_BIO


async def handle_edit_bio_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка новой информации о себе"""
    bio = update.message.text.strip()
    
    if len(bio) > MAX_BIO_LENGTH:
        await update.message.reply_text(f"❌ Слишком длинно! Максимум {MAX_BIO_LENGTH} символов.")
        return States.EDIT_BIO
    
    db.update_user(update.effective_user.id, {'bio': bio})
    
    await update.message.reply_text(
        "✅ *Информация о себе успешно обновлена!*",
        parse_mode=ParseMode.MARKDOWN
    )
    
    await start_edit_profile(update, context)
    return States.EDIT_BIO


async def handle_edit_photo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактирование фото"""
    await update.message.reply_text(
        "📸 *ИЗМЕНЕНИЕ ФОТО*\n\n"
        "Отправь новое фото:",
        parse_mode=ParseMode.MARKDOWN
    )
    
    return States.EDIT_PHOTO


async def handle_edit_photo_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нового фото"""
    if not update.message.photo:
        await update.message.reply_text("📸 Пожалуйста, отправь фото.")
        return States.EDIT_PHOTO
    
    photo_file = await update.message.photo[-1].get_file()
    
    db.update_user(update.effective_user.id, {
        'profile_photos': [photo_file.file_id]
    })
    
    await update.message.reply_text(
        "✅ *Фото успешно обновлено!*",
        parse_mode=ParseMode.MARKDOWN
    )
    
    await start_edit_profile(update, context)
    return States.EDIT_PHOTO


async def handle_edit_city_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактирование города"""
    reply_markup = ReplyKeyboardMarkup([
        ["📍 Отправить геолокацию"],
        ["🏙️ Ввести вручную"],
        ["🔙 Назад"]
    ], resize_keyboard=True)
    
    await update.message.reply_text(
        "📍 *ИЗМЕНЕНИЕ ГОРОДА*\n\n"
        "Выбери способ:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )
    
    return States.EDIT_CITY


async def handle_edit_city_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нового города"""
    text = update.message.text
    
    if text == "🔙 Назад":
        await start_edit_profile(update, context)
        return States.EDIT_PROFILE
    
    city = None
    updates = {}
    
    if text == "📍 Отправить геолокацию":
        await update.message.reply_text(
            "📍 Нажми на скрепку 📎 и выбери 'Геопозиция'",
            reply_markup=ReplyKeyboardRemove()
        )
        return States.EDIT_CITY
    elif text == "🏙️ Ввести вручную":
        await update.message.reply_text(
            "🏙️ Напиши название города:",
            reply_markup=ReplyKeyboardRemove()
        )
        return States.EDIT_CITY
    elif text:
        city = text.strip()
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
    
    await start_edit_profile(update, context)
    return States.EDIT_PROFILE


async def handle_edit_interests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактирование интересов"""
    reply_markup = get_interests_keyboard()
    
    user = db.get_user_by_telegram_id(update.effective_user.id)
    interests = user.get('interests', []) if user else []
    
    context.user_data['editing_interests'] = interests.copy()
    
    if interests:
        interests_text = "\n".join([f"• {i}" for i in interests])
        await update.message.reply_text(
            f"❤️ *ТЕКУЩИЕ ИНТЕРЕСЫ:*\n{interests_text}\n\n"
            f"Выбери интересы (можно несколько):",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            "❤️ *ДОБАВЛЕНИЕ ИНТЕРЕСОВ*\n\n"
            "Выбери свои интересы (можно несколько):",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
    
    return States.EDIT_INTERESTS


async def handle_edit_interests_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка редактирования интересов"""
    text = update.message.text
    
    if text == "🔙 Назад":
        await start_edit_profile(update, context)
        return States.EDIT_PROFILE
    
    if text == "✅ Готово":
        interests = context.user_data.get('editing_interests', [])
        db.update_user(update.effective_user.id, {'interests': interests})
        
        await update.message.reply_text(
            "✅ *Интересы успешно обновлены!*",
            parse_mode=ParseMode.MARKDOWN
        )
        
        await start_edit_profile(update, context)
        return States.EDIT_PROFILE
    
    # Добавляем/убираем интерес
    interests = context.user_data.get('editing_interests', [])
    interest_text = text.split(' ', 1)[-1] if ' ' in text else text
    
    if interest_text in interests:
        interests.remove(interest_text)
        await update.message.reply_text(f"❌ {text} - удалено")
    else:
        interests.append(interest_text)
        await update.message.reply_text(f"✅ {text} - добавлено")
    
    context.user_data['editing_interests'] = interests
    
    # Показываем текущий список
    if interests:
        interests_text = "\n".join([f"• {i}" for i in interests])
        await update.message.reply_text(
            f"📋 *Твои интересы:*\n{interests_text}\n\n"
            f"Выбрано: {len(interests)}\n"
            f"Нажми '✅ Готово' когда закончишь",
            parse_mode=ParseMode.MARKDOWN
        )
    
    return States.EDIT_INTERESTS


async def show_who_liked_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать кто лайкнул"""
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
            "• Добавь больше интересов",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    text = f"❤️ *ТЕБЯ ЛАЙКНУЛИ: {len(users_who_liked_me)} ЧЕЛ.*\n\n"
    
    for i, profile in enumerate(users_who_liked_me[:5], 1):
        username_link = f"@{profile['username']}" if profile.get('username') else f"tg://user?id={profile['telegram_id']}"
        text += f"{i}. *{profile['full_name']}, {profile['age']}*\n"
        if profile['city']:
            text += f"📍 {profile['city']}\n"
        if profile['interests']:
            text += f"❤️ {', '.join(profile['interests'][:3])}\n"
        text += f"💬 {username_link}\n\n"
    
    if len(users_who_liked_me) > 5:
        text += f"... и еще {len(users_who_liked_me) - 5} чел.\n\n"
    
    reply_markup = get_back_to_profile_keyboard()
    
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаление анкеты"""
    user = db.get_user_by_telegram_id(update.effective_user.id)
    
    if not user:
        await update.message.reply_text("❌ У тебя нет анкеты для удаления.")
        return
    
    reply_markup = get_confirm_delete_keyboard()
    
    await update.message.reply_text(
        "🗑️ *УДАЛЕНИЕ АНКЕТЫ*\n\n"
        "⚠️ *Внимание!* Это действие нельзя отменить.\n\n"
        "Ты уверен, что хочешь удалить свою анкету?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )
    
    context.user_data['confirming_delete'] = True


async def handle_delete_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение удаления"""
    if not context.user_data.get('confirming_delete'):
        return
    
    text = update.message.text
    user = update.effective_user
    
    if text == "✅ Да, удалить":
        success = db.delete_user(user.id)
        
        if success:
            await update.message.reply_text(
                "✅ *Твоя анкета успешно удалена!*\n\n"
                "Если захочешь вернуться, просто напиши /start",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ReplyKeyboardRemove()
            )
        else:
            await update.message.reply_text(
                "❌ *Не удалось удалить анкету.*\n"
                "Попробуй позже.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_quick_actions_keyboard()
            )
    elif text == "❌ Нет, отменить":
        await update.message.reply_text(
            "✅ *Удаление отменено.*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_quick_actions_keyboard()
        )
    
    context.user_data.pop('confirming_delete', None)


async def handle_report_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Жалоба"""
    await update.message.reply_text(
        "🚫 *ЖАЛОБА*\n\n"
        "Опиши причину жалобы:"
    )
    
    context.user_data['reporting'] = True
    context.user_data['reported_user_id'] = context.user_data.get('current_profile_id')


async def handle_report_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текста жалобы"""
    if context.user_data.get('reporting'):
        reason = update.message.text
        reported_user_id = context.user_data.get('reported_user_id')
        
        if reported_user_id:
            success = db.create_report(update.effective_user.id, reported_user_id, reason)
            
            if success:
                await update.message.reply_text(
                    "✅ *Жалоба отправлена администраторам.*"
                )
            else:
                await update.message.reply_text(
                    "❌ *Не удалось отправить жалобу.*"
                )
        
        context.user_data.pop('reporting', None)
        context.user_data.pop('reported_user_id', None)
        
        # Возвращаемся к просмотру
        if 'search_results' in context.user_data:
            await show_next_search_result(update, context)
        else:
            await show_next_profile(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Помощь"""
    help_text = """
    🤖 *БОТ ДЛЯ ЗНАКОМСТВ «РЯДОМ»*

    ⚡️ *ОСНОВНЫЕ ФУНКЦИИ:*
    • 👀 Смотреть анкеты - обычный поиск
    • 🔍 Поиск по интересам - ищет людей с общими интересами
    • ⚙️ Настройки поиска - настрой возраст и пол для поиска
    • 📊 Мой профиль - просмотр и редактирование профиля
    • ❤️ Кто меня лайкнул - список симпатий

    🎯 *КАК ЭТО РАБОТАЕТ:*
    1. Заполни профиль с интересами
    2. Настрой поиск (возраст, пол)
    3. Смотри анкеты и ставь лайки
    4. При взаимном лайке можно общаться!

    ⭐ *ПОИСК ПО ИНТЕРЕСАМ:*
    • Добавь интересы в профиле
    • Используй кнопку "🔍 Поиск по интересам"
    • Найди людей с общими увлечениями

    📞 *ПОДДЕРЖКА:* @w33RY
    """
    
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)


# === АДМИН-ПАНЕЛЬ ФУНКЦИИ ===

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ-панель"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ У тебя нет доступа.")
        return
    
    reply_markup = get_admin_keyboard()
    
    await update.message.reply_text(
        "⚙️ *АДМИН-ПАНЕЛЬ*\n\n"
        "Выберите действие:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )


async def handle_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика"""
    if not is_admin(update.effective_user.id):
        return
    
    stats = db.get_user_count()
    pending_reports = db.get_pending_reports()
    
    text = f"📊 *СТАТИСТИКА СИСТЕМЫ*\n\n"
    text += f"👥 *Пользователи:*\n"
    text += f"• Всего: {stats['total']}\n"
    text += f"• Активных: {stats['active']}\n"
    text += f"• Забаненных: {stats['banned']}\n"
    text += f"• Новых сегодня: {stats['today']}\n\n"
    
    text += f"⚠️ *Жалобы:*\n"
    text += f"• Ожидают обработки: {len(pending_reports)}\n\n"
    
    text += "⚡️ *Бот работает стабильно*"
    
    reply_markup = get_admin_back_keyboard()
    
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )


async def handle_admin_all_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать всех пользователей с ID"""
    if not is_admin(update.effective_user.id):
        return
    
    users = db.get_all_users(limit=50)
    
    if not users:
        await update.message.reply_text("📭 Пользователей пока нет.")
        return
    
    text = "👥 *ВСЕ ПОЛЬЗОВАТЕЛИ*\n\n"
    
    for i, user in enumerate(users, 1):
        status = "✅" if user['is_active'] else "❌"
        banned = "🚫" if user['is_banned'] else ""
        premium = "⭐" if user.get('is_premium') else ""
        
        text += f"{i}. {status}{banned}{premium} *ID: {user['telegram_id']}*\n"
        text += f"   👤 {user['full_name']}, {user['age']}\n"
        if user.get('username'):
            text += f"   @{user['username']}\n"
        text += f"   📍 {user['city'] or 'не указан'}\n"
        text += f"   📅 {user['created_at'][:10]}\n\n"
    
    text += f"Всего пользователей: {len(users)}"
    
    reply_markup = get_admin_back_keyboard()
    
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )


async def handle_admin_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Жалобы"""
    if not is_admin(update.effective_user.id):
        return
    
    reports = db.get_pending_reports()
    
    if not reports:
        await update.message.reply_text(
            "✅ *Нет необработанных жалоб.*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_admin_back_keyboard()
        )
        return
    
    text = "⚠️ *НЕОБРАБОТАННЫЕ ЖАЛОБЫ*\n\n"
    
    for i, report in enumerate(reports[:10], 1):
        text += f"{i}. *Жалоба #{report['report_id']}*\n"
        text += f"   👤 Жалобщик: {report['reporter_name']} (ID: {report['reporter_id']})\n"
        text += f"   🎯 На кого: {report['reported_name']} (ID: {report['reported_user_id']})\n"
        text += f"   📝 Причина: {report['reason'][:100]}...\n"
        text += f"   📅 {report['created_at'][:16]}\n\n"
    
    if len(reports) > 10:
        text += f"... и еще {len(reports) - 10} жалоб\n\n"
    
    text += "Для обработки жалобы используйте команды:\n"
    text += "`/resolve <ID_жалобы> <комментарий>`\n"
    text += "`/dismiss <ID_жалобы> <комментарий>`"
    
    reply_markup = get_admin_back_keyboard()
    
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )


async def handle_admin_search_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Поиск пользователя"""
    if not is_admin(update.effective_user.id):
        return
    
    await update.message.reply_text(
        "🔍 *ПОИСК ПОЛЬЗОВАТЕЛЯ*\n\n"
        "Введите:\n"
        "• Telegram ID пользователя\n"
        "• Имя пользователя (username) без @\n"
        "• Имя или часть имени\n\n"
        "Пример: `123456789` или `ivan` или `ivan123`",
        parse_mode=ParseMode.MARKDOWN
    )
    
    # Ждем ответа
    context.user_data['awaiting_admin_search'] = True


async def handle_admin_search_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ввода для поиска пользователя (обработка текста)"""
    if not is_admin(update.effective_user.id):
        return
    
    if not context.user_data.get('awaiting_admin_search'):
        return
    
    search_term = update.message.text.strip()
    
    if not search_term:
        await update.message.reply_text("❌ Пожалуйста, введите поисковый запрос.")
        return
    
    users = db.search_users(search_term)
    
    if not users:
        await update.message.reply_text(
            f"❌ Пользователи по запросу '{search_term}' не найдены.",
            reply_markup=get_admin_back_keyboard()
        )
    else:
        text = f"🔍 *РЕЗУЛЬТАТЫ ПОИСКА: '{search_term}'*\n\n"
        
        for i, user in enumerate(users[:5], 1):
            status = "✅" if user['is_active'] else "❌"
            banned = "🚫" if user['is_banned'] else "✅"
            
            text += f"{i}. {status} {banned} *ID: {user['telegram_id']}*\n"
            text += f"   👤 {user['full_name']}, {user['age']}\n"
            if user.get('username'):
                text += f"   @{user['username']}\n"
            text += f"   📍 {user['city'] or 'не указан'}\n"
            text += f"   📅 Зарегистрирован: {user['created_at'][:10]}\n"
            text += f"   👁 Последняя активность: {user['last_seen'][:16]}\n\n"
        
        if len(users) > 5:
            text += f"Найдено {len(users)} пользователей, показаны первые 5.\n\n"
        
        text += "Для бана пользователя используйте команду:\n"
        text += f"`/ban {users[0]['telegram_id']}`"
        
        reply_markup = get_admin_back_keyboard()
        
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
    
    context.user_data.pop('awaiting_admin_search', None)


async def handle_admin_ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Бан пользователя"""
    if not is_admin(update.effective_user.id):
        return
    
    await update.message.reply_text(
        "🚫 *БАН ПОЛЬЗОВАТЕЛЯ*\n\n"
        "Введите Telegram ID пользователя для бана:\n\n"
        "Пример: `123456789`\n\n"
        "⚠️ *Внимание:* Пользователь не сможет пользоваться ботом после бана.",
        parse_mode=ParseMode.MARKDOWN
    )
    
    # Ждем ответа
    context.user_data['awaiting_admin_ban'] = True


async def handle_admin_ban_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ввода ID для бана (обработка текста)"""
    if not is_admin(update.effective_user.id):
        return
    
    if not context.user_data.get('awaiting_admin_ban'):
        return
    
    try:
        telegram_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Неверный формат. Введите числовой Telegram ID.")
        return
    
    user = db.get_user_by_telegram_id(telegram_id)
    
    if not user:
        await update.message.reply_text(f"❌ Пользователь с ID {telegram_id} не найден.")
        context.user_data.pop('awaiting_admin_ban', None)
        return
    
    if user['is_banned']:
        success = db.unban_user(telegram_id)
        action = "разбанен"
    else:
        success = db.ban_user(telegram_id)
        action = "забанен"
    
    if success:
        await update.message.reply_text(
            f"✅ Пользователь *{user['full_name']}* (ID: {telegram_id}) успешно {action}.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_admin_back_keyboard()
        )
    else:
        await update.message.reply_text(
            f"❌ Не удалось {action} пользователя.",
            reply_markup=get_admin_back_keyboard()
        )
    
    context.user_data.pop('awaiting_admin_ban', None)


async def handle_admin_send_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправка сообщения"""
    if not is_admin(update.effective_user.id):
        return
    
    await update.message.reply_text(
        "📨 *ОТПРАВКА СООБЩЕНИЯ*\n\n"
        "Введите сообщение для отправки всем пользователям:\n\n"
        "Или укажите Telegram ID для отправки конкретному пользователю:\n"
        "`123456789 Ваше сообщение`\n\n"
        "Для отправки всем просто напишите сообщение.",
        parse_mode=ParseMode.MARKDOWN
    )
    
    # Ждем ответа
    context.user_data['awaiting_admin_message'] = True


async def handle_admin_message_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ввода сообщения (обработка текста)"""
    if not is_admin(update.effective_user.id):
        return
    
    if not context.user_data.get('awaiting_admin_message'):
        return
    
    message_text = update.message.text.strip()
    
    if not message_text:
        await update.message.reply_text("❌ Сообщение не может быть пустым.")
        return
    
    # Проверяем, указан ли конкретный пользователь
    parts = message_text.split(' ', 1)
    
    if len(parts) == 2 and parts[0].isdigit():
        # Отправка конкретному пользователю
        telegram_id = int(parts[0])
        message = parts[1]
        
        user = db.get_user_by_telegram_id(telegram_id)
        
        if not user:
            await update.message.reply_text(f"❌ Пользователь с ID {telegram_id} не найден.")
            context.user_data.pop('awaiting_admin_message', None)
            return
        
        try:
            await context.bot.send_message(
                chat_id=telegram_id,
                text=f"📨 *СООБЩЕНИЕ ОТ АДМИНИСТРАТОРА*\n\n{message}",
                parse_mode=ParseMode.MARKDOWN
            )
            
            await update.message.reply_text(
                f"✅ Сообщение отправлено пользователю *{user['full_name']}* (ID: {telegram_id}).",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_admin_back_keyboard()
            )
        except Exception as e:
            logging.error(f"Error sending message to user {telegram_id}: {e}")
            await update.message.reply_text(
                f"❌ Не удалось отправить сообщение пользователю {telegram_id}.\n"
                f"Возможно, пользователь заблокировал бота.",
                reply_markup=get_admin_back_keyboard()
            )
    else:
        # Отправка всем пользователям
        message = message_text
        
        all_users = db.get_all_users(limit=1000)
        
        sent_count = 0
        failed_count = 0
        
        await update.message.reply_text(
            f"🔄 Начинаю рассылку сообщения для {len(all_users)} пользователей...",
            reply_markup=get_admin_back_keyboard()
        )
        
        for user in all_users:
            if user['is_active'] and not user['is_banned']:
                try:
                    await context.bot.send_message(
                        chat_id=user['telegram_id'],
                        text=f"📨 *СООБЩЕНИЕ ОТ АДМИНИСТРАТОРА*\n\n{message}",
                        parse_mode=ParseMode.MARKDOWN
                    )
                    sent_count += 1
                    
                    await asyncio.sleep(0.1)
                except Exception as e:
                    failed_count += 1
        
        await update.message.reply_text(
            f"✅ *Рассылка завершена!*\n\n"
            f"📊 Результаты:\n"
            f"• Отправлено: {sent_count}\n"
            f"• Не отправлено: {failed_count}\n"
            f"• Всего: {len(all_users)}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_admin_back_keyboard()
        )
    
    context.user_data.pop('awaiting_admin_message', None)


# === АДМИН КОМАНДЫ ===

async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /stats"""
    await handle_admin_stats(update, context)

async def admin_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /users"""
    await handle_admin_all_users(update, context)

async def admin_reports_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /reports"""
    await handle_admin_reports(update, context)

async def admin_ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /ban <id>"""
    if not is_admin(update.effective_user.id):
        return
    
    if not context.args:
        await update.message.reply_text("❌ Использование: /ban <telegram_id>")
        return
    
    try:
        telegram_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный формат ID. Должно быть число.")
        return
    
    user = db.get_user_by_telegram_id(telegram_id)
    
    if not user:
        await update.message.reply_text(f"❌ Пользователь с ID {telegram_id} не найден.")
        return
    
    if user['is_banned']:
        success = db.unban_user(telegram_id)
        action = "разбанен"
    else:
        success = db.ban_user(telegram_id)
        action = "забанен"
    
    if success:
        await update.message.reply_text(
            f"✅ Пользователь *{user['full_name']}* (ID: {telegram_id}) успешно {action}.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            f"❌ Не удалось {action} пользователя."
        )


# === ОБРАБОТКА БЫСТРЫХ КНОПОК ===

async def handle_quick_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка быстрых кнопок"""
    text = update.message.text
    
    if text == "👀 Смотреть анкеты":
        await browse_profiles_command(update, context)
    elif text == "🔍 Поиск по интересам":
        await search_by_interests_command(update, context)
    elif text == "⚙️ Настройки поиска":
        await search_settings_command(update, context)
    elif text == "📊 Мой профиль":
        await profile_command(update, context)
    elif text == "❤️ Кто меня лайкнул":
        await show_who_liked_me(update, context)
    elif text == "🆘 Помощь":
        await help_command(update, context)
    elif text == "🗑️ Удалить анкету":
        await delete_command(update, context)
    elif text == "❤️ Лайк":
        await handle_like_action(update, context)
    elif text == "➡️ Дальше":
        await handle_next_action(update, context)
    elif text == "🚫 Пожаловаться":
        await handle_report_action(update, context)
    elif text == "🔙 Главное меню" or text == "🔙 К профилю":
        reply_markup = get_quick_actions_keyboard()
        await update.message.reply_text("⚡️ Возвращаю в меню...", reply_markup=reply_markup)
    elif text == "🔙 Обычный поиск":
        context.user_data.pop('search_results', None)
        await browse_profiles_command(update, context)
    elif text == "✏️ Редактировать профиль":
        await start_edit_profile(update, context)
    elif text == "👫 Кого искать":
        await handle_search_gender(update, context)
    elif text == "🎯 Возраст":
        await handle_search_age(update, context)
    elif text == "✏️ Имя":
        await handle_edit_name(update, context)
    elif text == "🎯 Возраст":
        await handle_edit_age(update, context)
    elif text == "📍 Город":
        await handle_edit_city_command(update, context)
    elif text == "📸 Фото":
        await handle_edit_photo_command(update, context)
    elif text == "❤️ Интересы":
        await handle_edit_interests(update, context)
    elif text == "📝 О себе":
        await handle_edit_bio_command(update, context)
    elif text == "🔙 В админ-меню":
        await admin_command(update, context)
    elif text == "📊 Статистика":
        await handle_admin_stats(update, context)
    elif text == "👥 Все пользователи":
        await handle_admin_all_users(update, context)
    elif text == "⚠️ Жалобы":
        await handle_admin_reports(update, context)
    elif text == "🔍 Найти пользователя":
        await handle_admin_search_user(update, context)
    elif text == "🚫 Забанить":
        await handle_admin_ban_user(update, context)
    elif text == "📨 Отправить сообщение":
        await handle_admin_send_message(update, context)
    else:
        # Проверяем состояния
        if context.user_data.get('confirming_delete'):
            await handle_delete_confirmation(update, context)
        elif context.user_data.get('reporting'):
            await handle_report_text(update, context)
        elif context.user_data.get('awaiting_admin_search'):
            await handle_admin_search_input(update, context)
        elif context.user_data.get('awaiting_admin_ban'):
            await handle_admin_ban_input(update, context)
        elif context.user_data.get('awaiting_admin_message'):
            await handle_admin_message_input(update, context)
        else:
            if text.startswith('/'):
                await update.message.reply_text(
                    "Используй кнопки меню для навигации.\n"
                    "Или команды: /start /help /admin"
                )
            else:
                await update.message.reply_text(
                    "Не понимаю команду. Используй кнопки меню."
                )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена"""
    await update.message.reply_text(
        "❌ Действие отменено.",
        reply_markup=get_quick_actions_keyboard()
    )
    return ConversationHandler.END


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    logging.error(f"Exception: {context.error}")
    
    try:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Произошла ошибка. Попробуй снова."
        )
    except:
        pass


def main():
    """Запуск бота"""
    
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    

    
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_error_handler(error_handler)
    

    registration_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start_command)],
        states={
            States.REG_PHOTO: [MessageHandler(filters.PHOTO, handle_registration_photo)],
            States.REG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_registration_name)],
            States.REG_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_registration_age)],
            States.REG_GENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_registration_gender)],
            States.REG_CITY: [MessageHandler(filters.TEXT | filters.LOCATION, handle_registration_city)],
            States.REG_INTERESTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_registration_interests)],
            States.REG_BIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_registration_bio)],
            States.SEARCH_SETTINGS: [
                MessageHandler(filters.TEXT & filters.Regex("^👫 Кого искать$"), handle_search_gender),
                MessageHandler(filters.TEXT & filters.Regex("^🎯 Возраст$"), handle_search_age),
                MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: handle_search_gender_input(u, c) or handle_search_age_input(u, c))
            ],
            States.EDIT_PROFILE: [
                MessageHandler(filters.TEXT & filters.Regex("^✏️ Имя$"), handle_edit_name),
                MessageHandler(filters.TEXT & filters.Regex("^🎯 Возраст$"), handle_edit_age),
                MessageHandler(filters.TEXT & filters.Regex("^📍 Город$"), handle_edit_city_command),
                MessageHandler(filters.TEXT & filters.Regex("^📸 Фото$"), handle_edit_photo_command),
                MessageHandler(filters.TEXT & filters.Regex("^❤️ Интересы$"), handle_edit_interests),
                MessageHandler(filters.TEXT & filters.Regex("^📝 О себе$"), handle_edit_bio_command),
                MessageHandler(filters.TEXT & filters.Regex("^🔙 К профилю$"), profile_command),
            ],
            States.EDIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_name_input)],
            States.EDIT_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_age_input)],
            States.EDIT_BIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_bio_input)],
            States.EDIT_PHOTO: [MessageHandler(filters.PHOTO, handle_edit_photo_input)],
            States.EDIT_CITY: [MessageHandler(filters.TEXT | filters.LOCATION, handle_edit_city_input)],
            States.EDIT_INTERESTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_interests_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    
    application.add_handler(registration_handler)
    

    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("profile", profile_command))
    application.add_handler(CommandHandler("browse", browse_profiles_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("delete", delete_command))
    
    application.add_handler(CommandHandler("stats", admin_stats_command))
    application.add_handler(CommandHandler("users", admin_users_command))
    application.add_handler(CommandHandler("reports", admin_reports_command))
    application.add_handler(CommandHandler("ban", admin_ban_command))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_quick_buttons))
    
    print("БОТ ЗАПУЩЕН")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
