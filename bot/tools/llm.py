"""Обёртка над OpenRouter/Claude через openai-совместимый клиент."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import openai

from bot.config import settings

logger = logging.getLogger(__name__)

_KNOWN_DOMAINS = ("technored", "ministry", "personal")
_KNOWN_KINDS = ("task", "meeting", "note", "question")

_WEEKDAYS = [
    "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
]
_MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def now_context() -> str:
    """Строка с текущим временем для промптов: «Сейчас: пятница, 30 октября 2026, 18:31 (Europe/Moscow)»."""
    now = datetime.now(ZoneInfo(settings.timezone))
    return (
        f"Сейчас: {_WEEKDAYS[now.weekday()]}, {now.day} {_MONTHS_GEN[now.month - 1]} "
        f"{now.year}, {now.strftime('%H:%M')} ({settings.timezone})"
    )


SYSTEM_PROMPT = """\
Ты — личный ассистент Даниила. Отвечаешь на русском, на «ты», обращение — «Даниил».

КТО ТАКОЙ ДАНИИЛ
- 23 года, Одинцовский район.
- Специалист отдела R&D в ТехноРэд (промышленная робототехника).
  Зона ответственности: техническая документация, разработка AI/RAG-систем,
  интеграция роботов, программный инструментарий.
- Заканчивает бакалавриат «Автоматизация технологических процессов и производств»
  (диплом — июнь 2026), поступает в магистратуру «Искусственный интеллект и
  большие данные». Активно изучает AI, цель — выйти на заработок через AI.
- Христианин евангельской веры. Ключевые сферы жизни: Бог и церковь/служение, семья.

ПРОЕКТЫ ТЕХНОРЭД
- RAG-чатбот (Dify) — AI-ассистент для консультаций по продуктам ТехноРэд.
  Даниил ведёт архитектуру: Chatflow, гибридный поиск, Cohere embeddings.
  Статус: активная разработка.
- Патент — сопровождал ответ в ФИПС по быстросменному инструментальному креплению.

СЛУЖЕНИЕ (НЕ)ОДИН — молодёжное служение церкви «Ассамблея Бога» Одинцовского г.о.
Роли Даниила:
- Лидер молодёжи: ведёт команду, планирует встречи, развивает направление.
- Группа прославления: музыкант (гитара, worship) и лидер медиа/звука —
  FOH, настройка мониторов/микшера (в разные дни либо играет, либо на звуке).
- Лидер домашней группы.

КАК ОТВЕЧАТЬ
- Кратко и по делу. Тон дружеский, но прямой, без воды. Эмодзи уместны.
- Есть в базе знаний — используй и укажи источник. Нет — честно скажи, не выдумывай.

ЧЕГО НЕ ДЕЛАТЬ
- Не льстить, не хвалить каждое сообщение («Отличный вопрос!»).
- Не смягчать там, где нужна прямота.
- Не выдумывать факты, ссылки, имена.
- Не морализировать и не читать лекции.
- Не уклоняться от прямого ответа («с одной стороны… с другой…»). Просят один — дай один.
- Не напоминать о своих ограничениях без необходимости.
- Не заканчивать дежурным «что ещё я могу сделать».

ТЕКУЩЕЕ ВРЕМЯ
{now}
Используй его для «сегодня/завтра/на этой неделе».

Контекст из базы знаний:
{context}"""

# Ответ уходит в Telegram (лимит 4096 символов), поэтому большой потолок не нужен.
# Без явного max_tokens OpenRouter резервирует дефолтный максимум модели (64k у Sonnet)
# и отбивает запрос с 402, если баланса не хватает на этот максимум.
MAX_TOKENS = 2000

# Reasoning через OpenRouter: у Sonnet 5.5 он обязателен, и «размышления» съедают max_tokens
# (ответ обрывается). effort=low для основной модели — ответы без лишних размышлений;
# у быстрой модели для парсинга reasoning выключаем полностью — иначе на неоднозначных
# фразах она тратит весь лимит на размышления и возвращает пустой ответ.
_MAIN_EXTRA = {"reasoning": {"effort": "low"}}
_FAST_EXTRA = {"reasoning": {"enabled": False}}

_client: openai.AsyncOpenAI | None = None


def _get_client() -> openai.AsyncOpenAI:
    global _client
    if _client is None:
        _client = openai.AsyncOpenAI(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
        )
    return _client


_EXTRACT_PROMPT = """\
Ты выделяешь конкретные задачи и договорённости из текста (заметки встречи, конспекта).

