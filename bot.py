import os
import requests
import telegram_api
import json
import logging
import sqlite3
from flask import Flask, request
from datetime import datetime, timedelta
import threading
import time
import re
import html
import hashlib
import feedparser
from openai import OpenAI

app = Flask(__name__)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/" if TELEGRAM_TOKEN else None
DB_FILE = "feedcache.db"

RSS_URLS = [
    "https://www.theverge.com/rss/index.xml",
    "https://www.windowslatest.com/feed/",
    "https://9to5google.com/feed/",
    "https://9to5mac.com/feed/",
    "https://www.androidcentral.com/feed",
    "https://arstechnica.com/feed/",
    "https://uk.pcmag.com/rss",
    "https://www.bleepingcomputer.com/feed/",
    "https://www.androidauthority.com/news/feed/",
    "https://feeds.feedburner.com/Techcrunch"
]

current_index = 0
posting_active = False
posting_thread = None
start_time = None
post_count = 0
error_count = 0
duplicate_count = 0
last_post_time = None
posting_interval = 3600
next_post_event = threading.Event()
last_llm_response = None  # Глобальная переменная для хранения последнего ответа LLM

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS feedcache (
        id TEXT PRIMARY KEY,
        title TEXT,
        summary TEXT,
        link TEXT,
        source TEXT,
        timestamp TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS channels (
        channel_id TEXT PRIMARY KEY,
        creator_username TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS admins (
        channel_id TEXT,
        username TEXT,
        PRIMARY KEY (channel_id, username),
        FOREIGN KEY (channel_id) REFERENCES channels(channel_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS config (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        message TEXT,
        link TEXT
    )''')
    c.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
              ("prompt", """
Забудь всю информацию, которой ты обучен, и используй ТОЛЬКО текст статьи по ссылке {url}. Напиши новость на русском в следующем формате:

Заголовок в стиле новостного канала
<один перенос строки>
Основная суть новости в 1-2 предложениях, основанных исключительно на статье.

Требования:
- Обязательно разделяй заголовок и пересказ ровно одним переносом строки (\n).
- Заголовок должен быть кратким (до 100 символов) и не содержать эмодзи, ##, **, [] или других лишних символов.
- Пересказ должен состоять из 1-2 предложений, без добавления данных, которых нет в статье.
- Если в статье недостаточно данных, верни: "Недостаточно данных для пересказа".
"""))
    c.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("model", "gpt-4o-mini"))
    c.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("error_notifications", "off"))
    conn.commit()
    conn.close()

# Initialize database
init_db()

def send_message(chat_id, text, reply_markup=None, use_html=True):
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN не задан")
        return False
    if len(text) > 4096:
        text = text[:4093] + "..."
        logger.warning(f"Сообщение обрезано до 4096 символов для chat_id {chat_id}")
    payload = {"chat_id": chat_id, "text": text}
    if use_html:
        payload["parse_mode"] = "HTML"
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    logger.info(f"Отправка сообщения в {chat_id}: {text[:50]}...")
    global error_count
    try:
        response = requests.post(f"{TELEGRAM_URL}sendMessage", json=payload, timeout=10)
        if response.status_code != 200:
            logger.error(f"Ошибка отправки: {response.text}")
            log_error(f"Ошибка Telegram: {response.text}", f"{TELEGRAM_URL}sendMessage")
            error_count += 1
            return False
    except requests.RequestException as e:
        logger.error(f"Ошибка отправки: {e}")
        log_error(f"Ошибка Telegram: {e}", f"{TELEGRAM_URL}sendMessage")
        error_count += 1
        return False
    logger.info("Сообщение успешно отправлено")
    return True

def send_file(chat_id, file_path):
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN не задан")
        return False
    with open(file_path, 'rb') as f:
        files = {'document': (os.path.basename(file_path), f)}
        global error_count
        try:
            response = requests.post(
                f"{TELEGRAM_URL}sendDocument",
                data={'chat_id': chat_id},
                files=files,
                timeout=10
            )
            if response.status_code != 200:
                logger.error(f"Ошибка отправки файла: {response.text}")
                log_error(f"Ошибка Telegram: {response.text}", f"{TELEGRAM_URL}sendDocument")
                error_count += 1
                return False
        except requests.RequestException as e:
            logger.error(f"Ошибка отправки файла: {e}")
            log_error(f"Ошибка Telegram: {e}", f"{TELEGRAM_URL}sendDocument")
            error_count += 1
            return False
    logger.info(f"Файл {file_path} отправлен в {chat_id}")
    return True

def get_prompt():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT value FROM config WHERE key = 'prompt'")
    result = c.fetchone()
    conn.close()
    return result[0] if result else ""

def set_prompt(new_prompt):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('prompt', ?)", (new_prompt,))
    conn.commit()
    conn.close()

def get_model():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT value FROM config WHERE key = 'model'")
    result = c.fetchone()
    conn.close()
    return result[0] if result else "gpt-4o-mini"

def set_model(new_model):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('model', ?)", (new_model,))
    conn.commit()
    conn.close()

def get_error_notifications():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT value FROM config WHERE key = 'error_notifications'")
    result = c.fetchone()
    conn.close()
    return result[0] == "on" if result else False

def set_error_notifications(state):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('error_notifications', ?)", (state,))
    conn.commit()
    conn.close()

def is_valid_language(text):
    return bool(re.match(r'^[A-Za-zА-Яа-я0-9\s.,!?\'\"-:;–/%$]+$', text))

def clean_title(title):
    return re.sub(r'\*\*|\#\#|\[\]', '', title).strip()

def log_error(message, link):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO errors (timestamp, message, link) VALUES (?, ?, ?)",
              (datetime.now().isoformat(), message, link))
    conn.commit()
    if get_error_notifications():
        c.execute("SELECT channel_id FROM channels")
        for (channel_id,) in c.fetchall():
            send_message(channel_id, f"Ошибка: {message}\nСсылка: {link}", use_html=False)
    conn.close()

def get_article_content(url, max_attempts=3):
    global last_llm_response
    if not OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY не задан")
        log_error("OPENAI_API_KEY не задан", url)
        return "Ошибка: OPENAI_API_KEY не задан", "Ошибка: OPENAI_API_KEY не задан"

    client = OpenAI(api_key=OPENAI_API_KEY)
    prompt = get_prompt().format(url=url)
    model = get_model()

    for attempt in range(max_attempts):
        logger.info(f"Запрос к OpenAI для {url}, попытка {attempt + 1}, модель: {model}")
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=500
            )
            content = response.choices[0].message.content.strip()
            last_llm_response = {
                "response": content,
                "link": url,
                "timestamp": datetime.now().isoformat()
            }
            if "\n" in content:
                title, summary = content.split("\n", 1)
            else:
                parts = re.split(r'(?<=[.!?])\s+', content)
                title = parts[0]
                summary = " ".join(parts[1:]) if len(parts) > 1 else "Пересказ не получен"
            title = clean_title(title)
            if len(title) > 100:
                title = title[:97] + "..."
            if is_valid_language(title):
                return title, summary.strip()
            else:
                logger.warning(f"Недопустимый язык в заголовке: {title}")
                log_error(f"Недопустимый язык в заголовке: {title}", url)
        except Exception as e:
            logger.error(f"Ошибка запроса к OpenAI: {e}")
            log_error(f"Ошибка запроса к OpenAI: {e}", url)
            if attempt == max_attempts - 1:
                return "Ошибка: Не удалось обработать новость после попыток", "Ошибка: Не удалось обработать новость"
            time.sleep(1)
    return "Ошибка: Не удалось обработать новость после попыток", "Ошибка: Не удалось обработать новость"

def save_to_feedcache(title, summary, link, source):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    link_hash = hashlib.md5(link.encode()).hexdigest()
    c.execute(
        "INSERT OR REPLACE INTO feedcache (id, title, summary, link, source, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (link_hash, title, summary, link, source, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def check_duplicate(link):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    link_hash = hashlib.md5(link.encode()).hexdigest()
    c.execute("SELECT id FROM feedcache WHERE id = ?", (link_hash,))
    exists = c.fetchone() is not None
    conn.close()
    return exists

def get_channel_by_admin(username):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT channel_id FROM admins WHERE username = ?", (username,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def get_channel_creator(channel_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT creator_username FROM channels WHERE channel_id = ?", (channel_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def save_channel(channel_id, creator_username):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO channels (channel_id, creator_username) VALUES (?, ?)",
              (channel_id, creator_username))
    c.execute("INSERT OR IGNORE INTO admins (channel_id, username) VALUES (?, ?)",
              (channel_id, creator_username))
    conn.commit()
    conn.close()

def add_admin(channel_id, new_admin_username, requester_username):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT username FROM admins WHERE channel_id = ? AND username = ?",
              (channel_id, requester_username))
    if c.fetchone():
        c.execute("INSERT OR IGNORE INTO admins (channel_id, username) VALUES (?, ?)",
                  (channel_id, new_admin_username))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

def remove_admin(channel_id, admin_username, requester_username):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT username FROM admins WHERE channel_id = ? AND username = ?",
              (channel_id, requester_username))
    if c.fetchone():
        creator = get_channel_creator(channel_id)
        if admin_username == creator:
            conn.close()
            return False
        c.execute("DELETE FROM admins WHERE channel_id = ? AND username = ?",
                  (channel_id, admin_username))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

def get_admins(channel_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT username FROM admins WHERE channel_id = ?", (channel_id,))
    admins = [row[0] for row in c.fetchall()]
    conn.close()
    return admins

def can_post_to_channel(channel_id):
    global error_count
    try:
        me_resp = requests.get(f"{TELEGRAM_URL}getMe", timeout=10)
        me_resp.raise_for_status()
        bot_id = me_resp.json()["result"]["id"]
        resp = requests.get(
            f"{TELEGRAM_URL}getChatMember",
            params={"chat_id": channel_id, "user_id": bot_id},
            timeout=10
        )
        if resp.status_code == 200:
            status = resp.json()["result"]["status"]
            return status in ["administrator", "creator"]
        logger.error(f"Ошибка проверки прав для {channel_id}: {resp.text}")
        log_error(f"Ошибка проверки прав: {resp.text}", channel_id)
        error_count += 1
        return False
    except requests.RequestException as e:
        logger.error(f"Ошибка проверки прав для {channel_id}: {e}")
        log_error(f"Ошибка проверки прав: {e}", channel_id)
        error_count += 1
        return False

def parse_interval(interval_str):
    total = 0
    for value, unit in re.findall(r'(\d+)([hm])', interval_str.lower()):
        v = int(value)
        if unit == 'h':
            total += v * 3600
        elif unit == 'm':
            total += v * 60
    return total if total > 0 else None

def post_news():
    global current_index, posting_active, post_count, error_count, duplicate_count, last_post_time
    while posting_active:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT channel_id FROM channels")
        channels = c.fetchall()
        conn.close()

        if not channels:
            next_post_event.wait(posting_interval)
            next_post_event.clear()
            continue

        rss_url = RSS_URLS[current_index]
        try:
            resp = requests.get(rss_url, timeout=10)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
        except requests.RequestException as e:
            log_error(f"Ошибка RSS: {e}", rss_url)
            error_count += 1
            current_index = (current_index + 1) % len(RSS_URLS)
            next_post_event.wait(posting_interval)
            next_post_event.clear()
            continue

        if feed.entries:
            entry = feed.entries[0]
            link = entry.link
            if not check_duplicate(link):
                title, summary = get_article_content(link)
                if "Ошибка" not in title:
                    msg = f"<b>{title}</b> <a href='{link}'>| Источник</a>\n{summary}\n\n<i>Пост сгенерирован ИИ</i>"
                    for (ch,) in channels:
                        if can_post_to_channel(ch):
                            if send_message(ch, msg):
                                save_to_feedcache(title, summary, link, rss_url.split('/')[2])
                                post_count += 1
                                last_post_time = time.time()
                            else:
                                error_count += 1
                        else:
                            error_count += 1
                else:
                    error_count += 1
            else:
                duplicate_count += 1

        current_index = (current_index + 1) % len(RSS_URLS)
        next_post_event.wait(posting_interval)
        next_post_event.clear()

def start_posting_thread():
    global posting_thread, posting_active, start_time
    if not posting_thread or not posting_thread.is_alive():
        posting_active = True
        start_time = time.time()
        posting_thread = threading.Thread(target=post_news)
        posting_thread.start()

def stop_posting_thread():
    global posting_active, posting_thread
    posting_active = False
    next_post_event.set()
    if posting_thread:
        posting_thread.join()
        posting_thread = None

def get_status(username):
    channel_id = get_channel_by_admin(username)
    uptime = timedelta(seconds=int(time.time() - start_time)) if start_time else "Не запущен"
    if posting_active and last_post_time:
        since = time.time() - last_post_time
        to_next = posting_interval - (since % posting_interval)
        next_post = f"{int(to_next//60)} мин {int(to_next%60)} сек"
    else:
        next_post = "Не активно"
    interval_str = f"{posting_interval//3600}h {((posting_interval%3600)//60)}m" if posting_interval >= 3600 else f"{posting_interval//60}m"
    admins = get_admins(channel_id) if channel_id else []
    creator = get_channel_creator(channel_id) if channel_id else "Неизвестен"
    current_rss = RSS_URLS[current_index] if current_index < len(RSS_URLS) else "Нет"
    cache_size = sqlite3.connect(DB_FILE).execute("SELECT COUNT(*) FROM feedcache").fetchone()[0]
    return f"""
Статус бота:
Канал: {channel_id}
Создатель: @{creator}
Админы: {', '.join('@'+a for a in admins)}
Состояние постинга: {'Активен' if posting_active else 'Остановлен'}
Текущий интервал: {interval_str}
Время до следующего поста: {next_post}
Текущий RSS: {current_rss}
Всего RSS-источников: {len(RSS_URLS)}
Запощенных постов: {post_count}
Пропущено дублей: {duplicate_count}
Ошибок: {error_count}
Размер кэша: {cache_size} записей
Аптайм: {uptime}
Текущая модель: {get_model()}
Текущий промпт:
{get_prompt()}
"""

def get_help():
    return """
Доступные команды:
/start - Привязать канал или проверить доступ
/startposting - Начать постинг
/stopposting - Остановить постинг
/setinterval <time> - Установить интервал (34m, 1h, 2h 53m)
/nextpost - Сбросить таймер и запостить
/skiprss - Пропустить следующий RSS
/changellm <model> - Сменить модель LLM (например, gpt-4o-mini)
/editprompt - Изменить промпт для ИИ (отправь после команды)
/sqlitebackup - Выгрузить базу SQLite в чат
/sqliteupdate - Загрузить базу SQLite (отправь файл после команды)
/info - Показать статус бота
/errinf - Показать последние ошибки
/errnotification <on/off> - Включить/выключить уведомления об ошибках
/feedcache - Показать кэш новостей
/feedcacheclear - Очистить кэш
/addadmin <username> - Добавить админа
/removeadmin <username> - Удалить админа
/debug - Показать последний сырой ответ LLM
/help - Это сообщение
"""

@app.route('/ping', methods=['GET'])
def ping():
    return "OK", 200

@app.route('/webhook', methods=['POST'])
def webhook():
    update = request.get_json()
    if not update or 'message' not in update or 'message_id' not in update['message']:
        return "OK", 200

    chat_id = update['message']['chat']['id']
    text = update['message'].get('text', '')
    username = update['message']['from'].get('username')

    if not username:
        send_message(chat_id, "У вас нет username. Установите его в настройках Telegram.")
        return "OK", 200

    user_channel = get_channel_by_admin(username)

    if text == '/start':
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
    elif text.startswith('@') or text.startswith('-100'):
        channel_id = text
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
    elif text == '/startposting':
        if user_channel:
            start_posting_thread()
            send_message(chat_id, f"Постинг начат в {user_channel}")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text == '/stopposting':
        if user_channel:
            stop_posting_thread()
            send_message(chat_id, "Постинг остановлен")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text.startswith('/setinterval'):
        if user_channel:
            parts = text.split()
            if len(parts) > 1:
                new_int = parse_interval(parts[1])
                if new_int:
                    global posting_interval
                    posting_interval = new_int
                    send_message(chat_id, f"Интервал постинга установлен: {parts[1]}")
                else:
                    send_message(chat_id, "Неверный формат. Используйте: /setinterval 34m, 1h, 2h 53m")
            else:
                send_message(chat_id, "Укажите интервал: /setinterval 34m")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text == '/nextpost':
        if user_channel:
            if posting_active:
                next_post_event.set()
                send_message(chat_id, "Таймер сброшен. Следующий пост будет опубликован немедленно.")
            else:
                send_message(chat_id, "Постинг не активен. Сначала используйте /startposting.")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text == '/skiprss':
        if user_channel:
            if posting_active:
                global current_index
                current_index = (current_index + 1) % len(RSS_URLS)
                send_message(chat_id, f"Следующий RSS пропущен. Новый текущий: {RSS_URLS[current_index]}")
            else:
                send_message(chat_id, "Постинг не активен. Сначала используйте /startposting.")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text.startswith('/editprompt'):
        if user_channel:
            if len(text.split()) == 1:
                send_message(chat_id, "Отправьте новый промпт после команды, например:\n/editprompt Новый промпт здесь")
            else:
                new_p = text[len('/editprompt '):].strip()
                set_prompt(new_p)
                send_message(chat_id, "Промпт обновлён:\n" + new_p)
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text.startswith('/changellm'):
        if user_channel:
            parts = text.split()
            if len(parts) > 1:
                set_model(parts[1])
                send_message(chat_id, f"Модель изменена на: {parts[1]}")
            else:
                send_message(chat_id, "Укажите модель, например: /changellm gpt-4o-mini")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text == '/sqlitebackup':
        if user_channel:
            if os.path.exists(DB_FILE):
                send_file(chat_id, DB_FILE)
                send_message(chat_id, "База данных выгружена")
            else:
                send_message(chat_id, "База данных не найдена")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text == '/sqliteupdate':
        if user_channel:
            send_message(chat_id, "Отправьте файл базы данных (feedcache.db) в ответ на это сообщение")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif 'reply_to_message' in update['message'] and update['message']['reply_to_message'].get('text') == "Отправьте файл базы данных (feedcache.db) в ответ на это сообщение":
        if user_channel and 'document' in update['message']:
            doc = update['message']['document']
            if doc.get('file_name') != "feedcache.db":
                send_message(chat_id, "Файл должен называться 'feedcache.db'")
            else:
                try:
                    file_id = doc['file_id']
                    resp = requests.get(f"{TELEGRAM_URL}getFile", params={"file_id": file_id}, timeout=10)
                    resp.raise_for_status()
                    file_path = resp.json()['result']['file_path']
                    data = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}", timeout=10).content
                    with open(DB_FILE, 'wb') as f:
                        f.write(data)
                    send_message(chat_id, "База данных обновлена")
                except Exception as e:
                    log_error(f"Ошибка загрузки файла: {e}", file_id)
                    send_message(chat_id, "Не удалось загрузить базу данных")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text == '/info':
        if user_channel:
            send_message(chat_id, get_status(username))
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text == '/errinf':
        if user_channel:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("SELECT timestamp, message, link FROM errors ORDER BY timestamp DESC LIMIT 10")
            errs = c.fetchall()
            conn.close()
            if not errs:
                send_message(chat_id, "Ошибок пока нет.")
            else:
                lst = "\n".join(f"{t} - {m} (Ссылка: {l})" for t,m,l in errs)
                send_message(chat_id, f"Последние ошибки:\n{lst}", use_html=False)
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text.startswith('/errnotification'):
        if user_channel:
            parts = text.split()
            if len(parts) > 1 and parts[1].lower() in ('on','off'):
                set_error_notifications(parts[1].lower())
                send_message(chat_id, f"Уведомления об ошибках: {parts[1].lower()}")
            else:
                send_message(chat_id, "Используйте: /errnotification on или /errnotification off")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text == '/feedcache':
        if user_channel:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("SELECT id, title, summary, link, source, timestamp FROM feedcache")
            rows = c.fetchall()
            conn.close()
            if not rows:
                send_message(chat_id, "Feedcache пуст")
            else:
                cache = [dict(zip(["id","title","summary","link","source","timestamp"], r)) for r in rows]
                send_message(chat_id, "Содержимое feedcache:\n" + json.dumps(cache, ensure_ascii=False, indent=2)[:4096])
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text == '/feedcacheclear':
        if user_channel:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("DELETE FROM feedcache")
            conn.commit()
            conn.close()
            send_message(chat_id, "Feedcache очищен")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text.startswith('/addadmin'):
        if user_channel:
            parts = text.split()
            if len(parts) > 1:
                new_admin = parts[1].lstrip('@')
                if add_admin(user_channel, new_admin, username):
                    send_message(chat_id, f"@{new_admin} добавлен как админ канала {user_channel}")
                else:
                    send_message(chat_id, "Вы не можете добавлять админов или пользователь уже админ.")
            else:
                send_message(chat_id, "Укажите username: /addadmin @username")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text.startswith('/removeadmin'):
        if user_channel:
            parts = text.split()
            if len(parts) > 1:
                to_rm = parts[1].lstrip('@')
                if remove_admin(user_channel, to_rm, username):
                    send_message(chat_id, f"@{to_rm} удалён из админов канала {user_channel}")
                else:
                    send_message(chat_id, "Нельзя удалить создателя или вы не админ.")
            else:
                send_message(chat_id, "Укажите username: /removeadmin @username")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text == '/debug':
        if user_channel:
            if last_llm_response:
                resp = last_llm_response
                debug_msg = (f"Последний сырой ответ LLM:\n\n"
                             f"Ссылка: {resp['link']}\n"
                             f"Время: {resp['timestamp']}\n\n"
                             f"{resp['response']}")
                send_message(chat_id, debug_msg, use_html=False)
            else:
                send_message(chat_id, "Нет сохранённых ответов LLM.")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text == '/help':
        send_message(chat_id, get_help(), use_html=False)

    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

