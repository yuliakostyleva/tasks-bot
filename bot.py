import os
import logging

import httpx
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TODOIST_TOKEN = os.environ["TODOIST_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
# Формат "HH:MM", например "08:00". Часовой пояс задаётся отдельно (см. TIMEZONE ниже)
SEND_TIME = os.environ.get("SEND_TIME", "08:00")
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

# WHOOP — опционально. Recovery, сон, активность.
WHOOP_CLIENT_ID = os.environ.get("WHOOP_CLIENT_ID")
WHOOP_CLIENT_SECRET = os.environ.get("WHOOP_CLIENT_SECRET")
WHOOP_REFRESH_TOKEN = os.environ.get("WHOOP_REFRESH_TOKEN")
WHOOP_ENABLED = bool(WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET and WHOOP_REFRESH_TOKEN)
logger.info(
    f"WHOOP debug: CLIENT_ID={'есть' if WHOOP_CLIENT_ID else 'ПУСТО'}, "
    f"CLIENT_SECRET={'есть' if WHOOP_CLIENT_SECRET else 'ПУСТО'}, "
    f"REFRESH_TOKEN={'есть' if WHOOP_REFRESH_TOKEN else 'ПУСТО'}, "
    f"ENABLED={WHOOP_ENABLED}"
)


def get_whoop_access_token() -> str:
    response = httpx.post(
        "https://api.prod.whoop.com/oauth/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": WHOOP_REFRESH_TOKEN,
            "client_id": WHOOP_CLIENT_ID,
            "client_secret": WHOOP_CLIENT_SECRET,
            "scope": "offline",
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def get_whoop_summary() -> str:
    if not WHOOP_ENABLED:
        return ""

    access_token = get_whoop_access_token()
    headers = {"Authorization": f"Bearer {access_token}"}

    lines = ["💪 WHOOP:\n"]

    # Recovery — самая свежая запись
    try:
        r = httpx.get(
            "https://api.prod.whoop.com/developer/v2/recovery",
            headers=headers,
            params={"limit": 1},
            timeout=15,
        )
        r.raise_for_status()
        records = r.json().get("records", [])
        if records and records[0].get("score_state") == "SCORED":
            score = records[0]["score"]
            lines.append(f"Recovery: {score['recovery_score']}%")
            lines.append(f"Пульс покоя: {score['resting_heart_rate']} уд/мин")
            lines.append(f"HRV: {round(score['hrv_rmssd_milli'])} мс")
        else:
            lines.append("Recovery: пока не подсчитан.")
    except Exception:
        logger.exception("Ошибка при получении WHOOP recovery")
        lines.append("Recovery: не удалось получить.")

    # Сон — самая свежая запись
    try:
        s = httpx.get(
            "https://api.prod.whoop.com/developer/v2/activity/sleep",
            headers=headers,
            params={"limit": 1},
            timeout=15,
        )
        s.raise_for_status()
        records = s.json().get("records", [])
        if records and records[0].get("score_state") == "SCORED":
            stage = records[0]["score"]["stage_summary"]
            total_ms = (
                stage["total_light_sleep_time_milli"]
                + stage["total_slow_wave_sleep_time_milli"]
                + stage["total_rem_sleep_time_milli"]
            )
            hours = total_ms // 3600000
            minutes = (total_ms % 3600000) // 60000
            performance = records[0]["score"].get("sleep_performance_percentage")
            lines.append(f"Сон: {hours}ч {minutes}м (производительность {performance}%)")
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
            strain = yesterday_cycle["score"]["strain"]
            lines.append(f"Вчерашний strain: {round(strain, 1)}")
    except Exception:
        logger.exception("Ошибка при получении WHOOP цикла")

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


def list_google_calendars(refresh_token: str):
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
        title = e.get("summary", "Без названия")
        start = e.get("start", {})
        time_str = ""
        if "dateTime" in start:
            # Формат: 2026-08-15T14:00:00+03:00 — берём только часы:минуты
            time_str = start["dateTime"][11:16] + " — "
        return f"• {time_str}{title}"

    lines = []
    for label, events, is_work in calendars:
        if events:
            lines.append(label)
            for e in events:
                lines.append(format_event(e))
            lines.append("")
        elif is_work and is_weekday:
            # Рабочие календари по будням показываем всегда, даже пустые
            lines.append(label)
            lines.append("Нет встреч.")
            lines.append("")
        # Личные пустые календари (и рабочие в выходные) просто пропускаем

    if not lines:
        return "🗓️ Встречи сегодня:\n\nВстреч нет."

    return "🗓️ Встречи сегодня:\n\n" + "\n".join(lines).strip()


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
    lines = ["🔥 Срочно на сегодня:\n"]
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
        lines.append(f"• {t['content']}{format_due(t.get('due'))}")
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
    except Exception as e:
        logger.exception("Ошибка при получении задач на завтра")
        text = f"Не смогла получить задачи: {e}"
    await update.message.reply_text(text)


async def week_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tasks = get_week_tasks()
        text = format_tasks(tasks, title="📆 На неделю:")
    except Exception as e:
        logger.exception("Ошибка при получении задач на неделю")
        text = f"Не смогла получить задачи: {e}"
    await update.message.reply_text(text)


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
    lines = ["📅 Завтра:\n"]
    if not tasks:
        lines.append("Ничего не запланировано.")
        return "\n".join(lines)

    for t in tasks:
        lines.append(f"• {t['content']}")
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

    lines = [f"{title}\n"]
    for i, (pid, group) in enumerate(grouped.items()):
        project_name = projects.get(pid, "Без проекта")
        emoji = get_project_emoji(project_name, i)
        lines.append(f"{emoji} {project_name}")
        for t in group:
            lines.append(f"• {t['content']}{format_due(t.get('due'))}")
        lines.append("")

    return "\n".join(lines).strip()


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
    await update.message.reply_text(text)


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

    lines.append("📅 Сегодня:")
    if today_tasks:
        for t in today_tasks:
            lines.append(f"• {t['content']}{format_due(t.get('due'))}")
    else:
        lines.append("Ничего на сегодня.")

    lines.append("")
    lines.append("⏰ Просрочено:")
    if overdue_tasks:
        for t in overdue_tasks:
            lines.append(f"• {t['content']}{format_due(t.get('due'))}")
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
    await update.message.reply_text(text)


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
    await update.message.reply_text(text)


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        today_tasks = get_today_only_tasks()
        overdue_tasks = get_overdue_tasks()
        text = format_two_sections(today_tasks, overdue_tasks)
        if CALENDAR_ENABLED:
            events = get_today_calendar_events()
            text += "\n\n" + format_calendar_section(events)
        if WHOOP_ENABLED:
            text += "\n\n" + get_whoop_summary()
    except Exception as e:
        logger.exception("Ошибка при получении задач на сегодня")
        text = f"Не смогла получить задачи: {e}"
    await update.message.reply_text(text)


async def backlog_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tasks = get_backlog_tasks()
        text = format_tasks(tasks, title="Задачи без даты (беклог):")
    except Exception as e:
        logger.exception("Ошибка при получении беклога")
        text = f"Не смогла получить задачи: {e}"
    await update.message.reply_text(text)


async def send_daily_summary(app: Application):
    try:
        today_tasks = get_today_only_tasks()
        overdue_tasks = get_overdue_tasks()
        text = format_two_sections(today_tasks, overdue_tasks)
        if CALENDAR_ENABLED:
            events = get_today_calendar_events()
            text += "\n\n" + format_calendar_section(events)
        if WHOOP_ENABLED:
            text += "\n\n" + get_whoop_summary()
    except Exception as e:
        logger.exception("Ошибка при получении задач для рассылки")
        text = f"Не смогла получить задачи: {e}"
    await app.bot.send_message(chat_id=CHAT_ID, text=text)


MAIN_KEYBOARD_ROWS = [
    ["📅 Сегодня", "🗂️ Беклог"],
    ["📋 Все задачи", "➡️ Завтра"],
    ["📆 Неделя", "🗓️ Календарь"],
    ["💪 WHOOP"],
    ["❤️ Для Саши"],
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
        "/tasks — срочное + завтра + всё остальное по проектам\n\n"
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
        note = "организм просит отдыха — поддержи её и не грузи сегодня 🧡"

    if sleep_performance is not None and sleep_performance < 70:
        note += "\nСпала не очень хорошо, может быть более чувствительной."

    return note


async def for_sasha_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        today_tasks = get_today_only_tasks()
    except Exception as e:
        logger.exception("Ошибка при получении задач для Саши")
        await update.message.reply_text(f"Не смогла получить задачи: {e}")
        return

    lines = ["❤️ Сегодня у Юлечки такие вот дела:\n"]
    if today_tasks:
        for t in today_tasks:
            lines.append(f"• {t['content']}")
    else:
        lines.append("Сегодня ничего не запланировано, можно просто обняться.")

    if WHOOP_ENABLED:
        try:
            access_token = get_whoop_access_token()
            headers = {"Authorization": f"Bearer {access_token}"}

            recovery_score = None
            sleep_performance = None
            sleep_hours = None
            sleep_minutes = None

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
                params={"limit": 1},
                timeout=15,
            )
            s.raise_for_status()
            sleep_records = s.json().get("records", [])
            if sleep_records and sleep_records[0].get("score_state") == "SCORED":
                stage = sleep_records[0]["score"]["stage_summary"]
                total_ms = (
                    stage["total_light_sleep_time_milli"]
                    + stage["total_slow_wave_sleep_time_milli"]
                    + stage["total_rem_sleep_time_milli"]
                )
                sleep_hours = total_ms // 3600000
                sleep_minutes = (total_ms % 3600000) // 60000
                sleep_performance = sleep_records[0]["score"].get("sleep_performance_percentage")

            if sleep_hours is not None:
                lines.append(f"\n😴 Спала {sleep_hours}ч {sleep_minutes}м")

            mood_hint = get_whoop_mood_hint(recovery_score, sleep_performance)
            if mood_hint:
                lines.append(f"\n{mood_hint}")
        except Exception:
            logger.exception("Ошибка при получении WHOOP данных для Саши")

    text = "\n".join(lines)

    # Присылаем ей самой — дальше она пересылает это сообщение Саше вручную
    await update.message.reply_text(text)


async def menu_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
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
    elif text == "❤️ Для Саши":
        await for_sasha_handler(update, context)
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_button_handler))

    hour, minute = map(int, SEND_TIME.split(":"))
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(
        send_daily_summary,
        "cron",
        hour=hour,
        minute=minute,
        args=[app],
    )
    scheduler.start()

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
