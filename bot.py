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
import time
import random
import logging
import sqlite3
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
import vk_api
from vk_api.bot_longpoll import VkBotLongPoll
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
import anthropic

# ---------------------------------------------------------------- настройки

load_dotenv()

GROUP_TOKEN = os.environ["VK_GROUP_TOKEN"]
GROUP_ID = int(os.environ["VK_GROUP_ID"])
READ_TOKEN = os.getenv("VK_READ_TOKEN", "").strip()
SHOP_NAME = os.getenv("SHOP_NAME", "наш магазин")
MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x]
ORDER_ALBUM_IDS = [int(x) for x in os.getenv("ORDER_ALBUM_IDS", "").replace(" ", "").split(",") if x]
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

group_session = vk_api.VkApi(token=GROUP_TOKEN, api_version=API_VERSION)
vk = group_session.get_api()
read_vk = vk_api.VkApi(token=READ_TOKEN, api_version=API_VERSION).get_api() if READ_TOKEN else None
claude = anthropic.Anthropic()  # ключ берётся из ANTHROPIC_API_KEY

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


db = DB(DB_FILE)


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


class Catalog:
    def __init__(self):
        self.items = []      # все позиции для поиска и для ИИ
        self.photos = {}     # photo_id -> позиция из альбома
        self.lock = threading.Lock()

    def _api(self):
        return read_vk or vk

    def refresh(self):
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
        try:
            album_ids = ORDER_ALBUM_IDS
            album_titles = {}
            albums = api.photos.getAlbums(owner_id=-GROUP_ID).get("items", [])
            for a in albums:
                album_titles[a["id"]] = a.get("title", "")
            if not album_ids:
                album_ids = [a["id"] for a in albums]
            for aid in album_ids:
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
                            "price": "",
                            "desc": text[:600],
                            "available": True,
                        }
                        items.append(entry)
                        photos[p["id"]] = entry
                    offset += 1000
                    if offset >= r.get("count", 0):
                        break
        except Exception as e:
            log.warning("Не удалось загрузить альбомы (нужен VK_READ_TOKEN?): %s", e)

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


def ask_claude(user_id, text, image_urls=(), use_history=True, max_tokens=1000):
    content = [{"type": "image", "source": {"type": "url", "url": u}} for u in image_urls]
    content.append({"type": "text", "text": text or "(прислал фото без текста)"})

    messages = get_history(user_id) if use_history else []
    messages.append({"role": "user", "content": content})
    system = [{"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}}]

    for _ in range(5):
        resp = claude.messages.create(
            model=MODEL, max_tokens=max_tokens, system=system,
            tools=TOOLS if use_history else [], messages=messages,
        )
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text").strip()
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                results.append({
                    "type": "tool_result", "tool_use_id": b.id,
                    "content": run_tool(b.name, b.input, user_id),
                })
        messages.append({"role": "user", "content": results})
    return "Секунду, уточню у менеджера."

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


def admin_command(user_id, peer_id, text):
    parts = text.split()
    cmd = parts[0].lower()
    if cmd == "/вкл" and len(parts) > 1:
        uid = int(re.sub(r"\D", "", parts[1]))
        resume_user(uid)
        send(peer_id, f"Бот снова отвечает пользователю {uid}.", keyboard=False)
    elif cmd == "/выкл" and len(parts) > 1:
        uid = int(re.sub(r"\D", "", parts[1]))
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
            return
        try:
            answer = ask_claude(user_id, text, images)
        except Exception:
            log.exception("Ошибка ИИ")
            answer = "Что-то пошло не так, передал вопрос менеджеру."
            call_manager(user_id, f"ошибка бота, вопрос: {text[:200]}")

        add_history(user_id, "user", (text or "") + (" [фото]" if images else ""))
        add_history(user_id, "assistant", answer)
        send(peer_id, answer)


ORDER_RE = re.compile(
    r"(^\s*\+|\bбер[уёе]|\bвозьм|\bзаказ|\bброн|\bхочу|\bмне\b|"
    r"^\s*\d+\s*$|\d+\s*(шт|штук|куст|уп|пак|пачк|горш|сем))")
CANCEL_RE = re.compile(r"\bотмен|\bне\s+буду|\bпередумал")


WORD_NUMS = {"одн": 1, "один": 1, "два": 2, "две": 2, "три": 3, "четыр": 4, "пят": 5,
             "шест": 6, "семь": 7, "восем": 8, "девят": 9, "десят": 10, "пару": 2, "пара": 2}


def parse_qty(text):
    m = re.search(r"\d+", text)
    if m and 0 < int(m.group()) <= 1000:
        return int(m.group())
    for word in re.findall(r"[а-яё]+", text):
        for stem, n in WORD_NUMS.items():
            if word == stem or (len(stem) >= 3 and word.startswith(stem)):
                return n
    return 1


def on_photo_comment(obj):
    user_id = obj.get("from_id", 0)
    if user_id <= 0:
        return
    photo_id = obj["photo_id"]
    comment_id = obj["id"]
    text = (obj.get("text") or "").strip()
    low = text.lower()

    item = catalog.photos.get(photo_id)
    if item is None and catalog.items:
        catalog.refresh()
        item = catalog.photos.get(photo_id)
    if ORDER_ALBUM_IDS and item and item["album_id"] not in ORDER_ALBUM_IDS:
        return
    title = item["title"] if item else f"фото {photo_id}"
    link = f"https://vk.com/photo-{GROUP_ID}_{photo_id}"

    if CANCEL_RE.search(low):
        db.run("UPDATE orders SET status='отменён' WHERE user_id=? AND ext_id LIKE ? AND status='новый'",
               (user_id, f"photo{photo_id}_%"))
        notify_admins(f"❌ Отмена в альбоме\n{title}\nОт: vk.com/id{user_id}\n{link}")
        reply_comment(photo_id, comment_id, "Отменили 👌")
    elif ORDER_RE.search(low):
        qty = parse_qty(low)
        try:
            db.run("INSERT INTO orders(source,user_id,item,qty,comment,ext_id,ts) VALUES (?,?,?,?,?,?,?)",
                   ("album", user_id, title, qty, text, f"photo{photo_id}_c{comment_id}", int(time.time())))
        except sqlite3.IntegrityError:
            return
        notify_admins(f"🛒 Заказ в альбоме\n{title} × {qty}\nКомментарий: {text}\n"
                      f"От: vk.com/id{user_id}\n{link}")
        reply_comment(photo_id, comment_id, f"Записали: {title} × {qty} 🌱")
    elif REPLY_IN_COMMENTS and read_vk and "?" in text:
        try:
            answer = ask_claude(
                user_id,
                f"Вопрос в комментарии под фото товара «{title}». Описание товара: "
                f"{item['desc'] if item else 'нет'}\nВопрос: {text}\n"
                f"Ответь в 1–3 предложениях.",
                use_history=False, max_tokens=300)
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
            pass
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


def main():
    threading.Thread(target=catalog_loop, daemon=True).start()
    pool = ThreadPoolExecutor(max_workers=4)
    log.info("Бот запущен для группы %s, модель %s", GROUP_ID, MODEL)
    while True:
        try:
            longpoll = VkBotLongPoll(group_session, GROUP_ID)
            for event in longpoll.listen():
                pool.submit(dispatch, event.raw)
        except Exception:
            log.exception("Long Poll упал, переподключаюсь через 5 секунд")
            time.sleep(5)


if __name__ == "__main__":
    main()
