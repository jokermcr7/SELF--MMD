import asyncio
import base64
import logging
import os
import random
from datetime import datetime, timezone, timedelta
from html import escape
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, RPCError, PhoneCodeInvalidError, PhoneCodeExpiredError, FloodWaitError
from telethon.tl.functions.account import UpdateProfileRequest, UpdateUsernameRequest

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import ReactionTypeEmoji
from sqlalchemy import Boolean, DateTime, Integer, String, Text, select, func, inspect, text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("self-panel")

TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DEFAULT_TZ = os.getenv("DEFAULT_TIMEZONE", "Europe/Berlin")
TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()
SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip()
if SESSION_SECRET:
    try:
        FERNET = Fernet(SESSION_SECRET.encode())
    except Exception as e:
        raise RuntimeError("SESSION_SECRET باید یک کلید Fernet معتبر باشد") from e
else:
    FERNET = None

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+asyncpg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
if not DATABASE_URL or "://" not in DATABASE_URL:
    DATABASE_URL = "sqlite+aiosqlite:///./bot.db"

class Base(DeclarativeBase): pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str | None] = mapped_column(String(255))
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    banned: Mapped[bool] = mapped_column(Boolean, default=False)
    timezone: Mapped[str] = mapped_column(String(64), default=DEFAULT_TZ)
    auto_reaction: Mapped[str | None] = mapped_column(String(32))
    animation_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    animation_style: Mapped[str] = mapped_column(String(32), default="نرم")
    auto_reply: Mapped[str | None] = mapped_column(Text)
    notifications: Mapped[bool] = mapped_column(Boolean, default=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    self_session: Mapped[str | None] = mapped_column(Text)
    phone_masked: Mapped[str | None] = mapped_column(String(64))
    pending_phone: Mapped[str | None] = mapped_column(String(64))
    access_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    self_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    self_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    clock_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    clock_target: Mapped[str] = mapped_column(String(16), default="bio")
    clock_font: Mapped[str] = mapped_column(String(32), default="classic")
    clock_prefix: Mapped[str] = mapped_column(String(40), default="🕐 ")
    smart_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    name_lock: Mapped[bool] = mapped_column(Boolean, default=False)
    locked_name: Mapped[str | None] = mapped_column(String(255))
    word_filter: Mapped[bool] = mapped_column(Boolean, default=False)
    word_filter_text: Mapped[str | None] = mapped_column(Text)
    media_lock: Mapped[bool] = mapped_column(Boolean, default=False)
    comments_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    button_theme: Mapped[str] = mapped_column(String(32), default="orange")
    diamonds: Mapped[int] = mapped_column(Integer, default=0)
    welcome_diamond_granted: Mapped[bool] = mapped_column(Boolean, default=False)
    diamond_billing_remainder: Mapped[float] = mapped_column(default=0.0)
    diamond_last_billed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class DiamondTransaction(Base):
    __tablename__ = "diamond_transactions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    actor_id: Mapped[int] = mapped_column(Integer, index=True)
    amount: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32), default="adjust")
    note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
dp = Dispatcher()
LOGIN_FLOWS: dict[int, dict] = {}
LOGIN_CANCEL_TEXT = "❌ لغو ورود"
CLOCK_TASKS: dict[int, asyncio.Task] = {}
BILLING_TASKS: dict[int, asyncio.Task] = {}

# فونت‌های ساعت؛ همگی فقط تبدیل ظاهری اعداد هستند و به متن اصلی دست نمی‌زنند.
CLOCK_FONTS = {
    "classic": ("کلاسیک", "0123456789", "0123456789"),
    "bold": ("ضخیم", "0123456789", "𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗"),
    "double": ("دوبل", "0123456789", "𝟘𝟙𝟚𝟛𝟜𝟝𝟞𝟟𝟠𝟡"),
    "sans": ("سن‌سریف", "0123456789", "𝟢𝟣𝟤𝟥𝟦𝟧𝟨𝟩𝟪𝟫"),
    "mono": ("تک‌عرض", "0123456789", "𝟶𝟷𝟸𝟹𝟺𝟻𝟼𝟽𝟾𝟿"),
    "full": ("فول‌ویدث", "0123456789", "０１２３４５６７８９"),
    "bubble": ("حبابی", "0123456789", "⓪①②③④⑤⑥⑦⑧⑨"),
    "superscript": ("بالانویس", "0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹"),
    "subscript": ("زیرنویس", "0123456789", "₀₁₂₃₄₅₆₇₈₉"),
}

def stylize_time(value: str, font: str) -> str:
    mapping = CLOCK_FONTS.get(font, CLOCK_FONTS["classic"])[2]
    return value.translate(str.maketrans("0123456789", mapping))

def main_kb():
    # منوی اصلی؛ ارزها و دکمه ورود سلف عمداً حذف شده‌اند. ورود بعد از تأیید مدیر خودکار شروع می‌شود.
    rows = [
        [("⏰ زمان و پروفایل", "time"), ("🖼 پروفایل", "profile"), ("✍️ استایل متن", "text_style")],
        [("✨ انیمیشن", "animation"), ("👤 کاربران", "users_menu"), ("🔒 قفل رسانه", "media")],
        [("💬 کامنت", "comments"), ("📌 عمومی", "public"), ("🎭 اکشن", "actions")],
        [("🎮 بازی‌ها", "games"), ("🌐 ترجمه", "translate"), ("🔎 گوگل", "google")],
        [("ℹ️ اطلاعات", "info"), ("✉️ مدیریت پیام", "messages"), ("👍 ریکت", "reaction")],
        [("🚫 مسدودی‌ها", "enemies"), ("✏️ تغییر پروفایل", "edit"), ("🔎 فیلتر کلمات", "filter")],
        [("🛡 حفاظت اسم", "namelock"), ("🤖 هوشمند", "smart"), ("📣 گزارش", "report")],
        [("🛠 ابزارها", "tools"), ("🧠 منشی", "secretary"), ("📢 اطلاعیه", "broadcast")],
        [("🔮 فال", "fortune"), ("🔐 متن رمزی", "secret"), ("🧩 ابزارک‌ها", "widgets")],
        [("💎 الماس", "diamonds"), ("👥 سلف‌های فعال", "self_users")],
        [("📦 بکاپ", "backup"), ("🎨 دکمه‌ها", "buttons"), ("❓ راهنمای کامل", "help_all")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=c) for t,c in r] for r in rows] + [[InlineKeyboardButton(text="✖️ بستن", callback_data="close")]])

def login_stage_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ لغو", callback_data="login_cancel")]])

def phone_help_kb():
    # شماره عمداً دکمه ورود ندارد؛ کاربر بعد از تأیید مدیر مستقیماً شماره را به‌صورت متن می‌فرستد.
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 راهنمای شماره", callback_data="help:login_phone")],
        [InlineKeyboardButton(text="❌ لغو ورود", callback_data="login_cancel")]
    ])

def password_stage_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📖 راهنمای رمز دومرحله‌ای",callback_data="help:password")],[InlineKeyboardButton(text="❌ لغو ورود",callback_data="login_cancel")]])

def back(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ منوی اصلی", callback_data="main")]])
def clock_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 ساعت روشن", callback_data="clock:on"), InlineKeyboardButton(text="🔴 ساعت خاموش", callback_data="clock:off")],
        [InlineKeyboardButton(text="📝 فقط بیو", callback_data="clocktarget:bio"), InlineKeyboardButton(text="👤 فقط نام", callback_data="clocktarget:name")],
        [InlineKeyboardButton(text="📝👤 بیو + نام", callback_data="clocktarget:both")],
        [InlineKeyboardButton(text="🔤 انتخاب فونت", callback_data="clockfonts")],
        [InlineKeyboardButton(text="🌍 تهران", callback_data="tz:Asia/Tehran"), InlineKeyboardButton(text="🇩🇪 برلین", callback_data="tz:Europe/Berlin"), InlineKeyboardButton(text="🇹🇷 استانبول", callback_data="tz:Europe/Istanbul")],
        [InlineKeyboardButton(text="⬅️ برگشت", callback_data="main")],
    ])
def fonts_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{v[0]}  12:34", callback_data=f"font:{k}")] for k,v in CLOCK_FONTS.items()] + [[InlineKeyboardButton(text="⬅️ زمان", callback_data="time")]])

def feature_page(title, body, buttons=None):
    kb = buttons or []
    kb.append([InlineKeyboardButton(text="⬅️ منوی اصلی", callback_data="main")])
    return InlineKeyboardMarkup(inline_keyboard=kb), f"<b>{title}</b>\n\n{body}"

async def resolve_user(s, target):
    target = target.strip()
    if target.startswith("@"): target = target[1:]
    if target.isdigit(): return await get_user(s, int(target))
    return (await s.execute(select(User).where(func.lower(User.username) == target.lower()))).scalar_one_or_none()

async def diamond_balance(uid):
    if uid == OWNER_ID: return None
    async with Session() as s:
        u=await get_user(s,uid)
        return int(u.diamonds or 0) if u else 0

async def log_diamond(s, user_id:int, actor_id:int, amount:int, kind:str, note:str|None=None):
    s.add(DiamondTransaction(user_id=user_id, actor_id=actor_id, amount=int(amount), kind=kind, note=note))

async def change_diamonds(s, user_id:int, actor_id:int, amount:int, kind:str, note:str|None=None):
    u=await get_user(s,user_id)
    if not u:
        return None
    amount=int(amount)
    if user_id != OWNER_ID and amount < 0 and (u.diamonds or 0) + amount < 0:
        return False
    if user_id != OWNER_ID:
        u.diamonds=max(0,(u.diamonds or 0)+amount)
    await log_diamond(s,user_id,actor_id,amount,kind,note)
    return True

SELF_MONTH_DIAMONDS = 1000
SELF_ACTIVATION_COST = 1
WELCOME_DIAMONDS = 50
DIAMONDS_PACK_SIZE = 100
DIAMONDS_PACK_PRICE = 10000
DIAMOND_ADMIN_USERNAME = "@jokm7"

def diamond_purchase_text(balance=0):
    balance = int(balance or 0)
    return (
        "💎 <b>الماس کافی نیست</b>\n\n"
        f"موجودی فعلی شما: <b>{balance}</b> 💎\n"
        f"برای شروع/فعال‌سازی سلف حداقل <b>{SELF_ACTIVATION_COST} الماس</b> لازم است.\n"
        f"بعد از فعال‌سازی، اعتبار یک‌ماهه با مصرف تدریجی تا سقف <b>{SELF_MONTH_DIAMONDS:,} الماس</b> محاسبه می‌شود.\n\n"
        f"💰 هر {DIAMONDS_PACK_SIZE} الماس: <b>{DIAMONDS_PACK_PRICE:,} تومان</b>\n"
        f"💰 ۱۰۰۰ الماس: <b>{DIAMONDS_PACK_PRICE * 10:,} تومان</b>\n\n"
        f"📩 برای خرید الماس به ادمین پیام بده: <b>{DIAMOND_ADMIN_USERNAME}</b>"
    )

async def require_diamond_for_self(uid):
    # شروع ورود فقط ۱ الماس نیاز دارد؛ مصرف دوره ۳۰ روزه جداگانه و ساعتی انجام می‌شود.
    if uid == OWNER_ID: return True
    async with Session() as s:
        u=await get_user(s,uid)
        return bool(u and (u.diamonds or 0) >= SELF_ACTIVATION_COST)

async def get_user(s, uid): return (await s.execute(select(User).where(User.id == uid))).scalar_one_or_none()
async def approved_only(uid):
    """اجازه مرحله‌ای: فقط تأیید مدیر؛ برای وارد کردن شماره و ادامه ورود استفاده می‌شود."""
    if uid == OWNER_ID: return True
    async with Session() as s:
        u = await get_user(s, uid)
        return bool(u and u.approved and not u.banned)

async def allowed(uid):
    """دسترسی کامل؛ مدیر اصلی همیشه مجاز است و کاربران بعد از تأیید + ثبت شماره مجاز می‌شوند."""
    if uid == OWNER_ID: return True
    async with Session() as s:
        u = await get_user(s, uid)
        return bool(u and u.approved and not u.banned and u.phone_masked)

