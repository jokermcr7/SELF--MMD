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
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, RPCError, PhoneCodeInvalidError, PhoneCodeExpiredError, FloodWaitError
from telethon.tl.functions.account import UpdateProfileRequest, UpdateUsernameRequest
from telethon.tl.functions.contacts import BlockRequest, UnblockRequest

from aiogram import Bot, Dispatcher, F
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import ReactionTypeEmoji
from sqlalchemy import Boolean, DateTime, Integer, String, Text, Float, select, func, inspect, text as sql_text, UniqueConstraint
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
    auto_reply_delay: Mapped[int] = mapped_column(Integer, default=3)
    auto_reply_cooldown: Mapped[int] = mapped_column(Integer, default=60)
    auto_reply_scope: Mapped[str] = mapped_column(String(16), default="private")
    auto_reply_keywords: Mapped[str | None] = mapped_column(Text)
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
    clock_base_name: Mapped[str | None] = mapped_column(String(255))
    clock_base_bio: Mapped[str | None] = mapped_column(Text)
    smart_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    name_lock: Mapped[bool] = mapped_column(Boolean, default=False)
    locked_name: Mapped[str | None] = mapped_column(String(255))
    word_filter: Mapped[bool] = mapped_column(Boolean, default=False)
    word_filter_text: Mapped[str | None] = mapped_column(Text)
    media_lock: Mapped[bool] = mapped_column(Boolean, default=False)
    comments_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    spam_protection: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_seen: Mapped[bool] = mapped_column(Boolean, default=False)
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
    balance_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class ChatRule(Base):
    __tablename__ = "chat_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    mode: Mapped[str] = mapped_column(String(16), default="allow")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    __table_args__ = (UniqueConstraint("user_id", "chat_id", name="uq_chat_rule_user_chat"),)

class SenderRule(Base):
    __tablename__ = "sender_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    sender_id: Mapped[int] = mapped_column(Integer, index=True)
    mode: Mapped[str] = mapped_column(String(16), default="allow")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    __table_args__ = (UniqueConstraint("user_id", "sender_id", name="uq_sender_rule_user_sender"),)

class Reminder(Base):
    __tablename__ = "reminders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    chat_id: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    done: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class AdminSetting(Base):
    __tablename__ = "admin_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
dp = Dispatcher()
LOGIN_FLOWS: dict[int, dict] = {}
LOGIN_CANCEL_TEXT = "❌ لغو ورود"
CLOCK_TASKS: dict[int, asyncio.Task] = {}
BILLING_TASKS: dict[int, asyncio.Task] = {}
ADMIN_FLOWS: dict[int, dict] = {}
AUTOMATION_TASKS: dict[int, asyncio.Task] = {}
AUTOMATION_LAST_REPLY: dict[int, float] = {}
REMINDER_TASK: asyncio.Task | None = None
STYLE_FLOWS: dict[int, str] = {}
SPAM_FLOWS: dict[int, dict] = {}

# فونت‌های ساعت؛ همگی فقط تبدیل ظاهری اعداد هستند و به متن اصلی دست نمی‌زنند.
CLOCK_FONTS = {
    "classic": ("کلاسیک", "0123456789", "0123456789"),
    "bold": ("ضخیم", "0123456789", "𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗"),
    "double": ("دوبل", "0123456789", "𝟘𝟙𝟚𝟛𝟜𝟝𝟞𝟟𝟠𝟡"),
    "sans": ("سنسریف", "0123456789", "𝟢𝟣𝟤𝟥𝟦𝟧𝟨𝟩𝟪𝟫"),
    "sansbold": ("سنسریف ضخیم", "0123456789", "𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵"),
    "mono": ("تک‌عرض", "0123456789", "𝟶𝟷𝟸𝟹𝟺𝟻𝟼𝟽𝟾𝟿"),
    "full": ("فول‌ویدث", "0123456789", "０１２３４５６７８９"),
    "bubble": ("حبابی", "0123456789", "⓪①②③④⑤⑥⑦⑧⑨"),
    "blackcircle": ("دایره مشکی", "0123456789", "⓿❶❷❸❹❺❻❼❽❾"),
    "square": ("مربعی", "0123456789", "🄌➊➋➌➍➎➏➐➑➒"),
    "parenthesis": ("پرانتزدار", "0123456789", "⑽⑴⑵⑶⑷⑸⑹⑺⑻⑼"),
    "superscript": ("بالانویس", "0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹"),
    "subscript": ("زیرنویس", "0123456789", "₀₁₂₃₄₅₆₇₈₉"),
    "persian": ("فارسی", "0123456789", "۰۱۲۳۴۵۶۷۸۹"),
    "arabic": ("عربی", "0123456789", "٠١٢٣٤٥٦٧٨٩"),
    "devanagari": ("هندی", "0123456789", "०१२३४५६७८९"),
    "bengali": ("بنگالی", "0123456789", "০১২৩৪৫৬৭৮৯"),
    "thai": ("تایلندی", "0123456789", "๐๑๒๓๔๕๖๗๘๙"),
    "myanmar": ("میانماری", "0123456789", "၀၁၂၃၄၅၆၇၈၉"),
    "khmer": ("خمری", "0123456789", "០១២៣៤៥៦៧៨៩"),
}


def stylize_time(value: str, font: str) -> str:
    mapping = CLOCK_FONTS.get(font, CLOCK_FONTS["classic"])[2]
    return value.translate(str.maketrans("0123456789", mapping))

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔴💎 الماس", callback_data="diamonds")],
        [InlineKeyboardButton(text="⏰ زمان و پروفایل", callback_data="time")],
        [InlineKeyboardButton(text="✍️ استایل متن", callback_data="textstyle"), InlineKeyboardButton(text="📢 تگ همه", callback_data="tagall")],
        [InlineKeyboardButton(text="💣 اسپم محدود", callback_data="spam")],
        [InlineKeyboardButton(text="✖️ بستن پنل", callback_data="close")],
    ])

def login_stage_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ لغو", callback_data="login_cancel")]])

def phone_help_kb():
    # در نسخه ریست‌شده، ورود فقط دکمه لغو دارد و شماره به‌صورت متن دریافت می‌شود.
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ لغو ورود", callback_data="login_cancel")]])

def password_stage_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ لغو ورود",callback_data="login_cancel")]])

def back(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ منوی اصلی", callback_data="main")]])
def clock_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 تایم روشن", callback_data="clock:on"), InlineKeyboardButton(text="🔴 تایم خاموش", callback_data="clock:off")],
        [InlineKeyboardButton(text="🚩 پرچم خاموش", callback_data="clocktarget:none")],
        [InlineKeyboardButton(text="⏰ ساعت در پروفایل", callback_data="clocktarget:name"), InlineKeyboardButton(text="📝 ساعت در بیو", callback_data="clocktarget:bio")],
        [InlineKeyboardButton(text="⏰📝 ساعت در پروفایل + بیو", callback_data="clocktarget:both")],
        [InlineKeyboardButton(text="🌍 ساعت جهانی", callback_data="clocktz")],
        [InlineKeyboardButton(text="🔤 انتخاب فونت تایم", callback_data="clockfonts")],
        [InlineKeyboardButton(text="✏️ تنظیمات بیو", callback_data="clockbio")],
        [InlineKeyboardButton(text="📖 راهنما", callback_data="clockhelp")],
        [InlineKeyboardButton(text="⬅️ برگشت", callback_data="main")],
    ])

def fonts_kb(page=0):
    items=list(CLOCK_FONTS.items())
    per_page=10
    page=max(0,min(page,(len(items)-1)//per_page))
    chunk=items[page*per_page:(page+1)*per_page]
    rows=[[InlineKeyboardButton(text=f"{v[0]}  {stylize_time('12:34',k)}", callback_data=f"font:{k}")] for k,v in chunk]
    nav=[]
    if page>0: nav.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=f"clockfonts:{page-1}"))
    if (page+1)*per_page<len(items): nav.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=f"clockfonts:{page+1}"))
    if nav: rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ زمان و پروفایل", callback_data="time")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def feature_page(title, body, buttons=None):
    kb = buttons or []
    kb.append([InlineKeyboardButton(text="⬅️ منوی اصلی", callback_data="main")])
    return InlineKeyboardMarkup(inline_keyboard=kb), f"<b>{title}</b>\n\n{body}"

async def resolve_user(s, target):
    target = target.strip()
    if target.startswith("@"): target = target[1:]
    if target.isdigit(): return await get_user(s, int(target))
    return (await s.execute(select(User).where(func.lower(User.username) == target.lower()))).scalar_one_or_none()

DEFAULT_DIAMOND_RATE = SELF_MONTH_DIAMONDS/720.0 if "SELF_MONTH_DIAMONDS" in globals() else 1000/720.0
DIAMOND_ICON = "🔴💎"

DEFAULT_ADMIN_SETTINGS = {
    "diamond_rate_per_hour": str(DEFAULT_DIAMOND_RATE),
    "activation_cost": "1",
    "welcome_diamonds": "50",
    "max_self_hours": "720",
    "diamond_pack_size": "100",
    "diamond_pack_price": "10000",
}

async def get_setting(key: str, default: str | None = None) -> str | None:
    async with Session() as s:
        row = await s.get(AdminSetting, key)
        return row.value if row else (DEFAULT_ADMIN_SETTINGS.get(key, default))

async def get_settings_map() -> dict[str, str]:
    async with Session() as s:
        rows = (await s.execute(select(AdminSetting))).scalars().all()
    out = dict(DEFAULT_ADMIN_SETTINGS)
    out.update({r.key:r.value for r in rows})
    return out

def setting_float(settings, key, fallback):
    try: return float(settings.get(key, fallback))
    except Exception: return float(fallback)

def setting_int(settings, key, fallback):
    try: return int(float(settings.get(key, fallback)))
    except Exception: return int(fallback)

async def set_setting(key: str, value: str):
    async with Session() as s:
        row = await s.get(AdminSetting, key)
        if row is None:
            s.add(AdminSetting(key=key, value=str(value)))
        else:
            row.value=str(value)
        await s.commit()

