"""
Бот-консультант для сообщества ВКонтакте (рассада, цветы, овощи).

Что умеет:
- Отвечает в личных сообщениях группы с помощью ИИ (Claude): уход, посадка,
  болезни, вредители, а также товары, цены, наличие, доставка (по каталогу и базе знаний).
- Понимает фото: можно прислать фото растения и спросить, что с ним.
- Загружает каталог из Маркета и из альбомов с заказами, обновляет его автоматически.
- Записывает заказы из комментариев под фото в альбомах ("беру 2", "+", "3 шт")
  и отмены ("отмена", удалённый комментарий), уведомляет админов.
- Ловит новые заказы из Маркета и их смену статуса.
- Кнопки: "Мои заказы", "Позвать менеджера".
- Если менеджер сам ответил человеку из группы, бот замолкает в этом диалоге на время.
"""

import os
import re
import sys
import time
import base64
import random
import logging
import sqlite3
import threading
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
import vk_api
from vk_api.bot_longpoll import VkBotLongPoll
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
import anthropic

# ---------------------------------------------------------------- настройки

load_dotenv()


def id_list(value):
    """'123, -456,club789' -> [123, 456, 789]: только цифры, минусы и лишние символы отбрасываем."""
    ids = []
    for part in (value or "").split(","):
        digits = re.sub(r"\D", "", part)
        if digits:
            ids.append(int(digits))
    return ids


GROUP_TOKEN = os.getenv("VK_GROUP_TOKEN", "").strip()
# Часто вписывают "-123456" или "club123456" — берём только цифры
GROUP_ID = (id_list(os.getenv("VK_GROUP_ID", "")) or [0])[0]
READ_TOKEN = os.getenv("VK_READ_TOKEN", "").strip()
SHOP_NAME = os.getenv("SHOP_NAME", "наш магазин")
MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
ADMIN_IDS = id_list(os.getenv("ADMIN_IDS", ""))
ORDER_ALBUM_IDS = id_list(os.getenv("ORDER_ALBUM_IDS", ""))
PAUSE_HOURS = float(os.getenv("PAUSE_HOURS", "12"))
REPLY_IN_COMMENTS = os.getenv("REPLY_IN_COMMENTS", "1") == "1"
CATALOG_REFRESH_MIN = int(os.getenv("CATALOG_REFRESH_MIN", "30"))
HISTORY_LEN = int(os.getenv("HISTORY_LEN", "12"))
KNOWLEDGE_FILE = os.getenv("KNOWLEDGE_FILE", "knowledge.md")
DB_FILE = os.getenv("DB_FILE", "bot.db")
API_VERSION = "5.199"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("bot")

# Клиенты создаются в init(), чтобы модуль можно было импортировать в тестах без ключей
group_session = None
vk = None
read_vk = None
claude = None
db = None

MARKET_STATUS = {
    0: "новый", 1: "согласуется", 2: "собирается", 3: "доставляется",
    4: "выполнен", 5: "отменён", 6: "возвращён",
}

# ---------------------------------------------------------------- база данных