async def ensure_schema():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        def cols(c): return {x["name"] for x in inspect(c).get_columns("users")}
        existing = await conn.run_sync(cols)
        additions = {
            "diamonds":"INTEGER DEFAULT 0",
            "welcome_diamond_granted":"BOOLEAN DEFAULT FALSE",
            "diamond_billing_remainder":"DOUBLE PRECISION DEFAULT 0",
            "diamond_last_billed_at":"TIMESTAMP NULL",
            "self_expires_at":"TIMESTAMP NULL",
            "pending_phone":"VARCHAR(64) NULL", "access_requested":"BOOLEAN DEFAULT FALSE", "clock_enabled":"BOOLEAN DEFAULT FALSE", "clock_target":"VARCHAR(16) DEFAULT 'bio'", "clock_font":"VARCHAR(32) DEFAULT 'classic'", "clock_prefix":"VARCHAR(40) DEFAULT '🕐 '",
            "notifications":"BOOLEAN DEFAULT TRUE", "animation_enabled":"BOOLEAN DEFAULT TRUE", "animation_style":"VARCHAR(32) DEFAULT 'نرم'", "message_count":"INTEGER DEFAULT 0", "last_seen":"TIMESTAMP NULL", "self_session":"TEXT NULL", "phone_masked":"VARCHAR(64) NULL", "self_enabled":"BOOLEAN DEFAULT FALSE", "smart_mode":"BOOLEAN DEFAULT FALSE", "name_lock":"BOOLEAN DEFAULT FALSE", "locked_name":"VARCHAR(255) NULL", "word_filter":"BOOLEAN DEFAULT FALSE", "word_filter_text":"TEXT NULL", "media_lock":"BOOLEAN DEFAULT FALSE", "comments_mode":"BOOLEAN DEFAULT FALSE", "button_theme":"VARCHAR(32) DEFAULT 'orange'"
        }
        for col, definition in additions.items():
            if col not in existing: await conn.execute(sql_text(f"ALTER TABLE users ADD COLUMN {col} {definition}"))

def now_for(u):
    try: return datetime.now(ZoneInfo(u.timezone))
    except Exception: return datetime.now(ZoneInfo(DEFAULT_TZ))
def masked(phone):
    d="".join(x for x in phone if x.isdigit()); return "***" if len(d)<=4 else "+"+"*"*(len(d)-4)+d[-4:]
def encrypt(v):
    if not FERNET: raise RuntimeError("SESSION_SECRET تنظیم نشده است")
    return FERNET.encrypt(v.encode()).decode()
def decrypt(v):
    if not v or not FERNET: return None
    try: return FERNET.decrypt(v.encode()).decode()
    except (InvalidToken, ValueError): return None

def self_config_status():
    missing=[]
    if TG_API_ID <= 0: missing.append("TG_API_ID")
    if not TG_API_HASH: missing.append("TG_API_HASH")
    if FERNET is None: missing.append("SESSION_SECRET")
    return missing

def self_ready(): return not self_config_status()

async def get_self_client(uid):
    if not self_ready(): return None
    async with Session() as s:
        u=await get_user(s,uid)
        if u and u.self_enabled and uid != OWNER_ID and u.self_expires_at:
            exp=u.self_expires_at
            if exp.tzinfo is None: exp=exp.replace(tzinfo=timezone.utc)
            if exp <= datetime.now(timezone.utc):
                u.self_enabled=False; u.self_session=None; u.phone_masked=None; u.clock_enabled=False; u.self_expires_at=None
                await s.commit()
                stop_clock(uid); stop_billing(uid)
                return None
        raw=decrypt(u.self_session) if u and u.self_enabled else None
    if not raw: return None
    c=TelegramClient(StringSession(raw), TG_API_ID, TG_API_HASH); await c.connect()
    if not await c.is_user_authorized(): await c.disconnect(); return None
    return c

async def update_clock_once(uid):
    c = await get_self_client(uid)
    if not c: return False
    try:
        async with Session() as s: u=await get_user(s,uid)
        if not u or not u.clock_enabled: return True
        tm=stylize_time(now_for(u).strftime("%H:%M"),u.clock_font)
        bio = f"{u.clock_prefix}{tm}"
        name = f"{u.locked_name or u.first_name or 'User'} {tm}"
        kwargs={}
        if u.clock_target in ("bio","both"): kwargs["about"]=bio[:70]
        if u.clock_target in ("name","both"): kwargs["first_name"]=name[:64]
        if kwargs: await c(UpdateProfileRequest(**kwargs))
        return True
    except Exception as e:
        log.warning("clock update %s: %s",uid,e); return False
    finally: await c.disconnect()

async def clock_loop(uid):
    try:
        while True:
            ok=await update_clock_once(uid)
            if not ok: break
            await asyncio.sleep(60)
    except asyncio.CancelledError: pass
    finally: CLOCK_TASKS.pop(uid,None)

def start_clock(uid):
    old=CLOCK_TASKS.get(uid)
    if old and not old.done(): old.cancel()
    CLOCK_TASKS[uid]=asyncio.create_task(clock_loop(uid))

def stop_clock(uid):
    t=CLOCK_TASKS.pop(uid,None)
    if t and not t.done(): t.cancel()

async def bill_self_once(uid):
    if uid == OWNER_ID:
        return True
    async with Session() as s:
        u=await get_user(s,uid)
        if not u or not u.self_enabled:
            return False
        now=datetime.now(timezone.utc)
        last=u.diamond_last_billed_at
        if last is None:
            u.diamond_last_billed_at=now
            await s.commit()
            return True
        if last.tzinfo is None: last=last.replace(tzinfo=timezone.utc)
        elapsed_seconds=max(0,(now-last).total_seconds())
        hours=int(elapsed_seconds // 3600)
        if hours <= 0:
            return True
        rate=SELF_MONTH_DIAMONDS/720.0
        due=u.diamond_billing_remainder + hours*rate
        charge=int(due)
        u.diamond_billing_remainder=due-charge
        u.diamond_last_billed_at=last + timedelta(hours=hours)
        if charge > 0:
            if (u.diamonds or 0) < charge:
                u.self_enabled=False; u.self_session=None; u.phone_masked=None; u.clock_enabled=False; u.self_expires_at=None
                u.diamond_billing_remainder=0.0; u.diamond_last_billed_at=None
                await s.commit()
                stop_clock(uid)
                return False
            u.diamonds -= charge
            await log_diamond(s, uid, uid, -charge, "hourly", "مصرف ساعتی سلف")
        exp=u.self_expires_at
        if exp and exp.tzinfo is None: exp=exp.replace(tzinfo=timezone.utc)
        if exp and now >= exp:
            u.self_enabled=False; u.self_session=None; u.phone_masked=None; u.clock_enabled=False; u.self_expires_at=None
            u.diamond_billing_remainder=0.0; u.diamond_last_billed_at=None
            await s.commit(); stop_clock(uid); return False
        await s.commit()
    return True

async def billing_loop(uid):
    try:
        while True:
            if not await bill_self_once(uid): break
            await asyncio.sleep(60)
    except asyncio.CancelledError: pass
    finally: BILLING_TASKS.pop(uid,None)

def start_billing(uid):
    if uid == OWNER_ID: return
    old=BILLING_TASKS.get(uid)
    if old and not old.done(): old.cancel()
    BILLING_TASKS[uid]=asyncio.create_task(billing_loop(uid))

def stop_billing(uid):
    t=BILLING_TASKS.pop(uid,None)
    if t and not t.done(): t.cancel()

async def profile_text(uid):
    async with Session() as s:
        u=await get_user(s,uid)
        if not u:return "پروفایل پیدا نشد."
        t=now_for(u).strftime("%H:%M:%S")
        balance = "∞" if u.id == OWNER_ID else str(u.diamonds or 0)
        return f"👤 <b>پروفایل شما</b>\n\n🆔 <code>{u.id}</code>\n🔗 @{escape(u.username or 'ندارد')}\n🌍 {u.timezone}\n💎 الماس: <b>{balance}</b>\n🕐 {stylize_time(t,u.clock_font)}\n💬 {u.message_count or 0} پیام\n🔐 سلف: {'فعال' if u.self_enabled else 'خاموش'}\n⏰ ساعت پروفایل: {'روشن' if u.clock_enabled else 'خاموش'}\n🔤 فونت: {CLOCK_FONTS.get(u.clock_font,('',))[0]}"

async def notify_access_request(bot, m, uid):
    try:
        await bot.send_message(OWNER_ID,
            f"📥 <b>درخواست دسترسی جدید</b>\n👤 {escape(m.from_user.full_name)}\n🆔 <code>{uid}</code>\n\nکاربر هنوز شماره‌ای نداده است؛ بعد از تأیید شما، شماره به‌صورت اجباری از او گرفته می‌شود.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ تأیید دسترسی",callback_data=f"approve:{uid}"),InlineKeyboardButton(text="❌ رد درخواست",callback_data=f"reject:{uid}")],
                [InlineKeyboardButton(text="📖 راهنمای تأیید",callback_data="help:admin_approval")]
            ]))
        return True
    except Exception as e:
        log.warning("access request notify failed: %s", e)
        return False

@dp.message(CommandStart())
async def start(m: Message):
    uid=m.from_user.id
    # مدیر اصلی همیشه مدیر است و هرگز وارد مرحله شماره/ورود سلف نمی‌شود.
    # حتی اگر رکورد قبلی در دیتابیس ناقص، ردشده یا مسدود شده باشد، با /start دوباره مدیریتش فعال می‌شود.
    if uid == OWNER_ID:
        async with Session() as s:
            u=await get_user(s,uid)
            if not u:
                u=User(id=uid,username=m.from_user.username,first_name=m.from_user.first_name,approved=True,banned=False,access_requested=False,self_enabled=False)
                s.add(u)
            else:
                u.username=m.from_user.username
                u.first_name=m.from_user.first_name
                u.approved=True
                u.banned=False
                u.access_requested=False
            await s.commit()
        await m.answer("👑 <b>پنل مدیریت اصلی</b>\n\nمدیر بدون نیاز به شماره و ورود سلف به همه بخش‌های مدیریتی دسترسی دارد.\n\nاز منوی زیر بات را مدیریت کن؛ هر بخش راهنمای فارسی دارد.",reply_markup=main_kb()); return

    async with Session() as s:
        u=await get_user(s,uid)
        if not u:
            u=User(id=uid,username=m.from_user.username,first_name=m.from_user.first_name,approved=False,access_requested=True)
            s.add(u); await s.commit()
            requested=True
            await notify_access_request(m.bot,m,uid)
        else:
            u.username=m.from_user.username; u.first_name=m.from_user.first_name
            requested=bool(u.access_requested)
            approved=bool(u.approved and not u.banned)
            await s.commit()
    if not approved:
        if requested:
            await m.answer("⏳ <b>درخواست دسترسی شما برای مدیر ارسال شده است.</b>\n\nفعلاً هیچ شماره یا کدی لازم نیست. بعد از تأیید مدیر، شماره اکانت را اجباری از شما می‌گیریم و سپس مرحله کد باز می‌شود.", reply_markup=login_stage_kb())
        else:
            async with Session() as s:
                u=await get_user(s,uid)
                u.access_requested=True
                await s.commit()
            ok=await notify_access_request(m.bot,m,uid)
            await m.answer("📨 درخواست دسترسی دوباره برای مدیر ارسال شد." if ok else "⚠️ درخواست ثبت شد، اما ارسال پیام به مدیر ناموفق بود.")
        return
    async with Session() as s:
        u=await get_user(s,uid)
        phone=bool(u.pending_phone); enabled=bool(u.self_enabled)
    if enabled:
        await m.answer("🎛 <b>ورود قبلاً کامل شده است.</b>",reply_markup=main_kb())
    elif not phone:
        await ask_phone(m)
    else:
        await ask_phone(m)

async def ask_phone(m:Message):
    await m.answer("📱 <b>شماره اکانت تلگرامت اجباری است</b>\n\nشماره را همینجا به‌صورت متن بفرست.\n\nنمونه:\n<code>+989967066405</code>\n\n⚠️ شماره باید با + و کد کشور شروع شود. به‌محض دریافت شماره معتبر، کد ورود خودکار ارسال می‌شود.")

@dp.message(Command("panel"))
async def panel(m:Message):
    # مدیر اصلی مستقل از وضعیت شماره/سلف همیشه پنل را می‌بیند.
    if m.from_user.id == OWNER_ID or await allowed(m.from_user.id):
        await m.answer("🎛 <b>پنل مدیریت</b>",reply_markup=main_kb())
    else:
        await m.answer("⛔ ابتدا باید توسط مدیر تأیید شوید و ورود سلف را کامل کنید.")

@dp.message(Command("admin"))
async def admin_panel(m:Message):
    # میانبر مطمئن برای مدیر؛ هیچ شماره‌ای برای باز کردن پنل لازم نیست.
    if m.from_user.id != OWNER_ID:
        await m.answer("⛔ این دستور فقط برای مدیر اصلی است.")
        return
    await m.answer("👑 <b>پنل مدیریت اصلی</b>\n\nمدیر برای مدیریت بات نیازی به شماره یا ورود سلف ندارد.",reply_markup=main_kb())

@dp.message(Command("selfapi"))
async def selfapi_cmd(m:Message):
    if m.from_user.id != OWNER_ID:
        await m.answer("⛔ این دستور فقط برای مدیر اصلی است.")
        return
    missing=self_config_status()
    if not missing:
        await m.answer("✅ <b>تنظیمات ورود سلف کامل است.</b>\n\nشناسه API و کلید API تنظیم شده‌اند و رمزنگاری نشست نیز آماده است.")
        return
    await m.answer(
        "⚙️ <b>بررسی تنظیمات ورود سلف</b>\n\n"
        "موارد ناقص:\n" + "\n".join(f"❌ <code>{x}</code>" for x in missing)
        + "\n\nاین سه مقدار باید در بخش Variables سرویس ریل‌وی قرار بگیرند."
    )

