import pytest

import bot


@pytest.mark.parametrize("text, price", [
    ("Петуния ампельная\nЦена 150 р.", "150 ₽"),
    ("Томат Бычье сердце — 80₽", "80 ₽"),
    ("Гортензия 1 200 руб", "1200 ₽"),
    ("Без цены", ""),
])
def test_parse_price(text, price):
    assert bot.parse_price(text) == price


@pytest.mark.parametrize("value, ids", [
    ("123", [123]), ("-123", [123]), ("club123", [123]), ("1, 2 ,3", [1, 2, 3]), ("", []),
])
def test_id_list(value, ids):
    assert bot.id_list(value) == ids


def test_refresh_loads_market_and_albums(env):
    env.read_vk.handlers.update({
        "market.get": lambda **k: {"count": 2, "items": [
            {"id": 1, "title": "Семена укропа", "price": {"text": "50 ₽"}, "availability": 0},
            {"id": 2, "title": "Удалённый", "availability": 1}]},
        "photos.getAlbums": lambda **k: {"items": [{"id": 5, "title": "Петунии"}, {"id": 6, "title": "Битый"}]},
        "photos.get": lambda album_id, **k: (
            {"count": 1, "items": [{"id": 70, "text": "Петуния синяя\n120 р."}]}
            if album_id == 5 else (_ for _ in ()).throw(RuntimeError("нет доступа"))),
    })
    bot.catalog.refresh()
    titles = [it["title"] for it in bot.catalog.items]
    assert titles == ["Семена укропа", "Петуния синяя"]   # ошибка в альбоме 6 не ломает остальное
    assert bot.catalog.photos[70]["price"] == "120 ₽"
    assert bot.catalog.photos[70]["album"] == "Петунии"
    assert [it["title"] for it in bot.catalog.search("петунию синюю")] == ["Петуния синяя"]


def test_failed_refresh_keeps_old_catalog(env):
    old = {"kind": "market", "id": 1, "title": "Старое", "price": "", "desc": "", "available": True}
    bot.catalog.items = [old]
    boom = lambda **k: (_ for _ in ()).throw(RuntimeError("сеть"))  # noqa: E731
    env.read_vk.handlers.update({"market.get": boom, "photos.getAlbums": boom})
    bot.catalog.refresh()
    assert bot.catalog.items == [old]


def test_market_order_new_and_edit(env):
    order = {"id": 9, "display_order_id": "100-9", "user_id": 42, "status": 0,
             "preview_order_items": [{"item": {"title": "Петуния"}, "quantity": 2}],
             "total_price": {"text": "300 ₽"}}
    bot.on_market_order(order, new=True)
    bot.on_market_order(order, new=True)   # повтор события — без второго уведомления
    assert len(env.vk.sent()) == 1
    assert "Петуния × 2" in env.vk.sent()[0]["message"]
    bot.on_market_order(dict(order, status=4), new=False)
    assert bot.user_orders(42)[0][3] == "выполнен"


def test_dispatch_routes_and_survives_errors(env, monkeypatch):
    seen = []
    monkeypatch.setattr(bot, "on_message", lambda m: seen.append(m["text"]))
    bot.dispatch({"type": "message_new", "object": {"message": {"text": "hi"}}})
    assert seen == ["hi"]
    monkeypatch.setattr(bot, "on_message", lambda m: 1 / 0)
    bot.dispatch({"type": "message_new", "object": {"message": {}}})   # не падает


def test_init_reports_missing_settings(monkeypatch):
    monkeypatch.setattr(bot, "GROUP_TOKEN", "")
    monkeypatch.setattr(bot, "GROUP_ID", 0)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit) as e:
        bot.init()
    assert "VK_GROUP_TOKEN" in str(e.value) and "VK_GROUP_ID" in str(e.value)


def test_system_prompt_contains_catalog_and_knowledge(env, monkeypatch, tmp_path):
    kb = tmp_path / "k.md"
    kb.write_text("Доставка СДЭКом", encoding="utf-8")
    monkeypatch.setattr(bot, "KNOWLEDGE_FILE", str(kb))
    bot.catalog.items = [{"kind": "album", "id": 1, "album": "Цветы", "title": "Петуния",
                          "price": "150 ₽", "desc": "", "available": True}]
    p = bot.system_prompt()
    assert "Доставка СДЭКом" in p and "Петуния — 150 ₽" in p
