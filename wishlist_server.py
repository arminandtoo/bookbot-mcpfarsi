"""Our own MCP server: a per-user book wishlist in SQLite. Launched over stdio by agent.py."""
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from fastmcp import FastMCP

DB = os.getenv("WISHLIST_DB", str(Path(__file__).with_name("wishlist.db")))
mcp = FastMCP("wishlist")


def query(sql: str, args: tuple = ()):
    with closing(sqlite3.connect(DB)) as con, con:
        con.row_factory = sqlite3.Row
        con.execute("CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, user_id TEXT, "
                    "title TEXT, store TEXT, price INTEGER, url TEXT)")
        cur = con.execute(sql, args)
        return [dict(r) for r in cur.fetchall()], cur.rowcount, cur.lastrowid


@mcp.tool
def add(user_id: str, title: str, store: str = "", price: int | None = None, url: str = "") -> str:
    """Save a book to the user's wishlist. store: '30book' or 'taaghche'. price in Toman."""
    if url and query("SELECT 1 FROM items WHERE user_id = ? AND url = ?", (user_id, url))[0]:
        return "already saved"
    _, _, item_id = query("INSERT INTO items (user_id, title, store, price, url) VALUES (?, ?, ?, ?, ?)",
                          (user_id, title, store, price, url))
    return f"saved as item {item_id}"


@mcp.tool
def list_items(user_id: str) -> list[dict]:
    """List the books saved in the user's wishlist."""
    rows, _, _ = query("SELECT id, title, store, price, url FROM items WHERE user_id = ?", (user_id,))
    return rows


@mcp.tool
def remove(user_id: str, item_id: int) -> str:
    """Remove one item (by id from list_items) from the user's wishlist."""
    _, count, _ = query("DELETE FROM items WHERE id = ? AND user_id = ?", (item_id, user_id))
    return "removed" if count else "not found"


if __name__ == "__main__":
    mcp.run(show_banner=False)
