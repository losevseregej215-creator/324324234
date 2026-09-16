import asyncio
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove,
    InputMediaPhoto, FSInputFile,
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Samara")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TZ = ZoneInfo(TIMEZONE)

def now() -> datetime:
    return datetime.now(TZ)

def now_iso() -> str:
    return now().isoformat()


DEFAULT_ADMIN_IDS = [1754507338]
RESERVE_TOTAL = 300
QUIET_SECONDS = 10

# Через сколько минут после начала визита запись автоматически
# считается завершённой (если админ не нажал ни одну кнопку).
AUTO_COMPLETE_AFTER_MINUTES = 60

# Сколько дней до визита — минимальный срок для переноса/отмены с возвратом
REFUND_DAYS = 2

# Список услуг
SERVICES = [
    "Ботокс",
    "Нанопластика",
    "Холодное восстановление",
    "Тотальная реконструкция волос",
]


def _load_admin_ids():
    env = os.getenv("ADMIN_IDS", "").strip()
    if not env:
        return DEFAULT_ADMIN_IDS
    ids = [int(x.strip()) for x in env.split(",") if x.strip().isdigit()]
    return ids or DEFAULT_ADMIN_IDS


ADMIN_IDS = _load_admin_ids()


def is_admin(uid):
    return uid in ADMIN_IDS


scheduler = AsyncIOScheduler(timezone=TZ)

DB_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DB_DIR, "Reconst.db")
os.makedirs(DB_DIR, exist_ok=True)

print("=" * 60)
print(f"[BOOT] TIMEZONE: {TIMEZONE}")
print(f"[BOOT] Scheduler TZ: {scheduler.timezone}")
print(f"[BOOT] now(): {now()}")
print(f"[BOOT] DB_PATH: {DB_PATH}")

if not os.path.exists(DB_PATH):
    print(f"[BOOT] Файла нет — создаю пустой Reconst.db")
    open(DB_PATH, "a").close()
else:
    print(f"[BOOT] Файл существует ({os.path.getsize(DB_PATH)} байт)")

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
router = Router()
print("=" * 60)

WELCOME_TEXT = (
    "Добро пожаловать в бот записи на восстановление волос 💇‍♀️\n"
    "Здесь вы можете посмотреть свободные окошки и записаться.\n\n"
    "Услуги: ботокс, нанопластика, холодное восстановление, "
    "тотальная реконструкция волос."
)

RULES_TEXT = (
    "Правила:\n"
    "📌 16+ (если младше — только с письменного разрешения родителей)\n\n"
    "💰 Предоплата — 1000 руб. Предоплата ВХОДИТ в стоимость услуги.\n\n"
    "⏳ Когда ты выбираешь окошко, оно бронируется за тобой. "
    "Пока ты заполняешь анкету, таймер на паузе. "
    "Как только перестаёшь писать на 10 секунд — включается отсчёт 5 минут. "
    "Снова пишешь — таймер снова на паузе.\n"
    "Если 5 минут активного времени пройдут — бронь снимется, "
    "и время сможет занять другая клиентка.\n"
    "Знак ⏳ в расписании значит, что окошко бронирует другая клиентка.\n"
    "После отправки скрина мастеру — бронь держится до ответа мастера.\n"
    "Отменить запись можно на любом этапе кнопкой «❌ Отменить запись».\n\n"
    "📌 Перенос и отмена записи:\n"
    "   • Если предупредила за 2 дня и раньше — предоплата (1000 руб, входит в стоимость) возвращается.\n"
    "   • Если предупредила менее чем за 2 дня — предоплата (1000 руб) НЕ возвращается.\n"
    "📌 Если задерживаешься — предупреди мастер через «Мои записи» → «⏰ Задержусь».\n\n"
    "⚠️ Противопоказания: беременность, аллергия на отдушки."
)

TRAINING_INFO_TEXT = (
    "🎓 Обучение\n\n"
    "Что входит в обучение:\n"
    "• Обучение проходит 3 дня\n"
    "• Отработка трёх и более моделей\n"
    "• Отработка на сложных моделях (кератин, ботокс, "
    "холодное восстановление, нанопластика)\n"
    "• Сопровождение после обучения 1 месяц\n"
    "• Сертификат об обучении\n\n"
    "Нажми «📝 Записаться на обучение», чтобы оставить заявку."
)

CONTRAINDICATIONS_TEXT = (
    "⚠️ Противопоказания:\n"
    "• Беременность\n"
    "• Аллергия на отдушки\n\n"
    "Если у тебя есть противопоказания — обязательно сообщи мастеру.\n\n"
    "Нажми «Продолжить», чтобы оформить запись."
)

RU_MONTHS = {
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "май": 5, "июнь": 6,
    "июль": 7, "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
}
RU_MONTHS_NUM = {v: k.capitalize() for k, v in RU_MONTHS.items()}


def _migrate_db():
    cols = {r["name"] for r in db_all("PRAGMA table_info(slot_reservations)")}
    if "elapsed_seconds" not in cols:
        db_exec("ALTER TABLE slot_reservations ADD COLUMN elapsed_seconds INTEGER DEFAULT 0")
    if "is_paused" not in cols:
        db_exec("ALTER TABLE slot_reservations ADD COLUMN is_paused INTEGER DEFAULT 1")
    if "pause_started_at" not in cols:
        db_exec("ALTER TABLE slot_reservations ADD COLUMN pause_started_at TEXT")
    if "tick_started_at" not in cols:
        db_exec("ALTER TABLE slot_reservations ADD COLUMN tick_started_at TEXT")
    if "notified_json" not in cols:
        db_exec("ALTER TABLE slot_reservations ADD COLUMN notified_json TEXT DEFAULT '[]'")
    if "is_forever" not in cols:
        db_exec("ALTER TABLE slot_reservations ADD COLUMN is_forever INTEGER DEFAULT 0")
        db_exec("UPDATE slot_reservations SET is_forever=1 WHERE expires_at IS NULL")

    b_cols = {r["name"] for r in db_all("PRAGMA table_info(bookings)")}
    if "arrival_notified" not in b_cols:
        db_exec("ALTER TABLE bookings ADD COLUMN arrival_notified INTEGER DEFAULT 0")
        db_exec("UPDATE bookings SET arrival_notified=1 WHERE status='confirmed'")
    if "service" not in b_cols:
        db_exec("ALTER TABLE bookings ADD COLUMN service TEXT")


def init_db():
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS months (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "name TEXT, year INTEGER, month_num INTEGER, UNIQUE(name, year))")
    conn.execute("CREATE TABLE IF NOT EXISTS slots (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "month_id INTEGER, day INTEGER, time TEXT, is_booked INTEGER DEFAULT 0, "
                 "UNIQUE(month_id, day, time))")
    conn.execute("CREATE TABLE IF NOT EXISTS bookings ("
                 "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, "
                 "name TEXT, phone TEXT, hand_photo_file_id TEXT, length_shape TEXT, "
                 "reference TEXT, promo_code TEXT, discount INTEGER DEFAULT 0, status TEXT, "
                 "slot_id INTEGER, month_id INTEGER, day INTEGER, time TEXT, "
                 "booking_datetime TEXT, payment_screenshot_file_id TEXT, created_at TEXT, "
                 "arrival_notified INTEGER DEFAULT 0, service TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS promos (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "code TEXT UNIQUE, discount INTEGER, type TEXT, max_uses INTEGER, used_count INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE IF NOT EXISTS pending_reschedules ("
                 "booking_id INTEGER PRIMARY KEY, new_slot_id INTEGER, new_month_id INTEGER, "
                 "new_day INTEGER, new_time TEXT, new_datetime TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS slot_reservations ("
                 "slot_id INTEGER PRIMARY KEY, user_id INTEGER, expires_at TEXT, created_at TEXT, "
                 "elapsed_seconds INTEGER DEFAULT 0, is_paused INTEGER DEFAULT 1, "
                 "pause_started_at TEXT, tick_started_at TEXT, "
                 "notified_json TEXT DEFAULT '[]', is_forever INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE IF NOT EXISTS booking_drafts ("
                 "user_id INTEGER PRIMARY KEY, state TEXT, data_json TEXT, slot_id INTEGER, created_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS training_requests ("
                 "user_id INTEGER PRIMARY KEY, username TEXT, name TEXT, phone TEXT, "
                 "status TEXT DEFAULT 'pending', created_at TEXT, contacted_at TEXT, "
                 "last_reminder_at TEXT)")
    conn.commit()
    _migrate_db()
    print(f"[BOOT] Месяцев: {len(db_all('SELECT * FROM months'))}")


def db_exec(q, p=()):
    cur = conn.execute(q, p); conn.commit(); return cur

def db_one(q, p=()):
    return conn.execute(q, p).fetchone()

def db_all(q, p=()):
    return conn.execute(q, p).fetchall()


# ================= РЕЗЕРВЫ =================

def _elapsed_now(r) -> int:
    if not r:
        return 0
    if r["is_forever"]:
        return 0
    elapsed = r["elapsed_seconds"] or 0
    if not r["is_paused"] and r["tick_started_at"]:
        try:
            tick = datetime.fromisoformat(r["tick_started_at"])
            elapsed += int((now() - tick).total_seconds())
        except Exception:
            pass
    return max(0, elapsed)


def _slot_is_free_for(slot_id: int, user_id: int) -> bool:
    r = db_one("SELECT * FROM slot_reservations WHERE slot_id=?", (slot_id,))
    if not r:
        return True
    if r["user_id"] == user_id:
        return True
    if r["is_forever"]:
        return False
    if _elapsed_now(r) >= RESERVE_TOTAL:
        db_exec("DELETE FROM slot_reservations WHERE slot_id=?", (slot_id,))
        return True
    return False


def _reserve_slot(slot_id: int, user_id: int) -> bool:
    if not _slot_is_free_for(slot_id, user_id):
        return False
    db_exec("""INSERT OR REPLACE INTO slot_reservations
        (slot_id, user_id, expires_at, created_at, elapsed_seconds, is_paused,
         pause_started_at, tick_started_at, notified_json, is_forever)
        VALUES(?,?,NULL,?,0,1,?,NULL,'[]',0)""",
        (slot_id, user_id, now_iso(), now_iso()))
    return True


def _reserve_slot_forever(slot_id: int, user_id: int):
    db_exec("UPDATE slot_reservations SET is_forever=1 WHERE slot_id=? AND user_id=?",
            (slot_id, user_id))
    r = db_one("SELECT * FROM slot_reservations WHERE slot_id=?", (slot_id,))
    if not r:
        db_exec("""INSERT OR REPLACE INTO slot_reservations
            (slot_id, user_id, expires_at, created_at, elapsed_seconds, is_paused,
             pause_started_at, tick_started_at, notified_json, is_forever)
            VALUES(?,?,NULL,?,0,0,NULL,?,'[]',1)""",
            (slot_id, user_id, now_iso(), now_iso()))


def _on_user_action(uid: int):
    r = db_one("SELECT * FROM slot_reservations WHERE user_id=?", (uid,))
    if not r or r["is_forever"]:
        return
    elapsed = _elapsed_now(r)
    elapsed = min(elapsed, RESERVE_TOTAL)
    db_exec("""UPDATE slot_reservations
        SET elapsed_seconds=?, is_paused=1, pause_started_at=?, tick_started_at=NULL, notified_json='[]'
        WHERE slot_id=?""",
        (elapsed, now_iso(), r["slot_id"]))
    print(f"[PAUSE] uid={uid}, elapsed={elapsed}s")


def _release_slot(slot_id: int):
    db_exec("DELETE FROM slot_reservations WHERE slot_id=?", (slot_id,))


def _release_user_reservations(user_id: int):
    db_exec("DELETE FROM slot_reservations WHERE user_id=?", (user_id,))


def _save_draft(uid: int, st: str, data: dict, slot_id: int):
    try:
        payload = json.dumps(data, ensure_ascii=False, default=str)
    except Exception:
        payload = "{}"
    db_exec("INSERT OR REPLACE INTO booking_drafts(user_id, state, data_json, slot_id, created_at) "
            "VALUES(?,?,?,?,?)",
            (uid, st, payload, slot_id, now_iso()))


def _draft_label(uid: int):
    r = db_one("SELECT * FROM slot_reservations WHERE user_id=?", (uid,))
    if not r:
        return None, None
    if not r["is_forever"] and _elapsed_now(r) >= RESERVE_TOTAL:
        return None, None
    draft = db_one("SELECT * FROM booking_drafts WHERE user_id=?", (uid,))
    if not draft:
        return None, None
    slot = db_one("SELECT * FROM slots WHERE id=?", (r["slot_id"],))
    if not slot:
        return None, None
    month = db_one("SELECT * FROM months WHERE id=?", (slot["month_id"],))
    m_num = month["month_num"] if month else 0
    label = f"{slot['day']:02d}.{m_num:02d} {slot['time']}"
    return slot["id"], label


async def _send_reminder_kind(bot, uid: int, kind: str):
    texts = {
        "4m": "⏰ Осталось 4 минуты, чтобы завершить оформление.",
        "3m": "⏰ Осталось 3 минуты, чтобы завершить оформление.",
        "2m": "⏰ Осталось 2 минуты, чтобы завершить оформление.",
        "1m": "⏰ Осталась 1 минута, чтобы завершить оформление.",
    }
    head = texts.get(kind, "⏰ Напоминание")
    slot_id, label = _draft_label(uid)
    kb = user_main_kb_with_resume(label) if label else user_main_kb()
    text = (f"{head}\n\n"
            f"Нажми ▶️ Вернуться к записи, чтобы продолжить,\n"
            f"или ❌ Отменить запись, если передумала.")
    try:
        await bot.send_message(uid, text, reply_markup=kb)
        print(f"[REMIND {kind}] → {uid}")
    except Exception as e:
        print(f"[REMIND {kind}] ошибка: {e}")


async def _expire_one(bot, r):
    slot_id = r["slot_id"]; uid = r["user_id"]
    _release_slot(slot_id)
    db_exec("DELETE FROM booking_drafts WHERE user_id=?", (uid,))

    b = db_one("SELECT * FROM bookings WHERE slot_id=? AND user_id=? AND status='pending_payment' "
               "ORDER BY id DESC LIMIT 1", (slot_id, uid))
    if b:
        db_exec("UPDATE bookings SET status='expired' WHERE id=?", (b["id"],))

    try:
        await bot.send_message(
            uid,
            "⏰ Кисуль ты не успела завершить оформление за 5 минут.\n"
            "Бронь снята, время освободилось.\n\n"
            "Хочешь записаться заново — нажми «Записаться».",
            reply_markup=user_main_kb()
        )
        print(f"[EXPIRE] → {uid}, слот {slot_id}")
    except Exception as e:
        print(f"[EXPIRE] ошибка: {e}")

    if b:
        month = db_one("SELECT * FROM months WHERE id=?", (b["month_id"],))
        month_num = month["month_num"] if month else "?"
        await send_to_admins(bot,
            text=(f"⏰ Бронь истекла (клиентка не успела за 5 мин)\n\n"
                  f"Клиент: id {uid}\n"
                  f"Слот: {b['day']:02d}.{month_num:02d} {b['time']}"))


async def _check_timers(bot):
    rows = db_all("SELECT * FROM slot_reservations WHERE is_forever=0")
    for r in rows:
        uid = r["user_id"]
        if r["is_paused"]:
            try:
                p = datetime.fromisoformat(r["pause_started_at"])
            except Exception:
                continue
            if (now() - p).total_seconds() >= QUIET_SECONDS:
                db_exec("UPDATE slot_reservations SET is_paused=0, tick_started_at=? WHERE slot_id=?",
                        (now_iso(), r["slot_id"]))
                print(f"[RESUME] uid={uid}, elapsed={r['elapsed_seconds']}s")
            continue
        elapsed = _elapsed_now(r)
        if elapsed >= RESERVE_TOTAL:
            await _expire_one(bot, r)
            continue
        try:
            sent = set(json.loads(r["notified_json"] or "[]"))
        except Exception:
            sent = set()
        changed = False
        for th, kind in ((60, "4m"), (120, "3m"), (180, "2m"), (240, "1m")):
            if elapsed >= th and kind not in sent:
                await _send_reminder_kind(bot, uid, kind)
                sent.add(kind); changed = True
        if changed:
            db_exec("UPDATE slot_reservations SET notified_json=? WHERE slot_id=?",
                    (json.dumps(list(sent)), r["slot_id"]))


# ================= УВЕДОМЛЕНИЕ О НАЧАЛЕ ЗАПИСИ =================

async def _send_arrival_notification(bot, b):
    month = db_one("SELECT * FROM months WHERE id=?", (b["month_id"],))
    month_num = month["month_num"] if month else "?"
    dn = await display_name(bot, b["user_id"], b["username"])
    text = (
        f"⏰ Время записи наступило!\n\n"
        f"Клиент: {dn}\n"
        f"Имя: {b['name']}\n"
        f"Телефон: {b['phone']}\n"
        f"Услуга: {b['service'] or '—'}\n"
        f"📅 {b['day']:02d}.{month_num:02d} {b['time']}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить запись",
                              callback_data=f"admin_cancel_booking:{b['id']}")],
        [InlineKeyboardButton(text="✅ Закончить прием",
                              callback_data=f"admin_finish_booking:{b['id']}")],
    ])
    try:
        await send_to_admins(bot, text=text, reply_markup=kb)
        print(f"[ARRIVAL] booking {b['id']} → админам")
    except Exception as e:
        print(f"[ARRIVAL] ошибка: {e}")


