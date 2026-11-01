"""
notion_sync.py — разбор сообщений из группы «Юлечкины Сохраненки» в базы Notion.

Как работает:
1. Ловит ВСЕ сообщения из групп раньше остальных обработчиков бота
   и не пускает их дальше (чтобы бот не создавал из них задачи/напитки).
2. Определяет тему (вкладку) → выбирает базу Notion.
3. Если в сообщении есть ссылка — открывает страницу и берёт заголовок/описание.
4. Claude разбирает текст/ссылку/фото и заполняет поля базы.
5. Создаёт строку в Notion и ставит реакцию 👍 на сообщение.

Переменные Railway:
  NOTION_TOKEN       — секрет Notion-интеграции (обязательно)
  ANTHROPIC_API_KEY  — уже есть
  CHAT_ID            — уже есть (твой user id, по нему проверяем, что пишешь именно ты)
  TOPIC_NAMES        — необязательно: JSON {"<thread_id>": "Название темы"}, если
                       бот не смог узнать название темы сам
"""

import os
import re
import json
import base64
import logging
import html as html_lib

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    ApplicationHandlerStop,
    filters,
    ContextTypes,
)

logger = logging.getLogger(__name__)

NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
OWNER_ID = int(os.environ["CHAT_ID"])
NOTION_VERSION = "2022-06-28"

# ---------- Базы Notion ----------

DATABASES = {
    "work": {
        "id": "7f2475682da74d848de72e1a0900c80e",
        "title_prop": "Название",
        "has_topic": True,
        "extra": {
            "type": ("Тип", "select"),
        },
        "prompt": (
            "Это рабочая заметка (AI, ASO, маркетинг, нетворкинг, обучение).\n"
            '"title": короткое понятное название (что это за инструмент/статья/кейс).\n'
            '"summary": суть в 1–2 предложениях — чем полезно.\n'
            '"type": одно из ["Инструмент", "Кейс", "Статья", "Курс", "Вакансия", "Новость", "Другое"].'
        ),
    },
    "watch": {
        "id": "18cda4c46be74025a18422d35bb27a12",
        "title_prop": "Название",
        "has_topic": True,
        "extra": {
            "author": ("Автор", "rich_text"),
            "format": ("Формат", "select"),
        },
        "prompt": (
            "Это то, что хочется прочитать/посмотреть/послушать.\n"
            '"title": название книги/фильма/подкаста/статьи/видео (если на фото обложка — прочитай название с неё).\n'
            '"author": автор/режиссёр/канал, если понятно, иначе "".\n'
            '"summary": о чём это, 1–2 предложения.\n'
            '"format": одно из ["Книга", "Фильм", "Сериал", "Подкаст", "Лонгрид", "Видео"].'
        ),
    },
    "wish": {
        "id": "891a914432dc4897a370db92726fa607",
        "title_prop": "Товар",
        "has_topic": True,
        "extra": {
            "shop": ("Магазин", "rich_text"),
            "price": ("Цена", "rich_text"),
            "for_whom": ("Для кого", "select"),
        },
        "prompt": (
            "Это товар из вишлиста.\n"
            '"title": название товара коротко (бренд + что это).\n'
            '"summary": чем примечателен, 1 предложение.\n'
            '"shop": магазин/сайт, если понятно, иначе "".\n'
            '"price": цена с валютой, только если она явно есть в данных, иначе "". Не выдумывай.\n'
            '"for_whom": "В подарок", если тема Gifts или явно подарок, иначе "Себе".'
        ),
    },
    "recipes": {
        "id": "cf9069c2d32a4232bf125f89f4671953",
        "title_prop": "Блюдо",
        "has_topic": False,
        "extra": {
            "category": ("Категория", "select"),
            "ingredients": ("Ингредиенты", "rich_text"),
        },
        "prompt": (
            "Это рецепт.\n"
            '"title": название блюда.\n'
            '"summary": коротко, что за блюдо, 1 предложение.\n'
            '"category": одно из ["Суп", "Второе", "Салат", "Завтрак", "Десерт", "Другое"].\n'
            '"ingredients": основные ингредиенты через запятую, если видны, иначе "".'
        ),
    },
    "places": {
        "id": "2828b592238f4402b3e2f67485feb821",
        "title_prop": "Название",
        "has_topic": True,
        "extra": {
            "type": ("Тип", "select"),
        },
        "prompt": (
            "Это заметка про страну/город/место, игру, мотивацию или что-то разное.\n"
            '"title": короткое понятное название.\n'
            '"summary": суть в 1–2 предложениях.\n'
            '"type": одно из ["Место", "Услуга", "Событие", "Полезное", "Вдохновение", "Другое"].'
        ),
    },
}

