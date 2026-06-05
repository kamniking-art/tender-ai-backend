"""
AI генератор уточняющих вопросов к тендеру.
Анализирует текст тендера и предлагает вопросы по категориям GPT:
- нет лицензии
- непонятен объём работ
- неясны сроки
- неясны требования к документам
- конфликт требований
Всегда draft — человек approves перед отправкой.
"""
import httpx
import json
import logging
from app.core.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты помощник участника государственных тендеров.
Твоя задача — найти неясности в тендерной документации и сформулировать уточняющие вопросы заказчику.

Категории вопросов (используй только релевантные):
1. Лицензии и допуски — какие конкретно требуются, есть ли альтернативы
2. Объём работ — что именно входит, что не входит, измеримые показатели
3. Сроки — промежуточные этапы, начало отсчёта, форс-мажор
4. Требования к документам — какие конкретно, форматы, заверения
5. Конфликт требований — противоречия между разными частями документации

Правила:
- Формулируй вопросы конкретно и профессионально
- Максимум 5 вопросов
- Каждый вопрос должен иметь чёткое обоснование
- Отвечай только на русском языке
- Возвращай ТОЛЬКО валидный JSON без markdown блоков"""

USER_TEMPLATE = """Проанализируй тендерную документацию и предложи уточняющие вопросы заказчику.

Тендер: {title}
Заказчик: {customer}

Текст документации:
{text}

Верни JSON в формате:
{{
  "questions": [
    {{
      "text": "Текст вопроса",
      "reason": "Почему этот вопрос важен",
      "category": "лицензии|объём|сроки|документы|конфликт"
    }}
  ]
}}"""


async def generate_clarification_questions(
    title: str,
    customer: str,
    text: str,
    max_questions: int = 5,
) -> list[dict]:
    """
    Генерирует уточняющие вопросы к тендеру через Claude.
    Возвращает список dict с полями: text, reason, category.
    При ошибке возвращает пустой список.
    """
    if not text or len(text.strip()) < 10:
        logger.warning("clarification_ai: text too short, skipping")
        return []

    # Обрезаем текст до разумного размера
    truncated = text[:8000] if len(text) > 8000 else text

    prompt = USER_TEMPLATE.format(
        title=title or "Не указано",
        customer=customer or "Не указан",
        text=truncated,
    )

    payload = {
        "model": settings.ai_extractor_model or "claude-3-5-sonnet-20241022",
        "max_tokens": 1000,
        "temperature": 0,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.ai_extractor_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        raw_text = data.get("content", [{}])[0].get("text", "")
        # Убираем markdown если есть
        clean = raw_text.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        clean = clean.strip()

        parsed = json.loads(clean)
        questions = parsed.get("questions", [])[:max_questions]
        logger.info(f"clarification_ai: generated {len(questions)} questions for '{title[:50]}'")
        return questions

    except Exception as e:
        logger.warning(f"clarification_ai: failed to generate questions: {e}")
        return []