async def _check_arrivals(bot):
    current = now()
    rows = db_all("SELECT * FROM bookings WHERE status='confirmed' AND arrival_notified=0")
    for b in rows:
        try:
            dt = datetime.fromisoformat(b["booking_datetime"])
        except Exception:
            db_exec("UPDATE bookings SET arrival_notified=1 WHERE id=?", (b["id"],))
            continue
        if dt > current:
            continue
        if current - dt <= timedelta(hours=2):
            await _send_arrival_notification(bot, b)
        db_exec("UPDATE bookings SET arrival_notified=1 WHERE id=?", (b["id"],))


# ================= АВТОЗАВЕРШЕНИЕ ПРОШЕДШИХ ЗАПИСЕЙ =================

async def _check_past_bookings(bot):
    current = now()
    threshold = timedelta(minutes=AUTO_COMPLETE_AFTER_MINUTES)
    rows = db_all("SELECT * FROM bookings WHERE status='confirmed'")
    for b in rows:
        try:
            dt = datetime.fromisoformat(b["booking_datetime"])
        except Exception:
            continue
        if current - dt < threshold:
            continue

        db_exec("UPDATE bookings SET status='completed' WHERE id=?", (b["id"],))

        if b["slot_id"]:
            slot = db_one("SELECT * FROM slots WHERE id=?", (b["slot_id"],))
            if slot and not slot["is_booked"]:
                db_exec("UPDATE slots SET is_booked=1 WHERE id=?", (b["slot_id"],))
            _release_slot(b["slot_id"])

        try:
            await bot.send_message(
                b["user_id"],
                "✅ Спасибо за визит! Ждём тебя снова 💇‍♀️",
                reply_markup=user_main_kb()
            )
        except Exception as e:
            print(f"[AUTOCOMPLETE] notify error: {e}")

        print(f"[AUTOCOMPLETE] booking {b['id']} → completed")


# ================= НАПОМИНАНИЯ ПО ОБУЧЕНИЮ =================

async def _check_training_reminders(bot):
    current = now()
    rows = db_all("SELECT * FROM training_requests WHERE status='pending'")
    for r in rows:
        last_str = r["last_reminder_at"] or r["created_at"]
        if not last_str:
            continue
        try:
            last_dt = datetime.fromisoformat(last_str)
        except Exception:
            continue
        if (current - last_dt).total_seconds() >= 3600:
            text = (
                f"⏰ Напоминание: свяжись с ученицей!\n\n"
                f"Юзернейм: @{r['username'] or '—'}\n"
                f"Имя: {r['name']}\n"
                f"Телефон: {r['phone']}"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Связалась",
                                      callback_data=f"training_contacted:{r['user_id']}")]
            ])
            await send_to_admins(bot, text=text, reply_markup=kb)
            db_exec("UPDATE training_requests SET last_reminder_at=? WHERE user_id=?",
                    (now_iso(), r["user_id"]))


# ================= ОБЩИЕ ХЕЛПЕРЫ =================

def get_payment_details():
    env = os.getenv("ADMIN_PAYMENT_DETAILS")
    if env:
        return env
    row = db_one("SELECT value FROM settings WHERE key='payment_details'")
    return row["value"] if row else "Реквизиты не заданы."

def get_months():
    return db_all("SELECT * FROM months ORDER BY year, month_num")

def get_slots(mid):
    return db_all("SELECT * FROM slots WHERE month_id=? ORDER BY day, time", (mid,))


def get_welcome_text():
    row = db_one("SELECT value FROM settings WHERE key='welcome_text'")
    return row["value"] if row else WELCOME_TEXT


def get_welcome_photos():
    row = db_one("SELECT value FROM settings WHERE key='welcome_photos'")
    if row is None:
        return None
    try:
        val = json.loads(row["value"])
    except Exception:
        return None
    if not isinstance(val, list) or len(val) == 0:
        return None
    return val


async def resolve_username(bot, uid, cached=None):
    if cached:
        return cached.lstrip("@")
    try:
        chat = await bot.get_chat(uid)
        if chat.username:
            return chat.username
    except Exception:
        pass
    return None


async def display_name(bot, uid, cached=None):
    un = await resolve_username(bot, uid, cached)
    return f"@{un}" if un else f"клиент (id {uid})"


async def send_to_admins(bot, text=None, photo=None, reply_markup=None,
                         media=None, caption=None):
    for aid in ADMIN_IDS:
        try:
            if media:
                await bot.send_media_group(aid, media=media)
                if caption:
                    await bot.send_message(aid, caption, reply_markup=reply_markup)
            elif photo:
                await bot.send_photo(aid, photo, caption=caption or "", reply_markup=reply_markup)
            elif text:
                await bot.send_message(aid, text, reply_markup=reply_markup)
        except Exception as e:
            print(f"send_to_admins error for {aid}: {e}")


class IsAdmin(BaseFilter):
    async def __call__(self, event):
        return is_admin(getattr(event.from_user, "id", None))


def parse_month(text):
    text = text.strip().lower()
    year = now().year
    parts = text.split()
    if len(parts) >= 2 and parts[-1].isdigit():
        year = int(parts[-1]); parts = parts[:-1]
    month_name = " ".join(parts)
    month_num = None
    for name, num in RU_MONTHS.items():
        if name in month_name:
            month_num = num; break
    if month_num is None and month_name.isdigit():
        month_num = int(month_name)
    if month_num is None:
        return None
    return RU_MONTHS_NUM.get(month_num, month_name.capitalize()), year, month_num


def parse_schedule(text):
    groups = [g.strip() for g in text.split(",") if g.strip()]
    result = {}
    for group in groups:
        parts = group.split()
        if not parts:
            continue
        day = int(parts[0]); rest = parts[1:]; times = []
        if any(":" in p for p in rest):
            for p in rest:
                if ":" in p:
                    h, m = p.split(":")
                    times.append(f"{int(h):02d}:{int(m):02d}")
        else:
            for i in range(0, len(rest), 2):
                if i + 1 < len(rest):
                    times.append(f"{int(rest[i]):02d}:{int(rest[i+1]):02d}")
        result[day] = times
    return result


def format_schedule_preview(name, year, month_num, schedule):
    lines = [f"📅 {name} {year}"]
    for day in sorted(schedule.keys()):
        lines.append(f"{day:02d}.{month_num:02d}")
        lines.append(", ".join(schedule[day]))
    return "\n".join(lines)


def format_month_schedule(mid):
    m = db_one("SELECT * FROM months WHERE id=?", (mid,))
    if not m:
        return ""
    slots = db_all("SELECT * FROM slots WHERE month_id=? ORDER BY day, time", (mid,))
    grouped = {}
    for s in slots:
        mark = ""
        if s["is_booked"]:
            mark = " ❌"
        else:
            r = db_one("SELECT * FROM slot_reservations WHERE slot_id=?", (s["id"],))
            if r:
                if r["is_forever"] or _elapsed_now(r) < RESERVE_TOTAL:
                    mark = " ⏳"
        grouped.setdefault(s["day"], []).append(s["time"] + mark)
    lines = [f"📅 {m['name']} {m['year']}"]
    for day in sorted(grouped.keys()):
        lines.append(f"{day:02d}.{m['month_num']:02d}")
        lines.append(", ".join(grouped[day]))
    return "\n".join(lines)


def format_all_schedules():
    months = get_months()
    if not months:
        return "Записей пока нет."
    return "\n\n".join(format_month_schedule(m["id"]) for m in months)


def admin_main_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Создать запись"), KeyboardButton(text="Записи")],
        [KeyboardButton(text="Промокод"), KeyboardButton(text="Реквизиты")],
        [KeyboardButton(text="Правила"), KeyboardButton(text="Главное сообщение")],
        [KeyboardButton(text="🎓 Ученицы")],
    ], resize_keyboard=True)


def user_main_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Записаться")],
        [KeyboardButton(text="Обучение")],
        [KeyboardButton(text="Правила"), KeyboardButton(text="Мои записи")],
    ], resize_keyboard=True)


