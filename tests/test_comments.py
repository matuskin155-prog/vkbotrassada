import pytest

import bot
from conftest import GROUP_ID, add_photo


@pytest.mark.parametrize("text", [
    "+", "+1", "++", "Беру 2", "беру", "берём три", "Возьму пару", "2 шт", "5 штук", "3",
    "мне тоже", "И мне", "Бронь", "забронируйте мне 2", "закажу 4", "хочу 2", "мне 3 шт",
    "беру 2, когда отправка?",
])
def test_orders(text):
    assert bot.classify_comment(text) == "order"


@pytest.mark.parametrize("text", [
    "хочу узнать цену?", "Сколько стоит 1 шт?", "как заказать?", "бронь есть?",
    "мне кажется это не петуния?", "когда будет в наличии?",
])
def test_questions(text):
    assert bot.classify_comment(text) == "question"


@pytest.mark.parametrize("text", [
    "", "Красота!", "берёза красивая", "бережно упакуйте", "хочу узнать цену",
    "мне кажется, это не томат", "спасибо, всё дошло",
])
def test_not_orders(text):
    assert bot.classify_comment(text) is None


@pytest.mark.parametrize("text", ["отмена", "Отмените пожалуйста", "не буду брать", "передумала"])
def test_cancel(text):
    assert bot.classify_comment(text) == "cancel"


@pytest.mark.parametrize("text, qty", [
    ("беру 2", 2), ("+", 1), ("возьму пару", 2), ("беру три", 3), ("беру одну", 1),
    ("10 шт", 10), ("беру пять кустов", 5), ("беру пятнадцать", 1), ("двадцать штук", 1),
    ("беру 5000", 1),
])
def test_parse_qty(text, qty):
    assert bot.parse_qty(text) == qty


def comment(cid, text, user=42, photo=7, **extra):
    obj = {"id": cid, "from_id": user, "photo_id": photo, "photo_owner_id": -GROUP_ID, "text": text}
    obj.update(extra)
    return obj


def orders():
    return bot.db.run("SELECT user_id, item, qty, status, ext_id FROM orders ORDER BY id")


def test_order_is_saved_and_admin_notified(env):
    add_photo(7)
    bot.on_photo_comment(comment(1, "Беру 3"))
    assert orders() == [(42, "Петуния ампельная", 3, "новый", "photo7_c1")]
    [msg] = env.vk.sent()
    assert msg["peer_id"] == 1 and "Петуния ампельная × 3" in msg["message"]
    [reply] = env.read_vk.sent("photos.createComment")
    assert reply["reply_to_comment"] == 1 and "× 3" in reply["message"]


def test_duplicate_event_does_not_double_order(env):
    add_photo(7)
    bot.on_photo_comment(comment(1, "+"))
    bot.on_photo_comment(comment(1, "+"))
    assert len(orders()) == 1
    assert len(env.vk.sent()) == 1


def test_question_is_not_an_order(env, monkeypatch):
    add_photo(7)
    monkeypatch.setattr(bot, "ask_claude", lambda *a, **k: "Стоит 150 ₽.")
    bot.on_photo_comment(comment(1, "Сколько стоит 1 шт?"))
    assert orders() == []
    assert env.vk.sent() == []
    [reply] = env.read_vk.sent("photos.createComment")
    assert reply["message"] == "Стоит 150 ₽."


def test_admin_comment_is_ignored(env):
    add_photo(7)
    bot.on_photo_comment(comment(1, "Заказ принят, беру в работу", user=1))
    assert orders() == []


def test_other_owner_photo_is_ignored(env):
    add_photo(7)
    bot.on_photo_comment(comment(1, "беру 2", photo_owner_id=-999))
    assert orders() == []


def test_order_albums_filter_skips_unknown_photo(env, monkeypatch):
    monkeypatch.setattr(bot, "ORDER_ALBUM_IDS", [5])
    add_photo(7, album_id=5)
    bot.catalog.refresh = lambda: None
    bot.on_photo_comment(comment(1, "беру 2", photo=8))   # фото не из альбомов с заказами
    assert orders() == []
    bot.on_photo_comment(comment(2, "беру 2", photo=7))
    assert len(orders()) == 1


def test_cancel_only_affects_own_order_on_that_photo(env):
    add_photo(12)
    add_photo(123, title="Томат")
    bot.on_photo_comment(comment(1, "беру 2", photo=12))
    bot.on_photo_comment(comment(2, "беру 1", photo=123))
    bot.on_photo_comment(comment(3, "беру 1", photo=12, user=43))
    bot.on_photo_comment(comment(4, "отмена", photo=12))
    statuses = {(u, ext): st for u, _, _, st, ext in orders()}
    assert statuses == {
        (42, "photo12_c1"): "отменён",
        (42, "photo123_c2"): "новый",   # раньше LIKE 'photo12_%' задевал и это фото
        (43, "photo12_c3"): "новый",
    }


def test_cancel_without_order_does_not_spam_admins(env):
    add_photo(7)
    bot.on_photo_comment(comment(1, "отмена"))
    assert env.vk.sent() == []
    assert env.read_vk.sent("photos.createComment") == []


def test_deleted_comment_cancels_order(env):
    add_photo(7)
    bot.on_photo_comment(comment(1, "беру 2"))
    bot.on_photo_comment_delete({"id": 1, "photo_id": 7})
    assert orders()[0][3] == "отменён"
    assert "Удалён комментарий" in env.vk.sent()[-1]["message"]


def test_unknown_photo_triggers_throttled_refresh(env):
    calls = []
    bot.catalog.refresh = lambda: calls.append(1)
    bot.catalog.last_refresh = 0
    bot.on_photo_comment(comment(1, "беру", photo=99))
    assert calls == [1]
    assert orders()[0][1] == "фото 99"
    bot.catalog.last_refresh = bot.time.time()
    bot.on_photo_comment(comment(2, "беру", photo=98))
    assert calls == [1]


def test_no_reply_in_comments_when_disabled(env, monkeypatch):
    monkeypatch.setattr(bot, "REPLY_IN_COMMENTS", False)
    add_photo(7)
    bot.on_photo_comment(comment(1, "беру 2"))
    assert len(orders()) == 1
    assert env.read_vk.sent("photos.createComment") == []
