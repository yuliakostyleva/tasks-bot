import os
import logging
import html
import json
import base64
import re

import httpx
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TODOIST_TOKEN = os.environ["TODOIST_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
# Формат "HH:MM", например "08:00". Часовой пояс задаётся отдельно (см. TIMEZONE ниже)
SEND_TIME = os.environ.get("SEND_TIME", "08:00")
EVENING_SEND_TIME = os.environ.get("EVENING_SEND_TIME", "22:30")
TIMEZONE = os.environ.get("TIMEZONE", "Europe/Belgrade")

# Актуальный (2026) единый Todoist API. Старый rest/v2 отключён (410 Gone).
TODOIST_API_BASE = "https://api.todoist.com/api/v1"

# Google Calendar — опционально. Поддерживает несколько аккаунтов/календарей.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

# Основной календарь (обязательно, если хочешь календарь вообще)
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN")

# Дополнительные календари — каждый со своим refresh token и понятным названием.
# Добавляй новые пары так же: NAME задаётся здесь в коде, TOKEN берётся из своей переменной в Railway.
EXTRA_CALENDARS = [
    {"label": "Simple", "refresh_token": os.environ.get("GOOGLE_REFRESH_TOKEN_SIMPLE")},
    {"label": "Coach", "refresh_token": os.environ.get("GOOGLE_REFRESH_TOKEN_COACH")},
]

CALENDAR_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN)

# Погода для отчёта Саше — через wttr.in, без ключей и регистрации.
# Координаты вместо названия города — так надёжнее (иначе "Saint Petersburg" иногда путается
# с одноимённым городом во Флориде, США, и погода показывает совсем не то полушарие).
# Текущие: Санкт-Петербург, Россия. Сменить можно в Railway переменной WEATHER_CITY,
# формат "широта,долгота", например "44.8125,20.4612" для Белграда.
WEATHER_CITY = os.environ.get("WEATHER_CITY", "59.9311,30.3609")

# Смены Ани во Фридыме — временная штука (актуальна только до 11 сентября 2026,
# дальше правила поставлены на паузу до новых указаний).
ANYA_SHIFT_DATES = {(9, 2), (9, 5), (9, 6), (9, 7), (9, 10), (9, 11)}
ANYA_SHIFT_VALID_UNTIL = (2026, 9, 11)  # (год, месяц, день) — включительно


def get_dym_status(days_ahead: int = 0) -> str:
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    target_date = datetime.now(ZoneInfo(TIMEZONE)) + timedelta(days=days_ahead)
    valid_until = datetime(*ANYA_SHIFT_VALID_UNTIL, 23, 59, 59, tzinfo=target_date.tzinfo)

    if target_date > valid_until:
        return ""  # правила устарели — молчим, пока не дашь новое расписание

    if (target_date.month, target_date.day) in ANYA_SHIFT_DATES:
        return "🟢💨 Благоприятный день, чтобы покурить в Дыме!"
    else:
        return "🔴 В Дыме Татьянин день! Лучше выбрать другое заведение для перекура."


WEATHER_CITY_LABEL = os.environ.get("WEATHER_CITY_LABEL", "Санкт-Петербург")


def get_weather_current() -> str:
    # Через JSON-эндпоинт — так единицы измерения (°C) и язык описания надёжно контролируются,
    # в отличие от текстового формата, где wttr.in сам решает Фаренгейты это или Цельсии.
    try:
        response = httpx.get(
            f"https://wttr.in/{WEATHER_CITY}",
            params={"format": "j1"},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        current = data["current_condition"][0]
        temp = current["temp_C"]
        desc = current["lang_ru"][0]["value"] if current.get("lang_ru") else current["weatherDesc"][0]["value"]
        return f"{temp}°C, {desc.lower()}"
    except Exception:
        logger.exception("Ошибка при получении текущей погоды")
        return ""


def get_weather_forecast() -> str:
    # Более подробный прогноз: сейчас + макс/мин на сегодня + краткое описание днём
    try:
        response = httpx.get(
            f"https://wttr.in/{WEATHER_CITY}",
            params={"format": "j1"},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()

        current = data["current_condition"][0]
        current_temp = current["temp_C"]
        current_desc = current["lang_ru"][0]["value"] if current.get("lang_ru") else current["weatherDesc"][0]["value"]

        today = data["weather"][0]
        max_temp = today["maxtempC"]
        min_temp = today["mintempC"]

        # Берём описание погоды в середине дня (индекс 4 из 8 трёхчасовых интервалов ~ полдень)
        midday = today["hourly"][4]
        midday_desc = midday["lang_ru"][0]["value"] if midday.get("lang_ru") else midday["weatherDesc"][0]["value"]

        return (
            f"<b>🌤️ Погода:</b> сейчас {current_temp}°C, {current_desc.lower()}\n"
            f"Днём {midday_desc.lower()}, от {min_temp}°C до {max_temp}°C"
        )
    except Exception:
        logger.exception("Ошибка при получении прогноза погоды")
        return ""

# WHOOP — опционально. Recovery, сон, активность.
WHOOP_CLIENT_ID = os.environ.get("WHOOP_CLIENT_ID")
WHOOP_CLIENT_SECRET = os.environ.get("WHOOP_CLIENT_SECRET")
WHOOP_REFRESH_TOKEN = os.environ.get("WHOOP_REFRESH_TOKEN")
WHOOP_ENABLED = bool(WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET and WHOOP_REFRESH_TOKEN)

# Чтобы новый (ротированный) refresh token не терялся при каждом передеплое,
# бот сам записывает его обратно в переменные Railway через официальный API.
RAILWAY_API_TOKEN = os.environ.get("RAILWAY_API_TOKEN")
# Эти три ID Railway подставляет сам, вручную их вписывать не нужно
RAILWAY_PROJECT_ID = os.environ.get("RAILWAY_PROJECT_ID")
RAILWAY_ENVIRONMENT_ID = os.environ.get("RAILWAY_ENVIRONMENT_ID")
RAILWAY_SERVICE_ID = os.environ.get("RAILWAY_SERVICE_ID")

# Google Places API — опционально, для поиска точного адреса места по названию
# (например "Tenders Lounge") при создании события в календаре из пересланного
# сообщения. Без ключа просто не ищет адрес и полагается на то, что сказано в тексте.
PLACES_API_KEY = os.environ.get("PLACES_API_KEY")


def search_place(query: str) -> dict | None:
    if not PLACES_API_KEY or not query:
        return None
    try:
        response = httpx.post(
            "https://places.googleapis.com/v1/places:searchText",
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": PLACES_API_KEY,
                "X-Goog-FieldMask": "places.displayName,places.formattedAddress",
            },
            json={"textQuery": query},
            timeout=15,
        )
        response.raise_for_status()
        places = response.json().get("places", [])
        if not places:
            return None
        top = places[0]
        return {
            "name": top.get("displayName", {}).get("text"),
            "address": top.get("formattedAddress"),
        }
    except Exception:
        logger.exception("Ошибка поиска места через Google Places API")
        return None

# Напоминания о ДР — за сколько дней предупреждать (0 = в сам день). По умолчанию за 3 дня и в день ДР.
BIRTHDAY_REMINDERS_ENABLED = os.environ.get("BIRTHDAY_REMINDERS_ENABLED", "true").lower() != "false"
BIRTHDAY_REMINDER_DAYS = [
    int(d) for d in os.environ.get("BIRTHDAY_REMINDER_DAYS", "3,0").split(",") if d.strip()
]
BIRTHDAY_CALENDAR_ID = "de7100c3a9532829d193b591ea59caa68bd523ee186ebf252ffc858cc1e63b8e@group.calendar.google.com"

# Обратный отсчёт для важных дедлайнов (аудиты, ТЗ и т.п.) — только для задач Todoist
# с этим лейблом, а не для всех задач подряд. Лейбл нужно проставлять в Todoist вручную.
DEADLINE_REMINDERS_ENABLED = os.environ.get("DEADLINE_REMINDERS_ENABLED", "true").lower() != "false"
IMPORTANT_DEADLINE_LABEL = os.environ.get("IMPORTANT_DEADLINE_LABEL", "важное")
DEADLINE_REMINDER_DAYS = [
    int(d) for d in os.environ.get("DEADLINE_REMINDER_DAYS", "3,1,0").split(",") if d.strip()
]

# Дайджест почты (Gmail) — читает основной аккаунт (GOOGLE_REFRESH_TOKEN), тому же
# refresh-токену нужен ещё scope gmail.readonly в дополнение к calendar.events.
EMAIL_DIGEST_ENABLED = os.environ.get("EMAIL_DIGEST_ENABLED", "true").lower() != "false"
EMAIL_DIGEST_HOUR = int(os.environ.get("EMAIL_DIGEST_HOUR", "10"))

# Отправители/паттерны, по которым письмо сразу попадает в "срочное" без ИИ-проверки
GMAIL_DELIVERY_DOMAINS = [
    "cdek.ru", "5post.ru", "ozon.ru", "market.yandex.ru", "lamoda.ru",
    "aliexpress.com", "vseinstrumenti.ru", "onlinetrade.ru",
]
GMAIL_LINKEDIN_DOMAINS = ["linkedin.com"]
GMAIL_STOCK_ALERT_KEYWORDS = ["в наличии", "снова доступен", "снова в наличии", "back in stock"]

# Отправители, которым для срочного нужна доп. проверка через ИИ (не любое письмо от них важно)
GMAIL_AVITO_DOMAINS = ["avito.ru"]
GMAIL_BANK_DOMAINS = ["tinkoff.ru", "tbank.ru", "sovcombank.ru", "pkobp.pl", "yettel.rs", "yettel.hu"]

# Полностью игнорируем — свои же уведомления безопасности, и Wise (читает, но пока
# ничего сделать с этим не может)
GMAIL_SKIP_DOMAINS = ["accounts.google.com", "binance.com", "whoop.com", "wise.com"]

# Особое правило: письма от "ленсбыт" → квитанция (PDF) прикладывается к задаче в Todoist
GMAIL_LENSBYT_MATCH = "ленсбыт"
GMAIL_LENSBYT_TODOIST_TASK_SEARCH = "Сарженку"

# ID писем, которые уже разобраны (показаны в срочном или отправлены в ленсбыт-обработку) —
# чтобы не показывать одно и то же непрочитанное письмо повторно при каждой проверке.
# Живёт в памяти процесса — сбрасывается при передеплое.
_seen_email_ids: set[str] = set()

# Типы напитков, которые можно отмечать. amounts — быстрые кнопки для каждого типа.
# unit — только для отображения в текстах ("мл"/"шт").
DRINK_TYPES = {
    "water": {"emoji": "💧", "label": "Вода", "unit": "мл", "amounts": [200, 300, 500]},
    "coffee": {"emoji": "☕", "label": "Кофе", "unit": "шт", "amounts": [1]},
    "energy": {"emoji": "⚡", "label": "Энергетик", "unit": "шт", "amounts": [1]},
    "soda": {"emoji": "🥤", "label": "Газировка/лимонад", "unit": "мл", "amounts": [250, 330, 500]},
}

# Лог напитков хранится в памяти процесса (перезапуск при передеплое обнуляет —
# для дневного счётчика это не проблема, он и так должен обнуляться каждый день).
# Структура: {"YYYY-MM-DD": {"water": 500, "coffee": 2, ...}}
_drinks_log = {}


def _today_key() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")


def add_drink(drink_type: str, amount: int) -> int:
    key = _today_key()
    day = _drinks_log.setdefault(key, {})
    day[drink_type] = day.get(drink_type, 0) + amount
    return day[drink_type]


def get_drinks_today() -> dict:
    return _drinks_log.get(_today_key(), {})


def get_water_today_ml() -> int:
    return get_drinks_today().get("water", 0)


def format_drinks_today() -> str:
    today = get_drinks_today()
    if not today:
        return "пока пусто"
    parts = []
    for dtype, meta in DRINK_TYPES.items():
        amount = today.get(dtype, 0)
        if amount:
            parts.append(f"{meta['emoji']} {amount} {meta['unit']}")
    return ", ".join(parts) if parts else "пока пусто"


def drinks_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for dtype, meta in DRINK_TYPES.items():
        row = [
            InlineKeyboardButton(
                f"{meta['emoji']} +{amount} {meta['unit']}"
                if len(meta["amounts"]) > 1
                else f"{meta['emoji']} {meta['label']}",
                callback_data=f"drink_{dtype}_{amount}",
            )
            for amount in meta["amounts"]
        ]
        rows.append(row)
    return InlineKeyboardMarkup(rows)


# Распознавание напитков через Claude API — по тексту (в т.ч. расшифрованному
# голосу) или по фото. Опционально: без ключа просто не срабатывает,
# остальной бот продолжает работать как раньше.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
DRINK_AI_ENABLED = bool(ANTHROPIC_API_KEY)

_DRINK_CLASSIFY_INSTRUCTIONS = """Определи, упоминаются ли в сообщении/на фото напитки, которые человек \
только что выпил или пьёт прямо сейчас. Сообщение может описывать ОДИН напиток или НЕСКОЛЬКО сразу \
(например, список за утро/день) — найди их все.

Верни ТОЛЬКО JSON, без пояснений и без markdown-разметки, в виде:
{"drinks": [{"drink_type": "water"|"coffee"|"energy"|"soda", "amount": <число>}, ...]}

Если напитков нет вообще — верни {"drinks": []}.

drink_type обязательно один из четырёх вариантов (water — вода, coffee — кофе/чай/какао/латте/капучино, \
energy — энергетик, soda — газировка/лимонад/сок). Если какой-то конкретный напиток не подходит ни под \
один из них уверенно — просто не включай его в список, а не подбирай ближайший.

amount — количество. Если не указано явно, оцени разумно:
вода — обычно 300 (мл), кофе — 1 (шт/чашка, "половинка кофе" тоже считай за 1), энергетик — 1 (шт/банка), \
газировка/лимонад — 330 (мл, "стакан" ~250)."""


def _call_anthropic(content) -> list[dict]:
    if not DRINK_AI_ENABLED:
        return []
    try:
        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 400,
                "messages": [{"role": "user", "content": content}],
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        raw = "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        ).strip()
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
        parsed = json.loads(raw)
        drinks = parsed.get("drinks", [])
        return [d for d in drinks if d.get("drink_type") in DRINK_TYPES]
    except Exception:
        logger.exception("Ошибка классификации напитков через Claude API")
        return []


def classify_drinks_from_text(text: str) -> list[dict]:
    if not DRINK_AI_ENABLED:
        return []
    prompt = f'{_DRINK_CLASSIFY_INSTRUCTIONS}\n\nСообщение: "{text}"'
    return _call_anthropic(prompt)


def classify_drinks_from_image(base64_data: str, media_type: str) -> list[dict]:
    if not DRINK_AI_ENABLED:
        return []
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": base64_data}},
        {"type": "text", "text": _DRINK_CLASSIFY_INSTRUCTIONS},
    ]
    return _call_anthropic(content)