def user_main_kb_with_resume(slot_label: str):
    if not slot_label:
        return user_main_kb()
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text=f"▶️ Вернуться к записи {slot_label}")],
        [KeyboardButton(text="❌ Отменить запись")],
        [KeyboardButton(text="Записаться")],
        [KeyboardButton(text="Обучение")],
        [KeyboardButton(text="Правила"), KeyboardButton(text="Мои записи")],
    ], resize_keyboard=True)


def cancel_booking_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="❌ Отменить запись")],
    ], resize_keyboard=True)


def yes_reset_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Да"), KeyboardButton(text="Сбросить")],
        [KeyboardButton(text="❌ Отменить запись")],
    ], resize_keyboard=True)


def contraindications_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Продолжить")],
        [KeyboardButton(text="❌ Отменить запись")],
    ], resize_keyboard=True)


def services_kb():
    rows = [[KeyboardButton(text=s)] for s in SERVICES]
    rows.append([KeyboardButton(text="❌ Отменить запись")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def training_apply_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📝 Записаться на обучение")],
        [KeyboardButton(text="Назад")],
    ], resize_keyboard=True)


def training_cancel_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Назад")],
    ], resize_keyboard=True)


def admin_month_menu_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Добавить запись"), KeyboardButton(text="Отменить запись")],
        [KeyboardButton(text="Забронировать"), KeyboardButton(text="Убрать бронь")],
        [KeyboardButton(text="🗑 Удалить месяц")],
        [KeyboardButton(text="Назад")],
    ], resize_keyboard=True)


def _find_local_welcome_files():
    search_dirs = [BASE_DIR, os.getcwd(), DB_DIR]
    seen = set(); dirs = []
    for d in search_dirs:
        if d not in seen:
            seen.add(d); dirs.append(d)
    paths = []
    for base in ("photo1", "photo2", "photo3"):
        for d in dirs:
            found = False
            for ext in (".jpg", ".jpeg", ".png", ".JPG", ".JPEG", ".PNG"):
                path = os.path.join(d, base + ext)
                if os.path.exists(path):
                    paths.append(path); found = True; break
            if found:
                break
    return paths


async def send_welcome(message: Message):
    text = get_welcome_text()
    file_ids = get_welcome_photos()
    sources = []
    if file_ids is None:
        for path in _find_local_welcome_files():
            sources.append(FSInputFile(path))
    else:
        for fid in file_ids:
            sources.append(fid)
    if not sources:
        await message.answer(text); return
    if len(sources) == 1:
        try:
            await message.answer_photo(photo=sources[0], caption=text); return
        except Exception as e:
            print(f"welcome single photo error: {e}")
        await message.answer(text); return
    try:
        media = [InputMediaPhoto(media=s) for s in sources]
        media[0].caption = text
        await message.answer_media_group(media=media); return
    except Exception as e:
        print(f"welcome media_group error: {e}")
    first = True
    for s in sources:
        try:
            if first:
                await message.answer_photo(photo=s, caption=text); first = False
            else:
                await message.answer_photo(photo=s)
        except Exception as e:
            print(f"welcome fallback error: {e}")
    if first:
        await message.answer(text)


async def send_user_schedules(message):
    months = get_months()
    if not months:
        await message.answer("Записей пока нет."); return
    for m in months:
        await message.answer(format_month_schedule(m["id"]))


async def send_reminder(bot, uid, text):
    try:
        await bot.send_message(uid, text)
    except Exception as e:
        print(f"Reminder error: {e}")


async def daily_report(bot):
    today = now().strftime("%Y-%m-%d")
    rows = db_all("SELECT * FROM bookings WHERE status='confirmed' AND date(booking_datetime)=?", (today,))
    if not rows:
        await send_to_admins(bot, text=f"📋 На сегодня ({today}) записей нет."); return
    text = f"📋 Записи на {today}:\n"
    for r in rows:
        dn = await display_name(bot, r["user_id"], r["username"])
        text += f"{r['time']} — {dn}, тел: {r['phone']}, {r['service'] or '—'}\n"
    await send_to_admins(bot, text=text)


async def schedule_existing_reminders(bot):
    rows = db_all("SELECT * FROM bookings WHERE status='confirmed'")
    current = now()
    for r in rows:
        dt = datetime.fromisoformat(r["booking_datetime"])
        if dt - timedelta(days=1) > current:
            scheduler.add_job(send_reminder, "date", run_date=dt - timedelta(days=1),
                              args=[bot, r["user_id"],
                                    f"Напоминание: Кисуль завтра у тебя запись на {dt.strftime('%d.%m %H:%M')}"])
        if dt - timedelta(hours=1) > current:
            scheduler.add_job(send_reminder, "date", run_date=dt - timedelta(hours=1),
                              args=[bot, r["user_id"],
                                    f"Напоминание: Кисуль через час у тебя запись на {dt.strftime('%d.%m %H:%M')}"])


async def edit_admin_message(cb, extra):
    try:
        if cb.message.photo:
            await cb.message.edit_caption(caption=(cb.message.caption or "") + f"\n\n{extra}")
        else:
            await cb.message.edit_text((cb.message.text or "") + f"\n\n{extra}")
    except Exception as e:
        print(f"edit_admin_message error: {e}")


async def build_booking_caption(bot, b, is_extra=False):
    month = db_one("SELECT * FROM months WHERE id=?", (b["month_id"],))
    date_str = f"{b['day']:02d}.{month['month_num']:02d} {b['time']}"
    dn = await display_name(bot, b["user_id"], b["username"])
    head = "💰 Бронирование\n"
    return (f"{head}Клиент: {dn}\nИмя: {b['name']}\nТелефон: {b['phone']}\n"
            f"Дата: {date_str}\nУслуга: {b['service'] or '—'}\n"
            f"Длина волос: {b['length_shape']}\n"
            f"Промокод: {b['promo_code'] or 'нет'} ({b['discount']}%)")


class AdminStates(StatesGroup):
    choosing_month = State()
    create_month = State()
    create_schedule = State()
    month_menu = State()
    edit_input = State()
    book_day = State()
    book_time = State()
    unbook_day = State()
    unbook_time = State()
    promo_code = State()
    promo_discount = State()
    set_payment = State()
    edit_welcome_text = State()
    edit_welcome_photos = State()
    booking_cancel_reason = State()
    students_menu = State()


class UserStates(StatesGroup):
    booking_month = State()
    booking_day = State()
    booking_time = State()
    booking_service = State()
    booking_contraindications = State()
    booking_hair_photo = State()
    booking_hair_confirm = State()
    booking_length = State()
    booking_phone = State()
    booking_name = State()
    booking_promo = State()
    booking_confirm = State()
    booking_payment_screenshot = State()
    my_records_menu = State()
    booking_actions = State()
    delay_input = State()


class TrainingStates(StatesGroup):
    enter_name = State()
    enter_phone = State()


# ============= ГЛОБАЛЬНАЯ ОТМЕНА =============

@router.message(F.text == "❌ Отменить запись")
async def cancel_booking_anytime(message: Message, state: FSMContext):
    uid = message.from_user.id
    data = await state.get_data()
    current_stage = await state.get_state()
    slot_id = data.get("slot_id")

    slot_info = ""
    if slot_id:
        slot = db_one("SELECT * FROM slots WHERE id=?", (slot_id,))
        if slot:
            month = db_one("SELECT * FROM months WHERE id=?", (slot["month_id"],))
            m_num = month["month_num"] if month else "?"
            slot_info = f"{slot['day']:02d}.{m_num:02d} {slot['time']}"

    if slot_id:
        _release_slot(slot_id)
    _release_user_reservations(uid)
    db_exec("DELETE FROM booking_drafts WHERE user_id=?", (uid,))
    await state.clear()

    stage_names = {
        "UserStates:booking_service": "выбор услуги",
        "UserStates:booking_contraindications": "противопоказания",
        "UserStates:booking_hair_photo": "фото волос сзади",
        "UserStates:booking_hair_confirm": "подтверждение фото волос",
        "UserStates:booking_length": "длина волос",
        "UserStates:booking_phone": "телефон",
        "UserStates:booking_name": "имя",
        "UserStates:booking_promo": "промокод",
        "UserStates:booking_confirm": "финальное подтверждение анкеты",
        "UserStates:booking_payment_screenshot": "скрин оплаты",
    }
    stage_label = stage_names.get(current_stage, current_stage or "—")

    un = await resolve_username(message.bot, uid, message.from_user.username)
    un_str = f"@{un}" if un else f"id {uid}"

    lines = ["❌ Клиентка ОТМЕНИЛА запись во время бронирования", ""]
    lines.append(f"Клиент: {un_str}")
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    if name:
        lines.append(f"Имя: {name}")
    if phone:
        lines.append(f"Телефон: {phone}")
    lines.append(f"Слот: {slot_info or '—'}")
    lines.append(f"Этап: {stage_label}")
    text = "\n".join(lines)

    await send_to_admins(message.bot, text=text)

    await message.answer("❌ Запись отменена.\n\nОкошко освобождено.",
                         reply_markup=user_main_kb())


# ============= START =============

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    uid = message.from_user.id
    current_state = await state.get_state()
    data = await state.get_data()

    r = db_one("SELECT * FROM slot_reservations WHERE user_id=?", (uid,))
    if r and (r["is_forever"] or _elapsed_now(r) < RESERVE_TOTAL):
        slot_id = r["slot_id"]
        draft = db_one("SELECT * FROM booking_drafts WHERE user_id=?", (uid,))
        if not draft and current_state and current_state.startswith("UserStates:"):
            _save_draft(uid, current_state, data, slot_id)
        elif not draft:
            _save_draft(uid, "UserStates:booking_hair_photo", data, slot_id)
        _on_user_action(uid)

    await state.clear()
    await send_welcome(message)

    if is_admin(uid):
        await message.answer(format_all_schedules())
        await message.answer("Админ-меню:", reply_markup=admin_main_kb())
        return

    await send_user_schedules(message)
    _, label = _draft_label(uid)
    if label:
        await message.answer(
            f"📌 У тебя есть незаконченная запись на {label}.\n"
            f"Нажми ▶️ Вернуться к записи, чтобы продолжить,\n"
            f"или ❌ Отменить запись, чтобы отменить.",
            reply_markup=user_main_kb_with_resume(label))
    else:
        await message.answer("Главное меню:", reply_markup=user_main_kb())


@router.message(F.text.regexp(r"^▶️ Вернуться к записи \d{2}\.\d{2} \d{2}:\d{2}$"))
async def resume_draft_reply(message: Message, state: FSMContext):
    uid = message.from_user.id
    draft = db_one("SELECT * FROM booking_drafts WHERE user_id=?", (uid,))
    if not draft:
        await message.answer("Черновик не найден.", reply_markup=user_main_kb()); return
    r = db_one("SELECT * FROM slot_reservations WHERE slot_id=?", (draft["slot_id"],))
    if not r or r["user_id"] != uid:
        db_exec("DELETE FROM booking_drafts WHERE user_id=?", (uid,))
        await message.answer("Бронь уже снята.", reply_markup=user_main_kb()); return
    if not r["is_forever"] and _elapsed_now(r) >= RESERVE_TOTAL:
        db_exec("DELETE FROM booking_drafts WHERE user_id=?", (uid,))
        await message.answer("Время брони истекло.", reply_markup=user_main_kb()); return
    try:
        data = json.loads(draft["data_json"]) if draft["data_json"] else {}
    except Exception:
        data = {}
    st = draft["state"]
    await state.set_state(st)
    await state.set_data(data)
    db_exec("DELETE FROM booking_drafts WHERE user_id=?", (uid,))
    _on_user_action(uid)
    await message.answer("✅ Продолжаем оформление", reply_markup=cancel_booking_kb())

    if st == "UserStates:booking_service":
        await message.answer("Выбери услугу:", reply_markup=services_kb())
    elif st == "UserStates:booking_contraindications":
        await message.answer(CONTRAINDICATIONS_TEXT, reply_markup=contraindications_kb())
    elif st == "UserStates:booking_hair_photo":
        await message.answer("Отправь фото волос сзади:", reply_markup=cancel_booking_kb())
    elif st == "UserStates:booking_hair_confirm":
        await message.answer("Фото отправила, правильно?", reply_markup=yes_reset_kb())
    elif st == "UserStates:booking_length":
        await message.answer("Введи длину волос:", reply_markup=cancel_booking_kb())
    elif st == "UserStates:booking_phone":
        await message.answer("Введи номер телефона:", reply_markup=cancel_booking_kb())
    elif st == "UserStates:booking_name":
        await message.answer("Введи имя:", reply_markup=cancel_booking_kb())
    elif st == "UserStates:booking_promo":
        await message.answer("Введи промокод или 'нет':", reply_markup=cancel_booking_kb())
    elif st == "UserStates:booking_confirm":
        month = db_one("SELECT * FROM months WHERE id=?", (data.get("month_id"),))
        month_num = month["month_num"] if month else "?"
        text_sum = (f"Проверь данные:\nИмя: {data.get('name')}\nТелефон: {data.get('phone')}\n"
                    f"Услуга: {data.get('service')}\n"
                    f"Длина волос: {data.get('length_shape')}\n"
                    f"Дата: {data.get('day'):02d}.{month_num:02d} {data.get('time')}\n"
                    f"Промокод: {data.get('promo_code') or 'нет'} (скидка {data.get('discount', 0)}%)")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить", callback_data="user_confirm_booking")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="user_cancel_booking")]])
        await message.answer(text_sum, reply_markup=kb)
    elif st == "UserStates:booking_payment_screenshot":
        await message.answer("Отправь скрин оплаты мастеру:", reply_markup=cancel_booking_kb())


