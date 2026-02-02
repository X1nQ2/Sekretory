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
    ReplyKeyboardMarkup
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters
)
from telegram.constants import ParseMode


BOT_TOKEN = ""  


ADMIN_IDS = []
DB_PATH = "baze.db"

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
    ADMIN_MENU = 12
    ADMIN_SEARCH_USER = 13
    ADMIN_BAN_USER = 14
    ADMIN_SEND_MESSAGE = 15

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
            
            # Таблица чатов (для будущего расширения)
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
            
            # Создаем индексы для оптимизации запросов
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_telegram ON users(telegram_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active) WHERE is_active = 1")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_gender ON users(gender)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_city ON users(city)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_likes_from_to ON likes(from_user_id, to_user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_likes_to_from ON likes(to_user_id, from_user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_likes_mutual ON likes(is_mutual) WHERE is_mutual = 1")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_profile_views ON profile_views(viewer_id, viewed_user_id)")
    
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
                # Десериализация photos
                if user['profile_photos']:
                    try:
                        user['profile_photos'] = json.loads(user['profile_photos'])
                    except:
                        user['profile_photos'] = []
                else:
                    user['profile_photos'] = []
                return user
            return None
    
    def get_user_by_id(self, user_id: int) -> Optional[Dict]:
        """Получить пользователя по внутреннему user_id"""
        with self.get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM users 
                WHERE user_id = ?
            """, (user_id,))
            row = cursor.fetchone()
            if row:
                user = dict(row)
                # Десериализация photos
                if user['profile_photos']:
                    try:
                        user['profile_photos'] = json.loads(user['profile_photos'])
                    except:
                        user['profile_photos'] = []
                else:
                    user['profile_photos'] = []
                return user
            return None
    
    def get_user_by_username(self, username: str) -> Optional[Dict]:
        """Получить пользователя по username"""
        with self.get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM users 
                WHERE username = ?
            """, (username,))
            row = cursor.fetchone()
            if row:
                user = dict(row)
                # Десериализация photos
                if user['profile_photos']:
                    try:
                        user['profile_photos'] = json.loads(user['profile_photos'])
                    except:
                        user['profile_photos'] = []
                else:
                    user['profile_photos'] = []
                return user
            return None
    
    def search_users(self, search_term: str) -> List[Dict]:
        """Поиск пользователей по имени, username или telegram_id"""
        with self.get_connection() as conn:
            try:
                # Пробуем найти по telegram_id
                telegram_id = int(search_term)
                cursor = conn.execute("""
                    SELECT * FROM users 
                    WHERE telegram_id = ?
                """, (telegram_id,))
            except ValueError:
                # Ищем по имени или username
                cursor = conn.execute("""
                    SELECT * FROM users 
                    WHERE full_name LIKE ? OR username LIKE ?
                    LIMIT 20
                """, (f"%{search_term}%", f"%{search_term}%"))
            
            rows = cursor.fetchall()
            users = []
            for row in rows:
                user = dict(row)
                if user['profile_photos']:
                    try:
                        user['profile_photos'] = json.loads(user['profile_photos'])
                    except:
                        user['profile_photos'] = []
                else:
                    user['profile_photos'] = []
                users.append(user)
            return users
    
    def create_user(self, user_data: Dict) -> Optional[Dict]:
        """Создать нового пользователя"""
        with self.get_connection() as conn:
            # Подготовка данных
            data_to_insert = user_data.copy()
            
            # Сериализация photos
            if 'profile_photos' in data_to_insert and isinstance(data_to_insert['profile_photos'], list):
                data_to_insert['profile_photos'] = json.dumps(data_to_insert['profile_photos'], ensure_ascii=False)
            
            # Установка временных меток
            now = datetime.now().isoformat()
            data_to_insert['created_at'] = now
            data_to_insert['updated_at'] = now
            data_to_insert['last_seen'] = now
            
            # Формирование SQL запроса
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
                return self.get_user_by_id(user_id)
            except Exception as e:
                logging.error(f"Error creating user: {e}")
                return None
    
    def update_user(self, telegram_id: int, updates: Dict) -> bool:
        """Обновить данные пользователя"""
        with self.get_connection() as conn:
            # Подготовка данных
            data_to_update = updates.copy()
            
            # Сериализация photos
            if 'profile_photos' in data_to_update and isinstance(data_to_update['profile_photos'], list):
                data_to_update['profile_photos'] = json.dumps(data_to_update['profile_photos'], ensure_ascii=False)
            
            # Обновляем временную метку
            data_to_update['updated_at'] = datetime.now().isoformat()
            
            set_clause = ', '.join([f"{key} = ?" for key in data_to_update.keys()])
            values = list(data_to_update.values()) + [telegram_id]
            
            sql = f"UPDATE users SET {set_clause} WHERE telegram_id = ?"
            cursor = conn.execute(sql, values)
            return cursor.rowcount > 0
    
    def delete_user(self, telegram_id: int) -> bool:
        """Удалить пользователя (анкету)"""
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
    
    def update_last_seen(self, telegram_id: int) -> bool:
        """Обновить время последней активности"""
        with self.get_connection() as conn:
            cursor = conn.execute("""
                UPDATE users 
                SET last_seen = ?, updated_at = ?
                WHERE telegram_id = ?
            """, (datetime.now().isoformat(), datetime.now().isoformat(), telegram_id))
            return cursor.rowcount > 0
    
    def reset_daily_likes_if_needed(self, telegram_id: int):
        """Сбросить счетчик лайков за день, если нужно"""
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
    
    def get_next_profile(self, current_user_telegram_id: int) -> Optional[Dict]:
        """Получить следующую анкету для показа текущему пользователю"""
        with self.get_connection() as conn:
            current_user = self.get_user_by_telegram_id(current_user_telegram_id)
            if not current_user:
                return None
            
            # Получаем пользователей, которых текущий пользователь еще не лайкал
            query = """
                SELECT u.* 
                FROM users u
                WHERE u.telegram_id != ?
                AND u.is_active = 1
                AND u.is_banned = 0
                AND NOT EXISTS (
                    SELECT 1 FROM likes l 
                    WHERE l.from_user_id = ? AND l.to_user_id = u.user_id
                )
                ORDER BY RANDOM()
                LIMIT 1
            """
            
            cursor = conn.execute(query, (current_user_telegram_id, current_user['user_id']))
            row = cursor.fetchone()
            
            if row:
                profile = dict(row)
                # Десериализация photos
                if profile['profile_photos']:
                    try:
                        profile['profile_photos'] = json.loads(profile['profile_photos'])
                    except:
                        profile['profile_photos'] = []
                else:
                    profile['profile_photos'] = []
                
                # Записываем просмотр
                self.record_profile_view(current_user['user_id'], profile['user_id'])
                
                return profile
            
            return None
    
    def record_profile_view(self, viewer_id: int, viewed_user_id: int):
        """Записать факт просмотра профиля"""
        with self.get_connection() as conn:
            try:
                conn.execute("""
                    INSERT INTO profile_views (viewer_id, viewed_user_id)
                    VALUES (?, ?)
                """, (viewer_id, viewed_user_id))
            except Exception as e:
                logging.error(f"Error recording profile view: {e}")
    
    def create_like(self, from_user_telegram_id: int, to_user_telegram_id: int) -> Tuple[bool, Optional[Dict]]:
        """Создать лайк и проверить на взаимность"""
        with self.get_connection() as conn:
            from_user = self.get_user_by_telegram_id(from_user_telegram_id)
            to_user = self.get_user_by_telegram_id(to_user_telegram_id)
            
            if not from_user or not to_user:
                return False, None
            
            # Проверяем и сбрасываем дневной лимит при необходимости
            today = datetime.now().strftime("%Y-%m-%d")
            if from_user.get('last_like_reset_date') != today:
                conn.execute("""
                    UPDATE users 
                    SET likes_given_today = 0, last_like_reset_date = ?
                    WHERE telegram_id = ?
                """, (today, from_user_telegram_id))
                from_user['likes_given_today'] = 0
            
            # Проверяем дневной лимит лайков
            if from_user.get('likes_given_today', 0) >= LIKES_PER_DAY_FREE and not from_user.get('is_premium', False):
                return False, None
            
            try:
                # Создаем лайк
                conn.execute("""
                    INSERT OR IGNORE INTO likes (from_user_id, to_user_id)
                    VALUES (?, ?)
                """, (from_user['user_id'], to_user['user_id']))
                
                # Обновляем счетчики
                conn.execute("""
                    UPDATE users 
                    SET likes_given_today = likes_given_today + 1,
                        updated_at = ?
                    WHERE telegram_id = ?
                """, (datetime.now().isoformat(), from_user_telegram_id))
                
                conn.execute("""
                    UPDATE users 
                    SET likes_received_total = likes_received_total + 1,
                        updated_at = ?
                    WHERE telegram_id = ?
                """, (datetime.now().isoformat(), to_user_telegram_id))
                
                # Проверяем на взаимность
                cursor = conn.execute("""
                    SELECT 1 FROM likes 
                    WHERE from_user_id = ? AND to_user_id = ?
                """, (to_user['user_id'], from_user['user_id']))
                
                is_mutual = cursor.fetchone() is not None
                
                # Если взаимный, обновляем оба лайка
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
                if profile['profile_photos']:
                    try:
                        profile['profile_photos'] = json.loads(profile['profile_photos'])
                    except:
                        profile['profile_photos'] = []
                else:
                    profile['profile_photos'] = []
                profiles.append(profile)
            
            return profiles
    
    def get_mutual_likes(self, telegram_id: int) -> List[Dict]:
        """Получить список взаимных лайков"""
        with self.get_connection() as conn:
            user = self.get_user_by_telegram_id(telegram_id)
            if not user:
                return []
            
            query = """
                SELECT u.* 
                FROM users u
                JOIN likes l ON l.from_user_id = u.user_id
                WHERE l.to_user_id = ? 
                AND l.is_mutual = 1
                AND u.is_active = 1
                AND u.is_banned = 0
                ORDER BY l.created_at DESC
            """
            
            cursor = conn.execute(query, (user['user_id'],))
            rows = cursor.fetchall()
            
            profiles = []
            for row in rows:
                profile = dict(row)
                if profile['profile_photos']:
                    try:
                        profile['profile_photos'] = json.loads(profile['profile_photos'])
                    except:
                        profile['profile_photos'] = []
                else:
                    profile['profile_photos'] = []
                profiles.append(profile)
            
            return profiles
    
    def get_user_stats(self, telegram_id: int) -> Dict:
        """Получить статистику пользователя"""
        with self.get_connection() as conn:
            user = self.get_user_by_telegram_id(telegram_id)
            if not user:
                return {}
            
            # Количество лайков, которые поставил пользователь
            cursor = conn.execute("""
                SELECT COUNT(*) as likes_given 
                FROM likes 
                WHERE from_user_id = ?
            """, (user['user_id'],))
            likes_given = cursor.fetchone()['likes_given']
            
            # Количество лайков, которые получил пользователь
            cursor = conn.execute("""
                SELECT COUNT(*) as likes_received 
                FROM likes 
                WHERE to_user_id = ?
            """, (user['user_id'],))
            likes_received = cursor.fetchone()['likes_received']
            
            # Количество взаимных лайков
            cursor = conn.execute("""
                SELECT COUNT(*) as mutual_likes 
                FROM likes 
                WHERE (from_user_id = ? OR to_user_id = ?)
                AND is_mutual = 1
            """, (user['user_id'], user['user_id']))
            mutual_likes = cursor.fetchone()['mutual_likes']
            
            # Количество просмотров профиля
            cursor = conn.execute("""
                SELECT COUNT(*) as profile_views 
                FROM profile_views 
                WHERE viewed_user_id = ?
            """, (user['user_id'],))
            profile_views = cursor.fetchone()['profile_views']
            
            return {
                'likes_given': likes_given,
                'likes_received': likes_received,
                'mutual_likes': mutual_likes,
                'profile_views': profile_views,
                'likes_given_today': user.get('likes_given_today', 0),
                'likes_received_total': user.get('likes_received_total', 0)
            }
    
    def create_report(self, reporter_telegram_id: int, reported_user_telegram_id: int, reason: str) -> bool:
        """Создать жалобу на пользователя"""
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
            except Exception as e:
                logging.error(f"Error creating report: {e}")
                return False
    
    def get_pending_reports(self) -> List[Dict]:
        """Получить список необработанных жалоб"""
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
    
    def update_report_status(self, report_id: int, status: str, admin_notes: str = None) -> bool:
        """Обновить статус жалобы"""
        with self.get_connection() as conn:
            cursor = conn.execute("""
                UPDATE reports 
                SET status = ?, admin_notes = ?
                WHERE report_id = ?
            """, (status, admin_notes, report_id))
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
                if user['profile_photos']:
                    try:
                        user['profile_photos'] = json.loads(user['profile_photos'])
                    except:
                        user['profile_photos'] = []
                else:
                    user['profile_photos'] = []
                users.append(user)
            return users
    
    def get_user_count(self) -> Dict:
        """Получить статистику по пользователям"""
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) as total FROM users")
            total = cursor.fetchone()['total']
            
            cursor = conn.execute("SELECT COUNT(*) as active FROM users WHERE is_active = 1")
            active = cursor.fetchone()['active']
            
            cursor = conn.execute("SELECT COUNT(*) as banned FROM users WHERE is_banned = 1")
            banned = cursor.fetchone()['banned']
            
            cursor = conn.execute("SELECT COUNT(*) as premium FROM users WHERE is_premium = 1")
            premium = cursor.fetchone()['premium']
            
            cursor = conn.execute("SELECT COUNT(*) as today FROM users WHERE DATE(created_at) = DATE('now')")
            today = cursor.fetchone()['today']
            
            return {
                'total': total,
                'active': active,
                'banned': banned,
                'premium': premium,
                'today': today
            }
    
    def create_admin_message(self, admin_id: int, user_id: Optional[int], message_text: str) -> bool:
        """Создать запись о сообщении администратора"""
        with self.get_connection() as conn:
            try:
                conn.execute("""
                    INSERT INTO admin_messages (admin_id, user_id, message_text)
                    VALUES (?, ?, ?)
                """, (admin_id, user_id, message_text))
                return True
            except Exception as e:
                logging.error(f"Error creating admin message: {e}")
                return False
    
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
        ["🗑️ Удалить анкету", "🔙 Назад в меню"]
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

