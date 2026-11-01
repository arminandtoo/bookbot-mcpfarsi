"""Telegram front-end for the book agent: menu buttons, per-book link/save buttons, live search status."""
import asyncio
import html
import logging
import os
import re

from fastmcp import Client
from telegram import BotCommand, LinkPreviewOptions
from telegram import InlineKeyboardButton as Button
from telegram import InlineKeyboardMarkup as Keyboard
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from agent import MCP_CONFIG, ask, build_agent

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # hides one log line per getUpdates poll

MENU = Keyboard([
    [Button("📈 پرفروش‌ها", callback_data="q:best"), Button("🆕 تازه‌ها", callback_data="q:new")],
    [Button("🏷 تخفیف‌ها", callback_data="q:deals"), Button("🎧 صوتی رایگان", callback_data="q:free")],
    [Button("⭐ لیست من", callback_data="list"), Button("🔄 گفتگوی تازه", callback_data="new")],
])
QUICK = {
    "best": "پرفروش‌ترین کتاب‌های الان در ۳۰بوک و طاقچه رو معرفی کن",
    "new": "تازه‌ترین کتاب‌های ۳۰بوک و طاقچه چیه؟",
    "deals": "بهترین تخفیف‌های کتاب در ۳۰بوک چیه؟",
    "free": "کتاب‌های صوتی رایگان پرطرفدار طاقچه رو معرفی کن",
}
WELCOME = ("📚 <b>سلام! من دستیار کتاب هستم.</b>\n\n"
           "از <b>۳۰بوک</b> (کتاب چاپی) و <b>طاقچه</b> (الکترونیکی و صوتی) برات می‌گردم، "
           "قیمت‌ها رو مقایسه می‌کنم و نظر خواننده‌ها رو می‌گم.\n\n"
           "هر سوالی داری بنویس، مثلا:\n"
           "• <i>یه رمان عاشقانه‌ی خوب معرفی کن</i>\n"
           "• <i>صد سال تنهایی رو مقایسه کن</i>\n\n"
           "یا از دکمه‌های زیر شروع کن 👇")
STORES = {"30book": "۳۰بوک", "taaghche": "طاقچه", "wishlist": "لیست شما"}
ICONS = {"ebook": "📱", "audiobook": "🎧"}
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def price_text(price):
    return "رایگان" if price == 0 else f"{price:,} تومان" if price else ""


async def send(chat, text, markup=None):
    chunks = [text[i:i + 4096] for i in range(0, len(text), 4096)]  # Telegram message limit
    for n, chunk in enumerate(chunks):
        kb = markup if n == len(chunks) - 1 else None
        try:
            await chat.send_message(chunk, parse_mode=ParseMode.HTML, reply_markup=kb, link_preview_options=NO_PREVIEW)
        except BadRequest:  # the model wrote invalid HTML: send it as plain text
            await chat.send_message(re.sub(r"<[^>]+>", "", chunk), reply_markup=kb, link_preview_options=NO_PREVIEW)


def book_keyboard(books, cache):
    rows = []
    for b in books[:8]:
        cache[b["url"]] = b  
        row = [Button(f"{ICONS.get(b.get('format'), '📕')} {b['title'][:25]}", url=b["url"])]
        if len(f"s:{b['url']}".encode()) <= 64:  # Telegram callback_data limit
            row.append(Button("❤️ ذخیره", callback_data=f"s:{b['url']}"))
        rows.append(row)
    return Keyboard(rows) if rows else None


async def wishlist_view(context, user_id):
    result = await context.bot_data["mcp"].call_tool("wishlist_list_items", {"user_id": str(user_id)})
    items = result.structured_content["result"]
    if not items:
        return "⭐ لیست شما خالیه.\nزیر جواب‌ها روی «❤️ ذخیره» بزن تا کتاب اینجا بیاد.", None
    lines, rows = [], []
    for n, it in enumerate(items, 1):
        lines.append(" · ".join(filter(None, [f"{n}. <b>{html.escape(it['title'])}</b>", price_text(it["price"])])))
        link = [Button(f"🛒 {it['title'][:25]}", url=it["url"])] if it["url"].startswith("http") else []
        rows.append(link + [Button(f"🗑 حذف {n}", callback_data=f"d:{it['id']}")])
    return "⭐ <b>لیست شما</b>\n\n" + "\n".join(lines), Keyboard(rows)


