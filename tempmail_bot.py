"""
Telegram Temp Mail Bot
Uses mail.tm API — reliable on cloud servers
Fixed: job_queue NoneType + email creation timeout
"""

import logging
import re
import asyncio
import os
import random
import string
from collections import defaultdict

import aiohttp
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

# ===================== CONFIG =====================
BOT_TOKEN     = os.environ.get("BOT_TOKEN", "8577440207:AAFjbJLScFEx1tPOs6WlHIFnLnPDyNWkW6o)
ADMIN_ID      = int(os.environ.get("7540185501", "0"))
MAIL_TM_API   = "https://api.mail.tm"
MAX_RETRIES   = 5          # increased retries
AUTO_INTERVAL = 10
POOL_SIZE     = 100
# ==================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

http_session: aiohttp.ClientSession | None = None
user_sessions: dict = {}
user_locks = defaultdict(asyncio.Lock)
all_users: set = set()


# ============================================================
# HTTP helpers
# ============================================================

async def get_http_session() -> aiohttp.ClientSession:
    global http_session
    if http_session is None or http_session.closed:
        connector = aiohttp.TCPConnector(
            limit=POOL_SIZE,
            limit_per_host=50,
            ttl_dns_cache=300,
            enable_cleanup_closed=True
        )
        timeout = aiohttp.ClientTimeout(total=30, connect=10)  # longer timeout
        http_session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={"Content-Type": "application/json", "Accept": "application/json"}
        )
    return http_session


async def api_request(method: str, endpoint: str, json_data: dict = None,
                      token: str = None, retries: int = MAX_RETRIES) -> dict | None:
    session = await get_http_session()
    url     = f"{MAIL_TM_API}{endpoint}"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    for attempt in range(retries):
        try:
            if method == "GET":
                async with session.get(url, headers=headers) as resp:
                    if resp.status in (200, 201):
                        return await resp.json()
                    logger.warning(f"GET {endpoint} → {resp.status} attempt {attempt+1}")
            elif method == "POST":
                async with session.post(url, json=json_data, headers=headers) as resp:
                    if resp.status in (200, 201):
                        return await resp.json()
                    text = await resp.text()
                    logger.warning(f"POST {endpoint} → {resp.status} attempt {attempt+1}: {text[:100]}")
            elif method == "DELETE":
                async with session.delete(url, headers=headers) as resp:
                    return {"ok": resp.status in (200, 204)}
        except asyncio.TimeoutError:
            logger.warning(f"Timeout attempt {attempt+1} for {endpoint}")
        except aiohttp.ClientError as e:
            logger.warning(f"ClientError attempt {attempt+1}: {e}")
        if attempt < retries - 1:
            await asyncio.sleep(1.0 * (attempt + 1))  # longer backoff
    return None


# ============================================================
# mail.tm API
# ============================================================

def random_string(length=10) -> str:
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))


async def get_domain() -> str | None:
    data = await api_request("GET", "/domains?page=1")
    if data and "hydra:member" in data and data["hydra:member"]:
        return data["hydra:member"][0]["domain"]
    return None


async def create_account() -> dict | None:
    domain = await get_domain()
    if not domain:
        logger.error("Failed to get domain from mail.tm")
        return None

    username = random_string(10)
    password = random_string(12) + "A1!"  # meets password requirements
    email    = f"{username}@{domain}"

    logger.info(f"Creating mail.tm account: {email}")

    reg = await api_request("POST", "/accounts", {"address": email, "password": password})
    if not reg or "id" not in reg:
        logger.error(f"Registration failed: {reg}")
        return None

    auth = await api_request("POST", "/token", {"address": email, "password": password})
    if not auth or "token" not in auth:
        logger.error(f"Auth failed: {auth}")
        return None

    logger.info(f"Account created successfully: {email}")
    return {
        "email":      email,
        "password":   password,
        "account_id": reg.get("id", ""),
        "token":      auth["token"]
    }