def get_admin_keyboard():
    """Клавиатура для админ-меню"""
    return ReplyKeyboardMarkup([
        ["📊 Статистика", "👥 Все пользователи"],
        ["⚠️ Жалобы", "🔍 Найти пользователя"],
        ["🚫 Забанить", "📨 Отправить сообщение"],
        ["🔙 Главное меню"]
    ], resize_keyboard=True, one_time_keyboard=False)

def get_admin_back_keyboard():
    """Кнопка возврата в админ-меню"""
    return ReplyKeyboardMarkup([
        ["🔙 В админ-меню"]
    ], resize_keyboard=True)

def get_confirm_delete_keyboard():
    """Клавиатура для подтверждения удаления"""
    return ReplyKeyboardMarkup([
        ["✅ Да, удалить", "❌ Нет, отменить"]
    ], resize_keyboard=True, one_time_keyboard=True)

def is_admin(telegram_id: int) -> bool:
    """Проверить, является ли пользователь администратором"""
    return telegram_id in ADMIN_IDS


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
        
        # Обновляем время последней активности
        db.update_last_seen(user.id)
        
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


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для удаления анкеты"""
    user = update.effective_user
    db_user = db.get_user_by_telegram_id(user.id)
    
    if not db_user:
        await update.message.reply_text("❌ У тебя нет анкеты для удаления.")
        return
    
    reply_markup = get_confirm_delete_keyboard()
    
    await update.message.reply_text(
        "🗑️ *УДАЛЕНИЕ АНКЕТЫ*\n\n"
        "⚠️ *Внимание!* Это действие нельзя отменить.\n\n"
        "При удалении анкеты:\n"
        "• Все твои данные будут удалены\n"
        "• Все лайки будут удалены\n"
        "• Жалобы на тебя будут удалены\n"
        "• Вся статистика будет потеряна\n\n"
        "Ты уверен, что хочешь удалить свою анкету?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )
    
    context.user_data['confirming_delete'] = True


async def handle_delete_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка подтверждения удаления"""
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
                "Попробуй позже или обратись в поддержку.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_quick_actions_keyboard()
            )
    elif text == "❌ Нет, отменить":
        await update.message.reply_text(
            "✅ *Удаление отменено.*\n"
            "Твоя анкета сохранена.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_quick_actions_keyboard()
        )
    
    context.user_data.pop('confirming_delete', None)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для админ-меню"""
    user = update.effective_user
    
    if not is_admin(user.id):
        await update.message.reply_text("❌ У тебя нет доступа к этой команде.")
        return
    
    reply_markup = get_admin_keyboard()
    
    await update.message.reply_text(
        "⚙️ *АДМИН-ПАНЕЛЬ*\n\n"
        "Выберите действие:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )
    
    return States.ADMIN_MENU


async def handle_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка просмотра статистики"""
    if not is_admin(update.effective_user.id):
        return
    
    stats = db.get_user_count()
    pending_reports = db.get_pending_reports()
    
    text = "📊 *СТАТИСТИКА СИСТЕМЫ*\n\n"
    text += f"👥 *Пользователи:*\n"
    text += f"• Всего: {stats['total']}\n"
    text += f"• Активных: {stats['active']}\n"
    text += f"• Забаненных: {stats['banned']}\n"
    text += f"• Премиум: {stats['premium']}\n"
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
    """Обработка просмотра всех пользователей"""
    if not is_admin(update.effective_user.id):
        return
    
    users = db.get_all_users(limit=20)
    
    if not users:
        await update.message.reply_text("📭 Пользователей пока нет.")
        return
    
    text = "👥 *ПОСЛЕДНИЕ ПОЛЬЗОВАТЕЛИ*\n\n"
    
    for i, user in enumerate(users, 1):
        status = "✅" if user['is_active'] else "❌"
        banned = "🚫" if user['is_banned'] else "✅"
        premium = "⭐" if user['is_premium'] else "🔹"
        
        text += f"{i}. {status} {banned} {premium} *{user['full_name']}*, {user['age']}\n"
        text += f"   👤 @{user['username'] or 'нет'}\n"
        text += f"   🆔 {user['telegram_id']}\n"
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
    """Обработка просмотра жалоб"""
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
        text += f"   👤 Жалобщик: {report['reporter_name']} (@{report['reporter_username'] or 'нет'})\n"
        text += f"   🎯 На кого: {report['reported_name']} (@{report['reported_username'] or 'нет'})\n"
        text += f"   📝 Причина: {report['reason'][:100]}...\n"
        text += f"   📅 {report['created_at'][:16]}\n\n"
    
    if len(reports) > 10:
        text += f"... и еще {len(reports) - 10} жалоб\n\n"
    
    text += "Для обработки жалобы используйте команду:\n"
    text += "`/resolve <ID_жалобы> <комментарий>`\n"
    text += "`/dismiss <ID_жалобы> <комментарий>`"
    
    reply_markup = get_admin_back_keyboard()
    
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )


