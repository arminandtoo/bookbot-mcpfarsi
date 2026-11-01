"""Run: python test_bot.py — checks wishlist isolation between users and user_id injection."""
import asyncio
import os
import tempfile
from types import SimpleNamespace

os.environ["WISHLIST_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

from fastmcp import Client  # noqa: E402

from agent import books_in, run_tool, to_schema  # noqa: E402
from wishlist_server import mcp  # noqa: E402


async def main():
    async with Client(mcp) as c:
        await c.call_tool("add", {"user_id": "a", "title": "صد سال تنهایی", "price": 495000})
        await c.call_tool("add", {"user_id": "b", "title": "بوف کور"})
        items = (await c.call_tool("list_items", {"user_id": "a"})).structured_content["result"]
        assert [i["title"] for i in items] == ["صد سال تنهایی"], items
        assert (await c.call_tool("remove", {"user_id": "b", "item_id": items[0]["id"]})).data == "not found"
        await c.call_tool("add", {"user_id": "a", "title": "x", "url": "https://u/1"})
        assert (await c.call_tool("add", {"user_id": "a", "title": "x", "url": "https://u/1"})).data == "already saved"

        # the model never sees user_id, and can't override it
        add = next(t for t in await c.list_tools() if t.name == "add")
        add.name = "wishlist_add"
        assert "user_id" not in to_schema(add)["function"]["parameters"]["properties"]

    sent = {}
    async def fake_call(name, args):
        sent.update(args)
        return SimpleNamespace(content=[])
    call = {"name": "wishlist_list_items", "args": {"user_id": "victim"}, "id": "1"}
    await run_tool(SimpleNamespace(call_tool=fake_call), call, "me")
    assert sent["user_id"] == "me", sent

    # buttons only for books the reply links to, in reply order; /book/37 must not match /book/376
    outputs = ['{"items": [{"title": "A", "url": "https://t.com/book/376"}, {"title": "B", "url": "https://t.com/book/37"}]}',
               '{"books": [{"title": "C", "url": "https://s.com/book/9"}]}', "Error: timeout"]
    reply = '<a href="https://s.com/book/9">C</a> then <a href="https://t.com/book/376">A</a>'
    assert [b["title"] for b in books_in(reply, outputs)] == ["C", "A"]
    print("ok")


asyncio.run(main())