def diamond_time_from_balance(u, settings):
    if not u or u.id == OWNER_ID:
        return "نامحدود"
    rate=max(setting_float(settings, "diamond_rate_per_hour", DEFAULT_DIAMOND_RATE), 0.000001)
    bal=max(0, int(u.diamonds or 0))
    rem=max(0.0, float(u.diamond_billing_remainder or 0.0))
    # مقدار زمان تا اولین لحظه‌ای که شارژ ساعتی بعدی موجودی را تمام می‌کند.
    hours=(bal + (1.0-rem if bal > 0 else 0.0))/rate
    if bal <= 0:
        hours=max(0.0, (1.0-rem)/rate)
    total_seconds=max(0, int(hours*3600))
    days, rest=divmod(total_seconds, 86400)
    hh, rest=divmod(rest, 3600)
    mm=rest//60
    parts=[]
    if days: parts.append(f"{days} روز")
    if hh or days: parts.append(f"{hh} ساعت")
    if not parts: parts.append(f"{mm} دقیقه")
    return " و ".join(parts)

async def diamond_remaining_text(u):
    settings=await get_settings_map()
    return diamond_time_from_balance(u, settings)

async def deactivate_self(uid: int, clear_phone=True):
    stop_clock(uid); stop_billing(uid); stop_automation(uid)
    async with Session() as s:
        u=await get_user(s,uid)
        if not u: return False
        u.self_enabled=False
        u.self_session=None
        if clear_phone: u.phone_masked=None
        u.pending_phone=None
        u.clock_enabled=False
        u.self_expires_at=None
        u.diamond_billing_remainder=0.0
        u.diamond_last_billed_at=None
        await s.commit()
    return True

async def diamond_balance(uid):
    if uid == OWNER_ID: return None
    async with Session() as s:
        u=await get_user(s,uid)
        return int(u.diamonds or 0) if u else 0

async def log_diamond(s, user_id:int, actor_id:int, amount:int, kind:str, note:str|None=None):
    u=await get_user(s,user_id)
    balance_after=None if user_id==OWNER_ID else int(u.diamonds or 0) if u else None
    s.add(DiamondTransaction(user_id=user_id, actor_id=actor_id, amount=int(amount), kind=kind, note=note, balance_after=balance_after))

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

async def diamond_purchase_text(balance=0):
    balance = int(balance or 0)
    st=await get_settings_map()
    cost=setting_int(st,"activation_cost",SELF_ACTIVATION_COST)
    rate=setting_float(st,"diamond_rate_per_hour",DEFAULT_DIAMOND_RATE)
    pack=setting_int(st,"diamond_pack_size",DIAMONDS_PACK_SIZE)
    price=setting_int(st,"diamond_pack_price",DIAMONDS_PACK_PRICE)
    return (
        f"{DIAMOND_ICON} <b>الماس کافی نیست</b>\n\n"
        f"موجودی فعلی شما: <b>{balance}</b> {DIAMOND_ICON}\n"
        f"برای شروع/فعال‌سازی سلف حداقل <b>{cost} الماس</b> لازم است.\n"
        f"مصرف فعلی: <b>{rate:g} الماس در ساعت</b>.\n\n"
        f"💰 هر {pack} الماس: <b>{price:,} تومان</b>\n"
        f"💰 ۱۰۰۰ الماس: <b>{price * (1000/max(pack,1)):,.0f} تومان</b>\n\n"
        f"📩 برای خرید الماس به ادمین پیام بده: <b>{DIAMOND_ADMIN_USERNAME}</b>"
    )


async def require_diamond_for_self(uid):
    if uid == OWNER_ID: return True
    settings=await get_settings_map(); cost=setting_int(settings,"activation_cost",SELF_ACTIVATION_COST)
    async with Session() as s:
        u=await get_user(s,uid)
        return bool(u and (u.diamonds or 0) >= cost)

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
        d_existing = await conn.run_sync(lambda c: {x["name"] for x in inspect(c).get_columns("diamond_transactions")})
        if "balance_after" not in d_existing:
            await conn.execute(sql_text("ALTER TABLE diamond_transactions ADD COLUMN balance_after INTEGER NULL"))
        additions = {
            "diamonds":"INTEGER DEFAULT 0",
            "welcome_diamond_granted":"BOOLEAN DEFAULT FALSE",
            "diamond_billing_remainder":"DOUBLE PRECISION DEFAULT 0",
            "diamond_last_billed_at":"TIMESTAMP NULL",
            "self_expires_at":"TIMESTAMP NULL",
            "pending_phone":"VARCHAR(64) NULL", "access_requested":"BOOLEAN DEFAULT FALSE", "clock_enabled":"BOOLEAN DEFAULT FALSE", "clock_target":"VARCHAR(16) DEFAULT 'bio'", "clock_font":"VARCHAR(32) DEFAULT 'classic'", "clock_prefix":"VARCHAR(40) DEFAULT '🕐 '", "clock_base_name":"VARCHAR(255) NULL", "clock_base_bio":"TEXT NULL",
            "notifications":"BOOLEAN DEFAULT TRUE", "auto_reply_delay":"INTEGER DEFAULT 3", "auto_reply_cooldown":"INTEGER DEFAULT 60", "auto_reply_scope":"VARCHAR(16) DEFAULT 'private'", "auto_reply_keywords":"TEXT NULL", "animation_enabled":"BOOLEAN DEFAULT TRUE", "animation_style":"VARCHAR(32) DEFAULT 'نرم'", "message_count":"INTEGER DEFAULT 0", "last_seen":"TIMESTAMP NULL", "self_session":"TEXT NULL", "phone_masked":"VARCHAR(64) NULL", "self_enabled":"BOOLEAN DEFAULT FALSE", "smart_mode":"BOOLEAN DEFAULT FALSE", "name_lock":"BOOLEAN DEFAULT FALSE", "locked_name":"VARCHAR(255) NULL", "word_filter":"BOOLEAN DEFAULT FALSE", "word_filter_text":"TEXT NULL", "media_lock":"BOOLEAN DEFAULT FALSE", "comments_mode":"BOOLEAN DEFAULT FALSE", "spam_protection":"BOOLEAN DEFAULT FALSE", "auto_seen":"BOOLEAN DEFAULT FALSE", "button_theme":"VARCHAR(32) DEFAULT 'orange'"
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
                stop_clock(uid); stop_billing(uid); stop_automation(uid)
                return None
        raw=decrypt(u.self_session) if u and u.self_enabled else None
    if not raw: return None
    c=TelegramClient(StringSession(raw), TG_API_ID, TG_API_HASH); await c.connect()
    if not await c.is_user_authorized(): await c.disconnect(); return None
    return c


async def _automation_handler(uid, event):
    """اتوماسیون حساب سلف: سین، ریکت و پاسخ خودکار با محدودکننده سرعت."""
    try:
        if getattr(event, "out", False):
            return
        async with Session() as s:
            u = await get_user(s, uid)
            if not u or not u.self_enabled:
                return
            # به‌روزرسانی آمار فعالیت
            u.last_seen = datetime.now(timezone.utc)
            u.message_count = (u.message_count or 0) + 1
            auto_seen = bool(u.auto_seen)
            reaction = (u.auto_reaction or "").strip()
            reply = u.auto_reply
            delay = max(0, min(int(u.auto_reply_delay or 0), 30))
            cooldown = max(30, min(int(u.auto_reply_cooldown or 60), 86400))
            scope = u.auto_reply_scope or "private"
            keywords_raw = u.auto_reply_keywords or ""
            await s.commit()

        # فیلترهای چت و فرستنده؛ allowlist در صورت وجود اولویت دارد.
        chat_id = int(event.chat_id or 0)
        sender_id = int(getattr(event, "sender_id", 0) or 0)
        async with Session() as s:
            chat_rules = (await s.execute(select(ChatRule).where(ChatRule.user_id == uid, ChatRule.chat_id == chat_id))).scalars().all()
            sender_rules = (await s.execute(select(SenderRule).where(SenderRule.user_id == uid, SenderRule.sender_id == sender_id))).scalars().all()
        if any(r.mode == "block" for r in chat_rules): return
        if any(r.mode == "block" for r in sender_rules): return
        
        # سین خودکار پیام دریافتی
        if auto_seen:
            try:
                await event.client.send_read_acknowledge(event.chat_id, max_id=event.message.id)
            except Exception as e:
                log.debug("auto seen %s: %s", uid, e)

        # ریکت خودکار
        if reaction:
            try:
                await event.message.react(reaction)
            except Exception:
                try:
                    from telethon.tl.functions.messages import SendReactionRequest
                    from telethon.tl.types import ReactionEmoji
                    await event.client(SendReactionRequest(peer=event.chat_id, msg_id=event.message.id, reaction=[ReactionEmoji(emoticon=reaction)]))
                except Exception as e:
                    log.debug("auto reaction %s: %s", uid, e)

        # پاسخ خودکار فقط با محدودکننده و بر اساس محدوده انتخاب‌شده
        if not reply and not keywords_raw:
            return
        if scope == "private" and not event.is_private:
            return
        now_ts = asyncio.get_running_loop().time()
        last = AUTOMATION_LAST_REPLY.get(uid, 0)
        if now_ts - last < cooldown:
            return

        text_in = (event.raw_text or "").strip()
        selected_reply = reply
        if keywords_raw:
            # فرمت: کلمه=>پاسخ | کلمه2=>پاسخ2
            for item in keywords_raw.split("|"):
                if "=>" not in item:
                    continue
                key, val = item.split("=>", 1)
                if key.strip() and key.strip().casefold() in text_in.casefold():
                    selected_reply = val.strip()
                    break
        if not selected_reply:
            return
        if delay:
            await asyncio.sleep(delay)
        try:
            await event.client.send_message(event.chat_id, selected_reply, reply_to=event.message.id)
            AUTOMATION_LAST_REPLY[uid] = asyncio.get_running_loop().time()
        except FloodWaitError as e:
            AUTOMATION_LAST_REPLY[uid] = asyncio.get_running_loop().time()
            log.warning("auto reply flood wait for %s: %s", uid, e)
        except Exception as e:
            log.debug("auto reply %s: %s", uid, e)
    except Exception as e:
        log.warning("automation handler %s: %s", uid, e)

async def automation_loop(uid):
    """یک کلاینت پایدار برای دریافت پیام‌های ورودی سلف نگه می‌دارد."""
    try:
        while True:
            raw = None
            async with Session() as s:
                u = await get_user(s, uid)
                if u and u.self_enabled:
                    raw = decrypt(u.self_session)
            if not raw:
                break
            client = TelegramClient(StringSession(raw), TG_API_ID, TG_API_HASH)
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    break
                client.add_event_handler(lambda e, _uid=uid: _automation_handler(_uid, e), events.NewMessage(incoming=True))
                await client.run_until_disconnected()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("automation connection %s: %s", uid, e)
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            await asyncio.sleep(5)
    except asyncio.CancelledError:
        pass
    finally:
        AUTOMATION_TASKS.pop(uid, None)
        AUTOMATION_LAST_REPLY.pop(uid, None)

def start_automation(uid):
    old = AUTOMATION_TASKS.get(uid)
    if old and not old.done():
        return
    AUTOMATION_TASKS[uid] = asyncio.create_task(automation_loop(uid))

def stop_automation(uid):
    t = AUTOMATION_TASKS.pop(uid, None)
    if t and not t.done():
        t.cancel()
    AUTOMATION_LAST_REPLY.pop(uid, None)

async def update_clock_once(uid):
    c = await get_self_client(uid)
    if not c: return False
    try:
        async with Session() as s:
            u=await get_user(s,uid)
            if not u or not u.clock_enabled: return True
            tm=stylize_time(now_for(u).strftime("%H:%M"),u.clock_font)
            prefix=(u.clock_prefix or "🕐 ")
            target=u.clock_target or "bio"
            base_name=u.clock_base_name or u.first_name or "User"
            base_bio=u.clock_base_bio or ""
        kwargs={}
        if target in ("bio","both"):
            kwargs["about"]=(f"{prefix}{tm}\n{base_bio}" if base_bio else f"{prefix}{tm}")[:70]
        if target in ("name","both"):
            kwargs["first_name"]=(f"{base_name} {tm}").strip()[:64]
        if kwargs:
            await c(UpdateProfileRequest(**kwargs))
        return True
    except FloodWaitError as e:
        log.warning("clock flood wait %s: %s",uid,e)
        await asyncio.sleep(min(int(getattr(e,"seconds",60)),300))
        return True
    except RPCError as e:
        log.warning("clock update %s rpc: %s",uid,e); return True
    except Exception as e:
        log.warning("clock update %s: %s",uid,e); return False
    finally:
        await c.disconnect()


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
        if (u.diamonds or 0) <= 0:
            u.self_enabled=False; u.self_session=None; u.phone_masked=None; u.clock_enabled=False; u.self_expires_at=None
            u.diamond_billing_remainder=0.0; u.diamond_last_billed_at=None
            await s.commit(); stop_clock(uid); stop_billing(uid); stop_automation(uid); return False
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
        settings=dict(DEFAULT_ADMIN_SETTINGS)
        settings.update({r.key:r.value for r in (await s.execute(select(AdminSetting))).scalars().all()})
        rate=max(setting_float(settings, "diamond_rate_per_hour", DEFAULT_DIAMOND_RATE), 0.000001)
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

def self_remaining_text(u):
    if not u or not u.self_enabled:
        return "غیرفعال"
    if u.id == OWNER_ID:
        return "نامحدود"
    if not u.self_expires_at:
        return "ثبت نشده"
    remaining = u.self_expires_at - datetime.now(timezone.utc)
    if remaining.tzinfo is None: remaining=remaining.replace(tzinfo=timezone.utc)
    seconds = int(remaining.total_seconds())
    if seconds <= 0:
        return "منقضی شده"
    days, rem=divmod(seconds,86400); hours, rem=divmod(rem,3600); minutes=rem//60
    parts=[]
    if days: parts.append(f"{days} روز")
    if hours or days: parts.append(f"{hours} ساعت")
    if not days and not hours: parts.append(f"{minutes} دقیقه")
    return " و ".join(parts)

async def panel_text(uid):
    async with Session() as s:
        u=await get_user(s,uid)
        if not u: return "❌ اطلاعات کاربر پیدا نشد."
        balance = "∞" if u.id == OWNER_ID else f"{int(u.diamonds or 0):,}"
        settings=await get_settings_map()
        diamond_time=diamond_time_from_balance(u,settings)
        return (f"🎛 <b>مدیریت سلف</b>\n\n"
                f"👤 <b>{escape(u.first_name or 'کاربر')}</b>\n"
                f"🆔 <code>{u.id}</code>\n"
                f"🔴💎 الماس: <b>{balance}</b>\n"
                f"🔐 سلف: <b>{'فعال' if u.self_enabled else 'خاموش'}</b>\n"
                f"⏳ زمان باقی‌مانده: <b>{self_remaining_text(u)}</b>\n"
                f"⏱ زمان قابل استفاده با موجودی فعلی: <b>{escape(diamond_time)}</b>\n\n"
                "از منوی زیر تنظیمات سلفت را مدیریت کن.")

async def profile_text(uid):
    async with Session() as s:
        u=await get_user(s,uid)
        if not u:return "پروفایل پیدا نشد."
        t=now_for(u).strftime("%H:%M:%S")
        balance = "∞" if u.id == OWNER_ID else str(u.diamonds or 0)
        return f"👤 <b>پروفایل شما</b>\n\n🆔 <code>{u.id}</code>\n🔗 @{escape(u.username or 'ندارد')}\n🌍 {u.timezone}\n🔴💎 الماس: <b>{balance}</b>\n🕐 {stylize_time(t,u.clock_font)}\n💬 {u.message_count or 0} پیام\n🔐 سلف: {'فعال' if u.self_enabled else 'خاموش'}\n⏰ ساعت پروفایل: {'روشن' if u.clock_enabled else 'خاموش'}\n🔤 فونت: {CLOCK_FONTS.get(u.clock_font,('',))[0]}"

async def notify_access_request(bot, m, uid):
    try:
        await bot.send_message(OWNER_ID,
            f"📥 <b>درخواست دسترسی جدید</b>\n👤 {escape(m.from_user.full_name)}\n🆔 <code>{uid}</code>\n\nکاربر هنوز شماره‌ای نداده است؛ بعد از تأیید شما، شماره به‌صورت اجباری از او گرفته می‌شود.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ تأیید دسترسی",callback_data=f"approve:{uid}"),InlineKeyboardButton(text="❌ رد درخواست",callback_data=f"reject:{uid}")],
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
    if m.from_user.id == OWNER_ID or await allowed(m.from_user.id):
        await m.answer(await panel_text(m.from_user.id),reply_markup=main_kb())
    else:
        await m.answer("⛔ ابتدا باید توسط مدیر تأیید شوید و ورود سلف را کامل کنید.")