async def handle_admin_search_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка поиска пользователя"""
    if not is_admin(update.effective_user.id):
        return
    
    await update.message.reply_text(
        "🔍 *ПОИСК ПОЛЬЗОВАТЕЛЯ*\n\n"
        "Введите:\n"
        "• Telegram ID пользователя\n"
        "• Имя пользователя (username) без @\n"
        "• Имя или часть имени\n\n"
        "Пример: `123456789` или `ivan` или `ivan123`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_admin_back_keyboard()
    )
    
    return States.ADMIN_SEARCH_USER


async def handle_admin_search_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ввода для поиска пользователя"""
    if not is_admin(update.effective_user.id):
        return
    
    search_term = update.message.text.strip()
    
    if not search_term:
        await update.message.reply_text("❌ Пожалуйста, введите поисковый запрос.")
        return States.ADMIN_SEARCH_USER
    
    users = db.search_users(search_term)
    
    if not users:
        await update.message.reply_text(
            f"❌ Пользователи по запросу '{search_term}' не найдены.",
            reply_markup=get_admin_back_keyboard()
        )
        return States.ADMIN_MENU
    
    text = f"🔍 *РЕЗУЛЬТАТЫ ПОИСКА: '{search_term}'*\n\n"
    
    for i, user in enumerate(users[:5], 1):
        status = "✅" if user['is_active'] else "❌"
        banned = "🚫" if user['is_banned'] else "✅"
        premium = "⭐" if user['is_premium'] else "🔹"
        
        text += f"{i}. {status} {banned} {premium} *{user['full_name']}*, {user['age']}\n"
        text += f"   👤 @{user['username'] or 'нет'}\n"
        text += f"   🆔 {user['telegram_id']}\n"
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
    
    return States.ADMIN_MENU


