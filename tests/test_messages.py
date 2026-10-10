import pytest

import bot
from conftest import FakeClaude, text_resp, tool_resp


def msg(text, user=42, **extra):
    m = {"from_id": user, "peer_id": user, "text": text}
    m.update(extra)
    return m


@pytest.fixture
def ai(env, monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "KNOWLEDGE_FILE", str(tmp_path / "knowledge.md"))
    fake = FakeClaude()
    monkeypatch.setattr(bot, "claude", fake)
    return fake


def test_greeting(env):
    bot.on_message(msg("Начать"))
    [m] = env.vk.sent()
    assert "Здравствуйте" in m["message"] and m["keyboard"]


def test_question_goes_to_ai_and_history(env, ai):
    ai.responses = [text_resp("Поливайте раз в два дня 🌱")]
    bot.on_message(msg("Как поливать петунию?"))
    assert env.vk.sent()[0]["message"] == "Поливайте раз в два дня 🌱"
    assert bot.get_history(42) == [
        {"role": "user", "content": "Как поливать петунию?"},
        {"role": "assistant", "content": "Поливайте раз в два дня 🌱"},
    ]
    assert ai.requests[0]["tools"] == bot.TOOLS


def test_tool_call_get_my_orders(env, ai):
    bot.db.run("INSERT INTO orders(source,user_id,item,qty,ext_id,ts) VALUES ('album',42,'Томат',2,'x',0)")
    ai.responses = [tool_resp("get_my_orders", {}), text_resp("У вас Томат × 2")]
    bot.on_message(msg("что я заказывал?"))
    tool_result = ai.requests[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and "Томат × 2" in tool_result["content"]
    assert env.vk.sent()[0]["message"] == "У вас Томат × 2"


def test_ai_error_calls_manager_and_pauses(env, ai):
    ai.responses = [RuntimeError("overloaded")]
    bot.on_message(msg("есть ли рассада томатов?"))
    texts = [(m["peer_id"], m["message"]) for m in env.vk.sent()]
    assert any(p == 1 and "Нужен менеджер" in t for p, t in texts)
    assert any(p == 42 and "передал вопрос менеджеру" in t for p, t in texts)
    assert bot.is_paused(42)


def test_empty_ai_answer_is_not_sent_as_blank(env, ai):
    ai.responses = [text_resp("")]
    bot.on_message(msg("вопрос"))
    assert any("передал вопрос менеджеру" in m["message"] for m in env.vk.sent())


def test_paused_user_gets_no_answer(env, ai):
    bot.pause_user(42)
    bot.on_message(msg("ау"))
    assert env.vk.sent() == [] and ai.requests == []


def test_manager_button(env):
    bot.on_message(msg("Позвать менеджера"))
    assert bot.is_paused(42)
    assert {m["peer_id"] for m in env.vk.sent()} == {1, 42}


def test_voice_message_gets_hint(env, ai):
    bot.on_message(msg("", attachments=[{"type": "audio_message", "audio_message": {}}]))
    assert "Голосовые" in env.vk.sent()[0]["message"]
    assert ai.requests == []


def test_photo_is_sent_to_ai(env, ai, monkeypatch):
    monkeypatch.setattr(bot, "image_block", lambda url: {"type": "image", "source": {"type": "url", "url": url}})
    ai.responses = [text_resp("Похоже на мучнистую росу")]
    photo = {"type": "photo", "photo": {"sizes": [
        {"width": 100, "url": "small"}, {"width": 800, "url": "big"}]}}
    bot.on_message(msg("", attachments=[photo]))
    content = ai.requests[0]["messages"][-1]["content"]
    assert content[0]["source"]["url"] == "big"
    assert bot.get_history(42)[0]["content"] == " [фото]"


def test_group_chats_are_ignored(env, ai):
    bot.on_message({"from_id": 42, "peer_id": 2_000_000_001, "text": "привет"})
    assert env.vk.sent() == []


def test_admin_commands(env):
    bot.on_message(msg("/выкл vk.com/id555", user=1))
    assert bot.is_paused(555)
    bot.on_message(msg("/вкл 555", user=1))
    assert not bot.is_paused(555)
    bot.on_message(msg("/вкл abc", user=1))   # раньше тут падало с ValueError
    assert "Укажите id" in env.vk.sent()[-1]["message"]
    bot.on_message(msg("/заказы", user=1))
    assert env.vk.sent()[-1]["message"] == "Активных заказов нет."


def test_manual_admin_reply_pauses_dialog(env):
    bot.on_message_reply({"admin_author_id": 1, "id": 999, "peer_id": 42})
    assert bot.is_paused(42)


def test_bot_own_reply_does_not_pause(env):
    bot.sent_ids.add(1000)
    bot.on_message_reply({"admin_author_id": 1, "id": 1000, "peer_id": 42})
    assert not bot.is_paused(42)


def test_long_answer_is_split(env):
    bot.send(42, "а" * 9000)
    assert [len(m["message"]) for m in env.vk.sent()] == [4000, 4000, 1000]


def test_history_drops_leading_assistant_and_merges(env):
    for role, text in [("assistant", "x"), ("user", "a"), ("user", "b"), ("assistant", "c")]:
        bot.add_history(7, role, text)
    assert bot.get_history(7) == [
        {"role": "user", "content": "a\nb"}, {"role": "assistant", "content": "c"}]


def test_comment_answer_has_no_tools(env, ai):
    ai.responses = [text_resp("ok")]
    bot.ask_claude(42, "вопрос", use_history=False, max_tokens=300)
    assert "tools" not in ai.requests[0]


def test_image_block_downloads_and_detects_type(tmp_path):
    f = tmp_path / "leaf.jpg"
    f.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 100)
    block = bot.image_block(f.as_uri())
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "image/jpeg"


def test_image_block_falls_back_to_url():
    url = "file:///nonexistent/photo.jpg"
    assert bot.image_block(url)["source"] == {"type": "url", "url": url}