@dp.message(Command("help"))
async def help_cmd(m:Message):
    await m.answer("📚 <b>راهنمای سریع</b>\n\n/login +شماره → ورود سلف\n/code کد → کد ورود\n/password رمز → تأیید دومرحله‌ای\n/selfstatus → وضعیت سلف\n/selfoff → خاموش‌کردن سلف\n/clock → تنظیم ساعت\n/profile → پروفایل\n/autoreply متن → پاسخ خودکار\n/reaction ❤️ → واکنش خودکار\n/setname متن → تغییر نام\n/setbio متن → تغییر بیو\n/setusername نام → تغییر نام کاربری\n\nبرای توضیح کامل هر بخش، از خود پنل روی «راهنمای کامل» بزن.")

@dp.message(Command("login"))
async def login(m:Message):
    """ورود سلف از صفر: تأیید مدیر → شماره متنی → کد متنی → رمز دومرحله‌ای در صورت نیاز."""
    if not await approved_only(m.from_user.id):
        await m.answer("⛔ ابتدا باید دسترسی شما توسط مدیر تأیید شود.")
        return
    await ask_phone(m)


def normalize_phone(value: str) -> str | None:
    value=(value or "").strip()
    if not value.startswith("+"):
        return None
    trans=str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    compact=value.translate(trans)
    compact="".join(ch for ch in compact if ch.isdigit() or ch=="+")
    if not compact.startswith("+") or not compact[1:].isdigit():
        return None
    digits=compact[1:]
    if not 8 <= len(digits) <= 15:
        return None
    return "+"+digits


def normalize_code(value: str) -> str:
    trans=str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    return "".join(ch for ch in (value or "").translate(trans) if ch.isdigit())


async def begin_login(m:Message, phone:str):
    if not self_ready():
        missing=self_config_status()
        await m.answer("❌ ورود سلف آماده نیست. موارد زیر را در Variables سرویس تنظیم کن:\n\n" + "\n".join(f"• <code>{x}</code>" for x in missing))
        return False
    old=LOGIN_FLOWS.pop(m.from_user.id,None)
    if old:
        try: await old["client"].disconnect()
        except Exception: pass
    c=TelegramClient(StringSession(),TG_API_ID,TG_API_HASH)
    try:
        await c.connect()
        sent=await c.send_code_request(phone)
        LOGIN_FLOWS[m.from_user.id]={"client":c,"phone":phone,"hash":sent.phone_code_hash,"stage":"code"}
        return True
    except FloodWaitError as e:
        try: await c.disconnect()
        except Exception: pass
        await m.answer(f"⏳ تلگرام موقتاً محدود کرده است. حدود {getattr(e,'seconds',60)} ثانیه صبر کن و بعد دوباره شماره را بفرست.")
    except Exception as e:
        try: await c.disconnect()
        except Exception: pass
        log.warning("login request failed: %s",e)
        await m.answer("❌ ارسال کد انجام نشد. شماره را بررسی کن و دوباره بفرست.")
    return False


@dp.message(F.contact)
async def contact_login(m:Message):
    await m.answer("ℹ️ این ورود فقط با شماره‌ای که به‌صورت متن می‌فرستی انجام می‌شود.\n\nنمونه: <code>+989967066405</code>")


@dp.message(F.text == ".login")
async def owner_login_command(m:Message):
    # این دستور فقط برای مدیر اصلی فعال است.
    if m.from_user.id != OWNER_ID:
        return
    old=LOGIN_FLOWS.pop(OWNER_ID,None)
    if old:
        try:
            await old["client"].disconnect()
        except Exception:
            pass
    if not self_ready():
        missing=self_config_status()
        await m.answer("❌ ورود سلف آماده نیست. موارد زیر را در Variables سرویس تنظیم کن:\n\n" + "\n".join(f"• <code>{x}</code>" for x in missing))
        return
    LOGIN_FLOWS[OWNER_ID]={"stage":"phone"}
    await m.answer(
        "🔐 <b>ورود سلف مدیر</b>\n\n"
        "📱 شماره اکانت تلگرام را به‌صورت متن بفرست.\n"
        "مثال: <code>+989967066405</code>\n\n"
        "بعد از شماره، کد ورود تلگرام خودکار درخواست می‌شود."
    )


@dp.message(F.text)
async def phone_text(m:Message):
    uid=m.from_user.id
    flow=LOGIN_FLOWS.get(uid)

    # مسیر اختصاصی مدیر: فقط بعد از .login شماره پذیرفته می‌شود.
    if uid == OWNER_ID and flow and flow.get("stage") == "phone":
        phone=normalize_phone(m.text or "")
        if not phone:
            await m.answer("⚠️ شماره معتبر نیست. شماره را با + و کد کشور بفرست.\n\nنمونه: <code>+989967066405</code>")
            return
        await m.answer(f"📱 شماره دریافت شد: <code>{escape(masked(phone))}</code>\n\n⏳ کد ورود تلگرام در حال ارسال است...")
        ok=await begin_login(m,phone)
        if ok:
            f=LOGIN_FLOWS.get(uid)
            if f:
                f["stage"]="code"
                f["phone"]=phone
            await m.answer("🔢 <b>کد ورود ارسال شد.</b>\n\nکد را همینجا <b>به‌صورت متن</b> بفرست؛ فارسی یا انگلیسی هر دو قبول است.")
        return

    # اگر ورود در جریان است، همین پیام مستقیماً به مرحله کد یا رمز می‌رود.
    if flow:
        if flow.get("stage")=="password":
            await submit_login_password(m,m.text or "")
        elif flow.get("stage")=="code":
            await submit_login_code(m,m.text or "")
        return

    # مدیر فقط از طریق .login اجازه شروع ورود دارد.
    if uid == OWNER_ID:
        return
    if not await approved_only(uid):
        return

    async with Session() as s:
        u=await get_user(s,uid)
        if not u or u.self_enabled:
            return
    phone=normalize_phone(m.text or "")
    if not phone:
        if (m.text or "").strip().startswith(("+","۰","۱","۲","۳","۴","۵","۶","۷","۸","۹")):
            await m.answer("⚠️ شماره معتبر نیست. شماره را با + و کد کشور بفرست.\n\nنمونه: <code>+989967066405</code>")
        return
    if not await require_diamond_for_self(uid):
        async with Session() as s:
            u=await get_user(s,uid); bal=(u.diamonds or 0) if u else 0
        await m.answer(diamond_purchase_text(bal))
        return
    await m.answer(f"📱 شماره دریافت شد: <code>{escape(masked(phone))}</code>\n\n⏳ کد ورود تلگرام در حال ارسال است...")
    ok=await begin_login(m,phone)
    if ok:
        async with Session() as s:
            u=await get_user(s,uid)
            if u: u.pending_phone=phone; await s.commit()
        await m.answer("🔢 <b>کد ورود ارسال شد.</b>\n\nکد را همینجا <b>به‌صورت متن</b> بفرست؛ فارسی یا انگلیسی هر دو قبول است. هیچ دکمه‌ای برای وارد کردن کد وجود ندارد.\n\nمثال: <code>12345</code>")


@dp.callback_query(F.data=="login_cancel")
async def login_cancel_cb(c:CallbackQuery):
    f=LOGIN_FLOWS.pop(c.from_user.id,None)
    if f:
        try: await f["client"].disconnect()
        except Exception: pass
    async with Session() as s:
        u=await get_user(s,c.from_user.id)
        if u: u.pending_phone=None; await s.commit()
    await c.message.edit_text("❎ ورود لغو شد. برای شروع دوباره <code>.login</code> را بزن.")
    await c.answer()


@dp.message(Command("code"))
async def code(m:Message):
    p=(m.text or "").split(maxsplit=1)
    if len(p)==2:
        await submit_login_code(m,p[1])
    else:
        await m.answer("🔢 کد تلگرام را همینجا به‌صورت متن بفرست.")


async def submit_login_code(m:Message, code_value:str):
    f=LOGIN_FLOWS.get(m.from_user.id)
    if not f or f.get("stage")!="code":
        await m.answer("❌ مرحله ورود کد فعال نیست. دوباره شماره را بفرست.")
        return
    normalized=normalize_code(code_value)
    if not 4 <= len(normalized) <= 8:
        await m.answer("⚠️ کد باید فقط ۴ تا ۸ رقم باشد. کد را دوباره به‌صورت متن بفرست.")
        return
    try:
        await f["client"].sign_in(phone=f["phone"],code=normalized,phone_code_hash=f["hash"])
        await finish_login(m,f)
    except SessionPasswordNeededError:
        f["stage"]="password"
        await m.answer("🔐 <b>رمز دومرحله‌ای فعال است.</b>\n\nرمز دومرحله‌ای را همینجا به‌صورت متن وارد کن. آن را برای هیچ‌کس ارسال نکن.")
    except PhoneCodeInvalidError:
        await m.answer("❌ کد اشتباه است. فقط آخرین کدی که تلگرام فرستاده را به‌صورت متن وارد کن.")
    except PhoneCodeExpiredError:
        f=LOGIN_FLOWS.pop(m.from_user.id,None)
        if f:
            try: await f["client"].disconnect()
            except Exception: pass
        async with Session() as s:
            u=await get_user(s,m.from_user.id)
            if u: u.pending_phone=None; await s.commit()
        await m.answer("⌛ کد منقضی شده است. دوباره شماره را بفرست تا یک کد جدید درخواست شود.")
    except FloodWaitError as e:
        await m.answer(f"⏳ محدودیت موقت تلگرام: حدود {getattr(e,'seconds',60)} ثانیه صبر کن.")
    except Exception as e:
        log.warning("login code failed: %s",e)
        await m.answer("❌ ورود با این کد انجام نشد. اگر کد جدیدی گرفته‌ای، فقط همان آخرین کد را وارد کن.")


@dp.message(Command("password"))
async def password(m:Message):
    p=(m.text or "").split(maxsplit=1)
    await submit_login_password(m, p[1] if len(p)==2 else "")


async def submit_login_password(m:Message, password_value:str):
    f=LOGIN_FLOWS.get(m.from_user.id)
    if not f or f.get("stage")!="password":
        await m.answer("❌ مرحله رمز دومرحله‌ای فعال نیست. دوباره ورود را شروع کن.")
        return
    if not password_value:
        await m.answer("🔐 رمز دومرحله‌ای را به‌صورت متن وارد کن.")
        return
    try:
        await f["client"].sign_in(password=password_value)
        await finish_login(m,f)
    except Exception:
        await m.answer("❌ رمز دومرحله‌ای درست نیست. دوباره واردش کن.")




async def finish_login(m,f):
    uid=m.from_user.id
    try:
        raw=f["client"].session.save()
    finally:
        try: await f["client"].disconnect()
        except Exception: pass
        LOGIN_FLOWS.pop(uid,None)
    async with Session() as s:
        u=await get_user(s,uid)
        if not u:
            await m.answer("❌ کاربر پیدا نشد. دوباره /start را بزن.")
            return
        if uid != OWNER_ID:
            if (u.diamonds or 0) < SELF_ACTIVATION_COST:
                await m.answer(diamond_purchase_text(u.diamonds or 0))
                return
            u.diamonds -= SELF_ACTIVATION_COST
            await log_diamond(s, uid, uid, -SELF_ACTIVATION_COST, "activation", "هزینه شروع سلف")
            now=datetime.now(timezone.utc)
            current=u.self_expires_at
            if current and current.tzinfo is None: current=current.replace(tzinfo=timezone.utc)
            base=current if current and current > now else now
            u.self_expires_at=base + timedelta(days=30)
            u.diamond_billing_remainder=0.0
            u.diamond_last_billed_at=now
        else:
            u.self_expires_at=None
        u.self_session=encrypt(raw)
        u.phone_masked=masked(f["phone"])
        u.pending_phone=None
        u.self_enabled=True
        await s.commit()
        expiry=u.self_expires_at
    if uid == OWNER_ID:
        expiry_text="♾️ اعتبار سلف مدیر نامحدود است."
    else:
        expiry_text=f"📅 اعتبار سلف تا <b>{expiry.astimezone(timezone.utc).strftime('%Y/%m/%d %H:%M UTC')}</b> است."
    await m.answer("✅ <b>ورود موفق بود!</b>\n\nاکانت با همین شماره به‌عنوان سلف متصل شد و نشست به‌صورت رمزنگاری‌شده ذخیره شد.\n"+expiry_text)
    start_billing(uid)
    await m.answer("🎛 <b>پنل حرفه‌ای آماده است</b>",reply_markup=main_kb())

