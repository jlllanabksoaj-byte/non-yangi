# -*- coding: utf-8 -*-
"""
Sotuv agentlari, omborchilar va Admin uchun Telegram bot.
- Admin: Barcha jarayonlarni kuzatadi, umumiy hisobotlarni ko'radi, operatsiyalardan instant xabar oladi.
- Omborchi: Agentlarga mahsulot yuklaydi.
- Agent: Mijozga mahsulot topshiradi (rasm, joylashuv, miqdor).
"""

import asyncio
import logging
import os
import sqlite3
from datetime import datetime

try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ---------------- SOZLAMALAR ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

PRODUCTS = [
    "Non1",
    "Non2 (angar)",
    "Non3",
    "Non4 (tie rod)",
    "Non5",
]

# Rollar (Telegram user_id laringizni kiriting)
ADMIN_IDS = {999999999}       # Admin(lar) ID
OMBORCHI_IDS = {766842087, 222222222}  # Omborchi ID
AGENT_IDS = {7010796701, 777777777}    # Agent ID

DB_PATH = "sales_bot.db"

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
SERVICE_ACCOUNT_JSON = "service_account.json"

# ConversationHandler bosqichlari
(
    LOAD_PRODUCT, LOAD_AGENT, LOAD_QTY,
    DELIVER_PRODUCT, DELIVER_QTY, DELIVER_CUSTOMER, DELIVER_LOCATION, DELIVER_PHOTO,
) = range(8)


# ---------------- BAZA AMALLARI ----------------
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS loads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                omborchi_id INTEGER NOT NULL,
                agent_id INTEGER NOT NULL,
                product TEXT NOT NULL,
                qty INTEGER NOT NULL,
                ts TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id INTEGER NOT NULL,
                product TEXT NOT NULL,
                qty INTEGER NOT NULL,
                customer TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                photo_file_id TEXT NOT NULL,
                ts TEXT NOT NULL
            )
        """)
        conn.commit()


def get_agent_balance(agent_id: int, product: str) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COALESCE(SUM(qty), 0) FROM loads WHERE agent_id=? AND product=?",
            (agent_id, product),
        )
        loaded = cursor.fetchone()[0]

        cursor.execute(
            "SELECT COALESCE(SUM(qty), 0) FROM deliveries WHERE agent_id=? AND product=?",
            (agent_id, product),
        )
        delivered = cursor.fetchone()[0]

    return loaded - delivered


# ---------------- GOOGLE SHEETS ASYNC ----------------
def _sync_to_sheet_sync(tab_name: str, row_data: list):
    if not GSPREAD_AVAILABLE or not GOOGLE_SHEET_ID:
        return
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_JSON, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(GOOGLE_SHEET_ID).worksheet(tab_name)
        sheet.append_row(row_data)
    except Exception as e:
        log.error(f"Google Sheets ({tab_name}) xatosi: {e}")


async def sync_to_sheet(tab_name: str, row_data: list):
    await asyncio.to_thread(_sync_to_sheet_sync, tab_name, row_data)


# ---------------- MENYULAR (KEYBOARDS) ----------------
def main_menu_keyboard(uid: int) -> ReplyKeyboardMarkup:
    """Foydalanuvchi rolidan kelib chiqib asosiy menyu tugmalarini beradi."""
    rows = []
    if uid in ADMIN_IDS:
        rows = [
            ["📊 Agentlar Qoldig'i", "📜 Oxirgi Topshirishlar"],
            ["📦 Oxirgi Yuklanmalar", "📈 Umumiy Hisobot"]
        ]
    elif uid in OMBORCHI_IDS:
        rows = [["📦 Agentga yuklash"], ["📊 Qoldiqlarni ko'rish"]]
    elif uid in AGENT_IDS:
        rows = [["🚚 Mijozga topshirish"], ["📊 Mening qoldig'im"]]

    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def product_inline_keyboard() -> InlineKeyboardMarkup:
    """Mahsulotlarni Inline tugma shaklida chiqaradi."""
    buttons = [[InlineKeyboardButton(p, callback_data=f"prod_{p}")] for p in PRODUCTS]
    buttons.append([InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel_action")])
    return InlineKeyboardMarkup(buttons)


def agent_inline_keyboard() -> InlineKeyboardMarkup:
    """Agentlarni tanlash uchun Inline tugmalar."""
    buttons = [[InlineKeyboardButton(f"Agent ID: {a}", callback_data=f"agent_{a}")] for a in AGENT_IDS]
    buttons.append([InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel_action")])
    return InlineKeyboardMarkup(buttons)


# ---------------- ADMIN UCHUN XABAR YUBORISH ----------------
async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str, photo_id: str = None, lat: float = None, lon: float = None):
    """Barcha adminlarga hodisalar bo'yicha bildirishnoma yuboradi."""
    for admin_id in ADMIN_IDS:
        try:
            if photo_id:
                await context.bot.send_photo(chat_id=admin_id, photo=photo_id, caption=text, parse_mode="Markdown")
            else:
                await context.bot.send_message(chat_id=admin_id, text=text, parse_mode="Markdown")

            if lat and lon:
                await context.bot.send_location(chat_id=admin_id, latitude=lat, longitude=lon)
        except Exception as e:
            log.warning(f"Admin ({admin_id})ga bildirishnoma boramadi: {e}")