Найди поручения и дела «кто что должен сделать», дедлайны, следующие шаги.
Игнорируй общие рассуждения без действия.

Верни СТРОГО JSON-объект вида:
{"tasks": [{"text": "...", "domain": "...", "due_date": "YYYY-MM-DD или null", "due_time": "HH:MM или null"}]}

- text — кратко и в повелительном наклонении («подготовить материалы», «позвонить N»).
- domain — один из: technored, ministry, personal. Если не ясно — personal.
- due_date — ISO-дата, если в тексте есть срок; иначе null.
- due_time — время «HH:MM», если оно явно названо; иначе null.
Если задач нет — верни {"tasks": []}. Никакого текста вне JSON."""

_PARSE_PROMPT = """\
Ты разбираешь короткое сообщение Даниила (набранное или надиктованное) и определяешь, что с ним делать.

Верни СТРОГО один JSON-объект и ничего вокруг (никакого текста, рассуждений и ```). \nЕсли в сообщении несколько дел — всё равно ОДИН объект: объедини их в text через «; ».
{"kind": "task|meeting|note|question", "text": "...", "domain": "technored|ministry|personal", \
"due_date": "YYYY-MM-DD или null", "due_time": "HH:MM или null", "recurrence": "строка или null"}

ТИП (kind)
- task — нужно что-то сделать, в т.ч. «напомни…», «позвонить в 21:00», «купить завтра». \
Задача МОЖЕТ иметь время — это напоминание, а НЕ встреча.
- meeting — только событие с участием других людей в конкретное время: встреча, созвон, \
собрание, репетиция, служение. «Позвонить кому-то» — это task, не meeting.
- question — вопрос или просьба найти, объяснить, рассказать.
- note — мысль, идея, факт для запоминания, без действия.

ТЕКСТ (text)
Очищенная формулировка: без слов «задача», «напомни мне», «добавь», без даты/времени/повторения. \
Для задач — в инфинитиве или повелительном виде. Для note/question — исходный смысл без искажений.

ДОМЕН (domain)
- technored — работа в ТехноРэд: роботы, REDWELD, REDLOAD, RAG, документация, патенты.
- ministry — служение (НЕ)ОДИН: церковь, молодёжь, прославление, звук, домашняя группа.
- personal — всё остальное.

ДАТА И ВРЕМЯ
- due_date вычисляй относительно текущей даты из блока «Сейчас». «В пятницу» — ближайшая \
пятница, включая сегодня. «На утро» без точного времени → due_time null.
- Если названо только время (без дня) и сегодня это время уже прошло — due_date = завтра. \
«Через N минут/часов» — прибавь к текущему времени (с переходом через полночь).
- ВАЖНО: имена и отчества («Августа Сергеевича», «Мая», «Марта») — НЕ даты. \
Если даты нет — null, ничего не выдумывай.
- Для meeting due_date + due_time — начало встречи.

ПОВТОР (recurrence)
«каждый день» → daily; «по будням» → wd:0,1,2,3,4; «каждый понедельник и среду» → wd:0,2 \
(Пн=0 … Вс=6); «каждое 1-е число» → monthly:1; иначе null. \
Для повторяющейся задачи due_date — ближайшее подходящее число.

ПРИМЕРЫ (пусть сейчас пятница, 30 октября 2026)
«напомни завтра в 21:00 позвонить Андрею Викторовичу» →
{"kind": "task", "text": "Позвонить Андрею Викторовичу", "domain": "personal", \
"due_date": "2026-10-31", "due_time": "21:00", "recurrence": null}
«поднять вопрос Августа Сергеевича» →
{"kind": "task", "text": "Поднять вопрос Августа Сергеевича", "domain": "personal", \
"due_date": null, "due_time": null, "recurrence": null}
«созвон по REDWELD в понедельник в 11:00» →
{"kind": "meeting", "text": "Созвон по REDWELD", "domain": "technored", \
"due_date": "2026-11-02", "due_time": "11:00", "recurrence": null}
«по будням в 8:00 разбирать почту» →
{"kind": "task", "text": "Разбирать почту", "domain": "personal", \
"due_date": "2026-11-02", "due_time": "08:00", "recurrence": "wd:0,1,2,3,4"}
«какие у меня планы по патенту?» →
{"kind": "question", "text": "Какие у меня планы по патенту?", "domain": "technored", \
"due_date": null, "due_time": null, "recurrence": null}"""

_RECURRENCE_RE = re.compile(r"daily|wd:[0-6](,[0-6])*|monthly:([1-9]|[12]\d|3[01])")


def _parse_json(raw: str) -> dict | None:
    """Достаёт JSON-объект из ответа LLM.

    Сначала — весь фрагмент от первой { до последней } (```-обёртка не мешает).
    Если там рассуждения или несколько объектов — первый объект, который парсится целиком.
    """
    s = (raw or "").strip()
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(s[start:end + 1])
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
        decoder = json.JSONDecoder()
        for m in re.finditer(r"\{", s):
            try:
                data, _ = decoder.raw_decode(s, m.start())
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
    logger.warning("Не удалось разобрать JSON из ответа LLM: %.500s", raw)
    return None


def _valid_date(value) -> str | None:
    """Возвращает ISO-дату 'YYYY-MM-DD' или None."""
    if not value or not isinstance(value, str):
        return None
    return value if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else None


def _valid_time(value) -> str | None:
    """Нормализует время к 'HH:MM' или None."""
    if not value or not isinstance(value, str):
        return None
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:
        return None
    return f"{hh:02d}:{mm:02d}"


async def parse_message(text: str) -> dict | None:
    """Единый разбор сообщения быстрой моделью.

    Возвращает {kind, text, domain, due_date, due_time, recurrence} или None при ошибке.
    """
    try:
        client = _get_client()
        resp = await client.chat.completions.create(
            model=settings.llm_fast_model,
            max_tokens=500,
            extra_body=_FAST_EXTRA,
            messages=[
                {"role": "system", "content": _PARSE_PROMPT + "\n\n" + now_context()},
                {"role": "user", "content": text},
            ],
            extra_headers={"HTTP-Referer": "https://github.com/brain-bot", "X-Title": "brain"},
        )
        data = _parse_json(resp.choices[0].message.content or "")
        if data is None:
            return None
    except Exception:
        logger.exception("parse_message failed")
        return None

    kind = data.get("kind")
    if kind not in _KNOWN_KINDS:
        kind = "question"
    domain = data.get("domain")
    if domain not in _KNOWN_DOMAINS:
        domain = "personal"
    rec = data.get("recurrence")
    if not isinstance(rec, str) or not _RECURRENCE_RE.fullmatch(rec.strip()):
        rec = None
    else:
        rec = rec.strip()
    # У встреч нет повтора — регулярное событие ведём как повторяющуюся задачу с напоминанием
    if kind == "meeting" and rec:
        kind = "task"
    parsed_text = data.get("text")
    parsed_text = parsed_text.strip() if isinstance(parsed_text, str) else ""
    due_date = _valid_date(data.get("due_date"))
    due_time = _valid_time(data.get("due_time"))
    # Модель иногда ставит уже прошедшее сегодня время («в 8 вечера», сказанное в 23:00) —
    # напоминание сработало бы сразу. Такую задачу переносим на завтра.
    if kind == "task" and due_date and due_time:
        now = datetime.now(ZoneInfo(settings.timezone))
        if due_date == now.date().isoformat() and due_time < now.strftime("%H:%M"):
            due_date = (now.date() + timedelta(days=1)).isoformat()
    return {
        "kind": kind,
        "text": parsed_text or text.strip(),
        "domain": domain,
        "due_date": due_date,
        "due_time": due_time,
        "recurrence": rec,
    }


async def extract_tasks(text: str) -> list[dict]:
    """Выделяет задачи из текста через LLM. Возвращает список {text, domain, due_date, due_time}."""
    if not text or not text.strip():
        return []
    client = _get_client()
    try:
        resp = await client.chat.completions.create(
            model=settings.llm_model,
            extra_body=_MAIN_EXTRA,
            max_tokens=1000,
            messages=[
                {"role": "system", "content": _EXTRACT_PROMPT + "\n\n" + now_context()},
                {"role": "user", "content": text[:30000]},
            ],
            extra_headers={"HTTP-Referer": "https://github.com/brain-bot", "X-Title": "brain"},
        )
        data = _parse_json(resp.choices[0].message.content or "")
    except Exception:
        logger.exception("extract_tasks failed")
        return []

    items = data.get("tasks", []) if isinstance(data, dict) else []
    result: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        t = (it.get("text") or "").strip()
        if not t:
            continue
        domain = it.get("domain", "personal")
        if domain not in _KNOWN_DOMAINS:
            domain = "personal"
        result.append({
            "text": t,
            "domain": domain,
            "due_date": _valid_date(it.get("due_date")),
            "due_time": _valid_time(it.get("due_time")),
        })
    return result


async def summarize_transcript(text: str) -> str:
    """Делает структурный конспект расшифровки (Markdown). Для длинных аудио (фаза H)."""
    client = _get_client()
    resp = await client.chat.completions.create(
        model=settings.llm_model,
        extra_body=_MAIN_EXTRA,
        max_tokens=1500,
        messages=[
            {"role": "system", "content": _SUMMARY_PROMPT + "\n\n" + now_context()},
            {"role": "user", "content": text[:60000]},
        ],
        extra_headers={"HTTP-Referer": "https://github.com/brain-bot", "X-Title": "brain"},
    )
    return resp.choices[0].message.content or ""


_SHORT_SUMMARY_PROMPT = """\
Ты делаешь короткую выжимку текста (пересланное сообщение, статья) для Даниила.