@dp.message(Command("selfstatus"))
async def selfstatus(m:Message):
    async with Session() as s:u=await get_user(s,m.from_user.id)
    if u and u.self_enabled and u.self_expires_at and u.id != OWNER_ID:
        exp=u.self_expires_at
        if exp.tzinfo is None: exp=exp.replace(tzinfo=timezone.utc)
        expiry=exp.strftime("%Y/%m/%d %H:%M UTC")
    else:
        expiry="∞ نامحدود" if u and u.id==OWNER_ID else "ثبت نشده"
    balance='∞' if u and u.id==OWNER_ID else str(u.diamonds or 0) if u else '0'
    await m.answer(f"🔐 <b>وضعیت سلف</b>\n\nفعال: {'✅' if u and u.self_enabled else '❌'}\nشماره: {escape(u.phone_masked or 'ثبت نشده') if u else 'ثبت نشده'}\n💎 موجودی الماس: <b>{balance}</b>\nاعتبار: <b>{expiry}</b>\nساعت زنده: {'✅' if u and u.clock_enabled else '❌'}")

@dp.message(Command("selfoff"))
async def selfoff(m:Message):
    stop_clock(m.from_user.id)
    async with Session() as s:
        u=await get_user(s,m.from_user.id)
        if u: u.self_enabled=False; u.self_session=None; u.phone_masked=None; u.clock_enabled=False; u.self_expires_at=None; await s.commit()
    await m.answer("🔴 سلف خاموش شد و نشست ذخیره‌شده حذف شد.")

@dp.message(Command("clock"))
async def clock_cmd(m:Message):
    if not await allowed(m.from_user.id): return
    await m.answer("⏰ <b>ساعت زنده پروفایل</b>\n\nاین قابلیت ساعت را به‌صورت خودکار در بیو یا نام پروفایل حساب سلف قرار می‌دهد.\n\n⚠️ برای جلوگیری از محدودیت تلگرام، بروزرسانی هر دقیقه انجام می‌شود و ممکن است تلگرام در بعضی حساب‌ها سرعت تغییرات پروفایل را محدود کند.",reply_markup=clock_kb())

@dp.message(Command("profile"))
async def profile(m:Message):
    if await allowed(m.from_user.id): await m.answer(await profile_text(m.from_user.id))

@dp.message(Command("setname"))
async def setname(m:Message):
    if not await allowed(m.from_user.id): return
    p=(m.text or "").split(maxsplit=1)
    if len(p)!=2: await m.answer("مثال: /setname نام جدید"); return
    c=await get_self_client(m.from_user.id)
    if not c: await m.answer("❌ ابتدا با /login وارد سلف شو."); return
    try: await c(UpdateProfileRequest(first_name=p[1][:64])); await m.answer("✅ نام تغییر کرد.")
    except Exception: await m.answer("❌ تغییر نام ناموفق بود.")
    finally: await c.disconnect()

@dp.message(Command("setbio"))
async def setbio(m:Message):
    if not await allowed(m.from_user.id): return
    p=(m.text or "").split(maxsplit=1)
    if len(p)!=2: await m.answer("مثال: /setbio متن بیو"); return
    c=await get_self_client(m.from_user.id)
    if not c: await m.answer("❌ ابتدا /login را انجام بده."); return
    try: await c(UpdateProfileRequest(about=p[1][:70])); await m.answer("✅ بیو تغییر کرد.")
    except Exception: await m.answer("❌ تغییر بیو ناموفق بود.")
    finally: await c.disconnect()

@dp.message(Command("setusername"))
async def setusername(m:Message):
    if not await allowed(m.from_user.id): return
    p=(m.text or "").split(maxsplit=1)
    if len(p)!=2: await m.answer("مثال: /setusername MyName"); return
    c=await get_self_client(m.from_user.id)
    if not c: await m.answer("❌ ابتدا /login را انجام بده."); return
    try: await c(UpdateUsernameRequest(p[1].lstrip("@"))); await m.answer("✅ نام کاربری تغییر کرد.")
    except Exception: await m.answer("❌ نام کاربری آزاد نیست یا شرایط تلگرام را ندارد.")
    finally: await c.disconnect()

@dp.message(Command("autoreply"))
async def autoreply(m:Message):
    if not await allowed(m.from_user.id): return
    p=(m.text or "").split(maxsplit=1)
    async with Session() as s:
        u=await get_user(s,m.from_user.id); u.auto_reply=p[1] if len(p)==2 else None; await s.commit()
    await m.answer("🤖 پاسخ خودکار " + ("فعال شد." if len(p)==2 else "خاموش شد."))

@dp.message(Command("reaction"))
async def reaction(m:Message):
    if not await allowed(m.from_user.id): return
    # تنظیم ریکت با ریپلای روی پیام کاربر؛ بدون ریپلای هم برای سازگاری، حالت کلی حساب ذخیره می‌شود.
    p=(m.text or "").split(maxsplit=1)
    if not len(p)==2:
        async with Session() as s:
            u=await get_user(s,m.from_user.id); u.auto_reaction=None; await s.commit()
        await m.answer("🗑 <b>ریکت خودکار حذف شد.</b>\n\nبرای تنظیم دوباره، روی پیام کاربر ریپلای کن و <code>/reaction ❤️</code> را بفرست.")
        return
    emoji=p[1].strip()[:8]
    async with Session() as s:
        u=await get_user(s,m.from_user.id); u.auto_reaction=emoji; await s.commit()
    scope="این کاربر" if m.reply_to_message else "حساب"
    await m.answer(f"👍 <b>ریکت خودکار تنظیم شد</b>\n\nریکت انتخابی: {escape(emoji)}\nمحدوده: {scope}\n\n🗑 <b>حذف ریکت:</b> روی پیام همان کاربر ریپلای کن و <code>/reaction</code> بفرست.")

@dp.callback_query(F.data=="main")
async def main_cb(c:CallbackQuery): await c.message.edit_text("🎛 <b>منوی اصلی</b>\n\nهر بخش راهنمای داخلی دارد.",reply_markup=main_kb()); await c.answer()
@dp.callback_query(F.data=="close")
async def close_cb(c:CallbackQuery): await c.message.delete(); await c.answer()

FEATURES={
"animation":("✨ انیمیشن","یک بخش نمایشی و قابل تنظیم برای ظاهر و حرکت پیام‌های پنل. می‌توانی انیمیشن را روشن یا خاموش کنی و بین چند سبک آماده انتخاب کنی."),
"users_menu":("👤 کاربران","مدیریت وضعیت دسترسی کاربران، مشاهده آمار و رسیدگی به درخواست‌ها از این بخش انجام می‌شود. مدیر می‌تواند کاربران را تأیید یا مسدود کند."),
"media":("🔒 قفل رسانه","با این قابلیت می‌توانی دریافت رسانه در گفت‌وگوهای مدیریت‌شده را کنترل کنی. برای روشن/خاموش کردن از دکمه زیر استفاده کن."),
"comments":("💬 کامنت","حالت مدیریت کامنت برای جریان‌های مرتبط با گروه/کانال. این قابلیت فقط در جاهایی که حساب سلف دسترسی لازم دارد عمل می‌کند."),
"public":("📌 عمومی","تنظیمات عمومی سلف و دسترسی‌های پنل در این قسمت قرار می‌گیرند."),
"actions":("🎭 اکشن","مجموعه ابزارهای واکنش و عملیات سریع روی پیام‌ها. برای ریکت خودکار روی پیام کاربر ریپلای کن و <code>/reaction ❤️</code> را بفرست."),
"games":("🎮 بازی‌ها","بازی‌های کوچک داخلی مثل تاس و فال. دستور نمونه: /dice"),
"translate":("🌐 ترجمه","متن را با /translate متن بفرست تا لینک ترجمه آماده شود. برای ترجمه زنده نیاز به سرویس ترجمه جداگانه است."),
"google":("🔎 گوگل","/google عبارت را بفرست تا لینک جست‌وجوی آماده دریافت کنی."),
"info":("ℹ️ اطلاعات","اطلاعات حساب و وضعیت سلف را با /profile و /selfstatus ببین."),
"profile":("🖼 پروفایل","مدیریت اطلاعات حساب سلف: نام، بیو و نام کاربری. دستورات /setname، /setbio و /setusername هستند."),
"text_style":("✍️ استایل متن","برای متن ضخیم /bold متن و برای متن کدی /codeText متن را استفاده کن."),
"messages":("✉️ مدیریت پیام","پاسخ خودکار با /autoreply متن فعال می‌شود و بدون متن خاموش می‌شود. مدیریت انبوه مزاحم ارائه نمی‌شود."),
"reaction":("👍 ریکت","برای تنظیم ریکت خودکار، روی پیام کاربر ریپلای کن و <code>/reaction ❤️</code> را بفرست. برای حذف ریکت، روی همان پیام ریپلای کن و <code>/reaction</code> را بفرست."),
"enemies":("🚫 مسدودی‌ها","مدیریت مسدودی کاربران پنل توسط مدیر با /ban ID و /unban ID انجام می‌شود."),
"edit":("✏️ تغییر پروفایل","نام، بیو و نام کاربری سلف را تغییر بده. هر تغییر از طریق API رسمی تلگرام انجام می‌شود."),
"filter":("🔎 فیلتر کلمات","فیلتر کلمات را برای پیام‌های دریافتی مدیریت کن. این قابلیت فقط روی پیام‌هایی که بات امکان مدیریتشان را دارد عمل می‌کند."),
"namelock":("🛡 حفاظت اسم","برای جلوگیری از تغییر ناخواسته نام توسط ابزار ساعت، هدف ساعت را روی بیو بگذار یا نام پایه را در تنظیمات نگه دار."),
"smart":("🤖 هوشمند","حالت پاسخ هوشمند ساده برای پیام‌های دریافتی. برای هوش مصنوعی واقعی باید API مدل را در محیط اضافه کرد."),
"report":("📣 گزارش","مشکل را با /report متن برای مدیر ارسال کن."),
"tools":("🛠 ابزارها","ابزارهای کمکی: زمان، پروفایل، تبدیل متن، جست‌وجو و مدیریت نشست."),
"secretary":("🧠 منشی","پاسخ خودکار با /autoreply و حالت هوشمند از این بخش قابل مدیریت است."),
"broadcast":("📢 اطلاعیه","مدیر می‌تواند با ریپلای روی یک پیام و /broadcast آن را با سرعت کنترل‌شده برای کاربران تأییدشده ارسال کند. اسپم و ارسال مزاحم عمداً محدود شده است."),
"fortune":("🔮 فال","/fortune یک فال سرگرمی تصادفی می‌دهد."),
"secret":("🔐 متن رمزی","/secret متن برای کدگذاری Base64 و /unsecret کد برای بازکردن آن. این رمزنگاری امن برای اطلاعات حساس نیست."),
"widgets":("🧩 ابزارک‌ها","ابزارک‌های داخلی مثل ساعت، پروفایل و تنظیمات سریع در این قسمت قرار دارند."),
"backup":("📦 بکاپ","اطلاعات تنظیمات در دیتابیس نگهداری می‌شود. نشست سلف رمزنگاری‌شده ذخیره می‌شود؛ توکن‌ها را در GitHub قرار نده."),
"buttons":("🎨 دکمه‌ها","منوی فارسی و توضیح هر بخش برای استفاده راحت آماده شده است."),
"diamonds":("💎 الماس",f"با تأیید مدیر، <b>{WELCOME_DIAMONDS} الماس هدیه</b> می‌گیری. برای شروع سلف فقط <b>{SELF_ACTIVATION_COST} الماس</b> همان لحظه کم می‌شود. اعتبار هر دوره <b>۳۰ روز</b> است و تا <b>{SELF_MONTH_DIAMONDS:,} الماس</b> به‌صورت ساعتی مصرف می‌شود؛ ۱۰۰۰ الماس یکجا صفر نمی‌شود. مدیر موجودی نامحدود دارد. قیمت هر ۱۰۰ الماس <b>۱۰٬۰۰۰ تومان</b> است و خرید از {DIAMOND_ADMIN_USERNAME} انجام می‌شود."),
"self_users":("👥 سلف‌های فعال","فهرست کاربرانی که ورود سلفشان با موفقیت انجام شده است. مدیر می‌تواند نشست سلف هر کاربر را حذف و دسترسی سلف او را لغو کند."),
"help_all":("❓ راهنمای کامل","مسیر ورود: درخواست دسترسی ← تأیید مدیر ← ورود اجباری شماره ← ارسال کد ← ورود کد با دکمه‌ها ← تأیید نهایی. بعد از ورود، هر بخش پنل راهنمای داخلی دارد."),
}

@dp.callback_query(F.data=="time")
async def time_page(c:CallbackQuery): await c.message.edit_text("⏰ <b>زمان و پروفایل</b>\n\nساعت زنده می‌تواند در بیو، نام یا هر دو قرار بگیرد. منطقه زمانی و فونت را خودت انتخاب می‌کنی.\n\nراهنما: اول وارد سلف شو، فونت را انتخاب کن، منطقه زمانی را بزن و بعد «ساعت روشن» را بزن.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📖 راهنمای این بخش",callback_data="help:time")]]+clock_kb().inline_keyboard)); await c.answer()
@dp.callback_query(F.data=="clockfonts")
async def clockfonts(c:CallbackQuery): await c.message.edit_text("🔤 <b>فونت ساعت</b>\n\nیکی را انتخاب کن. نمونه زیر هر دکمه نشان می‌دهد ساعت چگونه دیده می‌شود.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📖 راهنمای فونت",callback_data="help:time")]]+fonts_kb().inline_keyboard)); await c.answer()
@dp.callback_query(F.data.startswith("font:"))
async def font_cb(c:CallbackQuery):
    font=c.data.split(":",1)[1]
    async with Session() as s:u=await get_user(s,c.from_user.id); u.clock_font=font; await s.commit()
    await c.answer("فونت ذخیره شد")
    await c.message.edit_text("✅ فونت ساعت ذخیره شد.\n\nحالا می‌توانی از بخش زمان، ساعت را روشن کنی.",reply_markup=clock_kb())
