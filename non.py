# -*- coding: utf-8 -*-
"""
Sotuv agentlari uchun Telegram bot.
- Omborchilar (5 kishi) agentlarga mahsulot yuklaydi (dona hisobida).
- Agentlar mijozga topshirganda: mahsulot turi, miqdori, mijoz nomi,
  joylashuv (location) va rasm yuboradi.
- Barcha ma'lumot mahalliy SQLite bazasiga yoziladi (keyinchalik
  Google Sheets bilan sinxronlash uchun ham tayyor struktura).

O'RNATISH:
    pip install python-telegram-bot==21.* gspread google-auth --break-system-packages

ISHGA TUSHIRISH:
    python3 bot.py

Quyidagi joylarni o'zingizga moslang:
    BOT_TOKEN         - @BotFather'dan olingan token
    PRODUCTS          - 5 xil mahsulot nomi
    OMBORCHI_IDS      - 5 ta omborchi Telegram user_id (raqam)
    AGENT_IDS         - agentlar Telegram user_id (raqam)
    GOOGLE_SHEET_ID   - Google Sheets jadval ID (URL'dagi /d/... qismi)
    SERVICE_ACCOUNT_JSON - Google service account kaliti (pastga qarang)

Har bir foydalanuvchining Telegram user_id'sini bilish uchun
u botga /start yuborsin — konsolda ID chiqadi (pastdagi start() funksiyasida).

GOOGLE SHEETS BILAN ULASH (bir martalik sozlash):
    1. https://console.cloud.google.com -> yangi loyiha -> "Google Sheets API"ni yoqing
    2. "Service Accounts" bo'limida yangi service account yarating,
       "Keys" -> "Add Key" -> JSON -> yuklab oling, shu papkaga
       "service_account.json" nomi bilan saqlang
    3. JSON faylida "client_email" bor - shu email'ni Google Sheets
       jadvalingizga "Editor" huquqi bilan ulashing (Share tugmasi)
    4. Jadval URL'idagi ID'ni (masalan
       docs.google.com/spreadsheets/d/BU_YERDA_ID/edit) GOOGLE_SHEET_ID'ga qo'ying
    5. Jadvalda ikkita varaq (tab) yarating: "Loads" va "Deliveries"
       (bot ustunlarni o'zi to'ldiradi, sarlavha shart emas)
"""

import os
import sqlite3
import logging
from datetime import datetime

try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ---------------- SOZLAMALAR ----------------
# Token avval environment variable'dan o'qiladi (Railway/Render uchun xavfsiz);
# topilmasa, quyidagi qatorga to'g'ridan-to'g'ri yozib qo'yishingiz mumkin
# (lekin bu holda faylni hech qayerga ochiq joylashtirmang).
BOT_TOKEN = "8744917299:AAGOZMJwDggIbmOYdLyyumQBS412pl7Zeqk"

PRODUCTS = [
    "Teleskopik tirgak",
    "Skaffolding (angar)",
    "Opalubka",
    "Sterjen (tie rod)",
    "Boshqa",
]

# Bu yerga haqiqiy Telegram user_id'larni kiriting
OMBORCHI_IDS = [111111111, 222222222, 333333333, 444444444, 555555555]
AGENT_IDS = [666666666, 777777777]

DB_PATH = "sales_bot.db"

# Google Sheets sozlamalari (ixtiyoriy - to'ldirmasangiz bot faqat
# mahalliy SQLite bazasiga yozadi, xatosiz ishlayveradi)
GOOGLE_SHEET_ID = ""  # masalan: "1AbCdEfGhIjKlMnOpQrStUvWxYz"
SERVICE_ACCOUNT_JSON = "service_account.json"

# Suhbat bosqichlari (ConversationHandler states)
(
    LOAD_PRODUCT, LOAD_AGENT, LOAD_QTY,
    DELIVER_PRODUCT, DELIVER_QTY, DELIVER_CUSTOMER, DELIVER_LOCATION, DELIVER_PHOTO,
) = range(8)