async def run_query(update, context, text):
    chat = update.effective_chat
    status = await chat.send_message("⏳ یه لحظه صبر کن...")

    async def on_tools(names):
        where = " و ".join(sorted({STORES.get(n.split("_")[0], "منابع") for n in names}))
        try:
            await status.edit_text(f"🔎 در حال بررسی {where}...")
        except BadRequest:  # same text as the current status
            pass

    try:
        reply, books = await ask(context.bot_data["agent"], text, chat.id, update.effective_user.id, on_tools)
    except Exception:
        logging.exception("agent failed")
        reply, books = "⚠️ متاسفانه خطایی پیش آمد، دوباره امتحان کن.", []
    await status.delete()
    await send(chat, reply or "جوابی پیدا نکردم.", book_keyboard(books, context.bot_data.setdefault("books", {})))


async def start(update, context):
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.HTML, reply_markup=MENU)


async def reset(update, context):
    await context.bot_data["agent"].checkpointer.adelete_thread(str(update.effective_chat.id))


async def new(update, context):
    await reset(update, context)
    await update.message.reply_text("🔄 گفتگو از نو شروع شد.", reply_markup=MENU)


async def show_list(update, context):
    await send(update.effective_chat, *await wishlist_view(context, update.effective_user.id))


async def on_text(update, context):
    await run_query(update, context, update.message.text)


async def on_button(update, context):
    q = update.callback_query
    kind, _, arg = q.data.partition(":")
    user_id = str(update.effective_user.id)  # from Telegram, so forged callback data can't touch other users
    if kind == "q" and arg in QUICK:
        await q.answer()
        await run_query(update, context, QUICK[arg])
    elif kind == "list":
        await q.answer()
        await show_list(update, context)
    elif kind == "new":
        await reset(update, context)
        await q.answer("🔄 گفتگو از نو شروع شد")
    elif kind == "s":
        book = context.bot_data.get("books", {}).get(arg)
        if not book:
            await q.answer("این دکمه قدیمی شده، دوباره جستجو کن.", show_alert=True)
            return
        result = await context.bot_data["mcp"].call_tool("wishlist_add", {
            "user_id": user_id, "title": book["title"], "url": arg,
            "store": "30book" if "30book.com" in arg else "taaghche",
            "price": book.get("final_price", book.get("price"))})
        await q.answer("✅ قبلا ذخیره شده" if result.data == "already saved" else "❤️ به لیست اضافه شد")
    elif kind == "d" and arg.isdigit():
        await context.bot_data["mcp"].call_tool("wishlist_remove", {"user_id": user_id, "item_id": int(arg)})
        text, kb = await wishlist_view(context, user_id)
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        await q.answer("🗑 حذف شد")
    else:
        await q.answer()


async def main():
    builder = ApplicationBuilder().token(os.environ["TELEGRAM_BOT_TOKEN"]).concurrent_updates(True)
    if proxy := os.getenv("TELEGRAM_PROXY"):
        builder = builder.proxy(proxy).get_updates_proxy(proxy)
    app = builder.build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", show_list))
    app.add_handler(CommandHandler("new", new))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    async with Client(MCP_CONFIG) as mcp, app:
        app.bot_data["mcp"] = mcp
        app.bot_data["agent"] = await build_agent(mcp)
        await app.bot.set_my_commands([BotCommand("start", "منوی اصلی"), BotCommand("list", "لیست من"),
                                       BotCommand("new", "گفتگوی تازه")])
        await app.start()
        await app.updater.start_polling()
        print("Bot is running. Ctrl+C to stop.")
        try:
            await asyncio.Event().wait()
        finally:
            await app.updater.stop()
            await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