@dp.message(F.text.func(lambda x: isinstance(x, str) and x.strip().lower() in {"پنل", ".پنل", "/پنل"}))
async def panel_word(m:Message):
    # میانبر پنل در هر چتی؛ پاسخ در همان چتی ارسال می‌شود که میانبر در آن فرستاده شده است.
    if m.from_user.id == OWNER_ID or await allowed(m.from_user.id):
        await m.answer(await panel_text(m.from_user.id),reply_markup=main_kb())
    else:
        await m.answer("⛔ دسترسی پنل برای شما فعال نیست. ابتدا از /start درخواست دسترسی بده.")

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
    await m.answer("📚 <b>راهنمای سریع</b>\n\n/login → ورود سلف\n/code → کد ورود\n/password → رمز دومرحله‌ای\n\n🔴💎 قابلیت فعال پنل: مدیریت الماس\nبرای باز کردن پنل در همان چت: <code>پنل</code> یا <code>.پنل</code>")

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
    raw_text=(m.text or "").strip()
    # این مسیر قبل از منطق ورود اجرا می‌شود تا پنل و انتقال الماس بلعیده نشوند.
    if raw_text.lower() in {"پنل", ".پنل", "/پنل"}:
        if uid == OWNER_ID or await allowed(uid):
            await m.answer(await panel_text(uid),reply_markup=main_kb())
        else:
            await m.answer("⛔ دسترسی پنل برای شما فعال نیست. ابتدا از /start درخواست دسترسی بده.")
        return
    if raw_text.lower().startswith(("انتقال ", ".انتقال ")):
        await fa_transfer_short(m)
        return
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
        await m.answer(await diamond_purchase_text(bal))
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
            settings={r.key:r.value for r in (await s.execute(select(AdminSetting))).scalars().all()}
            cost=setting_int(settings,"activation_cost",SELF_ACTIVATION_COST)
            max_hours=setting_int(settings,"max_self_hours",720)
            if (u.diamonds or 0) < cost:
                await m.answer(await diamond_purchase_text(u.diamonds or 0))
                return
            u.diamonds -= cost
            await log_diamond(s, uid, uid, -cost, "activation", "هزینه شروع سلف")
            now=datetime.now(timezone.utc)
            current=u.self_expires_at
            if current and current.tzinfo is None: current=current.replace(tzinfo=timezone.utc)
            base=current if current and current > now else now
            u.self_expires_at=base + timedelta(hours=max_hours)
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
    start_automation(uid)
    await m.answer("🎛 <b>پنل حرفه‌ای آماده است</b>",reply_markup=main_kb())

















@dp.callback_query(F.data=="time")
async def time_cb(c:CallbackQuery):
    uid=c.from_user.id
    if not await allowed(uid):
        await c.answer("ابتدا ورود سلف را کامل کن.", show_alert=True); return
    async with Session() as s:
        u=await get_user(s,uid)
        if not u: await c.answer("کاربر پیدا نشد.",show_alert=True); return
        preview=stylize_time(now_for(u).strftime("%H:%M"),u.clock_font)
        status="روشن" if u.clock_enabled else "خاموش"
        target={"bio":"بیو","name":"پروفایل","both":"پروفایل + بیو","none":"خاموش"}.get(u.clock_target,"بیو")
        font=CLOCK_FONTS.get(u.clock_font,CLOCK_FONTS["classic"])[0]
    text=(f"⏰ <b>زمان و پروفایل</b>\n\n"
          f"وضعیت تایم: <b>{status}</b>\n"
          f"مقصد: <b>{target}</b>\n"
          f"فونت: <b>{font}</b>\n"
          f"پیش‌نمایش: <code>{escape(preview)}</code>\n\n"
          "از دکمه‌های زیر برای تنظیم ساعت زنده استفاده کن.")
    await c.message.edit_text(text,reply_markup=clock_kb()); await c.answer()