# ============= ПРАВИЛА =============

@router.message(F.text == "Правила")
async def rules_handler(message: Message):
    await message.answer(RULES_TEXT)


# ============= ОБУЧЕНИЕ =============

@router.message(F.text == "Обучение")
async def training_info(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(TRAINING_INFO_TEXT, reply_markup=training_apply_kb())


@router.message(F.text == "📝 Записаться на обучение")
async def training_apply(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(TrainingStates.enter_name)
    await message.answer("Введи своё имя:", reply_markup=training_cancel_kb())


@router.message(TrainingStates.enter_name)
async def training_name(message: Message, state: FSMContext):
    if message.text == "Назад":
        await state.clear()
        await message.answer("Главное меню:", reply_markup=user_main_kb()); return
    if not message.text or not message.text.strip():
        await message.answer("Введи имя текстом:"); return
    await state.update_data(training_name=message.text.strip())
    await state.set_state(TrainingStates.enter_phone)
    await message.answer("Введи номер телефона:", reply_markup=training_cancel_kb())


@router.message(TrainingStates.enter_phone)
async def training_phone(message: Message, state: FSMContext):
    if message.text == "Назад":
        await state.set_state(TrainingStates.enter_name)
        await message.answer("Введи своё имя:", reply_markup=training_cancel_kb()); return
    if not message.text or not message.text.strip():
        await message.answer("Введи номер телефона текстом:"); return
    phone = message.text.strip()
    data = await state.get_data()
    name = data.get("training_name", "—")
    uid = message.from_user.id
    un = await resolve_username(message.bot, uid, message.from_user.username)
    db_exec("""INSERT OR REPLACE INTO training_requests
        (user_id, username, name, phone, status, created_at, contacted_at, last_reminder_at)
        VALUES(?,?,?,?,?,?,NULL,?)""",
        (uid, un, name, phone, "pending", now_iso(), now_iso()))
    text = (
        f"🎓 Новая заявка на обучение!\n\n"
        f"Юзернейм: @{un or '—'}\n"
        f"Имя: {name}\n"
        f"Телефон: {phone}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Связалась", callback_data=f"training_contacted:{uid}")]
    ])
    await send_to_admins(message.bot, text=text, reply_markup=kb)
    await message.answer("✅ Заявка отправлена! Мастер скоро свяжется с тобой.",
                         reply_markup=user_main_kb())
    await state.clear()


# ============= ГЛАВНОЕ СООБЩЕНИЕ =============

@router.message(IsAdmin(), F.text == "Главное сообщение")
async def admin_edit_welcome(message: Message, state: FSMContext):
    await state.set_state(AdminStates.edit_welcome_text)
    await message.answer("Отправьте новый текст главного сообщения (или «Пропустить»).",
        reply_markup=ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="Пропустить"), KeyboardButton(text="Отмена")]
        ], resize_keyboard=True))


@router.message(AdminStates.edit_welcome_text)
async def admin_welcome_text(message: Message, state: FSMContext):
    if message.text == "Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=admin_main_kb()); return
    if message.text != "Пропустить":
        if not message.text:
            await message.answer("Отправьте текст или «Пропустить»."); return
        db_exec("INSERT OR REPLACE INTO settings(key, value) VALUES('welcome_text', ?)", (message.text,))
    await state.update_data(edit_photos=[])
    await state.set_state(AdminStates.edit_welcome_photos)
    await message.answer("Отправьте фото (по одному). «Готово» — закончить. «Сбросить» — вернуть дефолт.",
        reply_markup=ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="Готово"), KeyboardButton(text="Сбросить")],
            [KeyboardButton(text="Отмена")],
        ], resize_keyboard=True))


@router.message(AdminStates.edit_welcome_photos, F.photo)
async def admin_welcome_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("edit_photos", [])
    photos.append(message.photo[-1].file_id)
    await state.update_data(edit_photos=photos)
    await message.answer(f"Принято фото ({len(photos)}).")


@router.message(AdminStates.edit_welcome_photos, F.text == "Отмена")
async def admin_welcome_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=admin_main_kb())


@router.message(AdminStates.edit_welcome_photos, F.text == "Сбросить")
async def admin_welcome_reset_photos(message: Message, state: FSMContext):
    db_exec("DELETE FROM settings WHERE key='welcome_photos'")
    await state.clear()
    await message.answer("Фото сброшены.", reply_markup=admin_main_kb())
    await send_welcome(message)


@router.message(AdminStates.edit_welcome_photos, F.text == "Готово")
async def admin_welcome_done(message: Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("edit_photos", [])
    if not photos:
        await message.answer("Не отправлено ни одного фото."); return
    db_exec("INSERT OR REPLACE INTO settings(key, value) VALUES('welcome_photos', ?)", (json.dumps(photos),))
    await state.clear()
    await message.answer("Сохранено.", reply_markup=admin_main_kb())
    await send_welcome(message)


@router.message(AdminStates.edit_welcome_photos)
async def admin_welcome_photos_fallback(message: Message):
    await message.answer("Отправьте фото / «Готово» / «Сбросить» / «Отмена».")


# ============= ADMIN: УЧЕНИЦЫ =============

@router.message(IsAdmin(), F.text == "🎓 Ученицы")
async def admin_students(message: Message, state: FSMContext):
    await state.clear()
    rows = db_all("SELECT * FROM training_requests ORDER BY created_at DESC")
    if not rows:
        await message.answer("Пока нет заявок на обучение.", reply_markup=admin_main_kb())
        return
    buttons = []
    for r in rows:
        label = f"@{r['username']}" if r["username"] else f"ученица #{r['user_id']}"
        buttons.append([KeyboardButton(text=label)])
    buttons.append([KeyboardButton(text="Назад")])
    await state.set_state(AdminStates.students_menu)
    await message.answer("🎓 Ученицы:", reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True))


@router.message(AdminStates.students_menu)
async def admin_student_detail(message: Message, state: FSMContext):
    if message.text == "Назад":
        await state.clear()
        await message.answer("Админ-меню:", reply_markup=admin_main_kb())
        return
    text = (message.text or "").strip()
    r = None
    if text.startswith("@"):
        r = db_one("SELECT * FROM training_requests WHERE username=?", (text[1:],))
    elif text.startswith("ученица #"):
        try:
            uid = int(text.split("#")[1])
            r = db_one("SELECT * FROM training_requests WHERE user_id=?", (uid,))
        except Exception:
            r = None
    if not r:
        await message.answer("Не найдено. Выбери из кнопок.")
        return
    text_info = (
        f"🎓 Ученица\n\n"
        f"Юзернейм: @{r['username'] or '—'}\n"
        f"Имя: {r['name']}\n"
        f"Телефон: {r['phone']}"
    )
    if r["status"] == "pending":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Связалась",
                                  callback_data=f"training_contacted:{r['user_id']}")]
        ])
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить",
                                  callback_data=f"training_delete:{r['user_id']}")]
        ])
    await message.answer(text_info, reply_markup=kb)


@router.callback_query(F.data.startswith("training_contacted:"))
async def training_contacted(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    uid = int(cb.data.split(":")[1])
    r = db_one("SELECT * FROM training_requests WHERE user_id=?", (uid,))
    if not r:
        await cb.answer("Не найдено", show_alert=True); return
    db_exec("UPDATE training_requests SET status='contacted', contacted_at=? WHERE user_id=?",
            (now_iso(), uid))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"training_delete:{uid}")]
    ])
    try:
        await cb.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass
    await cb.answer("Отмечено ✅")


@router.callback_query(F.data.startswith("training_delete:"))
async def training_delete(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    uid = int(cb.data.split(":")[1])
    db_exec("DELETE FROM training_requests WHERE user_id=?", (uid,))
    try:
        await cb.message.edit_text((cb.message.text or "") + "\n\n🗑 Удалено")
    except Exception:
        pass
    await cb.answer("Удалено")


# ============= ADMIN: СОЗДАНИЕ ЗАПИСИ =============

@router.message(IsAdmin(), F.text == "Создать запись")
async def admin_create_record(message: Message, state: FSMContext):
    await state.set_state(AdminStates.choosing_month)
    months = get_months()
    buttons = [[KeyboardButton(text=f"{m['name']} {m['year']}")] for m in months]
    buttons.append([KeyboardButton(text="Создать следующий месяц")])
    buttons.append([KeyboardButton(text="Назад")])
    await message.answer("Выберите месяц:", reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True))


async def open_month_menu(message: Message, state: FSMContext, month_id: int):
    m = db_one("SELECT * FROM months WHERE id=?", (month_id,))
    if not m:
        await message.answer("Месяц не найден.", reply_markup=admin_main_kb())
        await state.clear(); return
    await state.update_data(month_id=month_id, month_num=m["month_num"],
                            month_name=m["name"], month_year=m["year"])
    await state.set_state(AdminStates.month_menu)
    await message.answer(format_month_schedule(month_id))
    await message.answer(f"Меню: {m['name']} {m['year']}", reply_markup=admin_month_menu_kb())


@router.message(AdminStates.choosing_month)
async def admin_choosing_month(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    if message.text == "Назад":
        await state.clear()
        await message.answer("Админ-меню:", reply_markup=admin_main_kb()); return
    if message.text == "Создать следующий месяц":
        await state.set_state(AdminStates.create_month)
        await message.answer("Введите месяц (например: Сентябрь 2026):", reply_markup=ReplyKeyboardRemove()); return
    parsed = parse_month(message.text)
    if not parsed:
        await message.answer("Выберите месяц из кнопок."); return
    name, year, month_num = parsed
    row = db_one("SELECT * FROM months WHERE name=? AND year=?", (name, year))
    if not row:
        await message.answer("Такого месяца ещё нет."); return
    await open_month_menu(message, state, row["id"])


@router.message(AdminStates.create_month)
async def admin_month_entered(message: Message, state: FSMContext):
    parsed = parse_month(message.text)
    if not parsed:
        await message.answer("Не понял. Пример: Сентябрь 2026"); return
    name, year, month_num = parsed
    row = db_one("SELECT * FROM months WHERE name=? AND year=?", (name, year))
    await state.update_data(month_name=name, month_year=year, month_num=month_num,
                            month_id=row["id"] if row else None)
    await state.set_state(AdminStates.create_schedule)
    await message.answer("Введите дни и время (формат: 1 15 30 16 30, 2 15 30):")


@router.message(AdminStates.create_schedule)
async def admin_schedule_entered(message: Message, state: FSMContext):
    try:
        parsed = parse_schedule(message.text)
    except Exception:
        parsed = None
    if not parsed:
        await message.answer("Не понял. Пример: 1 15 30 16 30"); return
    await state.update_data(schedule=parsed)
    data = await state.get_data()
    preview = format_schedule_preview(data["month_name"], data["month_year"], data["month_num"], parsed)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="admin_schedule_confirm")],
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="admin_schedule_edit")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="admin_schedule_cancel")]])
    await message.answer(preview, reply_markup=kb)


