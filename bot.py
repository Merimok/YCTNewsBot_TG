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
last_llm_response = None

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
    c.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("prompt", """
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
- Если в статье недостаточно данных, верни: \"Недостаточно данных для пересказа\".
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
    payload = {
        "chat_id": chat_id,
        "text": text
    }
    if use_html:
        payload["parse_mode"] = "HTML"
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    response = requests.post(f"{TELEGRAM_URL}sendMessage", json=payload)
    return response.status_code == 200

def send_file(chat_id, file_path):
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN не задан")
        return False
    with open(file_path, 'rb') as f:
        files = {'document': (os.path.basename(file_path), f)}
        response = requests.post(f"{TELEGRAM_URL}sendDocument", data={'chat_id': chat_id}, files=files)
    return response.status_code == 200

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
    return bool(re.match(r'^[A-Za-zА-Яа-я0-9\\s.,!?\\'\"-:;–/%$]+$', text))

def clean_title(title):
    cleaned = re.sub(r'\\*\\*|\\#\\#|\\[\\]', '', title).strip()
    return cleaned

def log_error(message, link):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO errors (timestamp, message, link) VALUES (?, ?, ?)", 
              (datetime.now().isoformat(), message, link))
    conn.commit()
    if get_error_notifications():
        c.execute("SELECT channel_id FROM channels")
        for (channel_id,) in c.fetchall():
            send_message(channel_id, f"Ошибка: {message}\\nСсылка: {link}", use_html=False)
    conn.close()

def extract_article_text(url, limit=8000):
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
        log_error(f\"Ошибка загрузки страницы: {e}\", url)
        return None
    soup = BeautifulSoup(response.text, \"html.parser\")
    for tag in soup([\"script\", \"style\", \"noscript\"]):
        tag.extract()
    text = \" \".join(t.get_text(separator=\" \", strip=True) for t in soup.find_all([\"p\", \"h1\", \"h2\", \"h3\"]))
    if not text:
        text = soup.get_text(separator=\" \", strip=True)
    return text.replace(\"\\xa0\", \" \")[:limit]

def get_article_content(url, max_attempts=3, text_limit=8000):
    global last_llm_response
    if not OPENAI_API_KEY:
        log_error(\"OPENAI_API_KEY не задан\", url)
        return \"Ошибка: OPENAI_API_KEY не задан\", \"Ошибка: OPENAI_API_KEY не задан\"

    client = OpenAI(api_key=OPENAI_API_KEY)
    article_text = extract_article_text(url, limit=text_limit)
    if not article_text:
        return \"Ошибка: не удалось загрузить статью\", \"Ошибка: не удалось загрузить статью\"
    prompt = get_prompt().format(text=article_text)
    model = get_model()

    for attempt in range(max_attempts):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{\"role\": \"user\", \"content\": prompt}],
                temperature=0.7,
                max_tokens=500
            )
            content = response.choices[0].message.content.strip()
            last_llm_response = {
                \"response\": content,
                \"link\": url,
                \"timestamp\": datetime.now().isoformat()
            }
            if \"\\n\" in content:
                title, summary = content.split(\"\\n\", 1)
            else:
                sentences = re.split(r'(?<=[.!?])\\s+', content)
                title, summary = (sentences[0], \" \".join(sentences[1:])) if len(sentences) > 1 else (content, \"Пересказ не получен\")
            title = clean_title(title)
            if len(title) > 100:
                title = title[:97] + \"...\"
            return title, summary
        except Exception as e:
            log_error(f\"Ошибка запроса к OpenAI: {str(e)}\", url)
            time.sleep(1)
    return \"Ошибка: Не удалось обработать новость\", \"Ошибка: Не удалось обработать новость\"

