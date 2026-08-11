import os
import logging
from datetime import datetime

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from todoist_api_python.api import TodoistAPI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TODOIST_TOKEN = os.environ["TODOIST_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
# Формат "HH:MM", например "08:00". Часовой пояс задаётся отдельно (см. TIMEZONE ниже)
SEND_TIME = os.environ.get("SEND_TIME", "08:00")
TIMEZONE = os.environ.get("TIMEZONE", "Europe/Belgrade")

todoist = TodoistAPI(TODOIST_TOKEN)


def format_tasks(tasks) -> str:
    if not tasks:
        return "Задач нет. Можно выдохнуть."

    lines = ["Задачи на сегодня:\n"]
    for t in tasks:
        due = ""
        if t.due:
            due = f" (до {t.due.date})"
        lines.append(f"• {t.content}{due}")
    return "\n".join(lines)


def get_active_tasks():
    # Берём все активные задачи. Если нужно только "на сегодня" —
    # можно заменить на filter="today | overdue"
    tasks = todoist.get_tasks(filter="today | overdue")
    return tasks


async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tasks = get_active_tasks()
        text = format_tasks(tasks)
    except Exception as e:
        logger.exception("Ошибка при получении задач")
        text = f"Не смогла получить задачи: {e}"
    await update.message.reply_text(text)


async def send_daily_summary(app: Application):
    try:
        tasks = get_active_tasks()
        text = format_tasks(tasks)
    except Exception as e:
        logger.exception("Ошибка при получении задач для рассылки")
        text = f"Не смогла получить задачи: {e}"
    await app.bot.send_message(chat_id=CHAT_ID, text=text)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет. Команда /tasks покажет текущие задачи из Todoist.\n"
        f"Ежедневная сводка приходит в {SEND_TIME} ({TIMEZONE})."
    )


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("tasks", tasks_command))

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