@router.callback_query(F.data == "admin_schedule_confirm")
async def admin_schedule_confirm(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    data = await state.get_data()
    name = data["month_name"]; year = data["month_year"]; month_num = data["month_num"]
    schedule = data["schedule"]
    row = db_one("SELECT id FROM months WHERE name=? AND year=?", (name, year))
    if row:
        month_id = row["id"]
        db_exec("DELETE FROM slots WHERE month_id=? AND is_booked=0", (month_id,))
    else:
        cur = db_exec("INSERT INTO months(name, year, month_num) VALUES(?,?,?)", (name, year, month_num))
        month_id = cur.lastrowid
    for day, times in schedule.items():
        for t in times:
            db_exec("INSERT OR IGNORE INTO slots(month_id, day, time) VALUES(?,?,?)", (month_id, day, t))
    await cb.message.edit_text("Месяц создан.")
    await state.clear()
    await cb.message.answer(format_all_schedules())
    await cb.message.answer("Админ-меню:", reply_markup=admin_main_kb())
    await cb.answer()


@router.callback_query(F.data == "admin_schedule_edit")
async def admin_schedule_edit(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.set_state(AdminStates.create_schedule)
    await cb.message.answer("Введите заново дни и время:")
    await cb.answer()


@router.callback_query(F.data == "admin_schedule_cancel")
async def admin_schedule_cancel(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.clear()
    await cb.message.edit_text("Отменено.")
    await cb.message.answer("Админ-меню:", reply_markup=admin_main_kb())
    await cb.answer()


# ============ МЕНЮ МЕСЯЦА ============

@router.message(AdminStates.month_menu)
async def admin_month_menu_handler(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    t = message.text
    if t == "Назад":
        await admin_create_record(message, state); return
    if t == "🗑 Удалить месяц":
        data = await state.get_data()
        mid = data.get("month_id")
        m = db_one("SELECT * FROM months WHERE id=?", (mid,)) if mid else None
        if not m:
            await message.answer("Месяц не найден.", reply_markup=admin_main_kb())
            await state.clear(); return

        for b in db_all(
            "SELECT id, user_id FROM bookings "
            "WHERE month_id=? AND status IN ('confirmed', 'pending_payment')",
            (mid,)
        ):
            db_exec("DELETE FROM pending_reschedules WHERE booking_id=?", (b["id"],))
            try:
                await message.bot.send_message(b["user_id"],
                    f"❌ Ваша запись на {m['name']} {m['year']} отменена мастером.")
            except Exception:
                pass

        for b in db_all("SELECT id FROM bookings WHERE month_id=?", (mid,)):
            db_exec("DELETE FROM pending_reschedules WHERE booking_id=?", (b["id"],))

        for s in db_all("SELECT id FROM slots WHERE month_id=?", (mid,)):
            _release_slot(s["id"])

        db_exec("DELETE FROM bookings WHERE month_id=?", (mid,))
        db_exec("DELETE FROM slots WHERE month_id=?", (mid,))
        db_exec("DELETE FROM months WHERE id=?", (mid,))
        await state.clear()
        await message.answer(f"🗑 Месяц {m['name']} {m['year']} удалён.", reply_markup=admin_main_kb())
        await message.answer(format_all_schedules()); return
    if t == "Добавить запись":
        await state.update_data(edit_action="add")
        await state.set_state(AdminStates.edit_input)
        await message.answer(
            "Введите день и время. Пример: 1 15 30:",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="Назад")]], resize_keyboard=True))
        return
    if t == "Отменить запись":
        await state.update_data(edit_action="remove")
        await state.set_state(AdminStates.edit_input)
        await message.answer(
            "Введите день и время для удаления. Пример: 1 15 30:",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="Назад")]], resize_keyboard=True))
        return
    if t == "Забронировать":
        await show_admin_days(message, state, "book"); return
    if t == "Убрать бронь":
        await show_admin_days(message, state, "unbook"); return
    await message.answer("Выберите кнопку из меню.", reply_markup=admin_month_menu_kb())


@router.message(AdminStates.edit_input)
async def admin_edit_input(message: Message, state: FSMContext):
    if message.text == "Назад":
        await state.clear()
        await message.answer("Админ-меню:", reply_markup=admin_main_kb())
        return
    data = await state.get_data()
    mid = data["month_id"]; action = data.get("edit_action")
    try:
        parsed = parse_schedule(message.text)
    except Exception:
        parsed = None
    if not parsed:
        await message.answer("Не понял. Пример: 1 15 30 16 30"); return
    log = []
    if action == "add":
        for day, times in parsed.items():
            for t in times:
                try:
                    db_exec("INSERT INTO slots(month_id, day, time) VALUES(?,?,?)", (mid, day, t))
                    log.append(f"+ {day:02d}.{data['month_num']:02d} {t}")
                except sqlite3.IntegrityError:
                    log.append(f"= {day:02d}.{data['month_num']:02d} {t} (уже есть)")
    else:
        for day, times in parsed.items():
            for t in times:
                slot = db_one("SELECT * FROM slots WHERE month_id=? AND day=? AND time=?", (mid, day, t))
                if slot:
                    db_exec("DELETE FROM slots WHERE id=?", (slot["id"],))
                    log.append(f"− {day:02d}.{data['month_num']:02d} {t}")
                else:
                    log.append(f"? {day:02d}.{data['month_num']:02d} {t} (нет)")
    await message.answer("Готово:\n" + "\n".join(log))
    await open_month_menu(message, state, mid)


async def show_admin_days(message: Message, state: FSMContext, mode: str):
    data = await state.get_data()
    mid = data["month_id"]
    month = db_one("SELECT * FROM months WHERE id=?", (mid,))
    slots = get_slots(mid)
    if mode == "book":
        slots = [s for s in slots if not s["is_booked"]]
    else:
        slots = [s for s in slots if s["is_booked"]]
    days = sorted(set(s["day"] for s in slots))
    if not days:
        await message.answer("Нет подходящих дней.")
        await open_month_menu(message, state, mid); return
    await state.update_data(admin_action=mode)
    buttons = [[KeyboardButton(text=f"{d:02d}.{month['month_num']:02d}")] for d in days]
    buttons.append([KeyboardButton(text="Назад")])
    if mode == "book":
        await state.set_state(AdminStates.book_day)
    else:
        await state.set_state(AdminStates.unbook_day)
    await message.answer("Выберите день:", reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True))


async def show_admin_times(message: Message, state: FSMContext, mode: str, day: int):
    data = await state.get_data()
    mid = data["month_id"]
    month = db_one("SELECT * FROM months WHERE id=?", (mid,))
    if mode == "book":
        slots = db_all("SELECT * FROM slots WHERE month_id=? AND day=? AND is_booked=0 ORDER BY time", (mid, day))
    else:
        slots = db_all("SELECT * FROM slots WHERE month_id=? AND day=? AND is_booked=1 ORDER BY time", (mid, day))
    if not slots:
        await message.answer("Нет слотов.")
        await show_admin_days(message, state, mode); return
    buttons = [[KeyboardButton(text=s["time"])] for s in slots]
    buttons.append([KeyboardButton(text="Назад")])
    if mode == "book":
        await state.set_state(AdminStates.book_time)
    else:
        await state.set_state(AdminStates.unbook_time)
    await message.answer(f"Выберите время на {day:02d}.{month['month_num']:02d}:",
                         reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True))


@router.message(AdminStates.book_day)
async def admin_book_day(message: Message, state: FSMContext):
    if message.text == "Назад":
        data = await state.get_data()
        await open_month_menu(message, state, data["month_id"]); return
    try:
        day = int(message.text.split(".")[0])
    except Exception:
        await message.answer("Выберите день из кнопок."); return
    await state.update_data(day=day)
    await show_admin_times(message, state, "book", day)


@router.message(AdminStates.book_time)
async def admin_book_time(message: Message, state: FSMContext):
    if message.text == "Назад":
        await show_admin_days(message, state, "book"); return
    data = await state.get_data()
    mid = data["month_id"]; day = data["day"]; time = message.text
    slot = db_one("SELECT * FROM slots WHERE month_id=? AND day=? AND time=?", (mid, day, time))
    if not slot:
        await message.answer("Выберите время из кнопок."); return
    db_exec("UPDATE slots SET is_booked=1 WHERE id=?", (slot["id"],))
    _release_slot(slot["id"])
    await message.answer(f"✅ Забронировано: {day:02d}.{data['month_num']:02d} {time}")
    await open_month_menu(message, state, mid)


@router.message(AdminStates.unbook_day)
async def admin_unbook_day(message: Message, state: FSMContext):
    if message.text == "Назад":
        data = await state.get_data()
        await open_month_menu(message, state, data["month_id"]); return
    try:
        day = int(message.text.split(".")[0])
    except Exception:
        await message.answer("Выберите день из кнопок."); return
    await state.update_data(day=day)
    await show_admin_times(message, state, "unbook", day)


@router.message(AdminStates.unbook_time)
async def admin_unbook_time(message: Message, state: FSMContext):
    if message.text == "Назад":
        await show_admin_days(message, state, "unbook"); return
    data = await state.get_data()
    mid = data["month_id"]; day = data["day"]; time = message.text
    slot = db_one("SELECT * FROM slots WHERE month_id=? AND day=? AND time=?", (mid, day, time))
    if not slot:
        await message.answer("Выберите время из кнопок."); return
    db_exec("UPDATE slots SET is_booked=0 WHERE id=?", (slot["id"],))
    await message.answer(f"✅ Бронь снята: {day:02d}.{data['month_num']:02d} {time}")
    await open_month_menu(message, state, mid)


# ============ ЗАПИСИ (КЛИЕНТЫ) ============

@router.message(IsAdmin(), F.text == "Записи")
async def admin_records(message: Message, state: FSMContext):
    await state.clear()
    rows = db_all("SELECT DISTINCT user_id, username FROM bookings WHERE status='confirmed'")
    if not rows:
        await message.answer("Пока нет подтверждённых клиентов.", reply_markup=admin_main_kb()); return
    buttons = []
    for r in rows:
        un = await resolve_username(message.bot, r["user_id"], r["username"])
        label = f"@{un}" if un else f"клиент #{r['user_id']}"
        buttons.append([KeyboardButton(text=label)])
    buttons.append([KeyboardButton(text="Назад")])
    await message.answer("Клиенты:", reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True))


async def show_client_cards(message: Message, rows):
    if not rows:
        await message.answer("Записей нет."); return
    for r in rows:
        month = db_one("SELECT * FROM months WHERE id=?", (r["month_id"],))
        date_str = f"{r['day']:02d}.{month['month_num']:02d} {r['time']}"
        dn = await display_name(message.bot, r["user_id"], r["username"])
        caption = (f"👤 {dn}\nИмя: {r['name']}\n📞 {r['phone']}\n📅 {date_str}\n"
                   f"💇‍♀️ Услуга: {r['service'] or '—'}\n"
                   f"📏 Длина волос: {r['length_shape']}")
        await message.answer_photo(r["hand_photo_file_id"], caption=caption)
    await message.answer("Админ-меню:", reply_markup=admin_main_kb())


@router.message(IsAdmin(), F.text.regexp(r"^@[A-Za-z0-9_]+$"))
async def admin_client_by_username(message: Message, state: FSMContext):
    un = message.text[1:].strip()
    rows = db_all("SELECT * FROM bookings WHERE username=? AND status='confirmed' ORDER BY booking_datetime", (un,))
    if not rows:
        for c in db_all("SELECT DISTINCT user_id FROM bookings WHERE status='confirmed'"):
            try:
                chat = await message.bot.get_chat(c["user_id"])
                if chat.username and chat.username.lower() == un.lower():
                    rows = db_all("SELECT * FROM bookings WHERE user_id=? AND status='confirmed' ORDER BY booking_datetime", (c["user_id"],))
                    break
            except Exception:
                pass
    await show_client_cards(message, rows)


@router.message(IsAdmin(), F.text.regexp(r"^клиент #\d+$"))
async def admin_client_by_id(message: Message, state: FSMContext):
    uid = int(re.search(r"\d+", message.text).group())
    rows = db_all("SELECT * FROM bookings WHERE user_id=? AND status='confirmed' ORDER BY booking_datetime", (uid,))
    await show_client_cards(message, rows)


@router.message(IsAdmin(), F.text == "Назад")
async def admin_back(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Админ-меню:", reply_markup=admin_main_kb())


# ============= ПРОМОКОДЫ =============

async def show_promos_menu(target, state: FSMContext, edit: bool = False):
    promos = db_all("SELECT * FROM promos ORDER BY id DESC")
    if not promos:
        text = "🎟 Промокодов пока нет."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать промокод", callback_data="promo_new")]])
    else:
        lines = ["🎟 <b>Промокоды:</b>\n"]
        for p in promos:
            t = "персональный" if p["type"] == "personal" else "обычный"
            uses_str = f"{p['used_count']}/{p['max_uses']}" if p["type"] == "personal" else f"использован {p['used_count']} раз"
            lines.append(f"• <b>{p['code']}</b> — {p['discount']}% ({t}, {uses_str})")
        text = "\n".join(lines)
        buttons = [[InlineKeyboardButton(text=f"🗑 Удалить {p['code']}", callback_data=f"promo_del:{p['id']}")] for p in promos]
        buttons.append([InlineKeyboardButton(text="➕ Создать промокод", callback_data="promo_new")])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    if edit and hasattr(target, "edit_text"):
        try:
            await target.edit_text(text, reply_markup=kb); return
        except Exception:
            pass
    await target.answer(text, reply_markup=kb)


@router.message(IsAdmin(), F.text == "Промокод")
async def admin_promo(message: Message, state: FSMContext):
    await state.clear()
    await show_promos_menu(message, state)


@router.callback_query(F.data == "promo_new")
async def promo_new(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Обычный", callback_data="promo_type:ordinary")],
        [InlineKeyboardButton(text="Персональный", callback_data="promo_type:personal")],
        [InlineKeyboardButton(text="⬅️ Назад к списку", callback_data="promo_back")]])
    await cb.message.answer("Выберите тип промокода:", reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == "promo_back")