async def handle_admin_ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка бана пользователя"""
    if not is_admin(update.effective_user.id):
        return
    
    await update.message.reply_text(
        "🚫 *БАН ПОЛЬЗОВАТЕЛЯ*\n\n"
        "Введите Telegram ID пользователя для бана:\n\n"
        "Пример: `123456789`\n\n"
        "⚠️ *Внимание:* Пользователь не сможет пользоваться ботом после бана.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_admin_back_keyboard()
    )
    
    return States.ADMIN_BAN_USER


async def handle_admin_ban_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ввода ID для бана пользователя"""
    if not is_admin(update.effective_user.id):
        return
    
    try:
        telegram_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Неверный формат. Введите числовой Telegram ID.")
        return States.ADMIN_BAN_USER
    
    user = db.get_user_by_telegram_id(telegram_id)
    
    if not user:
        await update.message.reply_text(f"❌ Пользователь с ID {telegram_id} не найден.")
        return States.ADMIN_MENU
    
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
    
    return States.ADMIN_MENU


async def handle_admin_send_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка отправки сообщения пользователям"""
    if not is_admin(update.effective_user.id):
        return
    
    await update.message.reply_text(
        "📨 *ОТПРАВКА СООБЩЕНИЯ*\n\n"
        "Введите сообщение для отправки всем пользователям:\n\n"
        "Или укажите Telegram ID для отправки конкретному пользователю:\n"
        "`123456789 Ваше сообщение`\n\n"
        "Для отправки всем просто напишите сообщение.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_admin_back_keyboard()
    )
    
    return States.ADMIN_SEND_MESSAGE


async def handle_admin_message_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ввода сообщения для отправки"""
    if not is_admin(update.effective_user.id):
        return
    
    message_text = update.message.text.strip()
    
    if not message_text:
        await update.message.reply_text("❌ Сообщение не может быть пустым.")
        return States.ADMIN_SEND_MESSAGE
    
    # Проверяем, указан ли конкретный пользователь
    parts = message_text.split(' ', 1)
    
    if len(parts) == 2 and parts[0].isdigit():
        # Отправка конкретному пользователю
        telegram_id = int(parts[0])
        message = parts[1]
        
        user = db.get_user_by_telegram_id(telegram_id)
        
        if not user:
            await update.message.reply_text(f"❌ Пользователь с ID {telegram_id} не найден.")
            return States.ADMIN_MENU
        
        try:
            # Пытаемся отправить сообщение пользователю
            await context.bot.send_message(
                chat_id=telegram_id,
                text=f"📨 *СООБЩЕНИЕ ОТ АДМИНИСТРАТОРА*\n\n{message}",
                parse_mode=ParseMode.MARKDOWN
            )
            
            # Записываем в базу данных
            admin_user = db.get_user_by_telegram_id(update.effective_user.id)
            db.create_admin_message(admin_user['user_id'], user['user_id'], message)
            
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
        
        # Получаем всех активных пользователей
        all_users = db.get_all_users(limit=1000)  # Ограничим для теста
        
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
                    
                    # Делаем небольшую паузу, чтобы не спамить
                    await asyncio.sleep(0.1)
                except Exception as e:
                    failed_count += 1
        
        # Записываем в базу данных (общая рассылка)
        admin_user = db.get_user_by_telegram_id(update.effective_user.id)
        db.create_admin_message(admin_user['user_id'], None, message)
        
        await update.message.reply_text(
            f"✅ *Рассылка завершена!*\n\n"
            f"📊 Результаты:\n"
            f"• Отправлено: {sent_count}\n"
            f"• Не отправлено: {failed_count}\n"
            f"• Всего: {len(all_users)}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_admin_back_keyboard()
        )
    
    return States.ADMIN_MENU


