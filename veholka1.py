BOT_TOKEN = '' 

SAVE_DIR = 

import os
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode 


if not os.path.exists(SAVE_DIR):
    os.makedirs(SAVE_DIR, exist_ok=True) 

async def hello(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_name = update.effective_user.first_name
    user_id = update.effective_user.id
    
    with open("id.txt", "a") as f:
        f.write(str(user_id) + '\n')
    await update.message.reply_text(f"Привет, {user_name}!")


async def send_cat_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет фото кота в ответ на слово 'кот'."""
    cat_photo_id = "AgACAgIAAxkBAAM0aTa_73BB7XTJnSMJeGcb4i-D6iIAAvAPaxvRKrBJsVl0AAFTRHhCAQADAgADeQADNgQ"
    
    await update.message.reply_photo(
        photo=cat_photo_id,
        caption="Вот вам котик!"
    )

async def send_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет стикер в ответ на слово 'стикер'."""

    sticker_file_id = "5429327717185110129.webp"
    
    await update.message.reply_sticker(sticker=sticker_file_id)


async def send_meow_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет голосовое сообщение (мяу) в ответ на слово 'мяу'."""

    meow_voice_file_id = "5312473687264365737.ogg" 
    
    await update.message.reply_voice(voice=meow_voice_file_id)



async def send_evaluation_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет текст оценки в ответ на слово 'оценка'."""
    evaluation_text = "Черепанов Максим и Балобанов Богдан"
    await update.message.reply_text(evaluation_text)


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_name = update.effective_user.first_name
    
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        extension = 'jpg'
        media_type = 'фото'
    elif update.message.video:
        file_id = update.message.video.file_id
        extension = 'mp4' 
        media_type = 'видео'
    else:
        return

    new_file = await context.bot.get_file(file_id)
    
    file_name = f"{file_id}.{extension}"
    file_path_on_disk = os.path.join(SAVE_DIR, file_name)

    await new_file.download_to_drive(file_path_on_disk)
    
    await update.message.reply_text(
        f"Спасибо, {user_name}! Я сохранил ваше фото-видео"
    )
    

def main() -> None:
    """Запускает бота."""
   
    application = Application.builder().token(BOT_TOKEN).build()
    

    hello_handler = MessageHandler(filters.TEXT & filters.Regex(r'(?i)^привет$'), hello)
    application.add_handler(hello_handler)

    media_handler = MessageHandler(filters.PHOTO | filters.VIDEO, handle_media)
    application.add_handler(media_handler)


    cat_handler = MessageHandler(filters.TEXT & filters.Regex(r'(?i)кот'), send_cat_photo)
    application.add_handler(cat_handler)

    sticker_handler = MessageHandler(filters.TEXT & filters.Regex(r'(?i)стикер'), send_sticker)
    application.add_handler(sticker_handler)
    
    voice_handler = MessageHandler(filters.TEXT & filters.Regex(r'(?i)мяу'), send_meow_voice)
    application.add_handler(voice_handler)
   
   
    evaluation_handler = MessageHandler(filters.TEXT & filters.Regex(r'(?i)оценка'), send_evaluation_text)
    application.add_handler(evaluation_handler)
 

    print("Бот запущен и слушает обновления...")
    application.run_polling(poll_interval=3.0)

if __name__ == "__main__":
    main()