async def promo_back(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.clear()
    await show_promos_menu(cb.message, state)
    await cb.answer()


@router.callback_query(F.data.startswith("promo_type:"))
async def promo_type_selected(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    ptype = cb.data.split(":")[1]
    await state.clear()
    await state.update_data(promo_type=ptype)
    await state.set_state(AdminStates.promo_code)
    await cb.message.answer("Введите код промокода:")
    await cb.answer()


@router.message(AdminStates.promo_code)
async def promo_code_entered(message: Message, state: FSMContext):
    code = (message.text or "").strip()
    if not code:
        await message.answer("Введите код текстом:"); return
    if db_one("SELECT id FROM promos WHERE code=?", (code,)):
        await message.answer("Такой промокод уже есть. Введи другой:"); return
    await state.update_data(promo_code=code)
    await state.set_state(AdminStates.promo_discount)
    await message.answer("Введите скидку в % (1-100):")


@router.message(AdminStates.promo_discount)
async def promo_discount_entered(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Введите число (например 10):"); return
    discount = int(text)
    if not (1 <= discount <= 100):
        await message.answer("1-100, попробуйте снова:"); return
    data = await state.get_data()
    code = data.get("promo_code"); ptype = data.get("promo_type", "ordinary")
    if not code:
        await message.answer("Сессия потеряна. Промокод заново.")
        await state.clear()
        await message.answer("Админ-меню:", reply_markup=admin_main_kb()); return
    max_uses = 1 if ptype == "personal" else 999999
    try:
        db_exec("INSERT INTO promos(code, discount, type, max_uses) VALUES(?,?,?,?)", (code, discount, ptype, max_uses))
        await message.answer(f"✅ Промокод успешно создан!\n\nКод: <b>{code}</b>\nСкидка: <b>{discount}%</b>\nТип: <b>{'персональный' if ptype == 'personal' else 'обычный'}</b>")
    except sqlite3.IntegrityError:
        await message.answer("Такой промокод уже есть в базе.")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
    await state.clear()
    await show_promos_menu(message, state)
    await message.answer("Админ-меню:", reply_markup=admin_main_kb())


@router.callback_query(F.data.startswith("promo_del:"))
async def promo_delete(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    pid = int(cb.data.split(":")[1])
    promo = db_one("SELECT * FROM promos WHERE id=?", (pid,))
    if not promo:
        await cb.answer("Не найден", show_alert=True); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"promo_del_ok:{pid}")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="promo_back")]])
    try:
        await cb.message.edit_text(f"Удалить промокод <b>{promo['code']}</b> ({promo['discount']}%)?", reply_markup=kb)
    except Exception:
        await cb.message.answer(f"Удалить промокод <b>{promo['code']}</b> ({promo['discount']}%)?", reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("promo_del_ok:"))
async def promo_delete_ok(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    pid = int(cb.data.split(":")[1])
    promo = db_one("SELECT * FROM promos WHERE id=?", (pid,))
    if not promo:
        await cb.answer("Не найден", show_alert=True); return
    db_exec("DELETE FROM promos WHERE id=?", (pid,))
    await cb.answer(f"Промокод {promo['code']} удалён")
    await show_promos_menu(cb.message, state, edit=True)


# ============= РЕКВИЗИТЫ =============

@router.message(IsAdmin(), F.text == "Реквизиты")
async def admin_set_payment(message: Message, state: FSMContext):
    await state.set_state(AdminStates.set_payment)
    await message.answer("Введите реквизиты для оплаты предоплаты:")


@router.message(AdminStates.set_payment)
async def set_payment(message: Message, state: FSMContext):
    db_exec("INSERT OR REPLACE INTO settings(key, value) VALUES('payment_details', ?)", (message.text,))
    await message.answer("Реквизиты сохранены.")
    await state.clear()
    await message.answer("Админ-меню:", reply_markup=admin_main_kb())


# ============= МОИ ЗАПИСИ =============

def _session_action_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔁 Перенести"), KeyboardButton(text="⏰ Задержусь")],
        [KeyboardButton(text="❌ Отменить"), KeyboardButton(text="Назад")],
    ], resize_keyboard=True)


async def _notify_admin_action(bot, action_emoji, action_label, b, extra=""):
    month_row = db_one("SELECT * FROM months WHERE id=?", (b["month_id"],))
    month_num = month_row["month_num"] if month_row else "?"
    un = await resolve_username(bot, b["user_id"], b["username"])
    un_str = f"@{un}" if un else f"id {b['user_id']}"
    text = (f"{action_emoji} {action_label}\n\nКлиент: {un_str}\nИмя: {b['name']}\n"
            f"Телефон: {b['phone']}\n📅 {b['day']:02d}.{month_num:02d} {b['time']}\n"
            f"💇‍♀️ {b['service'] or '—'}")
    if extra:
        text += f"\n\n{extra}"
    await send_to_admins(bot, text=text)


@router.message(F.text == "Мои записи")
async def user_my_records(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    rows = db_all("SELECT * FROM bookings WHERE user_id=? AND status='confirmed' ORDER BY booking_datetime", (uid,))
    if not rows:
        await message.answer("У тебя пока нет записей.", reply_markup=user_main_kb()); return
    buttons = []
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["booking_datetime"])
            label = f"📅 {dt.strftime('%d.%m %H:%M')}"
        except Exception:
            label = f"📅 {r['day']:02d}.{r['time']}"
        buttons.append([KeyboardButton(text=label)])
    buttons.append([KeyboardButton(text="Назад")])
    await state.set_state(UserStates.my_records_menu)
    await message.answer("📋 Твои записи. Выбери сеанс:",
                         reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True))


@router.message(UserStates.my_records_menu)
async def user_pick_session(message: Message, state: FSMContext):
    if message.text == "Назад":
        await state.clear()
        await message.answer("Главное меню:", reply_markup=user_main_kb()); return
    text = (message.text or "").strip()
    m = re.search(r"(\d{2})\.(\d{2})\s+(\d{2}):(\d{2})", text)
    if not m:
        await message.answer("Выбери сеанс из кнопок."); return
    day, month, hh, mm = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    uid = message.from_user.id
    rows = db_all("SELECT * FROM bookings WHERE user_id=? AND status='confirmed' ORDER BY booking_datetime", (uid,))
    found = None
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["booking_datetime"])
            if dt.day == day and dt.month == month and dt.hour == hh and dt.minute == mm:
                found = (r, dt); break
        except Exception:
            pass
    if not found:
        await message.answer("Сеанс не найден."); return
    r, dt = found
    month_row = db_one("SELECT * FROM months WHERE id=?", (r["month_id"],))
    month_num = month_row["month_num"] if month_row else "?"
    await state.update_data(session_id=r["id"])
    await state.set_state(UserStates.booking_actions)
    await message.answer(f"📋 Твоя запись:\n\n📅 {r['day']:02d}.{month_num:02d} {r['time']}\n"
                         f"💇‍♀️ {r['service'] or '—'}\n"
                         f"📏 Длина волос: {r['length_shape']}\n📞 {r['phone']}\n\nЧто хочешь сделать?",
                         reply_markup=_session_action_kb())


@router.message(UserStates.booking_actions)
async def user_session_action(message: Message, state: FSMContext):
    data = await state.get_data()
    bid = data.get("session_id")
    if not bid:
        await state.clear()
        await message.answer("Сессия устарела: /start", reply_markup=user_main_kb()); return
    b = db_one("SELECT * FROM bookings WHERE id=?", (bid,))
    if not b or b["status"] != "confirmed":
        await state.clear()
        await message.answer("Запись не найдена.", reply_markup=user_main_kb()); return
    t = (message.text or "").strip()
    if t == "Назад":
        await user_my_records(message, state); return
    if t == "🔁 Перенести":
        dt = datetime.fromisoformat(b["booking_datetime"])
        if dt - now() < timedelta(days=REFUND_DAYS):
            await message.answer(f"❌ Перенести можно не позднее чем за {REFUND_DAYS} дня до записи."); return
        await _notify_admin_action(message.bot, "🔁", "Клиентка хочет перенести запись", b)
        await state.update_data(mode="resched", resched_booking_id=bid)
        months = get_months()
        if len(months) == 1:
            mo = months[0]
            await state.update_data(month_id=mo["id"], month_num=mo["month_num"],
                                    month_name=mo["name"], month_year=mo["year"])
            await show_user_days(message, state)
        else:
            kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=f"{mo['name']} {mo['year']}")] for mo in months]
                                     + [[KeyboardButton(text="Назад")]], resize_keyboard=True)
            await state.set_state(UserStates.booking_month)
            await message.answer("Выбери месяц:", reply_markup=kb)
        return
    if t == "❌ Отменить":
        month_row = db_one("SELECT * FROM months WHERE id=?", (b["month_id"],))
        month_num = month_row["month_num"] if month_row else "?"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, отменить", callback_data=f"cancel_confirm:{bid}")],
            [InlineKeyboardButton(text="❌ Нет", callback_data="cancel_decline")]])
        await message.answer(f"❓ Подтвердить отмену?\n\n📅 {b['day']:02d}.{month_num:02d} {b['time']}\n"
                             f"💇‍♀️ {b['service'] or '—'}", reply_markup=kb)
        return
    if t == "⏰ Задержусь":
        await state.set_state(UserStates.delay_input)
        await message.answer("На сколько задержишься? (например: 15 минут)\nЕсли передумала — «Назад».",
                             reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Назад")]], resize_keyboard=True))
        return
    await message.answer("Выбери действие из кнопок.", reply_markup=_session_action_kb())


@router.callback_query(F.data.startswith("cancel_confirm:"))
async def cancel_confirm(cb: CallbackQuery, state: FSMContext):
    bid = int(cb.data.split(":")[1])
    b = db_one("SELECT * FROM bookings WHERE id=?", (bid,))
    if not b or b["user_id"] != cb.from_user.id:
        await cb.answer("Не найдено"); return
    if b["status"] != "confirmed":
        await cb.answer("Уже отменена"); return
    dt = datetime.fromisoformat(b["booking_datetime"])
    diff = dt - now()
    db_exec("UPDATE bookings SET status='cancelled' WHERE id=?", (bid,))
    db_exec("UPDATE slots SET is_booked=0 WHERE id=?", (b["slot_id"],))
    _release_slot(b["slot_id"])
    if diff >= timedelta(days=REFUND_DAYS):
        user_msg = (f"❌ Запись отменена.\n\n💰 До записи {REFUND_DAYS} дня и больше — "
                    f"предоплата (1000 руб, входит в стоимость) возвращается.\n"
                    f"Мастер скоро напишет тебе для возврата.")
        refund_str = f"💰 Предоплата 1000 руб подлежит возврату (≥ {REFUND_DAYS} дней)"
    else:
        user_msg = (f"❌ Запись отменена.\n\n💰 До записи менее {REFUND_DAYS} дней — "
                    f"предоплата (1000 руб) НЕ возвращается.")
        refund_str = f"💰 Предоплата 1000 руб НЕ возвращается (< {REFUND_DAYS} дней)"
    try:
        await cb.message.edit_text(user_msg)
    except Exception:
        await cb.message.answer(user_msg)
    await _notify_admin_action(cb.bot, "❌", "Клиентка ОТМЕНИЛА запись", b, extra=refund_str)
    await state.clear()
    await cb.message.answer("Главное меню:", reply_markup=user_main_kb())
    await cb.answer("Отменено")


@router.callback_query(F.data == "cancel_decline")
async def cancel_decline(cb: CallbackQuery, state: FSMContext):
    try:
        await cb.message.edit_text("Хорошо, запись в силе 🙂")
    except Exception:
        pass
    data = await state.get_data()
    bid = data.get("session_id")
    b = db_one("SELECT * FROM bookings WHERE id=?", (bid,)) if bid else None
    if b and b["status"] == "confirmed":
        await state.set_state(UserStates.booking_actions)
        await cb.message.answer("Выбери действие:", reply_markup=_session_action_kb())
    else:
        await state.clear()
        await cb.message.answer("Главное меню:", reply_markup=user_main_kb())
    await cb.answer()


@router.message(UserStates.delay_input)
async def user_delay_input(message: Message, state: FSMContext):
    t = (message.text or "").strip()
    if t == "Назад":
        data = await state.get_data()
        bid = data.get("session_id")
        b = db_one("SELECT * FROM bookings WHERE id=?", (bid,)) if bid else None
        if b and b["status"] == "confirmed":
            await state.set_state(UserStates.booking_actions)
            await message.answer("Выбери действие:", reply_markup=_session_action_kb())
        else:
            await state.clear()
            await message.answer("Главное меню:", reply_markup=user_main_kb())
        return
    if not t:
        await message.answer("Напиши на сколько:"); return
    data = await state.get_data()
    bid = data.get("session_id")
    b = db_one("SELECT * FROM bookings WHERE id=?", (bid,)) if bid else None
    if not b:
        await state.clear()
        await message.answer("Запись не найдена.", reply_markup=user_main_kb()); return
    await _notify_admin_action(message.bot, "⏰", "Клиентка ЗАДЕРЖИТСЯ", b, extra=f"⏱ Задержка: {t}")
    await message.answer("✅ Сообщение отправлено мастеру.", reply_markup=user_main_kb())
    await state.clear()


# ============= ФЛОУ ЗАПИСИ =============

@router.message(F.text == "Записаться")
async def user_book(message: Message, state: FSMContext):
    _release_user_reservations(message.from_user.id)
    db_exec("DELETE FROM booking_drafts WHERE user_id=?", (message.from_user.id,))
    await state.clear()
    await state.update_data(mode="book")
    months = get_months()
    if not months:
        await message.answer("Пока нет доступных записей."); return
    if len(months) == 1:
        m = months[0]
        await state.update_data(month_id=m["id"], month_num=m["month_num"],
                                month_name=m["name"], month_year=m["year"])
        await show_user_days(message, state)
    else:
        kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=f"{m['name']} {m['year']}")] for m in months]
                                 + [[KeyboardButton(text="Назад")]], resize_keyboard=True)
        await state.set_state(UserStates.booking_month)
        await message.answer("Выбери месяц:", reply_markup=kb)


