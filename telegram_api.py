import os
import json
import requests
import logging

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/" if TELEGRAM_TOKEN else None

logger = logging.getLogger(__name__)


def send_message(chat_id, text, reply_markup=None, use_html=True):
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN не задан")
        return False
    if len(text) > 4096:
        text = text[:4093] + "..."
        logger.warning("Сообщение обрезано до 4096 символов для chat_id %s", chat_id)
    payload = {
        "chat_id": chat_id,
        "text": text
    }
    if use_html:
        payload["parse_mode"] = "HTML"
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    logger.info("Отправка сообщения в %s: %s", chat_id, text[:50])
    response = requests.post(f"{TELEGRAM_URL}sendMessage", json=payload)
    if response.status_code != 200:
        logger.error("Ошибка отправки: %s", response.text)
        return False
    logger.info("Сообщение успешно отправлено")
    return True


def send_file(chat_id, file_path):
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN не задан")
        return False
    with open(file_path, 'rb') as f:
        files = {'document': (os.path.basename(file_path), f)}
        response = requests.post(f"{TELEGRAM_URL}sendDocument", data={'chat_id': chat_id}, files=files)
    if response.status_code != 200:
        logger.error("Ошибка отправки файла: %s", response.text)
        return False
    logger.info("Файл %s отправлен в %s", file_path, chat_id)
    return True


def can_post_to_channel(channel_id):
    response = requests.get(f"{TELEGRAM_URL}getChatMember", params={
        "chat_id": channel_id,
        "user_id": requests.get(f"{TELEGRAM_URL}getMe").json()["result"]["id"]
    })
    if response.status_code == 200:
        status = response.json()["result"]["status"]
        return status in ["administrator", "creator"]
    logger.error("Ошибка проверки прав для %s: %s", channel_id, response.text)
    return False