# Тема в Telegram → (база, подпись для поля «Тема TG»). Сопоставление по началу названия.
TOPIC_RULES = [
    ("ai", "work", "AI"),
    ("aso", "work", "ASO and Mobile"),
    ("marketing", "work", "Marketing"),
    ("linkedin", "work", "Linkedin/Networking"),
    ("edu", "work", "EDU"),
    ("books", "watch", "Books"),
    ("films", "watch", "Films"),
    ("podcasts", "watch", "Podcasts"),
    ("long reads", "watch", "Long reads"),
    ("reels", "watch", "Reels and Videos"),
    ("gifts", "wish", "Gifts"),
    ("items", "wish", "Items"),
    ("gadgets", "wish", "Gadgets"),
    ("beauty", "wish", "Beauty"),
    ("recepts", "recipes", None),
    ("recipes", "recipes", None),
    ("serbia", "places", "Serbia"),
    ("israel", "places", "Israel"),
    ("france", "places", "France"),
    ("st.p", "places", "St.P (and RU)"),
    ("games", "places", "Games"),
    ("motivation", "places", "Motivation"),
    ("other", "places", "Other"),
]

# Эти темы не разбираем вообще
SKIP_TOPICS = ("memes", "photo album", "documents", "адреса", "general")

# thread_id → название темы (заполняется само по служебным сообщениям Telegram)
_topic_names: dict[int, str] = {}
try:
    _topic_names.update({int(k): v for k, v in json.loads(os.environ.get("TOPIC_NAMES", "{}")).items()})
except Exception:
    logger.warning("TOPIC_NAMES не похож на JSON — игнорирую")

_seen_media_groups: set[str] = set()


def resolve_topic(topic_name: str | None):
    """Возвращает (db_key, label) или None, если тему пропускаем/не знаем."""
    if not topic_name:
        return None
    name = topic_name.strip().lower()
    if name.startswith(SKIP_TOPICS):
        return None
    for prefix, db_key, label in TOPIC_RULES:
        if name.startswith(prefix):
            return db_key, label
    return None


# ---------- Ссылки ----------

URL_RE = re.compile(r"https?://\S+")


def _meta(page: str, key: str) -> str:
    m = re.search(
        rf'<meta[^>]+(?:property|name)=["\']{re.escape(key)}["\'][^>]*content=["\']([^"\']*)', page, re.I
    ) or re.search(
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']{re.escape(key)}["\']', page, re.I
    )
    return html_lib.unescape(m.group(1)).strip() if m else ""