@dp.callback_query(F.data=="clock:on")
async def clock_on_cb(c:CallbackQuery):
    uid=c.from_user.id
    if not await allowed(uid): await c.answer("ورود سلف لازم است.",show_alert=True); return
    async with Session() as s:
        u=await get_user(s,uid)
        if not u or not u.self_enabled: await c.answer("سلف فعال نیست.",show_alert=True); return
        client=await get_self_client(uid)
        if not client: await c.answer("اتصال سلف برقرار نشد.",show_alert=True); return
        try:
            me=await client.get_me()
            full=await client.get_entity("me")
            bio=getattr(full,"about",None) or ""
            name=getattr(me,"first_name",None) or u.first_name or "User"
            if not u.clock_base_name: u.clock_base_name=name
            if u.clock_base_bio is None: u.clock_base_bio=bio
            u.clock_enabled=True
            await s.commit()
        finally:
            await client.disconnect()
    start_clock(uid)
    await update_clock_once(uid)
    await c.message.edit_text("✅ <b>ساعت زنده روشن شد.</b>\nهر دقیقه زمان جدید روی مقصد انتخاب‌شده اعمال می‌شود.",reply_markup=clock_kb()); await c.answer("روشن شد")

@dp.callback_query(F.data=="clock:off")
async def clock_off_cb(c:CallbackQuery):
    uid=c.from_user.id
    if not await allowed(uid): await c.answer("ورود سلف لازم است.",show_alert=True); return
    async with Session() as s:
        u=await get_user(s,uid)
        if not u: await c.answer("کاربر پیدا نشد.",show_alert=True); return
        base_name=u.clock_base_name
        base_bio=u.clock_base_bio
        u.clock_enabled=False
        await s.commit()
    stop_clock(uid)
    client=await get_self_client(uid)
    if client:
        try:
            kwargs={}
            if base_name: kwargs["first_name"]=base_name[:64]
            if base_bio is not None: kwargs["about"]=base_bio[:70]
            if kwargs: await client(UpdateProfileRequest(**kwargs))
        except Exception as e: log.warning("clock restore %s: %s",uid,e)
        finally: await client.disconnect()
    await c.message.edit_text("🔴 <b>ساعت زنده خاموش شد.</b>\nنام و بیوی قبل از ساعت تا حد امکان برگردانده شد.",reply_markup=clock_kb()); await c.answer("خاموش شد")

@dp.callback_query(F.data.startswith("clocktarget:"))
async def clock_target_cb(c:CallbackQuery):
    uid=c.from_user.id; target=c.data.split(":",1)[1]
    if target not in {"bio","name","both","none"}: await c.answer("گزینه نامعتبر.",show_alert=True); return
    if not await allowed(uid): await c.answer("ورود سلف لازم است.",show_alert=True); return
    async with Session() as s:
        u=await get_user(s,uid)
        if not u: await c.answer("کاربر پیدا نشد.",show_alert=True); return
        u.clock_target=target
        if target=="none": u.clock_enabled=False
        await s.commit()
    if target=="none": stop_clock(uid)
    elif (await get_user_state_clock(uid)): start_clock(uid)
    await c.message.edit_text(f"✅ مقصد ساعت روی <b>{{'بیو':'بیو','name':'پروفایل','both':'پروفایل + بیو','none':'خاموش'}}[target]</b> تنظیم شد.",reply_markup=clock_kb()); await c.answer()

async def get_user_state_clock(uid):
    async with Session() as s:
        u=await get_user(s,uid); return bool(u and u.clock_enabled)

@dp.callback_query(F.data=="clocktz")
async def clock_tz_cb(c:CallbackQuery):
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇮🇷 تهران",callback_data="tz:Asia/Tehran"),InlineKeyboardButton(text="🇩🇪 برلین",callback_data="tz:Europe/Berlin")],
        [InlineKeyboardButton(text="🇹🇷 استانبول",callback_data="tz:Europe/Istanbul"),InlineKeyboardButton(text="🌍 UTC",callback_data="tz:UTC")],
        [InlineKeyboardButton(text="⬅️ زمان",callback_data="time")]])
    await c.message.edit_text("🌍 <b>ساعت جهانی</b>\n\nمنطقه زمانی را انتخاب کن؛ ساعت زنده بر اساس آن نمایش داده می‌شود.",reply_markup=kb); await c.answer()

@dp.callback_query(F.data.startswith("tz:"))
async def timezone_cb(c:CallbackQuery):
    uid=c.from_user.id; tz=c.data.split(":",1)[1]
    if not await allowed(uid): await c.answer("ورود سلف لازم است.",show_alert=True); return
    try: ZoneInfo(tz)
    except Exception: await c.answer("منطقه زمانی نامعتبر.",show_alert=True); return
    async with Session() as s:
        u=await get_user(s,uid)
        if not u: await c.answer("کاربر پیدا نشد.",show_alert=True); return
        u.timezone=tz; await s.commit()
    if await get_user_state_clock(uid): await update_clock_once(uid)
    await c.message.edit_text(f"✅ منطقه زمانی روی <b>{escape(tz)}</b> تنظیم شد.",reply_markup=clock_kb()); await c.answer()

@dp.callback_query(F.data.startswith("clockfonts"))
async def clock_fonts_cb(c:CallbackQuery):
    page=0
    if ":" in c.data:
        try: page=int(c.data.split(":",1)[1])
        except: page=0
    await c.message.edit_text("🔤 <b>انتخاب فونت تایم</b>\n\n۲۰ فونت آماده داری؛ نمونه هر فونت داخل دکمه نمایش داده می‌شود.",reply_markup=fonts_kb(page)); await c.answer()

@dp.callback_query(F.data.startswith("font:"))
async def clock_font_cb(c:CallbackQuery):
    uid=c.from_user.id; font=c.data.split(":",1)[1]
    if font not in CLOCK_FONTS or not await allowed(uid): await c.answer("گزینه نامعتبر یا دسترسی ناکافی.",show_alert=True); return
    async with Session() as s:
        u=await get_user(s,uid)
        if not u: await c.answer("کاربر پیدا نشد.",show_alert=True); return
        u.clock_font=font; await s.commit()
    if await get_user_state_clock(uid): await update_clock_once(uid)
    label=CLOCK_FONTS[font][0]
    await c.message.edit_text(f"✅ فونت <b>{escape(label)}</b> انتخاب شد.\nنمونه: <code>{stylize_time('12:34',font)}</code>",reply_markup=clock_kb()); await c.answer()

@dp.callback_query(F.data=="clockbio")
async def clock_bio_cb(c:CallbackQuery):
    await c.message.edit_text("📝 <b>تنظیمات بیو</b>\n\nساعت، بیوی قبلی را نگه می‌دارد و زمان را در ابتدای بیو قرار می‌دهد. هنگام خاموش‌کردن ساعت، بیوی قبلی تا حد امکان برگردانده می‌شود.",reply_markup=clock_kb()); await c.answer()

@dp.callback_query(F.data=="clockhelp")
async def clock_help_cb(c:CallbackQuery):
    await c.message.edit_text("📖 <b>راهنمای زمان و پروفایل</b>\n\n‹ 🕐 تایم روشن — ساعت را فعال می‌کند.\n‹ 🚫 تایم خاموش — نمایش ساعت/پرچم را قطع می‌کند.\n‹ ⏰ ساعت در پروفایل — زمان کنار نام نمایش داده می‌شود.\n‹ 📝 ساعت در بیو — زمان در ابتدای بیو نمایش داده می‌شود.\n‹ ⏰📝 پروفایل + بیو — هر دو را به‌روزرسانی می‌کند.\n‹ 🌍 ساعت جهانی — تهران، برلین، استانبول یا UTC را انتخاب کن.\n‹ 🔤 انتخاب فونت تایم — بین ۲۰ فونت انتخاب کن.\n‹ 📝 تنظیمات بیو — بیوی اصلی حفظ می‌شود.\n\n⚠️ تلگرام روی تغییرات زیاد پروفایل محدودیت دارد؛ به همین دلیل ساعت تقریباً هر ۶۰ ثانیه به‌روزرسانی می‌شود.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ زمان",callback_data="time")]])); await c.answer()

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
"messages":("✉️ مدیریت پیام","پاسخ خودکار با /autoreply متن فعال می‌شود. برای سین، تأخیر، محدوده و پاسخ کلمه‌ای از بخش 🤖 اتوماسیون استفاده کن."),
"automation":("🤖 اتوماسیون سلف","سین خودکار، پاسخ خودکار، پاسخ بر اساس کلمه، تأخیر پاسخ، محدوده خصوصی/همه چت‌ها و ریکت خودکار. همه پاسخ‌ها محدودکننده سرعت دارند."),
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
"spam":("💣 اسپم","حالت ضداسپم فقط رفتارهای تکراری پنل را محدود می‌کند و برای ارسال انبوه استفاده نمی‌شود."),
"currencies":("💰 ارزها","نرخ‌های ارز و طلا از لینک‌های زنده باز می‌شوند."),
"tagall":("📢 تگ همه","تگ انبوه محدود شده است تا از ارسال مزاحم جلوگیری شود."),
"buttons":("🎨 دکمه‌ها","منوی فارسی و توضیح هر بخش برای استفاده راحت آماده شده است."),
"diamonds":("🔴💎 الماس",f"با تأیید مدیر، <b>{WELCOME_DIAMONDS} الماس هدیه</b> می‌گیری. برای شروع سلف فقط <b>{SELF_ACTIVATION_COST} الماس</b> همان لحظه کم می‌شود. اعتبار هر دوره <b>۳۰ روز</b> است و تا <b>{SELF_MONTH_DIAMONDS:,} الماس</b> به‌صورت ساعتی مصرف می‌شود؛ ۱۰۰۰ الماس یکجا صفر نمی‌شود. مدیر موجودی نامحدود دارد. قیمت هر ۱۰۰ الماس <b>۱۰٬۰۰۰ تومان</b> است و خرید از {DIAMOND_ADMIN_USERNAME} انجام می‌شود."),
"self_users":("👥 سلف‌های فعال","فهرست کاربرانی که ورود سلفشان با موفقیت انجام شده است. مدیر می‌تواند نشست سلف هر کاربر را حذف و دسترسی سلف او را لغو کند."),
"help_all":("❓ راهنمای کامل","مسیر ورود: درخواست دسترسی ← تأیید مدیر ← ورود اجباری شماره ← ارسال کد ← ارسال کد به‌صورت متن ← تأیید نهایی. بعد از ورود، هر بخش پنل راهنمای داخلی دارد."),
}