# ---------------- BAZA ----------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS loads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            omborchi_id INTEGER,
            agent_id INTEGER,
            product TEXT,
            qty INTEGER,
            ts TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id INTEGER,
            product TEXT,
            qty INTEGER,
            customer TEXT,
            lat REAL,
            lon REAL,
            photo_file_id TEXT,
            ts TEXT
        )
    """)
    conn.commit()
    return conn


def agent_balance(agent_id: int, product: str) -> int:
    """Agentda hozir qancha dona bor: yuklangan - topshirilgan."""
    conn = db()
    loaded = conn.execute(
        "SELECT COALESCE(SUM(qty),0) FROM loads WHERE agent_id=? AND product=?",
        (agent_id, product),
    ).fetchone()[0]
    delivered = conn.execute(
        "SELECT COALESCE(SUM(qty),0) FROM deliveries WHERE agent_id=? AND product=?",
        (agent_id, product),
    ).fetchone()[0]
    conn.close()
    return loaded - delivered


def get_sheet(tab_name: str):
    """Google Sheets varag'ini qaytaradi, sozlanmagan yoki xato bo'lsa None."""
    if not GSPREAD_AVAILABLE or not GOOGLE_SHEET_ID:
        return None
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_JSON, scopes=scopes)
        client = gspread.authorize(creds)
        sh = client.open_by_key(GOOGLE_SHEET_ID)
        return sh.worksheet(tab_name)
    except Exception as e:
        log.warning("Google Sheets bilan bog'lanib bo'lmadi: %s", e)
        return None


def sync_load_to_sheet(omborchi_id, agent_id, product, qty, ts):
    ws = get_sheet("Loads")
    if ws:
        try:
            ws.append_row([ts, omborchi_id, agent_id, product, qty])
        except Exception as e:
            log.warning("Loads varag'iga yozib bo'lmadi: %s", e)


def sync_delivery_to_sheet(agent_id, product, qty, customer, lat, lon, photo_file_id, ts):
    ws = get_sheet("Deliveries")
    if ws:
        try:
            ws.append_row([ts, agent_id, product, qty, customer, lat, lon, photo_file_id])
        except Exception as e:
            log.warning("Deliveries varag'iga yozib bo'lmadi: %s", e)


def product_keyboard():
    rows = [[p] for p in PRODUCTS]
    return ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)


# ---------------- UMUMIY ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    log.info("user_id: %s", uid)
    if uid in OMBORCHI_IDS:
        await update.message.reply_text(
            "Salom, omborchi! Yuklash uchun /yukla buyrug'ini yuboring."
        )
    elif uid in AGENT_IDS:
        await update.message.reply_text(
            "Salom, agent! Topshirish uchun /topshirish buyrug'ini yuboring.\n"
            "Qoldig'ingizni ko'rish uchun /qoldiq."
        )
    else:
        await update.message.reply_text(
            f"Salom! Sizning ID: {uid}\n"
            "Bu bot faqat ro'yxatdan o'tgan omborchi va agentlar uchun. "
            "Administratorga ID'ingizni yuboring."
        )


async def qoldiq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in AGENT_IDS:
        return
    lines = [f"{p}: {agent_balance(uid, p)} dona" for p in PRODUCTS]
    await update.message.reply_text("Sizning qoldig'ingiz:\n" + "\n".join(lines))


# ---------------- OMBORCHI: YUKLASH ----------------
async def yukla_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in OMBORCHI_IDS:
        await update.message.reply_text("Bu buyruq faqat omborchilar uchun.")
        return ConversationHandler.END
    await update.message.reply_text("Qaysi mahsulot?", reply_markup=product_keyboard())
    return LOAD_PRODUCT


async def yukla_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["product"] = update.message.text
    kb = ReplyKeyboardMarkup(
        [[str(a)] for a in AGENT_IDS], one_time_keyboard=True, resize_keyboard=True
    )
    await update.message.reply_text(
        "Qaysi agentga yuklanadi? (Agent ID'ni tanlang yoki yozing)", reply_markup=kb
    )
    return LOAD_AGENT


async def yukla_agent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["agent_id"] = int(update.message.text)
    except ValueError:
        await update.message.reply_text("Noto'g'ri ID, qaytadan kiriting.")
        return LOAD_AGENT
    await update.message.reply_text(
        "Necha dona?", reply_markup=ReplyKeyboardRemove()
    )
    return LOAD_QTY


async def yukla_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        qty = int(update.message.text)
    except ValueError:
        await update.message.reply_text("Raqam kiriting (masalan: 20).")
        return LOAD_QTY

    conn = db()
    conn.execute(
        "INSERT INTO loads (omborchi_id, agent_id, product, qty, ts) VALUES (?,?,?,?,?)",
        (
            update.effective_user.id,
            context.user_data["agent_id"],
            context.user_data["product"],
            qty,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    conn.close()

    ts = datetime.now().isoformat(timespec="seconds")
    sync_load_to_sheet(
        update.effective_user.id, context.user_data["agent_id"],
        context.user_data["product"], qty, ts,
    )

    await update.message.reply_text(
        f"✅ Yuklandi: {context.user_data['product']} — {qty} dona "
        f"(agent {context.user_data['agent_id']})"
    )
    try:
        await context.bot.send_message(
            context.user_data["agent_id"],
            f"📦 Sizga yuklandi: {context.user_data['product']} — {qty} dona",
        )
    except Exception as e:
        log.warning("Agentga xabar yuborilmadi: %s", e)

    context.user_data.clear()
    return ConversationHandler.END


# ---------------- AGENT: TOPSHIRISH ----------------
async def topshirish_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in AGENT_IDS:
        await update.message.reply_text("Bu buyruq faqat agentlar uchun.")
        return ConversationHandler.END
    await update.message.reply_text("Qaysi mahsulotni topshiryapsiz?", reply_markup=product_keyboard())
    return DELIVER_PRODUCT


async def topshirish_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["product"] = update.message.text
    bal = agent_balance(update.effective_user.id, update.message.text)
    await update.message.reply_text(
        f"Qoldig'ingiz: {bal} dona. Necha dona topshirasiz?",
        reply_markup=ReplyKeyboardRemove(),
    )
    return DELIVER_QTY


async def topshirish_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        qty = int(update.message.text)
    except ValueError:
        await update.message.reply_text("Raqam kiriting.")
        return DELIVER_QTY

    bal = agent_balance(update.effective_user.id, context.user_data["product"])
    if qty > bal:
        await update.message.reply_text(
            f"Xato: qoldig'ingizda faqat {bal} dona bor. Qaytadan kiriting."
        )
        return DELIVER_QTY

    context.user_data["qty"] = qty
    await update.message.reply_text("Mijoz ismi?")
    return DELIVER_CUSTOMER


async def topshirish_customer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["customer"] = update.message.text
    loc_btn = ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Joylashuvni yuborish", request_location=True)]],
        one_time_keyboard=True,
        resize_keyboard=True,
    )
    await update.message.reply_text("Joylashuvni yuboring:", reply_markup=loc_btn)
    return DELIVER_LOCATION


async def topshirish_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    if not loc:
        await update.message.reply_text("Iltimos, 📍 tugmasi orqali joylashuv yuboring.")
        return DELIVER_LOCATION
    context.user_data["lat"] = loc.latitude
    context.user_data["lon"] = loc.longitude
    await update.message.reply_text(
        "Endi topshirilgan mahsulot rasmini yuboring:", reply_markup=ReplyKeyboardRemove()
    )
    return DELIVER_PHOTO


async def topshirish_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Iltimos, rasm (fotosurat) yuboring.")
        return DELIVER_PHOTO

    file_id = update.message.photo[-1].file_id
    d = context.user_data

    conn = db()
    conn.execute(
        """INSERT INTO deliveries
           (agent_id, product, qty, customer, lat, lon, photo_file_id, ts)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            update.effective_user.id,
            d["product"],
            d["qty"],
            d["customer"],
            d["lat"],
            d["lon"],
            file_id,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    conn.close()

    sync_delivery_to_sheet(
        update.effective_user.id, d["product"], d["qty"], d["customer"],
        d["lat"], d["lon"], file_id, datetime.now().isoformat(timespec="seconds"),
    )

    await update.message.reply_text(
        f"✅ Topshirildi:\n"
        f"Mahsulot: {d['product']}\n"
        f"Miqdor: {d['qty']} dona\n"
        f"Mijoz: {d['customer']}\n"
        f"Joylashuv: {d['lat']:.5f}, {d['lon']:.5f}\n"
        f"Yangi qoldiq: {agent_balance(update.effective_user.id, d['product'])} dona"
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Bekor qilindi.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ---------------- ISHGA TUSHIRISH ----------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("qoldiq", qoldiq))

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("yukla", yukla_start)],
        states={
            LOAD_PRODUCT: [MessageHandler(filters.TEXT & ~filters.COMMAND, yukla_product)],
            LOAD_AGENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, yukla_agent)],
            LOAD_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, yukla_qty)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("topshirish", topshirish_start)],
        states={
            DELIVER_PRODUCT: [MessageHandler(filters.TEXT & ~filters.COMMAND, topshirish_product)],
            DELIVER_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, topshirish_qty)],
            DELIVER_CUSTOMER: [MessageHandler(filters.TEXT & ~filters.COMMAND, topshirish_customer)],
            DELIVER_LOCATION: [MessageHandler(filters.LOCATION, topshirish_location)],
            DELIVER_PHOTO: [MessageHandler(filters.PHOTO, topshirish_photo)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))

    log.info("Bot ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()
