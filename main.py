from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv
import os
import requests
import asyncio
import logging
from datetime import date
from database import get_limit_data, set_limit_data

# НОВЫЕ ИМПОРТЫ ДЛЯ ВЕБХУКОВ
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
# END НОВЫЕ ИМПОРТЫ

load_dotenv()

# --- КОНФИГУРАЦИЯ ДЛЯ ВЕБХУКОВ ---
# Render автоматически предоставляет порт через переменную окружения PORT
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = os.getenv("PORT", 8080) # Используем PORT, если есть, иначе 8080
WEBHOOK_PATH = "/webhook" # Это путь, по которому Telegram будет отправлять обновления
# Render предоставит базовый URL вашего сервиса через переменную окружения WEBHOOK_URL.
# На Render это обычно переменная SERVICE_URL или PUBLIC_URL.
# Мы будем использовать WEBHOOK_URL для простоты, как мы ее назовем в Render.
WEBHOOK_URL_BASE = os.getenv("WEBHOOK_URL")
WEBHOOK_URL = f"{WEBHOOK_URL_BASE}{WEBHOOK_PATH}" # Полный URL для Telegram

# Рекомендуется использовать секретный токен для вебхуков для безопасности
# Вы должны будете установить эту переменную окружения в Render.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "ваш_очень_секретный_токен") # ЗАМЕНИТЕ НА РЕАЛЬНЫЙ СЕКРЕТНЫЙ ТОКЕН

# Ваши API-ключи (используем тот же TELEGRAM_BOT_TOKEN для тестового бота)
API_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

DAILY_LIMIT = 1500
LANGUAGES = {
    'ru': 'Русский',
    'pt': 'Português',
    'fr': 'Français',
    'en': 'English'
}

# Logging setup for debugging
logging.basicConfig(level=logging.INFO)

# Using a dictionary to store message data in order to preserve state
message_data_store = {}


def check_and_reset_limit():
    today = date.today().isoformat()
    saved_date, count = get_limit_data()
    if saved_date != today:
        set_limit_data(today, 0)
        return 0
    return count


def increment_limit():
    today = date.today().isoformat()
    saved_date, count = get_limit_data()
    count += 1
    set_limit_data(today, count)
    return count


async def translate_text_gemini(text: str, target_lang: str) -> str:
    url = 'https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent'

    headers = {
        'Content-Type': 'application/json'
    }
    params = {
        'key': GEMINI_API_KEY
    }

    # Prompt for translation
    prompt = f'Translate this text to {LANGUAGES[target_lang]} exactly, return ONLY the translated text, no additional words or explanations:\n\n"{text}"'

    # Request structure for Gemini 1.5 Flash
    json_data = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.0,  # translation accuracy
            "maxOutputTokens": 512  # Maximum number of tokens in the response
        }
    }

    try:
        response = requests.post(url, headers=headers, params=params, json=json_data)
        response.raise_for_status()  # Raises an exception for HTTP errors (4xx or 5xx)

        resp_json = response.json()

        # Response parsing for Gemini 1.5 Flash
        if 'candidates' in resp_json and resp_json['candidates']:
            for candidate in resp_json['candidates']:
                if 'content' in candidate and 'parts' in candidate['content'] and candidate['content']['parts']:
                    return candidate['content']['parts'][0]['text'].strip()
            logging.warning(f"Gemini API returned no usable content in candidates: {resp_json}")
            return '[Error: No translation candidates found in Gemini response]'
        else:
            logging.warning(f"Gemini API returned no candidates for translation: {resp_json}")
            return '[Error: No translation candidates from Gemini API]'
    except requests.exceptions.RequestException as e:
        logging.error(f"Error calling Gemini API: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logging.error(f"Gemini API full error response: {e.response.text}")
        return f'[Error in translation API: {e}]'
    except Exception as e:
        logging.error(f"Unexpected exception during Gemini translation: {e}")
        return f'[Exception: {e}]'


@dp.message()
async def handle_message(message: types.Message):
    count = check_and_reset_limit()
    if count >= DAILY_LIMIT:
        await message.reply("Daily translation limit reached. The bot is ‘sleeping’ until tomorrow.")
        return

    original_text = message.text
    if not original_text:
        return

    increment_limit()

    buttons = []
    for code, name in LANGUAGES.items():
        flag = {'ru': '🇷🇺', 'pt': '🇧🇷', 'fr': '🇫🇷', 'en': '🇬🇧'}.get(code, '')
        buttons.append(InlineKeyboardButton(text=f'{flag} {name}', callback_data=f'translate_{code}'))

    keyboard_rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

    sent = await message.answer(
        '💬 Translation available:\nSelect a language:',
        reply_markup=keyboard
    )
    message_data_store[(sent.chat.id, sent.message_id)] = {
        'original_text': original_text
    }


@dp.callback_query(lambda c: c.data and c.data.startswith('translate_'))
async def process_translate(callback: types.CallbackQuery):
    target_lang = callback.data.split('_')[1]
    key = (callback.message.chat.id, callback.message.message_id)

    data = message_data_store.get(key)
    if not data:
        await callback.answer("Original message not found or has expired.", show_alert=True)
        return

    original_text = data['original_text']

    await callback.answer("Translating…", show_alert=False)

    translation = await translate_text_gemini(original_text, target_lang)

    text = f'{LANGUAGES[target_lang]} Translation: {translation}\n\n🔁 Show in another language:'

    buttons = []
    for code, name in LANGUAGES.items():
        if code != target_lang:
            flag = {'ru': '🇷🇺', 'pt': '🇧🇷', 'fr': '🇫🇷', 'en': '🇬🇧'}.get(code, '')
            buttons.append(InlineKeyboardButton(text=f'{flag} {name}', callback_data=f'translate_{code}'))

    keyboard_rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


# --- НОВЫЕ ФУНКЦИИ ЗАПУСКА ДЛЯ ВЕБХУКОВ ---
async def on_startup(dispatcher: Dispatcher, bot: Bot) -> None:
    # Установка вебхука при старте приложения
    # Сначала удалим старый вебхук (если он был), чтобы избежать конфликтов
    await bot.delete_webhook(drop_pending_updates=True) # Удаляем старый и все ожидающие обновления
    await bot.set_webhook(
        WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True # Также очистим ожидающие обновления при установке нового вебхука
    )
    logging.info(f"Webhook set to: {WEBHOOK_URL}")

async def on_shutdown(dispatcher: Dispatcher, bot: Bot) -> None:
    # Удаление вебхука при завершении работы приложения
    await bot.delete_webhook()
    logging.info("Webhook deleted.")
# END НОВЫЕ ФУНКЦИИ ЗАПУСКА

# --- ИЗМЕНЕННЫЙ ГЛАВНЫЙ БЛОК ЗАПУСКА ---
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    # Привязываем функции on_startup и on_shutdown к диспетчеру
    # Они будут вызваны aiogram при запуске/остановке веб-приложения
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # Создаем aiohttp приложение
    app = web.Application()

    # Создаем обработчик запросов для вебхука aiogram
    # Path - это путь, по которому Telegram будет отправлять обновления
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
    )
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)

    # Привязываем диспетчер aiogram к aiohttp приложению
    # Это позволяет aiogram управлять жизненным циклом бота через веб-приложение
    setup_application(app, dp, bot=bot)

    # Запускаем веб-сервер aiohttp
    logging.info(f"Starting web server on {WEB_SERVER_HOST}:{WEB_SERVER_PORT}")
    web.run_app(app, host=WEB_SERVER_HOST, port=WEB_SERVER_PORT)