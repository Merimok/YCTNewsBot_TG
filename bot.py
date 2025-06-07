import os
import requests
import telegram_api
import json
import logging
import sqlite3
from flask import Flask, request

import feeds
from telegram_api import send_message, send_file, can_post_to_channel
from database import (
    DB_FILE,
    init_db,
    set_prompt,
    set_model,
    set_error_notifications,
    get_channel_by_admin,
    save_channel,
    add_admin,
    remove_admin,
)
from llm import last_llm_response

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

init_db()


@app.route('/ping', methods=['GET'])
def ping():
    logger.info("Получен пинг")
    return "OK", 200


@app.route('/webhook', methods=['POST'])
def webhook():
    logger.info("Получен запрос на /webhook")
    update = request.get_json()
    logger.info("Данные запроса: %s", json.dumps(update, ensure_ascii=False))

    if not update or 'message' not in update or 'message_id' not in update['message']:
        logger.error("Некорректный запрос")
        return "OK", 200

    chat_id = update['message']['chat']['id']
    message_text = update['message'].get('text', '')
    username = update['message']['from'].get('username')

    logger.info("Получена команда: '%s' от @%s в чате %s", message_text, username, chat_id)

    if not username:
        send_message(chat_id, "У вас нет username. Установите его в настройках Telegram.")
        return "OK", 200

    user_channel = get_channel_by_admin(username)

    if message_text == '/start':
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT channel_id FROM channels")
        channels = c.fetchall()
        conn.close()

        if user_channel:
            send_message(chat_id, f"Вы уже админ канала {user_channel}. Используйте /startposting для начала.")
        elif not channels:
            send_message(chat_id, "Укажите ID канала для постинга (например, @channelname или -1001234567890):")
        else:
            send_message(chat_id, "У вас нет прав на управление ботом. Обратитесь к администратору канала.")
    elif message_text.startswith('@') or message_text.startswith('-100'):
        channel_id = message_text
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT channel_id FROM channels")
        if c.fetchone():
            send_message(chat_id, "Канал уже привязан. У вас нет прав на его управление.")
        elif can_post_to_channel(channel_id):
            save_channel(channel_id, username)
            send_message(chat_id, f"Канал {channel_id} привязан. Вы создатель. Используйте /startposting для начала.")
        else:
            send_message(chat_id, "Бот не имеет прав администратора в этом канале.")
        conn.close()
    elif message_text == '/startposting':
        if user_channel:
            feeds.start_posting_thread()
            send_message(chat_id, f"Постинг начат в {user_channel}")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif message_text == '/stopposting':
        if user_channel:
            feeds.stop_posting_thread()
            send_message(chat_id, "Постинг остановлен")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif message_text.startswith('/setinterval'):
        if user_channel:
            try:
                interval_str = message_text.split()[1]
                new_interval = feeds.parse_interval(interval_str)
                if new_interval:
                    feeds.posting_interval = new_interval
                    send_message(chat_id, f"Интервал постинга установлен: {interval_str}")
                else:
                    send_message(chat_id, "Неверный формат. Используйте: /setinterval 34m, 1h, 2h 53m")
            except IndexError:
                send_message(chat_id, "Укажите интервал: /setinterval 34m")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif message_text == '/nextpost':
        if user_channel:
            if feeds.posting_active:
                feeds.next_post_event.set()
                send_message(chat_id, "Таймер сброшен. Следующий пост будет опубликован немедленно.")
            else:
                send_message(chat_id, "Постинг не активен. Сначала используйте /startposting.")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif message_text == '/skiprss':
        if user_channel:
            if feeds.posting_active:
                feeds.current_index = (feeds.current_index + 1) % len(feeds.RSS_URLS)
                send_message(chat_id, f"Следующий RSS пропущен. Новый текущий: {feeds.RSS_URLS[feeds.current_index]}")
            else:
                send_message(chat_id, "Постинг не активен. Сначала используйте /startposting.")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif message_text.startswith('/editprompt'):
        if user_channel:
            if len(message_text.split()) == 1:
                send_message(chat_id, "Отправьте новый промпт после команды, например:\n/editprompt Новый промпт здесь")
            else:
                new_prompt = message_text[len('/editprompt '):].strip()
                set_prompt(new_prompt)
                send_message(chat_id, "Промпт обновлён:\n" + new_prompt)
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif message_text.startswith('/changellm'):
        if user_channel:
            if len(message_text.split()) == 1:
                send_message(chat_id, "Укажите модель, например: /changellm gpt-4o-mini\nДоступные модели: см. https://platform.openai.com/docs/models")
            else:
                new_model = message_text.split()[1]
                set_model(new_model)
                send_message(chat_id, f"Модель изменена на: {new_model}")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif message_text == '/sqlitebackup':
        if user_channel:
            if os.path.exists(DB_FILE):
                send_file(chat_id, DB_FILE)
                send_message(chat_id, "База данных выгружена")
            else:
                send_message(chat_id, "База данных не найдена")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif message_text == '/sqliteupdate':
        if user_channel:
            send_message(chat_id, "Отправьте файл базы данных (feedcache.db) в ответ на это сообщение")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif 'reply_to_message' in update['message'] and update['message']['reply_to_message'].get('text', '') == "Отправьте файл базы данных (feedcache.db) в ответ на это сообщение":
        if user_channel:
            if 'document' in update['message']:
                file_id = update['message']['document']['file_id']
                file_name = update['message']['document']['file_name']
                if file_name != "feedcache.db":
                    send_message(chat_id, "Файл должен называться 'feedcache.db'")
                    return "OK", 200
                response = requests.get(f"{telegram_api.TELEGRAM_URL}getFile?file_id={file_id}")
                file_path = response.json()['result']['file_path']
                file_url = f"https://api.telegram.org/file/bot{telegram_api.TELEGRAM_TOKEN}/{file_path}"
                with open(DB_FILE, 'wb') as f:
                    f.write(requests.get(file_url).content)
                send_message(chat_id, "База данных обновлена")
            else:
                send_message(chat_id, "Прикрепите файл базы данных")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif message_text == '/info':
        if user_channel:
            send_message(chat_id, feeds.get_status(username))
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif message_text == '/errinf':
        if user_channel:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("SELECT timestamp, message, link FROM errors ORDER BY timestamp DESC LIMIT 10")
            errors = c.fetchall()
            conn.close()
            if not errors:
                send_message(chat_id, "Ошибок пока нет.")
            else:
                error_list = "\n".join([f"{ts} - {msg} (Ссылка: {link})" for ts, msg, link in errors])
                send_message(chat_id, f"Последние ошибки:\n{error_list}", use_html=False)
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif message_text.startswith('/errnotification'):
        if user_channel:
            try:
                state = message_text.split()[1].lower()
                if state in ['on', 'off']:
                    set_error_notifications(state)
                    send_message(chat_id, f"Уведомления об ошибках: {state}")
                else:
                    send_message(chat_id, "Используйте: /errnotification on или /errnotification off")
            except IndexError:
                send_message(chat_id, "Укажите состояние: /errnotification on или /errnotification off")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif message_text == '/feedcache':
        if user_channel:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("SELECT * FROM feedcache")
            rows = c.fetchall()
            conn.close()
            if not rows:
                send_message(chat_id, "Feedcache пуст")
            else:
                cache = [dict(zip(["id", "title", "summary", "link", "source", "timestamp"], row)) for row in rows]
                send_message(chat_id, "Содержимое feedcache:\n" + json.dumps(cache, ensure_ascii=False, indent=2)[:4096])
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif message_text == '/feedcacheclear':
        if user_channel:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("DELETE FROM feedcache")
            conn.commit()
            conn.close()
            send_message(chat_id, "Feedcache очищен")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif message_text.startswith('/addadmin'):
        if user_channel:
            try:
                new_admin = message_text.split()[1].lstrip('@')
                if add_admin(user_channel, new_admin, username):
                    send_message(chat_id, f"@{new_admin} добавлен как админ канала {user_channel}")
                else:
                    send_message(chat_id, "Вы не можете добавлять админов или пользователь уже админ.")
            except IndexError:
                send_message(chat_id, "Укажите username: /addadmin @username")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif message_text.startswith('/removeadmin'):
        if user_channel:
            try:
                admin_to_remove = message_text.split()[1].lstrip('@')
                if remove_admin(user_channel, admin_to_remove, username):
                    send_message(chat_id, f"@{admin_to_remove} удалён из админов канала {user_channel}")
                else:
                    send_message(chat_id, "Нельзя удалить создателя или вы не админ.")
            except IndexError:
                send_message(chat_id, "Укажите username: /removeadmin @username")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif message_text == '/debug':
        if user_channel:
            logger.info("Debug: Запрос от @%s для показа последнего ответа LLM", username)
            if last_llm_response:
                response_text = (
                    f"Последний сырой ответ LLM:\n\n"
                    f"Ссылка: {last_llm_response['link']}\n"
                    f"Время: {last_llm_response['timestamp']}\n\n"
                    f"{last_llm_response['response']}"
                )
                send_message(chat_id, response_text, use_html=False)
                logger.info("Debug: Последний ответ отправлен в %s", chat_id)
            else:
                send_message(chat_id, "Нет сохранённых ответов LLM. Попробуйте позже после обработки новости.")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif message_text == '/help':
        logger.info("Команда /help вызвана @%s", username)
        send_message(chat_id, feeds.get_help(), use_html=False)

    return "OK", 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