def log_classified_drinks(results: list[dict]) -> str:
    logged_lines = []
    for result in results:
        drink_type = result["drink_type"]
        amount = result.get("amount") or DRINK_TYPES[drink_type]["amounts"][0]
        add_drink(drink_type, amount)
        meta = DRINK_TYPES[drink_type]
        logged_lines.append(f"{meta['emoji']} +{amount} {meta['unit']} ({meta['label']})")

    header = "Записала:\n" + "\n".join(logged_lines) if len(logged_lines) > 1 else logged_lines[0] + " записано."
    return (
        f"{header}\n\n"
        f"Всего за день: {format_drinks_today()}\n"
        f"💧 Из них вода: {get_water_today_ml()} мл"
    )


def _call_anthropic_raw_text(content) -> str | None:
    if not DRINK_AI_ENABLED:
        return None
    try:
        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 300,
                "messages": [{"role": "user", "content": content}],
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        return "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        ).strip()
    except Exception:
        logger.exception("Ошибка генерации отчёта по напиткам через Claude API")
        return None


def _call_anthropic_json(content) -> dict | None:
    if not DRINK_AI_ENABLED:
        return None
    try:
        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 800,
                "messages": [{"role": "user", "content": content}],
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        raw = "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        ).strip()
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
        return json.loads(raw)
    except Exception:
        logger.exception("Ошибка запроса к Claude API")
        return None


def generate_drink_report() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    today = get_drinks_today()
    if not today:
        return "Юля, пей водичку! 💧\n\nПравда, сегодня она вообще ничего не пила — пока нечего пересылать 🙂"

    breakdown = ", ".join(
        f"{meta['label'].lower()}: {today.get(dtype, 0)} {meta['unit']}"
        for dtype, meta in DRINK_TYPES.items()
        if today.get(dtype, 0)
    )
    now = datetime.now(ZoneInfo(TIMEZONE))

    prompt = (
        "Напиши короткий дружеский отчёт (Юля перешлёт его друзьям сама), про то, сколько она сегодня "
        "выпила. Заголовок ОБЯЗАТЕЛЬНО: «Юля, пей водичку! 💧».\n\n"
        f"Сейчас {now.strftime('%H:%M')} ({TIMEZONE}). Данные за сегодня: {breakdown}.\n\n"
        "ВАЖНО — оценивай честно, а не ругай по умолчанию: сначала сама прикинь, хороший ли это темп воды "
        "для текущего времени суток (например, 400-500 мл к середине дня — это нормально, а не мало). "
        "Если по факту воды выпито достаточно для этого времени дня — тон должен быть одобрительный "
        "и слегка поддразнивающий («ну ты сегодня прямо молодец»), а не отчитывающий. Ругай с юмором "
        "только если реально мало воды для этого времени суток или дело идёт к вечеру, а воды почти "
        "не было.\n\n"
        "КРИТИЧЕСКИ ВАЖНО: используй только напитки, перечисленные в данных выше. Если в данных есть "
        "только вода — шути именно про то, что кроме воды сегодня вообще ничего не пили, и НЕ упоминай "
        "кофе/энергетики/газировку, если их нет в данных. Никогда не придумывай напитки, которых нет "
        "в списке, даже для красного словца.\n\n"
        "СТРОГО ЗАПРЕЩЕНО упоминать чьи-либо имена — ни имя Юли, ни имена тех, кому это пересылается "
        "(включая имена Аня и Саша, даже как обращение) — вообще никаких имён и обращений по имени в "
        "тексте быть не должно, пиши только про \"она\"/безлично. Не используй гендерные обращения к "
        "читателям вроде «девочки», «подружки» и т.п.\n\n"
        "Особый акцент (только если в данных ЕСТЬ кофе/энергетики/газировка): подчеркни, что это не вода, "
        "и если воды реально мало, а остального много — обыграй этот контраст с юмором. Если в данных нет "
        "ничего кроме воды — шути или хвали именно про воду, без выдуманных напитков. Никаких врачебных "
        "советов, только лёгкий тон.\n\n"
        "По оформлению — это важно: пиши спокойным разговорным тоном, БЕЗ КАПСЛОКА ни для одного слова, "
        "не больше одного восклицательного знака на весь текст, максимум 2 эмодзи на весь текст, "
        "без списков и тире-буллетов, без markdown (##, **). 2-3 коротких предложения, слитным текстом. "
        "Избегай неуклюжих или двусмысленно звучащих оборотов (например «что-нибудь в рот») — язык должен "
        "быть естественным, как будто живой человек написал другу, а не сгенерированная шутка."
    )

    text = _call_anthropic_raw_text(prompt)
    if text:
        # На случай, если модель всё же вставит markdown, хотя мы шлём как обычный текст —
        # звёздочки/решётки иначе останутся в сообщении буквально.
        text = text.replace("**", "").replace("##", "").replace("# ", "")
        return text.strip()

    # Фолбэк без ИИ (например, если ключ не настроен) — просто сухие цифры с тем же заголовком
    water_ml = today.get("water", 0)
    lines = ["Юля, пей водичку! 💧", ""]
    for dtype, meta in DRINK_TYPES.items():
        amount = today.get(dtype, 0)
        if amount:
            lines.append(f"{meta['emoji']} {meta['label']}: {amount} {meta['unit']}")
    if water_ml < 500:
        lines.append("\nВоды — кот наплакал, остальное почему-то не считается 🙃")
    return "\n".join(lines)


def _forward_classify_instructions(for_image: bool = False) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TIMEZONE))
    weekday_names = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    source = "На фото — скриншот (например запись к врачу, бронирование, приглашение на встречу, список дел)." if for_image else "Тебе прислали пересланное в Telegram сообщение."
    return (
        f"Сегодня {now.strftime('%Y-%m-%d')} ({weekday_names[now.weekday()]}), сейчас {now.strftime('%H:%M')} "
        f"по времени {TIMEZONE}.\n\n"
        f"{source} Определи, что с этим нужно сделать:\n"
        '- "task" — дело без привязки к конкретному времени (например «купи хлеб», «пришли мне это»).\n'
        '- "event" — событие с конкретной датой И временем (например запись к врачу на определённое время, '
        '«завтра встреча в 9», «15 сентября в 14:00 созвон»).\n'
        '- "unclear" — похоже на задачу или событие, но не хватает данных (например не указано время '
        "события), или неоднозначно.\n"
        '- "none" — явно не требует ни задачи, ни события (случайный скриншот/форвард без действия).\n\n'
        "Верни ТОЛЬКО JSON, без пояснений:\n"
        '{"type": "task"|"event"|"unclear"|"none", "content": "<короткая суть, например название приёма/дела>", '
        '"date": "YYYY-MM-DD или null", "time": "HH:MM или null", '
        '"location_name": "<если type=event и упомянуто конкретное место/заведение — только его название '
        'без адреса и лишних слов, например «Tenders Lounge», иначе null>", '
        '"question": "<если type=unclear — короткий уточняющий вопрос, иначе null>"}'
    )


def classify_forwarded_message(text: str) -> dict | None:
    prompt = f'{_forward_classify_instructions()}\n\nСообщение: "{text}"'
    return _call_anthropic_json(prompt)


def classify_forwarded_image(base64_data: str, media_type: str) -> dict | None:
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": base64_data}},
        {"type": "text", "text": _forward_classify_instructions(for_image=True)},
    ]
    return _call_anthropic_json(content)


# Пересланные сообщения, ожидающие уточнения (задача это или событие, какая дата/время) —
# до ответа на уточняющий вопрос. В памяти процесса, этого достаточно для одного пользователя.
_pending_forward_clarify = {}

# Последнее созданное через бота событие в каждом чате — чтобы следующее сообщение вроде
# "поправь адрес" можно было применить к нему, а не создавать новое событие с нуля.
_last_created_event = {}