async def admin_user_card(u, title="👤 اطلاعات کامل کاربر"):
    status = "🚫 مسدود" if u.banned else ("✅ تأییدشده" if u.approved else "⏳ در انتظار")
    self_status = "🟢 فعال" if u.self_enabled else "🔴 خاموش"
    expiry = "—"
    if u.self_expires_at:
        exp=u.self_expires_at
        if exp.tzinfo is None: exp=exp.replace(tzinfo=timezone.utc)
        expiry=exp.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    last = u.last_seen
    last_txt = last.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if last else "ثبت نشده"
    created = u.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if u.created_at else "—"
    billing = f"{float(u.diamond_billing_remainder or 0):.3f} {DIAMOND_ICON}" if u.id != OWNER_ID else "∞"
    settings = await get_settings_map()
    diamond_time = diamond_time_from_balance(u, settings)
    return (
        f"{title}\n\n"
        f"👤 نام: <b>{escape(u.first_name or '—')}</b>\n"
        f"🔗 یوزرنیم: <b>@{escape(u.username or 'ندارد')}</b>\n"
        f"🆔 آیدی: <code>{u.id}</code>\n"
        f"📌 وضعیت دسترسی: <b>{status}</b>\n"
        f"📨 درخواست دسترسی: <b>{'بله' if u.access_requested else 'خیر'}</b>\n"
        f"📱 شماره: <b>{escape(u.phone_masked or 'ثبت نشده')}</b>\n"
        f"📱 شماره در انتظار: <b>{escape(masked(u.pending_phone) if u.pending_phone else '—')}</b>\n"
        f"🔐 سلف: <b>{self_status}</b>\n"
        f"⏳ پایان سلف: <b>{expiry}</b>\n"
        f"⏱ باقی‌مانده: <b>{self_remaining_text(u)}</b>\n"
        f"{DIAMOND_ICON} موجودی: <b>{'∞' if u.id==OWNER_ID else f'{int(u.diamonds or 0):,}'}</b>\n"
        f"⏱ زمان قابل استفاده با موجودی فعلی: <b>{escape(diamond_time)}</b>\n"
        f"📉 ذخیره مصرف ساعتی: <b>{billing}</b>\n"
        f"🕐 آخرین محاسبه مصرف: <b>{u.diamond_last_billed_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if u.diamond_last_billed_at else '—'}</b>\n"
        f"💬 تعداد پیام ثبت‌شده: <b>{int(u.message_count or 0):,}</b>\n"
        f"👀 آخرین فعالیت: <b>{last_txt}</b>\n"
        f"📅 تاریخ ثبت: <b>{created}</b>\n"
        f"🌍 منطقه زمانی: <b>{escape(u.timezone or '—')}</b>\n"
        f"⏰ ساعت پروفایل: <b>{'روشن' if u.clock_enabled else 'خاموش'}</b>\n"
        f"🎯 مقصد ساعت: <b>{escape(u.clock_target or '—')}</b>\n"
        f"🔤 فونت ساعت: <b>{escape(CLOCK_FONTS.get(u.clock_font, ('—',))[0])}</b>\n"
        f"✨ انیمیشن: <b>{'روشن' if u.animation_enabled else 'خاموش'}</b> | 🎨 {escape(u.animation_style or '—')}\n"
        f"👍 ریکت خودکار: <b>{escape(u.auto_reaction or 'خاموش')}</b>\n"
        f"💬 پاسخ خودکار: <b>{'روشن' if u.auto_reply else 'خاموش'}</b>\n"
        f"🔒 قفل رسانه: <b>{'روشن' if u.media_lock else 'خاموش'}</b>\n"
        f"🛡 حفاظت اسم: <b>{'روشن' if u.name_lock else 'خاموش'}</b>\n"
        f"🚫 فیلتر کلمات: <b>{'روشن' if u.word_filter else 'خاموش'}</b>\n"
        f"💬 کامنت: <b>{'روشن' if u.comments_mode else 'خاموش'}</b>\n"
        f"🛡 ضداسپم: <b>{'روشن' if u.spam_protection else 'خاموش'}</b>\n"
        f"🎨 تم دکمه: <b>{escape(u.button_theme or '—')}</b>"
    )

def users_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 همه کاربران",callback_data="users_all"),InlineKeyboardButton(text="🔎 جستجو",callback_data="users_search")],
        [InlineKeyboardButton(text="📥 درخواست‌ها",callback_data="pending_users"),InlineKeyboardButton(text="📊 آمار",callback_data="user_stats")],
        [InlineKeyboardButton(text="🟢 سلف‌های فعال",callback_data="self_users")],
        [InlineKeyboardButton(text="⬅️ منو",callback_data="main")]
    ])














def diamond_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 کاربران",callback_data="diamond_users"),InlineKeyboardButton(text="🔎 جستجو",callback_data="diamond_search")],
        [InlineKeyboardButton(text="📊 آمار",callback_data="diamond_stats"),InlineKeyboardButton(text="🧾 گردش حساب",callback_data="diamond_history")],
        [InlineKeyboardButton(text="➕ شارژ سریع",callback_data="diamond_quickadd"),InlineKeyboardButton(text="➖ کسر سریع",callback_data="diamond_quicksub")],
        [InlineKeyboardButton(text="⚙️ مدیریت پیشرفته",callback_data="diamond_advanced"),InlineKeyboardButton(text="🔄 انتقال",callback_data="diamond_transfer_help")],
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
    body=f"📊 <b>مرکز آمار الماس</b>\n\n👥 کاربران: <b>{total_users:,}</b>\n🔴💎 الماس در گردش کاربران: <b>{int(total_d):,}</b>\n🔐 سلف فعال: <b>{active:,}</b>\n📉 مصرف ثبت‌شده سلف: <b>{int(spent):,}</b>\n👑 موجودی مدیر: <b>∞</b>"
    await c.message.edit_text(body,reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🧾 گردش حساب",callback_data="diamond_history")],[InlineKeyboardButton(text="⬅️ مدیریت الماس",callback_data="diamonds")]])); await c.answer()

@dp.callback_query(F.data=="diamond_history")
async def diamond_history(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID: return
    async with Session() as s:
        rows=(await s.execute(select(DiamondTransaction).order_by(DiamondTransaction.created_at.desc()).limit(50))).scalars().all()
    if not rows:
        body="🧾 <b>گردش حساب</b>\n\nهنوز تراکنشی ثبت نشده است."
    else:
        lines=["🧾 <b>۵۰ تراکنش اخیر</b>",""]
        for r in rows:
            sign="+" if r.amount>=0 else ""
            dt=r.created_at.astimezone(timezone.utc).strftime("%m/%d %H:%M") if r.created_at else "—"
            after="∞" if r.user_id==OWNER_ID else (f"{int(r.balance_after):,}" if r.balance_after is not None else "—")
            note=f" — {escape(r.note)}" if r.note else ""
            lines.append(f"<code>{r.user_id}</code> | {sign}{r.amount:,} 🔴💎 | بعد: <b>{after}</b> | مدیر/کاربر: <code>{r.actor_id}</code> | {dt}{note}")
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

def diamond_advanced_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ نرخ مصرف/ساعت",callback_data="diamond_setting:rate"),InlineKeyboardButton(text="⚙️ هزینه فعال‌سازی",callback_data="diamond_setting:activation")],
        [InlineKeyboardButton(text="🎁 الماس هدیه",callback_data="diamond_setting:welcome"),InlineKeyboardButton(text="⏳ سقف ساعت سلف",callback_data="diamond_setting:maxhours")],
        [InlineKeyboardButton(text="💰 قیمت بسته",callback_data="diamond_setting:pack"),InlineKeyboardButton(text="📦 اندازه بسته",callback_data="diamond_setting:packsize")],
        [InlineKeyboardButton(text="⬅️ الماس",callback_data="diamonds")],
    ])

@dp.callback_query(F.data=="diamond_advanced")
async def diamond_advanced(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID:return
    st=await get_settings_map()
    body=(f"⚙️ <b>تنظیمات پیشرفته الماس و سلف</b>\n\n"
          f"{DIAMOND_ICON} مصرف فعلی: <b>{setting_float(st,'diamond_rate_per_hour',DEFAULT_DIAMOND_RATE):g}</b> در ساعت\n"
          f"🔑 هزینه فعال‌سازی: <b>{setting_int(st,'activation_cost',1):,}</b> {DIAMOND_ICON}\n"
          f"🎁 هدیه: <b>{setting_int(st,'welcome_diamonds',50):,}</b> {DIAMOND_ICON}\n"
          f"⏳ سقف اعتبار: <b>{setting_int(st,'max_self_hours',720):,} ساعت</b>\n"
          f"📦 بسته: <b>{setting_int(st,'diamond_pack_size',100):,}</b> {DIAMOND_ICON} / <b>{setting_int(st,'diamond_pack_price',10000):,}</b> تومان")
    await c.message.edit_text(body,reply_markup=diamond_advanced_kb()); await c.answer()

@dp.callback_query(F.data.startswith("diamond_setting:"))
async def diamond_setting(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID:return
    key=c.data.split(":",1)[1]
    prompts={
      "rate":"عدد مصرف الماس در هر ساعت را بفرست. مثال: <code>1.3888889</code>",
      "activation":"هزینه فعال‌سازی اولیه را به الماس بفرست. مثال: <code>1</code>",
      "welcome":"مقدار الماس هدیه را بفرست. مثال: <code>50</code>",
      "maxhours":"حداکثر ساعت اعتبار هر دوره را بفرست. مثال: <code>720</code>",
      "pack":"قیمت بسته فعلی را به تومان بفرست. مثال: <code>10000</code>",
      "packsize":"اندازه بسته الماس را بفرست. مثال: <code>100</code>",
    }
    ADMIN_FLOWS[c.from_user.id]={"type":"diamond_setting","key":key}
    await c.message.edit_text("⚙️ <b>تنظیم مدیر</b>\n\n"+prompts.get(key,"مقدار را بفرست."),reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ لغو",callback_data="diamond_advanced")]])); await c.answer()

@dp.callback_query(F.data=="diamond_transfer_help")
async def diamond_transfer_help(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID:return
    await c.message.edit_text(f"🔄 <b>انتقال {DIAMOND_ICON}</b>\n\nبرای انتقال به آیدی یا یوزرنیم:\n<code>انتقال 100 123456789</code>\n<code>انتقال 100 @username</code>\n\nیا روی پیام کاربر ریپلای کن و فقط بفرست:\n<code>انتقال 100</code>\n\nمدیر می‌تواند مقدار دلخواه را تعیین کند.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ الماس",callback_data="diamonds")]])); await c.answer()

# ---------------- متن و ابزارهای گروه ----------------
TEXT_STYLES = {
    "strike": ("خط خورده", "s"),
    "underline": ("زیرخط", "u"),
    "bold": ("بولد", "b"),
    "quote": ("نقل قول", "blockquote"),
    "spoiler": ("اسپویلر", "tg-spoiler"),
    "italic": ("کج", "i"),
    "code": ("کد", "code"),
    "pre": ("پیش", "pre"),
}

