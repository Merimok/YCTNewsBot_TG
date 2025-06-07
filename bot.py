import os
import requests
from bs4 import BeautifulSoup
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
Забудь всю информацию, которой ты обучен, и используй ТОЛЬКО этот текст статьи:
{text}
Напиши новость на русском в следующем формате:

Заголовок в стиле новостного канала
<один перенос строки>
Основная суть новости в 1-2 предложениях, основанных исключительно на статье.

Требования:
- Обязательно разделяй заголовок и пересказ ровно одним переносом строки (\\n).
- Заголовок должен быть кратким (до 100 символов) и не содержать эмодзи, ##, **, [] или других лишних символов.
- Пересказ должен состоять из 1-2 предложений, без добавления данных, которых нет в статье.
- Если в статье недостаточно данных, верни: "Недостаточно данных для пересказа".
"""))
    c.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("model", "gpt-4o-mini"))
    c.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("error_notifications", "off"))
    conn.commit()
    conn.close()

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
    res = c.fetchone()
    conn.close()
    return res[0] if res else ""

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
    res = c.fetchone()
    conn.close()
    return res[0] if res else "gpt-4o-mini"

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
    res = c.fetchone()
    conn.close()
    return res[0] == "on" if res else False

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
        for (ch,) in c.fetchall():
            send_message(ch, f"Ошибка: {message}\nСсылка: {link}", use_html=False)
    conn.close()

def extract_article_text(url, limit=8000):
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        log_error(f"Ошибка загрузки страницы: {e}", url)
        return None
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.extract()
    texts = [t.get_text(separator=" ", strip=True) for t in soup.find_all(["p","h1","h2","h3"])]
    text = " ".join(texts) or soup.get_text(separator=" ", strip=True)
    return text.replace("\xa0", " ")[:limit]

def get_article_content(url, max_attempts=3, text_limit=8000):
    global last_llm_response
    if not OPENAI_API_KEY:
        log_error("OPENAI_API_KEY не задан", url)
        return "Ошибка: OPENAI_API_KEY не задан", "Ошибка: OPENAI_API_KEY не задан"

    article_text = extract_article_text(url, limit=text_limit)
    if not article_text:
        return "Ошибка: не удалось загрузить статью", "Ошибка: не удалось загрузить статью"

    client = OpenAI(api_key=OPENAI_API_KEY)
    prompt = get_prompt().format(text=article_text)
    model = get_model()

    for attempt in range(max_attempts):
        logger.info(f"Запрос к OpenAI для {url}, попытка {attempt+1}, модель: {model}")
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=500
            )
            content = resp.choices[0].message.content.strip()
            last_llm_response = {"response": content, "link": url, "timestamp": datetime.now().isoformat()}

            if "\n" in content:
                title, summary = content.split("\n", 1)
            else:
                parts = re.split(r'(?<=[.!?])\s+', content)
                title = parts[0]
                summary = " ".join(parts[1:]) if len(parts)>1 else "Пересказ не получен"

            title = clean_title(title)
            if len(title) > 100:
                title = title[:97] + "..."
            if is_valid_language(title):
                return title, summary.strip()
            else:
                log_error(f"Недопустимый язык в заголовке: {title}", url)
        except Exception as e:
            log_error(f"Ошибка запроса к OpenAI: {e}", url)
            if attempt == max_attempts-1:
                return "Ошибка: Не удалось обработать новость после попыток", "Ошибка: Не удалось обработать новость"
            time.sleep(1)

    return "Ошибка: Не удалось обработать новость после попыток", "Ошибка: Не удалось обработать новость"

def save_to_feedcache(title, summary, link, source):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    link_hash = hashlib.md5(link.encode()).hexdigest()
    c.execute("INSERT OR REPLACE INTO feedcache (id,title,summary,link,source,timestamp) VALUES (?,?,?,?,?,?)",
              (link_hash, title, summary, link, source, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def check_duplicate(link):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    link_hash = hashlib.md5(link.encode()).hexdigest()
    c.execute("SELECT 1 FROM feedcache WHERE id=?", (link_hash,))
    exists = c.fetchone() is not None
    conn.close()
    return exists

def get_channel_by_admin(username):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT channel_id FROM admins WHERE username=?", (username,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def get_channel_creator(channel_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT creator_username FROM channels WHERE channel_id=?", (channel_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def save_channel(channel_id, creator):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO channels (channel_id,creator_username) VALUES (?,?)", (channel_id, creator))
    c.execute("INSERT OR IGNORE INTO admins (channel_id,username) VALUES (?,?)", (channel_id, creator))
    conn.commit()
    conn.close()

def add_admin(channel_id, new_admin, requester):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT 1 FROM admins WHERE channel_id=? AND username=?", (channel_id, requester))
    if c.fetchone():
        c.execute("INSERT OR IGNORE INTO admins (channel_id,username) VALUES (?,?)", (channel_id, new_admin))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

def remove_admin(channel_id, admin, requester):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT 1 FROM admins WHERE channel_id=? AND username=?", (channel_id, requester))
    if c.fetchone():
        creator = get_channel_creator(channel_id)
        if admin == creator:
            conn.close()
            return False
        c.execute("DELETE FROM admins WHERE channel_id=? AND username=?", (channel_id, admin))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

def get_admins(channel_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT username FROM admins WHERE channel_id=?", (channel_id,))
    admins = [r[0] for r in c.fetchall()]
    conn.close()
    return admins

def can_post_to_channel(channel_id):
    global error_count
    try:
        me = requests.get(f"{TELEGRAM_URL}getMe", timeout=10)
        me.raise_for_status()
        bot_id = me.json()["result"]["id"]
        resp = requests.get(f"{TELEGRAM_URL}getChatMember",
                            params={"chat_id": channel_id, "user_id": bot_id}, timeout=10)
        if resp.status_code == 200:
            return resp.json()["result"]["status"] in ["administrator", "creator"]
        log_error(f"Ошибка проверки прав: {resp.text}", channel_id)
        error_count += 1
        return False
    except requests.RequestException as e:
        log_error(f"Ошибка проверки прав: {e}", channel_id)
        error_count += 1
        return False

def parse_interval(s):
    sec = 0
    for v,u in re.findall(r'(\d+)([hm])', s.lower()):
        n = int(v)
        sec += n*3600 if u=='h' else n*60
    return sec if sec>0 else None

def post_news():
    global current_index, posting_active, post_count, error_count, duplicate_count, last_post_time
    while posting_active:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT channel_id FROM channels")
        channels = [r[0] for r in c.fetchall()]
        conn.close()

        if not channels:
            next_post_event.wait(posting_interval)
            next_post_event.clear()
            continue

        url = RSS_URLS[current_index]
        try:
            r = requests.get(url, timeout=10); r.raise_for_status()
            feed = feedparser.parse(r.content)
        except requests.RequestException as e:
            log_error(f"Ошибка RSS: {e}", url)
            error_count += 1
            current_index = (current_index + 1) % len(RSS_URLS)
            next_post_event.wait(posting_interval); next_post_event.clear()
            continue

        if feed.entries:
            link = feed.entries[0].link
            if not check_duplicate(link):
                title, summary = get_article_content(link)
                if "Ошибка" not in title:
                    msg = f"<b>{title}</b> <a href='{link}'>| Источник</a>\n{summary}\n\n<i>Пост сгенерирован ИИ</i>"
                    for ch in channels:
                        if can_post_to_channel(ch) and send_message(ch, msg):
                            save_to_feedcache(title, summary, link, url.split('/')[2])
                            post_count += 1
                            last_post_time = time.time()
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

def get_status(user):
    ch = get_channel_by_admin(user)
    uptime = timedelta(seconds=int(time.time()-start_time)) if start_time else "Не запущен"
    if posting_active and last_post_time:
        to_next = posting_interval - ((time.time() - last_post_time) % posting_interval)
        next_p = f"{int(to_next//60)} мин {int(to_next%60)} сек"
    else:
        next_p = "Не активно"
    intv = f"{posting_interval//3600}h {((posting_interval%3600)//60)}m" if posting_interval>=3600 else f"{posting_interval//60}m"
    admins = get_admins(ch) if ch else []
    creator = get_channel_creator(ch) if ch else "Неизвестен"
    rss = RSS_URLS[current_index] if current_index < len(RSS_URLS) else "Нет"
    cache_sz = sqlite3.connect(DB_FILE).execute("SELECT COUNT(*) FROM feedcache").fetchone()[0]
    return f"""
