
import os
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, ContextTypes, CallbackQueryHandler, CommandHandler,
    ConversationHandler, MessageHandler, filters
)

SAVE_DIR = 

if not os.path.exists(SAVE_DIR):
    os.makedirs(SAVE_DIR, exist_ok=True)

conn = sqlite3.connect(os.path.join(SAVE_DIR, 'files1.db'), check_same_thread=False)
cursor = conn.cursor()

cursor.execute('''
    CREATE TABLE IF NOT EXISTS registered_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER UNIQUE,
        user_name TEXT,
        user_username TEXT,
        registration_time DATETIME DEFAULT CURRENT_TIMESTAMP
    )
''')
conn.commit()

ADMIN_USER_ID = 

# Состояния для ConversationHandler
EDIT_NAME, EDIT_USERNAME = range(2)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    
    keyboard = [
        [InlineKeyboardButton("Регистрация", callback_data='register')]
    ]
    
    if user_id == ADMIN_USER_ID:
        keyboard.append([InlineKeyboardButton("Показать всех зарегистрированных", callback_data='show_all')])
    else:
        keyboard.append([InlineKeyboardButton("Изменить мои данные", callback_data='edit_data')])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Нажмите кнопку:", reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    await query.answer()

    if query.data == 'register':
        cursor.execute("SELECT 1 FROM registered_users WHERE user_id = ?", (user.id,))
        if cursor.fetchone():
            await query.edit_message_text("Вы уже зарегистрированы.")
            return

        cursor.execute(
            "INSERT INTO registered_users (user_id, user_name, user_username) VALUES (?, ?, ?)",
            (user.id, user.first_name or '', user.username or '')
        )
        conn.commit()
        await query.edit_message_text("Вы успешно зарегистрированы!")

    elif query.data == 'show_all':
        if user.id != ADMIN_USER_ID:
            await query.answer("Доступ запрещён", show_alert=True)
            return

        cursor.execute("SELECT user_id, user_name, user_username, registration_time FROM registered_users")
        rows = cursor.fetchall()

        if not rows:
            text = "Нет зарегистрированных пользователей"
        else:
            text = "Зарегистрированные пользователи:\n"
            for r in rows:
                user_id_, name, username, reg_time = r
                text += f"\nID: {user_id_}\nИмя: {name}\nUsername: @{username if username else '-'}\nРегистрация: {reg_time}\n"

        if len(text) > 4000:
            text = text[:3997] + "..."

        await query.edit_message_text(text)
    
    elif query.data == 'edit_data':
        cursor.execute("SELECT 1 FROM registered_users WHERE user_id = ?", (user.id,))
        if not cursor.fetchone():
            await query.edit_message_text("Вы не зарегистрированы, сначала зарегистрируйтесь.")
            return ConversationHandler.END

        await query.edit_message_text("Введите новое имя пользователя:")
        return EDIT_NAME

async def edit_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text.strip()
    context.user_data['new_name'] = new_name
    await update.message.reply_text("Введите новый username (без @, или оставьте пустым):")
    return EDIT_USERNAME

async def edit_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_username = update.message.text.strip().lstrip('@')
    user_id = update.message.from_user.id
    new_name = context.user_data.get('new_name', '')

    cursor.execute(
        "UPDATE registered_users SET user_name = ?, user_username = ? WHERE user_id = ?",
        (new_name, new_username, user_id)
    )
    conn.commit()
    await update.message.reply_text("Данные успешно обновлены.")
    return ConversationHandler.END

def main():
    # Замените ваш токен на актуальный
    application = Application.builder().token("").build()

    # Настраиваем обработчик диалога
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern='^edit_data$')],
        states={
            EDIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_name)],
            EDIT_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_username)],
        },
        fallbacks=[],
        per_message=False, # ВАЖНО: это уберет предупреждение и починит логику
    )

    # Добавляем обработчики в приложение
    application.add_handler(CommandHandler("start", start))
    # Важно: CallbackQueryHandler для регистрации должен быть ВНЕ диалога или в диалоге
    # Чтобы работали остальные кнопки, добавим общий обработчик:
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(button_handler))

    # Запуск бота
    application.run_polling()

if __name__ == '__main__':
    main()
