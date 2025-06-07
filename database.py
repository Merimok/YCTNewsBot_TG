import os
import sqlite3
import hashlib
from datetime import datetime
import logging
from typing import List, Optional

from telegram_api import send_message

DB_FILE = "feedcache.db"

logger = logging.getLogger(__name__)


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
              ("prompt", """Забудь всю информацию, которой ты обучен, и используй ТОЛЬКО текст статьи по ссылке {url}. Напиши новость на русском в следующем формате:

Заголовок в стиле новостного канала
<один перенос строки>
Основная суть новости в 1-2 предложениях, основанных исключительно на статье.

Требования:
- Обязательно разделяй заголовок и пересказ ровно одним переносом строки (\n).
- Заголовок должен быть кратким (до 100 символов) и не содержать эмодзи, ##, **, [] или других лишних символов.
- Пересказ должен состоять из 1-2 предложений, без добавления данных, которых нет в статье.
- Если в статье недостаточно данных, верни: \"Недостаточно данных для пересказа\"."""))
    c.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("model", "gpt-4o-mini"))
    c.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("error_notifications", "off"))
    conn.commit()
    conn.close()


def get_prompt() -> str:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT value FROM config WHERE key = 'prompt'")
    result = c.fetchone()
    conn.close()
    return result[0] if result else ""


def set_prompt(new_prompt: str) -> None:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('prompt', ?)", (new_prompt,))
    conn.commit()
    conn.close()


def get_model() -> str:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT value FROM config WHERE key = 'model'")
    result = c.fetchone()
    conn.close()
    return result[0] if result else "gpt-4o-mini"


def set_model(new_model: str) -> None:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('model', ?)", (new_model,))
    conn.commit()
    conn.close()


def get_error_notifications() -> bool:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT value FROM config WHERE key = 'error_notifications'")
    result = c.fetchone()
    conn.close()
    return result[0] == "on" if result else False


def set_error_notifications(state: str) -> None:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('error_notifications', ?)", (state,))
    conn.commit()
    conn.close()


def log_error(message: str, link: str) -> None:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO errors (timestamp, message, link) VALUES (?, ?, ?)",
              (datetime.now().isoformat(), message, link))
    conn.commit()
    if get_error_notifications():
        c.execute("SELECT channel_id FROM channels")
        channels = c.fetchall()
        for (channel_id,) in channels:
            send_message(channel_id, f"Ошибка: {message}\nСсылка: {link}", use_html=False)
    conn.close()


def save_to_feedcache(title: str, summary: str, link: str, source: str) -> None:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    link_hash = hashlib.md5(link.encode()).hexdigest()
    entry = (link_hash, title, summary, link, source, datetime.now().isoformat())
    try:
        c.execute("INSERT OR REPLACE INTO feedcache (id, title, summary, link, source, timestamp) VALUES (?, ?, ?, ?, ?, ?)", entry)
        conn.commit()
        logger.info("Сохранено в feedcache: %s для %s", link_hash, link)
    except sqlite3.Error as e:
        logger.error("Ошибка записи в feedcache: %s", str(e))
    finally:
        conn.close()


def check_duplicate(link: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    link_hash = hashlib.md5(link.encode()).hexdigest()
    c.execute("SELECT id FROM feedcache WHERE id = ?", (link_hash,))
    result = c.fetchone()
    conn.close()
    if result:
        logger.info("Найден дубль в feedcache: %s для %s", link_hash, link)
        return True
    logger.info("Дубль не найден: %s для %s", link_hash, link)
    return False


def get_channel_by_admin(username: str) -> Optional[str]:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT channel_id FROM admins WHERE username = ?", (username,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None


def get_channel_creator(channel_id: str) -> Optional[str]:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT creator_username FROM channels WHERE channel_id = ?", (channel_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None


def save_channel(channel_id: str, creator_username: str) -> None:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO channels (channel_id, creator_username) VALUES (?, ?)", (channel_id, creator_username))
    c.execute("INSERT OR IGNORE INTO admins (channel_id, username) VALUES (?, ?)", (channel_id, creator_username))
    conn.commit()
    conn.close()


def add_admin(channel_id: str, new_admin_username: str, requester_username: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT username FROM admins WHERE channel_id = ? AND username = ?", (channel_id, requester_username))
    if c.fetchone():
        c.execute("INSERT OR IGNORE INTO admins (channel_id, username) VALUES (?, ?)", (channel_id, new_admin_username))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False


def remove_admin(channel_id: str, admin_username: str, requester_username: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT username FROM admins WHERE channel_id = ? AND username = ?", (channel_id, requester_username))
    if c.fetchone():
        creator = get_channel_creator(channel_id)
        if admin_username == creator:
            conn.close()
            return False
        c.execute("DELETE FROM admins WHERE channel_id = ? AND username = ?", (channel_id, admin_username))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False


def get_admins(channel_id: str) -> List[str]:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT username FROM admins WHERE channel_id = ?", (channel_id,))
    result = c.fetchall()
    conn.close()
    return [row[0] for row in result]