class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS history(
                    user_id INTEGER, role TEXT, content TEXT, ts INTEGER);
                CREATE TABLE IF NOT EXISTS orders(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT, user_id INTEGER, item TEXT, qty INTEGER,
                    comment TEXT, ext_id TEXT UNIQUE, status TEXT DEFAULT 'новый',
                    ts INTEGER);
                CREATE TABLE IF NOT EXISTS paused(
                    user_id INTEGER PRIMARY KEY, until INTEGER);
            """)
            self.conn.commit()

    def run(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.fetchall()

    def update(self, sql, args=()):
        """UPDATE/DELETE: возвращает число изменённых строк."""
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.rowcount


def add_history(user_id, role, content):
    db.run("INSERT INTO history VALUES (?,?,?,?)", (user_id, role, content, int(time.time())))


def get_history(user_id):
    rows = db.run(
        "SELECT role, content FROM history WHERE user_id=? ORDER BY rowid DESC LIMIT ?",
        (user_id, HISTORY_LEN),
    )
    msgs = []
    for role, content in reversed(rows):
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + content
        else:
            msgs.append({"role": role, "content": content})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


def pause_user(user_id, hours=PAUSE_HOURS):
    db.run("INSERT OR REPLACE INTO paused VALUES (?,?)", (user_id, int(time.time() + hours * 3600)))


def resume_user(user_id):
    db.run("DELETE FROM paused WHERE user_id=?", (user_id,))


def is_paused(user_id):
    rows = db.run("SELECT until FROM paused WHERE user_id=?", (user_id,))
    return bool(rows) and rows[0][0] > time.time()


def user_orders(user_id, limit=20):
    return db.run(
        "SELECT source, item, qty, status, ts FROM orders WHERE user_id=? "
        "ORDER BY ts DESC LIMIT ?", (user_id, limit))


def format_orders(rows):
    if not rows:
        return "Заказов пока не нашёл. Если заказывали раньше, чем подключился бот, менеджер подскажет."
    lines = []
    for source, item, qty, status, ts in rows:
        date = time.strftime("%d.%m", time.localtime(ts))
        where = "Маркет" if source == "market" else "альбом"
        q = f" × {qty}" if qty else ""
        lines.append(f"{date} — {item}{q} ({where}, {status})")
    return "\n".join(lines)

# ---------------------------------------------------------------- каталог


PRICE_RE = re.compile(r"(\d[\d ]{0,6})\s*(₽|руб\w*|р\.|р\b)", re.IGNORECASE)


def parse_price(text):
    """Цена из подписи к фото: 'Петуния 150 р.' -> '150 ₽'."""
    m = PRICE_RE.search(text or "")
    if not m:
        return ""
    return m.group(1).replace(" ", "") + " ₽"


class Catalog:
    MIN_REFRESH_SEC = 60   # не чаще, когда обновляемся из-за неизвестного фото

    def __init__(self):
        self.items = []      # все позиции для поиска и для ИИ
        self.photos = {}     # photo_id -> позиция из альбома
        self.lock = threading.Lock()
        self.refresh_lock = threading.Lock()
        self.last_refresh = 0.0

    def _api(self):
        return read_vk or vk

    def refresh_if_stale(self):
        """Обновить каталог, если давно не обновляли (для комментариев под новыми фото)."""
        if time.time() - self.last_refresh < self.MIN_REFRESH_SEC:
            return
        self.refresh()

    def refresh(self):
        with self.refresh_lock:
            self.last_refresh = time.time()
            self._refresh()

    def _refresh(self):
        items, photos = [], {}
        api = self._api()

        # Маркет
        try:
            offset = 0
            while True:
                r = api.market.get(owner_id=-GROUP_ID, count=200, offset=offset, extended=1)
                for it in r.get("items", []):
                    if it.get("availability", 0) == 1:  # удалён
                        continue
                    items.append({
                        "kind": "market",
                        "id": it["id"],
                        "title": it.get("title", ""),
                        "price": (it.get("price") or {}).get("text", ""),
                        "desc": (it.get("description") or "")[:600],
                        "available": it.get("availability", 0) == 0,
                    })
                offset += 200
                if offset >= r.get("count", 0):
                    break
        except Exception as e:
            log.warning("Не удалось загрузить Маркет: %s", e)

        # Альбомы
        album_ids = ORDER_ALBUM_IDS
        album_titles = {}
        try:
            albums = api.photos.getAlbums(owner_id=-GROUP_ID).get("items", [])
            for a in albums:
                album_titles[a["id"]] = a.get("title", "")
            if not album_ids:
                album_ids = [a["id"] for a in albums]
        except Exception as e:
            log.warning("Не удалось получить список альбомов (нужен VK_READ_TOKEN?): %s", e)
        for aid in album_ids:
            # Ошибка в одном альбоме не должна ронять остальные
            try:
                offset = 0
                while True:
                    r = api.photos.get(owner_id=-GROUP_ID, album_id=aid, count=1000, offset=offset)
                    for p in r.get("items", []):
                        text = (p.get("text") or "").strip()
                        title = text.split("\n")[0][:120] if text else f"Фото {p['id']}"
                        entry = {
                            "kind": "album",
                            "id": p["id"],
                            "album_id": aid,
                            "album": album_titles.get(aid, ""),
                            "title": title,
                            "price": parse_price(text),
                            "desc": text[:600],
                            "available": True,
                        }
                        items.append(entry)
                        photos[p["id"]] = entry
                    offset += 1000
                    if offset >= r.get("count", 0):
                        break
            except Exception as e:
                log.warning("Не удалось загрузить альбом %s: %s", aid, e)

        with self.lock:
            if items:
                self.items, self.photos = items, photos
        log.info("Каталог: %d позиций (из них в альбомах %d)", len(items), len(photos))

    def as_text(self, limit=40000):
        with self.lock:
            items = list(self.items)
        lines = []
        for it in items:
            where = "Маркет" if it["kind"] == "market" else f"альбом «{it.get('album', '')}»"
            avail = "" if it["available"] else " [нет в наличии]"
            price = f" — {it['price']}" if it["price"] else ""
            desc = it["desc"].replace("\n", " ")
            lines.append(f"- {it['title']}{price}{avail} ({where}). {desc}")
        text = "\n".join(lines)
        if len(text) > limit:
            text = text[:limit] + "\n…(каталог обрезан, для остального используй search_catalog)"
        return text or "(каталог пока пуст или не загрузился)"

    def search(self, query, limit=15):
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
        with self.lock:
            items = list(self.items)
        scored = []
        for it in items:
            hay = (it["title"] + " " + it["desc"]).lower()
            score = sum(1 for w in words if w[:5] in hay)
            if score:
                scored.append((score, it))
        scored.sort(key=lambda x: -x[0])
        return [it for _, it in scored[:limit]]


catalog = Catalog()


def catalog_loop():
    while True:
        try:
            catalog.refresh()
        except Exception:
            log.exception("Ошибка обновления каталога")
        time.sleep(CATALOG_REFRESH_MIN * 60)

# ---------------------------------------------------------------- ВК: отправка


sent_ids = set()

KEYBOARD = VkKeyboard(one_time=False)
KEYBOARD.add_button("Мои заказы", color=VkKeyboardColor.SECONDARY)
KEYBOARD.add_button("Позвать менеджера", color=VkKeyboardColor.SECONDARY)
KEYBOARD = KEYBOARD.get_keyboard()


def send(peer_id, text, keyboard=True):
    text = text.strip() or "…"
    for i in range(0, len(text), 4000):
        try:
            mid = vk.messages.send(
                peer_id=peer_id,
                message=text[i:i + 4000],
                random_id=random.getrandbits(31),
                keyboard=KEYBOARD if keyboard else None,
            )
            if isinstance(mid, int):
                sent_ids.add(mid)
        except vk_api.exceptions.ApiError as e:
            log.warning("Не удалось отправить сообщение %s: %s", peer_id, e)
            return False
    return True


def notify_admins(text):
    for admin in ADMIN_IDS:
        send(admin, text, keyboard=False)


def reply_comment(photo_id, comment_id, text):
    if not (REPLY_IN_COMMENTS and read_vk):
        return
    try:
        read_vk.photos.createComment(
            owner_id=-GROUP_ID, photo_id=photo_id, message=text,
            reply_to_comment=comment_id, from_group=1,
        )
    except Exception as e:
        log.warning("Не удалось ответить в комментарии: %s", e)


def dialog_link(user_id):
    return f"https://vk.com/gim{GROUP_ID}?sel={user_id}"

# ---------------------------------------------------------------- ИИ


def load_knowledge():
    try:
        with open(KNOWLEDGE_FILE, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "(база знаний не заполнена)"


def system_prompt():
    return f"""Ты — консультант магазина «{SHOP_NAME}» в сообществе ВКонтакте. Магазин продаёт рассаду, цветы, семена и всё для огорода. Ты отвечаешь покупателям в личных сообщениях группы.