def textstyle_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="خط خورده", callback_data="style:strike"), InlineKeyboardButton(text="زیرخط", callback_data="style:underline"), InlineKeyboardButton(text="بولد", callback_data="style:bold")],
        [InlineKeyboardButton(text="نقل قول", callback_data="style:quote"), InlineKeyboardButton(text="اسپویلر", callback_data="style:spoiler"), InlineKeyboardButton(text="کج", callback_data="style:italic")],
        [InlineKeyboardButton(text="کد", callback_data="style:code"), InlineKeyboardButton(text="پیش", callback_data="style:pre")],
        [InlineKeyboardButton(text="📖 راهنما", callback_data="stylehelp")],
        [InlineKeyboardButton(text="⬅️ برگشت", callback_data="main")],
    ])

def spam_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💣 ارسال محدود", callback_data="spam:start")],
        [InlineKeyboardButton(text="📖 راهنما", callback_data="spamhelp")],
        [InlineKeyboardButton(text="⬅️ برگشت", callback_data="main")],
    ])

def tagall_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏷️ تگ همه [محدود]", callback_data="tagall:start")],
        [InlineKeyboardButton(text="⛔ لغو تگ", callback_data="tagall:cancel")],
        [InlineKeyboardButton(text="📖 راهنما", callback_data="tagallhelp")],
        [InlineKeyboardButton(text="⬅️ برگشت", callback_data="main")],
    ])

@dp.callback_query(F.data=="textstyle")
async def textstyle_page(c:CallbackQuery):
    await c.message.edit_text("✍️ <b>استایل متن</b>\n\nیکی از استایل‌ها را انتخاب کن، بعد متن بعدی را بفرست.\nهر بار فقط یک پیام قالب‌بندی می‌شود.", reply_markup=textstyle_kb()); await c.answer()

@dp.callback_query(F.data.startswith("style:"))
async def style_select(c:CallbackQuery):
    key=c.data.split(":",1)[1]
    if key not in TEXT_STYLES: await c.answer("نامعتبر", show_alert=True); return
    STYLE_FLOWS[c.from_user.id]=key
    await c.message.edit_text(f"✍️ <b>{TEXT_STYLES[key][0]}</b> انتخاب شد.\n\nحالا متن را در همین چت بفرست.", reply_markup=textstyle_kb()); await c.answer("انتخاب شد")

@dp.callback_query(F.data=="stylehelp")
async def style_help(c:CallbackQuery):
    await c.message.edit_text("📖 <b>راهنمای استایل متن</b>\n\n‹ خط خورده — متن را خط‌خورده می‌کند.\n‹ زیرخط — زیر متن خط می‌کشد.\n‹ بولد — ضخیم.\n‹ نقل قول — متن را به شکل نقل‌قول می‌فرستد.\n‹ اسپویلر — متن مخفی می‌شود تا روی آن بزنند.\n‹ کج — ایتالیک.\n‹ کد — متن کدی.\n‹ پیش — متن پیش‌فرمت‌شده.\n\nبعد از انتخاب، فقط یک پیام بعدی قالب‌بندی می‌شود.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ استایل متن",callback_data="textstyle")]])); await c.answer()

@dp.callback_query(F.data=="tagall")
async def tagall_page(c:CallbackQuery):
    await c.message.edit_text("📢 <b>تگ همه</b>\n\nهمه اعضای قابل‌دسترسی گروه را در <b>یک پیام</b> منشن می‌کند. برای جلوگیری از ارسال مزاحم، حداکثر ۳۰ نفر در هر اجرا پردازش می‌شوند.", reply_markup=tagall_kb()); await c.answer()

@dp.callback_query(F.data=="tagall:start")
async def tagall_start(c:CallbackQuery):
    uid=c.from_user.id
    if not await allowed(uid): await c.answer("دسترسی ندارید",show_alert=True); return
    SPAM_FLOWS.pop(uid,None)
    SPAM_FLOWS[uid]={"kind":"tagall","chat_id":c.message.chat.id}
    await c.message.edit_text("📢 <b>تگ همه</b>\n\nحالا یک متن کوتاه برای همراه تگ‌ها بفرست.\nمثال: <code>بچه‌ها آنلاینید؟</code>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ لغو",callback_data="tagall:cancel")]])); await c.answer()

@dp.callback_query(F.data=="tagall:cancel")
async def tagall_cancel(c:CallbackQuery):
    SPAM_FLOWS.pop(c.from_user.id,None); await c.message.edit_text("📢 تگ همه لغو شد.",reply_markup=tagall_kb()); await c.answer()

@dp.callback_query(F.data=="tagallhelp")
async def tagall_help(c:CallbackQuery):
    await c.message.edit_text("📖 <b>راهنمای تگ همه</b>\n\nدر یک گروه اجرا می‌شود و حداکثر ۳۰ عضو را در یک پیام منشن می‌کند.\nبرای جلوگیری از مزاحمت، اجرای پشت‌سرهم محدود شده است.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ تگ همه",callback_data="tagall")]])); await c.answer()

@dp.callback_query(F.data=="spam")
async def spam_page(c:CallbackQuery):
    await c.message.edit_text("💣 <b>اسپم محدود</b>\n\nاین بخش فقط برای تست ارسال تکراری در همان چت است: حداکثر ۳ پیام با فاصله ۲ ثانیه. اجرای نامحدود یا ارسال انبوه وجود ندارد.", reply_markup=spam_kb()); await c.answer()

@dp.callback_query(F.data=="spam:start")
async def spam_start(c:CallbackQuery):
    uid=c.from_user.id
    if not await allowed(uid): await c.answer("دسترسی ندارید",show_alert=True); return
    SPAM_FLOWS[uid]={"kind":"limited_spam","chat_id":c.message.chat.id}
    await c.message.edit_text("💣 <b>اسپم محدود</b>\n\nمتن را بفرست. ربات فقط تا ۳ بار با فاصله ۲ ثانیه همان متن را در همین چت می‌فرستد.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ لغو",callback_data="spam:cancel")]])); await c.answer()

@dp.callback_query(F.data=="spam:cancel")
async def spam_cancel(c:CallbackQuery):
    SPAM_FLOWS.pop(c.from_user.id,None); await c.message.edit_text("💣 اسپم محدود لغو شد.",reply_markup=spam_kb()); await c.answer()

@dp.callback_query(F.data=="spamhelp")
async def spam_help(c:CallbackQuery):
    await c.message.edit_text("📖 <b>راهنمای اسپم محدود</b>\n\nبرای تست قابلیت ارسال تکراری است و سقف آن ۳ پیام با فاصله ۲ ثانیه است. اسپم نامحدود یا Flood در این پنل فعال نیست.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ اسپم",callback_data="spam")]])); await c.answer()

@dp.message(F.text)
async def tool_text_handler(m:Message):
    uid=m.from_user.id
    if not await allowed(uid): return
    text=m.text or ""
    # اگر هیچ ابزار مرحله‌ای فعال نیست، این هندلر باید عبور کند تا fallbackهای اصلی کار کنند.
    if uid not in STYLE_FLOWS and uid not in SPAM_FLOWS:
        raise SkipHandler()
    # استایل متن
    style=STYLE_FLOWS.pop(uid,None)
    if style:
        tag=TEXT_STYLES[style][1]
        safe=escape(text)
        await m.answer(f"<{tag}>{safe}</{tag}>")
        return
    # تگ همه
    flow=SPAM_FLOWS.get(uid)
    if flow and flow.get("kind")=="tagall":
        SPAM_FLOWS.pop(uid,None)
        client=await get_self_client(uid)
        if not client: await m.answer("❌ سلف شما فعال نیست."); return
        try:
            entity=await client.get_entity(m.chat.id)
            parts=[]
            async for p in client.iter_participants(entity,limit=30):
                if getattr(p,"bot",False) or getattr(p,"deleted",False): continue
                name=escape((getattr(p,"first_name",None) or getattr(p,"username",None) or "کاربر").strip())
                parts.append(f'<a href="tg://user?id={p.id}">{name}</a>')
            body=escape(text)
            if not parts: await m.answer("❌ عضو قابل منشن پیدا نشد."); return
            await client.send_message(entity, body+"\n\n"+" ".join(parts),parse_mode="html")
            await m.answer(f"✅ تگ محدود انجام شد: {len(parts)} نفر")
        except Exception as e:
            log.warning("tagall: %s",e); await m.answer("❌ اجرای تگ همه انجام نشد؛ ممکن است گروه یا دسترسی اکانت محدود باشد.")
        finally:
            try: await client.disconnect()
            except Exception: pass
        return
    # اسپم محدود
    if flow and flow.get("kind")=="limited_spam":
        SPAM_FLOWS.pop(uid,None)
        client=await get_self_client(uid)
        if not client: await m.answer("❌ سلف شما فعال نیست."); return
        try:
            entity=await client.get_entity(m.chat.id)
            for i in range(3):
                await client.send_message(entity,text)
                if i<2: await asyncio.sleep(2)
            await m.answer("✅ ارسال محدود انجام شد (۳ پیام).")
        except FloodWaitError as e:
            await m.answer(f"⏳ تلگرام محدودیت سرعت گذاشت؛ {getattr(e,'seconds',0)} ثانیه صبر کن.")
        except Exception as e:
            log.warning("limited spam: %s",e); await m.answer("❌ ارسال انجام نشد.")
        finally:
            try: await client.disconnect()
            except Exception: pass
        return


