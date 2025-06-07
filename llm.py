import re
import logging
from datetime import datetime
from openai import OpenAI

from database import get_prompt, get_model, log_error

logger = logging.getLogger(__name__)

last_llm_response = None  # store last raw LLM response


def is_valid_language(text: str) -> bool:
    return bool(re.match(r'^[A-Za-zА-Яа-я0-9\s.,!?\'"-:;–/%$]+$', text))


def clean_title(title: str) -> str:
    return re.sub(r'\*\*|\#\#|\[\]', '', title).strip()


def get_article_content(url: str, max_attempts: int = 3):
    global last_llm_response
    client = OpenAI()
    prompt = get_prompt().format(url=url)
    model = get_model()

    for attempt in range(max_attempts):
        logger.info("Запрос к OpenAI для %s, попытка %s, модель: %s", url, attempt + 1, model)
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=500
            )
            content = response.choices[0].message.content.strip()
            logger.info("Сырой ответ LLM: %s", content)
            last_llm_response = {
                "response": content,
                "link": url,
                "timestamp": datetime.now().isoformat()
            }
            title, summary = None, None
            if "\n" in content:
                title, summary = content.split("\n", 1)
                title = title.strip()
                summary = summary.strip()
            else:
                sentences = re.split(r'(?<=[.!?])\s+', content.strip())
                if len(sentences) > 1:
                    title = sentences[0]
                    summary = " ".join(sentences[1:]).strip()
                else:
                    title = content
                    summary = "Пересказ не получен"
            cleaned_title = clean_title(title)
            if len(cleaned_title) > 100:
                cleaned_title = cleaned_title[:97] + "..."
                logger.warning("Заголовок укорочен: %s", cleaned_title)
            if is_valid_language(cleaned_title):
                return cleaned_title, summary
            logger.warning("Недопустимый язык в заголовке после очистки: %s", cleaned_title)
            log_error(f"Недопустимый язык в заголовке: {cleaned_title}", url)
        except Exception as e:
            logger.error("Ошибка запроса к OpenAI: %s", str(e))
            log_error(f"Ошибка запроса к OpenAI: {str(e)}", url)
            if attempt == max_attempts - 1:
                return "Ошибка: Не удалось обработать новость", "Ошибка"
    return "Ошибка: Не удалось обработать новость", "Ошибка"
