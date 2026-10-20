"""Telegram konkurs boti — HAMMA uchun konkurs yaratish (python-telegram-bot v21+).

Har bir foydalanuvchi o'z konkursini yaratadi va boshqaradi.
Faqat ADMIN_IDS dagilar qo'shimcha: foydalanuvchilar ro'yxati, CSV va xabar yuborish.
"""
import asyncio
import csv
import functools
import html
import io
import json
import logging
import os
import random
import re
import secrets
import sqlite3
import time
from datetime import datetime
from threading import Thread
from urllib.parse import quote
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup as Markup,
    KeyboardButton,
    KeyboardButtonRequestChat,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "")
ADMINS = [int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x]
CHANNELS = [c.strip() for c in os.getenv("CHANNELS", "").split(",") if c.strip()]
ANNOUNCE = os.getenv("ANNOUNCE_CHAT", "").strip()
START_STICKER = os.getenv("START_STICKER", "").strip()
REF_MAX = int(os.getenv("REF_MAX_BONUS", "10"))
TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Tashkent"))
DB_PATH = os.getenv("DB_PATH", "konkurs.db")
# HAR BIR konkurs uchun majburiy kanallar (vergul bilan ajrating)
FORCED = [c.strip() for c in os.getenv("FORCED_CHANNELS", "@Luckasuzb").split(",") if c.strip()]

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("konkurs")
esc = html.escape
HTML = ParseMode.HTML

REACTIONS = ["🔥", "🎉", "🏆", "⚡", "👍", "❤", "🤩", "💯", "👏", "🍾"]
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def is_admin(uid):
    return uid in ADMINS