async def get_messages(token: str) -> list:
    data = await api_request("GET", "/messages?page=1", token=token)
    if data and "hydra:member" in data:
        return data["hydra:member"]
    return []


async def get_message(token: str, msg_id: str) -> dict | None:
    return await api_request("GET", f"/messages/{msg_id}", token=token)


async def delete_account(token: str, account_id: str) -> None:
    await api_request("DELETE", f"/accounts/{account_id}", token=token)


# ============================================================
# Session helpers
# ============================================================

async def create_session(user_id: int) -> dict | None:
    async with user_locks[user_id]:
        old = user_sessions.get(user_id)
        if old:
            asyncio.create_task(delete_account(old["token"], old["account_id"]))

        data = await create_account()
        if data:
            user_sessions[user_id] = {
                "email":      data["email"],
                "token":      data["token"],
                "account_id": data["account_id"],
                "seen_ids":   set(),
                "auto_check": False
            }
        return data


def get_session(user_id: int) -> dict | None:
    return user_sessions.get(user_id)


# ============================================================
# Keyboards
# ============================================================

REPLY_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("📧 New Email"), KeyboardButton("🔄 Refresh Inbox")]],
    resize_keyboard=True,
    is_persistent=True
)

def main_inline_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📬 Inbox",       callback_data="check_inbox"),
            InlineKeyboardButton("📋 Copy Email",  callback_data="copy_email"),
        ],
        [
            InlineKeyboardButton("🆕 New Email",   callback_data="new_email"),
            InlineKeyboardButton("⚡ Auto-Check",  callback_data="auto_check"),
        ]
    ])


# ============================================================
# /start
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id    = update.effective_user.id
    first_name = update.effective_user.first_name
    all_users.add(user_id)

    msg = await update.message.reply_text(
        "⏳ Creating your email address, please wait...",
        reply_markup=REPLY_KB
    )

    data = await create_session(user_id)
    if not data:
        await msg.edit_text("❌ Could not create email. Please try /start again.")
        return

    await msg.edit_text(
        f"👋 Welcome, {first_name}!\n\n"
        f"⚡ *Temp Mail Bot*\n\n"
        f"📧 Your new email address:\n`{data['email']}`\n\n"
        f"_(Tap the email above to copy it)_\n\n"
        f"Use the buttons below 👇",
        parse_mode="Markdown"
    )
    await update.message.reply_text("Menu:", reply_markup=main_inline_kb())


# ============================================================
# Reply Keyboard handler
# ============================================================

async def reply_kb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    all_users.add(user_id)
    text    = update.message.text.strip()

    if text == "📧 New Email":
        wait_msg = await update.message.reply_text("⏳ Creating new email...")
        data = await create_session(user_id)
        if data:
            await wait_msg.edit_text(
                f"✅ New email created!\n\n"
                f"📧 `{data['email']}`\n\n"
                f"Old email deleted. Use this address anywhere.",
                parse_mode="Markdown"
            )
        else:
            await wait_msg.edit_text("❌ Failed to create email. Please try again.")
        await update.message.reply_text("Menu:", reply_markup=main_inline_kb())

    elif text == "🔄 Refresh Inbox":
        session = get_session(user_id)
        if not session:
            await update.message.reply_text("❌ Please type /start first.")
            return

        msgs = await get_messages(session["token"])
        if not msgs:
            await update.message.reply_text(
                f"📭 Inbox is empty!\n\n📧 `{session['email']}`\n\nNo emails yet.",
                parse_mode="Markdown"
            )
            return

        text_out = f"📬 *{len(msgs)} email(s) found*\n\n📧 `{session['email']}`\n\n"
        buttons  = []
        for m in msgs[:10]:
            subject = m.get("subject", "No Subject")[:40]
            sender  = m.get("from", {}).get("address", "Unknown")[:30]
            msg_id  = m.get("id", "")
            text_out += f"📩 *{subject}*\n   👤 {sender}\n\n"
            buttons.append([InlineKeyboardButton(
                f"📖 Read: {subject[:28]}", callback_data=f"read_{msg_id}"
            )])
        buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data="check_inbox")])
        await update.message.reply_text(text_out, parse_mode="Markdown")
        await update.message.reply_text("Inbox:", reply_markup=InlineKeyboardMarkup(buttons))