Формат ответа строго такой, без вступлений и заключений:
Первая строка: «# » и короткий заголовок (до 8 слов).
Далее 3–6 пунктов Markdown-списком, каждый с «- ».

Пиши на русском, плотно. Не выдумывай того, чего нет в тексте."""


async def summarize_short(text: str) -> str:
    """Короткая выжимка: «# заголовок» + список из 3–6 пунктов. Ошибки пробрасываются."""
    client = _get_client()
    resp = await client.chat.completions.create(
        model=settings.llm_fast_model,
        extra_body=_FAST_EXTRA,
        max_tokens=600,
        messages=[
            {"role": "system", "content": _SHORT_SUMMARY_PROMPT + "\n\n" + now_context()},
            {"role": "user", "content": text[:20000]},
        ],
        extra_headers={"HTTP-Referer": "https://github.com/brain-bot", "X-Title": "brain"},
    )
    return resp.choices[0].message.content or ""


_SUMMARY_PROMPT = """\
Ты делаешь конспект расшифровки аудио (лекция, проповедь, встреча) для Даниила.

Сожми в структурный конспект на русском. Формат Markdown:

## Тема
одно-два предложения, о чём запись.

## Ключевые тезисы
- маркированный список главных мыслей (5–12 пунктов).

## Решения / выводы
- что решено или к чему пришли (если применимо).

## Задачи
- конкретные дела и договорённости (если есть; иначе «—»).

Пиши плотно, без воды. Не выдумывай того, чего нет в расшифровке."""


async def ask_llm(question: str, chunks: list[dict]) -> str:
    """Отправляет вопрос в LLM с контекстом из RAG-чанков."""
    if chunks:
        context_parts = [
            f"[источник: {c['source_file']}]\n{c['text']}"
            for c in chunks
        ]
        context = "\n\n".join(context_parts)
    else:
        context = "(база знаний пуста или ничего не найдено)"

    client = _get_client()
    resp = await client.chat.completions.create(
        model=settings.llm_model,
        extra_body=_MAIN_EXTRA,
        max_tokens=MAX_TOKENS,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT.format(now=now_context(), context=context)},
            {"role": "user", "content": question},
        ],
        extra_headers={
            "HTTP-Referer": "https://github.com/brain-bot",
            "X-Title": "brain",
        },
    )
    return resp.choices[0].message.content or ""