@dp.callback_query(F.data=="diamonds")
async def diamonds_page(c:CallbackQuery):
    uid=c.from_user.id
    if uid==OWNER_ID:
        async with Session() as s:
            users=await s.scalar(select(func.count(User.id)).where(User.id!=OWNER_ID)) or 0
            total=await s.scalar(select(func.coalesce(func.sum(User.diamonds),0)).where(User.id!=OWNER_ID)) or 0
            active=await s.scalar(select(func.count(User.id)).where(User.self_enabled==True,User.id!=OWNER_ID)) or 0
        body=(f"🔴💎 <b>مرکز مدیریت الماس</b>\n\n"
              f"👑 موجودی مدیر: <b>∞</b>\n"
              f"👥 کاربران: <b>{int(users):,}</b>\n"
              f"🔴💎 الماس در گردش: <b>{int(total):,}</b>\n"
              f"🔐 سلف فعال: <b>{int(active):,}</b>\n\n"
              "از اینجا موجودی کاربران، گردش مالی، آمار و عملیات شارژ/کسر را کنترل کن.")
        await c.message.edit_text(body,reply_markup=diamond_admin_kb())
    else:
        async with Session() as s:u=await get_user(s,uid)
        bal=u.diamonds if u else 0
        expiry=(u.self_expires_at.astimezone(timezone.utc).strftime("%Y/%m/%d %H:%M UTC") if u and u.self_expires_at else "فعال نیست")
        body=(f"🔴💎 <b>کیف پول الماس من</b>\n\n"
              f"🔴💎 موجودی: <b>{bal:,}</b>\n"
              f"🔐 سلف: <b>{'فعال' if u and u.self_enabled else 'خاموش'}</b>\n"
              f"📅 اعتبار: <b>{expiry}</b>\n\n"
              f"⚡ شروع سلف: <b>{SELF_ACTIVATION_COST} الماس</b>\n"
              f"📆 دوره: <b>۳۰ روز</b>\n"
              f"💰 هر ۱۰۰ الماس: <b>۱۰٬۰۰۰ تومان</b>\n\n"
              f"🛒 برای خرید الماس به <b>{DIAMOND_ADMIN_USERNAME}</b> پیام بده.")
        kb=[[InlineKeyboardButton(text="🛒 خرید الماس",url="https://t.me/jokm7")],[InlineKeyboardButton(text="⬅️ منو",callback_data="main")]]
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
        buttons.append([InlineKeyboardButton(text=f"🔴💎 {name} | {u.diamonds or 0}",callback_data=f"diamond_user:{u.id}")])
    buttons.append([InlineKeyboardButton(text="⬅️ مدیریت الماس",callback_data="diamonds")])
    await c.message.edit_text("👥 <b>کاربران الماسی</b>\n\nیک کاربر را برای مدیریت انتخاب کن:",reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)); await c.answer()

@dp.callback_query(F.data.startswith("diamond_user:"))
async def diamond_user_detail(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID:return
    uid=int(c.data.split(":",1)[1])
    async with Session() as s:u=await get_user(s,uid)
    if not u: await c.answer("کاربر پیدا نشد",show_alert=True); return
    body=(f"🔴💎 <b>مدیریت کیف پول</b>\n\n👤 {escape(u.first_name or '—')}\n"
          f"🆔 <code>{u.id}</code>\n🔗 @{escape(u.username or 'ندارد')}\n"
          f"🔴💎 موجودی: <b>{u.diamonds or 0:,}</b>\n"
          f"🔐 سلف: <b>{'فعال' if u.self_enabled else 'خاموش'}</b>\n"
          f"⏳ زمان باقی‌مانده سلف: <b>{self_remaining_text(u)}</b>\n"
          f"⏱ زمان قابل استفاده با موجودی فعلی: <b>{escape(available_time)}</b>")
    kb=[[InlineKeyboardButton(text="➕ ۱۰۰",callback_data=f"diamond_adj:{uid}:100"),InlineKeyboardButton(text="➕ ۱۰۰۰",callback_data=f"diamond_adj:{uid}:1000")],
        [InlineKeyboardButton(text="➖ ۱۰۰",callback_data=f"diamond_adj:{uid}:-100"),InlineKeyboardButton(text="➖ ۱۰۰۰",callback_data=f"diamond_adj:{uid}:-1000")],
        [InlineKeyboardButton(text="➕ مقدار دلخواه",callback_data=f"diamond_custom:{uid}:add"),InlineKeyboardButton(text="➖ مقدار دلخواه",callback_data=f"diamond_custom:{uid}:sub")],
        [InlineKeyboardButton(text="🧹 صفر کردن موجودی",callback_data=f"diamond_zero:{uid}")],
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
    try: await c.bot.send_message(uid,f"🔴💎 موجودی الماس شما توسط مدیر {'+' if amount>0 else ''}{amount:,} تغییر کرد.\nموجودی جدید: <b>{bal:,}</b>")
    except Exception: pass
    await c.answer(f"موجودی به {bal:,} رسید",show_alert=True)
    await diamond_user_detail(c)

@dp.callback_query(F.data.startswith("diamond_user_history:"))
async def diamond_user_history(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID:return
    uid=int(c.data.split(":",1)[1])
    async with Session() as s: rows=(await s.execute(select(DiamondTransaction).where(DiamondTransaction.user_id==uid).order_by(DiamondTransaction.created_at.desc()).limit(15))).scalars().all()
    lines=[f"🧾 <b>گردش الماس {uid}</b>",""]
    for r in rows:
        sign="+" if r.amount>=0 else ""
        dt=r.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if r.created_at else "—"
        after="∞" if uid==OWNER_ID else (f"{int(r.balance_after):,}" if r.balance_after is not None else "—")
        note=f" | {escape(r.note)}" if r.note else ""
        lines.append(f"{sign}{r.amount:,} 🔴💎 | بعد: <b>{after}</b> | توسط <code>{r.actor_id}</code> | {dt} | {escape(r.kind)}{note}")
    await c.message.edit_text("\n".join(lines) if rows else lines[0]+"\n\nتراکنشی ثبت نشده.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ کاربر",callback_data=f"diamond_user:{uid}")]])); await c.answer()

@dp.callback_query(F.data=="diamond_search")
async def diamond_search(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID:return
    ADMIN_FLOWS[c.from_user.id]={"type":"diamond_search"}
    await c.message.edit_text("🔎 <b>جستجوی الماس</b>\n\nآیدی عددی یا @username یا بخشی از نام کاربر را بفرست.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ لغو",callback_data="diamonds")]])); await c.answer()