# ============================================================
# Inline button handler
# ============================================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    cb_data = query.data

    session = get_session(user_id)
    if not session:
        await query.edit_message_text("❌ Please type /start to begin.")
        return

    # ── Inbox ──
    if cb_data == "check_inbox":
        msgs = await get_messages(session["token"])
        if not msgs:
            await query.edit_message_text(
                f"📭 Inbox is empty!\n\n📧 `{session['email']}`",
                parse_mode="Markdown",
                reply_markup=main_inline_kb()
            )
        else:
            text    = f"📬 *{len(msgs)} email(s)*\n\n📧 `{session['email']}`\n\n"
            buttons = []
            for m in msgs[:10]:
                subject = m.get("subject", "No Subject")[:40]
                sender  = m.get("from", {}).get("address", "Unknown")[:30]
                msg_id  = m.get("id", "")
                text += f"📩 *{subject}*\n   👤 {sender}\n\n"
                buttons.append([InlineKeyboardButton(
                    f"📖 Read: {subject[:28]}", callback_data=f"read_{msg_id}"
                )])
            buttons.append([
                InlineKeyboardButton("🔙 Back",    callback_data="back_main"),
                InlineKeyboardButton("🔄 Refresh", callback_data="check_inbox")
            ])
            await query.edit_message_text(
                text, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(buttons)
            )

    # ── Read email ──
    elif cb_data.startswith("read_"):
        msg_id   = cb_data.replace("read_", "")
        msg_data = await get_message(session["token"], msg_id)
        if msg_data:
            subject = msg_data.get("subject", "No Subject")
            sender  = msg_data.get("from", {}).get("address", "Unknown")
            body    = msg_data.get("text", "")
            if not body:
                html_list = msg_data.get("html", [])
                body = html_list[0] if isinstance(html_list, list) and html_list else ""
            body_clean = re.sub(r'<[^>]+>', '', body).strip()
            body_clean = body_clean[:1500] + ("..." if len(body_clean) > 1500 else "")
            await query.edit_message_text(
                f"📩 *{subject}*\n👤 From: `{sender}`\n\n─────────────────\n{body_clean}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Inbox", callback_data="check_inbox")
                ]])
            )
        else:
            await query.answer("❌ Could not load email.", show_alert=True)

    # ── Copy email ──
    elif cb_data == "copy_email":
        await query.answer(f"✅ Copied: {session['email']}", show_alert=True)

    # ── New email ──
    elif cb_data == "new_email":
        await query.edit_message_text("⏳ Creating new email...")
        data = await create_session(user_id)
        if data:
            await query.edit_message_text(
                f"✅ New email created!\n\n📧 `{data['email']}`\n\nOld email deleted. Ready to use!",
                parse_mode="Markdown",
                reply_markup=main_inline_kb()
            )
        else:
            await query.edit_message_text(
                "❌ Failed. Please try again.",
                reply_markup=main_inline_kb()
            )

    # ── Auto-Check toggle ──
    elif cb_data == "auto_check":
        # Safe job_queue check
        if context.job_queue is None:
            await query.answer("⚠️ Auto-Check not available on this server.", show_alert=True)
            return
        async with user_locks[user_id]:
            session["auto_check"] = not session.get("auto_check", False)
        status = "✅ ON" if session["auto_check"] else "❌ OFF"
        await query.answer(f"Auto-Check {status}", show_alert=True)
        if session["auto_check"]:
            for job in context.job_queue.get_jobs_by_name(f"auto_{user_id}"):
                job.schedule_removal()
            context.job_queue.run_repeating(
                auto_check_job, interval=AUTO_INTERVAL, first=5,
                data={"user_id": user_id, "chat_id": query.message.chat_id},
                name=f"auto_{user_id}"
            )

    # ── Back ──
    elif cb_data == "back_main":
        await query.edit_message_text(
            f"📧 Your email:\n`{session['email']}`\n\nMenu 👇",
            parse_mode="Markdown",
            reply_markup=main_inline_kb()
        )