async def show_user_days(message: Message, state: FSMContext):
    data = await state.get_data()
    mid = data["month_id"]
    month = db_one("SELECT * FROM months WHERE id=?", (mid,))
    slots = get_slots(mid)
    days = sorted(set(s["day"] for s in slots))
    if not days:
        await message.answer("Нет доступных дней."); return
    buttons = [[KeyboardButton(text=f"{d:02d}.{month['month_num']:02d}")] for d in days]
    buttons.append([KeyboardButton(text="Назад")])
    await state.set_state(UserStates.booking_day)
    await message.answer("Выбери день:", reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True))


async def show_user_times(message: Message, state: FSMContext):
    data = await state.get_data()
    mid = data["month_id"]; day = data["day"]
    month = db_one("SELECT * FROM months WHERE id=?", (mid,))
    slots = db_all("SELECT * FROM slots WHERE month_id=? AND day=? AND is_booked=0 ORDER BY time", (mid, day))
    if not slots:
        await message.answer("На этот день нет свободного времени. Выбери другой день.")
        await show_user_days(message, state); return
    uid = message.from_user.id
    buttons = []
    for s in slots:
        label = s["time"]
        r = db_one("SELECT * FROM slot_reservations WHERE slot_id=?", (s["id"],))
        if r and r["user_id"] != uid:
            if r["is_forever"] or _elapsed_now(r) < RESERVE_TOTAL:
                label = f"⏳ {s['time']}"
        buttons.append([KeyboardButton(text=label)])
    buttons.append([KeyboardButton(text="Назад")])
    await state.set_state(UserStates.booking_time)
    await message.answer(f"Выбери время на {day:02d}.{month['month_num']:02d}:",
                         reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True))


@router.message(UserStates.booking_month)
async def user_month_selected(message: Message, state: FSMContext):
    if message.text == "Назад":
        await state.clear()
        await message.answer("Главное меню:", reply_markup=user_main_kb()); return
    parts = message.text.split()
    if len(parts) >= 2 and parts[-1].isdigit():
        year = int(parts[-1]); name = " ".join(parts[:-1])
    else:
        year = now().year; name = message.text
    row = db_one("SELECT * FROM months WHERE name=? AND year=?", (name, year))
    if not row:
        await message.answer("Выбери месяц из кнопок."); return
    await state.update_data(month_id=row["id"], month_num=row["month_num"],
                            month_name=row["name"], month_year=row["year"])
    await show_user_days(message, state)


@router.message(UserStates.booking_day)
async def user_day_selected(message: Message, state: FSMContext):
    if message.text == "Назад":
        await state.clear()
        await message.answer("Главное меню:", reply_markup=user_main_kb())
        return
    try:
        day = int(message.text.split(".")[0])
    except Exception:
        await message.answer("Выбери день из кнопок."); return
    await state.update_data(day=day)
    await show_user_times(message, state)


@router.message(UserStates.booking_time)
async def user_time_selected(message: Message, state: FSMContext):
    if message.text == "Назад":
        await show_user_days(message, state); return
    raw = (message.text or "").strip()
    time_str = raw.replace("⏳", "").strip()
    data = await state.get_data()
    mid = data["month_id"]; day = data["day"]
    slot = db_one("SELECT * FROM slots WHERE month_id=? AND day=? AND time=?", (mid, day, time_str))
    if not slot:
        await message.answer("Выбери время из кнопок."); return
    if slot["is_booked"]:
        await message.answer("❌ Это время уже занято. Выбери другое.")
        await show_user_times(message, state); return
    uid = message.from_user.id
    if not _slot_is_free_for(slot["id"], uid):
        await message.answer("❌ Другая клиентка уже резервирует это время.\nПопробуй через 5 минут.")
        await show_user_times(message, state); return
    _reserve_slot(slot["id"], uid)
    print(f"[RESERVE] Слот {slot['id']} за {uid}")
    await state.update_data(slot_id=slot["id"], time=time_str)
    if data.get("mode") == "resched":
        await user_resched_confirm(message, state); return
    await state.set_state(UserStates.booking_service)
    _save_draft(uid, "UserStates:booking_service", await state.get_data(), slot["id"])
    month_num = data.get("month_num", 0)
    await message.answer(
        f"⏳ Окошко {day:02d}.{month_num:02d} {time_str} забронировано за тобой.\n\n"
        f"Перестанешь мне отвечать, Кисуль. Включу 5 минутный таймер.\n\n"
        f"Выбери услугу:",
        reply_markup=services_kb())


@router.message(UserStates.booking_service)
async def user_service_selected(message: Message, state: FSMContext):
    _on_user_action(message.from_user.id)
    t = (message.text or "").strip()
    if t not in SERVICES:
        await message.answer("Выбери услугу из кнопок.", reply_markup=services_kb()); return
    await state.update_data(service=t)
    data = await state.get_data()
    await state.set_state(UserStates.booking_contraindications)
    if data.get("slot_id"):
        _save_draft(message.from_user.id, "UserStates:booking_contraindications",
                    await state.get_data(), data["slot_id"])
    await message.answer(CONTRAINDICATIONS_TEXT, reply_markup=contraindications_kb())


@router.message(UserStates.booking_contraindications)
async def user_contraindications(message: Message, state: FSMContext):
    _on_user_action(message.from_user.id)
    t = (message.text or "").strip()
    if t != "Продолжить":
        await message.answer(CONTRAINDICATIONS_TEXT, reply_markup=contraindications_kb()); return
    data = await state.get_data()
    await state.set_state(UserStates.booking_hair_photo)
    if data.get("slot_id"):
        _save_draft(message.from_user.id, "UserStates:booking_hair_photo",
                    await state.get_data(), data["slot_id"])
    await message.answer("Отправь фото волос сзади:", reply_markup=cancel_booking_kb())


async def user_resched_confirm(message: Message, state: FSMContext):
    data = await state.get_data()
    month = db_one("SELECT * FROM months WHERE id=?", (data["month_id"],))
    old = db_one("SELECT * FROM bookings WHERE id=?", (data["resched_booking_id"],))
    new_str = f"{data['day']:02d}.{month['month_num']:02d} {data['time']}"
    old_dt = datetime.fromisoformat(old["booking_datetime"])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="resched_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="resched_cancel")]])
    await message.answer(f"Перенести запись с {old_dt.strftime('%d.%m %H:%M')} на {new_str}?", reply_markup=kb)


@router.callback_query(F.data == "resched_confirm")
async def resched_confirm(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    bid = data.get("resched_booking_id")
    if not bid:
        await cb.answer("Сессия устарела"); return
    old = db_one("SELECT * FROM bookings WHERE id=?", (bid,))
    month = db_one("SELECT * FROM months WHERE id=?", (data["month_id"],))
    new_dt = datetime(month["year"], month["month_num"], data["day"], *map(int, data["time"].split(":")), tzinfo=TZ)
    db_exec("INSERT OR REPLACE INTO pending_reschedules(booking_id, new_slot_id, new_month_id, new_day, new_time, new_datetime) "
            "VALUES(?,?,?,?,?,?)",
            (bid, data["slot_id"], data["month_id"], data["day"], data["time"], new_dt.isoformat()))
    dn = await display_name(cb.bot, cb.from_user.id, cb.from_user.username)
    old_dt = datetime.fromisoformat(old["booking_datetime"])
    text = (f"🔁 Запрос на перенос\nКлиент: {dn}\nБыло: {old_dt.strftime('%d.%m %H:%M')}\n"
            f"Станет: {new_dt.strftime('%d.%m %H:%M')}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"admin_resched_ok:{bid}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_resched_no:{bid}")]])
    await send_to_admins(cb.bot, text=text, reply_markup=kb)
    await cb.message.edit_text("Запрос отправлен мастеру.")
    await state.clear(); await cb.answer()


@router.callback_query(F.data == "resched_cancel")
async def resched_cancel(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("slot_id"):
        _release_slot(data["slot_id"])
    await state.clear()
    await cb.message.edit_text("Отменено.")
    await cb.message.answer("Главное меню:", reply_markup=user_main_kb())
    await cb.answer()


@router.callback_query(F.data.startswith("admin_resched_ok:"))
async def admin_resched_ok(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    bid = int(cb.data.split(":")[1])
    pr = db_one("SELECT * FROM pending_reschedules WHERE booking_id=?", (bid,))
    booking = db_one("SELECT * FROM bookings WHERE id=?", (bid,))
    if not pr or not booking:
        await cb.answer("Не найдено"); return
    db_exec("UPDATE slots SET is_booked=0 WHERE id=?", (booking["slot_id"],))
    db_exec("UPDATE slots SET is_booked=1 WHERE id=?", (pr["new_slot_id"],))
    _release_slot(pr["new_slot_id"])
    db_exec("UPDATE bookings SET slot_id=?, month_id=?, day=?, time=?, booking_datetime=?, arrival_notified=0 WHERE id=?",
            (pr["new_slot_id"], pr["new_month_id"], pr["new_day"], pr["new_time"], pr["new_datetime"], bid))
    db_exec("DELETE FROM pending_reschedules WHERE booking_id=?", (bid,))
    new_dt = datetime.fromisoformat(pr["new_datetime"])
    try:
        await cb.bot.send_message(booking["user_id"], f"✅ Перенос подтверждён: {new_dt.strftime('%d.%m %H:%M')}",
                                  reply_markup=user_main_kb())
    except Exception:
        pass
    try:
        await cb.message.edit_text((cb.message.text or "") + "\n\n✅ Перенос подтверждён")
    except Exception:
        pass
    await cb.answer("ОК")


@router.callback_query(F.data.startswith("admin_resched_no:"))
async def admin_resched_no(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    bid = int(cb.data.split(":")[1])
    booking = db_one("SELECT * FROM bookings WHERE id=?", (bid,))
    pr = db_one("SELECT * FROM pending_reschedules WHERE booking_id=?", (bid,))
    if pr:
        _release_slot(pr["new_slot_id"])
    db_exec("DELETE FROM pending_reschedules WHERE booking_id=?", (bid,))
    if booking:
        try:
            await cb.bot.send_message(booking["user_id"], "❌ Перенос отклонён мастером.")
        except Exception:
            pass
    try:
        await cb.message.edit_text((cb.message.text or "") + "\n\n❌ Отклонён")
    except Exception:
        pass
    await cb.answer("Отклонено")


# ============= АНКЕТА КЛИЕНТА =============

@router.message(UserStates.booking_hair_photo, F.photo)
async def user_hair_photo(message: Message, state: FSMContext):
    _on_user_action(message.from_user.id)
    await state.update_data(hand_photo_file_id=message.photo[-1].file_id)
    await state.set_state(UserStates.booking_hair_confirm)
    data = await state.get_data()
    if data.get("slot_id"):
        _save_draft(message.from_user.id, "UserStates:booking_hair_confirm", data, data["slot_id"])
    await message.answer("Фото отправила, правильно?", reply_markup=yes_reset_kb())


@router.message(UserStates.booking_hair_photo)
async def user_hair_photo_invalid(message: Message):
    _on_user_action(message.from_user.id)
    await message.answer("Пожалуйста, отправь фото волос сзади.")


@router.message(UserStates.booking_hair_confirm)
async def user_hair_confirm(message: Message, state: FSMContext):
    _on_user_action(message.from_user.id)
    if message.text == "Да":
        data = await state.get_data()
        if data.get("slot_id"):
            _save_draft(message.from_user.id, "UserStates:booking_length", data, data["slot_id"])
        await state.set_state(UserStates.booking_length)
        await message.answer("Введи длину волос:", reply_markup=cancel_booking_kb())
    elif message.text == "Сбросить":
        await state.update_data(hand_photo_file_id=None)
        await state.set_state(UserStates.booking_hair_photo)
        await message.answer("Отправь фото волос сзади заново:", reply_markup=cancel_booking_kb())
    else:
        await message.answer("Нажми 'Да' или 'Сбросить'.", reply_markup=yes_reset_kb())


@router.message(UserStates.booking_length)
async def user_length(message: Message, state: FSMContext):
    _on_user_action(message.from_user.id)
    if not message.text:
        await message.answer("Введи текстом длину волос."); return
    await state.update_data(length_shape=message.text)
    await state.set_state(UserStates.booking_phone)
    data = await state.get_data()
    if data.get("slot_id"):
        _save_draft(message.from_user.id, "UserStates:booking_phone", data, data["slot_id"])
    await message.answer("Введи номер телефона:", reply_markup=cancel_booking_kb())


@router.message(UserStates.booking_phone)
async def user_phone(message: Message, state: FSMContext):
    _on_user_action(message.from_user.id)
    if not message.text:
        await message.answer("Введи номер телефона текстом."); return
    await state.update_data(phone=message.text)
    await state.set_state(UserStates.booking_name)
    data = await state.get_data()
    if data.get("slot_id"):
        _save_draft(message.from_user.id, "UserStates:booking_name", data, data["slot_id"])
    await message.answer("Введи имя:", reply_markup=cancel_booking_kb())


@router.message(UserStates.booking_name)
async def user_name(message: Message, state: FSMContext):
    _on_user_action(message.from_user.id)
    if not message.text:
        await message.answer("Введи имя текстом."); return
    await state.update_data(name=message.text)
    await state.set_state(UserStates.booking_promo)
    data = await state.get_data()
    if data.get("slot_id"):
        _save_draft(message.from_user.id, "UserStates:booking_promo", data, data["slot_id"])
    await message.answer("Введи промокод, если есть, или напиши 'нет':", reply_markup=cancel_booking_kb())


@router.message(UserStates.booking_promo)
async def user_promo(message: Message, state: FSMContext):
    _on_user_action(message.from_user.id)
    if not message.text:
        await message.answer("Введи промокод или 'нет'."); return
    text = message.text.strip()
    data = await state.get_data()
    discount = 0; promo_code = None
    if text.lower() not in ("нет", "-", "no"):
        promo = db_one("SELECT * FROM promos WHERE code=?", (text,))
        if not promo:
            await message.answer("Промокод не найден. Введи другой или 'нет'."); return
        if promo["type"] == "personal" and promo["used_count"] >= promo["max_uses"]:
            await message.answer("Этот промокод уже использован. Введи другой или 'нет'."); return
        discount = promo["discount"]; promo_code = text
    await state.update_data(discount=discount, promo_code=promo_code)
    month = db_one("SELECT * FROM months WHERE id=?", (data["month_id"],))
    dt_str = f"{data['day']:02d}.{month['month_num']:02d} {data['time']}"
    text_sum = (f"Проверь данные:\nИмя: {data['name']}\nТелефон: {data['phone']}\n"
                f"Услуга: {data.get('service')}\n"
                f"Длина волос: {data['length_shape']}\nДата: {dt_str}\n"
                f"Промокод: {promo_code or 'нет'} (скидка {discount}%)")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="user_confirm_booking")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="user_cancel_booking")]])
    await state.set_state(UserStates.booking_confirm)
    if data.get("slot_id"):
        _save_draft(message.from_user.id, "UserStates:booking_confirm", await state.get_data(), data["slot_id"])
    await message.answer(text_sum, reply_markup=kb)


