import json
import logging
import requests
import sqlite3
from flask import Flask, request

import database as db
import feeds
import telegram_api as tg
from llm import last_llm_response

app = Flask(__name__)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

db.init_db()


@app.route('/ping', methods=['GET'])
def ping():
    return "OK", 200


@app.route('/webhook', methods=['POST'])
def webhook():
    update = request.get_json()
    if not update or 'message' not in update or 'message_id' not in update['message']:
        return "OK", 200

    message = update['message']
    chat_id = message['chat']['id']
    text = message.get('text', '')
    username = message['from'].get('username')

    if not username:
        tg.send_message(chat_id, "У вас нет username. Установите его в настройках Telegram.")
        return "OK", 200

    user_channel = db.get_channel_by_admin(username)

    if not text.startswith('/'):
        return "OK", 200

    command, *rest = text.split(maxsplit=1)
    command = command.lower()
    arg = rest[0] if rest else ''

    if command == '/start':
        channel_id = None
        if message['chat'].get('type') == 'channel':
            channel_id = str(chat_id)
        elif arg:
            channel_id = arg

        if user_channel and not channel_id:
            tg.send_message(chat_id, f"Канал уже привязан: {user_channel}")
        elif channel_id:
            if tg.can_post_to_channel(channel_id):
                db.save_channel(channel_id, username)
                tg.send_message(chat_id, f"Канал {channel_id} привязан к @{username}")
            else:
                tg.send_message(chat_id, "Бот не имеет прав администратора в указанном канале")
        else:
            tg.send_message(chat_id, "Укажите канал командой /start @channel или отправьте команду из канала")

    elif command == '/startposting':
        if not user_channel:
            tg.send_message(chat_id, "Канал не привязан. Используйте /start в канале")
        elif feeds.posting_active:
            tg.send_message(chat_id, "Постинг уже запущен")
        else:
            feeds.start_posting_thread()
            tg.send_message(chat_id, "Постинг запущен")

    elif command == '/stopposting':
        if feeds.posting_active:
            feeds.stop_posting_thread()
            tg.send_message(chat_id, "Постинг остановлен")
        else:
            tg.send_message(chat_id, "Постинг и так не активен")

    elif command == '/setinterval':
        seconds = feeds.parse_interval(arg)
        if seconds:
            feeds.posting_interval = seconds
            feeds.next_post_event.set()
            tg.send_message(chat_id, f"Интервал обновлён: {arg}")
        else:
            tg.send_message(chat_id, "Неверный формат. Пример: /setinterval 1h 30m")

    elif command == '/nextpost':
        feeds.next_post_event.set()
        tg.send_message(chat_id, "Следующий пост скоро будет опубликован")

    elif command == '/skiprss':
        feeds.current_index = (feeds.current_index + 1) % len(feeds.RSS_URLS)
        feeds.next_post_event.set()
        tg.send_message(chat_id, "Следующий RSS-источник пропущен")

    elif command == '/changellm':
        if arg:
            db.set_model(arg)
            tg.send_message(chat_id, f"Модель изменена на {arg}")
        else:
            tg.send_message(chat_id, "Укажите модель, например /changellm gpt-4o-mini")

    elif command == '/editprompt':
        if arg:
            db.set_prompt(arg)
            tg.send_message(chat_id, "Промпт обновлён")
        else:
            tg.send_message(chat_id, "Используйте /editprompt <новый промпт>")

    elif command == '/sqlitebackup':
        tg.send_file(chat_id, db.DB_FILE)

    elif command == '/sqliteupdate':
        if 'document' in message:
            file_id = message['document']['file_id']
            info_resp = requests.get(
                f"{tg.TELEGRAM_URL}getFile", params={'file_id': file_id}
            )
            if info_resp.status_code != 200:
                tg.send_message(chat_id, "Ошибка запроса getFile")
                return "OK", 200
            info = info_resp.json()
            file_path = info.get('result', {}).get('file_path')
            if file_path:
                file_resp = requests.get(
                    f"https://api.telegram.org/file/bot{tg.TELEGRAM_TOKEN}/{file_path}"
                )
                if file_resp.status_code != 200:
                    tg.send_message(chat_id, "Ошибка скачивания файла")
                    return "OK", 200
                with open(db.DB_FILE, 'wb') as f:
                    f.write(file_resp.content)
                tg.send_message(chat_id, "База обновлена")
            else:
                tg.send_message(chat_id, "Не удалось получить файл")
        else:
            tg.send_message(chat_id, "Отправьте SQLite файл как документ с подписью /sqliteupdate")

    elif command == '/info':
        tg.send_message(chat_id, feeds.get_status(username))

    elif command == '/errinf':
        conn = sqlite3.connect(db.DB_FILE)
        c = conn.cursor()
        c.execute("SELECT timestamp, message, link FROM errors ORDER BY id DESC LIMIT 5")
        rows = c.fetchall()
        conn.close()
        if rows:
            msg = '\n\n'.join(f"{t}\n{m}\n{l}" for t, m, l in rows)
        else:
            msg = "Ошибок нет"
        tg.send_message(chat_id, msg, use_html=False)

    elif command == '/errnotification':
        if arg in ['on', 'off']:
            db.set_error_notifications(arg)
            tg.send_message(chat_id, f"Уведомления об ошибках: {arg}")
        else:
            tg.send_message(chat_id, "Использование: /errnotification <on|off>")

    elif command == '/feedcache':
        conn = sqlite3.connect(db.DB_FILE)
        c = conn.cursor()
        c.execute("SELECT title, link FROM feedcache ORDER BY timestamp DESC LIMIT 5")
        rows = c.fetchall()
        conn.close()
        if rows:
            msg = '\n\n'.join(f"{t}\n{l}" for t, l in rows)
        else:
            msg = "Кэш пуст"
        tg.send_message(chat_id, msg, use_html=False)

    elif command == '/feedcacheclear':
        conn = sqlite3.connect(db.DB_FILE)
        conn.execute("DELETE FROM feedcache")
        conn.commit()
        conn.close()
        tg.send_message(chat_id, "Кэш очищен")

    elif command == '/addadmin':
        if not user_channel:
            tg.send_message(chat_id, "Канал не привязан")
        elif arg:
            if db.add_admin(user_channel, arg.lstrip('@'), username):
                tg.send_message(chat_id, f"Админ {arg} добавлен")
            else:
                tg.send_message(chat_id, "Не удалось добавить админа")
        else:
            tg.send_message(chat_id, "Использование: /addadmin <username>")

    elif command == '/removeadmin':
        if not user_channel:
            tg.send_message(chat_id, "Канал не привязан")
        elif arg:
            if db.remove_admin(user_channel, arg.lstrip('@'), username):
                tg.send_message(chat_id, f"Админ {arg} удалён")
            else:
                tg.send_message(chat_id, "Не удалось удалить админа")
        else:
            tg.send_message(chat_id, "Использование: /removeadmin <username>")

    elif command == '/debug':
        if last_llm_response:
            tg.send_message(chat_id, json.dumps(last_llm_response, ensure_ascii=False), use_html=False)
        else:
            tg.send_message(chat_id, "Нет сохранённого ответа LLM")

    elif command == '/help':
        tg.send_message(chat_id, feeds.get_help())

    else:
        tg.send_message(chat_id, "Неизвестная команда. Используйте /help")

    return "OK", 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