# ================= DATABASE =================
class DB:
    def __init__(self, path=DB_PATH):
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
            first_seen INTEGER, last_seen INTEGER, ref INTEGER, blocked INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS contests(
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, prize TEXT,
            winners INTEGER, ends_at INTEGER, status TEXT DEFAULT 'active', created_at INTEGER);
        CREATE TABLE IF NOT EXISTS participants(
            contest_id INTEGER, user_id INTEGER, username TEXT, full_name TEXT,
            tickets INTEGER DEFAULT 1, invited_by INTEGER, joined_at INTEGER,
            PRIMARY KEY(contest_id, user_id));
        CREATE TABLE IF NOT EXISTS winners(contest_id INTEGER, user_id INTEGER, place INTEGER);
        """)
        for col in ("channels TEXT", "announce TEXT", "owner_id INTEGER"):
            try:
                self.c.execute(f"ALTER TABLE contests ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        self.c.commit()

    def q1(self, sql, *args):
        return self.c.execute(sql, args).fetchone()[0]

    # ---- users ----
    def touch(self, u):
        now = int(time.time())
        self.c.execute(
            """INSERT INTO users(user_id,username,full_name,first_seen,last_seen)
               VALUES(?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
                 full_name=excluded.full_name, last_seen=excluded.last_seen, blocked=0""",
            (u.id, u.username or "", u.full_name, now, now),
        )
        self.c.commit()

    def users_total(self):
        return self.q1("SELECT COUNT(*) FROM users")

    def users_page(self, offset, limit):
        return self.c.execute(
            "SELECT * FROM users ORDER BY first_seen, user_id LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()

    def user_ids(self):
        return [r[0] for r in self.c.execute("SELECT user_id FROM users WHERE blocked=0")]

    def set_blocked(self, uid):
        self.c.execute("UPDATE users SET blocked=1 WHERE user_id=?", (uid,))
        self.c.commit()

    def stats(self):
        now = int(time.time())
        today0 = int(datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        return dict(
            users=self.users_total(),
            today=self.q1("SELECT COUNT(*) FROM users WHERE first_seen>=?", today0),
            active24=self.q1("SELECT COUNT(*) FROM users WHERE last_seen>=?", now - 86400),
            blocked=self.q1("SELECT COUNT(*) FROM users WHERE blocked=1"),
            contests=self.q1("SELECT COUNT(*) FROM contests"),
            finished=self.q1("SELECT COUNT(*) FROM contests WHERE status='finished'"),
            parts_all=self.q1("SELECT COUNT(*) FROM participants"),
        )

    # ---- contests (egasi bo'yicha) ----
    @staticmethod
    def _own(uid):
        # owner_id bo'sh (eski) konkurslar adminlarga tegishli hisoblanadi
        return "(owner_id=? OR (owner_id IS NULL AND ?=1))", (uid, 1 if is_admin(uid) else 0)

    def active(self, uid):
        w, a = self._own(uid)
        return self.c.execute(
            f"SELECT * FROM contests WHERE status='active' AND {w} ORDER BY id DESC LIMIT 1", a
        ).fetchone()

    def all_active(self):
        return self.c.execute("SELECT * FROM contests WHERE status='active'").fetchall()

    def owner_stats(self, uid):
        w, a = self._own(uid)
        return dict(
            contests=self.q1(f"SELECT COUNT(*) FROM contests WHERE {w}", *a),
            finished=self.q1(f"SELECT COUNT(*) FROM contests WHERE status='finished' AND {w}", *a),
        )

    def get(self, cid):
        return self.c.execute("SELECT * FROM contests WHERE id=?", (cid,)).fetchone()

    def create(self, owner, title, prize, winners, ends_at, channels, announce):
        cur = self.c.execute(
            "INSERT INTO contests(owner_id,title,prize,winners,ends_at,created_at,channels,announce)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (owner, title, prize, winners, ends_at, int(time.time()),
             json.dumps(channels, ensure_ascii=False), announce),
        )
        self.c.commit()
        return cur.lastrowid

    def cancel(self, cid):
        self.c.execute("UPDATE contests SET status='cancelled' WHERE id=?", (cid,))
        self.c.commit()

    def count(self, cid):
        return self.q1("SELECT COUNT(*) FROM participants WHERE contest_id=?", cid)

    def tickets_total(self, cid):
        return self.q1("SELECT COALESCE(SUM(tickets),0) FROM participants WHERE contest_id=?", cid)

    def invites_total(self, cid):
        return self.q1("SELECT COUNT(*) FROM participants WHERE contest_id=? AND invited_by IS NOT NULL", cid)

    def me(self, cid, uid):
        return self.c.execute(
            "SELECT * FROM participants WHERE contest_id=? AND user_id=?", (cid, uid)
        ).fetchone()

    def my_active(self, uid):
        return self.c.execute(
            "SELECT p.*, c.title FROM participants p JOIN contests c ON c.id=p.contest_id "
            "WHERE p.user_id=? AND c.status='active'", (uid,)
        ).fetchall()

    def number(self, cid, uid):
        return self.q1(
            "SELECT COUNT(*) FROM participants WHERE contest_id=? AND rowid<="
            "(SELECT rowid FROM participants WHERE contest_id=? AND user_id=?)",
            cid, cid, uid,
        )

    def join(self, cid, user, ref=None, ref_max=10):
        row = self.me(cid, user.id)
        if row:
            return dict(new=False, tickets=row["tickets"], number=self.number(cid, user.id), bonus_to=None)
        inviter = self.me(cid, ref) if ref and ref != user.id else None
        self.c.execute(
            "INSERT INTO participants(contest_id,user_id,username,full_name,tickets,invited_by,joined_at)"
            " VALUES(?,?,?,?,1,?,?)",
            (cid, user.id, user.username or "", user.full_name,
             inviter["user_id"] if inviter else None, int(time.time())),
        )
        bonus_to = None
        if inviter and inviter["tickets"] < 1 + ref_max:
            self.c.execute(
                "UPDATE participants SET tickets=tickets+1 WHERE contest_id=? AND user_id=?",
                (cid, inviter["user_id"]),
            )
            bonus_to = inviter["user_id"]
        self.c.commit()
        return dict(new=True, tickets=1, number=self.number(cid, user.id), bonus_to=bonus_to)

    def invited_count(self, cid, uid):
        return self.q1("SELECT COUNT(*) FROM participants WHERE contest_id=? AND invited_by=?", cid, uid)

    def parts_page(self, cid, offset, limit):
        return self.c.execute(
            "SELECT * FROM participants WHERE contest_id=? ORDER BY rowid LIMIT ? OFFSET ?",
            (cid, limit, offset),
        ).fetchall()

    def draw(self, cid):
        c = self.get(cid)
        ps = [dict(r) for r in self.c.execute(
            "SELECT * FROM participants WHERE contest_id=? ORDER BY joined_at", (cid,))]
        won = []
        for _ in range(min(c["winners"], len(ps))):
            total = sum(p["tickets"] for p in ps)
            if total <= 0:
                break
            r = secrets.randbelow(total)
            acc = 0
            for i, p in enumerate(ps):
                acc += p["tickets"]
                if r < acc:
                    won.append(ps.pop(i))
                    break
        self.c.executemany(
            "INSERT INTO winners VALUES(?,?,?)", [(cid, w["user_id"], i + 1) for i, w in enumerate(won)]
        )
        self.c.execute("UPDATE contests SET status='finished' WHERE id=?", (cid,))
        self.c.commit()
        return won

    def history(self, uid, limit=5):
        w, a = self._own(uid)
        return self.c.execute(
            f"SELECT * FROM contests WHERE status='finished' AND {w} ORDER BY id DESC LIMIT ?", (*a, limit)
        ).fetchall()

    def winners_of(self, cid):
        return self.c.execute(
            "SELECT w.place, p.user_id, p.username, p.full_name FROM winners w "
            "JOIN participants p ON p.contest_id=w.contest_id AND p.user_id=w.user_id "
            "WHERE w.contest_id=? ORDER BY w.place", (cid,),
        ).fetchall()

    # ---- export ----
    def export_csv(self, cid):
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["raqam", "user_id", "username", "ism", "chiptalar", "taklif_qilgan", "qo'shilgan_vaqt"])
        for n, r in enumerate(self.c.execute(
                "SELECT * FROM participants WHERE contest_id=? ORDER BY rowid", (cid,)), 1):
            w.writerow([n, r["user_id"], r["username"], r["full_name"], r["tickets"], r["invited_by"] or "",
                        datetime.fromtimestamp(r["joined_at"], TZ).strftime("%Y-%m-%d %H:%M:%S")])
        return out.getvalue()

    def export_users_csv(self):
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["user_id", "username", "ism", "birinchi_kirgan", "oxirgi_faollik", "bloklagan"])
        f = lambda t: datetime.fromtimestamp(t, TZ).strftime("%Y-%m-%d %H:%M:%S")
        for r in self.c.execute("SELECT * FROM users ORDER BY first_seen"):
            w.writerow([r["user_id"], r["username"], r["full_name"], f(r["first_seen"]),
                        f(r["last_seen"]), r["blocked"]])
        return out.getvalue()


db = DB()


# ================= HELPERS =================
def short(s, n=28):
    s = s or "Nomsiz"
    return s if len(s) <= n else s[: n - 1] + "…"


def link(uid, name, username=""):
    s = f'<a href="tg://user?id={uid}">{esc(short(name))}</a>'
    return s + (f" @{esc(username)}" if username else "")


def person(w):
    return link(w["user_id"], w["full_name"], w["username"])


def fmt_end(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m.%Y %H:%M") if ts else "yaratuvchi e'lon qilganda"


def card(c):
    return (
        f"🎁 <b>{esc(c['title'])}</b>\n\n"
        f"🏆 Sovrin: <b>{esc(c['prize'])}</b>\n"
        f"👥 G'oliblar soni: <b>{c['winners']}</b>\n"
        f"⏰ Tugash: <b>{fmt_end(c['ends_at'])}</b>\n"
        f"🙋 Ishtirokchilar: <b>{db.count(c['id'])}</b>"
    )


def expired(c):
    return bool(c["ends_at"]) and c["ends_at"] <= time.time()


def contest_link(context, cid, ref=None):
    """Konkursga qo'shilish havolasi: ?start=c<id> yoki ?start=c<id>_<taklif qilgan>"""
    return f"https://t.me/{context.bot.username}?start=c{cid}" + (f"_{ref}" if ref else "")


def chan_url(ch):
    if ch.startswith("http"):
        return ch
    return f"https://t.me/{ch.lstrip('@')}"


def to_chat(x):
    return int(x) if str(x).lstrip("-").isdigit() else x


def forced_channels():
    return [{"id": ch, "title": ch.lstrip("@"), "url": chan_url(ch)} for ch in FORCED]


def contest_channels(c):
    if c["channels"] is None:  # eski konkurslar uchun .env dagi kanallar
        own = [{"id": ch, "title": ch, "url": chan_url(ch)} for ch in CHANNELS]
    else:
        own = json.loads(c["channels"])
    # majburiy (har doim) kanallar birinchi, takrorlanmasin
    forced = forced_channels()
    seen = {str(f["id"]).lower() for f in forced}
    return forced + [x for x in own if str(x["id"]).lower() not in seen]


async def missing_channels(bot, uid, c):
    miss = []
    for ch in contest_channels(c):
        try:
            m = await bot.get_chat_member(to_chat(ch["id"]), uid)
            if m.status in ("left", "kicked"):
                miss.append(ch)
        except Exception as e:
            log.warning("Obunani tekshirib bo'lmadi (%s): %s — bot kanalda admin bo'lishi kerak", ch["id"], e)
            miss.append(ch)
    return miss


def norm_ref(t):
    t = t.strip()
    if "t.me/" in t:
        t = "@" + t.split("t.me/")[1].strip("/").split("/")[0]
    return t if t.startswith("@") or t.lstrip("-").isdigit() else "@" + t


async def resolve_channel(bot, ref):
    chat = await bot.get_chat(to_chat(ref))
    if chat.type != "channel":
        raise ValueError("Bu kanal emas")
    m = await bot.get_chat_member(chat.id, bot.id)
    if m.status not in ("administrator", "creator"):
        raise ValueError("Bot bu kanalda admin emas")
    url = f"https://t.me/{chat.username}" if chat.username else await bot.export_chat_invite_link(chat.id)
    return {"id": chat.id, "title": chat.title or str(chat.id), "url": url}


async def react(msg, emoji=None):
    try:
        await msg.set_reaction(emoji or random.choice(REACTIONS))
    except Exception:
        pass


async def edit(q, text, kb=None):
    try:
        await q.edit_message_text(text, parse_mode=HTML, reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("edit xato: %s", e)


def admin_only(fn):
    @functools.wraps(fn)
    async def wrapper(update, context):
        if not is_admin(update.effective_user.id):
            if update.callback_query:
                await update.callback_query.answer("⛔ Faqat adminlar uchun", show_alert=True)
            return
        return await fn(update, context)
    return wrapper


# ================= USER SIDE (konkursga qo'shilish) =================
async def join_reply(user, context, cid, ref=None):
    """Foydalanuvchini tekshiradi va qo'shadi. (kind, text, markup) qaytaradi."""
    c = db.get(cid)
    if not c or c["status"] != "active" or expired(c):
        return "none", "Bu konkurs tugagan yoki mavjud emas. Yangiliklarni kuting! 🔔", None

    miss = await missing_channels(context.bot, user.id, c)
    if miss:
        rows = [[Btn(f"📢 {ch['title']}", url=ch["url"])] for ch in miss]
        rows.append([Btn("✅ Tekshirish", callback_data=f"check:{cid}:{ref or 0}")])
        return "sub", (
            "❌ Konkursga qatnashishdan oldin quyidagi kanallarga obuna bo'ling 👇\n"
            "Obuna bo'lgach, «Tekshirish» tugmasini bosing."
        ), Markup(rows)

    r = db.join(cid, user, ref, REF_MAX)
    if r["bonus_to"]:
        try:
            await context.bot.send_message(
                r["bonus_to"], "🎟🎉 Do'stingiz konkursga qo'shildi — sizga +1 chipta berildi!")
        except Exception:
            pass

    head = (f"✅ Siz {r['number']}-bo'lib «{esc(c['title'])}» konkursiga qatnashdingiz. Omad! 🍀" if r["new"]
            else f"ℹ️ Siz allaqachon qatnashgansiz ({r['number']}-ishtirokchi).")
    mylink = contest_link(context, cid, user.id)
    text = (
        f"{head}\n\n"
        f"👤 Ishtirokchi: <b>{r['number']}</b>\n"
        f"🏆 G'oliblar soni: <b>{c['winners']}</b>\n\n"
        f"🎟 Chiptalaringiz: <b>{r['tickets']}</b>\n"
        f"👫 Har bir taklif qilingan do'st uchun <b>+1 chipta</b> (g'olib bo'lish imkoniyati oshadi).\n"
        f"Sizning havolangiz:\n{mylink}"
    )
    share = f"https://t.me/share/url?url={quote(mylink)}&text={quote('🎁 Konkursda qatnash va yut!')}"
    kb = Markup([
        [Btn("👫 Do'stlarni taklif qilish", url=share)],
        [Btn("🎟 Mening chiptalarim", callback_data=f"me:{cid}")],
    ])
    return "ok", text, kb


JOIN_RE = re.compile(r"^c(\d+)(?:_(\d+))?$")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    db.touch(u)
    await react(update.message)
    m = JOIN_RE.match(context.args[0]) if context.args else None
    if m:  # konkurs havolasi orqali kirdi
        cid = int(m.group(1))
        ref = int(m.group(2)) if m.group(2) else None
        kind, text, kb = await join_reply(u, context, cid, ref)
        return await update.message.reply_text(text, parse_mode=HTML, reply_markup=kb)
    # Havolasiz /start — HAMMA uchun konkurs paneli
    await update.message.reply_text(panel_text(u.id), parse_mode=HTML, reply_markup=panel_kb(u.id))


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    db.touch(q.from_user)
    _, cid, ref = q.data.split(":")
    kind, text, kb = await join_reply(q.from_user, context, int(cid), int(ref) or None)
    if kind == "sub":
        await edit(q, text, kb)
        return await q.answer("❌ Siz hali barcha kanallarga obuna bo'lmadingiz!", show_alert=True)
    await q.answer("🎉 Zo'r! Qabul qilindi!" if kind == "ok" else None)
    await edit(q, text, kb)


async def me_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    cid = int(q.data.split(":")[1])
    me = db.me(cid, q.from_user.id)
    if not me:
        return await q.answer("Siz hali qatnashmayapsiz.", show_alert=True)
    await q.answer(
        f"🎟 Chiptalaringiz: {me['tickets']}\n👫 Taklif qilganlar: {db.invited_count(cid, me['user_id'])}",
        show_alert=True,
    )


async def mening(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await react(update.message, "👀")
    rows = db.my_active(update.effective_user.id)
    if not rows:
        return await update.message.reply_text("Siz hali hech qaysi konkursda qatnashmayapsiz.")
    parts = [
        f"🎁 <b>{esc(r['title'])}</b>\n"
        f"🎟 Chiptalaringiz: <b>{r['tickets']}</b>\n"
        f"👫 Taklif qilganlar: <b>{db.invited_count(r['contest_id'], r['user_id'])}</b>\n"
        f"Havolangiz: {contest_link(context, r['contest_id'], r['user_id'])}"
        for r in rows
    ]
    await update.message.reply_text("\n\n".join(parts), parse_mode=HTML)


async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await react(update.message, "🤝")
    await update.message.reply_text("Bosh menyu uchun /start ni bosing 🎁")


# ================= KONKURS PANELI (hamma uchun) =================
BACK = Markup([[Btn("🔙 Panel", callback_data="a:home")]])


def panel_text(uid):
    c = db.active(uid)
    if is_admin(uid):
        s = db.stats()
        txt = ("👑 <b>ADMIN PANEL</b>\n\n"
               f"👥 Foydalanuvchilar: <b>{s['users']}</b>  (bugun: +{s['today']})\n\n")
    else:
        txt = "🎁 <b>KONKURS PANELI</b>\n\nO'z konkursingizni yarating va boshqaring! 🚀\n\n"
    if c:
        txt += (
            f"🎁 Faol konkursingiz: <b>{esc(c['title'])}</b>\n"
            f"🙋 Ishtirokchilar: <b>{db.count(c['id'])}</b>\n"
            f"🏆 G'oliblar soni: <b>{c['winners']}</b>\n"
            f"⏰ Tugash: <b>{fmt_end(c['ends_at'])}</b>"
        )
    else:
        txt += "🎁 Faol konkursingiz: <b>yo'q</b>"
    return txt


def panel_kb(uid):
    rows = [
        [Btn("📊 Statistika", callback_data="a:stats"), Btn("🎁 Yangi konkurs", callback_data="a:new")],
        [Btn("🙋 Ishtirokchilar", callback_data="a:parts:0"), Btn("🏁 G'olib aniqlash", callback_data="a:draw")],
        [Btn("🚫 Bekor qilish", callback_data="a:cancel"), Btn("📥 Ishtirokchilar CSV", callback_data="a:csv")],
        [Btn("🏆 G'oliblar tarixi", callback_data="a:hist"), Btn("📢 Kanallar", callback_data="a:chan")],
        [Btn("🔗 Konkurs havolasi", callback_data="a:link")],
    ]
    if is_admin(uid):
        rows += [
            [Btn("👥 Foydalanuvchilar", callback_data="a:users:0"),
             Btn("📥 Foydalanuvchilar CSV", callback_data="a:ucsv")],
            [Btn("📣 Xabar yuborish", callback_data="a:bc")],
        ]
    return Markup(rows)


async def admin_cmd(update, context):
    uid = update.effective_user.id
    db.touch(update.effective_user)
    await react(update.message, "🎁")
    await update.message.reply_text(panel_text(uid), parse_mode=HTML, reply_markup=panel_kb(uid))


def pager(prefix, page, pages):
    nav = []
    if page > 0:
        nav.append(Btn("◀️", callback_data=f"{prefix}:{page - 1}"))
    nav.append(Btn(f"{page + 1}/{pages}", callback_data="a:noop"))
    if page < pages - 1:
        nav.append(Btn("▶️", callback_data=f"{prefix}:{page + 1}"))
    return Markup([nav, [Btn("🔙 Panel", callback_data="a:home")]])


async def show_users(q, page):
    per, total = 15, db.users_total()
    pages = max(1, (total + per - 1) // per)
    page = max(0, min(page, pages - 1))
    rows = db.users_page(page * per, per)
    lines = [
        f"{page * per + i}. {link(r['user_id'], r['full_name'], r['username'])} — <code>{r['user_id']}</code>"
        + (" 🚫" if r["blocked"] else "")
        for i, r in enumerate(rows, 1)
    ]
    body = "\n".join(lines) or "Hozircha hech kim yo'q."
    await edit(q, f"👥 <b>Foydalanuvchilar: {total}</b>\n\n{body}", pager("a:users", page, pages))


async def show_parts(q, page):
    c = db.active(q.from_user.id)
    if not c:
        return await q.answer("Faol konkurs yo'q.", show_alert=True)
    per, total = 15, db.count(c["id"])
    pages = max(1, (total + per - 1) // per)
    page = max(0, min(page, pages - 1))
    rows = db.parts_page(c["id"], page * per, per)
    lines = [
        f"#{page * per + i} {link(r['user_id'], r['full_name'], r['username'])} — "
        f"<code>{r['user_id']}</code> 🎟{r['tickets']}"
        for i, r in enumerate(rows, 1)
    ]
    body = "\n".join(lines) or "Hozircha ishtirokchi yo'q."
    await edit(q, f"🙋 <b>{esc(c['title'])}</b>\nIshtirokchilar: <b>{total}</b>\n\n{body}",
               pager("a:parts", page, pages))


async def show_stats(q):
    uid = q.from_user.id
    o, c = db.owner_stats(uid), db.active(uid)
    txt = "📊 <b>STATISTIKA</b>\n\n"
    if is_admin(uid):
        s = db.stats()
        txt += (
            f"👥 Jami foydalanuvchi: <b>{s['users']}</b>\n"
            f"🆕 Bugun qo'shilgan: <b>{s['today']}</b>\n"
            f"⚡ Oxirgi 24 soatda faol: <b>{s['active24']}</b>\n"
            f"🚫 Botni bloklagan: <b>{s['blocked']}</b>\n"
            f"🎁 Botdagi barcha konkurslar: <b>{s['contests']}</b> (yakunlangan: {s['finished']})\n"
            f"🙋 Barcha qatnashishlar: <b>{s['parts_all']}</b>\n\n"
        )
    txt += f"🎁 Sizning konkurslaringiz: <b>{o['contests']}</b> (yakunlangan: {o['finished']})\n"
    if c:
        txt += (
            f"\n🔥 <b>Faol konkurs: {esc(c['title'])}</b>\n"
            f"🙋 Ishtirokchilar: <b>{db.count(c['id'])}</b>\n"
            f"🎟 Jami chiptalar: <b>{db.tickets_total(c['id'])}</b>\n"
            f"👫 Taklif orqali kelganlar: <b>{db.invites_total(c['id'])}</b>"
        )
    await edit(q, txt, BACK)


async def show_channels(q, context):
    c = db.active(q.from_user.id)
    chs = contest_channels(c) if c else []
    if not chs:
        return await edit(q, "📢 Majburiy kanal yo'q.", BACK)
    lines = []
    for ch in chs:
        try:
            m = await context.bot.get_chat_member(to_chat(ch["id"]), context.bot.id)
            cnt = await context.bot.get_chat_member_count(to_chat(ch["id"]))
            ok = "✅ bot admin" if m.status in ("administrator", "creator") else "⚠️ bot admin EMAS"
            lines.append(f"📢 <b>{esc(ch['title'])}</b>\n    {ok} · 👥 {cnt}")
        except Exception:
            lines.append(f"📢 <b>{esc(ch['title'])}</b>\n    ❌ Kanalga ulanib bo'lmadi")
    await edit(q, "📢 <b>Majburiy kanallar</b>\n\n" + "\n\n".join(lines), BACK)


async def show_history(q):
    hs = db.history(q.from_user.id, 5)
    if not hs:
        return await edit(q, "🏆 Hali yakunlangan konkurs yo'q.", BACK)
    parts = []
    for c in hs:
        ws = db.winners_of(c["id"])
        lines = "\n".join(f"{MEDALS.get(w['place'], str(w['place']) + '.')} {person(w)}" for w in ws) or "—"
        parts.append(f"🎁 <b>{esc(c['title'])}</b> — {esc(c['prize'])}\n{lines}")
    await edit(q, "🏆 <b>G'oliblar tarixi</b>\n\n" + "\n\n".join(parts), BACK)


async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    p = q.data.split(":")
    act = p[1]

    if act == "noop":
        return await q.answer()
    if act == "home":
        await q.answer()
        return await edit(q, panel_text(uid), panel_kb(uid))
    if act == "stats":
        await q.answer("📊")
        return await show_stats(q)
    if act == "parts":
        await q.answer()
        return await show_parts(q, int(p[2]))
    if act == "chan":
        await q.answer("📢")
        return await show_channels(q, context)
    if act == "hist":
        await q.answer("🏆")
        return await show_history(q)

    # --- faqat adminlar uchun ---
    if act in ("users", "ucsv"):
        if not is_admin(uid):
            return await q.answer("⛔ Faqat adminlar uchun", show_alert=True)
        if act == "users":
            await q.answer()
            return await show_users(q, int(p[2]))
        await q.answer("📥 Tayyorlanmoqda...")
        data = db.export_users_csv().encode("utf-8-sig")
        return await q.message.reply_document(io.BytesIO(data), filename="foydalanuvchilar.csv")

    c = db.active(uid)
    if act == "link":
        if not c:
            return await q.answer("Faol konkurs yo'q.", show_alert=True)
        await q.answer()
        lk = contest_link(context, c["id"])
        share = f"https://t.me/share/url?url={quote(lk)}&text={quote('🎁 Konkursda qatnash va yut!')}"
        return await edit(
            q, f"🔗 <b>{esc(c['title'])}</b> konkursi havolasi:\n\n{lk}\n\nShu havolani tarqating 👇",
            Markup([[Btn("📤 Ulashish", url=share)], [Btn("🔙 Panel", callback_data="a:home")]]),
        )
    if act == "csv":
        if not c:
            return await q.answer("Faol konkurs yo'q.", show_alert=True)
        await q.answer("📥 Tayyorlanmoqda...")
        data = db.export_csv(c["id"]).encode("utf-8-sig")
        return await q.message.reply_document(io.BytesIO(data), filename=f"ishtirokchilar_{c['id']}.csv")
    if act == "draw":
        if not c:
            return await q.answer("Faol konkurs yo'q.", show_alert=True)
        await q.answer()
        return await edit(
            q, f"🏁 <b>{esc(c['title'])}</b> konkursini hozir yakunlab, g'oliblarni aniqlaymizmi?\n"
               f"🙋 Ishtirokchilar: <b>{db.count(c['id'])}</b>",
            Markup([[Btn("✅ Ha, aniqlansin", callback_data="a:draw_y"), Btn("❌ Yo'q", callback_data="a:home")]]),
        )
    if act == "draw_y":
        if not c:
            return await q.answer("Faol konkurs yo'q.", show_alert=True)
        await q.answer("🏁 G'oliblar aniqlanmoqda...")
        await finish(context.application, c["id"])
        return await edit(q, "🏁 Konkurs yakunlandi! 🎉\n\n" + panel_text(uid), panel_kb(uid))
    if act == "cancel":
        if not c:
            return await q.answer("Faol konkurs yo'q.", show_alert=True)
        await q.answer()
        return await edit(
            q, f"🚫 <b>{esc(c['title'])}</b> konkursi bekor qilinsinmi?",
            Markup([[Btn("✅ Ha, bekor qilinsin", callback_data="a:cancel_y"), Btn("❌ Yo'q", callback_data="a:home")]]),
        )
    if act == "cancel_y":
        if not c:
            return await q.answer("Faol konkurs yo'q.", show_alert=True)
        db.cancel(c["id"])
        unschedule(context.application, c["id"])
        await q.answer("🚫 Bekor qilindi")
        return await edit(q, "🚫 Konkurs bekor qilindi.\n\n" + panel_text(uid), panel_kb(uid))
    await q.answer()


# ================= YANGI KONKURS (conversation) =================
TITLE, PRIZE, WINNERS, ENDS, CHANS, ANN = range(6)
BC_MSG, BC_OK = range(10, 12)


def add_kb(label, req_id):
    return ReplyKeyboardMarkup(
        [[KeyboardButton(label, request_chat=KeyboardButtonRequestChat(
            request_id=req_id, chat_is_channel=True))],
         ["/next"]],
        resize_keyboard=True,
    )


async def conv_home(update, context):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    await edit(q, panel_text(uid), panel_kb(uid))
    return ConversationHandler.END


async def new_start(update, context):
    q = update.callback_query
    if q:
        await q.answer()
    msg = q.message if q else update.message
    uid = update.effective_user.id
    db.touch(update.effective_user)
    if db.active(uid):
        await msg.reply_text("Avval joriy konkursingizni yakunlang (/natija) yoki bekor qiling (/bekor).")
        return ConversationHandler.END
    context.user_data.clear()
    await msg.reply_text("🎁✨ <b>Yangi konkurs!</b>\n\n📝 Konkurs nomi? (to'xtatish: /chiqish)", parse_mode=HTML)
    return TITLE


async def y_title(update, context):
    context.user_data["t"] = update.message.text.strip()
    await update.message.reply_text("🏆 Sovrin nima? 💎")
    return PRIZE


async def y_prize(update, context):
    context.user_data["p"] = update.message.text.strip()
    await update.message.reply_text("👥 Nechta g'olib bo'ladi? (1–100) 🥇")
    return WINNERS


async def y_winners(update, context):
    t = update.message.text.strip()
    if not (t.isdigit() and 1 <= int(t) <= 100):
        await update.message.reply_text("⚠️ 1 dan 100 gacha son yozing.")
        return WINNERS
    context.user_data["w"] = int(t)
    await update.message.reply_text(
        "⏰ Tugash vaqti? Format: 2026-11-01 20:00 (Toshkent vaqti).\nQo'lda yakunlash uchun «-» yozing."
    )
    return ENDS


async def y_ends(update, context):
    t, ends = update.message.text.strip(), None
    if t != "-":
        try:
            ends = int(datetime.strptime(t, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp())
        except ValueError:
            await update.message.reply_text("⚠️ Format noto'g'ri. Misol: 2026-11-01 20:00 yoki «-»")
            return ENDS
        if ends <= time.time():
            await update.message.reply_text("⚠️ Bu vaqt o'tib ketgan. Kelajakdagi vaqtni yozing.")
            return ENDS
    context.user_data["e"] = ends
    context.user_data["chs"] = []
    await update.message.reply_text(
        "📢 <b>Majburiy obuna kanallari</b>\n\n"
        "Pastdagi <b>➕ Qo'shish</b> tugmasini bosib kanalni tanlang "
        "(yoki @username yozing).\n"
        "⚠️ Bot kanalda <b>admin</b> bo'lishi shart.\n\n"
        "Bir nechta qo'shish mumkin 🔁\n"
        "Tugatish yoki o'tkazib yuborish: /next ⏭",
        parse_mode=HTML, reply_markup=add_kb("➕ Qo'shish", 1),
    )
    return CHANS


async def chans_add(update, context):
    m = update.message
    ref = m.chat_shared.chat_id if m.chat_shared else norm_ref(m.text)
    try:
        ch = await resolve_channel(context.bot, ref)
    except Exception as e:
        await m.reply_text(f"❌ Qo'shib bo'lmadi: {e}\nBotni kanalga admin qilib, qayta urinib ko'ring.")
        return CHANS
    chs = context.user_data.setdefault("chs", [])
    if any(x["id"] == ch["id"] for x in chs):
        await m.reply_text("ℹ️ Bu kanal allaqachon qo'shilgan.")
        return CHANS
    chs.append(ch)
    await m.reply_text(
        f"✅ <b>{esc(ch['title'])}</b> qo'shildi! 🎉 (jami: {len(chs)})\n"
        "Yana qo'shing yoki /next ⏭", parse_mode=HTML)
    return CHANS


async def chans_next(update, context):
    n = len(context.user_data.get("chs", []))
    await update.message.reply_text(
        f"✅ Majburiy kanallar: <b>{n}</b>\n\n"
        "📣 <b>Konkurs qaysi kanalda e'lon qilinsin?</b>\n"
        "Pastdagi <b>➕ Kanal</b> tugmasini bosing 👇\n"
        "O'tkazib yuborish: /next ⏭",
        parse_mode=HTML, reply_markup=add_kb("➕ Kanal", 2),
    )
    return ANN


async def ann_add(update, context):
    m = update.message
    ref = m.chat_shared.chat_id if m.chat_shared else norm_ref(m.text)
    try:
        ch = await resolve_channel(context.bot, ref)
    except Exception as e:
        await m.reply_text(f"❌ Bo'lmadi: {e}\nBotni kanalga admin qilib, qayta urinib ko'ring.")
        return ANN
    context.user_data["ann"] = ch["id"]
    await m.reply_text(f"📣 E'lon kanali: <b>{esc(ch['title'])}</b> ✅", parse_mode=HTML)
    return await create_contest(update, context)


async def ann_next(update, context):
    return await create_contest(update, context)


async def create_contest(update, context):
    d, m = context.user_data, update.message
    uid = update.effective_user.id
    # .env dagi standart e'lon kanali faqat adminlarga qo'llanadi
    ann = d.get("ann") or (ANNOUNCE if is_admin(uid) else None) or None
    cid = db.create(uid, d["t"], d["p"], d["w"], d.get("e"), d.get("chs", []), str(ann) if ann else None)
    c = db.get(cid)
    schedule(context.application, c)
    await m.reply_text("🚀🎉 Konkurs ishga tushmoqda...", reply_markup=ReplyKeyboardRemove())
    try:
        if START_STICKER:
            await m.reply_sticker(START_STICKER)
        else:
            await m.reply_dice("🎯")
    except Exception:
        pass
    await react(m, "🎉")
    chs = contest_channels(c)
    ch_txt = "\n".join(f"   📢 {esc(x['title'])}" for x in chs) or "   — yo'q"
    lk = contest_link(context, cid)
    await m.reply_text(
        "✅ <b>Konkurs ishga tushdi!</b> 🚀🔥\n\n" + card(c) +
        f"\n\n🔒 Majburiy kanallar:\n{ch_txt}"
        f"\n\n🔗 Konkurs havolasi (tarqating):\n{lk}",
        parse_mode=HTML, reply_markup=BACK,
    )
    if ann:
        try:
            await context.bot.send_message(
                to_chat(ann), "🎁🔥 <b>YANGI KONKURS!</b> 🔥🎁\n\n" + card(c), parse_mode=HTML,
                reply_markup=Markup([[Btn("🎉 Qatnashish", url=lk)]]),
            )
            if START_STICKER:
                await context.bot.send_sticker(to_chat(ann), START_STICKER)
        except Exception as e:
            log.warning("Kanalga e'lon yuborilmadi: %s", e)
            await m.reply_text(f"⚠️ E'lon kanalga yuborilmadi: {e}")
    d.clear()
    return ConversationHandler.END


async def chiqish(update, context):
    uid = update.effective_user.id
    context.user_data.clear()
    await update.message.reply_text("🚫 Bekor qilindi.", reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text(panel_text(uid), parse_mode=HTML, reply_markup=panel_kb(uid))
    return ConversationHandler.END


# ================= XABAR YUBORISH (broadcast, faqat adminlar) =================
async def bc_start(update, context):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return ConversationHandler.END
    await q.message.reply_text(
        f"📣 Barcha foydalanuvchilarga ({len(db.user_ids())} ta) yuboriladigan xabarni yuboring "
        "(matn, rasm, video — istalgani).\nTo'xtatish: /chiqish"
    )
    return BC_MSG


async def bc_get(update, context):
    m = update.message
    context.user_data["bc"] = (m.chat_id, m.message_id)
    await m.reply_text(
        "👆 Mana shu xabar hammaga yuboriladi. Tasdiqlaysizmi?",
        reply_markup=Markup([[Btn("✅ Yuborish", callback_data="bc:yes"), Btn("❌ Bekor", callback_data="bc:no")]]),
    )
    return BC_OK


async def bc_confirm(update, context):
    q = update.callback_query
    if q.data == "bc:no" or "bc" not in context.user_data:
        await q.answer("Bekor qilindi")
        await edit(q, "❌ Xabar yuborish bekor qilindi.", BACK)
        return ConversationHandler.END
    from_chat, mid = context.user_data.pop("bc")
    await q.answer("🚀 Yuborilmoqda...")
    await edit(q, "🚀 Xabar yuborilmoqda... Tugagach hisobot beraman.")
    context.application.create_task(do_broadcast(context.application, q.message.chat_id, from_chat, mid))
    return ConversationHandler.END


async def do_broadcast(app, admin_chat, from_chat, mid):
    ok = fail = 0
    for uid in db.user_ids():
        for attempt in range(2):
            try:
                await app.bot.copy_message(uid, from_chat, mid)
                ok += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Forbidden:
                db.set_blocked(uid)
                fail += 1
                break
            except Exception:
                fail += 1
                break
        await asyncio.sleep(0.05)
    try:
        await app.bot.send_message(
            admin_chat, f"📣 <b>Yuborish tugadi!</b>\n✅ Yetkazildi: <b>{ok}</b>\n❌ Yetkazilmadi: <b>{fail}</b>",
            parse_mode=HTML, reply_markup=BACK,
        )
    except Exception:
        pass


# ================= YAKUNLASH =================
def unschedule(app, cid):
    for j in app.job_queue.get_jobs_by_name(f"draw_{cid}"):
        j.schedule_removal()


def schedule(app, c):
    """Har bir konkurs uchun alohida avtomatik yakunlash vazifasi."""
    if not c:
        return
    unschedule(app, c["id"])
    if c["ends_at"]:
        app.job_queue.run_once(auto_draw, max(1, c["ends_at"] - time.time()), name=f"draw_{c['id']}", data=c["id"])


async def auto_draw(context: ContextTypes.DEFAULT_TYPE):
    await finish(context.application, context.job.data)


async def finish(app, cid):
    c = db.get(cid)
    if not c or c["status"] != "active":
        return None
    won = db.draw(cid)
    unschedule(app, cid)
    if not won:
        text = f"🏁 <b>{esc(c['title'])}</b> yakunlandi, lekin ishtirokchi bo'lmadi."
    else:
        lines = "\n".join(f"{MEDALS.get(i, f'{i}.')} {person(w)}" for i, w in enumerate(won, 1))
        text = (f"🏁 <b>{esc(c['title'])}</b> yakunlandi! 🎊\n"
                f"🏆 Sovrin: <b>{esc(c['prize'])}</b>\n\n🥳 G'oliblar:\n{lines}")
    owner = c["owner_id"]
    ann = c["announce"] or (ANNOUNCE if (owner is None or is_admin(owner)) else "")
    targets = [owner] if owner else list(ADMINS)
    for chat in dict.fromkeys(targets + ([ann] if ann else [])):
        try:
            await app.bot.send_message(to_chat(chat), text, parse_mode=HTML)
            if START_STICKER and won:
                await app.bot.send_sticker(to_chat(chat), START_STICKER)
        except Exception as e:
            log.warning("E'lon yuborilmadi (%s): %s", chat, e)
    for w in won:
        try:
            await app.bot.send_message(
                w["user_id"], f"🎉🥳 Tabriklaymiz! Siz «{c['title']}» konkursida g'olib bo'ldingiz! 🏆")
        except Exception:
            pass
    return text


# ================= TEZKOR BUYRUQLAR (hamma o'z konkursi uchun) =================
async def natija(update, context):
    c = db.active(update.effective_user.id)
    if not c:
        return await update.message.reply_text("Faol konkursingiz yo'q.")
    await react(update.message, "🏆")
    await finish(context.application, c["id"])


async def bekor(update, context):
    c = db.active(update.effective_user.id)
    if not c:
        return await update.message.reply_text("Faol konkursingiz yo'q.")
    db.cancel(c["id"])
    unschedule(context.application, c["id"])
    await update.message.reply_text("🚫 Konkurs bekor qilindi.")


async def holat(update, context):
    c = db.active(update.effective_user.id)
    if not c:
        return await update.message.reply_text("Faol konkursingiz yo'q. /yangi bilan boshlang.")
    data = db.export_csv(c["id"]).encode("utf-8-sig")
    await update.message.reply_text(card(c), parse_mode=HTML)
    await update.message.reply_document(io.BytesIO(data), filename=f"ishtirokchilar_{c['id']}.csv")


# ================= ISHGA TUSHIRISH =================
USER_COMMANDS = [
    BotCommand("start", "🎁 Bosh menyu / konkurs yaratish"),
    BotCommand("yangi", "🎁 Yangi konkurs yaratish"),
    BotCommand("natija", "🏁 G'olib aniqlash"),
    BotCommand("bekor", "🚫 Konkursni bekor qilish"),
    BotCommand("holat", "📊 Holat + CSV"),
    BotCommand("mening", "🎟 Mening chiptalarim"),
]


async def post_init(app):
    for c in db.all_active():
        schedule(app, c)
    try:
        await app.bot.set_my_commands(USER_COMMANDS)
    except Exception as e:
        log.warning("Buyruqlar o'rnatilmadi: %s", e)
    for a in ADMINS:  # eski admin-scope buyruqlarini yangilash
        try:
            await app.bot.set_my_commands(
                USER_COMMANDS + [BotCommand("admin", "👑 Admin panel")], scope=BotCommandScopeChat(a))
        except Exception:
            pass


async def on_error(update, context):
    log.error("Xatolik: %s", context.error)


def keep_alive():
    """Render (Web Service) uchun: PORT berilgan bo'lsagina kichik veb-server ishga tushadi."""
    port = os.getenv("PORT")
    if not port:
        return
    from flask import Flask
    web = Flask("keepalive")

    @web.route("/")
    def home():
        return "Bot ishlayapti"

    Thread(target=lambda: web.run(host="0.0.0.0", port=int(port)), daemon=True).start()


def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN topilmadi — .env faylini tekshiring.")
    if not ADMINS:
        log.warning("ADMIN_IDS bo'sh — foydalanuvchilar ro'yxati/xabar yuborish hech kimga ochilmaydi!")

    keep_alive()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    text_only = filters.TEXT & ~filters.COMMAND
    exit_fb = [CommandHandler("chiqish", chiqish), CallbackQueryHandler(conv_home, pattern="^a:home$")]

    new_conv = ConversationHandler(
        entry_points=[CommandHandler("yangi", new_start), CallbackQueryHandler(new_start, pattern="^a:new$")],
        states={
            TITLE: [MessageHandler(text_only, y_title)],
            PRIZE: [MessageHandler(text_only, y_prize)],
            WINNERS: [MessageHandler(text_only, y_winners)],
            ENDS: [MessageHandler(text_only, y_ends)],
            CHANS: [
                CommandHandler("next", chans_next),
                MessageHandler(filters.StatusUpdate.CHAT_SHARED | text_only, chans_add),
            ],
            ANN: [
                CommandHandler("next", ann_next),
                MessageHandler(filters.StatusUpdate.CHAT_SHARED | text_only, ann_add),
            ],
        },
        fallbacks=exit_fb,
    )
    bc_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(bc_start, pattern="^a:bc$")],
        states={
            BC_MSG: [MessageHandler(~filters.COMMAND, bc_get)],
            BC_OK: [CallbackQueryHandler(bc_confirm, pattern="^bc:(yes|no)$")],
        },
        fallbacks=exit_fb,
    )

    app.add_handler(new_conv)
    app.add_handler(bc_conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("mening", mening))
    app.add_handler(CommandHandler("natija", natija))
    app.add_handler(CommandHandler("bekor", bekor))
    app.add_handler(CommandHandler("holat", holat))
    app.add_handler(CallbackQueryHandler(check, pattern="^check:"))
    app.add_handler(CallbackQueryHandler(me_cb, pattern="^me:"))
    app.add_handler(CallbackQueryHandler(admin_cb, pattern="^a:"))
    app.add_handler(MessageHandler(text_only, fallback))
    app.add_error_handler(on_error)

    log.info("Bot ishga tushdi")
    app.run_polling()


if __name__ == "__main__":
    main()
