import os
import logging
import pytesseract
import tempfile
import base64
import json
import requests

from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from pdf2image import convert_from_path
import openai

# ============ Настройки ============
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SERVICE_ACCOUNT_JSON_BASE64 = os.getenv("SERVICE_ACCOUNT_JSON_BASE64")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
FOLDER_ID = os.getenv("FOLDER_ID")

# Проверка наличия необходимых переменных окружения
required_env_vars = [
    "TELEGRAM_BOT_TOKEN",
    "OPENAI_API_KEY",
    "SERVICE_ACCOUNT_JSON_BASE64",
    "SPREADSHEET_ID",
    "FOLDER_ID"
]

for var in required_env_vars:
    if not os.getenv(var):
        raise EnvironmentError(f"{var} is not set")

# Декодируем service_account.json из Base64 и сохраняем локально
try:
    decoded_json = base64.b64decode(SERVICE_ACCOUNT_JSON_BASE64)
    with open("service_account.json", "wb") as f:
        f.write(decoded_json)
except base64.binascii.Error as e:
    raise EnvironmentError("Failed to decode SERVICE_ACCOUNT_JSON_BASE64") from e

openai.api_key = OPENAI_API_KEY


def test_openai_connection():
    try:
        response = requests.get("https://api.openai.com/v1/models", headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}"
        }, timeout=10)
        if response.status_code == 200:
            logger.info("Успешное подключение к OpenAI API.")
        else:
            logger.error(f"Ошибка подключения к OpenAI API: {response.status_code} - {response.text}")
    except Exception as e:
        logger.error(f"Не удалось подключиться к OpenAI API: {e}")

# Логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Настройка Google API через сервисный аккаунт
SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
creds = Credentials.from_service_account_file('service_account.json', scopes=SCOPES)
gs_client = gspread.authorize(creds)
sheet = gs_client.open_by_key(SPREADSHEET_ID).sheet1
drive_service = build('drive', 'v3', credentials=creds)

def start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "Привет! Отправь мне счет (изображение или PDF), я распознаю текст, извлеку нужную информацию и сохраню ее в таблицу и файл на Диск."
    )

def handle_file(update: Update, context: CallbackContext):
    # Получаем файл (документ или фото)
    file = update.message.document or (update.message.photo[-1] if update.message.photo else None)
    if file is None:
        update.message.reply_text("Не удалось определить файл.")
        return

    with tempfile.NamedTemporaryFile(delete=False) as f:
        downloaded_path = f.name
        context.bot.get_file(file.file_id).download(custom_path=downloaded_path)

    # Проверяем тип файла
    is_pdf = False
    if update.message.document and 'pdf' in (update.message.document.mime_type or '').lower():
        is_pdf = True

    # Выполняем OCR
    if is_pdf:
        try:
            images = convert_from_path(downloaded_path)
        except Exception as e:
            logger.error(f"Failed to convert PDF to images: {e}")
            update.message.reply_text("Произошла ошибка при обработке PDF файла.")
            os.remove(downloaded_path)
            return

        text_pages = []
        for img in images:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as img_temp:
                img.save(img_temp.name, 'PNG')
                try:
                    page_text = pytesseract.image_to_string(img_temp.name, lang='rus+eng')
                except Exception as e:
                    logger.error(f"OCR failed on image {img_temp.name}: {e}")
                    page_text = ""
                text_pages.append(page_text)
                os.remove(img_temp.name)
        ocr_text = "\n".join(text_pages)
    else:
        try:
            ocr_text = pytesseract.image_to_string(downloaded_path, lang='rus+eng')
        except Exception as e:
            logger.error(f"OCR failed on image {downloaded_path}: {e}")
            update.message.reply_text("Произошла ошибка при распознавании текста.")
            os.remove(downloaded_path)
            return

    # Промпт для OpenAI
    prompt = f"""
You are a helpful assistant that extracts structured data from invoices.

I will provide you with raw text of an invoice. You will respond ONLY with JSON containing the following fields:
- supplier: The supplier's name (string)
- date: The invoice date in YYYY-MM-DD format if possible, or raw date if can't parse
- total: The total amount (float or string if can't parse as float)
- vat: The VAT amount (float or string if can't parse as float)

Do not include any extra text outside the JSON and do not use code blocks. Here is the invoice text:
{ocr_text}
    """

    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt}
            ],
            temperature=0
        )
    except Exception as e:
        logger.error(f"OpenAI API request failed: {e}")
        update.message.reply_text("Произошла ошибка при обращении к OpenAI API.")
        os.remove(downloaded_path)
        return

    content = response.choices[0].message.content.strip()
    try:
        data = json.loads(content)
        supplier = data.get("supplier", "")
        date = data.get("date", "")
        total = data.get("total", "")
        vat = data.get("vat", "")
    except Exception as e:
        logger.error(f"JSON parse error: {e}. Response was: {content}")
        update.message.reply_text("Произошла ошибка при извлечении данных через AI.")
        os.remove(downloaded_path)
        return

    # Генерируем новое имя файла: supplier - date - total.(pdf/jpg)
    safe_supplier = supplier.replace("/", "_").replace("\\", "_")
    safe_total = str(total).replace("/", "_").replace("\\", "_")
    safe_date = date.replace("/", "-").replace("\\", "-")

    file_extension = ".pdf" if is_pdf else ".jpg"
    new_file_name = f"{safe_supplier} - {safe_date} - {safe_total}{file_extension}"

    # Загрузка файла на Google Диск
    file_metadata = {
        'name': new_file_name,
        'parents': [FOLDER_ID]
    }
    media = MediaFileUpload(downloaded_path, resumable=True)
    try:
        uploaded = drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        file_id = uploaded.get('id')
    except Exception as e:
        logger.error(f"Failed to upload file to Google Drive: {e}")
        update.message.reply_text("Произошла ошибка при загрузке файла на Google Диск.")
        os.remove(downloaded_path)
        return

    # Делаем файл общедоступным по ссылке
    try:
        drive_service.permissions().create(
            fileId=file_id,
            body={"role": "reader", "type": "anyone"},
            fields='id'
        ).execute()
    except Exception as e:
        logger.error(f"Failed to set permissions on Google Drive file: {e}")
        update.message.reply_text("Произошла ошибка при настройке доступа к файлу на Google Диске.")
        os.remove(downloaded_path)
        return

    file_link = f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"

    # Записываем данные в таблицу (предполагается, что в таблице колонки: A: Supplier, B: Date, C: Total, D: VAT, E: Link)
    try:
        sheet.append_row([supplier, date, total, vat, file_link])
    except Exception as e:
        logger.error(f"Failed to append row to Google Sheets: {e}")
        update.message.reply_text("Произошла ошибка при записи данных в Google Таблицу.")
        os.remove(downloaded_path)
        return

    update.message.reply_text("Данные извлечены и записаны в таблицу, файл переименован и сохранен на Диск.")

    os.remove(downloaded_path)

def main():
    # Создаём Updater и передаём ему токен вашего бота.
    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)

    # Получаем диспетчер для регистрации обработчиков
    dp = updater.dispatcher

    # Регистрируем обработчик команды /start
    dp.add_handler(CommandHandler("start", start))

    # Регистрируем обработчик сообщений с файлами (документы и фотографии)
    dp.add_handler(MessageHandler(Filters.document | Filters.photo, handle_file))

    # Начинаем получать обновления с помощью длинного опроса
    updater.start_polling()

    logger.info("Бот запущен и готов к работе.")

    # Вызовите функцию при запуске бота
    test_openai_connection()

    # Блокируем программу до остановки её пользователем
    updater.idle()

if __name__ == '__main__':
    main()
