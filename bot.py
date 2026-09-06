# -*- coding: utf-8 -*-
"""
Universal Downloader Bot — v2.0
- Multi-engine: yt-dlp -> gallery-dl -> direct -> SmartGrab (custom fallback scraper)
- Video / MP3 / Photo support (YouTube, Pinterest, Instagram, TikTok, X, +1000 sites)
- Big files -> Cloudflare quick tunnel direct link (no splitting)
- Local SQLite database (users, language memory, full history)
- Admin dashboard on http://SERVER_IP:2000
- Languages: FA / EN / AR / TR / RU
"""
import os
import re
import sqlite3
import asyncio
import logging
import shutil
import subprocess
import threading
import time
import functools
import mimetypes
from pathlib import Path
from string import Template
from urllib.parse import quote, urljoin
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
import yt_dlp
import requests

# ---------------- Config ----------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is missing in .env")

BASE_DIR = Path(__file__).resolve().parent
DL_DIR = BASE_DIR / "downloads"
DL_DIR.mkdir(exist_ok=True)
DB_PATH = BASE_DIR / "bot.db"

MAX_TG_SIZE   = 49 * 1024 * 1024   # > this -> Cloudflare direct link
HTTP_PORT     = 8765               # internal file server (tunnel source)
DASH_PORT     = 2000               # admin dashboard
MAX_PHOTOS    = 10                 # telegram album limit
FILE_TTL      = 2 * 3600           # keep tunnel files 2h then auto-clean

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("dlbot")

EXEC = ThreadPoolExecutor(max_workers=6)          # parallel downloads
user_locks: dict[int, asyncio.Lock] = {}
def ulock(uid: int) -> asyncio.Lock:
    if uid not in user_locks:
        user_locks[uid] = asyncio.Lock()
    return user_locks[uid]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# ---------------- Database ----------------
def db():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.execute("PRAGMA journal_mode=WAL")
    return con

