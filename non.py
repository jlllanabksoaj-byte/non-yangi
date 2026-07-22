# -*- coding: utf-8 -*-
"""
Sotuv agentlari, Omborchilar va Admin uchun mahalliy SQLite bazasidagi bot.
- Google Sheets olib tashlangan (Maksimal tezlik).
- Mijozlar bazasi (yangi qo'shish, ro'yxatdan tanlash, tahrirlash).
- Admin uchun to'liq monitoring va mijozlar boshqaruvi.
"""

import logging
import os
import sqlite3
from datetime import datetime

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

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ---------------- SOZLAMALAR ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

PRODUCTS = [
    "non2",
    "non3 (angar)",
    "non4",
    "non5 (tie rod)",
    "Boshqa",
]

# ID'lar (O'zingiznikini kiriting)
ADMIN_IDS = {999999999}
OMBORCHI_IDS = {766842087, 222222222}
AGENT_IDS = {7010796701, 777777777}

DB_PATH = "sales_bot.db"

# Conversation States
(
    LOAD_PRODUCT, LOAD_AGENT, LOAD_QTY,
    DELIVER_PRODUCT, DELIVER_QTY, DELIVER_SELECT_CUSTOMER,
    NEW_CUST_NAME, NEW_CUST_PHONE, NEW_CUST_LOCATION, DELIVER_PHOTO,
    EDIT_CUST_SELECT, EDIT_CUST_NAME, EDIT_CUST_PHONE
) = range(13)


# ---------------- BAZA AMALLARI ----------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        # Ombor yuklanmalari
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
        # Topshirishlar
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id INTEGER NOT NULL,
                product TEXT NOT NULL,
                qty INTEGER NOT NULL,
                customer_id INTEGER NOT NULL,
                photo_file_id TEXT NOT NULL,
                ts TEXT NOT NULL
            )
        """)
        # Mijozlar bazasi
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT DEFAULT '',
                lat REAL,
                lon REAL,
                created_by INTEGER NOT NULL,
                ts TEXT NOT NULL
            )
        """)
        conn.commit()

def get_agent_balance(agent_id: int, product: str) -> int:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COALESCE(SUM(qty), 0) FROM loads WHERE agent_id=? AND product=?", (agent_id, product))
        loaded = cursor.fetchone()[0]
        cursor.execute("SELECT COALESCE(SUM(qty), 0) FROM deliveries WHERE agent_id=? AND product=?", (agent_id, product))
        delivered = cursor.fetchone()[0]
    return loaded - delivered

def get_customers_list():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, phone FROM customers ORDER BY name ASC")
        return cursor.fetchall()


