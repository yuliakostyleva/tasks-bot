# Tasks bot

Телеграм-бот: команда `/tasks` — задачи из Todoist прямо сейчас,
плюс ежедневная сводка в заданное время.

## Локальный тест

```bash
pip install -r requirements.txt
cp .env.example .env
# заполни .env своими значениями
export $(cat .env | xargs)   # или используй python-dotenv, если удобнее
python bot.py
```

Где взять значения для `.env`:

- `TELEGRAM_TOKEN` — от @BotFather, команда /newbot
- `TODOIST_TOKEN` — Todoist → Settings → Integrations → Developer → API token
- `CHAT_ID` — узнать через @userinfobot (напиши ему, он пришлёт твой id)
- `SEND_TIME` — во сколько присылать сводку, формат HH:MM
- `TIMEZONE` — часовой пояс, например Europe/Belgrade или Europe/Moscow

## Деплой на Railway (бесплатно для такого объёма)

1. Зарегистрируйся на railway.app через GitHub
2. Залей эту папку в отдельный репозиторий на GitHub
3. В Railway: New Project → Deploy from GitHub repo → выбери репозиторий
4. В настройках проекта (Variables) добавь те же переменные, что в `.env.example`,
   со своими реальными значениями
5. Railway сам увидит `requirements.txt` и запустит `python bot.py`
6. Проверь логи (Deployments → View Logs) — там должно быть "Бот запущен"

После этого пиши боту /start в Telegram — должен ответить.

## Что дальше можно добавить

- Фильтр по конкретному проекту Todoist вместо всех задач
- Кнопки "выполнено" прямо в сообщении бота
- Второй источник (Google Calendar) — отдельным модулем, без переписывания текущего кода
