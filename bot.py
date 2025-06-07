import os
import requests
import telegram_api
import json
import logging
import sqlite3
from flask import Flask, request

# Проблема: в начале файла есть конфликт слияния Git (<<<<<<<, =======, >>>>>>>)
# Решение: удалить все маркеры конфликтов Git и выбрать финальную, согласованную версию импортов и кода.
# Пример - удалить лишние импорты, если они дублируются или относятся к конфликтным веткам:

# Был конфликт:
# <<<<<<< codex/добавить-обработку-html-в-текст
# =======
# >>>>>>> main

# Очищенный и согласованный импорт
from datetime import datetime, timedelta
import threading
import time
import re
import html
import hashlib
import feedparser
from openai import OpenAI

# Также убедитесь, что переменные вроде TELEGRAM_TOKEN и TELEGRAM_URL определены в telegram_api или в окружении.
# Далее исправляйте такие ошибки, как неправильный параметр в функции remove_admin:
# Было: def remove_admin(channel_id, admin_username348, requester_username):
# Стало:

def remove_admin(channel_id, admin_username, requester_username):
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