async def fetch_link_info(url: str) -> str:
    """Заголовок, описание и начало текста страницы. Пустая строка, если сайт не отдал."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=12) as client:
            r = await client.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                    "Accept-Language": "ru,en;q=0.8",
                },
            )
        if "text/html" not in r.headers.get("content-type", ""):
            return ""
        page = r.text[:400_000]
        title = _meta(page, "og:title")
        if not title:
            m = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
            title = html_lib.unescape(m.group(1)).strip() if m else ""
        desc = _meta(page, "og:description") or _meta(page, "description")
        site = _meta(page, "og:site_name")
        body = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"\s+", " ", html_lib.unescape(body)).strip()[:2500]
        parts = [f"Сайт: {site}" if site else "", f"Заголовок: {title}", f"Описание: {desc}", f"Текст: {body}"]
        return "\n".join(p for p in parts if p and not p.endswith(": "))
    except Exception as e:
        logger.info(f"Не смогла открыть ссылку {url}: {e}")
        return ""


# ---------- Claude ----------

async def ask_claude(db_key: str, topic_label: str | None, text: str, link_info: str,
                     source: str, image_b64: str | None) -> dict | None:
    db = DATABASES[db_key]
    keys = ["title", "summary"] + list(db["extra"].keys())
    instructions = (
        "Ты разбираешь заметку, которую Юля сохранила в Telegram, и раскладываешь её в базу Notion.\n"
        f"Тема в Telegram: {topic_label or 'Recepts'}.\n"
        f"{db['prompt']}\n\n"
        "Пиши по-русски (названия книг/фильмов/товаров оставляй как в оригинале). "
        "Не выдумывай факты, которых нет в данных. Если данных мало — опиши то, что есть.\n"
        f"Верни ТОЛЬКО JSON без markdown с ключами: {', '.join(keys)}."
    )
    data_parts = []
    if source:
        data_parts.append(f"Переслано из: {source}")
    if text:
        data_parts.append(f"Текст сообщения:\n{text[:4000]}")
    if link_info:
        data_parts.append(f"Данные со страницы по ссылке:\n{link_info}")
    if not data_parts and not image_b64:
        return None
    content = []
    if image_b64:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}})
    content.append({"type": "text", "text": instructions + "\n\n" + ("\n\n".join(data_parts) or "(только картинка)")})

    try:
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={"model": ANTHROPIC_MODEL, "max_tokens": 600,
                      "messages": [{"role": "user", "content": content}]},
            )
        r.raise_for_status()
        raw = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip()
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
        return json.loads(raw)
    except Exception:
        logger.exception("Ошибка разбора заметки через Claude API")
        return None


# ---------- Notion ----------

def _rt(value: str) -> list:
    value = (value or "")[:1900]
    return [{"type": "text", "text": {"content": value}}] if value else []


def _notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


async def already_saved(db_key: str, msg_key: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"https://api.notion.com/v1/databases/{DATABASES[db_key]['id']}/query",
                headers=_notion_headers(),
                json={"filter": {"property": "TG msg", "rich_text": {"equals": msg_key}}, "page_size": 1},
            )
        r.raise_for_status()
        return bool(r.json().get("results"))
    except Exception:
        logger.exception("Не смогла проверить дубли в Notion")
        return False


async def create_notion_page(db_key: str, topic_label: str | None, parsed: dict, url: str | None,
                             date_iso: str, msg_key: str, original_text: str, source: str) -> str:
    db = DATABASES[db_key]
    title = (parsed.get("title") or "").strip() or (original_text[:80] if original_text else "Без названия")
    props = {
        db["title_prop"]: {"title": _rt(title)},
        "Суть": {"rich_text": _rt(parsed.get("summary", ""))},
        "Дата": {"date": {"start": date_iso}},
        "TG msg": {"rich_text": _rt(msg_key)},
    }
    if url:
        props["Ссылка"] = {"url": url[:2000]}
    if db["has_topic"] and topic_label:
        props["Тема TG"] = {"select": {"name": topic_label}}
    for key, (prop, kind) in db["extra"].items():
        val = (parsed.get(key) or "").strip() if isinstance(parsed.get(key), str) else ""
        if not val:
            continue
        props[prop] = {"select": {"name": val[:100]}} if kind == "select" else {"rich_text": _rt(val)}

    children = []
    if source:
        children.append({"object": "block", "type": "paragraph",
                         "paragraph": {"rich_text": _rt(f"Источник: {source}")}})
    if original_text:
        for i in range(0, min(len(original_text), 6000), 1900):
            children.append({"object": "block", "type": "quote",
                             "quote": {"rich_text": _rt(original_text[i:i + 1900])}})

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.notion.com/v1/pages",
            headers=_notion_headers(),
            json={"parent": {"database_id": db["id"]}, "properties": props, "children": children},
        )
    if r.status_code >= 400:
        raise RuntimeError(f"Notion {r.status_code}: {r.text[:300]}")
    return r.json().get("url", "")


async def process_item(topic_name: str, text: str, image_b64: str | None, date_iso: str,
                       msg_key: str, source: str = "") -> str:
    """
    Общая точка входа — используется и ботом, и разовым импортом старой истории.
    Возвращает: "saved" | "skipped" | "duplicate" | "failed".
    """
    resolved = resolve_topic(topic_name)
    if not resolved:
        return "skipped"
    db_key, label = resolved
    if await already_saved(db_key, msg_key):
        return "duplicate"
    urls = URL_RE.findall(text or "")
    url = urls[0].rstrip(").,") if urls else None
    link_info = await fetch_link_info(url) if url else ""
    parsed = await ask_claude(db_key, label, text, link_info, source, image_b64)
    if parsed is None:
        if not (text or url):
            return "failed"
        parsed = {"title": "", "summary": ""}  # всё равно сохраняем ссылку/текст, чтобы не потерять
    await create_notion_page(db_key, label, parsed, url, date_iso, msg_key, text or "", source)
    return "saved"


# ---------- Telegram ----------

def _message_text(msg) -> str:
    """Текст/подпись + ссылки, спрятанные под словами (text_link)."""
    text = msg.text or msg.caption or ""
    entities = msg.entities or msg.caption_entities or []
    hidden = [e.url for e in entities if e.type == "text_link" and e.url]
    if hidden:
        text = (text + "\n" + "\n".join(hidden)).strip()
    return text


def _forward_source(msg) -> str:
    origin = getattr(msg, "forward_origin", None)
    if not origin:
        return ""
    chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
    if chat is not None:
        return chat.title or (f"@{chat.username}" if chat.username else "")
    user = getattr(origin, "sender_user", None)
    if user is not None:
        return user.full_name
    return getattr(origin, "sender_user_name", "") or ""


def _remember_topic(msg):
    """Запоминаем названия тем из служебных сообщений Telegram."""
    if msg.forum_topic_created and msg.message_thread_id:
        _topic_names[msg.message_thread_id] = msg.forum_topic_created.name
    if msg.forum_topic_edited and msg.forum_topic_edited.name and msg.message_thread_id:
        _topic_names[msg.message_thread_id] = msg.forum_topic_edited.name
    reply = msg.reply_to_message
    if reply and reply.forum_topic_created and msg.message_thread_id:
        _topic_names.setdefault(msg.message_thread_id, reply.forum_topic_created.name)


async def _react(context, msg, emoji: str):
    try:
        await context.bot.set_message_reaction(chat_id=msg.chat_id, message_id=msg.message_id, reaction=emoji)
    except Exception:
        logger.info("Не смогла поставить реакцию")


async def group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    try:
        if msg is None or msg.from_user is None or msg.from_user.id != OWNER_ID:
            return
        _remember_topic(msg)

        text = _message_text(msg)

        # /topicid — подсказать номер и название темы (для настройки)
        if text.strip().startswith("/topicid"):
            tid = msg.message_thread_id if msg.is_topic_message else None
            name = _topic_names.get(tid, "не знаю название") if tid else "General"
            target = resolve_topic(name)
            where = target[0] if target else "не разбираю"
            await msg.reply_text(f"Тема: {name}\nthread_id: {tid}\nКуда пишу: {where}")
            return

        if msg.forum_topic_created or msg.forum_topic_edited or not msg.is_topic_message:
            return
        if not NOTION_TOKEN or not ANTHROPIC_API_KEY:
            logger.warning("Нет NOTION_TOKEN или ANTHROPIC_API_KEY — заметки не разбираю")
            return

        # из альбома берём только первое фото
        if msg.media_group_id:
            if msg.media_group_id in _seen_media_groups:
                return
            _seen_media_groups.add(msg.media_group_id)

        topic_name = _topic_names.get(msg.message_thread_id)
        if not topic_name:
            await msg.reply_text(
                "Не знаю, как называется эта тема. Добавь её в переменную TOPIC_NAMES на Railway "
                f'(thread_id: {msg.message_thread_id}) или переименуй тему — тогда я её запомню.'
            )
            return
        if not resolve_topic(topic_name):
            return  # мемы, фото, документы, адреса — молча пропускаем

        image_b64 = None
        if msg.photo:
            file = await msg.photo[-1].get_file()
            image_b64 = base64.b64encode(bytes(await file.download_as_bytearray())).decode()

        result = await process_item(
            topic_name=topic_name,
            text=text,
            image_b64=image_b64,
            date_iso=msg.date.date().isoformat(),
            msg_key=f"{msg.chat_id}:{msg.message_id}",
            source=_forward_source(msg),
        )
        if result in ("saved", "duplicate"):
            await _react(context, msg, "👍")
        elif result == "failed":
            await _react(context, msg, "🤔")
    except Exception as e:
        logger.exception("Ошибка сохранения заметки в Notion")
        try:
            await msg.reply_text(f"Не смогла сохранить в Notion: {e}")
        except Exception:
            pass
    finally:
        # Сообщения из групп никогда не идут в остальные обработчики бота
        raise ApplicationHandlerStop


def register(app: Application):
    """Подключение: вызвать в main() bot.py ДО остальных add_handler."""
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, group_message_handler), group=-1)