# ---------------- MENYULAR ----------------
def main_menu_keyboard(uid: int) -> ReplyKeyboardMarkup:
    rows = []
    if uid in ADMIN_IDS:
        rows = [
            ["📊 Agentlar Qoldig'i", "📜 Oxirgi Topshirishlar"],
            ["📦 Oxirgi Yuklanmalar", "👥 Mijozlar Bazasi"],
            ["✏️ Mijozni Tahrirlash"]
        ]
    elif uid in OMBORCHI_IDS:
        rows = [["📦 Agentga yuklash"], ["📊 Qoldiqlarni ko'rish"]]
    elif uid in AGENT_IDS:
        rows = [["🚚 Mijozga topshirish"], ["📊 Mening qoldig'im"]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def product_inline_keyboard() -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(p, callback_data=f"prod_{p}")] for p in PRODUCTS]
    buttons.append([InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel_action")])
    return InlineKeyboardMarkup(buttons)

def agent_inline_keyboard() -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(f"Agent ID: {a}", callback_data=f"agent_{a}")] for a in AGENT_IDS]
    buttons.append([InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel_action")])
    return InlineKeyboardMarkup(buttons)

def customers_inline_keyboard() -> InlineKeyboardMarkup:
    customers = get_customers_list()
    buttons = []
    for c_id, c_name, c_phone in customers:
        phone_str = f" ({c_phone})" if c_phone else ""
        buttons.append([InlineKeyboardButton(f"🏢 {c_name}{phone_str}", callback_data=f"cust_{c_id}")])
    
    buttons.append([InlineKeyboardButton("➕ YANGI MIJOZ QO'SHISH", callback_data="cust_new")])
    buttons.append([InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel_action")])
    return InlineKeyboardMarkup(buttons)


# ---------------- ADMINGA BILDIRISHNOMA ----------------
async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str, photo_id: str = None, lat: float = None, lon: float = None):
    for admin_id in ADMIN_IDS:
        try:
            if photo_id:
                await context.bot.send_photo(chat_id=admin_id, photo=photo_id, caption=text, parse_mode="Markdown")
            else:
                await context.bot.send_message(chat_id=admin_id, text=text, parse_mode="Markdown")
            if lat and lon:
                await context.bot.send_location(chat_id=admin_id, latitude=lat, longitude=lon)
        except Exception as e:
            log.warning(f"Admin notification error: {e}")


# ---------------- HANDLERLAR ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in ADMIN_IDS:
        text = "👑 *Admin Panel:* Barcha ma'lumotlar va Mijozlar bazasini boshqarishingiz mumkin."
    elif uid in OMBORCHI_IDS:
        text = "🏭 *Omborchi Paneli:*"
    elif uid in AGENT_IDS:
        text = "🚚 *Agent Paneli:*"
    else:
        await update.message.reply_text(f"🚫 Kirish taqiqlandi! Sizning ID: `{uid}`", parse_mode="Markdown")
        return
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard(uid))


# --- ADMIN HISOBOTLARI & MIJOZLAR BAZA KUZATUVI ---
async def admin_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    txt = update.message.text

    if txt == "📊 Agentlar Qoldig'i":
        report = "📊 *Agentlar Qoldig'i:*\n\n"
        for agent in AGENT_IDS:
            report += f"👤 *Agent ID: {agent}*\n"
            for p in PRODUCTS:
                report += f"  • {p}: `{get_agent_balance(agent, p)}` dona\n"
            report += "\n"
        await update.message.reply_text(report, parse_mode="Markdown")

    elif txt == "📜 Oxirgi Topshirishlar":
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT d.agent_id, d.product, d.qty, c.name, d.ts 
                FROM deliveries d 
                JOIN customers c ON d.customer_id = c.id 
                ORDER BY d.id DESC LIMIT 5
            """)
            rows = cursor.fetchall()
        if not rows:
            await update.message.reply_text("Topshirishlar tarixi bo'sh.")
            return
        msg = "📜 *Oxirgi 5 ta topshiruv:*\n\n" + "\n".join([f"🗓 `{r[4]}` | Agent: `{r[0]}`\n📦 {r[1]} - {r[2]} dona -> *{r[3]}*\n---" for r in rows])
        await update.message.reply_text(msg, parse_mode="Markdown")

    elif txt == "👥 Mijozlar Bazasi":
        customers = get_customers_list()
        if not customers:
            await update.message.reply_text("Mijozlar bazasi hali bo'sh.")
            return
        msg = "👥 *Ro'yxatdan o'tgan mijozlar:*\n\n"
        for c_id, c_name, c_phone in customers:
            msg += f"🔹 ID: `{c_id}` | *{c_name}* | Tel: {c_phone or 'Kiritilmagan'}\n"
        await update.message.reply_text(msg, parse_mode="Markdown")


# --- OMBORCHI YUKLASH ---
async def yukla_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in OMBORCHI_IDS: return ConversationHandler.END
    await update.message.reply_text("📦 *Mahsulotni tanlang:*", parse_mode="Markdown", reply_markup=product_inline_keyboard())
    return LOAD_PRODUCT

async def yukla_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel_action":
        await query.edit_message_text("Bekor qilindi.")
        return ConversationHandler.END
    context.user_data["product"] = query.data.replace("prod_", "")
    await query.edit_message_text("👤 *Agentni tanlang:*", parse_mode="Markdown", reply_markup=agent_inline_keyboard())
    return LOAD_AGENT

async def yukla_agent_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel_action":
        await query.edit_message_text("Bekor qilindi.")
        return ConversationHandler.END
    context.user_data["agent_id"] = int(query.data.replace("agent_", ""))
    await query.edit_message_text("🔢 *Necha dona yuklaysiz?* (Raqam kiriting):", parse_mode="Markdown")
    return LOAD_QTY

async def yukla_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        qty = int(update.message.text)
        if qty <= 0: raise ValueError()
    except ValueError:
        await update.message.reply_text("⚠️ Noto'g'ri miqdor. Musbat raqam kiriting:")
        return LOAD_QTY

    uid = update.effective_user.id
    agent_id, product = context.user_data["agent_id"], context.user_data["product"]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_db() as conn:
        conn.execute("INSERT INTO loads (omborchi_id, agent_id, product, qty, ts) VALUES (?, ?, ?, ?, ?)", (uid, agent_id, product, qty, ts))
        conn.commit()

    await update.message.reply_text(f"✅ *Yuklandi!*\n• {product}: {qty} dona -> Agent: {agent_id}", parse_mode="Markdown", reply_markup=main_menu_keyboard(uid))
    await notify_admins(context, f"🔔 *YANGI YUKLASH*\nOmborchi: `{uid}`\nAgent: `{agent_id}`\n📦 {product} - {qty} dona")
    context.user_data.clear()
    return ConversationHandler.END


# --- AGENT TOPSHIRISHI VA MIJOZ TANLASH ---
async def topshirish_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in AGENT_IDS: return ConversationHandler.END
    await update.message.reply_text("🚚 *Topshiriladigan mahsulotni tanlang:*", parse_mode="Markdown", reply_markup=product_inline_keyboard())
    return DELIVER_PRODUCT

async def topshirish_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel_action":
        await query.edit_message_text("Bekor qilindi.")
        return ConversationHandler.END

    prod = query.data.replace("prod_", "")
    bal = get_agent_balance(update.effective_user.id, prod)
    if bal <= 0:
        await query.edit_message_text(f"⚠️ Sizda *{prod}* bo'yicha qoldiq yo'q!", parse_mode="Markdown")
        return ConversationHandler.END

    context.user_data.update({"product": prod, "max_qty": bal})
    await query.edit_message_text(f"Qoldig'ingiz: `{bal}` dona.\n\n🔢 *Necha dona topshirmoqchisiz?*", parse_mode="Markdown")
    return DELIVER_QTY

async def topshirish_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        qty = int(update.message.text)
        if qty <= 0 or qty > context.user_data["max_qty"]: raise ValueError()
    except ValueError:
        await update.message.reply_text("⚠️ Noto'g'ri miqdor. Qaytadan kiriting:")
        return DELIVER_QTY

    context.user_data["qty"] = qty
    await update.message.reply_text("🏢 *Mijozni tanlang yoki yangi mijoz qo'shing:*", parse_mode="Markdown", reply_markup=customers_inline_keyboard())
    return DELIVER_SELECT_CUSTOMER


async def topshirish_customer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_action":
        await query.edit_message_text("Bekor qilindi.")
        return ConversationHandler.END

    if query.data == "cust_new":
        await query.edit_message_text("🏢 *Yangi Mijoz / Ob'ekt nomini kiriting:*", parse_mode="Markdown")
        return NEW_CUST_NAME
    else:
        cust_id = int(query.data.replace("cust_", ""))
        context.user_data["customer_id"] = cust_id
        await query.edit_message_text("📸 *Topshirilgan mahsulot yoki yuk xati rasmini yuboring:*", parse_mode="Markdown")
        return DELIVER_PHOTO


# --- YANGI MIJOZ QO'SHISH BOSQICHLARI ---
async def new_cust_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_cust_name"] = update.message.text.strip()
    await update.message.reply_text("📞 *Mijoz telefon raqamini kiriting* (Yoki 'Yo'q' deb yozing):", parse_mode="Markdown")
    return NEW_CUST_PHONE

async def new_cust_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    context.user_data["new_cust_phone"] = "" if phone.lower() in ["yo'q", "yoq", "no"] else phone
    loc_btn = ReplyKeyboardMarkup([[KeyboardButton("📍 Joylashuvni yuborish", request_location=True)]], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text("📍 *Mijozning doimiy joylashuvini (location) yuboring:*", parse_mode="Markdown", reply_markup=loc_btn)
    return NEW_CUST_LOCATION

async def new_cust_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    if not loc:
        await update.message.reply_text("Iltimos, tugma orqali joylashuvni yuboring.")
        return NEW_CUST_LOCATION

    uid = update.effective_user.id
    name = context.user_data["new_cust_name"]
    phone = context.user_data["new_cust_phone"]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Mijozni bazaga saqlaymiz
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO customers (name, phone, lat, lon, created_by, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (name, phone, loc.latitude, loc.longitude, uid, ts)
        )
        cust_id = cursor.lastrowid
        conn.commit()

    context.user_data["customer_id"] = cust_id
    await update.message.reply_text(f"✅ *Yangi mijoz bazaga saqlandi:* {name}\n\n📸 Endi topshirilgan *mahsulot rasmini* yuboring:", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    return DELIVER_PHOTO


async def topshirish_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Iltimos, foto rasm yuboring.")
        return DELIVER_PHOTO

    file_id = update.message.photo[-1].file_id
    uid = update.effective_user.id
    d = context.user_data
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO deliveries (agent_id, product, qty, customer_id, photo_file_id, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, d["product"], d["qty"], d["customer_id"], file_id, ts),
        )
        # Mijoz ma'lumotlarini admin xabari uchun olamiz
        cursor.execute("SELECT name, lat, lon FROM customers WHERE id=?", (d["customer_id"],))
        c_name, c_lat, c_lon = cursor.fetchone()
        conn.commit()

    await update.message.reply_text(
        f"✅ *Topshirildi!*\n• {d['product']}: {d['qty']} dona\n• Mijoz: *{c_name}*",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(uid)
    )

    admin_msg = f"🚨 *YANGI TOPSHIRUV*\nAgent: `{uid}`\n📦 {d['product']} - {d['qty']} dona\n👤 Mijoz: *{c_name}*"
    await notify_admins(context, admin_msg, photo_id=file_id, lat=c_lat, lon=c_lon)

    context.user_data.clear()
    return ConversationHandler.END


# --- ADMIN: MIJOZLARNI TAHRIRLASH PROCESS ---
async def edit_cust_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return ConversationHandler.END
    
    customers = get_customers_list()
    if not customers:
        await update.message.reply_text("Tahrirlash uchun mijozlar yo'q.")
        return ConversationHandler.END

    buttons = [[InlineKeyboardButton(f"✏️ {c[1]}", callback_data=f"editc_{c[0]}")] for c in customers]
    buttons.append([InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel_action")])
    await update.message.reply_text("✏️ *Qaysi mijoz ma'lumotlarini tahrirlaysiz?*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    return EDIT_CUST_SELECT

async def edit_cust_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel_action":
        await query.edit_message_text("Bekor qilindi.")
        return ConversationHandler.END

    c_id = int(query.data.replace("editc_", ""))
    context.user_data["edit_cust_id"] = c_id
    await query.edit_message_text("🏢 *Mijoz uchun yangi nom kiriting:*", parse_mode="Markdown")
    return EDIT_CUST_NAME

async def edit_cust_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["edit_cust_name"] = update.message.text.strip()
    await update.message.reply_text("📞 *Yangi telefon raqamini kiriting:*", parse_mode="Markdown")
    return EDIT_CUST_PHONE

async def edit_cust_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    c_id = context.user_data["edit_cust_id"]
    name = context.user_data["edit_cust_name"]

    with get_db() as conn:
        conn.execute("UPDATE customers SET name=?, phone=? WHERE id=?", (name, phone, c_id))
        conn.commit()

    await update.message.reply_text("✅ *Mijoz ma'lumotlari muvaffaqiyatli yangilandi!*", parse_mode="Markdown", reply_markup=main_menu_keyboard(update.effective_user.id))
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Bekor qilindi.", reply_markup=main_menu_keyboard(update.effective_user.id))
    return ConversationHandler.END


# ---------------- MAIN ----------------
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^(📊 Mening qoldig'im|📊 Qoldiqlarni ko'rish)$"), lambda u, c: u.message.reply_text("Qoldiq menyusi")))
    app.add_handler(MessageHandler(filters.Regex("^(📊 Agentlar Qoldig'i|📜 Oxirgi Topshirishlar|📦 Oxirgi Yuklanmalar|👥 Mijozlar Bazasi)$"), admin_reports))

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

    # Conversation - Agent (Mijoz tanlash va Yangi mijoz qo'shish bilan)
    deliver_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🚚 Mijozga topshirish$"), topshirish_start)],
        states={
            DELIVER_PRODUCT: [CallbackQueryHandler(topshirish_product_callback, pattern="^(prod_|cancel_action)")],
            DELIVER_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, topshirish_qty)],
            DELIVER_SELECT_CUSTOMER: [CallbackQueryHandler(topshirish_customer_callback, pattern="^(cust_|cancel_action)")],
            NEW_CUST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_cust_name)],
            NEW_CUST_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_cust_phone)],
            NEW_CUST_LOCATION: [MessageHandler(filters.LOCATION, new_cust_location)],
            DELIVER_PHOTO: [MessageHandler(filters.PHOTO, topshirish_photo)],
        },
        fallbacks=[CommandHandler("cancel", cancel_fallback)],
    )

    # Conversation - Admin Edit Customer
    edit_customer_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^✏️ Mijozni Tahrirlash$"), edit_cust_start)],
        states={
            EDIT_CUST_SELECT: [CallbackQueryHandler(edit_cust_select_callback, pattern="^(editc_|cancel_action)")],
            EDIT_CUST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_cust_name)],
            EDIT_CUST_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_cust_phone)],
        },
        fallbacks=[CommandHandler("cancel", cancel_fallback)],
    )

    app.add_handler(load_handler)
    app.add_handler(deliver_handler)
    app.add_handler(edit_customer_handler)

    log.info("Bot ishga tushirildi...")
    app.run_polling()

if __name__ == "__main__":
    main()