@dp.callback_query(F.data.startswith("diamond_custom:"))
async def diamond_custom(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID:return
    _,uid_s,mode=c.data.split(":")
    uid=int(uid_s)
    ADMIN_FLOWS[c.from_user.id]={"type":"diamond_adjust","uid":uid,"mode":mode}
    await c.message.edit_text(("➕ <b>شارژ سفارشی</b>\n\nمقدار الماس مثبت را بفرست:" if mode=="add" else "➖ <b>کسر سفارشی</b>\n\nمقدار الماس مثبت را بفرست:"),reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ لغو",callback_data=f"diamond_user:{uid}")]])); await c.answer()

@dp.callback_query(F.data.startswith("diamond_zero:"))
async def diamond_zero(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID:return
    uid=int(c.data.split(":",1)[1])
    async with Session() as s:
        u=await get_user(s,uid)
        if not u: await c.answer("کاربر پیدا نشد",show_alert=True); return
        old=int(u.diamonds or 0)
        if old:
            await change_diamonds(s,uid,OWNER_ID,-old,"admin_zero","صفر کردن موجودی از پنل")
            await s.commit()
    await c.answer("موجودی صفر شد",show_alert=True)
    await diamond_user_detail(c)














def simple_page(title, body, rows=None):
    rows = rows or []
    rows.append([InlineKeyboardButton(text="📖 راهنمای این بخش", callback_data=f"help:{title.split(' ',1)[-1]}")])
    rows.append([InlineKeyboardButton(text="⬅️ منوی اصلی", callback_data="main")])
    return InlineKeyboardMarkup(inline_keyboard=rows), f"<b>{title}</b>\n\n{body}"












































@dp.callback_query(F.data.startswith("approve:"))
async def approve(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID:return
    uid=int(c.data.split(":")[1])
    async with Session() as s:
        u=await get_user(s,uid)
        if not u: await c.answer("کاربر پیدا نشد.",show_alert=True); return
        was_welcomed=bool(u.welcome_diamond_granted)
        settings={r.key:r.value for r in (await s.execute(select(AdminSetting))).scalars().all()}
        welcome=setting_int(settings,"welcome_diamonds",WELCOME_DIAMONDS)
        u.approved=True; u.banned=False; u.access_requested=False; u.pending_phone=None
        if not was_welcomed and welcome>0:
            u.diamonds=(u.diamonds or 0) + welcome
            u.welcome_diamond_granted=True
            await log_diamond(s, uid, OWNER_ID, welcome, "welcome", "هدیه تأیید مدیر")
        new_balance=u.diamonds or 0
        await s.commit()
    await c.answer("دسترسی تأیید شد")
    try:
        await c.message.edit_text(c.message.text + "\n\n✅ <b>تأیید شد.</b> حالا شماره را از کاربر بگیر؛ کد هنوز ارسال نشده است.")
    except Exception: pass
    bonus_text = f"\n\n🎁 <b>{welcome} الماس هدیه</b> برای اولین تأیید به حسابت اضافه شد.\n🔴💎 موجودی فعلی: <b>{new_balance}</b>" if not was_welcomed and welcome>0 else ""
    await c.bot.send_message(uid,"🎉 <b>دسترسی شما تأیید شد.</b>" + bonus_text + "\n\nحالا شماره اکانت تلگرامت اجباری است. شماره را همینجا به‌صورت متن بفرست.\n\nنمونه: <code>+989967066405</code>\n\nبه‌محض دریافت شماره معتبر، کد ورود به‌صورت خودکار ارسال می‌شود و هیچ دکمه‌ای برای ارسال شماره یا کد لازم نیست.")

@dp.callback_query(F.data.startswith("reject:"))
async def reject(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID:return
    uid=int(c.data.split(":")[1])
    async with Session() as s:u=await get_user(s,uid); u.approved=False; u.access_requested=False; u.pending_phone=None; await s.commit()
    await c.answer("رد شد"); await c.message.edit_reply_markup(reply_markup=None)







@dp.message(F.text.startswith("/الماس"))
async def fa_diamonds(m:Message):
    p=(m.text or "").split()
    if len(p)==1:
        async with Session() as s:u=await get_user(s,m.from_user.id)
        await m.answer(f"🔴💎 موجودی شما: <b>{'∞' if m.from_user.id==OWNER_ID else (u.diamonds if u else 0)}</b>\n\n🎁 هدیه اولین تأیید: {WELCOME_DIAMONDS} الماس\n🔑 هزینه شروع سلف: {SELF_ACTIVATION_COST} الماس\n📅 اعتبار: ۳۰ روز\n📉 مصرف دوره: تا {SELF_MONTH_DIAMONDS:,} الماس به‌صورت ساعتی\n💰 هر ۱۰۰ الماس: ۱۰٬۰۰۰ تومان\n📩 خرید الماس: {DIAMOND_ADMIN_USERNAME}")
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
    await m.answer(f"🔴💎 موجودی کاربر <code>{uid}</code> به <b>{bal}</b> رسید.")

@dp.message(F.text.startswith("/انتقال_الماس"))
async def fa_transfer_diamonds(m:Message):
    p=(m.text or "").split()
    if len(p)!=3 or not p[2].isdigit() or int(p[2])<=0: await m.answer("نمونه: <code>/انتقال_الماس @username 5</code>"); return
    amount=int(p[2]); sender_id=m.from_user.id
    async with Session() as s:
        sender=await get_user(s,sender_id); target=await resolve_user(s,p[1])
        if not sender or not target: await m.answer("❌ فرستنده یا گیرنده پیدا نشد."); return
        if target.id==sender_id: await m.answer("❌ انتقال به خودت امکان‌پذیر نیست."); return
        if sender_id!=OWNER_ID and (sender.diamonds or 0)<amount: await m.answer("🔴💎 موجودی کافی نیست."); return
        if sender_id!=OWNER_ID:
            sender.diamonds-=amount
            await log_diamond(s,sender_id,sender_id,-amount,"transfer_out",f"انتقال به {target.id}")
        target.diamonds=(target.diamonds or 0)+amount
        await log_diamond(s,target.id,sender_id,amount,"transfer_in",f"دریافت از {sender_id}")
        await s.commit(); target_name=escape(target.first_name or str(target.id))
    await m.answer(f"✅ {amount} 🔴💎 به {target_name} منتقل شد.")
    try: await m.bot.send_message(target.id,f"🔴💎 <b>{amount} الماس</b> از طرف یک کاربر برایت انتقال داده شد.")
    except Exception: pass

@dp.message(F.text.func(lambda x: isinstance(x,str) and x.strip().lower().startswith(("انتقال ",".انتقال "))))
async def fa_transfer_short(m:Message):
    raw=(m.text or "").strip()
    parts=raw.split()
    if len(parts)<2 or not parts[1].isdigit() or int(parts[1])<=0:
        await m.answer(f"نمونه: <code>انتقال 100</code> با ریپلای، یا <code>انتقال 100 @username</code> / <code>انتقال 100 123456789</code>")
        return
    amount=int(parts[1]); sender_id=m.from_user.id
    target_id=None
    if m.reply_to_message and m.reply_to_message.from_user:
        target_id=m.reply_to_message.from_user.id
    elif len(parts)>=3:
        target_token=parts[2].strip()
        async with Session() as s:
            target=await resolve_user(s,target_token)
            target_id=target.id if target else None
    if not target_id:
        await m.answer("❌ گیرنده پیدا نشد. روی پیام کاربر ریپلای کن یا آیدی/یوزرنیم بده.")
        return
    if target_id==sender_id:
        await m.answer("❌ انتقال به خودت امکان‌پذیر نیست."); return
    async with Session() as s:
        sender=await get_user(s,sender_id); target=await get_user(s,target_id)
        if not target:
            await m.answer("❌ گیرنده در پنل ثبت نشده است."); return
        if sender_id!=OWNER_ID and (not sender or (sender.diamonds or 0)<amount):
            await m.answer(f"{DIAMOND_ICON} موجودی کافی نیست."); return
        if sender_id!=OWNER_ID:
            sender.diamonds-=amount
            await log_diamond(s,sender_id,sender_id,-amount,"transfer_out",f"انتقال به {target.id}")
        target.diamonds=(target.diamonds or 0)+amount
        await log_diamond(s,target.id,sender_id,amount,"transfer_in",f"دریافت از {sender_id}")
        await s.commit()
        target_name=escape(target.first_name or target.username or str(target.id)); bal=int(target.diamonds or 0)
    await m.answer(f"✅ {amount:,} {DIAMOND_ICON} به <b>{target_name}</b> منتقل شد.\nموجودی جدید: <b>{bal:,}</b>")
    try: await m.bot.send_message(target.id,f"{DIAMOND_ICON} <b>{amount:,} الماس</b> از طرف مدیر/کاربر برایت انتقال داده شد.\nموجودی جدید: <b>{bal:,}</b>")
    except Exception: pass



@dp.message(F.text.func(lambda x: isinstance(x,str) and bool(ADMIN_FLOWS)))
async def admin_flow_message(m:Message):
    if m.from_user.id!=OWNER_ID or not m.text:
        return
    flow=ADMIN_FLOWS.get(m.from_user.id)
    if not flow: return
    value=m.text.strip()
    if value.startswith(".") and value in {".پنل"}:
        ADMIN_FLOWS.pop(m.from_user.id,None)
        await m.answer(await panel_text(m.from_user.id),reply_markup=main_kb()); return
    if flow["type"]=="diamond_setting":
        key=flow["key"]
        try:
            if key=="rate":
                val=float(value.replace(",",".")); assert val>0
            else:
                val=int(value); assert val>=0
            if key=="maxhours" and val<=0: raise ValueError
        except Exception:
            await m.answer("❌ مقدار نامعتبر است. یک عدد معتبر بفرست."); return
        await set_setting({"rate":"diamond_rate_per_hour","activation":"activation_cost","welcome":"welcome_diamonds","maxhours":"max_self_hours","pack":"diamond_pack_price","packsize":"diamond_pack_size"}[key], str(val))
        ADMIN_FLOWS.pop(m.from_user.id,None)
        await m.answer("✅ تنظیم مدیر ذخیره شد.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ تنظیمات پیشرفته",callback_data="diamond_advanced")],[InlineKeyboardButton(text="⬅️ الماس",callback_data="diamonds")]])); return
    if flow["type"]=="user_search":
        async with Session() as s:
            target=value.lstrip("@")
            q=select(User)
            if target.isdigit(): q=q.where(User.id==int(target))
            else: q=q.where((func.lower(User.username)==target.lower()) | (User.first_name.ilike(f"%{target}%")))
            rows=(await s.execute(q.limit(20))).scalars().all()
        ADMIN_FLOWS.pop(m.from_user.id,None)
        if not rows:
            await m.answer("❌ کاربری با این مشخصات پیدا نشد.",reply_markup=users_admin_kb()); return
        kb=[[InlineKeyboardButton(text=f"👤 {(u.first_name or u.username or str(u.id))[:22]} | 🔴💎 {'∞' if u.id==OWNER_ID else int(u.diamonds or 0):,}",callback_data=f"user_detail:{u.id}")] for u in rows]
        kb.append([InlineKeyboardButton(text="⬅️ کاربران",callback_data="users_menu")])
        await m.answer("🔎 <b>نتیجه جستجو</b>",reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)); return
    if flow["type"]=="diamond_search":
        async with Session() as s:
            target=value.lstrip("@")
            q=select(User)
            if target.isdigit(): q=q.where(User.id==int(target))
            else: q=q.where((func.lower(User.username)==target.lower()) | (User.first_name.ilike(f"%{target}%")))
            rows=(await s.execute(q.limit(20))).scalars().all()
        ADMIN_FLOWS.pop(m.from_user.id,None)
        if not rows:
            await m.answer("❌ کاربری با این مشخصات پیدا نشد.",reply_markup=diamond_admin_kb()); return
        kb=[[InlineKeyboardButton(text=f"🔴💎 {(u.first_name or u.username or str(u.id))[:20]} | {'∞' if u.id==OWNER_ID else f"{int(u.diamonds or 0):,}"}",callback_data=f"diamond_user:{u.id}")] for u in rows]
        kb.append([InlineKeyboardButton(text="⬅️ الماس",callback_data="diamonds")])
        await m.answer("🔎 <b>نتیجه جستجوی الماس</b>",reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)); return
    if flow["type"]=="diamond_adjust":
        if not value.isdigit() or int(value)<=0:
            await m.answer("❌ فقط یک عدد مثبت بفرست."); return
        amount=int(value)
        if flow["mode"]=="sub": amount=-amount
        uid=flow["uid"]; ADMIN_FLOWS.pop(m.from_user.id,None)
        async with Session() as s:
            ok=await change_diamonds(s,uid,OWNER_ID,amount,"admin_add" if amount>0 else "admin_sub","مقدار سفارشی از پنل")
            u=await get_user(s,uid)
            if ok is None: await m.answer("❌ کاربر پیدا نشد."); return
            if ok is False: await m.answer("❌ موجودی کافی نیست."); return
            await s.commit(); bal=int(u.diamonds or 0)
        try: await m.bot.send_message(uid,f"🔴💎 موجودی الماس شما توسط مدیر تغییر کرد.\nموجودی جدید: <b>{bal:,}</b>")
        except Exception: pass
        await m.answer(f"✅ انجام شد. موجودی جدید: <b>{bal:,}</b>",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👤 اطلاعات کامل",callback_data=f"user_detail:{uid}"),InlineKeyboardButton(text="🔴💎 کیف پول",callback_data=f"diamond_user:{uid}")]])); return


def parse_duration(raw: str):
    import re
    m=re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", raw.lower())
    if not m: return None
    n=int(m.group(1)); unit=m.group(2)
    return n*{"s":1,"m":60,"h":3600,"d":86400}[unit]









async def reminder_loop(bot: Bot):
    while True:
        try:
            async with Session() as s:
                rows=(await s.execute(select(Reminder).where(Reminder.done==False,Reminder.due_at<=datetime.now(timezone.utc)).limit(50))).scalars().all()
                for r in rows:
                    try: await bot.send_message(r.chat_id, f"⏰ <b>یادآوری</b>\n{escape(r.text)}")
                    except Exception as e: log.debug("reminder send %s: %s", r.id, e)
                    r.done=True
                await s.commit()
        except Exception as e: log.warning("reminder loop: %s", e)
        await asyncio.sleep(2)

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
    for uid in enabled_ids:
        await bill_self_once(uid)
        async with Session() as s:
            chk=await get_user(s,uid)
            still=bool(chk and chk.self_enabled)
        if still: start_billing(uid)
    for uid in clock_ids: start_clock(uid)
    for uid in enabled_ids: start_automation(uid)
    reminder_task=asyncio.create_task(reminder_loop(bot))
    log.info("Bot started")
    try: await dp.start_polling(bot)
    finally:
        for t in list(CLOCK_TASKS.values()): t.cancel()
        for t in list(BILLING_TASKS.values()): t.cancel()
        for t in list(AUTOMATION_TASKS.values()): t.cancel()
        reminder_task.cancel()
        await bot.session.close()

if __name__=="__main__": asyncio.run(main())