@dp.callback_query(F.data.startswith("clock:"))
async def clock_toggle(c:CallbackQuery):
    val=c.data.split(":",1)[1]
    async with Session() as s:
        u=await get_user(s,c.from_user.id)
        if not u: await c.answer("کاربر پیدا نشد",show_alert=True); return
        if val=="on" and not u.self_enabled:
            await c.answer("اول اتصال اکانت را کامل کن",show_alert=True); return
        u.clock_enabled=val=="on"; enabled=u.clock_enabled; await s.commit()
    if enabled:
        start_clock(c.from_user.id); await update_clock_once(c.from_user.id); await c.answer("⏰ ساعت زنده روشن شد")
    else: stop_clock(c.from_user.id); await c.answer("⏰ ساعت خاموش شد")
@dp.callback_query(F.data.startswith("clocktarget:"))
async def target_cb(c:CallbackQuery):
    target=c.data.split(":",1)[1]
    async with Session() as s:u=await get_user(s,c.from_user.id); u.clock_target=target; await s.commit()
    await c.answer("محل نمایش ساعت ذخیره شد")
@dp.callback_query(F.data.startswith("tz:"))
async def tz_cb(c:CallbackQuery):
    tz=c.data.split(":",1)[1]
    async with Session() as s:u=await get_user(s,c.from_user.id); u.timezone=tz; await s.commit()
    await c.answer("منطقه زمانی ذخیره شد")
    if u.clock_enabled: await update_clock_once(c.from_user.id)

@dp.callback_query(F.data=="users_menu")
async def users_menu(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID:
        await c.answer("این بخش فقط برای مدیر است",show_alert=True); return
    async with Session() as s:
        total=(await s.execute(select(func.count()).select_from(User))).scalar() or 0
        pending=(await s.execute(select(func.count()).select_from(User).where(User.approved==False,User.banned==False))).scalar() or 0
        approved=(await s.execute(select(func.count()).select_from(User).where(User.approved==True,User.banned==False))).scalar() or 0
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📥 درخواست‌های در انتظار",callback_data="pending_users")],[InlineKeyboardButton(text="📊 آمار کاربران",callback_data="user_stats")],[InlineKeyboardButton(text="📖 راهنمای این بخش",callback_data="help:users_menu")],[InlineKeyboardButton(text="⬅️ منوی اصلی",callback_data="main")]])
    await c.message.edit_text(f"👤 <b>مدیریت کاربران</b>\n\nکل: {total}\nتأییدشده: {approved}\nدر انتظار: {pending}\n\nهر کاربر تا قبل از تأیید مدیر کاملاً مسدود است.",reply_markup=kb); await c.answer()

@dp.callback_query(F.data=="user_stats")
async def user_stats(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID: return
    async with Session() as s:
        rows=(await s.execute(select(User).order_by(User.message_count.desc()).limit(10))).scalars().all()
    lines=["📊 <b>آمار کاربران</b>",""]
    for i,u in enumerate(rows,1): lines.append(f"{i}. <code>{u.id}</code> — {u.message_count or 0} پیام — {'فعال' if u.approved and not u.banned else 'غیرفعال'}")
    await c.message.edit_text("\n".join(lines),reply_markup=back()); await c.answer()

@dp.callback_query(F.data=="pending_users")
async def pending_users(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID: return
    async with Session() as s:
        rows=(await s.execute(select(User).where(User.approved==False,User.banned==False).order_by(User.created_at.desc()).limit(15))).scalars().all()
    if not rows:
        await c.message.edit_text("📥 <b>درخواست معلقی وجود ندارد.</b>",reply_markup=back()); await c.answer(); return
    buttons=[]
    for u in rows: buttons.append([InlineKeyboardButton(text=f"👤 {u.first_name or u.id}",callback_data=f"pending:{u.id}"),InlineKeyboardButton(text="✅",callback_data=f"approve:{u.id}"),InlineKeyboardButton(text="❌",callback_data=f"reject:{u.id}")])
    buttons.append([InlineKeyboardButton(text="⬅️ برگشت",callback_data="users_menu")])
    await c.message.edit_text("📥 <b>درخواست‌های در انتظار</b>",reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)); await c.answer()

@dp.callback_query(F.data.startswith("pending:"))
async def pending_detail(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID: return
    uid=int(c.data.split(":",1)[1])
    async with Session() as s: u=await get_user(s,uid)
    if not u: await c.answer("کاربر پیدا نشد",show_alert=True); return
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ تأیید",callback_data=f"approve:{uid}"),InlineKeyboardButton(text="❌ رد",callback_data=f"reject:{uid}")],[InlineKeyboardButton(text="⬅️ درخواست‌ها",callback_data="pending_users")]])
    await c.message.edit_text(f"👤 <b>جزئیات درخواست</b>\n\nنام: {escape(u.first_name or '—')}\nیوزرنیم: @{escape(u.username or 'ندارد')}\nID: <code>{uid}</code>",reply_markup=kb); await c.answer()

def diamond_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 کاربران",callback_data="diamond_users"),InlineKeyboardButton(text="📊 آمار",callback_data="diamond_stats")],
        [InlineKeyboardButton(text="🧾 گردش حساب",callback_data="diamond_history")],
        [InlineKeyboardButton(text="➕ شارژ سریع",callback_data="diamond_quickadd"),InlineKeyboardButton(text="➖ کسر سریع",callback_data="diamond_quicksub")],
        [InlineKeyboardButton(text="📖 راهنمای مدیریت",callback_data="help:diamonds_admin")],
        [InlineKeyboardButton(text="⬅️ منو",callback_data="main")],
    ])

@dp.callback_query(F.data=="diamond_stats")
async def diamond_stats(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID: return
    async with Session() as s:
        total_users=await s.scalar(select(func.count(User.id)).where(User.id!=OWNER_ID)) or 0
        total_d=await s.scalar(select(func.coalesce(func.sum(User.diamonds),0)).where(User.id!=OWNER_ID)) or 0
        active=await s.scalar(select(func.count(User.id)).where(User.self_enabled==True,User.id!=OWNER_ID)) or 0
        spent=await s.scalar(select(func.coalesce(func.sum(-DiamondTransaction.amount),0)).where(DiamondTransaction.kind.in_(["hourly","activation"]),DiamondTransaction.amount<0)) or 0
    body=f"📊 <b>مرکز آمار الماس</b>\n\n👥 کاربران: <b>{total_users:,}</b>\n💎 الماس در گردش کاربران: <b>{int(total_d):,}</b>\n🔐 سلف فعال: <b>{active:,}</b>\n📉 مصرف ثبت‌شده سلف: <b>{int(spent):,}</b>\n👑 موجودی مدیر: <b>∞</b>"
    await c.message.edit_text(body,reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🧾 گردش حساب",callback_data="diamond_history")],[InlineKeyboardButton(text="⬅️ مدیریت الماس",callback_data="diamonds")]])); await c.answer()

@dp.callback_query(F.data=="diamond_history")
async def diamond_history(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID: return
    async with Session() as s:
        rows=(await s.execute(select(DiamondTransaction).order_by(DiamondTransaction.created_at.desc()).limit(20))).scalars().all()
    if not rows:
        body="🧾 <b>گردش حساب</b>\n\nهنوز تراکنشی ثبت نشده است."
    else:
        lines=["🧾 <b>۲۰ تراکنش اخیر</b>",""]
        for r in rows:
            sign="+" if r.amount>=0 else ""
            lines.append(f"<code>{r.user_id}</code>  <b>{sign}{r.amount:,}</b> 💎  {escape(r.kind)}")
        body="\n".join(lines)
    await c.message.edit_text(body,reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ مدیریت الماس",callback_data="diamonds")]])); await c.answer()

@dp.callback_query(F.data=="diamond_quickadd")
async def diamond_quickadd(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID:return
    await c.message.edit_text("➕ <b>شارژ سریع</b>\n\nبرای امنیت، عملیات مالی از طریق دستور فارسی انجام می‌شود.\n\n<code>/الماس 123456789 100</code>\n\nیا برای کم‌کردن:\n<code>/الماس 123456789 -100</code>",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👥 انتخاب کاربر",callback_data="diamond_users")],[InlineKeyboardButton(text="⬅️ مدیریت الماس",callback_data="diamonds")]])); await c.answer()

@dp.callback_query(F.data=="diamond_quicksub")
async def diamond_quicksub(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID:return
    await c.message.edit_text("➖ <b>کسر سریع</b>\n\nنمونه:\n<code>/الماس 123456789 -100</code>\n\nموجودی هیچ کاربری منفی نمی‌شود و همه تغییرات در گردش حساب ثبت می‌شوند.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👥 انتخاب کاربر",callback_data="diamond_users")],[InlineKeyboardButton(text="⬅️ مدیریت الماس",callback_data="diamonds")]])); await c.answer()

@dp.callback_query(F.data=="diamonds")
async def diamonds_page(c:CallbackQuery):
    uid=c.from_user.id
    if uid==OWNER_ID:
        async with Session() as s:
            users=await s.scalar(select(func.count(User.id)).where(User.id!=OWNER_ID)) or 0
            total=await s.scalar(select(func.coalesce(func.sum(User.diamonds),0)).where(User.id!=OWNER_ID)) or 0
            active=await s.scalar(select(func.count(User.id)).where(User.self_enabled==True,User.id!=OWNER_ID)) or 0
        body=(f"💎 <b>مرکز مدیریت الماس</b>\n\n"
              f"👑 موجودی مدیر: <b>∞</b>\n"
              f"👥 کاربران: <b>{int(users):,}</b>\n"
              f"💎 الماس در گردش: <b>{int(total):,}</b>\n"
              f"🔐 سلف فعال: <b>{int(active):,}</b>\n\n"
              "از اینجا موجودی کاربران، گردش مالی، آمار و عملیات شارژ/کسر را کنترل کن.")
        await c.message.edit_text(body,reply_markup=diamond_admin_kb())
    else:
        async with Session() as s:u=await get_user(s,uid)
        bal=u.diamonds if u else 0
        expiry=(u.self_expires_at.astimezone(timezone.utc).strftime("%Y/%m/%d %H:%M UTC") if u and u.self_expires_at else "فعال نیست")
        body=(f"💎 <b>کیف پول الماس من</b>\n\n"
              f"💎 موجودی: <b>{bal:,}</b>\n"
              f"🔐 سلف: <b>{'فعال' if u and u.self_enabled else 'خاموش'}</b>\n"
              f"📅 اعتبار: <b>{expiry}</b>\n\n"
              f"⚡ شروع سلف: <b>{SELF_ACTIVATION_COST} الماس</b>\n"
              f"📆 دوره: <b>۳۰ روز</b>\n"
              f"💰 هر ۱۰۰ الماس: <b>۱۰٬۰۰۰ تومان</b>\n\n"
              f"🛒 برای خرید الماس به <b>{DIAMOND_ADMIN_USERNAME}</b> پیام بده.")
        kb=[[InlineKeyboardButton(text="🛒 خرید الماس",url="https://t.me/jokm7")],[InlineKeyboardButton(text="📖 راهنمای الماس",callback_data="help:diamonds")],[InlineKeyboardButton(text="⬅️ منو",callback_data="main")]]
        await c.message.edit_text(body,reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()

@dp.callback_query(F.data=="diamond_users")
async def diamond_users(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID: return
    async with Session() as s: rows=(await s.execute(select(User).where(User.id!=OWNER_ID).order_by(User.diamonds.desc()).limit(30))).scalars().all()
    if not rows:
        await c.message.edit_text("👥 <b>کاربر الماسی وجود ندارد.</b>",reply_markup=diamond_admin_kb()); await c.answer(); return
    buttons=[]
    for u in rows:
        name=(u.first_name or u.username or str(u.id))[:18]
        buttons.append([InlineKeyboardButton(text=f"💎 {name} | {u.diamonds or 0}",callback_data=f"diamond_user:{u.id}")])
    buttons.append([InlineKeyboardButton(text="⬅️ مدیریت الماس",callback_data="diamonds")])
    await c.message.edit_text("👥 <b>کاربران الماسی</b>\n\nیک کاربر را برای مدیریت انتخاب کن:",reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)); await c.answer()

@dp.callback_query(F.data.startswith("diamond_user:"))
async def diamond_user_detail(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID:return
    uid=int(c.data.split(":",1)[1])
    async with Session() as s:u=await get_user(s,uid)
    if not u: await c.answer("کاربر پیدا نشد",show_alert=True); return
    body=(f"💎 <b>مدیریت کیف پول</b>\n\n👤 {escape(u.first_name or '—')}\n"
          f"🆔 <code>{u.id}</code>\n🔗 @{escape(u.username or 'ندارد')}\n"
          f"💎 موجودی: <b>{u.diamonds or 0:,}</b>\n"
          f"🔐 سلف: <b>{'فعال' if u.self_enabled else 'خاموش'}</b>")
    kb=[[InlineKeyboardButton(text="➕ ۱۰۰",callback_data=f"diamond_adj:{uid}:100"),InlineKeyboardButton(text="➕ ۱۰۰۰",callback_data=f"diamond_adj:{uid}:1000")],
        [InlineKeyboardButton(text="➖ ۱۰۰",callback_data=f"diamond_adj:{uid}:-100"),InlineKeyboardButton(text="➖ ۱۰۰۰",callback_data=f"diamond_adj:{uid}:-1000")],
        [InlineKeyboardButton(text="📜 گردش این کاربر",callback_data=f"diamond_user_history:{uid}")],
        [InlineKeyboardButton(text="⬅️ کاربران",callback_data="diamond_users")]]
    await c.message.edit_text(body,reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)); await c.answer()

@dp.callback_query(F.data.startswith("diamond_adj:"))
async def diamond_adjust(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID:return
    _,uid_s,amount_s=c.data.split(":")
    uid=int(uid_s); amount=int(amount_s)
    async with Session() as s:
        ok=await change_diamonds(s,uid,OWNER_ID,amount,"admin_add" if amount>0 else "admin_sub", "تغییر از پنل مدیر")
        u=await get_user(s,uid)
        if ok is None: await c.answer("کاربر پیدا نشد",show_alert=True); return
        if ok is False: await c.answer("موجودی کافی نیست",show_alert=True); return
        await s.commit(); bal=u.diamonds or 0
    try: await c.bot.send_message(uid,f"💎 موجودی الماس شما توسط مدیر {'+' if amount>0 else ''}{amount:,} تغییر کرد.\nموجودی جدید: <b>{bal:,}</b>")
    except Exception: pass
    await c.answer(f"موجودی به {bal:,} رسید",show_alert=True)
    await diamond_user_detail(c)

@dp.callback_query(F.data.startswith("diamond_user_history:"))
async def diamond_user_history(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID:return
    uid=int(c.data.split(":",1)[1])
    async with Session() as s: rows=(await s.execute(select(DiamondTransaction).where(DiamondTransaction.user_id==uid).order_by(DiamondTransaction.created_at.desc()).limit(15))).scalars().all()
    lines=[f"🧾 <b>گردش الماس {uid}</b>",""]
    lines += [f"{'+' if r.amount>=0 else ''}{r.amount:,} 💎 — {escape(r.kind)}" for r in rows]
    await c.message.edit_text("\n".join(lines) if rows else lines[0]+"\n\nتراکنشی ثبت نشده.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ کاربر",callback_data=f"diamond_user:{uid}")]])); await c.answer()

@dp.callback_query(F.data=="self_users")
async def self_users(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID: await c.answer("این بخش فقط برای مدیر است",show_alert=True); return
    async with Session() as s: rows=(await s.execute(select(User).where(User.self_enabled==True).order_by(User.last_seen.desc()).limit(30))).scalars().all()
    if not rows: text="👥 <b>سلف فعال</b>\n\nهیچ کاربری در حال حاضر سلف فعال ندارد."; kb=[[InlineKeyboardButton(text="⬅️ منو",callback_data="main")]]
    else:
        text="👥 <b>کاربران دارای سلف فعال</b>\n\nبرای حذف نشست، روی کاربر بزن:"; kb=[[InlineKeyboardButton(text=f"👤 {u.first_name or u.id} | {u.id}",callback_data=f"self_revoke:{u.id}")] for u in rows]; kb += [[InlineKeyboardButton(text="📖 راهنمای این بخش",callback_data="help:self_users"),InlineKeyboardButton(text="⬅️ منو",callback_data="main")]]
    await c.message.edit_text(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)); await c.answer()

@dp.callback_query(F.data.startswith("self_revoke:"))
async def self_revoke(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID: return
    uid=int(c.data.split(":",1)[1]); stop_clock(uid)
    async with Session() as s:
        u=await get_user(s,uid)
        if not u: await c.answer("کاربر پیدا نشد",show_alert=True); return
        u.self_enabled=False; u.self_session=None; u.phone_masked=None; u.clock_enabled=False; u.self_expires_at=None; u.diamond_billing_remainder=0.0; u.diamond_last_billed_at=None; await s.commit()
    stop_billing(uid)
    try: await c.bot.send_message(uid,"🚫 مدیر نشست سلف شما را حذف کرد. برای ورود دوباره باید طبق روند تأیید و پرداخت الماس اقدام کنید.")
    except Exception: pass
    await c.answer("نشست سلف حذف شد",show_alert=True); await self_users(c)

@dp.callback_query(F.data=="profile")
async def profile_page(c:CallbackQuery):
    if not await allowed(c.from_user.id): await c.answer("دسترسی ندارید",show_alert=True); return
    text=await profile_text(c.from_user.id)
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✏️ تغییر نام",callback_data="edit_name"),InlineKeyboardButton(text="📝 تغییر بیو",callback_data="edit_bio")],[InlineKeyboardButton(text="🔗 تغییر یوزرنیم",callback_data="edit_username")],[InlineKeyboardButton(text="🔄 بروزرسانی",callback_data="profile"),InlineKeyboardButton(text="⬅️ منو",callback_data="main")],[InlineKeyboardButton(text="📖 راهنمای این بخش",callback_data="help:profile")]])
    await c.message.edit_text(text+"\n\n<b>تغییر سریع:</b> از دستورهای /setname، /setbio و /setusername هم می‌توانی استفاده کنی.",reply_markup=kb); await c.answer()

@dp.callback_query(F.data=="text_style")
async def text_style_page(c:CallbackQuery):
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="𝐁 ضخیم",callback_data="style:bold"),InlineKeyboardButton(text="𝙼 تک‌عرض",callback_data="style:mono")],[InlineKeyboardButton(text="𝘐 کج",callback_data="style:italic"),InlineKeyboardButton(text="S̶ خط‌خورده",callback_data="style:strike")],[InlineKeyboardButton(text="🔐 رمزی",callback_data="secret")],[InlineKeyboardButton(text="⬅️ منو",callback_data="main")],[InlineKeyboardButton(text="📖 راهنمای این بخش",callback_data="help:text_style")]])
    await c.message.edit_text("✍️ <b>استایل متن</b>\n\nبرای تست سریع، دستورهای /bold، /codeText و /secret را استفاده کن.\nاین بخش عمداً متن را بدون تغییر خطرناک روی اکانت ارسال نمی‌کند.",reply_markup=kb); await c.answer()