# ---------------- HANDLERLAR ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if uid in ADMIN_IDS:
        text = "👑 *Admin panelga xush kelibsiz!*\nQuyidagi menyudan foydalanib jarayonlarni kuzatishingiz mumkin:"
    elif uid in OMBORCHI_IDS:
        text = "🏭 *Omborchi paneli:*\nAgentlarga mahsulot yuklash uchun menyudan foydalaning."
    elif uid in AGENT_IDS:
        text = "🚚 *Agent paneli:*\nMijozlarga topshirish va qoldiqni ko'rish menyusi."
    else:
        text = f"🚫 *Kirish taqiqlandi!*\nSizning ID: `{uid}`\nAdministrator bilan bog'laning."
        await update.message.reply_text(text, parse_mode="Markdown")
        return

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard(uid))


# --- ADMIN FUNKSIYALARI ---
async def admin_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS:
        return

    txt = update.message.text

    if txt == "📊 Agentlar Qoldig'i":
        report = "📊 *Hozirgi agentlar qoldig'i:*\n\n"
        for agent in AGENT_IDS:
            report += f"👤 *Agent ID: {agent}*\n"
            for p in PRODUCTS:
                bal = get_agent_balance(agent, p)
                report += f"  • {p}: `{bal}` dona\n"
            report += "\n"
        await update.message.reply_text(report, parse_mode="Markdown")

    elif txt == "📜 Oxirgi Topshirishlar":
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT agent_id, product, qty, customer, ts FROM deliveries ORDER BY id DESC LIMIT 5")
            rows = cursor.fetchall()

        if not rows:
            await update.message.reply_text("Topshirishlar tarixi bo'sh.")
            return

        msg = "📜 *Oxirgi 5 ta topshiruv:*\n\n"
        for r in rows:
            msg += f"🗓 `{r[4]}` | Agent: `{r[0]}`\n📦 {r[1]} - {r[2]} dona -> *{r[3]}*\n---\n"
        await update.message.reply_text(msg, parse_mode="Markdown")

    elif txt == "📦 Oxirgi Yuklanmalar":
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT omborchi_id, agent_id, product, qty, ts FROM loads ORDER BY id DESC LIMIT 5")
            rows = cursor.fetchall()

        if not rows:
            await update.message.reply_text("Yuklanmalar tarixi bo'sh.")
            return

        msg = "📦 *Oxirgi 5 ta ombor yuklovi:*\n\n"
        for r in rows:
            msg += f"🗓 `{r[4]}`\nOmborchi: `{r[0]}` -> Agent: `{r[1]}`\n📦 {r[2]} - {r[3]} dona\n---\n"
        await update.message.reply_text(msg, parse_mode="Markdown")


# --- AGENT QOLDIG'I ---
async def agent_qoldiq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in AGENT_IDS and uid not in OMBORCHI_IDS:
        return

    lines = [f"• *{p}*: `{get_agent_balance(uid, p)}` dona" for p in PRODUCTS]
    await update.message.reply_text("📊 *Sizning qoldig'ingiz:*\n\n" + "\n".join(lines), parse_mode="Markdown")


# --- CONVERSATION: OMBORCHI YUKLASHI ---
async def yukla_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in OMBORCHI_IDS:
        return ConversationHandler.END

    await update.message.reply_text("📦 *Qaysi mahsulotni yuklaysiz?*", parse_mode="Markdown", reply_markup=product_inline_keyboard())
    return LOAD_PRODUCT


