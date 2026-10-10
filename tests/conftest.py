import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot  # noqa: E402

GROUP_ID = 100


class FakeMethods:
    """Записывает вызовы методов ВК: fake.messages.send(...) -> fake.calls."""

    def __init__(self, owner, section):
        self._owner, self._section = owner, section

    def __getattr__(self, name):
        def call(**kwargs):
            method = f"{self._section}.{name}"
            self._owner.calls.append((method, kwargs))
            handler = self._owner.handlers.get(method)
            if handler:
                return handler(**kwargs)
            return len(self._owner.calls)
        return call


class FakeVk:
    def __init__(self):
        self.calls = []
        self.handlers = {}

    def __getattr__(self, section):
        return FakeMethods(self, section)

    def sent(self, method="messages.send"):
        return [kw for m, kw in self.calls if m == method]


@pytest.fixture
def env(monkeypatch):
    vk, read_vk = FakeVk(), FakeVk()
    monkeypatch.setattr(bot, "vk", vk)
    monkeypatch.setattr(bot, "read_vk", read_vk)
    monkeypatch.setattr(bot, "db", bot.DB(":memory:"))
    monkeypatch.setattr(bot, "catalog", bot.Catalog())
    monkeypatch.setattr(bot, "GROUP_ID", GROUP_ID)
    monkeypatch.setattr(bot, "ADMIN_IDS", [1])
    monkeypatch.setattr(bot, "ORDER_ALBUM_IDS", [])
    monkeypatch.setattr(bot, "REPLY_IN_COMMENTS", True)
    monkeypatch.setattr(bot, "user_locks", bot.defaultdict(bot.threading.Lock))
    return SimpleNamespace(vk=vk, read_vk=read_vk)


def add_photo(photo_id, title="Петуния ампельная", album_id=5):
    entry = {"kind": "album", "id": photo_id, "album_id": album_id, "album": "Цветы",
             "title": title, "price": "", "desc": title, "available": True}
    bot.catalog.items.append(entry)
    bot.catalog.photos[photo_id] = entry
    bot.catalog.last_refresh = bot.time.time()
    return entry


class FakeClaude:
    """Отдаёт заранее заданные ответы и запоминает запросы."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def text_resp(text):
    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])


def tool_resp(name, args, tool_id="t1"):
    return SimpleNamespace(stop_reason="tool_use",
                           content=[SimpleNamespace(type="tool_use", name=name, input=args, id=tool_id)])