async def _create_event_from_result(update: Update, result: dict) -> None:
    if not CALENDAR_ENABLED:
        await update.message.reply_text(
            f"Похоже на событие («{result.get('content')}»), но календарь не настроен — добавь вручную."
        )
        return

    date = result.get("date")
    time_str = result.get("time")
    if not date or not time_str:
        chat_id = update.effective_chat.id
        _pending_forward_clarify[chat_id] = {"content": result.get("content", "")}
        await update.message.reply_text("❓ Уточни дату и время события (например «12 сентября в 15:00»).")
        return

    from datetime import datetime, timedelta

    try:
        start_dt = datetime.strptime(f"{date} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        await update.message.reply_text("Не смогла разобрать дату/время — добавь событие вручную.")
        return

    end_dt = start_dt + timedelta(hours=1)
    summary = result.get("content") or "Без названия"
    location_name = result.get("location_name")

    location_address = None
    address_note = ""
    if location_name:
        place = search_place(location_name)
        if place and place.get("address"):
            location_address = place["address"]
        else:
            address_note = f"\n\n📍 Не нашла точный адрес места «{location_name}» — если знаешь, напиши его следующим сообщением, добавлю в событие."

    try:
        event = create_google_calendar_event(
            GOOGLE_REFRESH_TOKEN,
            summary,
            start_dt.isoformat(),
            end_dt.isoformat(),
            location=location_address,
        )
        _last_created_event[update.effective_chat.id] = {
            "event_id": event["id"],
            "calendar_id": "primary",
            "refresh_token": GOOGLE_REFRESH_TOKEN,
            "summary": summary,
            "start_dt": start_dt,
            "created_at": datetime.now(),
        }
        confirmation = f"✅ Добавила в календарь: {summary}, {start_dt.strftime('%d.%m %H:%M')}"
        if location_address:
            confirmation += f"\n📍 {location_address}"
        await update.message.reply_text(confirmation + address_note)
    except Exception as e:
        logger.exception("Ошибка при создании события в календаре из пересланного сообщения")
        await update.message.reply_text(f"Не смогла добавить событие: {e}")


async def _handle_forward_result(update: Update, result: dict) -> None:
    result_type = result.get("type")

    if result_type == "task":
        content = result.get("content") or update.message.text or ""
        try:
            create_todoist_task(content)
            await update.message.reply_text(f"✅ Добавила в Todoist: {content}")
        except Exception as e:
            logger.exception("Ошибка при добавлении задачи из пересланного сообщения")
            await update.message.reply_text(f"Не смогла добавить задачу: {e}")
        return

    if result_type == "event":
        await _create_event_from_result(update, result)
        return

    if result_type == "unclear":
        question = result.get("question") or "Это задача или событие с датой/временем?"
        chat_id = update.effective_chat.id
        _pending_forward_clarify[chat_id] = {"content": result.get("content", "")}
        await update.message.reply_text(f"❓ {question}")
        return

    await update.message.reply_text("Не поняла, что с этим сделать — добавь вручную, если нужно.")


async def forwarded_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text:
        return

    if not DRINK_AI_ENABLED:
        await update.message.reply_text(
            "Распознавание пересланных сообщений не настроено — нужен ANTHROPIC_API_KEY."
        )
        return

    result = classify_forwarded_message(text)
    if not result:
        await update.message.reply_text("Не поняла, что с этим сделать — добавь вручную, если нужно.")
        return

    await _handle_forward_result(update, result)


async def handle_forward_clarification(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    # Возвращает True, если сообщение обработано как ответ на уточняющий вопрос по пересылке
    chat_id = update.effective_chat.id
    if chat_id not in _pending_forward_clarify:
        return False

    pending = _pending_forward_clarify.pop(chat_id)
    combined = f'{pending.get("content", "")}. Уточнение: {update.message.text}'

    result = classify_forwarded_message(combined)
    if not result:
        await update.message.reply_text("Всё ещё не поняла — добавь вручную, если нужно.")
        return True

    await _handle_forward_result(update, result)
    return True


def classify_event_correction(text: str, last_event: dict) -> dict | None:
    prompt = (
        f"Только что было создано календарное событие: «{last_event['summary']}», "
        f"{last_event['start_dt'].strftime('%Y-%m-%d %H:%M')}.\n\n"
        "Следующее сообщение от пользователя может быть исправлением/уточнением этого события "
        "(например, поправить название, сообщить точный адрес места, изменить дату/время). "
        "Определи, так ли это. Если сообщение — это просто адрес или название заведения "
        "(ответ на вопрос «какой адрес») — это тоже исправление, используй поле location.\n\n"
        "Верни ТОЛЬКО JSON:\n"
        '{"is_correction": true|false, "summary": "<новое название события — или null, если не меняется>", '
        '"location": "<точный адрес/место, если сообщён — или null>", '
        '"date": "YYYY-MM-DD или null, только если дата меняется", '
        '"time": "HH:MM или null, только если время меняется"}\n\n'
        f'Сообщение: "{text}"'
    )
    return _call_anthropic_json(prompt)


async def handle_event_correction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    # Возвращает True, если сообщение обработано как исправление последнего созданного события
    chat_id = update.effective_chat.id
    if not DRINK_AI_ENABLED or chat_id not in _last_created_event:
        return False

    last = _last_created_event[chat_id]

    from datetime import datetime as _dt, timedelta as _td

    if _dt.now() - last["created_at"] > _td(minutes=15):
        del _last_created_event[chat_id]
        return False

    correction = classify_event_correction(update.message.text, last)
    if not correction or not correction.get("is_correction"):
        return False

    from datetime import datetime, timedelta

    updates = {}
    if correction.get("summary"):
        updates["summary"] = correction["summary"]
        last["summary"] = correction["summary"]
    if correction.get("location"):
        updates["location"] = correction["location"]
    if correction.get("date") and correction.get("time"):
        try:
            new_start = datetime.strptime(f'{correction["date"]} {correction["time"]}', "%Y-%m-%d %H:%M")
            updates["start"] = {"dateTime": new_start.isoformat(), "timeZone": TIMEZONE}
            updates["end"] = {"dateTime": (new_start + timedelta(hours=1)).isoformat(), "timeZone": TIMEZONE}
            last["start_dt"] = new_start
        except ValueError:
            pass

    if not updates:
        await update.message.reply_text("Поняла, что это про событие, но не разобрала, что именно поменять.")
        return True

    try:
        update_google_calendar_event(last["refresh_token"], last["event_id"], updates, last["calendar_id"])
        confirmation = f"✅ Обновила событие: {last['summary']}, {last['start_dt'].strftime('%d.%m %H:%M')}"
        if updates.get("location"):
            confirmation += f"\n📍 {updates['location']}"
        await update.message.reply_text(confirmation)
    except Exception as e:
        logger.exception("Ошибка при обновлении события в календаре")
        await update.message.reply_text(f"Не смогла обновить событие: {e}")
    return True


def persist_whoop_refresh_token(new_token: str):
    if not RAILWAY_API_TOKEN:
        return  # не настроено — просто не сохраняем, бот продолжит работать в памяти

    try:
        httpx.post(
            "https://backboard.railway.com/graphql/v2",
            headers={"Authorization": f"Bearer {RAILWAY_API_TOKEN}"},
            json={
                "query": """
                    mutation UpdateWhoopToken($projectId: String!, $environmentId: String!, $serviceId: String!, $value: String!) {
                        variableUpsert(input: {
                            projectId: $projectId,
                            environmentId: $environmentId,
                            serviceId: $serviceId,
                            name: "WHOOP_REFRESH_TOKEN",
                            value: $value,
                            skipDeploys: true
                        })
                    }
                """,
                "variables": {
                    "projectId": RAILWAY_PROJECT_ID,
                    "environmentId": RAILWAY_ENVIRONMENT_ID,
                    "serviceId": RAILWAY_SERVICE_ID,
                    "value": new_token,
                },
            },
            timeout=10,
        )
    except Exception:
        logger.exception("Не удалось сохранить обновлённый WHOOP refresh token в Railway")

# WHOOP выдаёт НОВЫЙ refresh token при каждом обновлении access token и сразу
# гасит старый. Поэтому храним актуальную пару токенов в памяти процесса,
# а не берём каждый раз статичное значение из переменной окружения.
_whoop_token_cache = {
    "access_token": None,
    "refresh_token": WHOOP_REFRESH_TOKEN,
    "expires_at": 0,
}


def get_whoop_access_token() -> str:
    import time

    now = time.time()
    # Если есть свежий (не истёкший) access token — используем его без лишнего похода в WHOOP
    if _whoop_token_cache["access_token"] and now < _whoop_token_cache["expires_at"] - 300:
        return _whoop_token_cache["access_token"]

    response = httpx.post(
        "https://api.prod.whoop.com/oauth/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": _whoop_token_cache["refresh_token"],
            "client_id": WHOOP_CLIENT_ID,
            "client_secret": WHOOP_CLIENT_SECRET,
            "scope": "offline",
        },
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()

    _whoop_token_cache["access_token"] = data["access_token"]
    new_refresh_token = data.get("refresh_token")
    # Важно: сохраняем НОВЫЙ refresh token, иначе следующий вызов снова упадёт
    if new_refresh_token and new_refresh_token != _whoop_token_cache["refresh_token"]:
        _whoop_token_cache["refresh_token"] = new_refresh_token
        persist_whoop_refresh_token(new_refresh_token)
    _whoop_token_cache["expires_at"] = now + data.get("expires_in", 3600)

    return _whoop_token_cache["access_token"]


def get_whoop_summary() -> str:
    if not WHOOP_ENABLED:
        return ""

    access_token = get_whoop_access_token()
    headers = {"Authorization": f"Bearer {access_token}"}

    lines = ["<b>💪 WHOOP:</b>\n"]

    recovery_pct = None
    sleep_performance = None
    yesterday_strain = None

    # Recovery — берём историю за ~месяц: одна запись для сегодня + остальные для расчёта личной нормы
    try:
        r = httpx.get(
            "https://api.prod.whoop.com/developer/v2/recovery",
            headers=headers,
            params={"limit": 25},
            timeout=15,
        )
        r.raise_for_status()
        records = r.json().get("records", [])
        scored = [rec for rec in records if rec.get("score_state") == "SCORED"]

        if scored:
            score = scored[0]["score"]
            recovery_pct = score["recovery_score"]
            # Пороги те же, что использует сам WHOOP (зелёный/жёлтый/красный)
            if recovery_pct >= 67:
                recovery_label = "хорошее 💚"
            elif recovery_pct >= 34:
                recovery_label = "среднее 💛"
            else:
                recovery_label = "низкое ❤️❗ — стоит отдохнуть"

            prev = scored[1] if len(scored) > 1 else None
            compare = ""
            if prev:
                delta = round(recovery_pct - prev["score"]["recovery_score"], 1)
                if delta > 0:
                    compare = f" (+{delta} к прошлому разу)"
                elif delta < 0:
                    compare = f" ({delta} к прошлому разу)"
                else:
                    compare = " (как и в прошлый раз)"

            lines.append(f"Recovery: {recovery_pct}% — {recovery_label}{compare}")

            rhr = score["resting_heart_rate"]
            hrv = round(score["hrv_rmssd_milli"])

            # Личная норма — среднее по всем предыдущим записям (не считая сегодняшнюю)
            history = scored[1:]
            baseline_note_rhr = ""
            baseline_note_hrv = ""
            if len(history) >= 5:
                avg_rhr = sum(h["score"]["resting_heart_rate"] for h in history) / len(history)
                avg_hrv = sum(h["score"]["hrv_rmssd_milli"] for h in history) / len(history)

                rhr_diff_pct = (rhr - avg_rhr) / avg_rhr * 100
                hrv_diff_pct = (hrv - avg_hrv) / avg_hrv * 100

                if rhr_diff_pct > 8:
                    baseline_note_rhr = f" (твой обычный ~{round(avg_rhr)}, сегодня выше — тревожный сигнал)"
                elif rhr_diff_pct < -8:
                    baseline_note_rhr = f" (твой обычный ~{round(avg_rhr)}, сегодня ниже — хорошо)"
                else:
                    baseline_note_rhr = f" (твой обычный ~{round(avg_rhr)}, в норме)"

                if hrv_diff_pct > 8:
                    baseline_note_hrv = f" (твой обычный ~{round(avg_hrv)}, сегодня выше — хорошо)"
                elif hrv_diff_pct < -8:
                    baseline_note_hrv = f" (твой обычный ~{round(avg_hrv)}, сегодня ниже — тревожный сигнал)"
                else:
                    baseline_note_hrv = f" (твой обычный ~{round(avg_hrv)}, в норме)"

            lines.append(f"Пульс покоя: {rhr} уд/мин{baseline_note_rhr}")
            lines.append(f"HRV: {hrv} мс{baseline_note_hrv}")

            # Связываем recovery с тем, что реально на него влияет — HRV и пульс покоя
            if len(history) >= 5:
                hrv_low = hrv_diff_pct < -8
                rhr_high = rhr_diff_pct > 8
                hrv_high = hrv_diff_pct > 8
                rhr_low = rhr_diff_pct < -8

                if recovery_pct < 67 and (hrv_low or rhr_high):
                    causes = []
                    if hrv_low:
                        causes.append("HRV ниже обычного")
                    if rhr_high:
                        causes.append("пульс покоя выше обычного")
                    lines.append(f"📊 Recovery снижен — {', '.join(causes)}.")
                elif recovery_pct >= 67 and (hrv_high or rhr_low):
                    causes = []
                    if hrv_high:
                        causes.append("HRV выше обычного")
                    if rhr_low:
                        causes.append("пульс покоя ниже обычного")
                    lines.append(f"📊 Recovery хороший — {', '.join(causes)}.")
        else:
            lines.append("Recovery: пока не подсчитан.")
    except Exception:
        logger.exception("Ошибка при получении WHOOP recovery")
        lines.append("Recovery: не удалось получить.")

    # Сон — берём с запасом записей, чтобы отфильтровать дневной сон и найти именно ночной
    try:
        s = httpx.get(
            "https://api.prod.whoop.com/developer/v2/activity/sleep",
            headers=headers,
            params={"limit": 10},
            timeout=15,
        )
        s.raise_for_status()
        all_records = s.json().get("records", [])
        # Оставляем только ночной сон (nap=False), дневной сон в этот подсчёт не идёт
        records = [r for r in all_records if r.get("score_state") == "SCORED" and not r.get("nap", False)]

        if records:
            stage = records[0]["score"]["stage_summary"]
            total_ms = (
                stage["total_light_sleep_time_milli"]
                + stage["total_slow_wave_sleep_time_milli"]
                + stage["total_rem_sleep_time_milli"]
            )
            hours = total_ms // 3600000
            minutes = (total_ms % 3600000) // 60000
            sleep_performance = records[0]["score"].get("sleep_performance_percentage")
            if sleep_performance is not None and sleep_performance >= 85:
                sleep_label = "выспалась 💚"
            elif sleep_performance is not None and sleep_performance >= 70:
                sleep_label = "нормально 💛"
            else:
                sleep_label = "маловато ❤️❗"

            compare = ""
            if len(records) > 1:
                prev_stage = records[1]["score"]["stage_summary"]
                prev_total_ms = (
                    prev_stage["total_light_sleep_time_milli"]
                    + prev_stage["total_slow_wave_sleep_time_milli"]
                    + prev_stage["total_rem_sleep_time_milli"]
                )
                delta_minutes = (total_ms - prev_total_ms) // 60000
                if delta_minutes > 5:
                    compare = f" (на {delta_minutes} мин больше, чем в прошлый раз)"
                elif delta_minutes < -5:
                    compare = f" (на {abs(delta_minutes)} мин меньше, чем в прошлый раз)"

            lines.append(f"Сон: {hours}ч {minutes}м — {sleep_label} (производительность {sleep_performance}%){compare}")
        else:
            lines.append("Сон: пока не подсчитан.")
    except Exception:
        logger.exception("Ошибка при получении WHOOP сна")
        lines.append("Сон: не удалось получить.")

    # Активность за вчера — берём последние 2 цикла, второй обычно завершённый (вчерашний)
    try:
        c = httpx.get(
            "https://api.prod.whoop.com/developer/v2/cycle",
            headers=headers,
            params={"limit": 2},
            timeout=15,
        )
        c.raise_for_status()
        records = c.json().get("records", [])
        # Первый цикл обычно "сегодняшний" (ещё идёт), второй — вчерашний завершённый
        yesterday_cycle = records[1] if len(records) > 1 else None
        if yesterday_cycle and yesterday_cycle.get("score_state") == "SCORED":
            yesterday_strain = yesterday_cycle["score"]["strain"]
            # Шкала strain у WHOOP: 0-9 лёгкая нагрузка, 10-13 умеренная, 14-17 высокая, 18-21 максимальная
            if yesterday_strain < 10:
                strain_label = "лёгкий день"
            elif yesterday_strain < 14:
                strain_label = "умеренная нагрузка"
            elif yesterday_strain < 18:
                strain_label = "высокая нагрузка"
            else:
                strain_label = "максимальная нагрузка"
            lines.append(f"Вчерашний strain: {round(yesterday_strain, 1)} — {strain_label}")
    except Exception:
        logger.exception("Ошибка при получении WHOOP цикла")

    advice = get_whoop_advice(recovery_pct, sleep_performance, yesterday_strain)
    if advice:
        lines.append(f"\n💡 {advice}")

    return "\n".join(lines)


def get_google_access_token(refresh_token: str) -> str:
    # Refresh token не истекает сам, но обменивать его на access token
    # нужно перед каждым запросом к API — access token живёт всего час.
    response = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def list_unread_gmail_messages(max_results: int = 50):
    access_token = get_google_access_token(GOOGLE_REFRESH_TOKEN)
    response = httpx.get(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"q": "is:unread in:inbox", "maxResults": max_results},
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("messages", [])


def get_gmail_message_metadata(message_id: str) -> dict:
    access_token = get_google_access_token(GOOGLE_REFRESH_TOKEN)
    response = httpx.get(
        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"format": "metadata", "metadataHeaders": ["From", "Subject"]},
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    headers = {h["name"]: h["value"] for h in data.get("payload", {}).get("headers", [])}
    return {
        "id": message_id,
        "from": headers.get("From", ""),
        "subject": headers.get("Subject", ""),
        "snippet": data.get("snippet", ""),
    }


def get_gmail_message_full(message_id: str) -> dict:
    access_token = get_google_access_token(GOOGLE_REFRESH_TOKEN)
    response = httpx.get(
        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"format": "full"},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def find_pdf_attachment(payload: dict) -> dict | None:
    for part in payload.get("parts", []) or []:
        filename = part.get("filename", "") or ""
        if filename.lower().endswith(".pdf") and part.get("body", {}).get("attachmentId"):
            return {"attachment_id": part["body"]["attachmentId"], "filename": filename}
        nested = find_pdf_attachment(part)
        if nested:
            return nested
    return None


def get_gmail_attachment_bytes(message_id: str, attachment_id: str) -> bytes:
    access_token = get_google_access_token(GOOGLE_REFRESH_TOKEN)
    response = httpx.get(
        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}/attachments/{attachment_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()["data"]
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded)


def create_google_calendar_event(
    refresh_token: str,
    summary: str,
    start_iso: str,
    end_iso: str,
    calendar_id: str = "primary",
    location: str | None = None,
):
    access_token = get_google_access_token(refresh_token)
    payload = {
        "summary": summary,
        "start": {"dateTime": start_iso, "timeZone": TIMEZONE},
        "end": {"dateTime": end_iso, "timeZone": TIMEZONE},
    }
    if location:
        payload["location"] = location
    response = httpx.post(
        f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
        headers={"Authorization": f"Bearer {access_token}"},
        json=payload,
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def update_google_calendar_event(
    refresh_token: str, event_id: str, updates: dict, calendar_id: str = "primary"
):
    access_token = get_google_access_token(refresh_token)
    response = httpx.patch(
        f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        json=updates,
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def create_recurring_birthday_event(
    refresh_token: str, calendar_id: str, summary: str, month: int, day: int, year: int | None = None
):
    from datetime import date, timedelta

    # Год ставим текущий (или ближайший будущий), если год рождения неизвестен —
    # RRULE:FREQ=YEARLY заставит Google Calendar повторять событие каждый год всё равно.
    today = date.today()
    base_year = year or today.year
    try:
        event_date = date(base_year, month, day)
    except ValueError:
        raise ValueError(f"Некорректная дата: {base_year}-{month:02d}-{day:02d}")

    if year is None and event_date < today:
        event_date = date(today.year + 1, month, day)

    end_date = event_date + timedelta(days=1)

    access_token = get_google_access_token(refresh_token)
    response = httpx.post(
        f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
        headers={"Authorization": f"Bearer {access_token}"},
        json={
            "summary": summary,
            "start": {"date": event_date.isoformat()},
            "end": {"date": end_date.isoformat()},
            "recurrence": ["RRULE:FREQ=YEARLY"],
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def classify_birthday_list(text: str) -> list[dict]:
    prompt = (
        "Пользователь прислал список дней рождения в свободной форме (имя и дата, в любом порядке "
        "и формате — текстом, цифрами, с годом или без). Извлеки каждую запись отдельно.\n\n"
        "Верни ТОЛЬКО JSON, без пояснений:\n"
        '{"birthdays": [{"name": "<имя>", "month": <1-12>, "day": <1-31>, '
        '"year": <год рождения, если указан явно, иначе null>}]}\n\n'
        f'Текст: "{text}"'
    )
    result = _call_anthropic_json(prompt)
    if not result:
        return []
    return result.get("birthdays", [])


    access_token = get_google_access_token(refresh_token)
    response = httpx.get(
        "https://www.googleapis.com/calendar/v3/users/me/calendarList",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("items", [])


def get_today_events_for_token(refresh_token: str, calendar_id: str = "primary"):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)

    access_token = get_google_access_token(refresh_token)
    # ID календаря нужно закодировать для URL, т.к. там могут быть @ и другие спецсимволы
    from urllib.parse import quote
    encoded_id = quote(calendar_id, safe="")

    response = httpx.get(
        f"https://www.googleapis.com/calendar/v3/calendars/{encoded_id}/events",
        headers={"Authorization": f"Bearer {access_token}"},
        params={
            "timeMin": start_of_day.isoformat(),
            "timeMax": end_of_day.isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("items", [])


def get_calendar_events_range(refresh_token: str, calendar_id: str, days_ahead: int):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from urllib.parse import quote

    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=days_ahead)).replace(hour=23, minute=59, second=59, microsecond=0)

    access_token = get_google_access_token(refresh_token)
    encoded_id = quote(calendar_id, safe="")

    response = httpx.get(
        f"https://www.googleapis.com/calendar/v3/calendars/{encoded_id}/events",
        headers={"Authorization": f"Bearer {access_token}"},
        params={
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("items", [])


# Календари внутри личного аккаунта, которые нас интересуют (праздничные пропускаем)
MAIN_ACCOUNT_CALENDARS = [
    {"id": "primary", "label": "Основной"},
    {"id": "t7iblka8tatqu1eh5lei2trh1o@group.calendar.google.com", "label": "🧘‍♀️ Sport"},
    {"id": "abj57mm23d217iiks6ob2lc9o4@group.calendar.google.com", "label": "💞 Self Care"},
    {"id": "jnk3uer5kha8tu014tf9bossos@group.calendar.google.com", "label": "👯 Встречи с друзьями"},
    {"id": "0apnocv3q4lb3lvqgokc72kke4@group.calendar.google.com", "label": "✈️ Travel"},
    {"id": "642dc43a7926e7a7fd079090b82a3aaa0ebdd285ee67b2ee01d9ec1ab94fd5a9@group.calendar.google.com", "label": "💸 Deposits"},
    {"id": "de7100c3a9532829d193b591ea59caa68bd523ee186ebf252ffc858cc1e63b8e@group.calendar.google.com", "label": "🎂 ДР в Фридым"},
    {"id": "f3f6c62cee4ba8909450e53cf6fdb2d063139a71459696de01420bb4f5a9486f@group.calendar.google.com", "label": "📋 TAX"},
]


def get_today_calendar_events():
    # Возвращает список (label, events, is_work)
    if not CALENDAR_ENABLED:
        return []

    results = []

    for cal in MAIN_ACCOUNT_CALENDARS:
        try:
            events = get_today_events_for_token(GOOGLE_REFRESH_TOKEN, cal["id"])
            results.append((cal["label"], events, False))
        except Exception:
            logger.exception(f"Ошибка при получении календаря {cal['label']}")

    for cal in EXTRA_CALENDARS:
        if not cal["refresh_token"]:
            continue
        try:
            events = get_today_events_for_token(cal["refresh_token"])
            results.append((f"[{cal['label']}]", events, True))
        except Exception:
            logger.exception(f"Ошибка при получении календаря {cal['label']}")

    return results


def format_calendar_section(calendars) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(TIMEZONE)
    is_weekday = datetime.now(tz).weekday() < 5  # 0=понедельник ... 4=пятница

    def format_event(e) -> str:
        title = html.escape(e.get("summary", "Без названия"))
        start = e.get("start", {})
        time_str = ""
        if "dateTime" in start:
            # Формат: 2026-08-15T14:00:00+03:00 — берём только часы:минуты
            time_str = start["dateTime"][11:16] + " — "
        return f"• {time_str}{title}"

    lines = []
    for label, events, is_work in calendars:
        if events:
            lines.append(f"<b>{html.escape(label)}</b>")
            for e in events:
                lines.append(format_event(e))
            lines.append("")
        elif is_work and is_weekday:
            # Рабочие календари по будням показываем всегда, даже пустые
            lines.append(f"<b>{html.escape(label)}</b>")
            lines.append("Нет встреч.")
            lines.append("")
        # Личные пустые календари (и рабочие в выходные) просто пропускаем

    if not lines:
        return "<b>🗓️ Встречи сегодня:</b>\n\nВстреч нет."

    return "<b>🗓️ Встречи сегодня:</b>\n\n" + "\n".join(lines).strip()


def get_projects():
    # Название проектов нужно для группировки задач по разделам
    response = httpx.get(
        f"{TODOIST_API_BASE}/projects",
        headers={"Authorization": f"Bearer {TODOIST_TOKEN}"},
        params={"limit": 200},
        timeout=15,
    )
    response.raise_for_status()
    projects = response.json().get("results", [])
    return {p["id"]: p["name"] for p in projects}


PROJECT_EMOJI_RULES = [
    (("coloristo",), "🎨"),
    (("дач", "участ", "межев"), "🏡"),
    (("авито",), "🛍️"),
    (("банк", "сбер", "вклад"), "💰"),
    (("газ", "тгк", "жкс", "коммунал", "счетчик", "счётчик"), "🔧"),
    (("маш", "авто", "свеч", "фильтр"), "🚗"),
    (("здоров", "врач", "офтальм"), "🩺"),
    (("работа", "клиент", "aso", "асо"), "💼"),
    (("покуп", "магазин"), "🛒"),
    (("дом", "ремонт", "квартир"), "🛠️"),
]
FALLBACK_EMOJIS = ["✨", "📌", "🗂️", "🔹", "🌀", "🧩"]


def get_project_emoji(project_name: str, fallback_index: int) -> str:
    name_lower = project_name.lower()
    for keywords, emoji in PROJECT_EMOJI_RULES:
        if any(kw in name_lower for kw in keywords):
            return emoji
    return FALLBACK_EMOJIS[fallback_index % len(FALLBACK_EMOJIS)]


def format_urgent_section(tasks) -> str:
    lines = ["<b>🔥 Срочно на сегодня:</b>\n"]
    if not tasks:
        lines.append("Ничего горящего.")
        return "\n".join(lines)

    def format_due(due_info) -> str:
        if not due_info:
            return ""
        raw = due_info.get("date", "")
        if "T" in raw:
            date_part, time_part = raw.split("T")
            return f" (до {date_part} {time_part[:5]})"
        return f" (до {raw})"

    for t in tasks:
        lines.append(f"• {html.escape(t['content'])}{format_due(t.get('due'))}")
    return "\n".join(lines)


def get_week_tasks():
    response = httpx.get(
        f"{TODOIST_API_BASE}/tasks/filter",
        headers={"Authorization": f"Bearer {TODOIST_TOKEN}"},
        params={"query": "7 days"},
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("results", [])


async def tomorrow_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tasks = get_tomorrow_tasks()
        text = format_tomorrow_section(tasks)

        dym_status = get_dym_status(days_ahead=1)
        if dym_status:
            text = f"{dym_status}\n\n{text}"
    except Exception as e:
        logger.exception("Ошибка при получении задач на завтра")
        text = f"Не смогла получить задачи: {e}"
    await update.message.reply_text(text, parse_mode="HTML")


async def week_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tasks = get_week_tasks()
        text = format_tasks(tasks, title="📆 На неделю:")
    except Exception as e:
        logger.exception("Ошибка при получении задач на неделю")
        text = f"Не смогла получить задачи: {e}"
    await update.message.reply_text(text, parse_mode="HTML")


def get_tomorrow_tasks():
    response = httpx.get(
        f"{TODOIST_API_BASE}/tasks/filter",
        headers={"Authorization": f"Bearer {TODOIST_TOKEN}"},
        params={"query": "tomorrow"},
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("results", [])


def format_tomorrow_section(tasks) -> str:
    lines = ["<b>📅 Завтра:</b>\n"]
    if not tasks:
        lines.append("Ничего не запланировано.")
        return "\n".join(lines)

    for t in tasks:
        lines.append(f"• {html.escape(t['content'])}")
    return "\n".join(lines)


def build_full_report() -> str:
    urgent = get_today_tasks()
    urgent_ids = {t["id"] for t in urgent}

    tomorrow = get_tomorrow_tasks()

    all_tasks = get_all_tasks()
    rest = [t for t in all_tasks if t["id"] not in urgent_ids]

    parts = [
        format_urgent_section(urgent),
        format_tomorrow_section(tomorrow),
        format_tasks(rest, title="Остальные задачи:"),
    ]
    return "\n\n".join(parts)


def format_tasks(tasks, title="Задачи:") -> str:
    if not tasks:
        return "Задач нет. Можно выдохнуть."

    def format_due(due_info) -> str:
        if not due_info:
            return ""
        raw = due_info.get("date", "")
        if "T" in raw:
            date_part, time_part = raw.split("T")
            return f" (до {date_part} {time_part[:5]})"
        return f" (до {raw})"

    projects = get_projects()

    grouped = {}
    for t in tasks:
        pid = t.get("project_id")
        grouped.setdefault(pid, []).append(t)

    lines = [f"<b>{html.escape(title)}</b>\n"]
    for i, (pid, group) in enumerate(grouped.items()):
        project_name = projects.get(pid, "Без проекта")
        emoji = get_project_emoji(project_name, i)
        lines.append(f"<b>{emoji} {html.escape(project_name)}</b>")
        for t in group:
            lines.append(f"• {html.escape(t['content'])}{format_due(t.get('due'))}")
        lines.append("")

    return "\n".join(lines).strip()


def create_todoist_task(content: str):
    response = httpx.post(
        f"{TODOIST_API_BASE}/tasks",
        headers={"Authorization": f"Bearer {TODOIST_TOKEN}"},
        json={"content": content},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def find_todoist_task_by_text(text: str):
    response = httpx.get(
        f"{TODOIST_API_BASE}/tasks/filter",
        headers={"Authorization": f"Bearer {TODOIST_TOKEN}"},
        params={"query": f"search: {text}"},
        timeout=15,
    )
    response.raise_for_status()
    results = response.json().get("results", [])
    return results[0] if results else None


def upload_todoist_file(file_bytes: bytes, filename: str) -> dict:
    response = httpx.post(
        f"{TODOIST_API_BASE}/uploads",
        headers={"Authorization": f"Bearer {TODOIST_TOKEN}"},
        files={"file": (filename, file_bytes, "application/pdf")},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def add_todoist_comment_with_attachment(task_id: str, content: str, upload: dict):
    response = httpx.post(
        f"{TODOIST_API_BASE}/comments",
        headers={"Authorization": f"Bearer {TODOIST_TOKEN}"},
        json={
            "task_id": task_id,
            "content": content,
            "attachment": {
                "resource_type": "file",
                "file_url": upload["file_url"],
                "file_name": upload["file_name"],
                "file_type": upload.get("file_type", "application/pdf"),
            },
        },
        timeout=15,
    )
    response.raise_for_status()


def close_todoist_task(task_id: str):
    response = httpx.post(
        f"{TODOIST_API_BASE}/tasks/{task_id}/close",
        headers={"Authorization": f"Bearer {TODOIST_TOKEN}"},
        timeout=15,
    )
    response.raise_for_status()


def get_completed_tasks_today():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)

    response = httpx.get(
        f"{TODOIST_API_BASE}/tasks/completed/by_completion_date",
        headers={"Authorization": f"Bearer {TODOIST_TOKEN}"},
        params={
            "since": start_of_day.isoformat(),
            "until": now.isoformat(),
            "limit": 100,
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("items", [])


async def send_evening_summary(app: Application):
    try:
        completed = get_completed_tasks_today()
    except Exception as e:
        logger.exception("Ошибка при получении выполненных задач для вечерней сводки")
        await app.bot.send_message(chat_id=CHAT_ID, text=f"Не смогла получить список сделанного: {e}")
        return

    lines = ["<b>✅ Сделано сегодня:</b>\n"]
    if completed:
        for item in completed:
            lines.append(f"• {html.escape(item.get('content', 'Без названия'))}")
    else:
        lines.append("Пока ничего не отмечено выполненным.")

    await app.bot.send_message(chat_id=CHAT_ID, text="\n".join(lines), parse_mode="HTML")


def get_all_tasks():
    # Новый API отдаёт результат постранично (cursor), собираем все страницы.
    all_tasks = []
    cursor = None

    while True:
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor

        response = httpx.get(
            f"{TODOIST_API_BASE}/tasks",
            headers={"Authorization": f"Bearer {TODOIST_TOKEN}"},
            params=params,
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()

        all_tasks.extend(data.get("results", []))
        cursor = data.get("next_cursor")
        if not cursor:
            break

    return all_tasks


def get_today_only_tasks():
    response = httpx.get(
        f"{TODOIST_API_BASE}/tasks/filter",
        headers={"Authorization": f"Bearer {TODOIST_TOKEN}"},
        params={"query": "today"},
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("results", [])


def get_overdue_tasks():
    response = httpx.get(
        f"{TODOIST_API_BASE}/tasks/filter",
        headers={"Authorization": f"Bearer {TODOIST_TOKEN}"},
        params={"query": "overdue"},
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("results", [])


def get_important_deadline_tasks():
    response = httpx.get(
        f"{TODOIST_API_BASE}/tasks/filter",
        headers={"Authorization": f"Bearer {TODOIST_TOKEN}"},
        params={"query": f"@{IMPORTANT_DEADLINE_LABEL}"},
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("results", [])


def get_today_tasks():
    # Оставлено для рассылки/остальных мест, где нужны "сегодня + просрочено" вместе
    return get_today_only_tasks() + get_overdue_tasks()


def get_backlog_tasks():
    # Задачи без даты выполнения — фильтруем локально из полного списка
    tasks = get_all_tasks()
    return [t for t in tasks if not t.get("due")]


async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = build_full_report()
    except Exception as e:
        logger.exception("Ошибка при получении задач")
        text = f"Не смогла получить задачи: {e}"
    await update.message.reply_text(text, parse_mode="HTML")


def format_two_sections(today_tasks, overdue_tasks) -> str:
    def format_due(due_info) -> str:
        if not due_info:
            return ""
        raw = due_info.get("date", "")
        if "T" in raw:
            date_part, time_part = raw.split("T")
            return f" (до {time_part[:5]})"
        return ""

    lines = []

    lines.append("<b>📅 Сегодня:</b>")
    if today_tasks:
        for t in today_tasks:
            lines.append(f"• {html.escape(t['content'])}{format_due(t.get('due'))}")
    else:
        lines.append("Ничего на сегодня.")

    lines.append("")
    lines.append("<b>⏰ Просрочено:</b>")
    if overdue_tasks:
        for t in overdue_tasks:
            lines.append(f"• {html.escape(t['content'])}{format_due(t.get('due'))}")
    else:
        lines.append("Просроченного нет.")

    return "\n".join(lines)


async def listcalendars_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not CALENDAR_ENABLED:
        await update.message.reply_text("Календарь ещё не подключен.")
        return
    try:
        calendars = list_google_calendars(GOOGLE_REFRESH_TOKEN)
    except Exception as e:
        logger.exception("Ошибка при получении списка календарей")
        await update.message.reply_text(f"Не смогла получить список: {e}")
        return

    lines = ["Твои календари в этом аккаунте:\n"]
    for c in calendars:
        lines.append(f"• {c.get('summary', 'Без названия')}\n  id: {c.get('id')}")
    await update.message.reply_text("\n".join(lines))


async def whoop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not WHOOP_ENABLED:
        await update.message.reply_text("WHOOP ещё не подключен.")
        return
    try:
        text = get_whoop_summary()
    except Exception as e:
        logger.exception("Ошибка при получении WHOOP сводки")
        text = f"Не смогла получить данные WHOOP: {e}"
    await update.message.reply_text(text, parse_mode="HTML")


async def calendar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not CALENDAR_ENABLED:
        await update.message.reply_text("Календарь ещё не подключен.")
        return
    try:
        events = get_today_calendar_events()
        text = format_calendar_section(events)
    except Exception as e:
        logger.exception("Ошибка при получении событий календаря")
        text = f"Не смогла получить события: {e}"
    await update.message.reply_text(text, parse_mode="HTML")


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        today_tasks = get_today_only_tasks()
        overdue_tasks = get_overdue_tasks()
        tasks_text = format_two_sections(today_tasks, overdue_tasks)

        parts = []

        dym_status = get_dym_status()
        if dym_status:
            parts.append(dym_status)

        weather = get_weather_forecast()
        if weather:
            parts.append(weather)

        parts.append(tasks_text)

        if CALENDAR_ENABLED:
            try:
                events = get_today_calendar_events()
                parts.append(format_calendar_section(events))
            except Exception:
                logger.exception("Ошибка при получении календаря")
                parts.append("🗓️ Календарь: не удалось получить.")

        if WHOOP_ENABLED:
            try:
                parts.append(get_whoop_summary())
            except Exception:
                logger.exception("Ошибка при получении WHOOP")
                parts.append("💪 WHOOP: не удалось получить.")

        text = "\n\n".join(parts)
    except Exception as e:
        logger.exception("Ошибка при получении задач на сегодня")
        text = f"Не смогла получить задачи: {e}"
    await update.message.reply_text(text, parse_mode="HTML")


async def backlog_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tasks = get_backlog_tasks()
        text = format_tasks(tasks, title="Задачи без даты (беклог):")
    except Exception as e:
        logger.exception("Ошибка при получении беклога")
        text = f"Не смогла получить задачи: {e}"
    await update.message.reply_text(text, parse_mode="HTML")


async def send_daily_summary(app: Application):
    try:
        today_tasks = get_today_only_tasks()
        overdue_tasks = get_overdue_tasks()
        tasks_text = format_two_sections(today_tasks, overdue_tasks)

        parts = []

        dym_status = get_dym_status()
        if dym_status:
            parts.append(dym_status)

        weather = get_weather_forecast()
        if weather:
            parts.append(weather)

        parts.append(tasks_text)

        if CALENDAR_ENABLED:
            try:
                events = get_today_calendar_events()
                parts.append(format_calendar_section(events))
            except Exception:
                logger.exception("Ошибка при получении календаря для рассылки")
                parts.append("🗓️ Календарь: не удалось получить.")

        if WHOOP_ENABLED:
            try:
                parts.append(get_whoop_summary())
            except Exception:
                logger.exception("Ошибка при получении WHOOP для рассылки")
                parts.append("💪 WHOOP: не удалось получить.")

        text = "\n\n".join(parts)
    except Exception as e:
        logger.exception("Ошибка при получении задач для рассылки")
        text = f"Не смогла получить задачи: {e}"
    await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML")


MAIN_KEYBOARD_ROWS = [
    ["📅 Сегодня", "🗂️ Беклог"],
    ["📋 Все задачи", "➡️ Завтра"],
    ["📆 Неделя", "🗓️ Календарь"],
    ["💪 WHOOP", "✅ Отметить сделанное"],
    ["💧 Вода", "📊 Статус"],
    ["❤️ Для Саши", "😬 Юля, пей водичку"],
    ["📬 Почта"],
]

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    MAIN_KEYBOARD_ROWS,
    resize_keyboard=True,
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет. Можешь пользоваться кнопками внизу или командами:\n"
        "/today — сегодня и просроченные, отдельными блоками\n"
        "/backlog — задачи без даты\n"
        "/tasks — срочное + завтра + всё остальное по проектам\n"
        "/status — проверка, что все интеграции живы (Todoist/Calendar/WHOOP)\n"
        "/water — сколько воды выпито сегодня + отметить ещё\n"
        "/birthdays — ближайшие ДР из календаря\n"
        f"/deadlines — задачи с лейблом «{IMPORTANT_DEADLINE_LABEL}» и приближающимся дедлайном\n"
        "/mail — срочное из непрочитанной почты\n\n"
        f"Ежедневная сводка (сегодня/просрочено) приходит в {SEND_TIME} ({TIMEZONE}).",
        reply_markup=MAIN_KEYBOARD,
    )


def get_whoop_mood_hint(recovery_score, sleep_performance) -> str:
    # Мягкая, не медицинская интерпретация — просто чтобы Саша понимал, как аккуратнее быть
    if recovery_score is None:
        return ""

    if recovery_score >= 67:
        note = "чувствует себя бодро, всё в порядке 💚"
    elif recovery_score >= 34:
        note = "немного уставшая, будь к ней бережнее сегодня 💛"
    else:
        note = "организм просит отдыха — поддержи её и не грузи сегодня ❤️❗"

    if sleep_performance is not None and sleep_performance < 70:
        note += "\nСпала не очень хорошо, может быть более чувствительной."

    return note


def get_whoop_advice(recovery_score, sleep_performance, yesterday_strain) -> str:
    tips = []

    if recovery_score is not None and recovery_score < 34:
        tips.append("Стоит поспать днём и не планировать сегодня тяжёлую тренировку.")
    elif recovery_score is not None and recovery_score >= 67 and (yesterday_strain is None or yesterday_strain < 14):
        tips.append("Отличный день для тренировки, если хочется.")
    elif recovery_score is not None:
        tips.append("Сегодня лучше в спокойном темпе, без перегрузок.")

    if sleep_performance is not None and sleep_performance < 70:
        tips.append("Стоит лечь спать пораньше сегодня.")

    return " ".join(tips)


async def for_sasha_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        today_tasks = get_today_only_tasks()
    except Exception as e:
        logger.exception("Ошибка при получении задач для Саши")
        await update.message.reply_text(f"Не смогла получить задачи: {e}")
        return

    lines = []

    dym_status = get_dym_status()
    if dym_status:
        lines.append(f"{dym_status}\n")

    weather = get_weather_current()
    if weather:
        lines.append(f"📍 {WEATHER_CITY_LABEL}: {html.escape(weather)}\n")

    lines.append("<b>❤️ Сегодня у Юлечки такие вот дела:</b>\n")

    if today_tasks:
        for t in today_tasks:
            lines.append(f"• {html.escape(t['content'])}")
    else:
        lines.append("Задач на сегодня нет.")

    # Встречи из календаря — просто плоский список, без деления по проектам/аккаунтам
    all_events = []
    if CALENDAR_ENABLED:
        try:
            calendars = get_today_calendar_events()
            for _, events, _ in calendars:
                all_events.extend(events)
            if all_events:
                from datetime import datetime
                from zoneinfo import ZoneInfo

                lines.append("\n<b>🗓️ Встречи:</b>")
                for e in all_events:
                    title = html.escape(e.get("summary", "Без названия"))
                    start = e.get("start", {})
                    time_str = ""
                    if "dateTime" in start:
                        dt = datetime.fromisoformat(start["dateTime"])
                        msk_time = dt.astimezone(ZoneInfo("Europe/Moscow")).strftime("%H:%M")
                        belgrade_time = dt.astimezone(ZoneInfo("Europe/Belgrade")).strftime("%H:%M")
                        time_str = f"{msk_time} МСК / {belgrade_time} Белград — "
                    lines.append(f"• {time_str}{title}")
        except Exception:
            logger.exception("Ошибка при получении календаря для Саши")

    if not today_tasks and not all_events:
        lines.append("Можно просто написать ей тёплое сообщение.")

    if WHOOP_ENABLED:
        try:
            access_token = get_whoop_access_token()
            headers = {"Authorization": f"Bearer {access_token}"}

            recovery_score = None
            sleep_performance = None
            sleep_hours = None
            sleep_minutes = None
            yesterday_strain = None

            r = httpx.get(
                "https://api.prod.whoop.com/developer/v2/recovery",
                headers=headers,
                params={"limit": 1},
                timeout=15,
            )
            r.raise_for_status()
            records = r.json().get("records", [])
            if records and records[0].get("score_state") == "SCORED":
                recovery_score = records[0]["score"]["recovery_score"]

            s = httpx.get(
                "https://api.prod.whoop.com/developer/v2/activity/sleep",
                headers=headers,
                params={"limit": 10},
                timeout=15,
            )
            s.raise_for_status()
            all_sleep_records = s.json().get("records", [])
            sleep_records = [
                r for r in all_sleep_records
                if r.get("score_state") == "SCORED" and not r.get("nap", False)
            ]
            if sleep_records:
                stage = sleep_records[0]["score"]["stage_summary"]
                total_ms = (
                    stage["total_light_sleep_time_milli"]
                    + stage["total_slow_wave_sleep_time_milli"]
                    + stage["total_rem_sleep_time_milli"]
                )
                sleep_hours = total_ms // 3600000
                sleep_minutes = (total_ms % 3600000) // 60000
                sleep_performance = sleep_records[0]["score"].get("sleep_performance_percentage")

            c = httpx.get(
                "https://api.prod.whoop.com/developer/v2/cycle",
                headers=headers,
                params={"limit": 2},
                timeout=15,
            )
            c.raise_for_status()
            cycle_records = c.json().get("records", [])
            yesterday_cycle = cycle_records[1] if len(cycle_records) > 1 else None
            if yesterday_cycle and yesterday_cycle.get("score_state") == "SCORED":
                yesterday_strain = yesterday_cycle["score"]["strain"]

            if sleep_hours is not None:
                lines.append(f"\n😴 Спала {sleep_hours}ч {sleep_minutes}м")

            mood_hint = get_whoop_mood_hint(recovery_score, sleep_performance)
            if mood_hint:
                lines.append(f"\n{mood_hint}")

            advice = get_whoop_advice(recovery_score, sleep_performance, yesterday_strain)
            if advice:
                lines.append(f"\n💡 {advice}")
        except Exception:
            logger.exception("Ошибка при получении WHOOP данных для Саши")

    text = "\n".join(lines)

    # Присылаем ей самой — дальше она пересылает это сообщение Саше вручную
    await update.message.reply_text(text, parse_mode="HTML")


# Распознавание голосовых сообщений — работает локально на сервере бота,
# без сторонних платных сервисов. Модель загружается один раз при первом
# использовании и хранится в памяти.
_whisper_model = None


def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        # "base" — компромисс между скоростью и качеством, подходит для CPU
        _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
    return _whisper_model


def transcribe_voice(file_path: str) -> str:
    model = get_whisper_model()
    segments, _ = model.transcribe(file_path, language="ru")
    return " ".join(segment.text for segment in segments).strip()


# Хранит пронумерованный список задач между сообщением /done и ответом с номером.
# В памяти процесса — этого достаточно, раз ботом пользуется один человек.
_pending_done_lists = {}


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        overdue_tasks = get_overdue_tasks()
        today_tasks = get_today_only_tasks()
        all_tasks = overdue_tasks + today_tasks
    except Exception as e:
        logger.exception("Ошибка при получении задач для /done")
        await update.message.reply_text(f"Не смогла получить задачи: {e}")
        return

    if not all_tasks:
        await update.message.reply_text("На сегодня нечего отмечать — всё чисто.")
        return

    chat_id = update.effective_chat.id
    numbered = {}
    lines = ["<b>Что сделано?</b> Ответь номером задачи:\n"]
    for i, t in enumerate(all_tasks, start=1):
        numbered[i] = (t["id"], t["content"])
        lines.append(f"{i}. {html.escape(t['content'])}")

    _pending_done_lists[chat_id] = numbered
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def handle_done_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    # Возвращает True, если сообщение было обработано как ответ на /done
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    if not text.isdigit() or chat_id not in _pending_done_lists:
        return False

    number = int(text)
    pending = _pending_done_lists[chat_id]

    if number not in pending:
        await update.message.reply_text("Нет такого номера в списке. Напиши /done ещё раз, если список устарел.")
        return True

    task_id, content = pending.pop(number)
    try:
        close_todoist_task(task_id)
        await update.message.reply_text(f"✅ Готово: {html.escape(content)}", parse_mode="HTML")
    except Exception as e:
        logger.exception("Ошибка при закрытии задачи через /done")
        await update.message.reply_text(f"Не смогла отметить задачу: {e}")

    if not pending:
        del _pending_done_lists[chat_id]

    return True


async def voice_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎙️ Слушаю...")

    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    file_path = f"/tmp/voice_{voice.file_id}.ogg"

    try:
        await file.download_to_drive(file_path)
        text = transcribe_voice(file_path)
    except Exception as e:
        logger.exception("Ошибка при распознавании голоса")
        await update.message.reply_text(f"Не смогла распознать голос: {e}")
        return
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

    if not text:
        await update.message.reply_text("Не расслышала, попробуй ещё раз или скажи чуть чётче.")
        return

    await update.message.reply_text(f'Распознала: «{html.escape(text)}»')

    drink_results = classify_drinks_from_text(text)
    if drink_results:
        await update.message.reply_text(log_classified_drinks(drink_results), parse_mode="HTML")
        return

    if DRINK_AI_ENABLED:
        result = classify_forwarded_message(text)
        if result and result.get("type") in ("task", "event", "unclear"):
            await _handle_forward_result(update, result)
            return

    # Фолбэк (ИИ недоступен, или классификация не дала ответа) — старое поведение: всё в Todoist
    await update.message.reply_text("Добавляю в Todoist...")
    try:
        create_todoist_task(text)
        await update.message.reply_text("✅ Добавила в Todoist.")
    except Exception as e:
        logger.exception("Ошибка при добавлении задачи из голоса")
        await update.message.reply_text(f"Не смогла добавить задачу: {e}")


async def photo_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not DRINK_AI_ENABLED:
        await update.message.reply_text(
            "Распознавание фото не настроено — добавь ANTHROPIC_API_KEY в переменные Railway."
        )
        return

    await update.message.reply_text("📷 Смотрю...")

    photo = update.message.photo[-1]  # последний элемент — самое высокое разрешение
    file = await context.bot.get_file(photo.file_id)
    file_path = f"/tmp/photo_{photo.file_id}.jpg"

    try:
        await file.download_to_drive(file_path)
        with open(file_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode()
    except Exception as e:
        logger.exception("Ошибка при загрузке фото")
        await update.message.reply_text(f"Не смогла обработать фото: {e}")
        return
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

    drink_results = classify_drinks_from_image(image_b64, "image/jpeg")
    if drink_results:
        await update.message.reply_text(log_classified_drinks(drink_results), parse_mode="HTML")
        return

    forward_result = classify_forwarded_image(image_b64, "image/jpeg")
    if forward_result:
        await _handle_forward_result(update, forward_result)
        return

    await update.message.reply_text(
        "Не поняла, что на фото — если это напиток, отметь вручную через /water; "
        "если задача или событие, добавь вручную."
    )


async def water_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today_ml = get_water_today_ml()
    text = (
        f"Всего за день: {format_drinks_today()}\n"
        f"💧 Из них вода: <b>{today_ml} мл</b>\n\nОтметить:"
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=drinks_keyboard())


async def drink_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, drink_type, amount_str = query.data.split("_")
        amount = int(amount_str)
    except (ValueError, IndexError):
        return

    if drink_type not in DRINK_TYPES:
        return

    add_drink(drink_type, amount)
    meta = DRINK_TYPES[drink_type]
    text = (
        f"{meta['emoji']} +{amount} {meta['unit']} записано.\n"
        f"Всего за день: {format_drinks_today()}\n"
        f"💧 Из них вода: {get_water_today_ml()} мл"
    )
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=drinks_keyboard())
    except Exception:
        # Если сообщение уже устарело/удалено — просто шлём новое, не критично
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=drinks_keyboard())



def check_upcoming_birthdays() -> list[str]:
    if not CALENDAR_ENABLED:
        return []

    from datetime import datetime
    from zoneinfo import ZoneInfo

    days_ahead = max(BIRTHDAY_REMINDER_DAYS) + 1
    events = []

    # Календарь "ДР в Фридым" — там сплошь дни рождения, берём всё как есть
    try:
        events += get_calendar_events_range(GOOGLE_REFRESH_TOKEN, BIRTHDAY_CALENDAR_ID, days_ahead)
    except Exception:
        logger.exception("Ошибка получения календаря ДР в Фридым")

    # Основной календарь — там вперемешку разное, берём только события с меткой 🎂
    # (так их помечает /addbirthdays)
    try:
        primary_events = get_calendar_events_range(GOOGLE_REFRESH_TOKEN, "primary", days_ahead)
        events += [e for e in primary_events if e.get("summary", "").strip().startswith("🎂")]
    except Exception:
        logger.exception("Ошибка получения основного календаря для ДР")

    today = datetime.now(ZoneInfo(TIMEZONE)).date()
    lines = []
    for event in events:
        start = event.get("start", {})
        date_str = start.get("date") or (start.get("dateTime", "")[:10] if start.get("dateTime") else None)
        if not date_str:
            continue
        try:
            event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        delta = (event_date - today).days
        if delta not in BIRTHDAY_REMINDER_DAYS:
            continue
        summary = event.get("summary", "День рождения").lstrip("🎂").strip()
        if delta == 0:
            lines.append(f"🎂 Сегодня: {summary}!")
        else:
            lines.append(f"🎂 Через {delta} дн. ({event_date.strftime('%d.%m')}): {summary}")
    return lines


async def birthday_reminder_job(app: Application):
    if not BIRTHDAY_REMINDERS_ENABLED:
        return
    lines = check_upcoming_birthdays()
    if lines:
        await app.bot.send_message(chat_id=CHAT_ID, text="\n".join(lines))


def check_important_deadlines() -> list[str]:
    from datetime import datetime

    try:
        tasks = get_important_deadline_tasks()
    except Exception:
        logger.exception("Ошибка получения задач с дедлайном для напоминаний")
        return []

    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo(TIMEZONE)).date()
    lines = []
    for task in tasks:
        due = task.get("due")
        if not due or not due.get("date"):
            continue
        try:
            due_date = datetime.strptime(due["date"][:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        delta = (due_date - today).days
        content = task.get("content", "Задача")
        if delta < 0:
            lines.append(f"🔴 Просрочено на {-delta} дн.: {content}")
        elif delta == 0:
            lines.append(f"🔴 Сегодня дедлайн: {content}")
        elif delta in DEADLINE_REMINDER_DAYS:
            lines.append(f"⏳ Осталось {delta} дн. ({due_date.strftime('%d.%m')}): {content}")
    return lines


def _sender_domain(from_header: str) -> str:
    match = re.search(r"@([\w.-]+)", from_header)
    return match.group(1).lower() if match else ""


def classify_gmail_message(msg: dict) -> dict:
    domain = _sender_domain(msg["from"])
    from_lower = msg["from"].lower()
    subject_lower = msg["subject"].lower()

    if any(d in domain for d in GMAIL_SKIP_DOMAINS):
        return {"bucket": "skip"}
    if GMAIL_LENSBYT_MATCH in from_lower:
        return {"bucket": "lensbyt"}
    if any(k in subject_lower for k in GMAIL_STOCK_ALERT_KEYWORDS):
        return {"bucket": "urgent", "reason": "товар в наличии"}
    if any(d in domain for d in GMAIL_LINKEDIN_DOMAINS):
        return {"bucket": "urgent", "reason": "LinkedIn"}
    if any(d in domain for d in GMAIL_DELIVERY_DOMAINS):
        return {"bucket": "urgent", "reason": "посылка"}
    if any(d in domain for d in GMAIL_AVITO_DOMAINS):
        return {"bucket": "check_avito"}
    if any(d in domain for d in GMAIL_BANK_DOMAINS):
        return {"bucket": "check_bank"}
    return {"bucket": "check_general"}


def classify_email_action_needed(msg: dict, context_hint: str) -> bool:
    if not DRINK_AI_ENABLED:
        return False
    prompt = (
        f"Письмо: от {msg['from']}, тема: «{msg['subject']}», начало текста: «{msg['snippet']}».\n\n"
        f"{context_hint}\n\n"
        'Верни ТОЛЬКО JSON: {"needs_action": true|false}'
    )
    result = _call_anthropic_json(prompt)
    return bool(result and result.get("needs_action"))


def handle_lensbyt_email(msg: dict) -> str:
    task = find_todoist_task_by_text(GMAIL_LENSBYT_TODOIST_TASK_SEARCH)
    if not task:
        return f"⚠️ Письмо от ленсбыт («{msg['subject']}»), но не нашла задачу «{GMAIL_LENSBYT_TODOIST_TASK_SEARCH}» в Todoist."
    try:
        full = get_gmail_message_full(msg["id"])
        attachment_info = find_pdf_attachment(full.get("payload", {}))
        if not attachment_info:
            return f"✉️ Письмо от ленсбыт («{msg['subject']}») — без PDF-вложения, просто к сведению."
        pdf_bytes = get_gmail_attachment_bytes(msg["id"], attachment_info["attachment_id"])
        upload = upload_todoist_file(pdf_bytes, attachment_info["filename"])
        add_todoist_comment_with_attachment(task["id"], f"Квитанция из письма: {msg['subject']}", upload)
        return f"📎 Квитанция от ленсбыт добавлена в задачу «{task['content']}»"
    except Exception:
        logger.exception("Ошибка обработки письма ленсбыт")
        return f"⚠️ Не смогла обработать письмо от ленсбыт («{msg['subject']}»)."


def build_email_digest_lines() -> list[str]:
    if not GOOGLE_REFRESH_TOKEN:
        return []
    try:
        messages = list_unread_gmail_messages()
    except Exception:
        logger.exception("Ошибка получения списка писем Gmail")
        return []

    lines = []
    for m in messages:
        msg_id = m["id"]
        if msg_id in _seen_email_ids:
            continue
        try:
            msg = get_gmail_message_metadata(msg_id)
        except Exception:
            logger.exception("Ошибка получения письма Gmail")
            continue
        _seen_email_ids.add(msg_id)

        result = classify_gmail_message(msg)
        bucket = result["bucket"]

        if bucket == "skip":
            continue
        elif bucket == "lensbyt":
            lines.append(handle_lensbyt_email(msg))
        elif bucket == "urgent":
            reason = result.get("reason", "")
            suffix = f" ({reason})" if reason else ""
            lines.append(f"🔴 {msg['from']}: {msg['subject']}{suffix}")
        elif bucket == "check_avito":
            if classify_email_action_needed(
                msg,
                "Это письмо от Avito. Важно только если там про забрать/вернуть товар или получить "
                "деньги — обычные переписки без конкретного действия не считаются.",
            ):
                lines.append(f"🔴 Avito: {msg['subject']}")
        elif bucket == "check_bank":
            if classify_email_action_needed(
                msg,
                "Это письмо от банка. Важно только если реально требуется действие (оплатить, "
                "подтвердить, среагировать) — не обычная информационная рассылка или выписка.",
            ):
                lines.append(f"🔴 {msg['from']}: {msg['subject']}")
        elif bucket == "check_general":
            if classify_email_action_needed(
                msg,
                "Обычное письмо не из известного списка отправителей. Определи, ждут ли от Юли "
                "ответа или какого-то действия прямо сейчас.",
            ):
                lines.append(f"✉️ {msg['from']}: {msg['subject']}")

    return lines


async def mail_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not GOOGLE_REFRESH_TOKEN:
        await update.message.reply_text("Google-аккаунт не настроен.")
        return
    await update.message.reply_text("📬 Проверяю почту...")
    lines = build_email_digest_lines()
    if lines:
        text = "<b>📬 Срочное из почты:</b>\n" + "\n".join(lines)
    else:
        text = "📬 В непрочитанных писем ничего срочного не нашла."
    await update.message.reply_text(text, parse_mode="HTML")


async def email_digest_job(app: Application):
    if not EMAIL_DIGEST_ENABLED:
        return
    lines = build_email_digest_lines()
    if lines:
        text = "<b>📬 Срочное из почты:</b>\n" + "\n".join(lines)
        await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML")


async def deadline_reminder_job(app: Application):
    if not DEADLINE_REMINDERS_ENABLED:
        return
    lines = check_important_deadlines()
    if lines:
        text = f"<b>Дедлайны по «{IMPORTANT_DEADLINE_LABEL}»:</b>\n" + "\n".join(lines)
        await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML")


async def birthdays_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = check_upcoming_birthdays()
    text = "\n".join(lines) if lines else f"Ближайших {max(BIRTHDAY_REMINDER_DAYS)} дней без ДР в календаре."
    await update.message.reply_text(text)


# Чаты, ожидающие список ДР для импорта после команды /addbirthdays
_pending_birthday_import = set()


async def addbirthdays_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _pending_birthday_import.add(update.effective_chat.id)
    await update.message.reply_text(
        "Пришли список ДР одним сообщением, в любом формате — например:\n"
        "Аня — 15 марта\nСаша — 3 сентября 1990"
    )


async def handle_birthday_import(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat_id = update.effective_chat.id
    if chat_id not in _pending_birthday_import:
        return False
    _pending_birthday_import.discard(chat_id)

    if not CALENDAR_ENABLED:
        await update.message.reply_text("Календарь не настроен — не могу добавить.")
        return True
    if not DRINK_AI_ENABLED:
        await update.message.reply_text("Нужен ANTHROPIC_API_KEY, чтобы разбирать список — сейчас не настроен.")
        return True

    entries = classify_birthday_list(update.message.text)
    if not entries:
        await update.message.reply_text("Не смогла разобрать ни одной записи — попробуй ещё раз, по-другому.")
        return True

    added = []
    failed = []
    for entry in entries:
        name = entry.get("name") or "Без имени"
        month = entry.get("month")
        day = entry.get("day")
        year = entry.get("year")
        if not month or not day:
            failed.append(name)
            continue
        try:
            create_recurring_birthday_event(
                GOOGLE_REFRESH_TOKEN, "primary", f"🎂 {name}", month, day, year
            )
            added.append(f"{name} ({day:02d}.{month:02d}{f'.{year}' if year else ''})")
        except Exception:
            logger.exception("Ошибка при добавлении ДР в календарь: %s", name)
            failed.append(name)

    lines = []
    if added:
        lines.append("✅ Добавила:\n" + "\n".join(added))
    if failed:
        lines.append("⚠️ Не разобрала:\n" + "\n".join(failed))
    await update.message.reply_text("\n\n".join(lines))
    return True


async def deadlines_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = check_important_deadlines()
    if lines:
        text = f"<b>Дедлайны по «{IMPORTANT_DEADLINE_LABEL}»:</b>\n" + "\n".join(lines)
    else:
        text = f"Нет задач с лейблом «{IMPORTANT_DEADLINE_LABEL}» с приближающимся дедлайном."
    await update.message.reply_text(text, parse_mode="HTML")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["<b>📊 Статус интеграций</b>\n"]

    try:
        get_today_only_tasks()
        lines.append("✅ Todoist")
    except Exception:
        logger.exception("Ошибка проверки Todoist в /status")
        lines.append("⚠️ Todoist — не отвечает, проверь токен")

    if CALENDAR_ENABLED:
        accounts = [("основной", GOOGLE_REFRESH_TOKEN)]
        accounts += [
            (c["label"], c["refresh_token"]) for c in EXTRA_CALENDARS if c["refresh_token"]
        ]
        for label, token in accounts:
            try:
                get_google_access_token(token)
                lines.append(f"✅ Google Calendar ({label})")
            except Exception:
                logger.exception("Ошибка проверки Google Calendar (%s) в /status", label)
                lines.append(
                    f"⚠️ Google Calendar ({label}) — токен не сработал, возможна нужна реавторизация"
                )
    else:
        lines.append("⏸️ Google Calendar не настроен")

    if WHOOP_ENABLED:
        try:
            get_whoop_access_token()
            lines.append("✅ WHOOP")
        except Exception:
            logger.exception("Ошибка проверки WHOOP в /status")
            lines.append("⚠️ WHOOP — токен не сработал, может понадобиться реавторизация через Colab")
    else:
        lines.append("⏸️ WHOOP не настроен")

    if DRINK_AI_ENABLED:
        lines.append("✅ Claude API (распознавание напитков)")
    else:
        lines.append("⏸️ Claude API — нет ANTHROPIC_API_KEY, распознавание напитков не работает")

    if GOOGLE_REFRESH_TOKEN:
        try:
            list_unread_gmail_messages(max_results=1)
            lines.append("✅ Gmail")
        except Exception:
            logger.exception("Ошибка проверки Gmail в /status")
            lines.append("⚠️ Gmail — не отвечает, возможно у токена нет scope gmail.readonly")
    else:
        lines.append("⏸️ Gmail не настроен")

    lines.append(f"\n💧 Вода сегодня: {get_water_today_ml()} мл")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Присылаем ей самой — дальше она пересылает Ане/Саше вручную, как с "Для Саши"
    text = generate_drink_report()
    await update.message.reply_text(text)


async def menu_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if await handle_done_number(update, context):
        return

    if await handle_birthday_import(update, context):
        return

    if await handle_forward_clarification(update, context):
        return

    if await handle_event_correction(update, context):
        return

    if text == "📅 Сегодня":
        await today_command(update, context)
    elif text == "🗂️ Беклог":
        await backlog_command(update, context)
    elif text == "📋 Все задачи":
        await tasks_command(update, context)
    elif text == "➡️ Завтра":
        await tomorrow_command(update, context)
    elif text == "📆 Неделя":
        await week_command(update, context)
    elif text == "🗓️ Календарь":
        await calendar_command(update, context)
    elif text == "💪 WHOOP":
        await whoop_command(update, context)
    elif text == "✅ Отметить сделанное":
        await done_command(update, context)
    elif text == "❤️ Для Саши":
        await for_sasha_handler(update, context)
    elif text == "💧 Вода":
        await water_command(update, context)
    elif text == "📊 Статус":
        await status_command(update, context)
    elif text == "😬 Юля, пей водичку":
        await report_command(update, context)
    elif text == "📬 Почта":
        await mail_command(update, context)
    else:
        drink_results = classify_drinks_from_text(text) if DRINK_AI_ENABLED else []
        if drink_results:
            await update.message.reply_text(log_classified_drinks(drink_results), parse_mode="HTML")
        else:
            await update.message.reply_text("Не поняла, воспользуйся кнопками внизу.")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("tasks", tasks_command))
    app.add_handler(CommandHandler("today", today_command))
    app.add_handler(CommandHandler("backlog", backlog_command))
    app.add_handler(CommandHandler("tomorrow", tomorrow_command))
    app.add_handler(CommandHandler("week", week_command))
    app.add_handler(CommandHandler("calendar", calendar_command))
    app.add_handler(CommandHandler("listcalendars", listcalendars_command))
    app.add_handler(CommandHandler("whoop", whoop_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("birthdays", birthdays_command))
    app.add_handler(CommandHandler("addbirthdays", addbirthdays_command))
    app.add_handler(CommandHandler("deadlines", deadlines_command))
    app.add_handler(CommandHandler("mail", mail_command))
    app.add_handler(CommandHandler("water", water_command))
    app.add_handler(CallbackQueryHandler(drink_button_handler, pattern=r"^drink_\w+_\d+$"))
    app.add_handler(MessageHandler(filters.VOICE, voice_message_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_message_handler))
    app.add_handler(
        MessageHandler(filters.FORWARDED & filters.TEXT & ~filters.COMMAND, forwarded_message_handler)
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_button_handler))

    hour, minute = map(int, SEND_TIME.split(":"))
    evening_hour, evening_minute = map(int, EVENING_SEND_TIME.split(":"))
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(
        send_daily_summary,
        "cron",
        hour=hour,
        minute=minute,
        args=[app],
    )
    scheduler.add_job(
        send_evening_summary,
        "cron",
        hour=evening_hour,
        minute=evening_minute,
        args=[app],
    )
    if BIRTHDAY_REMINDERS_ENABLED:
        scheduler.add_job(
            birthday_reminder_job,
            "cron",
            hour=hour,
            minute=(minute + 10) % 60,
            args=[app],
        )
    if DEADLINE_REMINDERS_ENABLED:
        scheduler.add_job(
            deadline_reminder_job,
            "cron",
            hour=hour,
            minute=(minute + 20) % 60,
            args=[app],
        )
    if EMAIL_DIGEST_ENABLED:
        scheduler.add_job(
            email_digest_job,
            "cron",
            hour=EMAIL_DIGEST_HOUR,
            minute=0,
            args=[app],
        )
    scheduler.start()

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