@dp.callback_query(F.data=="tools")
async def tools_page(c:CallbackQuery):
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏰ ساعت",callback_data="time"),InlineKeyboardButton(text="👤 پروفایل",callback_data="profile")],[InlineKeyboardButton(text="🎲 تاس",callback_data="tool:dice"),InlineKeyboardButton(text="🔮 فال",callback_data="tool:fortune")],[InlineKeyboardButton(text="📚 راهنما",callback_data="help_all"),InlineKeyboardButton(text="⬅️ منو",callback_data="main")],[InlineKeyboardButton(text="📖 راهنمای ابزارها",callback_data="help:tools")]])
    await c.message.edit_text("🛠 <b>ابزارهای سریع</b>\n\nابزارهای پرکاربرد را مستقیم از همین صفحه اجرا کن.",reply_markup=kb); await c.answer()

@dp.callback_query(F.data=="tool:dice")
async def tool_dice(c:CallbackQuery): await c.answer(f"🎲 {random.randint(1,6)}",show_alert=True)
@dp.callback_query(F.data=="tool:fortune")
async def tool_fortune(c:CallbackQuery): await c.answer("🔮 "+random.choice(["امروز برای شروع یک کار خوب مناسب است.","کمی صبر کن؛ نتیجه بهتر خواهد شد.","یک خبر خوب می‌تواند نزدیک باشد."]),show_alert=True)

@dp.callback_query(F.data.in_({"edit_name","edit_bio","edit_username"}))
async def edit_shortcuts(c:CallbackQuery):
    cmd={"edit_name":"/setname نام جدید","edit_bio":"/setbio متن بیو","edit_username":"/setusername username"}[c.data]
    await c.answer("دستور آماده شد",show_alert=False)
    await c.message.edit_text(f"✏️ <b>تغییر پروفایل</b>\n\nاین دستور را ارسال کن:\n<code>{cmd}</code>\n\nمثال را با مقدار دلخواه خودت جایگزین کن.",reply_markup=back())

@dp.callback_query(F.data.startswith("style:"))
async def style_action(c:CallbackQuery):
    style=c.data.split(":",1)[1]
    labels={"bold":"ضخیم","mono":"تک‌عرض","italic":"کج","strike":"خط‌خورده"}
    await c.answer(f"استایل «{labels.get(style,style)}» انتخاب شد؛ برای اعمال روی متن، از راهنمای همین بخش استفاده کن.",show_alert=True)

ANIMATION_STYLES = [
    ("🌊 نرم", "نرم"),
    ("⚡ سریع", "سریع"),
    ("💫 درخشان", "درخشان"),
    ("🌀 موجی", "موجی"),
    ("🎬 سینمایی", "سینمایی"),
    ("🎈 شاد", "شاد"),
    ("🧊 مینیمال", "مینیمال"),
]