@router.callback_query(F.data == "user_confirm_booking")
async def user_confirm_booking(cb: CallbackQuery, state: FSMContext):
    _on_user_action(cb.from_user.id)
    data = await state.get_data()
    month = db_one("SELECT * FROM months WHERE id=?", (data["month_id"],))
    day = data["day"]; time = data["time"]
    h, m = map(int, time.split(":"))
    dt = datetime(month["year"], month["month_num"], day, h, m, tzinfo=TZ)
    cur = db_exec("""INSERT INTO bookings(user_id, username, name, phone, hand_photo_file_id, length_shape,
        reference, promo_code, discount, status, slot_id, month_id, day, time, booking_datetime, created_at, service)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        cb.from_user.id, cb.from_user.username, data["name"], data["phone"],
        data["hand_photo_file_id"], data["length_shape"], None,
        data.get("promo_code"), data.get("discount", 0), "pending_payment",
        data["slot_id"], data["month_id"], day, time, dt.isoformat(), now_iso(),
        data.get("service")))
    await state.update_data(booking_id=cur.lastrowid)
    _save_draft(cb.from_user.id, "UserStates:booking_payment_screenshot",
                await state.get_data(), data["slot_id"])
    await cb.message.edit_text("Данные приняты.")
    await cb.message.answer(
        f"💰 Предоплата 1000 руб (ВХОДИТ в стоимость услуги).\n\n"
        f"Реквизиты:\n{get_payment_details()}\n\nОплати и отправь скрин сюда.",
        reply_markup=cancel_booking_kb())
    await state.set_state(UserStates.booking_payment_screenshot)
    await cb.answer()


@router.callback_query(F.data == "user_cancel_booking")
async def user_cancel_booking(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("slot_id"):
        _release_slot(data["slot_id"])
    db_exec("DELETE FROM booking_drafts WHERE user_id=?", (cb.from_user.id,))
    await state.clear()
    await cb.message.edit_text("Отменено.")
    await cb.message.answer("Главное меню:", reply_markup=user_main_kb())
    await cb.answer()


@router.message(UserStates.booking_payment_screenshot, F.photo)
async def user_payment_screenshot(message: Message, state: FSMContext):
    data = await state.get_data()
    bid = data.get("booking_id")
    if not bid:
        await message.answer("Что-то пошло не так: /start", reply_markup=user_main_kb())
        await state.clear(); return
    file_id = message.photo[-1].file_id
    db_exec("UPDATE bookings SET payment_screenshot_file_id=? WHERE id=?", (file_id, bid))
    booking = db_one("SELECT * FROM bookings WHERE id=?", (bid,))
    if booking and booking["slot_id"]:
        _reserve_slot_forever(booking["slot_id"], message.from_user.id)
        print(f"[FOREVER] Слот {booking['slot_id']} → бесконечный резерв")
    db_exec("DELETE FROM booking_drafts WHERE user_id=?", (message.from_user.id,))
    media = [InputMediaPhoto(media=booking["hand_photo_file_id"])]
    media.append(InputMediaPhoto(media=file_id))
    caption = await build_booking_caption(message.bot, booking, is_extra=False)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"admin_confirm_booking:{bid}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_cancel_booking:{bid}")]])
    try:
        await send_to_admins(message.bot, media=media, caption=caption, reply_markup=kb)
    except Exception as e:
        print(f"send media error: {e}")
        for aid in ADMIN_IDS:
            try:
                await message.bot.send_photo(aid, booking["hand_photo_file_id"])
                await message.bot.send_photo(aid, file_id)
                await message.bot.send_message(aid, caption, reply_markup=kb)
            except Exception as e2:
                print(f"fallback error {aid}: {e2}")
    await message.answer("Скрин отправлен мастеру ✅\nОжидай подтверждения. Окошко держится за тобой.",
                         reply_markup=user_main_kb())
    await state.clear()


@router.callback_query(F.data.startswith("admin_confirm_booking:"))
async def admin_confirm_booking(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    bid = int(cb.data.split(":")[1])
    booking = db_one("SELECT * FROM bookings WHERE id=?", (bid,))
    if not booking:
        await cb.answer("Не найдено"); return
    db_exec("UPDATE bookings SET status='confirmed', arrival_notified=0 WHERE id=?", (bid,))
    db_exec("UPDATE slots SET is_booked=1 WHERE id=?", (booking["slot_id"],))
    _release_slot(booking["slot_id"])
    db_exec("DELETE FROM booking_drafts WHERE user_id=?", (booking["user_id"],))
    if booking["promo_code"]:
        db_exec("UPDATE promos SET used_count = used_count + 1 WHERE code=?", (booking["promo_code"],))
    dt = datetime.fromisoformat(booking["booking_datetime"])
    current = now()
    if dt - timedelta(days=1) > current:
        scheduler.add_job(send_reminder, "date", run_date=dt - timedelta(days=1),
                          args=[cb.bot, booking["user_id"],
                                f"Напоминание: завтра у вас запись на {dt.strftime('%d.%m %H:%M')}"])
    if dt - timedelta(hours=1) > current:
        scheduler.add_job(send_reminder, "date", run_date=dt - timedelta(hours=1),
                          args=[cb.bot, booking["user_id"],
                                f"Напоминание: через час у вас запись на {dt.strftime('%d.%m %H:%M')}"])
    try:
        await cb.bot.send_message(booking["user_id"],
            f"✅ Твоя запись подтверждена: {dt.strftime('%d.%m %H:%M')}\n\n"
            f"Управлять записью можно в разделе «Мои записи».",
            reply_markup=user_main_kb())
    except Exception as e:
        print(f"notify error: {e}")
    await edit_admin_message(cb, "✅ Подтверждено")
    await cb.answer("Подтверждено")


@router.callback_query(F.data.startswith("admin_finish_booking:"))
async def admin_finish_booking(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    bid = int(cb.data.split(":")[1])
    booking = db_one("SELECT * FROM bookings WHERE id=?", (bid,))
    if not booking:
        await cb.answer("Не найдено", show_alert=True); return
    if booking["status"] not in ("confirmed", "pending_payment"):
        await cb.answer("Запись уже неактивна", show_alert=True); return

    db_exec("UPDATE bookings SET status='completed' WHERE id=?", (bid,))

    try:
        await cb.bot.send_message(
            booking["user_id"],
            "✅ Спасибо за визит! Ждём тебя снова 💇‍♀️",
            reply_markup=user_main_kb()
        )
    except Exception as e:
        print(f"finish notify error: {e}")

    await edit_admin_message(cb, "✅ Приём завершён")
    await cb.answer("Готово")


@router.callback_query(F.data.startswith("admin_cancel_booking:"))
async def admin_cancel_booking(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    bid = int(cb.data.split(":")[1])
    booking = db_one("SELECT * FROM bookings WHERE id=?", (bid,))
    if not booking:
        await cb.answer("Не найдено"); return
    orig = cb.message.text or cb.message.caption or ""
    await state.update_data(cancel_booking_id=bid,
                            cancel_msg_id=cb.message.message_id,
                            cancel_chat_id=cb.message.chat.id,
                            cancel_orig=orig)
    await state.set_state(AdminStates.booking_cancel_reason)
    await cb.message.answer("Введите причину отмены записи — она уйдёт клиентке:")
    await cb.answer()


@router.message(AdminStates.booking_cancel_reason)
async def admin_booking_cancel_reason(message: Message, state: FSMContext):
    data = await state.get_data()
    bid = data.get("cancel_booking_id")
    reason = (message.text or "").strip() or "Без указания причины"
    booking = db_one("SELECT * FROM bookings WHERE id=?", (bid,))
    if booking:
        db_exec("UPDATE bookings SET status='cancelled' WHERE id=?", (bid,))
        db_exec("UPDATE slots SET is_booked=0 WHERE id=?", (booking["slot_id"],))
        _release_slot(booking["slot_id"])
        db_exec("DELETE FROM booking_drafts WHERE user_id=?", (booking["user_id"],))
        try:
            await message.bot.send_message(booking["user_id"],
                f"❌ Твоя запись отменена мастером.\n\n📌 Причина: {reason}",
                reply_markup=user_main_kb())
        except Exception:
            pass
    orig = data.get("cancel_orig", "")
    try:
        await message.bot.edit_message_text(chat_id=data["cancel_chat_id"],
            message_id=data["cancel_msg_id"], text=orig + f"\n\n❌ Отменено. Причина: {reason}")
    except Exception:
        pass
    await state.clear()
    await message.answer("Причина отправлена клиентке.", reply_markup=admin_main_kb())


async def main():
    init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    scheduler.start()
    scheduler.add_job(daily_report, "cron", hour=8, minute=0, args=[bot])
    scheduler.add_job(_check_timers, "interval", seconds=2, args=[bot])
    scheduler.add_job(_check_arrivals, "interval", seconds=30, args=[bot])
    scheduler.add_job(_check_past_bookings, "interval", seconds=60, args=[bot])
    scheduler.add_job(_check_training_reminders, "interval", minutes=5, args=[bot])
    await schedule_existing_reminders(bot)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())