def init_db():
    con = db()
    con.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        username TEXT, first_name TEXT,
        lang TEXT DEFAULT '',
        links INTEGER DEFAULT 0,
        downloads INTEGER DEFAULT 0,
        videos INTEGER DEFAULT 0,
        photos INTEGER DEFAULT 0,
        audios INTEGER DEFAULT 0,
        joined TEXT DEFAULT (datetime('now')))""")
    con.execute("""CREATE TABLE IF NOT EXISTS history(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, url TEXT, kind TEXT,
        size INTEGER DEFAULT 0, status TEXT DEFAULT 'ok',
        at TEXT DEFAULT (datetime('now')))""")
    # migrate old db (add new columns if missing)
    cols = [r[1] for r in con.execute("PRAGMA table_info(users)")]
    for c, ddl in [("username","ALTER TABLE users ADD COLUMN username TEXT"),
                   ("first_name","ALTER TABLE users ADD COLUMN first_name TEXT"),
                   ("links","ALTER TABLE users ADD COLUMN links INTEGER DEFAULT 0"),
                   ("videos","ALTER TABLE users ADD COLUMN videos INTEGER DEFAULT 0"),
                   ("photos","ALTER TABLE users ADD COLUMN photos INTEGER DEFAULT 0"),
                   ("audios","ALTER TABLE users ADD COLUMN audios INTEGER DEFAULT 0")]:
        if c not in cols:
            con.execute(ddl)
    cols = [r[1] for r in con.execute("PRAGMA table_info(history)")]
    if "status" not in cols:
        con.execute("ALTER TABLE history ADD COLUMN status TEXT DEFAULT 'ok'")
    con.commit(); con.close()

def upsert_user(u):
    con = db()
    con.execute("""INSERT INTO users(user_id,username,first_name) VALUES(?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
                   first_name=excluded.first_name""",
                (u.id, u.username or "", u.first_name or ""))
    con.commit(); con.close()

def get_user(uid: int):
    con = db()
    row = con.execute("SELECT lang,links,downloads,videos,photos,audios FROM users WHERE user_id=?",
                      (uid,)).fetchone()
    con.close()
    return row

def get_lang(uid: int) -> str:
    row = get_user(uid)
    return row[0] if row and row[0] else ""

def set_lang(uid: int, lang: str):
    con = db()
    con.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (uid,))
    con.execute("UPDATE users SET lang=? WHERE user_id=?", (lang, uid))
    con.commit(); con.close()

def bump(uid: int, field: str):
    con = db()
    con.execute(f"UPDATE users SET {field}={field}+1 WHERE user_id=?", (uid,))
    con.commit(); con.close()

def log_dl(uid, url, kind, size, status="ok"):
    con = db()
    con.execute("INSERT INTO history(user_id,url,kind,size,status) VALUES(?,?,?,?,?)",
                (uid, url, kind, size, status))
    con.commit(); con.close()

# ---------------- Strings (5 languages) ----------------
T = {
"fa": {
 "choose_lang": "🌐 <b>خوش اومدی!</b>\n\nلطفاً زبانت رو انتخاب کن:",
 "welcome": "👋 <b>سلام {name} عزیز!</b>\n\nمن یه <b>دانلودر همه‌کاره</b>‌ام 🤖\nکافیه لینک رو از این سایت‌ها بفرستی:\n\n▫️ YouTube (ویدیو / Shorts)\n▫️ Pinterest (pin.it)\n▫️ Instagram / TikTok / X\n▫️ +۱۰۰۰ سایت دیگه\n\n🖼 عکس هم پشتیبانی میشه!\n📎 <i>فقط لینک رو بفرست، بقیه‌ش با من!</i>",
 "menu_help": "📥 راهنمای استفاده",
 "menu_about": "ℹ️ درباره ربات",
 "menu_lang": "🌐 تغییر زبان",
 "help_text": "📥 <b>راهنمای استفاده</b>\n\n1️⃣ لینک رو کپی کن\n2️⃣ همین‌جا بفرست\n3️⃣ نوع دانلود رو انتخاب کن (🎬 ویدیو یا 🎵 MP3)\n4️⃣ صبر کن ✅\n\n🖼 اگه لینک عکس باشه، خودکار برات می‌فرستم\n⚠️ فایل‌های بالای ۴۹ مگابایت با <b>لینک دانلود مستقیم</b> موقت داده میشن 🔗",
 "about_text": "ℹ️ <b>درباره ربات</b>\n\n🤖 دانلودر یونیورسال — نسخه ۲.۰\n⚙️ موتورها: yt-dlp / gallery-dl / SmartGrab\n🗄 دیتابیس: SQLite لوکال\n☁️ فایل‌های حجیم: لینک مستقیم Cloudflare\n🌐 زبان‌ها: فارسی، English، العربية، Türkçe، Русский\n\n💡 بدون محدودیت — برای همه آزاده",
 "ask_type": "🔗 <b>لینک دریافت شد!</b>\n\nنوع دانلود رو انتخاب کن:",
 "btn_video": "🎬 ویدیو (MP4)",
 "btn_audio": "🎵 صوت (MP3)",
 "btn_cancel": "❌ انصراف",
 "downloading": "⏳ <b>در حال دانلود...</b>\n{title}\n\n▫️ پیشرفت: {pct}",
 "uploading": "📤 <b>در حال آپلود به تلگرام...</b>",
 "done": "✅ <b>تمام شد!</b>",
 "photo_done": "🖼 {n} عکس برات اومد!",
 "tunnel_making": "☁️ فایل بزرگه ({size} مگ)!\nدارم یه <b>لینک دانلود مستقیم</b> می‌سازم...",
 "tunnel_ready": "🔗 <b>لینک دانلود مستقیمت آماده‌ست:</b>\n\n<code>{url}</code>\n\n📖 <b>چطور دانلودش کنم؟</b>\n1️⃣ روی لینک بالا بزن — مرورگر گوشیت باز میشه و دانلود خودکار شروع میشه\n2️⃣ اگه شروع نشد: لینک رو نگه دار و <b>کپی</b> کن، بعد توی مرورگر (Chrome/Safari) پیست کن و برو\n3️⃣ روی کامپیوتر هم می‌تونی با IDM یا wget دانلودش کنی\n\n⚠️ <i>لینک موقتیه — تا ۲ ساعت و تا وقتی ربات روشنه فعاله.</i>",
 "no_url": "🤔 لینکی پیدا نکردم! یه لینک معتبر بفرست.",
 "unsupported": "❌ متأسفانه نتونستم از این لینک چیزی دانلود کنم. یه لینک دیگه امتحان کن.",
 "error": "❌ خطا: {err}",
 "cancelled": "❌ لغو شد.",
 "working": "⏳ یه دانلود دیگه‌ت در حال انجامه، صبر کن تموم شه...",
 "back": "🔙 برگشت",
},
"en": {
 "choose_lang": "🌐 <b>Welcome!</b>\n\nPlease choose your language:",
 "welcome": "👋 <b>Hey {name}!</b>\n\nI'm a <b>universal downloader</b> 🤖\nJust send a link from:\n\n▫️ YouTube (videos / Shorts)\n▫️ Pinterest (pin.it)\n▫️ Instagram / TikTok / X\n▫️ +1000 more sites\n\n🖼 Photos are supported too!\n📎 <i>Send the link — I handle the rest!</i>",
 "menu_help": "📥 How to use",
 "menu_about": "ℹ️ About",
 "menu_lang": "🌐 Language",
 "help_text": "📥 <b>How to use</b>\n\n1️⃣ Copy a link\n2️⃣ Send it here\n3️⃣ Choose type (🎬 Video or 🎵 MP3)\n4️⃣ Wait ✅\n\n🖼 Photo links are sent automatically\n⚠️ Files over 49 MB come as a temporary <b>direct download link</b> 🔗",
 "about_text": "ℹ️ <b>About</b>\n\n🤖 Universal Downloader — v2.0\n⚙️ Engines: yt-dlp / gallery-dl / SmartGrab\n🗄 Database: local SQLite\n☁️ Large files: Cloudflare direct link\n🌐 Languages: فارسی, English, العربية, Türkçe, Русский\n\n💡 Free for everyone",
 "ask_type": "🔗 <b>Link received!</b>\n\nChoose download type:",
 "btn_video": "🎬 Video (MP4)",
 "btn_audio": "🎵 Audio (MP3)",
 "btn_cancel": "❌ Cancel",
 "downloading": "⏳ <b>Downloading...</b>\n{title}\n\n▫️ Progress: {pct}",
 "uploading": "📤 <b>Uploading to Telegram...</b>",
 "done": "✅ <b>Done!</b>",
 "photo_done": "🖼 Got {n} photo(s) for you!",
 "tunnel_making": "☁️ Big file ({size} MB)!\nCreating a <b>direct download link</b>...",
 "tunnel_ready": "🔗 <b>Your direct download link:</b>\n\n<code>{url}</code>\n\n📖 <b>How to download?</b>\n1️⃣ Tap the link — your browser opens and download starts automatically\n2️⃣ If not: long-press & <b>copy</b> the link, paste it into Chrome/Safari\n3️⃣ On PC you can use IDM or wget\n\n⚠️ <i>Temporary link — valid for 2 hours while the bot is online.</i>",
 "no_url": "🤔 No link found! Send a valid URL.",
 "unsupported": "❌ Sorry, I couldn't download anything from this link. Try another one.",
 "error": "❌ Error: {err}",
 "cancelled": "❌ Cancelled.",
 "working": "⏳ Another download of yours is running, please wait...",
 "back": "🔙 Back",
},
"ar": {
 "choose_lang": "🌐 <b>أهلاً بك!</b>\n\nاختر لغتك من فضلك:",
 "welcome": "👋 <b>مرحباً {name}!</b>\n\nأنا <b>بوت تحميل شامل</b> 🤖\nأرسل رابطاً من:\n\n▫️ يوتيوب (فيديو / Shorts)\n▫️ Pinterest (pin.it)\n▫️ Instagram / TikTok / X\n▫️ +1000 موقع آخر\n\n🖼 الصور مدعومة أيضاً!\n📎 <i>أرسل الرابط فقط!</i>",
 "menu_help": "📥 طريقة الاستخدام",
 "menu_about": "ℹ️ حول البوت",
 "menu_lang": "🌐 تغيير اللغة",
 "help_text": "📥 <b>طريقة الاستخدام</b>\n\n1️⃣ انسخ الرابط\n2️⃣ أرسله هنا\n3️⃣ اختر النوع (🎬 فيديو أو 🎵 MP3)\n4️⃣ انتظر ✅\n\n🖼 روابط الصور تُرسل تلقائياً\n⚠️ الملفات أكبر من 49 ميغا تأتي كـ<b>رابط تحميل مباشر</b> مؤقت 🔗",
 "about_text": "ℹ️ <b>حول البوت</b>\n\n🤖 محمّل شامل — الإصدار 2.0\n⚙️ المحركات: yt-dlp / gallery-dl / SmartGrab\n🗄 قاعدة بيانات: SQLite محلية\n☁️ الملفات الكبيرة: رابط Cloudflare مباشر\n🌐 اللغات: فارسی, English, العربية, Türkçe, Русский\n\n💡 مجاني للجميع",
 "ask_type": "🔗 <b>تم استلام الرابط!</b>\n\nاختر نوع التحميل:",
 "btn_video": "🎬 فيديو (MP4)",
 "btn_audio": "🎵 صوت (MP3)",
 "btn_cancel": "❌ إلغاء",
 "downloading": "⏳ <b>جارٍ التحميل...</b>\n{title}\n\n▫️ التقدم: {pct}",
 "uploading": "📤 <b>جارٍ الرفع إلى تيليغرام...</b>",
 "done": "✅ <b>تم!</b>",
 "photo_done": "🖼 وصلتك {n} صورة!",
 "tunnel_making": "☁️ الملف كبير ({size} ميغا)!\nجارٍ إنشاء <b>رابط تحميل مباشر</b>...",
 "tunnel_ready": "🔗 <b>رابط التحميل المباشر:</b>\n\n<code>{url}</code>\n\n📖 <b>كيف أحمّل؟</b>\n1️⃣ اضغط على الرابط — يفتح المتصفح ويبدأ التحميل تلقائياً\n2️⃣ إذا لم يبدأ: انسخ الرابط والصقه في المتصفح\n3️⃣ على الكمبيوتر يمكنك استخدام IDM أو wget\n\n⚠️ <i>رابط مؤقت — صالح لساعتين ما دام البوت يعمل.</i>",
 "no_url": "🤔 لم أجد رابطاً! أرسل رابطاً صحيحاً.",
 "unsupported": "❌ عذراً، لم أستطع التحميل من هذا الرابط. جرّب رابطاً آخر.",
 "error": "❌ خطأ: {err}",
 "cancelled": "❌ تم الإلغاء.",
 "working": "⏳ لديك تحميل آخر قيد التنفيذ، انتظر من فضلك...",
 "back": "🔙 رجوع",
},
"tr": {
 "choose_lang": "🌐 <b>Hoş geldin!</b>\n\nLütfen dilini seç:",
 "welcome": "👋 <b>Merhaba {name}!</b>\n\nBen <b>evrensel bir indirici botuyum</b> 🤖\nŞu sitelerden link gönder:\n\n▫️ YouTube (video / Shorts)\n▫️ Pinterest (pin.it)\n▫️ Instagram / TikTok / X\n▫️ +1000 site daha\n\n🖼 Fotoğraflar da destekleniyor!\n📎 <i>Linki gönder, gerisini ben hallederim!</i>",
 "menu_help": "📥 Nasıl kullanılır",
 "menu_about": "ℹ️ Hakkında",
 "menu_lang": "🌐 Dil değiştir",
 "help_text": "📥 <b>Nasıl kullanılır</b>\n\n1️⃣ Linki kopyala\n2️⃣ Buraya gönder\n3️⃣ Türü seç (🎬 Video veya 🎵 MP3)\n4️⃣ Bekle ✅\n\n🖼 Fotoğraf linkleri otomatik gönderilir\n⚠️ 49 MB üzeri dosyalar geçici <b>direkt indirme linki</b> ile verilir 🔗",
 "about_text": "ℹ️ <b>Hakkında</b>\n\n🤖 Evrensel İndirici — v2.0\n⚙️ Motorlar: yt-dlp / gallery-dl / SmartGrab\n🗄 Veritabanı: yerel SQLite\n☁️ Büyük dosyalar: Cloudflare direkt link\n🌐 Diller: فارسی, English, العربية, Türkçe, Русский\n\n💡 Herkes için ücretsiz",
 "ask_type": "🔗 <b>Link alındı!</b>\n\nİndirme türünü seç:",
 "btn_video": "🎬 Video (MP4)",
 "btn_audio": "🎵 Ses (MP3)",
 "btn_cancel": "❌ İptal",
 "downloading": "⏳ <b>İndiriliyor...</b>\n{title}\n\n▫️ İlerleme: {pct}",
 "uploading": "📤 <b>Telegram'a yükleniyor...</b>",
 "done": "✅ <b>Tamamlandı!</b>",
 "photo_done": "🖼 {n} fotoğraf geldi!",
 "tunnel_making": "☁️ Dosya büyük ({size} MB)!\n<b>Direkt indirme linki</b> oluşturuluyor...",
 "tunnel_ready": "🔗 <b>Direkt indirme linkin:</b>\n\n<code>{url}</code>\n\n📖 <b>Nasıl indirilir?</b>\n1️⃣ Linke dokun — tarayıcı açılır ve indirme otomatik başlar\n2️⃣ Başlamazsa: linki <b>kopyala</b>, Chrome/Safari'ye yapıştır\n3️⃣ PC'de IDM veya wget kullanabilirsin\n\n⚠️ <i>Geçici link — bot açıkken 2 saat geçerli.</i>",
 "no_url": "🤔 Link bulunamadı! Geçerli bir URL gönder.",
 "unsupported": "❌ Üzgünüm, bu linkten bir şey indiremedim. Başka bir link dene.",
 "error": "❌ Hata: {err}",
 "cancelled": "❌ İptal edildi.",
 "working": "⏳ Başka bir indirmen sürüyor, lütfen bekle...",
 "back": "🔙 Geri",
},
"ru": {
 "choose_lang": "🌐 <b>Добро пожаловать!</b>\n\nВыберите язык:",
 "welcome": "👋 <b>Привет, {name}!</b>\n\nЯ <b>универсальный загрузчик</b> 🤖\nПришли ссылку с:\n\n▫️ YouTube (видео / Shorts)\n▫️ Pinterest (pin.it)\n▫️ Instagram / TikTok / X\n▫️ +1000 других сайтов\n\n🖼 Фото тоже поддерживаются!\n📎 <i>Просто отправь ссылку!</i>",
 "menu_help": "📥 Как пользоваться",
 "menu_about": "ℹ️ О боте",
 "menu_lang": "🌐 Сменить язык",
 "help_text": "📥 <b>Как пользоваться</b>\n\n1️⃣ Скопируй ссылку\n2️⃣ Отправь сюда\n3️⃣ Выбери тип (🎬 Видео или 🎵 MP3)\n4️⃣ Подожди ✅\n\n🖼 Фото-ссылки отправляются автоматически\n⚠️ Файлы больше 49 МБ приходят <b>прямой ссылкой</b> 🔗",
 "about_text": "ℹ️ <b>О боте</b>\n\n🤖 Универсальный загрузчик — v2.0\n⚙️ Движки: yt-dlp / gallery-dl / SmartGrab\n🗄 База: локальный SQLite\n☁️ Большие файлы: прямая ссылка Cloudflare\n🌐 Языки: فارسی, English, العربية, Türkçe, Русский\n\n💡 Бесплатно для всех",
 "ask_type": "🔗 <b>Ссылка получена!</b>\n\nВыбери тип загрузки:",
 "btn_video": "🎬 Видео (MP4)",
 "btn_audio": "🎵 Аудио (MP3)",
 "btn_cancel": "❌ Отмена",
 "downloading": "⏳ <b>Загрузка...</b>\n{title}\n\n▫️ Прогресс: {pct}",
 "uploading": "📤 <b>Загружаю в Telegram...</b>",
 "done": "✅ <b>Готово!</b>",
 "photo_done": "🖼 Получено фото: {n}!",
 "tunnel_making": "☁️ Файл большой ({size} МБ)!\nСоздаю <b>прямую ссылку</b>...",
 "tunnel_ready": "🔗 <b>Твоя прямая ссылка:</b>\n\n<code>{url}</code>\n\n📖 <b>Как скачать?</b>\n1️⃣ Нажми на ссылку — откроется браузер и загрузка начнётся сама\n2️⃣ Если нет: <b>скопируй</b> ссылку и вставь в Chrome/Safari\n3️⃣ На ПК можно использовать IDM или wget\n\n⚠️ <i>Временная ссылка — действует 2 часа, пока бот онлайн.</i>",
 "no_url": "🤔 Ссылка не найдена! Отправь корректный URL.",
 "unsupported": "❌ Увы, с этой ссылки ничего скачать не удалось. Попробуй другую.",
 "error": "❌ Ошибка: {err}",
 "cancelled": "❌ Отменено.",
 "working": "⏳ У тебя уже идёт загрузка, подожди...",
 "back": "🔙 Назад",
},
}

def t(uid, key, **kw):
    lang = get_lang(uid) or "fa"
    return T.get(lang, T["fa"])[key].format(**kw)

# ---------------- Keyboards ----------------
def kb_lang():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇮🇷 فارسی", callback_data="setlang_fa"),
         InlineKeyboardButton("🇬🇧 English", callback_data="setlang_en")],
        [InlineKeyboardButton("🇸🇦 العربية", callback_data="setlang_ar"),
         InlineKeyboardButton("🇹🇷 Türkçe", callback_data="setlang_tr")],
        [InlineKeyboardButton("🇷🇺 Русский", callback_data="setlang_ru")],
    ])

def kb_main(uid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(uid, "menu_help"), callback_data="menu_help")],
        [InlineKeyboardButton(t(uid, "menu_about"), callback_data="menu_about"),
         InlineKeyboardButton(t(uid, "menu_lang"), callback_data="menu_lang")],
    ])

def kb_type(uid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(uid, "btn_video"), callback_data="dl_video"),
         InlineKeyboardButton(t(uid, "btn_audio"), callback_data="dl_audio")],
        [InlineKeyboardButton(t(uid, "btn_cancel"), callback_data="dl_cancel")],
    ])

# ---------------- Engines ----------------
URL_RE = re.compile(r"https?://\S+", re.I)
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

def is_direct_media(url: str) -> str | None:
    """Return 'image'/'video'/'audio' if URL points directly to a media file."""
    path = url.split("?")[0].lower()
    ext = os.path.splitext(path)[1]
    if ext in IMG_EXTS:
        return "image"
    if ext in (".mp4", ".mkv", ".webm", ".mov"):
        return "video"
    if ext in (".mp3", ".m4a", ".ogg", ".wav", ".flac"):
        return "audio"
    return None

def snapshot() -> set:
    return {p.name for p in DL_DIR.iterdir()}

def new_files(before: set) -> list[Path]:
    return sorted([p for p in DL_DIR.iterdir()
                   if p.name not in before and p.suffix != ".part"],
                  key=lambda p: p.stat().st_mtime)

def download_http(url: str, dest: Path, timeout=120) -> Path | None:
    """Plain HTTP download (direct engine)."""
    try:
        with requests.get(url, headers={"User-Agent": UA}, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1024 * 256):
                    f.write(chunk)
        return dest if dest.stat().st_size > 0 else None
    except Exception as e:
        log.warning("direct dl failed: %s", e)
        dest.unlink(missing_ok=True)
        return None

def engine_direct(url: str) -> Path | None:
    ext = os.path.splitext(url.split("?")[0])[1] or ".bin"
    dest = DL_DIR / f"direct_{int(time.time()*1000)}{ext}"
    return download_http(url, dest)

def engine_ytdlp(url: str, kind: str, hook) -> tuple[Path | None, str]:
    """Engine 1: yt-dlp. Returns (file, title)."""
    opts = {
        "outtmpl": str(DL_DIR / "%(id)s.%(ext)s"),
        "noplaylist": True, "quiet": True, "no_warnings": True,
        "progress_hooks": [hook], "socket_timeout": 30, "retries": 3,
        "http_headers": {"User-Agent": UA},
    }
    if kind == "audio":
        opts.update({"format": "bestaudio/best",
                     "postprocessors": [{"key": "FFmpegExtractAudio",
                                         "preferredcodec": "mp3",
                                         "preferredquality": "192"}]})
    else:
        opts.update({"format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/"
                               "best[height<=720][ext=mp4]/best[height<=720]/best",
                     "merge_output_format": "mp4"})
    before = snapshot()
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url, download=True)
    title = (info.get("title") or "file")[:60]
    vid_id = info.get("id")
    if vid_id:
        for f in DL_DIR.glob(f"{vid_id}.*"):
            if f.suffix != ".part":
                return f, title
    fresh = new_files(before)          # BUGFIX v1: id could be None/odd
    return (fresh[-1] if fresh else None), title

def engine_gallerydl(url: str) -> list[Path]:
    """Engine 2: gallery-dl for photo posts (Instagram, Pinterest, ...)."""
    if not shutil.which("gallery-dl"):
        return []
    before = snapshot()
    try:
        subprocess.run(["gallery-dl", "-d", str(DL_DIR), "--no-mtime", url],
                       capture_output=True, timeout=180)
    except Exception as e:
        log.warning("gallery-dl failed: %s", e)
        return []
    imgs = [p for p in new_files(before) if p.suffix.lower() in IMG_EXTS]
    return imgs[:MAX_PHOTOS]

def engine_smartgrab(url: str, want: str) -> tuple[list[Path], str]:
    """Engine 3 (custom): SmartGrab — fetch the page HTML, sniff og:video /
    og:image / <video src> / JSON contentUrl and download the media directly."""
    found: list[Path] = []
    kind = "file"
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=25, allow_redirects=True)
        html = r.text
    except Exception as e:
        log.warning("smartgrab fetch failed: %s", e)
        return found, kind

    pats_video = [
        r'property=["\']og:video(?::secure_url|:url)?["\']\s+content=["\']([^"\']+)["\']',
        r'content=["\']([^"\']+)["\']\s+property=["\']og:video',
        r'"contentUrl"\s*:\s*"([^"]+\.(?:mp4|webm|mov)[^"]*)"',
        r'<video[^>]+src=["\']([^"\']+)["\']',
        r'"playbackUrls"\s*:\s*\[\s*\{[^}]*"url"\s*:\s*"([^"]+)"',
    ]
    pats_image = [
        r'property=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
        r'content=["\']([^"\']+)["\']\s+property=["\']og:image',
        r'name=["\']twitter:image["\']\s+content=["\']([^"\']+)["\']',
        r'"image"\s*:\s*"(https?://[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"',
    ]
    pats = pats_video + pats_image if want == "video" else pats_image + pats_video

    for pat in pats:
        m = re.search(pat, html, re.I)
        if not m:
            continue
        media = m.group(1).replace("\\u0026", "&").replace("\\/", "/")
        media = urljoin(url, media)
        ext = os.path.splitext(media.split("?")[0])[1].lower() or ".bin"
        dest = DL_DIR / f"sg_{int(time.time()*1000)}{ext}"
        got = download_http(media, dest)
        if got:
            if ext in IMG_EXTS:
                kind = "image"
            elif ext in (".mp4", ".webm", ".mov", ".mkv"):
                kind = "video"
            found.append(got)
            break
    return found, kind

# ---------------- Cloudflare tunnel ----------------
_tunnel_lock = threading.Lock()
_tunnel_url = None
_tunnel_proc = None
_http_started = False

def ensure_http_server():
    global _http_started
    if _http_started:
        return
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(DL_DIR))
    srv = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _http_started = True
    log.info("HTTP file server on 127.0.0.1:%s", HTTP_PORT)

def get_tunnel_url(timeout=30) -> str | None:
    global _tunnel_url, _tunnel_proc
    with _tunnel_lock:
        if _tunnel_url and _tunnel_proc and _tunnel_proc.poll() is None:
            return _tunnel_url                    # reuse only if alive (bugfix)
        _tunnel_url = None
        if not shutil.which("cloudflared"):
            log.error("cloudflared not installed!")
            return None
        ensure_http_server()
        _tunnel_proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{HTTP_PORT}",
             "--no-autoupdate"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        start = time.time()
        while time.time() - start < timeout:
            line = _tunnel_proc.stderr.readline()
            if not line:
                continue
            m = re.search(r"https://[a-z0-9\-]+\.trycloudflare\.com", line)
            if m:
                _tunnel_url = m.group(0)
                log.info("Cloudflare tunnel: %s", _tunnel_url)
                return _tunnel_url
        _tunnel_proc.kill()
        return None

def janitor():
    """Delete files older than FILE_TTL from downloads dir."""
    while True:
        time.sleep(1800)
        try:
            now = time.time()
            for p in DL_DIR.iterdir():
                if now - p.stat().st_mtime > FILE_TTL:
                    p.unlink(missing_ok=True)
        except Exception:
            pass

# ---------------- Handlers ----------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u)
    if get_lang(u.id):
        # language remembered -> straight to main menu
        await update.message.reply_text(
            t(u.id, "welcome", name=u.first_name or ""),
            parse_mode="HTML", reply_markup=kb_main(u.id))
    else:
        await update.message.reply_text(T["fa"]["choose_lang"],
                                        parse_mode="HTML", reply_markup=kb_lang())

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    u = q.from_user
    uid = u.id
    upsert_user(u)
    data = q.data
    await q.answer()

    if data.startswith("setlang_"):
        set_lang(uid, data.split("_")[1])
        await q.edit_message_text(t(uid, "welcome", name=u.first_name or ""),
                                  parse_mode="HTML", reply_markup=kb_main(uid))
        return

    back_kb = lambda: InlineKeyboardMarkup(
        [[InlineKeyboardButton(t(uid, "back"), callback_data="menu_home")]])
    if data == "menu_help":
        await q.edit_message_text(t(uid, "help_text"), parse_mode="HTML", reply_markup=back_kb())
        return
    if data == "menu_about":
        await q.edit_message_text(t(uid, "about_text"), parse_mode="HTML", reply_markup=back_kb())
        return
    if data == "menu_lang":
        await q.edit_message_text(T["fa"]["choose_lang"], parse_mode="HTML", reply_markup=kb_lang())
        return
    if data == "menu_home":
        await q.edit_message_text(t(uid, "welcome", name=u.first_name or ""),
                                  parse_mode="HTML", reply_markup=kb_main(uid))
        return

    if data == "dl_cancel":
        ctx.user_data.pop("url", None)
        await q.edit_message_text(t(uid, "cancelled"))
        return

    if data in ("dl_video", "dl_audio"):
        url = ctx.user_data.get("url")
        if not url:
            await q.edit_message_text(t(uid, "no_url"))
            return
        lock = ulock(uid)
        if lock.locked():
            await q.answer(t(uid, "working"), show_alert=True)
            return
        kind = "video" if data == "dl_video" else "audio"
        async with lock:
            try:
                await run_download(q, uid, url, kind)
            finally:
                ctx.user_data.pop("url", None)
        return

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u)
    if not get_lang(u.id):
        await update.message.reply_text(T["fa"]["choose_lang"],
                                        parse_mode="HTML", reply_markup=kb_lang())
        return
    m = URL_RE.search(update.message.text or "")
    if not m:
        await update.message.reply_text(t(u.id, "no_url"))
        return
    bump(u.id, "links")
    ctx.user_data["url"] = m.group(0)
    await update.message.reply_text(t(u.id, "ask_type"), parse_mode="HTML",
                                    reply_markup=kb_type(u.id))

# ---------------- Download pipeline ----------------
async def safe_edit(bot, chat_id, msg_id, text):
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id,
                                    parse_mode="HTML")
    except Exception:
        pass

async def run_download(q, uid: int, url: str, kind: str):
    bot = q.get_bot()
    status = await q.edit_message_text(t(uid, "downloading", title=url[:60], pct="..."),
                                       parse_mode="HTML")
    chat_id, msg_id = status.chat_id, status.message_id
    loop = asyncio.get_running_loop()
    state = {"last": 0.0, "title": url[:60]}

    def hook(d):
        if d.get("status") == "downloading":
            pct = (d.get("_percent_str") or "").strip()
            if pct and time.time() - state["last"] > 3:
                state["last"] = time.time()
                asyncio.run_coroutine_threadsafe(
                    safe_edit(bot, chat_id, msg_id,
                              t(uid, "downloading", title=state["title"], pct=pct)),
                    loop)

    media_path, media_kind, title = None, kind, state["title"]
    photos: list[Path] = []

    try:
        # Engine 0: direct media URL
        dm = is_direct_media(url)
        if dm:
            media_path = await loop.run_in_executor(EXEC, engine_direct, url)
            media_kind = dm
        else:
            # Engine 1: yt-dlp
            try:
                media_path, title = await loop.run_in_executor(
                    EXEC, engine_ytdlp, url, kind, hook)
            except Exception as e:
                log.info("yt-dlp failed (%s), trying next engines", e)
                media_path = None

            # Engine 2: gallery-dl (photos)
            if not media_path:
                await safe_edit(bot, chat_id, msg_id,
                                t(uid, "downloading", title=title, pct="📷 engine 2..."))
                photos = await loop.run_in_executor(EXEC, engine_gallerydl, url)
                if photos:
                    media_kind = "image"

            # Engine 3: SmartGrab custom scraper
            if not media_path and not photos:
                await safe_edit(bot, chat_id, msg_id,
                                t(uid, "downloading", title=title, pct="🧠 SmartGrab..."))
                got, gkind = await loop.run_in_executor(EXEC, engine_smartgrab, url, kind)
                if got:
                    if gkind == "image":
                        photos = got
                        media_kind = "image"
                    else:
                        media_path = got[0]
                        media_kind = gkind
    except Exception as e:
        log_dl(uid, url, kind, 0, "fail")
        await status.edit_text(t(uid, "error", err=str(e)[:300]))
        return

    # ---- nothing found ----
    if not media_path and not photos:
        log_dl(uid, url, kind, 0, "fail")
        await status.edit_text(t(uid, "unsupported"), parse_mode="HTML")
        return

    # ---- send photos ----
    if photos:
        try:
            for i in range(0, len(photos), MAX_PHOTOS):
                batch = photos[i:i + MAX_PHOTOS]
                if len(batch) == 1:
                    with open(batch[0], "rb") as fh:
                        await bot.send_photo(chat_id, fh,
                                             caption=t(uid, "photo_done", n=len(photos)),
                                             parse_mode="HTML")
                else:
                    media = []
                    fhs = []
                    for p in batch:
                        fh = open(p, "rb"); fhs.append(fh)
                        media.append(InputMediaPhoto(fh))
                    await bot.send_media_group(chat_id, media)
                    for fh in fhs:
                        fh.close()
            bump(uid, "photos"); bump(uid, "downloads")
            log_dl(uid, url, "image", sum(p.stat().st_size for p in photos))
            await status.edit_text(t(uid, "photo_done", n=len(photos)), parse_mode="HTML")
        except Exception as e:
            await status.edit_text(t(uid, "error", err=str(e)[:300]))
        finally:
            for p in photos:
                p.unlink(missing_ok=True)
        return

    size = media_path.stat().st_size
    log_dl(uid, url, media_kind, size)

    # ---- fits in Telegram ----
    if size <= MAX_TG_SIZE:
        await status.edit_text(t(uid, "uploading"), parse_mode="HTML")
        try:
            with open(media_path, "rb") as fh:
                if media_kind == "audio" or media_path.suffix == ".mp3":
                    await bot.send_audio(chat_id, fh, title=title,
                                         caption=t(uid, "done"), parse_mode="HTML")
                    bump(uid, "audios")
                elif media_path.suffix in (".mp4", ".mkv", ".webm", ".mov"):
                    await bot.send_video(chat_id, fh, caption=t(uid, "done"),
                                         parse_mode="HTML", supports_streaming=True)
                    bump(uid, "videos")
                elif media_path.suffix.lower() in IMG_EXTS:
                    await bot.send_photo(chat_id, fh, caption=t(uid, "done"),
                                         parse_mode="HTML")
                    bump(uid, "photos")
                else:
                    await bot.send_document(chat_id, fh, caption=t(uid, "done"),
                                            parse_mode="HTML")
            bump(uid, "downloads")
            await status.delete()
        except Exception as e:
            await status.edit_text(t(uid, "error", err=str(e)[:300]))
        finally:
            media_path.unlink(missing_ok=True)
        return

    # ---- big file -> Cloudflare direct link (no splitting) ----
    bump(uid, "downloads")
    if media_kind == "audio":
        bump(uid, "audios")
    else:
        bump(uid, "videos")
    await status.edit_text(t(uid, "tunnel_making", size=f"{size/1048576:.1f}"),
                           parse_mode="HTML")
    base = await loop.run_in_executor(EXEC, get_tunnel_url)
    if not base:
        await status.edit_text(t(uid, "error", err="cloudflared not available"))
        return
    link = f"{base}/{quote(media_path.name)}"
    await status.edit_text(t(uid, "tunnel_ready", url=link), parse_mode="HTML")
    # file is kept for the tunnel; janitor() removes it after FILE_TTL

# ---------------- Admin dashboard (port 2000) ----------------
DASH_HTML = Template("""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>📊 پنل مدیریت دانلودر</title>
<meta http-equiv="refresh" content="30">
<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:Tahoma,Vazirmatn,sans-serif}
body{min-height:100vh;background:linear-gradient(135deg,#0f2027,#203a43,#2c5364);color:#fff;padding:24px}
h1{text-align:center;margin-bottom:6px;font-size:26px}
.sub{text-align:center;opacity:.6;margin-bottom:28px;font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:16px;max-width:1000px;margin:0 auto 30px}
.card{background:rgba(255,255,255,.08);backdrop-filter:blur(14px);border:1px solid rgba(255,255,255,.15);
border-radius:18px;padding:20px;text-align:center;box-shadow:0 8px 24px rgba(0,0,0,.3)}
.card .num{font-size:30px;font-weight:700;margin-top:6px}
.card .lbl{opacity:.75;font-size:13px}
table{width:100%;max-width:1100px;margin:0 auto 30px;border-collapse:collapse;
background:rgba(255,255,255,.07);backdrop-filter:blur(14px);border-radius:16px;overflow:hidden}
th,td{padding:11px 10px;text-align:center;font-size:13px;border-bottom:1px solid rgba(255,255,255,.08)}
th{background:rgba(255,255,255,.12);font-size:12px}
tr:hover td{background:rgba(255,255,255,.05)}
.badge{padding:3px 10px;border-radius:20px;font-size:11px}
.ok{background:#16a34a55;color:#86efac}.fail{background:#dc262655;color:#fca5a5}
h2{max-width:1100px;margin:0 auto 12px;font-size:18px}
a{color:#7dd3fc}
.url{max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:inline-block;vertical-align:middle}
</style></head>
<body>
<h1>📊 پنل مدیریت بات دانلودر</h1>
<div class="sub">به‌روزرسانی خودکار هر ۳۰ ثانیه • $now</div>
<div class="cards">
  <div class="card">👥<div class="num">$users</div><div class="lbl">کاربر</div></div>
  <div class="card">🔗<div class="num">$links</div><div class="lbl">لینک دریافتی</div></div>
  <div class="card">✅<div class="num">$downloads</div><div class="lbl">دانلود موفق</div></div>
  <div class="card">🎬<div class="num">$videos</div><div class="lbl">ویدیو</div></div>
  <div class="card">🖼<div class="num">$photos</div><div class="lbl">عکس</div></div>
  <div class="card">🎵<div class="num">$audios</div><div class="lbl">صوت</div></div>
</div>
<h2>👤 کاربران</h2>
<table><tr><th>#</th><th>آیدی عددی</th><th>نام</th><th>یوزرنیم</th><th>زبان</th>
<th>🔗 لینک</th><th>✅ دانلود</th><th>🎬</th><th>🖼</th><th>🎵</th><th>عضویت</th></tr>
$user_rows
</table>
<h2>🕓 آخرین دانلودها</h2>
<table><tr><th>#</th><th>کاربر</th><th>لینک</th><th>نوع</th><th>حجم</th><th>وضعیت</th><th>زمان</th></tr>
$hist_rows
</table>
</body></html>""")

def render_dash() -> bytes:
    con = db()
    u = con.execute("""SELECT user_id,first_name,username,lang,links,downloads,
                       videos,photos,audios,joined FROM users ORDER BY downloads DESC""").fetchall()
    h = con.execute("""SELECT id,user_id,url,kind,size,status,at FROM history
                       ORDER BY id DESC LIMIT 25""").fetchall()
    totals = con.execute("""SELECT COUNT(*),COALESCE(SUM(links),0),COALESCE(SUM(downloads),0),
                            COALESCE(SUM(videos),0),COALESCE(SUM(photos),0),COALESCE(SUM(audios),0)
                            FROM users""").fetchone()
    con.close()

    LANG_FLAG = {"fa":"🇮🇷","en":"🇬🇧","ar":"🇸🇦","tr":"🇹🇷","ru":"🇷🇺"}
    urows = "".join(
        f"<tr><td>{i}</td><td><code>{r[0]}</code></td><td>{r[1] or '-'}</td>"
        f"<td>{'@'+r[2] if r[2] else '-'}</td><td>{LANG_FLAG.get(r[3],'❓')} {r[3] or '-'}</td>"
        f"<td>{r[4]}</td><td>{r[5]}</td><td>{r[6]}</td><td>{r[7]}</td><td>{r[8]}</td>"
        f"<td>{r[9]}</td></tr>" for i, r in enumerate(u, 1))
    hrows = "".join(
        f"<tr><td>{r[0]}</td><td><code>{r[1]}</code></td>"
        f"<td><a href='{r[2]}' target='_blank'><span class='url'>{r[2]}</span></a></td>"
        f"<td>{r[3]}</td><td>{r[4]/1048576:.1f} MB</td>"
        f"<td><span class='badge {r[5]}'>{'موفق' if r[5]=='ok' else 'ناموفق'}</span></td>"
        f"<td>{r[6]}</td></tr>" for r in h)

    return DASH_HTML.substitute(
        now=time.strftime("%Y-%m-%d %H:%M"),
        users=totals[0], links=totals[1], downloads=totals[2],
        videos=totals[3], photos=totals[4], audios=totals[5],
        user_rows=urows or "<tr><td colspan='11'>هنوز کاربری نیست</td></tr>",
        hist_rows=hrows or "<tr><td colspan='7'>هنوز دانلودی ثبت نشده</td></tr>",
    ).encode("utf-8")

class DashHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = render_dash()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass

def start_dashboard():
    srv = ThreadingHTTPServer(("0.0.0.0", DASH_PORT), DashHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Dashboard on http://0.0.0.0:%s", DASH_PORT)

# ---------------- Main ----------------
def main():
    init_db()
    for p in DL_DIR.iterdir():
        p.unlink(missing_ok=True)
    threading.Thread(target=janitor, daemon=True).start()
    start_dashboard()
    app = (ApplicationBuilder().token(BOT_TOKEN)
           .concurrent_updates(True)            # faster: handle users in parallel
           .connect_timeout(20).read_timeout(60).write_timeout(60)
           .build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Bot v2 started 🚀")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
