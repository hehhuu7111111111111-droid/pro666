# HAMZA AUTOPOST PREMIUM
# Requirements: pip install pyTelegramBotAPI telethon
# Fill BOT_TOKEN, API_ID, API_HASH, ADMIN_ID before running.

import os
import time
import asyncio
import sqlite3
import threading
from concurrent.futures import TimeoutError as FutureTimeout

import telebot
from telebot import types
from telethon import TelegramClient, utils

# ===================== CONFIG =====================
BOT_TOKEN = "PUT_NEW_BOT_TOKEN_HERE"
API_ID = 0  # Your numeric Telegram API ID
API_HASH = "PUT_API_HASH_HERE"
ADMIN_ID = 5242898647  # Telegram user ID allowed to control this bot
PHONE = ""  # Optional: +964...; leave blank to enter it when prompted
# ==================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "hamza_autopost.sqlite3")
SESSION_PATH = os.path.join(BASE_DIR, "hamza_personal_session")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "hamza_media")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

if BOT_TOKEN.startswith("PUT_") or not API_ID or API_HASH.startswith("PUT_"):
    raise SystemExit("عدّل BOT_TOKEN و API_ID و API_HASH بأعلى الملف أولاً.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True)
client = None
client_loop = None
client_ready = threading.Event()
client_error = None
paused = False
pending_setting = {}  # admin_id -> setting name awaiting numeric input

# --------------------- DATABASE ---------------------
def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables and safely migrate an older posts table."""
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL DEFAULT 'text',
            text_value TEXT,
            file_path TEXT,
            caption TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL DEFAULT 0
        )""")
        # Migration for older installations whose posts table lacks newer columns.
        existing = {row[1] for row in cur.execute("PRAGMA table_info(posts)").fetchall()}
        migrations = {
            "kind": "TEXT NOT NULL DEFAULT 'text'",
            "text_value": "TEXT",
            "file_path": "TEXT",
            "caption": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'pending'",
            "created_at": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, declaration in migrations.items():
            if column not in existing:
                cur.execute(f"ALTER TABLE posts ADD COLUMN {column} {declaration}")
        cur.execute("""CREATE TABLE IF NOT EXISTS groups (
            peer_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            username TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            added_at INTEGER NOT NULL DEFAULT 0
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS publish_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            peer_id INTEGER NOT NULL,
            result TEXT NOT NULL,
            detail TEXT,
            created_at INTEGER NOT NULL
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
        cur.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('repeat_count','1')")
        cur.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('repeat_seconds','60')")
        conn.commit()


def add_post(kind, text_value=None, file_path=None, caption=None):
    with db_connect() as conn:
        cur = conn.execute(
            "INSERT INTO posts(kind,text_value,file_path,caption,status,created_at) VALUES(?,?,?,?,?,?)",
            (kind, text_value, file_path, caption, "pending", int(time.time())),
        )
        return cur.lastrowid


def get_post(post_id):
    with db_connect() as conn:
        return conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()


def set_post_status(post_id, status):
    with db_connect() as conn:
        conn.execute("UPDATE posts SET status=? WHERE id=?", (status, post_id))
        conn.commit()


def get_groups(active_only=True):
    with db_connect() as conn:
        if active_only:
            return conn.execute("SELECT * FROM groups WHERE active=1 ORDER BY title").fetchall()
        return conn.execute("SELECT * FROM groups ORDER BY title").fetchall()


def save_group(peer_id, title, username=None):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO groups(peer_id,title,username,active,added_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(peer_id) DO UPDATE SET title=excluded.title, username=excluded.username, active=1",
            (int(peer_id), title, username, 1, int(time.time())),
        )
        conn.commit()


def log_result(post_id, peer_id, result, detail=""):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO publish_log(post_id,peer_id,result,detail,created_at) VALUES(?,?,?,?,?)",
            (post_id, int(peer_id), result, detail[:500], int(time.time())),
        )
        conn.commit()


def get_setting(key, default):
    with db_connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else str(default)


def set_setting(key, value):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        conn.commit()

# --------------------- TELETHON THREAD ---------------------
def telethon_worker():
    global client, client_loop, client_error

    async def runner():
        global client
        client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
        await client.start(phone=PHONE.strip() or None)
        me = await client.get_me()
        print(f"\nتم الاتصال بالحساب الشخصي: {me.id} (@{me.username or 'بدون معرف'})")
        try:
            synced = await sync_groups_async()
            print(f"تمت مزامنة {synced} مجموعة تلقائياً.")
        except Exception as sync_exc:
            print("تعذرت المزامنة الأولية:", sync_exc)
        client_ready.set()
        await client.run_until_disconnected()

    loop = asyncio.new_event_loop()
    client_loop = loop
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(runner())
    except Exception as exc:
        client_error = str(exc)
        print("خطأ اتصال الحساب الشخصي:", client_error)
        client_ready.set()
    finally:
        try:
            loop.close()
        except Exception:
            pass


def call_telethon(coro, timeout=180):
    if not client_loop or not client_ready.is_set() or client is None:
        raise RuntimeError("الحساب الشخصي بعده ما متصل. راجع رسالة الاتصال بالكونسول.")
    future = asyncio.run_coroutine_threadsafe(coro, client_loop)
    try:
        return future.result(timeout=timeout)
    except FutureTimeout:
        future.cancel()
        raise RuntimeError("انتهى وقت العملية؛ جرّب مرة ثانية.")


async def sync_groups_async():
    count = 0
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        # Only groups/supergroups, not broadcast channels or private chats.
        is_group = bool(getattr(dialog, "is_group", False))
        if not is_group:
            continue
        peer_id = utils.get_peer_id(entity)
        save_group(peer_id, getattr(entity, "title", dialog.name or "مجموعة"), getattr(entity, "username", None))
        count += 1
    return count


async def publish_async(post_id):
    global paused
    post = get_post(post_id)
    if not post:
        raise RuntimeError("المنشور غير موجود.")
    groups = get_groups(active_only=True)
    if not groups:
        raise RuntimeError("ماكو مجموعات مسجلة. اضغط مزامنة المجموعات أولاً.")

    # Repeat count and interval are independent saved settings. A short minimum
    # interval helps prevent accidental rapid-fire posting.
    repeat_count = max(1, min(100, int(get_setting("repeat_count", 1))))
    repeat_seconds = max(10, min(86400, int(get_setting("repeat_seconds", 60))))
    success = 0
    failed = 0
    for round_no in range(repeat_count):
        if paused:
            raise RuntimeError("تم إيقاف النشر أثناء التكرار.")
        if round_no > 0:
            # Sleep in small steps so the stop button can take effect.
            remaining = repeat_seconds
            while remaining > 0:
                if paused:
                    raise RuntimeError("تم إيقاف النشر أثناء انتظار التكرار.")
                step = min(1, remaining)
                await asyncio.sleep(step)
                remaining -= step
        for group in groups:
            if paused:
                raise RuntimeError("تم إيقاف النشر أثناء التكرار.")
            try:
                entity = await client.get_entity(int(group["peer_id"]))
                kind = post["kind"]
                path = post["file_path"]
                caption = post["caption"] or ""
                text_value = post["text_value"] or ""
                if kind == "text":
                    await client.send_message(entity, text_value)
                elif path and os.path.isfile(path):
                    await client.send_file(entity, path, caption=caption or None)
                else:
                    raise RuntimeError("ملف الوسائط غير موجود على الجهاز")
                log_result(post_id, group["peer_id"], "ok", f"تم النشر، التكرار {round_no + 1}/{repeat_count}")
                success += 1
                await asyncio.sleep(1.2)
            except Exception as exc:
                log_result(post_id, group["peer_id"], "failed", str(exc))
                failed += 1
    set_post_status(post_id, "published" if success else "failed")
    return success, failed

# --------------------- BOT UI ---------------------
def is_admin(message_or_call):
    user = message_or_call.from_user
    return bool(user and user.id == ADMIN_ID)


def main_keyboard():
    # Arabic controls stay readable; the premium Latin title uses the selected small-caps style.
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("📤 إدارة المنشورات", "📝 إرسال منشور")
    kb.add("👥 المجموعات", "🔄 مزامنة المجموعات")
    kb.add("📊 الإحصائيات", "⏸ إيقاف النشر")
    kb.add("▶️ استئناف النشر", "🔢 عدد النشر")
    kb.add("⏱️ وقت التكرار", "🔁 إعدادات التكرار")
    kb.add("ℹ️ المساعدة")
    return kb


def inline_post_keyboard(post_id):
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("🚀 نشر الآن", callback_data=f"publish:{post_id}"),
        types.InlineKeyboardButton("🗑 إلغاء", callback_data=f"cancel:{post_id}"),
    )
    return kb


def admin_only_reply(message):
    if not is_admin(message):
        bot.reply_to(message, "هذا البوت مخصص للمالك فقط.")
        return False
    return True


def create_preview(message, kind, text_value=None, file_id=None, caption=None):
    file_path = None
    if file_id:
        info = bot.get_file(file_id)
        data = bot.download_file(info.file_path)
        ext = os.path.splitext(info.file_path)[1] or ".bin"
        file_path = os.path.join(DOWNLOAD_DIR, f"post_{int(time.time()*1000)}{ext}")
        with open(file_path, "wb") as f:
            f.write(data)
    post_id = add_post(kind, text_value, file_path, caption)
    title = f"📝 <b>معاينة المنشور #{post_id}</b>\n\n"
    if kind == "text":
        body = (text_value or "")[:3500]
        bot.send_message(message.chat.id, title + body, reply_markup=inline_post_keyboard(post_id))
    else:
        bot.send_message(message.chat.id, title + "الوسائط جاهزة للنشر.\n" + (caption or "بدون وصف"), reply_markup=inline_post_keyboard(post_id))


@bot.message_handler(commands=["start", "help"])
def start(message):
    if not admin_only_reply(message):
        return
    bot.send_message(message.chat.id,
        "<b>ʜᴀᴍᴢᴀ ᴘʀᴇᴍɪᴜᴍ</b>\n\n"
        "أرسل نص أو صورة أو فيديو أو ملف حتى أجهز معاينة. بعدها اضغط (نشر الآن) للموافقة.\n"
        "النشر يتم من حسابك الشخصي إلى المجموعات المسجلة فقط.",
        reply_markup=main_keyboard())


@bot.message_handler(content_types=["text", "photo", "video", "document", "audio", "voice", "animation"])
def incoming(message):
    global paused
    if not admin_only_reply(message):
        return
    if message.text and message.text.startswith("/"):
        return
    text = message.text or message.caption or ""
    if message.from_user and message.from_user.id in pending_setting:
        key = pending_setting.pop(message.from_user.id)
        try:
            value = int((message.text or "").strip())
            if key == "repeat_count":
                if not 1 <= value <= 100:
                    raise ValueError("العدد لازم يكون من 1 إلى 100.")
                set_setting(key, value)
                bot.send_message(message.chat.id, f"✅ تم حفظ عدد النشر: {value} مرة.", reply_markup=main_keyboard())
            else:
                if not 10 <= value <= 86400:
                    raise ValueError("الوقت لازم يكون من 10 إلى 86400 ثانية.")
                set_setting(key, value)
                bot.send_message(message.chat.id, f"✅ تم حفظ وقت التكرار: {value} ثانية.", reply_markup=main_keyboard())
        except ValueError as exc:
            bot.send_message(message.chat.id, f"❌ قيمة غير صحيحة. {exc}\nاضغط الزر مرة ثانية وأدخل رقماً صحيحاً.", reply_markup=main_keyboard())
        return

    if message.text in ["📤 إدارة المنشورات", "📝 إرسال منشور", "📊 الإحصائيات", "🔄 مزامنة المجموعات", "👥 المجموعات", "📚 المجموعات", "⏸ إيقاف النشر", "▶️ استئناف النشر", "🔢 عدد النشر", "⏱️ وقت التكرار", "🔁 إعدادات التكرار", "ℹ️ المساعدة"]:
        if message.text == "📤 إدارة المنشورات":
            with db_connect() as conn:
                rows = conn.execute("SELECT id, status, kind, created_at FROM posts ORDER BY id DESC LIMIT 15").fetchall()
            if not rows:
                bot.send_message(message.chat.id, "ماكو منشورات محفوظة لحد الآن. اضغط 📝 إرسال منشور أو أرسل المحتوى مباشرة.")
            else:
                lines = [f"#{r['id']} — {r['kind']} — {r['status']}" for r in rows]
                bot.send_message(message.chat.id, "<b>📤 آخر 15 منشور:</b>\n" + "\n".join(lines))
        elif message.text == "📝 إرسال منشور":
            bot.send_message(message.chat.id, "أرسل النص أو الصورة أو الفيديو أو الملف هنا، وبعدها راح تظهر المعاينة وزر الموافقة.")
        elif message.text == "📊 الإحصائيات":
            groups = len(get_groups())
            with db_connect() as conn:
                posts = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
                published = conn.execute("SELECT COUNT(*) FROM posts WHERE status='published'").fetchone()[0]
            bot.send_message(message.chat.id, f"📊 <b>الإحصائيات</b>\nالمجموعات النشطة: {groups}\nالمنشورات المحفوظة: {posts}\nالمنشورات المنشورة: {published}\nالحالة: {'متوقف' if paused else 'يعمل'}")
        elif message.text == "🔄 مزامنة المجموعات":
            try:
                count = call_telethon(sync_groups_async())
                bot.send_message(message.chat.id, f"✅ تمت مزامنة المجموعات. عدد المجموعات: {count}")
            except Exception as exc:
                bot.send_message(message.chat.id, f"❌ تعذرت المزامنة: {exc}")
        elif message.text in ["👥 المجموعات", "📚 المجموعات"]:
            groups = get_groups()
            if not groups:
                bot.send_message(message.chat.id, "ماكو مجموعات مسجلة. اضغط مزامنة المجموعات.")
            else:
                lines = [f"• {g['title']}" + (f" (@{g['username']})" if g['username'] else "") for g in groups[:80]]
                bot.send_message(message.chat.id, "<b>المجموعات النشطة:</b>\n" + "\n".join(lines))
        elif message.text == "⏸ إيقاف النشر":
            paused = True
            bot.send_message(message.chat.id, "⏸ تم إيقاف النشر مؤقتاً.")
        elif message.text == "▶️ استئناف النشر":
            paused = False
            bot.send_message(message.chat.id, "▶️ تم استئناف النشر.", reply_markup=main_keyboard())
        elif message.text == "🔢 عدد النشر":
            pending_setting[message.from_user.id] = "repeat_count"
            current = get_setting("repeat_count", 1)
            bot.send_message(
                message.chat.id,
                f"🔢 عدد النشر الحالي: {current} مرة\n\nأرسل العدد الجديد كرقم من 1 إلى 100.\nمثال: 5",
                reply_markup=main_keyboard(),
            )
        elif message.text == "⏱️ وقت التكرار":
            pending_setting[message.from_user.id] = "repeat_seconds"
            current = get_setting("repeat_seconds", 60)
            bot.send_message(
                message.chat.id,
                f"⏱️ وقت التكرار الحالي: {current} ثانية\n\nأرسل الفاصل الجديد بالثواني من 10 إلى 86400.\nمثال: 60",
                reply_markup=main_keyboard(),
            )
        elif message.text == "🔁 إعدادات التكرار":
            bot.send_message(
                message.chat.id,
                "🔁 <b>إعدادات التكرار الحالية</b>\n"
                f"🔢 عدد النشر: {get_setting('repeat_count', 1)} مرة\n"
                f"⏱️ الفاصل الزمني: {get_setting('repeat_seconds', 60)} ثانية\n\n"
                "لتغيير كل إعداد، اضغط زره وحده من القائمة.",
                reply_markup=main_keyboard(),
            )
        else:
            bot.send_message(message.chat.id, "أرسل منشوراً لمعاينته، ثم وافق بزر نشر الآن. استخدم مزامنة المجموعات لتحديث القائمة.", reply_markup=main_keyboard())
        return

    try:
        if message.text:
            create_preview(message, "text", text_value=message.text)
        elif message.photo:
            create_preview(message, "photo", file_id=message.photo[-1].file_id, caption=message.caption)
        elif message.video:
            create_preview(message, "video", file_id=message.video.file_id, caption=message.caption)
        elif message.document:
            create_preview(message, "document", file_id=message.document.file_id, caption=message.caption)
        elif message.audio:
            create_preview(message, "audio", file_id=message.audio.file_id, caption=message.caption)
        elif message.voice:
            create_preview(message, "voice", file_id=message.voice.file_id, caption=message.caption)
        elif message.animation:
            create_preview(message, "animation", file_id=message.animation.file_id, caption=message.caption)
    except Exception as exc:
        bot.send_message(message.chat.id, f"❌ ما قدرت أجهز المعاينة: {exc}")


@bot.callback_query_handler(func=lambda call: True)
def callbacks(call):
    if not is_admin(call):
        bot.answer_callback_query(call.id, "غير مسموح", show_alert=True)
        return
    try:
        action, raw_id = call.data.split(":", 1)
        post_id = int(raw_id)
    except Exception:
        bot.answer_callback_query(call.id, "طلب غير صالح")
        return

    if action == "cancel":
        post = get_post(post_id)
        if post:
            set_post_status(post_id, "cancelled")
            if post["file_path"] and os.path.isfile(post["file_path"]):
                try:
                    os.remove(post["file_path"])
                except OSError:
                    pass
        bot.answer_callback_query(call.id, "تم إلغاء المنشور")
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            bot.send_message(call.message.chat.id, f"🗑 تم إلغاء المنشور #{post_id}.")
        except Exception:
            pass
        return

    if action == "publish":
        bot.answer_callback_query(call.id, "بدأ النشر؛ انتظر النتيجة")
        bot.send_message(call.message.chat.id, f"🚀 جاري نشر المنشور #{post_id}...")
        try:
            success, failed = call_telethon(publish_async(post_id), timeout=900)
            bot.send_message(call.message.chat.id, f"✅ انتهى النشر للمنشور #{post_id}\nنجح: {success}\nفشل: {failed}")
        except Exception as exc:
            bot.send_message(call.message.chat.id, f"❌ تعذر النشر: {exc}")


def main():
    global PHONE
    init_db()
    print("=" * 42)
    print("ʜᴀᴍᴢᴀ ᴘʀᴇᴍɪᴜᴍ")
    print("قاعدة البيانات جاهزة.")
    print("لا تشارك رمز الدخول أو ملف الجلسة مع أي شخص.")
    print("=" * 42)

    # Ask for the personal account phone in the main console before starting
    # Telethon. Passing None to Telethon can raise: No phone number or bot token provided.
    if not PHONE.strip():
        PHONE = input("اكتب رقم الهاتف مع رمز الدولة (مثال +9647xxxxxxxxx): ").strip()
    if not PHONE:
        raise SystemExit("ما دخلت رقم الهاتف. أعد التشغيل وأدخل الرقم الدولي.")

    threading.Thread(target=telethon_worker, daemon=True).start()
    # Start bot polling even if personal account login is still pending.
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)


if __name__ == "__main__":
    main()