Темы, на которые ты отвечаешь: цветы, рассада, овощи, посадка, полив, подкормки, болезни и вредители, сроки посева, а также товары, цены, наличие, заказы, доставка и оплата в этом магазине. На посторонние темы вежливо скажи, что помогаешь только с растениями и заказами.

Правила:
- Цены, наличие, сроки отправки, доставку, оплату и правила магазина бери только из базы знаний и каталога ниже. Если там этого нет — не придумывай, честно скажи, что уточнит менеджер.
- Советы по уходу за растениями давай из общих знаний садоводства, коротко и практично.
- Не обещай скидок, не подтверждай оплату, не меняй и не отменяй заказы сам — это делает менеджер.
- Вызывай call_manager, если человек хочет оформить что-то нестандартное, жалуется, хочет вернуть деньги, сообщает о проблеме с заказом, явно просит человека, или ты не можешь помочь.
- Если прислали фото растения — опиши, что видишь, назови возможные причины и что делать. Если по фото не уверен — так и скажи.
- Если спрашивают о своих заказах — используй get_my_orders.
- Если товара нет в каталоге ниже — попробуй search_catalog, прежде чем говорить, что его нет.
- Пиши коротко и по-человечески, как живой продавец. ВКонтакте не показывает разметку: не используй **, #, таблицы. Можно переносы строк, дефисы и немного эмодзи 🌱.