async def resolve_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для разрешения жалобы"""
    if not is_admin(update.effective_user.id):
        return
    
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "❌ Неверный формат команды.\n"
            "Используйте: `/resolve <ID_жалобы> <комментарий>`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    try:
        report_id = int(context.args[0])
        comment = ' '.join(context.args[1:])
    except ValueError:
        await update.message.reply_text("❌ ID жалобы должен быть числом.")
        return
    
    success = db.update_report_status(report_id, 'resolved', comment)
    
    if success:
        await update.message.reply_text(f"✅ Жалоба #{report_id} отмечена как разрешенная.")
    else:
        await update.message.reply_text(f"❌ Не удалось обновить жалобу #{report_id}.")


async def dismiss_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для отклонения жалобы"""
    if not is_admin(update.effective_user.id):
        return
    
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "❌ Неверный формат команды.\n"
            "Используйте: `/dismiss <ID_жалобы> <комментарий>`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    try:
        report_id = int(context.args[0])
        comment = ' '.join(context.args[1:])
    except ValueError:
        await update.message.reply_text("❌ ID жалобы должен быть числом.")
        return
    
    success = db.update_report_status(report_id, 'dismissed', comment)
    
    if success:
        await update.message.reply_text(f"✅ Жалоба #{report_id} отклонена.")
    else:
        await update.message.reply_text(f"❌ Не удалось обновить жалобу #{report_id}.")


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для бана пользователя"""
    if not is_admin(update.effective_user.id):
        return
    
    if not context.args:
        await update.message.reply_text(
            "❌ Неверный формат команды.\n"
            "Используйте: `/ban <Telegram_ID>`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    try:
        telegram_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Telegram ID должен быть числом.")
        return
    
    user = db.get_user_by_telegram_id(telegram_id)
    
    if not user:
        await update.message.reply_text(f"❌ Пользователь с ID {telegram_id} не найден.")
        return
    
    success = db.ban_user(telegram_id)
    
    if success:
        await update.message.reply_text(f"✅ Пользователь *{user['full_name']}* (ID: {telegram_id}) забанен.")
    else:
        await update.message.reply_text(f"❌ Не удалось забанить пользователя.")


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


# Остальные функции (handle_registration_photo, handle_registration_name_age, и т.д.)
# остаются без изменений, я добавлю только те, что необходимы для работы

async def handle_registration_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка фото при регистрации"""
    if not update.message.photo:
        await update.message.reply_text("📸 Пожалуйста, отправь фото.")
        return States.REG_PHOTO
    
    photo_file = await update.message.photo[-1].get_file()
    context.user_data['registration'] = {
        'profile_photos': [photo_file.file_id],
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
        
        context.user_data['registration']['full_name'] = name
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
    user = update.effective_user
    
    user_data = {
        'telegram_id': user.id,
        'username': user.username,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'full_name': reg_data.get('full_name', user.full_name),
        'age': reg_data.get('age'),
        'city': reg_data.get('city', 'Не указан'),
        'bio': reg_data.get('bio', ''),
        'gender': reg_data.get('gender', 'male'),
        'profile_photos': reg_data.get('profile_photos', []),
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
            
            profile_completion = db.get_user_profile_completion(user.id)
            
            await update.message.reply_text(
                f"🎉 *РЕГИСТРАЦИЯ ЗАВЕРШЕНА!*\n\n"
                f"🔥 Добро пожаловать, {user_data['full_name']}!\n\n"
                f"📊 *ТВОЙ ПРОФИЛЬ:*\n"
                f"• 👤 {user_data['full_name']}, {user_data['age']}\n"
                f"• 📍 {user_data['city']}\n\n"
                f"⚡️ *СТАТИСТИКА:*\n"
                f"• ❤️ {LIKES_PER_DAY_FREE} лайков в день\n"
                f"• 📈 Заполненность профиля: {profile_completion['percentage']}%\n\n"
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
            "• Зайти позже\n"
            "• Расширить радиус поиска\n"
            "• Активнее заполнить свой профиль\n\n"
            "🔥 Новые пользователи появляются каждый день!",
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
            f"📊 *Статистика:*\n"
            f"• ❤️ Лайков сегодня: {user_stats['likes_given_today']}/{LIKES_PER_DAY_FREE}\n"
            f"• 💌 Всего лайков отправлено: {user_stats['likes_given']}",
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
            success = db.create_report(update.effective_user.id, reported_user_id, reason)
            
            if success:
                await update.message.reply_text(
                    "✅ *Жалоба отправлена администраторам.*\n\n"
                    "Спасибо за помощь в поддержании сообщества!"
                )
            else:
                await update.message.reply_text(
                    "❌ *Не удалось отправить жалобу.*\n"
                    "Попробуйте позже."
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
    
    db.update_last_seen(user['telegram_id'])
    
    user_stats = db.get_user_stats(user['telegram_id'])
    profile_completion = db.get_user_profile_completion(user['telegram_id'])
    
    text = f"📊 *ТВОЙ ПРОФИЛЬ*\n\n"
    text += f"🔥 *{user['full_name']}, {user['age']}*\n"
    
    if user['gender'] == 'male':
        text += "👨 Мужчина\n"
    elif user['gender'] == 'female':
        text += "👩 Женщина\n"
    
    text += f"📍 {user['city'] or 'Город не указан'}\n"
    
    if user['bio']:
        text += f"\n*О СЕБЕ:*\n{user['bio']}\n\n"
    
    text += f"⚡️ *СТАТИСТИКА:*\n"
    text += f"• ❤️ Лайков сегодня: {user_stats['likes_given_today']}/{LIKES_PER_DAY_FREE}\n"
    text += f"• 💌 Тебя лайкнули: {user_stats['likes_received']} чел.\n"
    text += f"• 🤝 Взаимных лайков: {user_stats['mutual_likes']}\n"
    text += f"• 👀 Просмотров профиля: {user_stats['profile_views']}\n"
    text += f"• 📈 Заполненность: {profile_completion['percentage']}%\n"
    text += f"• 🔥 Активен: {'✅ ДА' if user['is_active'] else '❌ НЕТ'}\n\n"
    
    if profile_completion['percentage'] < 80:
        text += "⚡️ *Совет:* Заполни профиль на 100% для лучших результатов!\n\n"
    
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
        'profile_photos': photos
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
    
    return States.EDIT_PHOTO


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
    
    if users_who_liked_me and users_who_liked_me[0]['profile_photos']:
        photo = users_who_liked_me[0]['profile_photos'][0]
        
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
        user_stats = db.get_user_stats(user['telegram_id'])
        profile_completion = db.get_user_profile_completion(user['telegram_id'])
        
        welcome_text = f"🔥 *ГЛАВНОЕ МЕНЮ*\n\n"
        welcome_text += f"Привет, {user['full_name'] or 'друг'}!\n\n"
        welcome_text += f"⚡️ *Статус:* БАЗОВЫЙ\n"
        welcome_text += f"❤️ Лайков сегодня: {user_stats['likes_given_today']}/{LIKES_PER_DAY_FREE}\n"
        welcome_text += f"💌 Тебя лайкнули: {len(users_who_liked_me)} чел.\n"
        welcome_text += f"📈 Заполненность профиля: {profile_completion['percentage']}%\n\n"
        welcome_text += f"🎯 *Что делаем?*"
        
        await update.message.reply_text(
            welcome_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
        
        db.update_last_seen(user['telegram_id'])
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
    /delete - Удалить свою анкету
    /admin - Админ-панель (только для администраторов)
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
    • 📊 Подробная статистика

    ⚠️ *ПРАВИЛА:*
    • 🙏 Будь вежлив и уважителен
    • 🚫 Не спамь
    • 🔒 Не передавай личные данные сразу
    • 📢 Сообщай о нарушениях

    📞 *ПОДДЕРЖКА:* @w33RY
    """
    
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)


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
    elif text == "🗑️ Удалить анкету":
        await delete_command(update, context)
    elif text == "❤️ Лайк":
        await handle_like_action(update, context)
    elif text == "➡️ Дальше":
        await handle_next_action(update, context)
    elif text == "🚫 Пожаловаться":
        await handle_report_action(update, context)
    elif text == "🔙 В меню" or text == "🔙 Назад в меню":
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
    elif text == "🔙 Главное меню":
        await update.message.reply_text("⚡️ Возвращаю в главное меню...", reply_markup=get_quick_actions_keyboard())
        await main_menu_command(update, context)
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
        # Проверяем различные состояния
        if context.user_data.get('confirming_delete'):
            await handle_delete_confirmation(update, context)
        elif context.user_data.get('reporting'):
            await handle_report_text(update, context)
        else:
            # Проверяем состояния админ-панели
            user_state = context.user_data.get('user_state')
            if user_state == States.ADMIN_SEARCH_USER:
                await handle_admin_search_input(update, context)
            elif user_state == States.ADMIN_BAN_USER:
                await handle_admin_ban_input(update, context)
            elif user_state == States.ADMIN_SEND_MESSAGE:
                await handle_admin_message_input(update, context)
            else:
                await update.message.reply_text(
                    "Используй кнопки меню для навигации или команды:\n"
                    "/start - Главное меню\n"
                    "/admin - Админ-панель (для администраторов)\n"
                    "/delete - Удалить анкету\n"
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
    
    
    admin_handler = ConversationHandler(
        entry_points=[
            CommandHandler("admin", admin_command)
        ],
        states={
            States.ADMIN_MENU: [
                MessageHandler(filters.TEXT & filters.Regex("^📊 Статистика$"), handle_admin_stats),
                MessageHandler(filters.TEXT & filters.Regex("^👥 Все пользователи$"), handle_admin_all_users),
                MessageHandler(filters.TEXT & filters.Regex("^⚠️ Жалобы$"), handle_admin_reports),
                MessageHandler(filters.TEXT & filters.Regex("^🔍 Найти пользователя$"), handle_admin_search_user),
                MessageHandler(filters.TEXT & filters.Regex("^🚫 Забанить$"), handle_admin_ban_user),
                MessageHandler(filters.TEXT & filters.Regex("^📨 Отправить сообщение$"), handle_admin_send_message),
                MessageHandler(filters.TEXT & filters.Regex("^🔙 Главное меню$"), main_menu_command),
            ],
            States.ADMIN_SEARCH_USER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_search_input)
            ],
            States.ADMIN_BAN_USER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_ban_input)
            ],
            States.ADMIN_SEND_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_message_input)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )
    
    
    application.add_handler(registration_handler)
    application.add_handler(edit_profile_handler)
    application.add_handler(admin_handler)
    application.add_handler(CommandHandler("delete", delete_command))
    application.add_handler(CommandHandler("resolve", resolve_report_command))
    application.add_handler(CommandHandler("dismiss", dismiss_report_command))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("profile", profile_command))
    application.add_handler(CommandHandler("browse", browse_profiles_command))
    application.add_handler(CommandHandler("start", main_menu_command))
    
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_quick_buttons))
    
    
    print("БОТ ЗАПУЩЕН")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