def animation_kb():
    rows=[]
    rows.append([InlineKeyboardButton(text="🟢 انیمیشن روشن", callback_data="animation:toggle:on"), InlineKeyboardButton(text="🔴 انیمیشن خاموش", callback_data="animation:toggle:off")])
    rows += [[InlineKeyboardButton(text=a, callback_data=f"animation:style:{b}")] for a,b in ANIMATION_STYLES]
    rows.append([InlineKeyboardButton(text="📖 راهنمای این بخش", callback_data="help:animation")])
    rows.append([InlineKeyboardButton(text="⬅️ منوی اصلی", callback_data="main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.callback_query(F.data=="animation")
async def animation_page(c:CallbackQuery):
    if not await allowed(c.from_user.id): await c.answer("ابتدا دسترسی خود را دریافت کن",show_alert=True); return
    async with Session() as s:
        u=await get_user(s,c.from_user.id)
        enabled = bool(u.animation_enabled) if u else True
        style = u.animation_style if u else "نرم"
    status="روشن ✅" if enabled else "خاموش ❌"
    await c.message.edit_text(f"✨ <b>مرکز انیمیشن</b>\n\nوضعیت: <b>{status}</b>\nسبک فعلی: <b>{escape(style)}</b>\n\nاز اینجا سبک نمایش پنل را انتخاب کن. هر سبک را بزنی همان لحظه ذخیره می‌شود.", reply_markup=animation_kb()); await c.answer()

@dp.callback_query(F.data.startswith("animation:toggle:"))
async def animation_toggle(c:CallbackQuery):
    if not await allowed(c.from_user.id): return
    value=c.data.rsplit(":",1)[1]=="on"
    async with Session() as s:
        u=await get_user(s,c.from_user.id)
        if u: u.animation_enabled=value; await s.commit()
    await c.answer("✨ انیمیشن روشن شد" if value else "🛑 انیمیشن خاموش شد", show_alert=True)
    await animation_page(c)

@dp.callback_query(F.data.startswith("animation:style:"))
async def animation_style(c:CallbackQuery):
    if not await allowed(c.from_user.id): return
    style=c.data.split(":",2)[2]
    async with Session() as s:
        u=await get_user(s,c.from_user.id)
        if u: u.animation_style=style; u.animation_enabled=True; await s.commit()
    previews={"نرم":"✨ ... ✨","سریع":"⚡ آماده!","درخشان":"💫 ✨ 💫","موجی":"🌊 ~ ~ ~","سینمایی":"🎬 ▶️ ✨","شاد":"🎉 😄 🎈","مینیمال":"• • •"}
    await c.answer(f"سبک «{style}» انتخاب شد",show_alert=True)
    await c.message.edit_text(f"✨ <b>انیمیشن انتخاب شد</b>\n\nسبک: <b>{escape(style)}</b>\nپیش‌نمایش: {previews.get(style,'✨')}\n\nاین تنظیم برای پنل شما ذخیره شد.", reply_markup=animation_kb())

@dp.callback_query(F.data.in_(list(FEATURES.keys())))
async def feature(c:CallbackQuery):
    if not await allowed(c.from_user.id): await c.answer("ابتدا دسترسی را بگیر",show_alert=True); return
    title,body=FEATURES[c.data]
    rows=[[InlineKeyboardButton(text="📖 راهنمای این بخش",callback_data=f"help:{c.data}")]]
    if c.data=="media": rows.insert(0,[InlineKeyboardButton(text="🔒 روشن/خاموش",callback_data="toggle:media")])
    elif c.data=="games": rows.insert(0,[InlineKeyboardButton(text="🎲 تاس",callback_data="tool:dice"),InlineKeyboardButton(text="🔮 فال",callback_data="tool:fortune")])
    elif c.data=="time": rows.insert(0,[InlineKeyboardButton(text="⏰ باز کردن تنظیمات ساعت",callback_data="time")])
    rows.append([InlineKeyboardButton(text="⬅️ منوی اصلی",callback_data="main")])
    await c.message.edit_text(f"{title}\n\n{body}\n\n💡 <b>راهنما:</b> برای آموزش همین قسمت روی دکمه راهنما بزن.",reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)); await c.answer()

@dp.callback_query(F.data.startswith("help:"))
async def section_help(c:CallbackQuery):
    key=c.data.split(":",1)[1]
    guides={
      "login_phone":"📱 <b>راهنمای شماره</b>\n\nبعد از تأیید مدیر، شماره اکانت تلگرام را خودت با کد کشور بفرست. بدون شماره هیچ کد ورود درخواست نمی‌شود.\n\nنمونه: <code>+989123456789</code>",
      "login_code":"🔢 <b>راهنمای کد ورود</b>\n\nکدی که تلگرام می‌فرستد را مستقیم و به‌صورت متن وارد کن. اعداد فارسی و انگلیسی هر دو پذیرفته می‌شوند. اگر چند کد دریافت کردی، فقط آخرین کد را وارد کن.",
      "password":"🔐 <b>راهنمای رمز دومرحله‌ای</b>\n\nاگر تأیید دومرحله‌ای فعال باشد، بعد از کد صفحه رمز باز می‌شود. رمز را فقط داخل خود تلگرام/بات وارد کن و برای هیچ‌کس ارسال نکن.",
      "admin_approval":"👑 <b>راهنمای تأیید مدیر</b>\n\nدرخواست ابتدا برای مدیر می‌رود. مدیر دسترسی را تأیید یا رد می‌کند. بعد از تأیید، شماره اجباری است و سپس مرحله کد ورود باز می‌شود.",
      "time":"⏰ <b>راهنمای زمان و پروفایل</b>\n\n۱) منطقه زمانی را انتخاب کن.\n۲) فونت ساعت را انتخاب کن.\n۳) مشخص کن ساعت در نام، بیو یا هر دو نمایش داده شود.\n۴) «ساعت روشن» را بزن.\n\nخاموش کردن از همین بخش انجام می‌شود.",
      "profile":"🖼 <b>راهنمای پروفایل</b>\n\nوضعیت حساب سلف، نام، بیو و نام کاربری را ببین. برای تغییر، از دکمه‌های همان صفحه استفاده کن یا دستور مربوط به همان گزینه را بفرست.",
      "text_style":"✍️ <b>راهنمای استایل متن</b>\n\nبرای متن ضخیم از <code>/bold متن</code>، برای متن تک‌عرض از <code>/codeText متن</code> و برای متن رمزی از <code>/secret متن</code> استفاده کن. راهنما فقط روش استفاده را توضیح می‌دهد و محدودیت‌های تلگرام را دور نمی‌زند.",
      "users_menu":"👤 <b>راهنمای کاربران</b>\n\nاین قسمت مخصوص مدیر است. درخواست‌های در انتظار، آمار کاربران و وضعیت دسترسی‌ها را ببین. از صفحه هر کاربر می‌توانی دسترسی او را مدیریت کنی.",
      "media":"🔒 <b>راهنمای قفل رسانه</b>\n\nبا دکمه «روشن/خاموش» وضعیت قفل را تغییر بده. هنگام روشن بودن، رسانه‌های مشمول تنظیمات مدیریت می‌شوند. برای جلوگیری از تغییر ناخواسته، وضعیت را قبل از استفاده بررسی کن.",
      "comments":"💬 <b>راهنمای کامنت</b>\n\nاین بخش برای تنظیم قابلیت‌های مربوط به کامنت است. وارد بخش شو، گزینه موردنظر را انتخاب کن و از «راهنمای این بخش» برای توضیح همان گزینه استفاده کن.",
      "public":"📌 <b>راهنمای عمومی</b>\n\nتنظیمات عمومی سلف را از این قسمت بررسی کن. هر گزینه را انتخاب کن تا توضیح کاربرد و روش استفاده‌اش نمایش داده شود.",
      "actions":"🎭 <b>راهنمای اکشن</b>\n\nابزارهای واکنش و عملیات سریع روی پیام‌ها اینجا معرفی می‌شوند. برای ریکت خودکار، روی پیام کاربر ریپلای کن و <code>/reaction ❤️</code> بفرست. برای حذف ریکت، روی پیام همان کاربر ریپلای کن و <code>/reaction</code> بفرست.",
      "games":"🎮 <b>راهنمای بازی‌ها</b>\n\n«تاس» یک نتیجه تصادفی می‌دهد. «فال» یک پیام سرگرمی تصادفی نمایش می‌دهد. این قابلیت‌ها صرفاً سرگرمی هستند.",
      "translate":"🌐 <b>راهنمای ترجمه</b>\n\nمتن را بعد از دستور ترجمه وارد کن؛ نمونه: <code>/translate hello</code>. برای ترجمه حرفه‌ای و زنده به سرویس ترجمه نیاز است.",
      "google":"🔎 <b>راهنمای گوگل</b>\n\nعبارت جست‌وجو را بعد از دستور بفرست؛ نمونه: <code>/google Telegram</code>. بات لینک جست‌وجوی آماده می‌سازد.",
      "info":"ℹ️ <b>راهنمای اطلاعات</b>\n\nبرای دیدن وضعیت سلف از <code>/selfstatus</code> و برای اطلاعات پروفایل از <code>/profile</code> استفاده کن.",
      "messages":"✉️ <b>راهنمای مدیریت پیام</b>\n\nبرای پاسخ خودکار بنویس <code>/autoreply متن</code>. برای خاموش کردن، <code>/autoreply</code> را بدون متن بفرست. برای ریکت خودکار، روی پیام کاربر ریپلای کن و <code>/reaction ❤️</code> بفرست.",
      "reaction":"👍 <b>راهنمای ریکت</b>\n\n<b>تنظیم ریکت:</b> روی پیام کاربر ریپلای کن و <code>/reaction ❤️</code> را بفرست.\n\n<b>حذف ریکت:</b> روی پیام همان کاربر ریپلای کن و <code>/reaction</code> را بفرست.\n\n📍 در گروه و پی‌وی، هر جا حساب سلف اجازه واکنش داشته باشد، قابل استفاده است.\n💡 می‌توانی به‌جای ❤️ یک ایموجی مجاز دیگر بگذاری.\n\nنکته: دستور <code>/reaction</code> نام فنی تلگرام است؛ بقیه متن‌ها و آموزش‌های پنل کاملاً فارسی هستند.",
      "enemies":"🚫 <b>راهنمای مسدودی‌ها</b>\n\nمدیر می‌تواند کاربر را با ID عددی مسدود یا رفع مسدودی کند. نمونه: <code>/ban 123456789</code> و <code>/unban 123456789</code>.",
      "edit":"✏️ <b>راهنمای تغییر پروفایل</b>\n\nنام: <code>/setname نام جدید</code>\nبیو: <code>/setbio متن بیو</code>\nنام کاربری: <code>/setusername username</code>\n\nمقدار دلخواه را جایگزین نمونه کن.",
      "filter":"🔎 <b>راهنمای فیلتر کلمات</b>\n\nکلمات موردنظر را در تنظیمات فیلتر وارد کن. پیام‌هایی که مشمول قوانین فیلتر باشند طبق تنظیم فعال مدیریت می‌شوند. قبل از فعال‌سازی، قوانین خودت را بررسی کن.",
      "namelock":"🛡 <b>راهنمای حفاظت اسم</b>\n\nاگر ساعت را روی نام گذاشتی و نمی‌خواهی نام پایه دائماً تغییر کند، هدف ساعت را روی بیو قرار بده. تنظیمات نام را از بخش پروفایل کنترل کن.",
      "smart":"🤖 <b>راهنمای هوشمند</b>\n\nحالت هوشمند برای پاسخ‌های خودکار ساده است. پاسخ‌ها را کنترل کن و اگر سرویس هوش مصنوعی جداگانه‌ای نداری، انتظار پاسخ مدل خارجی نداشته باش.",
      "report":"📣 <b>راهنمای گزارش</b>\n\nمشکل را با <code>/report توضیح مشکل</code> برای مدیر ارسال کن. اطلاعات غیرضروری یا رمز ورود را داخل گزارش نفرست.",
      "tools":"🛠 <b>راهنمای ابزارها</b>\n\nاز این بخش به ساعت، پروفایل، تاس و فال دسترسی سریع داری. هر ابزار صفحه یا دکمه راهنمای مخصوص خودش را دارد.",
      "secretary":"🧠 <b>راهنمای منشی</b>\n\nبرای پاسخ خودکار از <code>/autoreply متن</code> استفاده کن. برای خاموش کردن، همان دستور را بدون متن بفرست.",
      "broadcast":"📢 <b>راهنمای اطلاعیه</b>\n\nمدیر می‌تواند روی یک پیام ریپلای کند و دستور <code>/broadcast</code> را بفرستد تا اطلاعیه برای کاربران تأییدشده ارسال شود. ارسال محدود و کنترل‌شده است.",
      "fortune":"🔮 <b>راهنمای فال</b>\n\nاز دکمه فال یا دستور مربوط به آن استفاده کن. نتیجه کاملاً سرگرمی و تصادفی است.",
      "secret":"🔐 <b>راهنمای متن رمزی</b>\n\nبرای تبدیل متن از <code>/secret متن</code> و برای بازکردن متن تولیدشده از <code>/unsecret کد</code> استفاده کن. برای اطلاعات حساس مناسب نیست.",
      "widgets":"🧩 <b>راهنمای ابزارک‌ها</b>\n\nابزارک‌های سریع مثل ساعت، پروفایل و تنظیمات پرکاربرد را از این بخش اجرا کن.",
      "backup":"📦 <b>راهنمای بکاپ</b>\n\nتنظیمات در دیتابیس نگهداری می‌شوند و نشست سلف رمزنگاری می‌شود. فایل تنظیمات محرمانه را در GitHub منتشر نکن.",
      "buttons":"🎨 <b>راهنمای دکمه‌ها</b>\n\nدکمه‌ها برای دسترسی سریع به قابلیت‌ها هستند. هر صفحه یک دکمه «📖 راهنمای این بخش» دارد؛ با زدن آن، آموزش همان قابلیت نمایش داده می‌شود.",
      "diamonds":f"💎 <b>راهنمای الماس</b>\n\n🎁 بعد از اولین تأیید مدیر، {WELCOME_DIAMONDS} الماس هدیه می‌گیری.\n🔑 هنگام شروع موفق سلف، {SELF_ACTIVATION_COST} الماس کم می‌شود.\n📅 اعتبار دوره ۳۰ روز است و مصرف دوره به‌صورت ساعتی انجام می‌شود؛ موجودی یکجا صفر نمی‌شود.\n💰 هر ۱۰۰ الماس = ۱۰٬۰۰۰ تومان.\n📩 خرید الماس: {DIAMOND_ADMIN_USERNAME}\n👑 مدیر موجودی نامحدود دارد.",
      "diamonds_admin":"👑 <b>راهنمای مدیریت حرفه‌ای الماس</b>\n\nمدیر می‌تواند موجودی کاربران را افزایش/کاهش دهد، گردش حساب را ببیند، کاربران را جست‌وجو کند و سابقه تراکنش‌ها را بررسی کند. هیچ موجودی کاربر نباید منفی شود و تغییرات باید قابل پیگیری باشند.",
      "diamond_transfer":"📤 <b>راهنمای انتقال الماس</b>\n\nمقصد را با ID عددی یا @username مشخص کن. نمونه: <code>/انتقال_الماس 123456789 100</code> یا <code>/انتقال_الماس @user 100</code>. قبل از انتقال، مقصد و مقدار را دقیق بررسی کن.",
      "self_users":"👥 <b>راهنمای سلف‌های فعال</b>\n\nاین فهرست کاربرانی است که ورود سلفشان موفق شده و نشستشان ذخیره شده است. مدیر می‌تواند کاربر را انتخاب و نشست سلف او را حذف کند. پس از حذف، برای ورود دوباره باید روند ورود را از ابتدا طی کند.",
      "help_all":"📚 <b>راهنمای کامل</b>\n\nهر بخش پنل یک دکمه «📖 راهنمای این بخش» دارد. وارد هر قابلیت شو، راهنمای همان صفحه را بزن و دستور/مراحل استفاده را ببین. برای ورود سلف: تأیید مدیر ← شماره ← کد متنی ← در صورت نیاز رمز دومرحله‌ای ← فعال شدن سلف."
    }
    body=guides.get(key,"📚 <b>راهنمای این بخش</b>\n\nاین قسمت از پنل برای مدیریت قابلیت مربوط به خودش است. گزینه‌های همین صفحه را یکی‌یکی امتحان کن؛ تنظیمات قابل تغییر از همان‌جا ذخیره می‌شوند.")
    await c.message.edit_text(body,reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ برگشت",callback_data=key if key in FEATURES else "main")],[InlineKeyboardButton(text="🏠 منوی اصلی",callback_data="main")]])); await c.answer()

@dp.callback_query(F.data.startswith("toggle:"))
async def toggle(c:CallbackQuery):
    field=c.data.split(":",1)[1]
    if field not in {"media"}: return
    async with Session() as s:
        u=await get_user(s,c.from_user.id); u.media_lock=not u.media_lock; v=u.media_lock; await s.commit()
    await c.answer("قفل رسانه "+("روشن شد 🔒" if v else "خاموش شد 🔓"))

@dp.callback_query(F.data.startswith("approve:"))
async def approve(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID:return
    uid=int(c.data.split(":")[1])
    async with Session() as s:
        u=await get_user(s,uid)
        if not u: await c.answer("کاربر پیدا نشد.",show_alert=True); return
        was_welcomed=bool(u.welcome_diamond_granted)
        u.approved=True; u.banned=False; u.access_requested=False; u.pending_phone=None
        if not was_welcomed:
            u.diamonds=(u.diamonds or 0) + WELCOME_DIAMONDS
            u.welcome_diamond_granted=True
            await log_diamond(s, uid, OWNER_ID, WELCOME_DIAMONDS, "welcome", "هدیه تأیید مدیر")
        new_balance=u.diamonds or 0
        await s.commit()
    await c.answer("دسترسی تأیید شد")
    try:
        await c.message.edit_text(c.message.text + "\n\n✅ <b>تأیید شد.</b> حالا شماره را از کاربر بگیر؛ کد هنوز ارسال نشده است.")
    except Exception: pass
    bonus_text = f"\n\n🎁 <b>{WELCOME_DIAMONDS} الماس هدیه</b> برای اولین تأیید به حسابت اضافه شد.\n💎 موجودی فعلی: <b>{new_balance}</b>" if not was_welcomed else ""
    await c.bot.send_message(uid,"🎉 <b>دسترسی شما تأیید شد.</b>" + bonus_text + "\n\nحالا شماره اکانت تلگرامت اجباری است. شماره را همینجا به‌صورت متن بفرست.\n\nنمونه: <code>+989967066405</code>\n\nبه‌محض دریافت شماره معتبر، کد ورود به‌صورت خودکار ارسال می‌شود و هیچ دکمه‌ای برای ارسال شماره یا کد لازم نیست.")

@dp.callback_query(F.data.startswith("reject:"))
async def reject(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID:return
    uid=int(c.data.split(":")[1])
    async with Session() as s:u=await get_user(s,uid); u.approved=False; u.access_requested=False; u.pending_phone=None; await s.commit()
    await c.answer("رد شد"); await c.message.edit_reply_markup(reply_markup=None)

@dp.message(Command("ban"))
async def ban(m:Message):
    if m.from_user.id!=OWNER_ID:return
    p=(m.text or "").split(maxsplit=1)
    if len(p)!=2 or not p[1].isdigit(): await m.answer("مثال: /ban 123456789"); return
    async with Session() as s:u=await get_user(s,int(p[1]));
    if not u: await m.answer("کاربر پیدا نشد."); return
    target_id=int(p[1])
    async with Session() as s:
        u=await get_user(s,target_id)
        u.banned=True; u.approved=False; u.self_enabled=False; u.self_session=None; u.phone_masked=None; u.clock_enabled=False; u.self_expires_at=None; u.diamond_billing_remainder=0.0; u.diamond_last_billed_at=None; await s.commit()
    stop_clock(target_id); stop_billing(target_id); await m.answer("🚫 کاربر مسدود شد و نشست سلفش حذف شد.")
@dp.message(Command("unban"))
async def unban(m:Message):
    if m.from_user.id!=OWNER_ID:return
    p=(m.text or "").split(maxsplit=1)
    if len(p)!=2 or not p[1].isdigit(): await m.answer("مثال: /unban 123456789"); return
    async with Session() as s:u=await get_user(s,int(p[1]));
    if not u: await m.answer("کاربر پیدا نشد."); return
    async with Session() as s:u=await get_user(s,int(p[1])); u.banned=False;await s.commit()
    await m.answer("✅ رفع مسدودی شد.")

@dp.message(F.text.startswith("/الماس"))
async def fa_diamonds(m:Message):
    p=(m.text or "").split()
    if len(p)==1:
        async with Session() as s:u=await get_user(s,m.from_user.id)
        await m.answer(f"💎 موجودی شما: <b>{'∞' if m.from_user.id==OWNER_ID else (u.diamonds if u else 0)}</b>\n\n🎁 هدیه اولین تأیید: {WELCOME_DIAMONDS} الماس\n🔑 هزینه شروع سلف: {SELF_ACTIVATION_COST} الماس\n📅 اعتبار: ۳۰ روز\n📉 مصرف دوره: تا {SELF_MONTH_DIAMONDS:,} الماس به‌صورت ساعتی\n💰 هر ۱۰۰ الماس: ۱۰٬۰۰۰ تومان\n📩 خرید الماس: {DIAMOND_ADMIN_USERNAME}")
        return
    if m.from_user.id!=OWNER_ID: await m.answer("⛔ فقط مدیر می‌تواند موجودی کاربران را مدیریت کند."); return
    if len(p)!=3 or not p[1].lstrip("@").isdigit() or not p[2].lstrip("-").isdigit():
        await m.answer("نمونه: <code>/الماس 123456789 10</code>\nعدد منفی برای کم‌کردن الماس است.\n\n💰 هر ۱۰۰ الماس = ۱۰٬۰۰۰ تومان\n📩 خرید: <b>@jokm7</b>"); return
    uid=int(p[1].lstrip("@")); amount=int(p[2])
    async with Session() as s:
        u=await get_user(s,uid)
        if not u: await m.answer("❌ کاربر پیدا نشد."); return
        ok=await change_diamonds(s,uid,OWNER_ID,amount,"admin_add" if amount>0 else "admin_sub", "دستور مدیر");
        if ok is False: await m.answer("❌ موجودی کافی نیست."); return
        await s.commit(); bal=u.diamonds
    await m.answer(f"💎 موجودی کاربر <code>{uid}</code> به <b>{bal}</b> رسید.")

@dp.message(F.text.startswith("/انتقال_الماس"))
async def fa_transfer_diamonds(m:Message):
    p=(m.text or "").split()
    if len(p)!=3 or not p[2].isdigit() or int(p[2])<=0: await m.answer("نمونه: <code>/انتقال_الماس @username 5</code>"); return
    amount=int(p[2]); sender_id=m.from_user.id
    async with Session() as s:
        sender=await get_user(s,sender_id); target=await resolve_user(s,p[1])
        if not sender or not target: await m.answer("❌ فرستنده یا گیرنده پیدا نشد."); return
        if target.id==sender_id: await m.answer("❌ انتقال به خودت امکان‌پذیر نیست."); return
        if sender_id!=OWNER_ID and (sender.diamonds or 0)<amount: await m.answer("💎 موجودی کافی نیست."); return
        if sender_id!=OWNER_ID:
            sender.diamonds-=amount
            await log_diamond(s,sender_id,sender_id,-amount,"transfer_out",f"انتقال به {target.id}")
        target.diamonds=(target.diamonds or 0)+amount
        await log_diamond(s,target.id,sender_id,amount,"transfer_in",f"دریافت از {sender_id}")
        await s.commit(); target_name=escape(target.first_name or str(target.id))
    await m.answer(f"✅ {amount} 💎 به {target_name} منتقل شد.")
    try: await m.bot.send_message(target.id,f"💎 <b>{amount} الماس</b> از طرف یک کاربر برایت انتقال داده شد.")
    except Exception: pass

@dp.message(Command("google"))
async def google(m:Message):
    p=(m.text or "").split(maxsplit=1)
    await m.answer(f"🔎 <a href=\"https://www.google.com/search?q={quote_plus(p[1])}\">جست‌وجوی گوگل</a>" if len(p)==2 else "مثال: /google عبارت")
@dp.message(Command("translate"))
async def translate(m:Message):
    p=(m.text or "").split(maxsplit=1)
    await m.answer(f"🌐 <a href=\"https://translate.google.com/?sl=auto&tl=fa&text={quote_plus(p[1])}\">بازکردن ترجمه</a>" if len(p)==2 else "مثال: /translate متن")
@dp.message(Command("dice"))
async def dice(m:Message): await m.answer(f"🎲 نتیجه: <b>{random.randint(1,6)}</b>")
@dp.message(Command("fortune"))
async def fortune(m:Message): await m.answer("🔮 "+random.choice(["امروز برای شروع یک کار خوب مناسب است.","کمی صبر کن؛ نتیجه بهتر خواهد شد.","یک خبر خوب می‌تواند نزدیک باشد.","روی چیزی که کنترلش می‌کنی تمرکز کن."]))
@dp.message(Command("bold"))
async def bold(m:Message):
    p=(m.text or "").split(maxsplit=1); await m.answer(f"<b>{escape(p[1])}</b>" if len(p)==2 else "مثال: /bold سلام")
@dp.message(Command("codeText"))
async def codetext(m:Message):
    p=(m.text or "").split(maxsplit=1); await m.answer(f"<code>{escape(p[1])}</code>" if len(p)==2 else "مثال: /codeText hello")
@dp.message(Command("secret"))
async def secret(m:Message):
    p=(m.text or "").split(maxsplit=1); await m.answer("🔐 <code>"+base64.b64encode(p[1].encode()).decode()+"</code>" if len(p)==2 else "مثال: /secret متن")
@dp.message(Command("unsecret"))
async def unsecret(m:Message):
    p=(m.text or "").split(maxsplit=1)
    if len(p)!=2: await m.answer("مثال: /unsecret کد"); return
    try: await m.answer("🔓 "+escape(base64.b64decode(p[1]).decode()))
    except Exception: await m.answer("❌ کد معتبر نیست.")
@dp.message(Command("report"))
async def report(m:Message):
    p=(m.text or "").split(maxsplit=1)
    if len(p)!=2: await m.answer("مثال: /report مشکل"); return
    await m.bot.send_message(OWNER_ID,f"📣 <b>گزارش</b>\n👤 <code>{m.from_user.id}</code>\n{escape(p[1])}"); await m.answer("✅ گزارش ارسال شد.")

@dp.message(Command("broadcast"))
async def broadcast(m:Message):
    if m.from_user.id!=OWNER_ID or not m.reply_to_message: await m.answer("📢 برای اطلاعیه، روی پیام ریپلای کن و /broadcast بزن."); return
    async with Session() as s: users=(await s.execute(select(User).where(User.approved==True,User.banned==False))).scalars().all()
    status=await m.answer(f"📢 ارسال کنترل‌شده برای {len(users)} کاربر…"); ok=bad=0
    for u in users:
        try: await m.bot.copy_message(u.id,m.chat.id,m.reply_to_message.message_id); ok+=1
        except Exception: bad+=1
        await asyncio.sleep(.08)
    await status.edit_text(f"✅ تمام شد\n📨 موفق: {ok}\n❌ ناموفق: {bad}")

@dp.message()
async def fallback(m:Message):
    uid=m.from_user.id
    if not await allowed(uid): return
    async with Session() as s:
        u=await get_user(s,uid)
        if u:
            u.username=m.from_user.username;u.first_name=m.from_user.first_name;u.last_seen=datetime.now(timezone.utc);u.message_count=(u.message_count or 0)+1
            reply=u.auto_reply; reaction=u.auto_reaction; media=u.media_lock
            await s.commit()
    if media and m.content_type in {"photo","video","document","audio","voice","animation","sticker"}:
        try: await m.delete()
        except Exception: pass
        return
    if reply and not (m.text or "").startswith("/"): await m.answer(reply)
    if reaction:
        try: await m.bot.set_message_reaction(m.chat.id,m.message_id,[ReactionTypeEmoji(emoji=reaction)])
        except Exception: pass

async def main():
    await ensure_schema()
    bot=Bot(TOKEN,default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    # ساعت‌های فعال بعد از Restart دوباره راه‌اندازی می‌شوند.
    async with Session() as s:
        enabled_ids=(await s.execute(select(User.id).where(User.self_enabled==True))).scalars().all()
        clock_ids=(await s.execute(select(User.id).where(User.self_enabled==True,User.clock_enabled==True))).scalars().all()
    for uid in enabled_ids: start_billing(uid)
    for uid in clock_ids: start_clock(uid)
    log.info("Bot started")
    try: await dp.start_polling(bot)
    finally:
        for t in list(CLOCK_TASKS.values()): t.cancel()
        for t in list(BILLING_TASKS.values()): t.cancel()
        await bot.session.close()

if __name__=="__main__": asyncio.run(main())
