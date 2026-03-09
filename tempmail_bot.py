"""
Telegram Temp Mail Bot - mail.tm API
=====================================
- No inline buttons (clean UI)
- Tap email to copy instantly
- Always-on auto scan (no toggle needed)
- New email = old deleted instantly

Setup:
1. pip install python-telegram-bot aiohttp
2. Set env vars: BOT_TOKEN, ADMIN_ID
3. python tempmail_bot.py
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
    Update, ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    MessageHandler, filters, ContextTypes
)

# ===================== CONFIG =====================
BOT_TOKEN     = os.environ.get("BOT_TOKEN", "8577440207:AAFjbJLScFEx1tPOs6WlHIFnLnPDyNWkW6o")
ADMIN_ID      = int(os.environ.get("7540185501", "0"))
MAIL_TM_API   = "https://api.mail.tm"
MAX_RETRIES   = 3
POOL_SIZE     = 100
AUTO_INTERVAL = 10   # check every 10 seconds
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
        timeout = aiohttp.ClientTimeout(total=15, connect=5)
        http_session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={"Content-Type": "application/json"}
        )
    return http_session


async def api_request(method: str, endpoint: str, json_data: dict = None,
                      token: str = None, retries: int = MAX_RETRIES) -> dict | None:
    session = await get_http_session()
    url     = f"{MAIL_TM_API}{endpoint}"
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    for attempt in range(retries):
        try:
            if method == "GET":
                async with session.get(url, headers=headers) as resp:
                    if resp.status in (200, 201):
                        return await resp.json()
            elif method == "POST":
                async with session.post(url, json=json_data, headers=headers) as resp:
                    if resp.status in (200, 201):
                        return await resp.json()
            elif method == "DELETE":
                async with session.delete(url, headers=headers) as resp:
                    return {"ok": resp.status in (200, 204)}
        except asyncio.TimeoutError:
            logger.warning(f"Timeout attempt {attempt+1} for {endpoint}")
        except aiohttp.ClientError as e:
            logger.warning(f"Client error attempt {attempt+1}: {e}")
        if attempt < retries - 1:
            await asyncio.sleep(0.5 * (attempt + 1))
    return None


# ============================================================
# mail.tm API
# ============================================================

def random_string(length=10) -> str:
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))


async def get_domains() -> str | None:
    data = await api_request("GET", "/domains")
    if data and "hydra:member" in data and data["hydra:member"]:
        return data["hydra:member"][0]["domain"]
    return None


async def create_account() -> dict | None:
    domain = await get_domains()
    if not domain:
        return None

    username = random_string(10)
    password = random_string(12)
    email    = f"{username}@{domain}"

    reg = await api_request("POST", "/accounts", {"address": email, "password": password})
    if not reg:
        return None

    auth = await api_request("POST", "/token", {"address": email, "password": password})
    if not auth or "token" not in auth:
        return None

    return {
        "email":      email,
        "password":   password,
        "account_id": reg.get("id", ""),
        "token":      auth["token"]
    }


async def get_messages(token: str) -> list:
    data = await api_request("GET", "/messages", token=token)
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
                "chat_id":    None
            }
        return data


def get_session(user_id: int) -> dict | None:
    return user_sessions.get(user_id)


# ============================================================
# Keyboard (only 2 bottom buttons, no inline)
# ============================================================

REPLY_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("📧 New Email"), KeyboardButton("🔄 Refresh Inbox")]],
    resize_keyboard=True,
    is_persistent=True
)


# ============================================================
# /start
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id    = update.effective_user.id
    first_name = update.effective_user.first_name
    all_users.add(user_id)

    await update.message.reply_text("⏳ Creating your email address...")
    data = await create_session(user_id)
    if not data:
        await update.message.reply_text("❌ API error! Please try /start again.")
        return

    # Save chat_id for auto-scan notifications
    user_sessions[user_id]["chat_id"] = update.effective_chat.id

    # Start always-on auto scan
    _start_auto_scan(user_id, update.effective_chat.id, context)

    await update.message.reply_text(
        f"👋 Welcome, {first_name}!\n\n"
        f"⚡ *Temp Mail Bot*\n\n"
        f"📧 Your email address:\n`{data['email']}`\n\n"
        f"_(Tap the email above to copy it)_\n\n"
        f"🔔 Auto-scan is *always ON* — you'll be notified instantly when an email arrives!",
        parse_mode="Markdown",
        reply_markup=REPLY_KB
    )


# ============================================================
# Reply Keyboard handler
# ============================================================

async def reply_kb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    all_users.add(user_id)
    text    = update.message.text.strip()

    if text == "📧 New Email":
        await update.message.reply_text("⏳ Creating new email...")
        data = await create_session(user_id)
        if data:
            user_sessions[user_id]["chat_id"] = update.effective_chat.id
            # Restart auto-scan for new email
            _start_auto_scan(user_id, update.effective_chat.id, context)
            await update.message.reply_text(
                f"✅ New email created!\n\n"
                f"📧 `{data['email']}`\n\n"
                f"Old email deleted.\n"
                f"🔔 Auto-scan restarted for new address!",
                parse_mode="Markdown",
                reply_markup=REPLY_KB
            )
        else:
            await update.message.reply_text(
                "❌ Failed to create email. Please try again.",
                reply_markup=REPLY_KB
            )

    elif text == "🔄 Refresh Inbox":
        session = get_session(user_id)
        if not session:
            await update.message.reply_text("❌ Please type /start first.")
            return

        msgs = await get_messages(session["token"])
        if not msgs:
            await update.message.reply_text(
                f"📭 *Inbox is empty*\n\n"
                f"📧 `{session['email']}`\n\n"
                f"No emails yet. Auto-scan is running 🔔",
                parse_mode="Markdown",
                reply_markup=REPLY_KB
            )
            return

        text_out = f"📬 *{len(msgs)} email(s) in inbox*\n\n📧 `{session['email']}`\n\n"
        for msg in msgs[:10]:
            subject = msg.get("subject", "No Subject")[:50]
            sender  = msg.get("from", {}).get("address", "Unknown")[:40]
            intro   = msg.get("intro", "")[:80]
            text_out += f"📩 *{subject}*\n👤 {sender}\n_{intro}_\n\n"

        await update.message.reply_text(
            text_out,
            parse_mode="Markdown",
            reply_markup=REPLY_KB
        )


# ============================================================
# Always-on auto scan
# ============================================================

def _start_auto_scan(user_id: int, chat_id: int, context):
    """Remove old job and start fresh auto-scan for this user"""
    for job in context.job_queue.get_jobs_by_name(f"scan_{user_id}"):
        job.schedule_removal()
    context.job_queue.run_repeating(
        auto_scan_job,
        interval=AUTO_INTERVAL,
        first=5,
        data={"user_id": user_id, "chat_id": chat_id},
        name=f"scan_{user_id}"
    )


async def auto_scan_job(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    user_id  = job_data["user_id"]
    chat_id  = job_data["chat_id"]

    session = get_session(user_id)
    if not session:
        context.job.schedule_removal()
        return

    try:
        msgs     = await get_messages(session["token"])
        new_msgs = [m for m in msgs if m.get("id") not in session["seen_ids"]]

        if new_msgs:
            for m in new_msgs:
                session["seen_ids"].add(m.get("id"))

            # Fetch full content for each new email
            tasks = []
            for m in new_msgs:
                tasks.append(_send_email_notification(context, chat_id, session["token"], m))
            await asyncio.gather(*tasks, return_exceptions=True)

    except Exception as e:
        logger.warning(f"Auto-scan error for user {user_id}: {e}")


async def _send_email_notification(context, chat_id: int, token: str, mail_summary: dict):
    """Fetch full email and send to user"""
    msg_id   = mail_summary.get("id", "")
    subject  = mail_summary.get("subject", "No Subject")
    sender   = mail_summary.get("from", {}).get("address", "Unknown")

    # Fetch full body
    full = await get_message(token, msg_id)
    if full:
        body = full.get("text", "") or ""
        if not body and full.get("html"):
            html = full["html"]
            body = re.sub(r'<[^>]+>', '', html[0] if isinstance(html, list) else html).strip()
        body = body[:2000] + ("..." if len(body) > 2000 else "")
    else:
        body = "(Could not load email body)"

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🔔 *New Email Received!*\n\n"
            f"📩 *Subject:* {subject}\n"
            f"👤 *From:* `{sender}`\n\n"
            f"─────────────────\n"
            f"{body}"
        ),
        parse_mode="Markdown"
    )


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
    status_msg = await update.message.reply_text(
        f"📤 Sending to {len(user_list)} users..."
    )

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
        f"✅ Broadcast complete!\n\n"
        f"✔️ Sent: {success}\n"
        f"❌ Failed: {len(results) - success}\n"
        f"👥 Total: {len(user_list)}"
    )


async def users_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ You are not an admin!")
        return
    await update.message.reply_text(
        f"👥 Total users: *{len(all_users)}*",
        parse_mode="Markdown"
    )


# ============================================================
# /help
# ============================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    all_users.add(update.effective_user.id)
    await update.message.reply_text(
        "🤖 *Temp Mail Bot — Help*\n\n"
        "*/start* — Create a new temp email\n"
        "*/help* — Show this help\n\n"
        "*Buttons:*\n"
        "📧 New Email — Delete old & get new instantly\n"
        "🔄 Refresh Inbox — View current emails\n\n"
        "🔔 *Auto-scan is always ON* — no setup needed!\n"
        "You'll get notified the moment an email arrives.\n\n"
        "*Admin:*\n"
        "`/broadcast [msg]` — Message all users\n"
        "`/users` — Total user count\n\n"
        "⚡ Powered by mail.tm + async engine 🚀",
        parse_mode="Markdown",
        reply_markup=REPLY_KB
    )


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
# Main
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

    app.add_handler(CommandHandler("start",     start))
    app.add_handler(CommandHandler("help",      help_command))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("users",     users_count))
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex("^(📧 New Email|🔄 Refresh Inbox)$"),
        reply_kb_handler
    ))

    logger.info("✅ Bot started! Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