Статус бота:
Канал: {ch}
Создатель: @{creator}
Админы: {', '.join('@'+a for a in admins)}
Состояние постинга: {'Активен' if posting_active else 'Остановлен'}
Текущий интервал: {intv}
Время до следующего поста: {next_p}
Текущий RSS: {rss}
Всего RSS-источников: {len(RSS_URLS)}
Запощенных постов: {post_count}
Пропущено дублей: {duplicate_count}
Ошибок: {error_count}
Размер кэша: {cache_sz} записей
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
    upd = request.get_json()
    if not upd or 'message' not in upd or 'message_id' not in upd['message']:
        return "OK", 200

    msg = upd['message']
    chat_id = msg['chat']['id']
    text = msg.get('text', '')
    user = msg['from'].get('username')

    if not user:
        send_message(chat_id, "У вас нет username. Установите его в настройках Telegram.")
        return "OK", 200

    user_ch = get_channel_by_admin(user)

    if text == '/start':
        conn = sqlite3.connect(DB_FILE); c = conn.cursor()
        c.execute("SELECT channel_id FROM channels")
        exists = c.fetchone()
        conn.close()
        if user_ch:
            send_message(chat_id, f"Вы уже админ канала {user_ch}. Используйте /startposting для начала.")
        elif not exists:
            send_message(chat_id, "Укажите ID канала для постинга (например, @channelname или -1001234567890):")
        else:
            send_message(chat_id, "У вас нет прав на управление ботом. Обратитесь к администратору канала.")
    elif text.startswith('@') or text.startswith('-100'):
        ch_id = text
        conn = sqlite3.connect(DB_FILE); c = conn.cursor()
        c.execute("SELECT channel_id FROM channels")
        if c.fetchone():
            send_message(chat_id, "Канал уже привязан.")
        elif can_post_to_channel(ch_id):
            save_channel(ch_id, user)
            send_message(chat_id, f"Канал {ch_id} привязан. Вы создатель. Используйте /startposting.")
        else:
            send_message(chat_id, "Бот не имеет прав администратора в этом канале.")
        conn.close()
    elif text == '/startposting':
        if user_ch:
            start_posting_thread()
            send_message(chat_id, f"Постинг начат в {user_ch}")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text == '/stopposting':
        if user_ch:
            stop_posting_thread()
            send_message(chat_id, "Постинг остановлен")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text.startswith('/setinterval'):
        if user_ch:
            parts = text.split()
            if len(parts)>1:
                iv = parse_interval(parts[1])
                if iv:
                    global posting_interval
                    posting_interval = iv
                    send_message(chat_id, f"Интервал постинга установлен: {parts[1]}")
                else:
                    send_message(chat_id, "Неверный формат. Используйте: /setinterval 34m, 1h, 2h 53m")
            else:
                send_message(chat_id, "Укажите интервал: /setinterval 34m")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text == '/nextpost':
        if user_ch:
            if posting_active:
                next_post_event.set()
                send_message(chat_id, "Таймер сброшен. Следующий пост будет опубликован немедленно.")
            else:
                send_message(chat_id, "Постинг не активен.")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text == '/skiprss':
        if user_ch:
            if posting_active:
                global current_index
                current_index = (current_index+1) % len(RSS_URLS)
                send_message(chat_id, f"Следующий RSS пропущен: {RSS_URLS[current_index]}")
            else:
                send_message(chat_id, "Постинг не активен.")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text.startswith('/editprompt'):
        if user_ch:
            if len(text.split())==1:
                send_message(chat_id, "Отправьте новый промпт после команды:\n/editprompt Новый промпт")
            else:
                new_p = text[len('/editprompt '):].strip()
                set_prompt(new_p)
                send_message(chat_id, "Промпт обновлён:\n" + new_p)
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text.startswith('/changellm'):
        if user_ch:
            parts = text.split()
            if len(parts)>1:
                set_model(parts[1])
                send_message(chat_id, f"Модель изменена на: {parts[1]}")
            else:
                send_message(chat_id, "Укажите модель: /changellm gpt-4o-mini")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text == '/sqlitebackup':
        if user_ch:
            if os.path.exists(DB_FILE):
                send_file(chat_id, DB_FILE)
                send_message(chat_id, "База данных выгружена")
            else:
                send_message(chat_id, "База не найдена")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text == '/sqliteupdate':
        if user_ch:
            send_message(chat_id, "Отправьте файл feedcache.db в ответ на это сообщение")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif 'reply_to_message' in msg and msg['reply_to_message'].get('text','').startswith("Отправьте файл feedcache.db"):
        if user_ch and 'document' in msg:
            doc = msg['document']
            if doc.get('file_name') != "feedcache.db":
                send_message(chat_id, "Неверное имя файла")
            else:
                try:
                    fid = doc['file_id']
                    r = requests.get(f"{TELEGRAM_URL}getFile", params={"file_id":fid}, timeout=10); r.raise_for_status()
                    path = r.json()['result']['file_path']
                    data = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{path}", timeout=10).content
                    with open(DB_FILE,'wb') as f: f.write(data)
                    send_message(chat_id, "База данных обновлена")
                except Exception as e:
                    log_error(f"Ошибка загрузки файла: {e}", fid)
                    send_message(chat_id, "Не удалось обновить базу")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text == '/info':
        if user_ch:
            send_message(chat_id, get_status(user))
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text == '/errinf':
        if user_ch:
            conn = sqlite3.connect(DB_FILE); c = conn.cursor()
            c.execute("SELECT timestamp,message,link FROM errors ORDER BY timestamp DESC LIMIT 10")
            errs = c.fetchall()
            conn.close()
            if not errs:
                send_message(chat_id, "Ошибок нет.")
            else:
                lst = "\n".join(f"{t} - {m} ({l})" for t,m,l in errs)
                send_message(chat_id, f"Последние ошибки:\n{lst}", use_html=False)
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text.startswith('/errnotification'):
        if user_ch:
            parts = text.split()
            if len(parts)>1 and parts[1].lower() in ('on','off'):
                set_error_notifications(parts[1].lower())
                send_message(chat_id, f"Уведомления: {parts[1].lower()}")
            else:
                send_message(chat_id, "Используйте: /errnotification on/off")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text == '/feedcache':
        if user_ch:
            conn = sqlite3.connect(DB_FILE); c = conn.cursor()
            c.execute("SELECT id,title,summary,link,source,timestamp FROM feedcache")
            rows = c.fetchall()
            conn.close()
            if not rows:
                send_message(chat_id, "Кэш пуст.")
            else:
                cache = [dict(zip(["id","title","summary","link","source","timestamp"], r)) for r in rows]
                send_message(chat_id, "Кэш:\n" + json.dumps(cache, ensure_ascii=False, indent=2)[:4096])
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text == '/feedcacheclear':
        if user_ch:
            conn = sqlite3.connect(DB_FILE); c = conn.cursor()
            c.execute("DELETE FROM feedcache"); conn.commit(); conn.close()
            send_message(chat_id, "Кэш очищен.")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text.startswith('/addadmin'):
        if user_ch:
            parts = text.split()
            if len(parts)>1:
                new = parts[1].lstrip('@')
                if add_admin(user_ch, new, user):
                    send_message(chat_id, f"@{new} добавлен.")
                else:
                    send_message(chat_id, "Не удалось добавить.")
            else:
                send_message(chat_id, "Укажите @username.")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text.startswith('/removeadmin'):
        if user_ch:
            parts = text.split()
            if len(parts)>1:
                rem = parts[1].lstrip('@')
                if remove_admin(user_ch, rem, user):
                    send_message(chat_id, f"@{rem} удалён.")
                else:
                    send_message(chat_id, "Не удалось удалить.")
            else:
                send_message(chat_id, "Укажите @username.")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text == '/debug':
        if user_ch:
            if last_llm_response:
                resp = last_llm_response
                dbg = (f"Последний ответ LLM:\nСсылка: {resp['link']}\nВремя: {resp['timestamp']}\n\n{resp['response']}")
                send_message(chat_id, dbg, use_html=False)
            else:
                send_message(chat_id, "Нет сохранённых ответов.")
        else:
            send_message(chat_id, "Вы не админ ни одного канала.")
    elif text == '/help':
        send_message(chat_id, get_help(), use_html=False)

    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
