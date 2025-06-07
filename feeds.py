import feedparser
import threading
import time
import logging
from datetime import timedelta
import re

from telegram_api import send_message, can_post_to_channel
from database import (
    DB_FILE,
    get_admins,
    get_channel_by_admin,
    get_channel_creator,
    save_to_feedcache,
    check_duplicate,
    get_prompt,
    get_model,
)
from llm import get_article_content
import sqlite3

logger = logging.getLogger(__name__)

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
    "https://feeds.feedburner.com/Techcrunch",
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


def parse_interval(interval_str: str) -> int | None:
    total_seconds = 0
    for value, unit in re.findall(r"(\d+)([hm])", interval_str.lower()):
        value = int(value)
        if unit == "h":
            total_seconds += value * 3600
        elif unit == "m":
            total_seconds += value * 60
    return total_seconds if total_seconds > 0 else None


def post_news():
    global current_index, posting_active, post_count, error_count, duplicate_count, last_post_time
    while posting_active:
        logger.info("Начало цикла постинга, posting_active=%s", posting_active)
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT channel_id FROM channels")
        channels = c.fetchall()
        conn.close()

        if not channels:
            logger.info("Нет каналов для постинга")
            next_post_event.wait(posting_interval)
            if not posting_active:
                break
            continue

        rss_url = RSS_URLS[current_index]
        logger.info("Обрабатываем RSS: %s", rss_url)
        feed = feedparser.parse(rss_url)

        if not feed.entries:
            logger.warning("Нет записей в %s", rss_url)
            error_count += 1
        else:
            latest_entry = feed.entries[0]
            link = latest_entry.link
            logger.info("Проверяем ссылку: %s", link)
            if not check_duplicate(link):
                title, summary = get_article_content(link)
                if "Ошибка" in title:
                    error_count += 1
                    logger.error("Ошибка обработки новости: %s", title)
                    continue
                message = f"<b>{title}</b> <a href='{link}'>| Источник</a>\n{summary}\n\n<i>Пост сгенерирован ИИ</i>"
                logger.info("Сформировано сообщение: %s", message[:50])
                for (channel_id,) in channels:
                    if can_post_to_channel(channel_id):
                        if send_message(channel_id, message, use_html=True):
                            save_to_feedcache(title, summary, link, rss_url.split('/')[2])
                            post_count += 1
                            last_post_time = time.time()
                            logger.info("Новость успешно запощена в %s", channel_id)
                        else:
                            error_count += 1
                            logger.error("Не удалось запостить в %s", channel_id)
                    else:
                        error_count += 1
                        logger.error("Нет прав для постинга в %s", channel_id)
            else:
                duplicate_count += 1
                logger.info("Дубль пропущен: %s, общее число дублей: %s", link, duplicate_count)

        current_index = (current_index + 1) % len(RSS_URLS)
        logger.info("Ожидание следующего поста (%s сек)", posting_interval)
        next_post_event.wait(posting_interval)
        next_post_event.clear()
        if not posting_active:
            break


def start_posting_thread():
    global posting_thread, posting_active, start_time
    if posting_thread is None or not posting_thread.is_alive():
        posting_active = True
        start_time = time.time()
        posting_thread = threading.Thread(target=post_news)
        posting_thread.start()
        logger.info("Постинг запущен")
    else:
        logger.info("Постинг уже активен")


def stop_posting_thread():
    global posting_active, posting_thread
    posting_active = False
    next_post_event.set()
    if posting_thread:
        posting_thread.join()
        posting_thread = None
    logger.info("Постинг остановлен")


def get_status(username: str) -> str:
    channel_id = get_channel_by_admin(username)
    uptime = timedelta(seconds=int(time.time() - start_time)) if start_time else "Не запущен"
    next_post = "Не активно"
    if posting_active and last_post_time:
        time_since_last = time.time() - last_post_time
        time_to_next = posting_interval - (time_since_last % posting_interval)
        next_post = f"{int(time_to_next // 60)} мин {int(time_to_next % 60)} сек"
    interval_str = f"{posting_interval // 3600}h {((posting_interval % 3600) // 60)}m" if posting_interval >= 3600 else f"{posting_interval // 60}m"
    admins = get_admins(channel_id) if channel_id else []
    creator = get_channel_creator(channel_id) if channel_id else "Неизвестен"
    current_rss = RSS_URLS[current_index] if current_index < len(RSS_URLS) else "Нет"
    prompt = get_prompt()
    current_model = get_model()
    with sqlite3.connect(DB_FILE) as conn:
        feedcache_size = conn.execute("SELECT COUNT(*) FROM feedcache").fetchone()[0]
    return f"""Статус бота:
Канал: {channel_id}
Создатель: @{creator}
Админы: {', '.join([f'@{a}' for a in admins])}
Состояние постинга: {'Активен' if posting_active else 'Остановлен'}
Текущий интервал: {interval_str}
Время до следующего поста: {next_post}
Текущий RSS: {current_rss}
Всего RSS-источников: {len(RSS_URLS)}
Запощенных постов: {post_count}
Пропущено дублей: {duplicate_count}
Ошибок: {error_count}
Размер кэша: {feedcache_size} записей
Аптайм: {uptime}
Текущая модель: {current_model}
Текущий промпт:
{prompt}
"""


def get_help() -> str:
    help_text = """Доступные команды:
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
/help - Это сообщение"""
    logger.info("Текст помощи перед отправкой: %s", help_text)
    return help_text