async def yukla_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_action":
        await query.edit_message_text("Jarayon bekor qilindi.")
        return ConversationHandler.END

    product = query.data.replace("prod_", "")
    context.user_data["product"] = product

    await query.edit_message_text(
        f"Tanlandi: *{product}*\n\n👤 *Qaysi agentga yuklaysiz?*",
        parse_mode="Markdown",
        reply_markup=agent_inline_keyboard()
    )
    return LOAD_AGENT


async def yukla_agent_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_action":
        await query.edit_message_text("Jarayon bekor qilindi.")
        return ConversationHandler.END

    agent_id = int(query.data.replace("agent_", ""))
    context.user_data["agent_id"] = agent_id

    await query.edit_message_text(
        f"Agent ID: `{agent_id}`\n\n🔢 *Necha dona yuklamoqchisiz?* (Faqat raqam kiriting):",
        parse_mode="Markdown"
    )
    return LOAD_QTY


async def yukla_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        qty = int(update.message.text)
        if qty <= 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("⚠️ Noto'g'ri miqdor. Musbat butun raqam yuboring:")
        return LOAD_QTY

    uid = update.effective_user.id
    agent_id = context.user_data["agent_id"]
    product = context.user_data["product"]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # DB va Sheets
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO loads (omborchi_id, agent_id, product, qty, ts) VALUES (?, ?, ?, ?, ?)",
            (uid, agent_id, product, qty, ts),
        )
        conn.commit()

    asyncio.create_task(sync_to_sheet("Loads", [ts, uid, agent_id, product, qty]))

    # Omborchiga javob
    await update.message.reply_text(
        f"✅ *Muvaffaqiyatli yuklandi!*\n\n• Mahsulot: {product}\n• Miqdor: {qty} dona\n• Agent ID: {agent_id}",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(uid)
    )

    # Agentga bildirishnoma
    try:
        await context.bot.send_message(
            chat_id=agent_id,
            text=f"📦 *Sizga yangi mahsulot kelib tushdi!*\n\n• {product}: {qty} dona",
            parse_mode="Markdown"
        )
    except Exception as e:
        log.warning(f"Agentga bildirish boramadi: {e}")

    # ADMINGA BILDIRISHNOMA
    admin_msg = f"🔔 *YANGI YUKLASH OPERATSIYASI*\n\n🏭 Omborchi ID: `{uid}`\n🚚 Agent ID: `{agent_id}`\n📦 Mahsulot: {product}\n🔢 Miqdor: {qty} dona\n🗓 Vaqt: {ts}"
    await notify_admins(context, admin_msg)

    context.user_data.clear()
    return ConversationHandler.END


# --- CONVERSATION: AGENT TOPSHIRISHI ---
async def topshirish_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in AGENT_IDS:
        return ConversationHandler.END

    await update.message.reply_text("🚚 *Qaysi mahsulotni topshirmoqchisiz?*", parse_mode="Markdown", reply_markup=product_inline_keyboard())
    return DELIVER_PRODUCT


async def topshirish_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_action":
        await query.edit_message_text("Jarayon bekor qilindi.")
        return ConversationHandler.END

    prod = query.data.replace("prod_", "")
    uid = update.effective_user.id
    bal = get_agent_balance(uid, prod)

    if bal <= 0:
        await query.edit_message_text(f"⚠️ Sizda *{prod}* bo'yicha qoldiq mavjud emas!", parse_mode="Markdown")
        return ConversationHandler.END

    context.user_data["product"] = prod
    context.user_data["max_qty"] = bal

    await query.edit_message_text(
        f"Mahsulot: *{prod}*\nSizdagi qoldiq: `{bal}` dona.\n\n🔢 *Necha dona topshiryapsiz?*",
        parse_mode="Markdown"
    )
    return DELIVER_QTY


async def topshirish_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        qty = int(update.message.text)
        max_qty = context.user_data["max_qty"]
        if qty <= 0 or qty > max_qty:
            raise ValueError()
    except ValueError:
        await update.message.reply_text(f"⚠️ Noto'g'ri miqdor. 1 va {context.user_data['max_qty']} oralig'ida raqam kiriting:")
        return DELIVER_QTY

    context.user_data["qty"] = qty
    await update.message.reply_text("👤 *Mijoz nomini yoki korxona nomini kiriting:*", parse_mode="Markdown")
    return DELIVER_CUSTOMER