# ============================================================
# Auto-check job
# ============================================================

async def auto_check_job(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    user_id  = job_data["user_id"]
    chat_id  = job_data["chat_id"]

    session = get_session(user_id)
    if not session or not session.get("auto_check", False):
        context.job.schedule_removal()
        return

    msgs     = await get_messages(session["token"])
    new_msgs = [m for m in msgs if m.get("id") not in session["seen_ids"]]
    if new_msgs:
        for m in new_msgs:
            session["seen_ids"].add(m.get("id"))
        tasks = [
            context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🔔 *New email received!*\n\n"
                    f"📩 *{m.get('subject', 'No Subject')}*\n"
                    f"👤 From: `{m.get('from', {}).get('address', 'Unknown')}`"
                ),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📖 Read", callback_data=f"read_{m.get('id', '')}")
                ]])
            )
            for m in new_msgs
        ]
        await asyncio.gather(*tasks, return_exceptions=True)


# ============================================================
# BROADCAST (Admin only)
# ============================================================

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ You are not an admin!")
        return
    if not context.args:
        await update.message.reply_text(
            "📢 *Usage:*\n`/broadcast your message here`",
            parse_mode="Markdown"
        )
        return

    msg_text   = " ".join(context.args)
    user_list  = list(all_users)
    status_msg = await update.message.reply_text(f"📤 Sending to {len(user_list)} users...")
    sem = asyncio.Semaphore(30)

    async def send_one(uid):
        async with sem:
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=f"📢 *Message from Admin:*\n\n{msg_text}",
                    parse_mode="Markdown"
                )
                return True
            except Exception:
                return False

    results = await asyncio.gather(*[send_one(uid) for uid in user_list])
    success  = sum(results)
    await status_msg.edit_text(
        f"✅ Broadcast complete!\n\n✔️ Sent: {success}\n❌ Failed: {len(results)-success}\n👥 Total: {len(user_list)}"
    )


async def users_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ You are not an admin!")
        return
    await update.message.reply_text(f"👥 Total users: *{len(all_users)}*", parse_mode="Markdown")


# ============================================================
# /help
# ============================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    all_users.add(update.effective_user.id)
    await update.message.reply_text(
        "🤖 *Temp Mail Bot — Help*\n\n"
        "*/start* — Create a new temp email\n"
        "*/help* — Show this help\n\n"
        "*Bottom buttons (always visible):*\n"
        "📧 New Email — Delete old & get new instantly\n"
        "🔄 Refresh Inbox — Check for new emails\n\n"
        "*Inline buttons:*\n"
        "📬 Inbox — View all emails\n"
        "📋 Copy Email — Copy your address\n"
        "🆕 New Email — Delete old & create new\n"
        "⚡ Auto-Check — Get notified every 10 sec\n\n"
        "*Admin Commands:*\n"
        "`/broadcast [message]` — Send to all users\n"
        "`/users` — Show total user count\n\n"
        "⚡ Powered by mail.tm + async engine 🚀",
        parse_mode="Markdown",
        reply_markup=REPLY_KB
    )


# ============================================================
# Error handler
# ============================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception: {context.error}", exc_info=context.error)


# ============================================================
# Startup / Shutdown
# ============================================================

async def on_startup(app):
    await get_http_session()
    logger.info("✅ HTTP connection pool ready")


async def on_shutdown(app):
    global http_session
    if http_session and not http_session.closed:
        await http_session.close()
    logger.info("🔴 HTTP session closed")


# ============================================================
# Main — IMPORTANT: .job_queue() enables job_queue
# ============================================================

def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start",     start))
    app.add_handler(CommandHandler("help",      help_command))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("users",     users_count))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex("^(📧 New Email|🔄 Refresh Inbox)$"),
        reply_kb_handler
    ))

    logger.info("✅ Bot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