=== БАЗА ЗНАНИЙ МАГАЗИНА ===
{load_knowledge()}

=== КАТАЛОГ (Маркет и альбомы) ===
{catalog.as_text()}
"""


TOOLS = [
    {
        "name": "search_catalog",
        "description": "Поиск товаров в каталоге магазина по названию или описанию (сорт, культура, цвет).",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "что искать"}},
            "required": ["query"],
        },
    },
    {
        "name": "get_my_orders",
        "description": "Заказы этого покупателя, записанные ботом (из Маркета и комментариев в альбомах).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "call_manager",
        "description": "Позвать живого менеджера в этот диалог. После вызова бот перестаёт отвечать человеку.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "кратко, что нужно человеку"}},
            "required": ["reason"],
        },
    },
]


def call_manager(user_id, reason):
    pause_user(user_id)
    notify_admins(f"🙋 Нужен менеджер\nПричина: {reason}\nДиалог: {dialog_link(user_id)}")


def run_tool(name, args, user_id):
    if name == "search_catalog":
        found = catalog.search(args.get("query", ""))
        if not found:
            return "Ничего не найдено."
        return "\n".join(
            f"- {it['title']} {it['price']} {'' if it['available'] else '[нет в наличии]'}: {it['desc'][:300]}"
            for it in found)
    if name == "get_my_orders":
        return format_orders(user_orders(user_id))
    if name == "call_manager":
        call_manager(user_id, args.get("reason", ""))
        return "Менеджер уведомлён и ответит в этом диалоге. Скажи человеку об этом коротко."
    return "Неизвестный инструмент."


IMAGE_TYPES = {b"\xff\xd8\xff": "image/jpeg", b"\x89PNG": "image/png", b"GIF8": "image/gif"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def image_block(url):
    """Скачиваем фото сами и отдаём ИИ в base64: так не зависим от того, пустит ли ВК чужой сервер.
    Если скачать не вышло — отдаём ссылку."""
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = r.read(MAX_IMAGE_BYTES + 1)
        media = next((t for sig, t in IMAGE_TYPES.items() if data.startswith(sig)), None)
        if media is None and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            media = "image/webp"
        if media and len(data) <= MAX_IMAGE_BYTES:
            return {"type": "image", "source": {
                "type": "base64", "media_type": media, "data": base64.b64encode(data).decode()}}
    except Exception as e:
        log.warning("Не удалось скачать фото %s: %s", url, e)
    return {"type": "image", "source": {"type": "url", "url": url}}


def ask_claude(user_id, text, image_urls=(), use_history=True, max_tokens=1000):
    content = [image_block(u) for u in image_urls]
    content.append({"type": "text", "text": text or "(прислал фото без текста)"})

    messages = get_history(user_id) if use_history else []
    messages.append({"role": "user", "content": content})
    system = [{"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}}]
    extra = {"tools": TOOLS} if use_history else {}

    for _ in range(5):
        resp = claude.messages.create(
            model=MODEL, max_tokens=max_tokens, system=system, messages=messages, **extra,
        )
        if resp.stop_reason != "tool_use":
            answer = "".join(b.text for b in resp.content if b.type == "text").strip()
            return answer
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                results.append({
                    "type": "tool_result", "tool_use_id": b.id,
                    "content": run_tool(b.name, b.input, user_id),
                })
        messages.append({"role": "user", "content": results})
    log.warning("ИИ не закончил ответ за 5 шагов, пользователь %s", user_id)
    return ""

# ---------------------------------------------------------------- обработчики

user_locks = defaultdict(threading.Lock)
MANAGER_WORDS = {"позвать менеджера", "менеджер", "оператор", "человек"}
GREET_WORDS = {"начать", "start", "старт"}


def photo_urls(attachments):
    urls = []
    for a in attachments or []:
        if a.get("type") == "photo":
            sizes = a["photo"].get("sizes", [])
            if sizes:
                urls.append(max(sizes, key=lambda s: s.get("width", 0))["url"])
    return urls[:4]


def parse_user_id(arg):
    """'123', 'id123', 'vk.com/id123' -> 123; иначе None."""
    m = re.search(r"\d+", arg or "")
    return int(m.group()) if m else None


def admin_command(user_id, peer_id, text):
    parts = text.split()
    cmd = parts[0].lower()
    if cmd in ("/вкл", "/выкл"):
        uid = parse_user_id(parts[1]) if len(parts) > 1 else None
        if uid is None:
            send(peer_id, f"Укажите id страницы: {cmd} 123456 или {cmd} vk.com/id123456", keyboard=False)
        elif cmd == "/вкл":
            resume_user(uid)
            send(peer_id, f"Бот снова отвечает пользователю {uid}.", keyboard=False)
        else:
            pause_user(uid, hours=24 * 365)
            send(peer_id, f"Бот молчит в диалоге с {uid}.", keyboard=False)
    elif cmd == "/обновить":
        catalog.refresh()
        send(peer_id, f"Каталог обновлён: {len(catalog.items)} позиций.", keyboard=False)
    elif cmd == "/заказы":
        rows = db.run(
            "SELECT user_id, item, qty, status, source FROM orders "
            "WHERE status NOT IN ('отменён','выполнен') ORDER BY ts DESC LIMIT 40")
        if not rows:
            send(peer_id, "Активных заказов нет.", keyboard=False)
        else:
            send(peer_id, "\n".join(
                f"vk.com/id{u} — {i} × {q or '?'} ({s}, {src})" for u, i, q, s, src in rows), keyboard=False)
    else:
        send(peer_id, "Команды: /вкл id, /выкл id, /обновить, /заказы", keyboard=False)


def has_voice(attachments):
    return any(a.get("type") == "audio_message" for a in attachments or [])


def on_message(msg):
    user_id = msg["from_id"]
    peer_id = msg["peer_id"]
    text = (msg.get("text") or "").strip()
    if peer_id > 2_000_000_000 or user_id < 0:   # беседы и сообщества пропускаем
        return

    with user_locks[user_id]:
        if user_id in ADMIN_IDS and text.startswith("/"):
            admin_command(user_id, peer_id, text)
            return
        if is_paused(user_id):
            return

        low = text.lower()
        if low in GREET_WORDS:
            send(peer_id, f"Здравствуйте! Я помощник «{SHOP_NAME}» 🌱 Спросите про рассаду, цветы, уход "
                          f"или ваш заказ. Можно прислать фото растения.")
            return
        if low in MANAGER_WORDS:
            call_manager(user_id, "человек нажал «Позвать менеджера»")
            send(peer_id, "Позвал менеджера, он ответит здесь, как только освободится.")
            return
        if low == "мои заказы":
            send(peer_id, format_orders(user_orders(user_id)))
            return

        images = photo_urls(msg.get("attachments"))
        if not text and not images:
            if has_voice(msg.get("attachments")):
                send(peer_id, "Голосовые я пока не слушаю 🙈 Напишите, пожалуйста, текстом "
                              "или нажмите «Позвать менеджера».")
            return
        try:
            answer = ask_claude(user_id, text, images)
        except Exception:
            log.exception("Ошибка ИИ")
            answer = ""
        if not answer:
            answer = "Не смог сразу ответить, передал вопрос менеджеру. Он напишет здесь."
            call_manager(user_id, f"бот не смог ответить, вопрос: {text[:200] or '[фото]'}")

        add_history(user_id, "user", (text or "") + (" [фото]" if images else ""))
        add_history(user_id, "assistant", answer)
        send(peer_id, answer)


# --- разбор комментариев под фото
#
# Заказ: «+», «беру 2», «возьму пару», «бронь», «закажу», «2 шт», просто «3», «мне тоже».
# «Хочу»/«мне» считаем заказом только с количеством («хочу 2», «мне 3 шт»),
# иначе «хочу узнать цену» или «мне кажется…» превращались в заказы.
# Вопрос («сколько стоит 1 шт?», «бронь есть?») — не заказ, если в нём нет явного «+», «беру» или «возьму».

CANCEL_RE = re.compile(r"\bотмен|\bне\s+буду\b|\bпередумал|\bуже\s+не\s+надо\b")
STRONG_ORDER_RE = re.compile(r"^\s*\+|\bбер(у|ём|ем)\b|\bвозьм(у|ём|ем)\b")
ORDER_WORDS_RE = re.compile(
    r"\bброн|\bзакаж(у|ем)\b|\bзаказываю\b|\bмне\s+(тоже|так\s*же)\b|^\s*и\s+мне\b")
QTY_RE = re.compile(r"\d+\s*(шт|штук|куст|уп|пак|пачк|горш|сем)|^\s*\d+\s*(шт\w*)?\s*[.!)]*\s*$")
WEAK_ORDER_RE = re.compile(r"\bхочу\b|\bмне\b|\bнужн[оаы]?\b")

WORD_NUMS = {"одн": 1, "один": 1, "два": 2, "две": 2, "три": 3, "четыр": 4, "пят": 5,
             "шест": 6, "семь": 7, "восем": 8, "девят": 9, "десят": 10, "пару": 2, "пара": 2}


def word_qty(text):
    for word in re.findall(r"[а-яё]+", text):
        if "дцат" in word:   # «пятнадцать», «двадцать» — не угадываем, пусть уточнит менеджер
            continue
        for stem, n in WORD_NUMS.items():
            if word == stem or (len(stem) >= 3 and word.startswith(stem)):
                return n
    return None


def parse_qty(text):
    m = re.search(r"\d+", text)
    if m and 0 < int(m.group()) <= 1000:
        return int(m.group())
    return word_qty(text.lower()) or 1


def classify_comment(text):
    """'cancel', 'order', 'question' или None (обычный комментарий)."""
    low = (text or "").lower().strip()
    if not low:
        return None
    if CANCEL_RE.search(low):
        return "cancel"
    question = "?" in low
    if STRONG_ORDER_RE.search(low):
        return "order"
    if not question:
        if ORDER_WORDS_RE.search(low) or QTY_RE.search(low):
            return "order"
        if WEAK_ORDER_RE.search(low) and (re.search(r"\d", low) or word_qty(low)):
            return "order"
    return "question" if question else None


def album_ext_prefix(photo_id):
    # "_" в LIKE означает «любой символ», поэтому экранируем: иначе photo12_% задевал бы photo123_…
    return f"photo{photo_id}\\_c%"


def on_photo_comment(obj):
    user_id = obj.get("from_id", 0)
    if user_id <= 0 or user_id in ADMIN_IDS:   # сообщество и админы заказы не оформляют
        return
    owner = obj.get("photo_owner_id")
    if owner is not None and owner != -GROUP_ID:
        return
    photo_id = obj["photo_id"]
    comment_id = obj["id"]
    text = (obj.get("text") or "").strip()
    kind = classify_comment(text)
    if kind is None:
        return

    item = catalog.photos.get(photo_id)
    if item is None:
        catalog.refresh_if_stale()   # фото могли добавить после последнего обновления
        item = catalog.photos.get(photo_id)
    if ORDER_ALBUM_IDS and (item is None or item["album_id"] not in ORDER_ALBUM_IDS):
        return   # фото не из альбомов с заказами
    title = item["title"] if item else f"фото {photo_id}"
    link = f"https://vk.com/photo-{GROUP_ID}_{photo_id}"

    if kind == "cancel":
        n = db.update(
            "UPDATE orders SET status='отменён' "
            "WHERE user_id=? AND ext_id LIKE ? ESCAPE '\\' AND status='новый'",
            (user_id, album_ext_prefix(photo_id)))
        if not n:
            return   # отменять нечего — не дёргаем админов
        notify_admins(f"❌ Отмена в альбоме\n{title}\nОт: vk.com/id{user_id}\n{link}")
        reply_comment(photo_id, comment_id, "Отменили 👌")
    elif kind == "order":
        qty = parse_qty(text.lower())
        try:
            db.run("INSERT INTO orders(source,user_id,item,qty,comment,ext_id,ts) VALUES (?,?,?,?,?,?,?)",
                   ("album", user_id, title, qty, text, f"photo{photo_id}_c{comment_id}", int(time.time())))
        except sqlite3.IntegrityError:
            return
        notify_admins(f"🛒 Заказ в альбоме\n{title} × {qty}\nКомментарий: {text}\n"
                      f"От: vk.com/id{user_id}\n{link}")
        reply_comment(photo_id, comment_id, f"Записали: {title} × {qty} 🌱")
    elif REPLY_IN_COMMENTS and read_vk:
        try:
            answer = ask_claude(
                user_id,
                f"Вопрос в комментарии под фото товара «{title}». Описание товара: "
                f"{item['desc'] if item else 'нет'}\nВопрос: {text}\n"
                f"Ответь в 1–3 предложениях.",
                use_history=False, max_tokens=300)
            if answer:
                reply_comment(photo_id, comment_id, answer)
        except Exception:
            log.exception("Ошибка ответа в комментарии")


def on_photo_comment_delete(obj):
    photo_id, comment_id = obj.get("photo_id"), obj.get("id")
    rows = db.run("SELECT user_id, item FROM orders WHERE ext_id=? AND status='новый'",
                  (f"photo{photo_id}_c{comment_id}",))
    if rows:
        db.run("UPDATE orders SET status='отменён' WHERE ext_id=?", (f"photo{photo_id}_c{comment_id}",))
        notify_admins(f"❌ Удалён комментарий с заказом\n{rows[0][1]}\nОт: vk.com/id{rows[0][0]}")


def order_items_text(order):
    items = order.get("preview_order_items") or []
    parts = [f"{(i.get('item') or {}).get('title', 'товар')} × {i.get('quantity', 1)}" for i in items]
    return ", ".join(parts) or f"{order.get('items_count', '?')} поз."


def on_market_order(obj, new):
    user_id = obj.get("user_id", 0)
    oid = obj.get("display_order_id") or obj.get("id")
    status = MARKET_STATUS.get(obj.get("status", 0), "новый")
    items = order_items_text(obj)
    total = (obj.get("total_price") or {}).get("text", "")
    if new:
        try:
            db.run("INSERT INTO orders(source,user_id,item,qty,comment,ext_id,status,ts) VALUES (?,?,?,?,?,?,?,?)",
                   ("market", user_id, f"Заказ №{oid}: {items}", None, total,
                    f"market{obj.get('id')}", status, int(time.time())))
        except sqlite3.IntegrityError:
            return   # это событие уже обработали
        notify_admins(f"🛒 Новый заказ в Маркете №{oid}\n{items}\nСумма: {total}\nОт: vk.com/id{user_id}")
    else:
        db.run("UPDATE orders SET status=? WHERE ext_id=?", (status, f"market{obj.get('id')}"))


def on_message_reply(obj):
    # Ответ из группы, написанный вручную менеджером, — бот замолкает в этом диалоге
    if obj.get("admin_author_id") and obj.get("id") not in sent_ids:
        peer_id = obj.get("peer_id", 0)
        if 0 < peer_id < 2_000_000_000:
            pause_user(peer_id)


def dispatch(raw):
    etype, obj = raw.get("type"), raw.get("object") or {}
    try:
        if etype == "message_new":
            on_message(obj.get("message", obj))
        elif etype == "message_reply":
            on_message_reply(obj)
        elif etype == "photo_comment_new":
            on_photo_comment(obj)
        elif etype == "photo_comment_delete":
            on_photo_comment_delete(obj)
        elif etype == "market_order_new":
            on_market_order(obj, new=True)
        elif etype == "market_order_edit":
            on_market_order(obj, new=False)
    except Exception:
        log.exception("Ошибка обработки события %s", etype)


def init():
    """Проверить настройки и создать клиентов ВК, ИИ и базу."""
    global group_session, vk, read_vk, claude, db
    missing = [name for name, ok in (
        ("VK_GROUP_TOKEN", GROUP_TOKEN),
        ("VK_GROUP_ID", GROUP_ID),
        ("ANTHROPIC_API_KEY", os.getenv("ANTHROPIC_API_KEY", "").strip()),
    ) if not ok]
    if missing:
        sys.exit("Не заполнены настройки в .env: " + ", ".join(missing))
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS пуст: уведомления о заказах и вызовах менеджера никто не получит")
    if not READ_TOKEN:
        log.warning("VK_READ_TOKEN пуст: бот не увидит альбомы и не ответит в комментариях")

    group_session = vk_api.VkApi(token=GROUP_TOKEN, api_version=API_VERSION)
    vk = group_session.get_api()
    read_vk = vk_api.VkApi(token=READ_TOKEN, api_version=API_VERSION).get_api() if READ_TOKEN else None
    claude = anthropic.Anthropic()  # ключ берётся из ANTHROPIC_API_KEY
    db = DB(DB_FILE)


def main():
    init()
    threading.Thread(target=catalog_loop, daemon=True).start()
    pool = ThreadPoolExecutor(max_workers=4)
    log.info("Бот запущен для группы %s, модель %s", GROUP_ID, MODEL)
    while True:
        try:
            longpoll = VkBotLongPoll(group_session, GROUP_ID)
            for event in longpoll.listen():
                pool.submit(dispatch, event.raw)
        except vk_api.exceptions.ApiError as e:
            if e.code == 5:
                sys.exit("ВК не принял VK_GROUP_TOKEN: проверь ключ сообщества в .env")
            log.error("Ошибка ВК при подключении к Long Poll: %s. Проверь, что Long Poll API включён "
                      "и у ключа есть нужные права (README, раздел 1). Повтор через 30 секунд", e)
            time.sleep(30)
        except Exception:
            log.exception("Long Poll упал, переподключаюсь через 5 секунд")
            time.sleep(5)


if __name__ == "__main__":
    main()