async def topshirish_customer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["customer"] = update.message.text.strip()
    loc_btn = ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Joylashuvni yuborish", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await update.message.reply_text("📍 *Topshirish joylashuvini (Location) yuboring:*", parse_mode="Markdown", reply_markup=loc_btn)
    return DELIVER_LOCATION


async def topshirish_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    if not loc:
        await update.message.reply_text("Iltimos, pastdagi '📍 Joylashuvni yuborish' tugmasidan foydalaning.")
        return DELIVER_LOCATION

    context.user_data["lat"] = loc.latitude
    context.user_data["lon"] = loc.longitude
    await update.message.reply_text("📸 *Topshirilgan mahsulot yoki yuk xati (chek) rasmini yuboring:*", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    return DELIVER_PHOTO


async def topshirish_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Iltimos, fotosurat yuboring.")
        return DELIVER_PHOTO

    file_id = update.message.photo[-1].file_id
    uid = update.effective_user.id
    d = context.user_data
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # DB & Sheets
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT INTO deliveries 
               (agent_id, product, qty, customer, lat, lon, photo_file_id, ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (uid, d["product"], d["qty"], d["customer"], d["lat"], d["lon"], file_id, ts),
        )
        conn.commit()

    asyncio.create_task(
        sync_to_sheet("Deliveries", [ts, uid, d["product"], d["qty"], d["customer"], d["lat"], d["lon"], file_id])
    )

    new_bal = get_agent_balance(uid, d["product"])

    await update.message.reply_text(
        f"✅ *Topshirish muvaffaqiyatli saqlandi!*\n\n"
        f"📦 Mahsulot: {d['product']}\n"
        f"🔢 Miqdor: {d['qty']} dona\n"
        f"👤 Mijoz: {d['customer']}\n"
        f"📉 Yangi qoldiq: {new_bal} dona",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(uid)
    )

    # ADMINGA REAL-TIME BILDIRISHNOMA VA RASM/LOCATION YUBORISH
    admin_msg = (
        f"🚨 *YANGI TOPSHIRUV AMALGA OSHIRILDI*\n\n"
        f"🚚 Agent ID: `{uid}`\n"
        f"📦 Mahsulot: {d['product']}\n"
        f"🔢 Miqdor: {d['qty']} dona\n"
        f"👤 Mijoz: *{d['customer']}*\n"
        f"🗓 Vaqt: {ts}"
    )
    await notify_admins(context, admin_msg, photo_id=file_id, lat=d["lat"], lon=d["lon"])

    context.user_data.clear()
    return ConversationHandler.END


async def cancel_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    uid = update.effective_user.id
    await update.message.reply_text("Jarayon bekor qilindi.", reply_markup=main_menu_keyboard(uid))
    return ConversationHandler.END


# ---------------- MAIN ----------------
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^📊 Mening qoldig'im$") | filters.Regex("^📊 Qoldiqlarni ko'rish$"), agent_qoldiq))

    # Admin Handler
    app.add_handler(MessageHandler(filters.Regex("^(📊 Agentlar Qoldig'i|📜 Oxirgi Topshirishlar|📦 Oxirgi Yuklanmalar)$"), admin_reports))

    # Conversation - Omborchi
    load_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📦 Agentga yuklash$"), yukla_start)],
        states={
            LOAD_PRODUCT: [CallbackQueryHandler(yukla_product_callback, pattern="^(prod_|cancel_action)")],
            LOAD_AGENT: [CallbackQueryHandler(yukla_agent_callback, pattern="^(agent_|cancel_action)")],
            LOAD_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, yukla_qty)],
        },
        fallbacks=[CommandHandler("cancel", cancel_fallback)],
    )

    # Conversation - Agent
    deliver_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🚚 Mijozga topshirish$"), topshirish_start)],
        states={
            DELIVER_PRODUCT: [CallbackQueryHandler(topshirish_product_callback, pattern="^(prod_|cancel_action)")],
            DELIVER_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, topshirish_qty)],
            DELIVER_CUSTOMER: [MessageHandler(filters.TEXT & ~filters.COMMAND, topshirish_customer)],
            DELIVER_LOCATION: [MessageHandler(filters.LOCATION, topshirish_location)],
            DELIVER_PHOTO: [MessageHandler(filters.PHOTO, topshirish_photo)],
        },
        fallbacks=[CommandHandler("cancel", cancel_fallback)],
    )

    app.add_handler(load_handler)
    app.add_handler(deliver_handler)

    log.info("Bot tayyor va ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()
