"""
Telegram Temp Mail Bot - Guerrilla Mail API
==========================================
Production-grade async bot for 10k+ concurrent users

Setup:
1. pip install python-telegram-bot aiohttp
2. BOT_TOKEN = get from @BotFather
3. ADMIN_ID  = your Telegram user ID
4. python tempmail_bot.py
"""

import logging
import re
import asyncio
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
BOT_TOKEN     = "8577440207:AAFjbJLScFEx1tPOs6WlHIFnLnPDyNWkW6o"
ADMIN_ID      = 7540185501
GUERRILLA_API = "https://api.guerrillamail.com/ajax.php"
MAX_RETRIES   = 3
POOL_SIZE     = 100
AUTO_INTERVAL = 10
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
            limit_per_host=POOL_SIZE,
            ttl_dns_cache=300,
            enable_cleanup_closed=True
        )
        timeout = aiohttp.ClientTimeout(total=10, connect=3)
        http_session = aiohttp.ClientSession(connector=connector, timeout=timeout)
    return http_session


async def api_get(params: dict, retries: int = MAX_RETRIES) -> dict | None:
    session = await get_http_session()
    for attempt in range(retries):
        try:
            async with session.get(GUERRILLA_API, params=params) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
                logger.warning(f"API status {resp.status}, attempt {attempt+1}")
        except asyncio.TimeoutError:
            logger.warning(f"Timeout on attempt {attempt+1}")
        except aiohttp.ClientError as e:
            logger.warning(f"Client error on attempt {attempt+1}: {e}")
        if attempt < retries - 1:
            await asyncio.sleep(0.3 * (attempt + 1))
    return None


# ============================================================
# Guerrilla Mail API
# ============================================================

async def async_get_email() -> dict | None:
    data = await api_get({"f": "get_email_address", "lang": "en"})
    if data:
        return {"email": data.get("email_addr", ""), "sid_token": data.get("sid_token", "")}
    return None


async def async_check_email(sid_token: str, seq: int = 0) -> list:
    data = await api_get({"f": "check_email", "sid_token": sid_token, "seq": seq})
    return data.get("list", []) if data else []


async def async_fetch_email(mail_id: str, sid_token: str) -> dict | None:
    return await api_get({"f": "fetch_email", "email_id": mail_id, "sid_token": sid_token})


async def async_forget_me(sid_token: str, email_addr: str) -> None:
    await api_get({"f": "forget_me", "sid_token": sid_token, "email_addr": email_addr})


# ============================================================
# Session helpers
# ============================================================

async def create_session(user_id: int) -> dict | None:
    """Delete old email first (if exists), then create new one instantly"""
    async with user_locks[user_id]:
        # Delete old session silently before creating new
        old = user_sessions.get(user_id)
        if old:
            asyncio.create_task(async_forget_me(old["sid_token"], old["email"]))

        data = await async_get_email()
        if data:
            user_sessions[user_id] = {
                "sid_token":  data["sid_token"],
                "email":      data["email"],
                "seq":        0,
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
            InlineKeyboardButton("📬 Inbox",        callback_data="check_inbox"),
            InlineKeyboardButton("📋 Copy Email",   callback_data="copy_email"),
        ],
        [
            InlineKeyboardButton("🆕 New Email",    callback_data="new_email"),
            InlineKeyboardButton("⚡ Auto-Check",   callback_data="auto_check"),
        ]
    ])


# ============================================================
# /start
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id    = update.effective_user.id
    first_name = update.effective_user.first_name
    all_users.add(user_id)

    data = await create_session(user_id)
    if not data:
        await update.message.reply_text("❌ API error! Please try again later.")
        return

    await update.message.reply_text(
        f"👋 Welcome, {first_name}!\n\n"
        f"⚡ *Temp Mail Bot*\n\n"
        f"📧 Your new email address:\n`{data['email']}`\n\n"
        f"_(Tap the email above to copy it)_\n\n"
        f"Use the buttons below 👇",
        parse_mode="Markdown",
        reply_markup=REPLY_KB
    )
    await update.message.reply_text("Menu:", reply_markup=main_inline_kb())


# ============================================================
# Reply Keyboard handler
# ============================================================

async def reply_kb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    all_users.add(user_id)
    text = update.message.text.strip()

    if text == "📧 New Email":
        # Delete old + create new in under 1 second
        data = await create_session(user_id)
        if data:
            msg = (
                f"✅ New email created!\n\n"
                f"📧 `{data['email']}`\n\n"
                f"Old email has been deleted. Use this address anywhere."
            )
        else:
            msg = "❌ Failed to create email. Please try again."
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=REPLY_KB)
        await update.message.reply_text("Menu:", reply_markup=main_inline_kb())

    elif text == "🔄 Refresh Inbox":
        session = get_session(user_id)
        if not session:
            await create_session(user_id)
            session = get_session(user_id)
        if not session:
            await update.message.reply_text("❌ Please type /start first.")
            return

        emails = await async_check_email(session["sid_token"], session["seq"])
        if not emails:
            await update.message.reply_text(
                f"📭 Inbox is empty!\n\n📧 `{session['email']}`\n\nNo emails yet.",
                parse_mode="Markdown",
                reply_markup=REPLY_KB
            )
            return

        text_out = f"📬 *{len(emails)} email(s) found*\n\n📧 `{session['email']}`\n\n"
        buttons  = []
        for mail in emails[:10]:
            subject = mail.get("mail_subject", "No Subject")[:40]
            sender  = mail.get("mail_from", "Unknown")[:30]
            mail_id = mail.get("mail_id", "")
            text_out += f"📩 *{subject}*\n   👤 {sender}\n\n"
            buttons.append([InlineKeyboardButton(f"📖 Read: {subject[:28]}", callback_data=f"read_{mail_id}")])
        buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data="check_inbox")])

        await update.message.reply_text(text_out, parse_mode="Markdown", reply_markup=REPLY_KB)
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
        data = await create_session(user_id)
        if not data:
            await query.edit_message_text("❌ Please type /start to begin.")
            return
        session = get_session(user_id)

    # ── Inbox ──
    if cb_data == "check_inbox":
        emails = await async_check_email(session["sid_token"], session["seq"])
        if not emails:
            await query.edit_message_text(
                f"📭 Inbox is empty!\n\n📧 `{session['email']}`",
                parse_mode="Markdown",
                reply_markup=main_inline_kb()
            )
        else:
            text    = f"📬 *{len(emails)} email(s)*\n\n📧 `{session['email']}`\n\n"
            buttons = []
            for mail in emails[:10]:
                subject = mail.get("mail_subject", "No Subject")[:40]
                sender  = mail.get("mail_from", "Unknown")[:30]
                mail_id = mail.get("mail_id", "")
                text += f"📩 *{subject}*\n   👤 {sender}\n\n"
                buttons.append([InlineKeyboardButton(f"📖 Read: {subject[:28]}", callback_data=f"read_{mail_id}")])
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
        mail_id   = cb_data.replace("read_", "")
        mail_data = await async_fetch_email(mail_id, session["sid_token"])
        if mail_data:
            subject    = mail_data.get("mail_subject", "No Subject")
            sender     = mail_data.get("mail_from", "Unknown")
            body       = mail_data.get("mail_body", "")
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
            await query.answer("❌ Could not load email. Try again.", show_alert=True)

    # ── Copy email ──
    elif cb_data == "copy_email":
        await query.answer(f"✅ Copied: {session['email']}", show_alert=True)

    # ── New email (delete old + create new instantly) ──
    elif cb_data == "new_email":
        data = await create_session(user_id)
        if data:
            await query.edit_message_text(
                f"✅ New email created!\n\n"
                f"📧 `{data['email']}`\n\n"
                f"Old email deleted. Ready to use!",
                parse_mode="Markdown",
                reply_markup=main_inline_kb()
            )
        else:
            await query.answer("❌ Failed. Please try again.", show_alert=True)

    # ── Auto-Check toggle ──
    elif cb_data == "auto_check":
        async with user_locks[user_id]:
            session["auto_check"] = not session.get("auto_check", False)
        status = "✅ ON" if session["auto_check"] else "❌ OFF"
        await query.answer(f"Auto-Check {status}", show_alert=True)
        if session["auto_check"]:
            for job in context.job_queue.get_jobs_by_name(f"auto_{user_id}"):
                job.schedule_removal()
            context.job_queue.run_repeating(
                auto_check_job,
                interval=AUTO_INTERVAL,
                first=5,
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

    emails = await async_check_email(session["sid_token"], session["seq"])
    if emails:
        async with user_locks[user_id]:
            session["seq"] += len(emails)

        tasks = [
            context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🔔 *New email received!*\n\n"
                    f"📩 *{mail.get('mail_subject', 'No Subject')}*\n"
                    f"👤 From: `{mail.get('mail_from', 'Unknown')}`"
                ),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📖 Read", callback_data=f"read_{mail.get('mail_id', '')}")
                ]])
            )
            for mail in emails
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
    status_msg = await update.message.reply_text(
        f"📤 Sending broadcast to {len(user_list)} users..."
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
    failed   = len(results) - success

    await status_msg.edit_text(
        f"✅ Broadcast complete!\n\n"
        f"✔️ Sent: {success}\n"
        f"❌ Failed: {failed}\n"
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
        "⚡ Async engine — Supports 10k+ users 🚀",
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
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex("^(📧 New Email|🔄 Refresh Inbox)$"),
        reply_kb_handler
    ))

    logger.info("✅ Bot started! Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
