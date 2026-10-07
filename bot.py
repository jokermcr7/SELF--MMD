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
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, RPCError, PhoneCodeInvalidError, PhoneCodeExpiredError, FloodWaitError
from telethon.tl.functions.account import UpdateProfileRequest, UpdateUsernameRequest
from telethon.tl.functions.contacts import BlockRequest, UnblockRequest
from telethon.tl.functions.messages import CreateChatRequest
from telethon.tl.types import InputUserSelf

from aiogram import Bot, Dispatcher, F
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
BOT_USERNAME = ""
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
    smart_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    name_lock: Mapped[bool] = mapped_column(Boolean, default=False)
    locked_name: Mapped[str | None] = mapped_column(String(255))
    word_filter: Mapped[bool] = mapped_column(Boolean, default=False)
    word_filter_text: Mapped[str | None] = mapped_column(Text)
    media_lock: Mapped[bool] = mapped_column(Boolean, default=False)
    private_lock: Mapped[bool] = mapped_column(Boolean, default=False)
    backup_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    backup_chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
BACKUP_TASKS: dict[int, asyncio.Task] = {}

# فونت‌های ساعت؛ همگی فقط تبدیل ظاهری اعداد هستند و به متن اصلی دست نمی‌زنند.
CLOCK_FONTS = {
    "classic": ("کلاسیک", "0123456789", "0123456789"),
    "bold": ("ضخیم", "0123456789", "𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗"),
    "double": ("دوبل", "0123456789", "𝟘𝟙𝟚𝟛𝟜𝟝𝟞𝟟𝟠𝟡"),
    "sans": ("سنس", "0123456789", "𝟢𝟣𝟤𝟥𝟦𝟧𝟨𝟩𝟪𝟫"),
    "sans_bold": ("سنس ضخیم", "0123456789", "𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵"),
    "mono": ("تک‌عرض", "0123456789", "𝟶𝟷𝟸𝟹𝟺𝟻𝟼𝟽𝟾𝟿"),
    "full": ("فول‌ویدث", "0123456789", "０１２３４５６７８９"),
    "bubble": ("حبابی", "0123456789", "⓪①②③④⑤⑥⑦⑧⑨"),
    "parenthesis": ("پرانتزی", "0123456789", "⑴⑵⑶⑷⑸⑹⑺⑻⑼⑽"),
    "superscript": ("بالانویس", "0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹"),
    "subscript": ("زیرنویس", "0123456789", "₀₁₂₃₄₅₆₇₈₉"),
    "arabic": ("عربی", "0123456789", "٠١٢٣٤٥٦٧٨٩"),
    "persian": ("فارسی", "0123456789", "۰۱۲۳۴۵۶۷۸۹"),
    "devanagari": ("دواناگری", "0123456789", "०१२३४५६७८९"),
    "bengali": ("بنگالی", "0123456789", "০১২৩৪৫৬৭৮৯"),
    "gurmukhi": ("گورموخی", "0123456789", "੦੧੨੩੪੫੬੭੮੯"),
    "gujarati": ("گجراتی", "0123456789", "૦૧૨૩૪૫૬૭૮૯"),
    "oriya": ("اودیا", "0123456789", "୦୧୨୩୪୫୬୭୮୯"),
    "tamil": ("تامیل", "0123456789", "௦௧௨௩௪௫௬௭௮௯"),
    "telugu": ("تلوگو", "0123456789", "౦౧౨౩౪౫౬౭౮౯"),
    "smallcaps": ("اعداد کوچک", "0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹"),
    "smallcircle": ("دایره کوچک", "0123456789", "₀₁₂₃₄₅₆₇₈₉"),
}


def stylize_time(value: str, font: str) -> str:
    mapping = CLOCK_FONTS.get(font, CLOCK_FONTS["classic"])[2]
    return value.translate(str.maketrans("0123456789", mapping))

MESSAGE_FONTS = {
    "bold": ("𝐁 ضخیم", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz", "𝐀𝐁𝐂𝐃𝐄𝐅𝐆𝐇𝐈𝐉𝐊𝐋𝐌𝐍𝐎𝐏𝐐𝐑𝐒𝐓𝐔𝐕𝐖𝐗𝐘𝐙𝐚𝐛𝐜𝐝𝐞𝐟𝐠𝐡𝐢𝐣𝐤𝐥𝐦𝐧𝐨𝐩𝐪𝐫𝐬𝐭𝐮𝐯𝐰𝐱𝐲𝐳"),
    "double": ("𝔻 دوبل", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz", "𝔸𝔹ℂ𝔻𝔼𝔽𝔾ℍ𝕀𝕁𝕂𝕃𝕄ℕ𝕆ℙℚℝ𝕊𝕋𝕌𝕍𝕎𝕏𝕐ℤ𝕒𝕓𝕔𝕕𝕖𝕗𝕘𝕙𝕚𝕛𝕜𝕝𝕞𝕟𝕠𝕡𝕢𝕣𝕤𝕥𝕦𝕧𝕨𝕩𝕪𝕫"),
    "italic": ("𝘐 کج", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz", "𝐴𝐵𝐶𝐷𝐸𝐹𝐺𝐻𝐼𝐽𝐾𝐿𝑀𝑁𝑂𝑃𝑄𝑅𝑆𝑇𝑈𝑉𝑊𝑋𝑌𝑍𝑎𝑏𝑐𝑑𝑒𝑓𝑔ℎ𝑖𝑗𝑘𝑙𝑚𝑛𝑜𝑝𝑞𝑟𝑠𝑡𝑢𝑣𝑤𝑥𝑦𝑧"),
    "bolditalic": ("𝑩 ضخیم کج", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz", "𝑨𝑩𝑪𝑫𝑬𝑭𝑮𝑯𝑰𝑱𝑲𝑳𝑴𝑵𝑶𝑷𝑸𝑹𝑺𝑻𝑼𝑽𝑾𝑿𝒀𝒁𝒂𝒃𝒄𝒅𝒆𝒇𝒈𝒉𝒊𝒋𝒌𝒍𝒎𝒏𝒐𝒑𝒒𝒓𝒔𝒕𝒖𝒗𝒘𝒙𝒚𝒛"),
    "script": ("𝒜 دست‌نویس", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz", "𝒜ℬ𝒞𝒟ℰℱ𝒢ℋℐ𝒥𝒦ℒℳ𝒩𝒪𝒫𝒬ℛ𝒮𝒯𝒰𝒱𝒲𝒳𝒴𝒵𝒶𝒷𝒸𝒹ℯ𝒻ℊ𝒽𝒾𝒿𝓀𝓁𝓂𝓃ℴ𝓅𝓆𝓇𝓈𝓉𝓊𝓋𝓌𝓍𝓎𝓏"),
    "fraktur": ("𝔄 گوتیک", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz", "𝔄𝔅ℭ𝔇𝔈𝔉𝔊ℌℑ𝔍𝔎𝔏𝔐𝔑𝔒𝔓𝔔ℜ𝔖𝔗𝔘𝔙𝔚𝔛𝔜ℨ𝔞𝔟𝔠𝔡𝔢𝔣𝔤𝔥𝔦𝔧𝔨𝔩𝔪𝔫𝔬𝔭𝔮𝔯𝔰𝔱𝔲𝔳𝔴𝔵𝔶𝔷"),
    "sans": ("𝖠 سنس", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz", "𝖠𝖡𝖢𝖣𝖤𝖥𝖦𝖧𝖨𝖩𝖪𝖫𝖬𝖭𝖮𝖯𝖰𝖱𝖲𝖳𝖴𝖵𝖶𝖷𝖸𝖹𝖺𝖻𝖼𝖽𝖾𝖿𝗀𝗁𝗂𝗃𝗄𝗅𝗆𝗇𝗈𝗉𝗊𝗋𝗌𝗍𝗎𝗏𝗐𝗑𝗒𝗓"),
    "sansbold": ("𝗔 سنس ضخیم", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz", "𝗔𝗕𝗖𝗗𝗘𝗙𝗚𝗛𝗜𝗝𝗞𝗟𝗠𝗡𝗢𝗣𝗤𝗥𝗦𝗧𝗨𝗩𝗪𝗫𝗬𝗭𝗮𝗯𝗰𝗱𝗲𝗳𝗴𝗵𝗶𝗷𝗸𝗹𝗺𝗻𝗼𝗽𝗾𝗿𝘀𝘁𝘂𝘃𝘄𝘅𝘆𝘇"),
    "mono": ("𝙼 تک‌عرض", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz", "𝙰𝙱𝙲𝙳𝙴𝙵𝙶𝙷𝙸𝙹𝙺𝙻𝙼𝙽𝙾𝙿𝚀𝚁𝚂𝚃𝚄𝚅𝚆𝚇𝚈𝚉𝚊𝚋𝚌𝚍𝚎𝚏𝚐𝚑𝚒𝚓𝚔𝚕𝚖𝚗𝚘𝚙𝚚𝚛𝚜𝚝𝚞𝚟𝚠𝚡𝚢𝚣"),
    "circled": ("ⓐ حبابی", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz", "ⒶⒷⒸⒹⒺⒻⒼⒽⒾⒿⓀⓁⓂⓃⓄⓅⓆⓇⓈⓉⓊⓋⓌⓍⓎⓏⓐⓑⓒⓓⓔⓕⓖⓗⓘⓙⓚⓛⓜⓝⓞⓟⓠⓡⓢⓣⓤⓥⓦⓧⓨⓩ"),
    "fullwidth": ("Ａ پهن", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789", "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ０１２３４５６７８９"),
    "smallcaps": ("ᴀʙᴄ کوچک", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘQʀꜱᴛᴜᴠᴡXYZ"),
    "squared": ("🅰 مربعی", "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "🅰🅱🅲🅳🅴🅵🅶🅷🅸🅹🅺🅻🅼🅽🅾🅿🆀🆁🆂🆃🆄🆅🆆🆇🆈🆉"),
}
MESSAGE_FONT_FLOWS: dict[int, str] = {}
MANAGER_MENU_IDS: set[int] = set()

def apply_message_font(text: str, font: str) -> str:
    item=MESSAGE_FONTS.get(font)
    if not item: return text
    src,dst=item[1],item[2]
    return text.translate(str.maketrans(src,dst))

def main_kb(uid: int | None = None):
    # منوی اصلی: قابلیت‌های کاربردی + مدیریت کاربران فقط برای مدیر اصلی.
    rows = [
        [InlineKeyboardButton(text="📊 داشبورد", callback_data="dashboard"), InlineKeyboardButton(text="🩺 سلامت سیستم", callback_data="health")],
        [InlineKeyboardButton(text="🔴💎 الماس", callback_data="diamonds"), InlineKeyboardButton(text="🔐 وضعیت سلف", callback_data="self_status")],
        [InlineKeyboardButton(text="👤 کاربران", callback_data="self_users_menu"), InlineKeyboardButton(text="🛡 حریم و قوانین", callback_data="privacy")],
        [InlineKeyboardButton(text="📦 بکاپ‌گیری", callback_data="backup_panel"), InlineKeyboardButton(text="⏰ ساعت و پروفایل", callback_data="time")],
        [InlineKeyboardButton(text="🤖 اتوماسیون", callback_data="automation"), InlineKeyboardButton(text="🧠 هوشمند", callback_data="smart")],
        [InlineKeyboardButton(text="✉️ مدیریت پیام", callback_data="messages"), InlineKeyboardButton(text="✏️ پروفایل", callback_data="profile")],
        [InlineKeyboardButton(text="🛠 ابزارها", callback_data="tools"), InlineKeyboardButton(text="⏰ یادآوری‌ها", callback_data="reminders_panel")],
        [InlineKeyboardButton(text="📈 فعالیت من", callback_data="activity"), InlineKeyboardButton(text="🎨 دکمه‌ها", callback_data="buttons")],
    ]
    # فقط مدیر اصلی و مدیران جانشین پنل مدیریت را می‌بینند.
    # در ساخت کیبورد async نیستیم؛ دکمه برای مدیر اصلی همیشه دیده می‌شود و
    # جانشین‌ها از دستور /پنل وارد می‌شوند و داخل پنل به مدیریت دسترسی دارند.
    if uid == OWNER_ID or (uid is not None and uid != OWNER_ID and uid in MANAGER_MENU_IDS):
        rows.append([InlineKeyboardButton(text="👑 مدیریت کاربران", callback_data="users_menu")])
    rows.append([InlineKeyboardButton(text="✖️ بستن پنل", callback_data="close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

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

async def is_manager(uid: int) -> bool:
    """مدیر اصلی + مدیران جانشین ذخیره‌شده."""
    if uid == OWNER_ID:
        MANAGER_MENU_IDS.add(uid)
        return True
    raw = await get_setting("delegated_admin_ids", "")
    try:
        ids={int(x) for x in (raw or "").split(",") if x.strip().isdigit()}
        MANAGER_MENU_IDS.clear(); MANAGER_MENU_IDS.add(OWNER_ID); MANAGER_MENU_IDS.update(ids)
        return uid in ids
    except Exception:
        return False

async def delegated_admin_ids() -> list[int]:
    raw = await get_setting("delegated_admin_ids", "")
    return [int(x) for x in (raw or "").split(",") if x.strip().isdigit()]

async def set_delegated_admin_ids(ids: list[int]):
    await set_setting("delegated_admin_ids", ",".join(str(x) for x in sorted(set(ids))))

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
            "pending_phone":"VARCHAR(64) NULL", "access_requested":"BOOLEAN DEFAULT FALSE", "clock_enabled":"BOOLEAN DEFAULT FALSE", "clock_target":"VARCHAR(16) DEFAULT 'bio'", "clock_font":"VARCHAR(32) DEFAULT 'classic'", "clock_prefix":"VARCHAR(40) DEFAULT '🕐 '",
            "notifications":"BOOLEAN DEFAULT TRUE", "auto_reply_delay":"INTEGER DEFAULT 3", "auto_reply_cooldown":"INTEGER DEFAULT 60", "auto_reply_scope":"VARCHAR(16) DEFAULT 'private'", "auto_reply_keywords":"TEXT NULL", "animation_enabled":"BOOLEAN DEFAULT TRUE", "animation_style":"VARCHAR(32) DEFAULT 'نرم'", "message_count":"INTEGER DEFAULT 0", "last_seen":"TIMESTAMP NULL", "self_session":"TEXT NULL", "phone_masked":"VARCHAR(64) NULL", "self_enabled":"BOOLEAN DEFAULT FALSE", "smart_mode":"BOOLEAN DEFAULT FALSE", "name_lock":"BOOLEAN DEFAULT FALSE", "locked_name":"VARCHAR(255) NULL", "word_filter":"BOOLEAN DEFAULT FALSE", "word_filter_text":"TEXT NULL", "media_lock":"BOOLEAN DEFAULT FALSE", "comments_mode":"BOOLEAN DEFAULT FALSE", "spam_protection":"BOOLEAN DEFAULT FALSE", "auto_seen":"BOOLEAN DEFAULT FALSE", "button_theme":"VARCHAR(32) DEFAULT 'orange'", "private_lock":"BOOLEAN DEFAULT FALSE", "backup_enabled":"BOOLEAN DEFAULT FALSE", "backup_chat_id":"BIGINT NULL"
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



async def _ensure_backup_group(client, uid: int) -> int | None:
    """Create/find a private backup group owned by the user's self account."""
    async with Session() as s:
        u=await get_user(s,uid)
        if not u: return None
        existing=u.backup_chat_id
    if existing:
        try:
            await client.get_entity(existing)
            return int(existing)
        except Exception:
            pass
    try:
        result=await client(CreateChatRequest(users=[InputUserSelf()], title="بکاپ"))
        chats=getattr(result,"chats",None) or []
        if not chats: return None
        chat=chats[0]
        chat_id=int(getattr(chat,"id",0))
        if not chat_id: return None
        async with Session() as s:
            u=await get_user(s,uid)
            if u:
                u.backup_chat_id=chat_id
                await s.commit()
        return chat_id
    except Exception as e:
        log.warning("backup group create %s: %s",uid,e)
        return None

async def _backup_message(client, uid: int, message, backup_chat_id: int):
    try:
        # Forward keeps text/media/stickers/files/voice/etc. intact.
        await message.forward_to(backup_chat_id)
    except Exception as e:
        log.debug("backup forward %s: %s",uid,e)

async def _backup_recent(client, uid: int, chat_id: int, count: int, backup_chat_id: int):
    count=max(1,min(int(count),50))
    try:
        msgs=[m async for m in client.iter_messages(chat_id, limit=count)]
        msgs=list(reversed(msgs))
        if not msgs: return 0
        sent=0
        for msg in msgs:
            try:
                await msg.forward_to(backup_chat_id)
                sent+=1
            except Exception:
                continue
        return sent
    except Exception as e:
        log.warning("backup recent %s: %s",uid,e)
        return 0

async def _handle_self_backup_event(uid, event):
    """Backup private chats when enabled; also handles outgoing 'بکاپ N'."""
    try:
        if not getattr(event,"chat_id",None): return
        async with Session() as s:
            u=await get_user(s,uid)
            if not u or not u.self_enabled: return
            enabled=bool(u.backup_enabled)
            backup_chat_id=u.backup_chat_id
        text=(getattr(event.message,"message",None) or "").strip()
        import re
        m=re.fullmatch(r"بکاپ(?:\s+([0-9۰-۹]{1,2}))?",text,flags=re.IGNORECASE)
        if m and getattr(event,"out",False):
            raw=m.group(1) or "10"
            raw=raw.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹","0123456789"))
            count=max(1,min(int(raw),50))
            bid=await _ensure_backup_group(event.client,uid)
            if not bid:
                try: await event.reply("❌ گروه بکاپ ساخته نشد. دوباره گزینه بکاپ را روشن کن.")
                except Exception: pass
                return
            sent=await _backup_recent(event.client,uid,int(event.chat_id),count,bid)
            try: await event.reply(f"📦 {sent} پیام آخر در گروه بکاپ ذخیره شد.")
            except Exception: pass
            return
        # Automatic backup is intentionally limited to private chats.
        if not enabled or not getattr(event,"is_private",False): return
        if backup_chat_id is None:
            backup_chat_id=await _ensure_backup_group(event.client,uid)
        if backup_chat_id:
            await _backup_message(event.client,uid,event.message,backup_chat_id)
    except Exception as e:
        log.debug("backup event %s: %s",uid,e)

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
        if any(r.mode == "block" for r in sender_rules):
            return
        # قفل پیوی: فقط افراد دوست اجازه عبور دارند. برای پیام‌های ناشناس، در صورت امکان بلاک و حذف انجام می‌شود.
        async with Session() as s:
            u2=await get_user(s,uid)
            private_lock=bool(u2.private_lock) if u2 else False
        if private_lock and getattr(event,"is_private",False) and sender_id and sender_id != uid:
            if not any(r.mode == "allow" for r in sender_rules):
                try:
                    await event.client(BlockRequest(sender_id))
                except Exception: pass
                try:
                    await event.message.delete()
                except Exception: pass
                return
        
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

async def _self_panel_trigger(uid, event):
    """وقتی خود اکانت سلف در یک پیوی «پنل» می‌فرستد، مسیر امن باز کردن پنل بات را نشان می‌دهد.
    پنل کامل Bot API فقط داخل چت خود بات قابل نمایش است؛ تلگرام اجازه نمی‌دهد بات
    داخل پیوی دو کاربر پیام/کیبورد Bot API ارسال کند.
    """
    try:
        text_in = (event.raw_text or "").strip()
        if not has_panel_trigger(text_in):
            return
        if not event.is_private:
            return
        async with Session() as s:
            u = await get_user(s, uid)
            if not u or not u.self_enabled:
                return
        if not BOT_USERNAME:
            return
        try:
            await event.delete()
        except Exception:
            pass
        await event.client.send_message(
            event.chat_id,
            "🎛 <b>پنل مدیریت سلف</b>\n\nبرای باز کردن پنل کامل، روی دکمه زیر بزن:",
            buttons=[[Button.url("🎛 باز کردن پنل مدیریت", f"https://t.me/{BOT_USERNAME}")]],
        )
    except Exception as e:
        log.warning("self panel trigger %s: %s", uid, e)

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
                client.add_event_handler(lambda e, _uid=uid: _handle_self_backup_event(_uid, e), events.NewMessage(incoming=True))
                client.add_event_handler(lambda e, _uid=uid: _handle_self_backup_event(_uid, e), events.NewMessage(outgoing=True))
                client.add_event_handler(lambda e, _uid=uid: _self_panel_trigger(_uid, e), events.NewMessage(outgoing=True))
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
        await m.answer("👑 <b>پنل مدیریت اصلی</b>\n\nمدیر بدون نیاز به شماره و ورود سلف به همه بخش‌های مدیریتی دسترسی دارد.\n\nاز منوی زیر بات را مدیریت کن؛ هر بخش راهنمای فارسی دارد.",reply_markup=main_kb(OWNER_ID)); return

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
        await m.answer("🎛 <b>ورود قبلاً کامل شده است.</b>",reply_markup=main_kb(m.from_user.id))
    elif not phone:
        await ask_phone(m)
    else:
        await ask_phone(m)

async def ask_phone(m:Message):
    await m.answer("📱 <b>شماره اکانت تلگرامت اجباری است</b>\n\nشماره را همینجا به‌صورت متن بفرست.\n\nنمونه:\n<code>+989967066405</code>\n\n⚠️ شماره باید با + و کد کشور شروع شود. به‌محض دریافت شماره معتبر، کد ورود خودکار ارسال می‌شود.")

@dp.message(Command("panel"))
async def panel(m:Message):
    if m.from_user.id == OWNER_ID or await allowed(m.from_user.id):
        await m.answer(await panel_text(m.from_user.id),reply_markup=main_kb(m.from_user.id))
    else:
        await m.answer("⛔ ابتدا باید توسط مدیر تأیید شوید و ورود سلف را کامل کنید.")

def has_panel_trigger(text: str) -> bool:
    """پنل را به‌صورت کلمه در هر متن تشخیص می‌دهد؛ در همان چت پاسخ می‌دهد."""
    if not isinstance(text, str):
        return False
    import re
    t = text.strip().lower()
    # پنل، .پنل و /پنل به‌عنوان یک کلمه؛ همچنین اگر جمله‌ای مثل «سلام پنل» باشد.
    return bool(re.search(r"(?<!\w)(?:[./]?)پنل(?!\w)", t))

@dp.message(F.text.func(has_panel_trigger))
async def panel_word(m:Message):
    # میانبر پنل در هر چتی؛ پاسخ در همان چتی ارسال می‌شود که میانبر در آن فرستاده شده است.
    if m.from_user.id == OWNER_ID or await allowed(m.from_user.id):
        await m.answer(await panel_text(m.from_user.id),reply_markup=main_kb(m.from_user.id))
    else:
        await m.answer("⛔ دسترسی پنل برای شما فعال نیست. ابتدا از /start درخواست دسترسی بده.")

@dp.message(Command("admin"))
async def admin_panel(m:Message):
    # میانبر مطمئن برای مدیر؛ هیچ شماره‌ای برای باز کردن پنل لازم نیست.
    if m.from_user.id != OWNER_ID:
        await m.answer("⛔ این دستور فقط برای مدیر اصلی است.")
        return
    await m.answer("👑 <b>پنل مدیریت اصلی</b>\n\nمدیر برای مدیریت بات نیازی به شماره یا ورود سلف ندارد.",reply_markup=main_kb(OWNER_ID))

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
    raw_text=(m.text or "").strip()
    if uid in MESSAGE_FONT_FLOWS:
        font=MESSAGE_FONT_FLOWS.pop(uid)
        if raw_text:
            await m.answer(apply_message_font(raw_text,font))
        else:
            await m.answer("⚠️ یک متن بفرست.")
        return
    # این مسیر قبل از منطق ورود اجرا می‌شود تا پنل و انتقال الماس بلعیده نشوند.
    if has_panel_trigger(raw_text):
        if await is_manager(uid) or await allowed(uid):
            await m.answer(await panel_text(uid),reply_markup=main_kb(m.from_user.id))
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
    await m.answer("🎛 <b>پنل حرفه‌ای آماده است</b>",reply_markup=main_kb(uid))

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
    await m.answer(f"🔐 <b>وضعیت سلف</b>\n\nفعال: {'✅' if u and u.self_enabled else '❌'}\nشماره: {escape(u.phone_masked or 'ثبت نشده') if u else 'ثبت نشده'}\n🔴💎 موجودی الماس: <b>{balance}</b>\nاعتبار: <b>{expiry}</b>\nساعت زنده: {'✅' if u and u.clock_enabled else '❌'}")

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


@dp.message(Command("autoseen"))
async def autoseen_cmd(m: Message):
    if not await allowed(m.from_user.id): return
    arg=(m.text or "").split(maxsplit=1)
    enabled = len(arg)==1 or arg[1].strip().lower() in {"on","1","روشن","فعال"}
    if len(arg)>1 and arg[1].strip().lower() in {"off","0","خاموش","غیرفعال"}: enabled=False
    async with Session() as s:
        u=await get_user(s,m.from_user.id); u.auto_seen=enabled; await s.commit()
    await m.answer(f"👁 سین خودکار: <b>{'روشن' if enabled else 'خاموش'}</b>")

@dp.message(Command("autoreplydelay"))
async def autoreplydelay_cmd(m: Message):
    if not await allowed(m.from_user.id): return
    parts=(m.text or "").split()
    if len(parts)!=2 or not parts[1].isdigit():
        await m.answer("فرمت: <code>/autoreplydelay 3</code>\nحداکثر ۳۰ ثانیه."); return
    sec=max(0,min(int(parts[1]),30))
    async with Session() as s:
        u=await get_user(s,m.from_user.id); u.auto_reply_delay=sec; await s.commit()
    await m.answer(f"⏱ تأخیر پاسخ خودکار روی <b>{sec}</b> ثانیه تنظیم شد.")

@dp.message(Command("autoreplycooldown"))
async def autoreplycooldown_cmd(m: Message):
    if not await allowed(m.from_user.id): return
    parts=(m.text or "").split()
    if len(parts)!=2 or not parts[1].isdigit():
        await m.answer("فرمت: <code>/autoreplycooldown 60</code>\nحداقل ۳۰ ثانیه."); return
    sec=max(30,min(int(parts[1]),86400))
    async with Session() as s:
        u=await get_user(s,m.from_user.id); u.auto_reply_cooldown=sec; await s.commit()
    await m.answer(f"🛡 فاصله پاسخ خودکار روی <b>{sec}</b> ثانیه تنظیم شد.")

@dp.message(Command("autoscope"))
async def autscope_cmd(m: Message):
    if not await allowed(m.from_user.id): return
    parts=(m.text or "").split()
    if len(parts)!=2 or parts[1].lower() not in {"private","all"}:
        await m.answer("فرمت: <code>/autoscope private</code> یا <code>/autoscope all</code>"); return
    scope=parts[1].lower()
    async with Session() as s:
        u=await get_user(s,m.from_user.id); u.auto_reply_scope=scope; await s.commit()
    await m.answer("🌐 محدوده پاسخ خودکار: <b>خصوصی</b>" if scope=="private" else "🌐 محدوده پاسخ خودکار: <b>همه چت‌ها</b>")

@dp.message(Command("keywordreply"))
async def keywordreply_cmd(m: Message):
    if not await allowed(m.from_user.id): return
    raw=(m.text or "").split(maxsplit=1)
    if len(raw)==1:
        await m.answer("فرمت: <code>/keywordreply سلام=>سلام! | قیمت=>لطفاً صبر کن</code>\nبرای حذف همه: <code>/keywordreply clear</code>"); return
    val=raw[1].strip()
    async with Session() as s:
        u=await get_user(s,m.from_user.id)
        if val.lower()=="clear": u.auto_reply_keywords=None
        else:
            pairs=[x.strip() for x in val.split("|") if "=>" in x]
            if not pairs: await m.answer("❌ حداقل یک جفت کلمه=>پاسخ وارد کن."); return
            u.auto_reply_keywords=" | ".join(pairs[:10])
        await s.commit()
    await m.answer("✅ پاسخ‌های کلمه‌ای ذخیره شد.")

@dp.message(Command("automation"))
async def automation_cmd(m: Message):
    if not await allowed(m.from_user.id): return
    await m.answer("🤖 <b>اتوماسیون سلف</b>\n\n👁 سین خودکار: /autoseen on|off\n💬 پاسخ خودکار: /autoreply متن\n⏱ تأخیر: /autoreplydelay 3\n🛡 فاصله پاسخ: /autoreplycooldown 60\n🌐 محدوده: /autoscope private|all\n🔎 پاسخ کلمه‌ای: /keywordreply کلمه=>پاسخ | کلمه2=>پاسخ2\n👍 ریکت: /reaction ❤️\n\nهمه پاسخ‌های خودکار دارای محدودکننده سرعت هستند.")

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
async def main_cb(c:CallbackQuery): await c.message.edit_text("🎛 <b>منوی اصلی</b>\n\nهر بخش راهنمای داخلی دارد.",reply_markup=main_kb(c.from_user.id)); await c.answer()
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
"backup":("📦 بکاپ","با روشن شدن بکاپ، گروه خصوصی «بکاپ» خودکار ساخته می‌شود و پیام‌های چت‌های خصوصی ذخیره می‌شوند. در همان چت بنویس «بکاپ ۱۰» تا ۱۰ پیام آخر به گروه بکاپ فرستاده شود. سقف هر درخواست ۵۰ پیام است."),
"self_users":("👤 کاربران","دشمن/دوست برای افراد، دشمن گروه/دوست گروه برای چت‌ها، قفل پیوی و بلاک در این بخش مدیریت می‌شوند."),
"spam":("💣 اسپم","حالت ضداسپم فقط رفتارهای تکراری پنل را محدود می‌کند و برای ارسال انبوه استفاده نمی‌شود."),
"currencies":("💰 ارزها","نرخ‌های ارز و طلا از لینک‌های زنده باز می‌شوند."),
"tagall":("📢 تگ همه","تگ انبوه محدود شده است تا از ارسال مزاحم جلوگیری شود."),
"buttons":("🎨 دکمه‌ها","منوی فارسی و توضیح هر بخش برای استفاده راحت آماده شده است."),
"diamonds":("🔴💎 الماس",f"با تأیید مدیر، <b>{WELCOME_DIAMONDS} الماس هدیه</b> می‌گیری. برای شروع سلف فقط <b>{SELF_ACTIVATION_COST} الماس</b> همان لحظه کم می‌شود. اعتبار هر دوره <b>۳۰ روز</b> است و تا <b>{SELF_MONTH_DIAMONDS:,} الماس</b> به‌صورت ساعتی مصرف می‌شود؛ ۱۰۰۰ الماس یکجا صفر نمی‌شود. مدیر موجودی نامحدود دارد. قیمت هر ۱۰۰ الماس <b>۱۰٬۰۰۰ تومان</b> است و خرید از {DIAMOND_ADMIN_USERNAME} انجام می‌شود."),
"self_users":("👥 سلف‌های فعال","فهرست کاربرانی که ورود سلفشان با موفقیت انجام شده است. مدیر می‌تواند نشست سلف هر کاربر را حذف و دسترسی سلف او را لغو کند."),
"help_all":("❓ راهنمای کامل","مسیر ورود: درخواست دسترسی ← تأیید مدیر ← ورود اجباری شماره ← ارسال کد ← ارسال کد به‌صورت متن ← تأیید نهایی. بعد از ورود، هر بخش پنل راهنمای داخلی دارد."),
}

@dp.callback_query(F.data=="automation")
async def automation_page(c: CallbackQuery):
    async with Session() as s:
        u=await get_user(s,c.from_user.id)
        if not u: await c.answer("کاربر پیدا نشد",show_alert=True); return
        body=(f"🤖 <b>اتوماسیون سلف</b>\n\n"
              f"👁 سین خودکار: <b>{'روشن' if u.auto_seen else 'خاموش'}</b>\n"
              f"💬 پاسخ خودکار: <b>{'روشن' if u.auto_reply else 'خاموش'}</b>\n"
              f"⏱ تأخیر: <b>{int(u.auto_reply_delay or 0)} ثانیه</b>\n"
              f"🛡 فاصله پاسخ: <b>{int(u.auto_reply_cooldown or 60)} ثانیه</b>\n"
              f"🌐 محدوده: <b>{'همه چت‌ها' if u.auto_reply_scope=='all' else 'خصوصی'}</b>\n"
              f"🔎 پاسخ کلمه‌ای: <b>{'تنظیم شده' if u.auto_reply_keywords else 'خاموش'}</b>\n"
              f"👍 ریکت: <b>{escape(u.auto_reaction or 'خاموش')}</b>")
    kb=[
        [InlineKeyboardButton(text="👁 سین روشن",callback_data="auto_seen:on"),InlineKeyboardButton(text="🔴 سین خاموش",callback_data="auto_seen:off")],
        [InlineKeyboardButton(text="🌐 فقط خصوصی",callback_data="auto_scope:private"),InlineKeyboardButton(text="🌍 همه چت‌ها",callback_data="auto_scope:all")],
        [InlineKeyboardButton(text="📖 راهنمای دستورات",callback_data="help:automation")],
        [InlineKeyboardButton(text="⬅️ منوی اصلی",callback_data="main")]]
    await c.message.edit_text(body,reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)); await c.answer()

@dp.callback_query(F.data.startswith("auto_seen:"))
async def auto_seen_cb(c: CallbackQuery):
    val=c.data.split(":",1)[1]=="on"
    async with Session() as s:
        u=await get_user(s,c.from_user.id)
        if not u: await c.answer("کاربر پیدا نشد",show_alert=True); return
        u.auto_seen=val; await s.commit()
    await c.answer("سین خودکار به‌روزرسانی شد")
    await automation_page(c)

@dp.callback_query(F.data.startswith("auto_scope:"))
async def auto_scope_cb(c: CallbackQuery):
    scope=c.data.split(":",1)[1]
    async with Session() as s:
        u=await get_user(s,c.from_user.id)
        if not u: await c.answer("کاربر پیدا نشد",show_alert=True); return
        u.auto_reply_scope=scope; await s.commit()
    await c.answer("محدوده ذخیره شد")
    await automation_page(c)


@dp.callback_query(F.data=="self_users_menu")
async def self_users_menu(c:CallbackQuery):
    if not await allowed(c.from_user.id): return await c.answer("دسترسی ندارید",show_alert=True)
    async with Session() as s:
        u=await get_user(s,c.from_user.id)
        private_lock=bool(u.private_lock) if u else False
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="😈 دشمن",callback_data="rule:enemy_user"),InlineKeyboardButton(text="💚 دوست",callback_data="rule:friend_user")],
        [InlineKeyboardButton(text="😈 دشمن گروه",callback_data="rule:enemy_chat"),InlineKeyboardButton(text="💚 دوست گروه",callback_data="rule:friend_chat")],
        [InlineKeyboardButton(text="🔒 قفل پیوی",callback_data="private_lock:on"),InlineKeyboardButton(text="🔓 باز پیوی",callback_data="private_lock:off")],
        [InlineKeyboardButton(text="🚫 بلاک",callback_data="rule:block")],
        [InlineKeyboardButton(text="📖 راهنما",callback_data="help:self_users"),InlineKeyboardButton(text="⬅️ منو",callback_data="main")],
    ])
    await c.message.edit_text(f"👤 <b>کاربران</b>\n\n🔒 قفل پیوی: <b>{'روشن' if private_lock else 'خاموش'}</b>\n\nاز این بخش دوست/دشمن، قوانین گروه و قفل پیام خصوصی را مدیریت کن. برای دشمن و دوست، روی پیام کاربر ریپلای کن یا دستور مربوطه را بفرست.",reply_markup=kb); await c.answer()

@dp.callback_query(F.data.startswith("rule:"))
async def rule_hint(c:CallbackQuery):
    key=c.data.split(":",1)[1]
    texts={
      "enemy_user":"😈 <b>دشمن</b>\n\nروی پیام کاربر ریپلای کن و <code>/دشمن</code> بفرست؛ یا <code>/دشمن @username</code>.",
      "friend_user":"💚 <b>دوست</b>\n\nروی پیام کاربر ریپلای کن و <code>/دوست</code> بفرست؛ یا <code>/دوست @username</code>.",
      "enemy_chat":"😈 <b>دشمن گروه</b>\n\nداخل همان گروه <code>/دشمن_گروه</code> بفرست تا آن گروه برای اتوماسیون مسدود شود.",
      "friend_chat":"💚 <b>دوست گروه</b>\n\nداخل همان گروه <code>/دوست_گروه</code> بفرست تا آن گروه مجاز شود.",
      "block":"🚫 <b>بلاک</b>\n\nروی پیام کاربر ریپلای کن و <code>/block</code> بفرست یا <code>/block @username</code>. رفع بلاک: <code>/unblock @username</code>.",
    }
    await c.message.edit_text(texts[key],reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ کاربران",callback_data="self_users_menu")]])); await c.answer()

@dp.callback_query(F.data.startswith("private_lock:"))
async def private_lock_cb(c:CallbackQuery):
    val=c.data.split(":",1)[1]=="on"
    async with Session() as s:
        u=await get_user(s,c.from_user.id)
        if not u: return await c.answer("کاربر پیدا نشد",show_alert=True)
        u.private_lock=val; await s.commit()
    await c.answer("🔒 قفل پیوی روشن شد" if val else "🔓 پیوی باز شد",show_alert=True)
    await self_users_menu(c)

@dp.callback_query(F.data=="backup_panel")
async def backup_panel(c:CallbackQuery):
    if not await allowed(c.from_user.id): return await c.answer("دسترسی ندارید",show_alert=True)
    async with Session() as s:
        u=await get_user(s,c.from_user.id)
        enabled=bool(u.backup_enabled) if u else False
        gid=u.backup_chat_id if u else None
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 بکاپ روشن",callback_data="backup:on"),InlineKeyboardButton(text="🔴 بکاپ خاموش",callback_data="backup:off")],
        [InlineKeyboardButton(text="📜 بکاپ ۱۰",callback_data="backup:hint10")],
        [InlineKeyboardButton(text="📖 راهنما",callback_data="help:backup"),InlineKeyboardButton(text="⬅️ منو",callback_data="main")],
    ])
    status="روشن 🟢" if enabled else "خاموش 🔴"
    group="ساخته شده ✅" if gid else "هنوز ساخته نشده"
    await c.message.edit_text(f"📦 <b>بکاپ‌گیری سلف</b>\n\nوضعیت: <b>{status}</b>\nگروه بکاپ: <b>{group}</b>\n\nبا روشن کردن بکاپ، سلف به‌صورت خودکار یک گروه با نام <b>بکاپ</b> می‌سازد و پیام‌های چت‌های خصوصی را در آن ذخیره می‌کند.\n\nدر هر گروه یا پیوی می‌توانی از داخل همان حساب سلف بنویسی: <code>بکاپ ۱۰</code> تا ۱۰ پیام آخر همان چت به گروه بکاپ فرستاده شود. برای جلوگیری از فشار، سقف هر درخواست ۵۰ پیام است.",reply_markup=kb); await c.answer()

@dp.callback_query(F.data.startswith("backup:"))
async def backup_toggle(c:CallbackQuery):
    val=c.data.split(":",1)[1]
    if val=="hint10":
        await c.answer("در همان چت سلف بنویس: بکاپ ۱۰",show_alert=True); return
    enabled=val=="on"
    if enabled:
        client=await get_self_client(c.from_user.id)
        if not client: return await c.answer("❌ ابتدا سلف را فعال کن.",show_alert=True)
        try:
            gid=await _ensure_backup_group(client,c.from_user.id)
            if not gid: return await c.answer("❌ ساخت گروه بکاپ ناموفق بود.",show_alert=True)
            async with Session() as s:
                u=await get_user(s,c.from_user.id); u.backup_enabled=True; u.backup_chat_id=gid; await s.commit()
            await c.answer("📦 بکاپ روشن شد و گروه «بکاپ» ساخته شد.",show_alert=True)
        finally:
            try: await client.disconnect()
            except Exception: pass
    else:
        async with Session() as s:
            u=await get_user(s,c.from_user.id)
            if u: u.backup_enabled=False; await s.commit()
        await c.answer("📦 بکاپ خاموش شد.",show_alert=True)
    await backup_panel(c)

@dp.callback_query(F.data=="dashboard")
async def dashboard_page(c:CallbackQuery):
    if not await allowed(c.from_user.id): return await c.answer("دسترسی ندارید",show_alert=True)
    async with Session() as s:
        u=await get_user(s,c.from_user.id)
        tx=(await s.execute(select(func.count(DiamondTransaction.id)).where(DiamondTransaction.user_id==u.id))).scalar() or 0
        rem=(await s.execute(select(func.count(Reminder.id)).where(Reminder.user_id==u.id,Reminder.done==False))).scalar() or 0
        rules=(await s.execute(select(func.count(SenderRule.id)).where(SenderRule.user_id==u.id))).scalar() or 0
        chats=(await s.execute(select(func.count(ChatRule.id)).where(ChatRule.user_id==u.id))).scalar() or 0
    body=(f"📊 <b>داشبورد شخصی</b>\n\n🔐 سلف: <b>{'فعال 🟢' if u.self_enabled else 'خاموش 🔴'}</b>\n"
          f"⏳ زمان: <b>{escape(self_remaining_text(u))}</b>\n🔴💎 الماس: <b>{'∞' if u.id==OWNER_ID else u.diamonds}</b>\n"
          f"💬 پیام‌های ثبت‌شده: <b>{u.message_count or 0}</b>\n⏰ یادآوری فعال: <b>{rem}</b>\n"
          f"👤 قوانین کاربر: <b>{rules}</b>\n👥 قوانین گروه: <b>{chats}</b>\n💳 تراکنش الماس: <b>{tx}</b>\n"
          f"🕐 آخرین فعالیت: <b>{escape(str(u.last_seen or '—'))}</b>")
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 بروزرسانی",callback_data="dashboard"),InlineKeyboardButton(text="📈 فعالیت",callback_data="activity")],[InlineKeyboardButton(text="🩺 سلامت",callback_data="health"),InlineKeyboardButton(text="🔐 وضعیت سلف",callback_data="self_status")],[InlineKeyboardButton(text="⬅️ منو",callback_data="main")]])
    await c.message.edit_text(body,reply_markup=kb); await c.answer("📊 بروزرسانی شد")

@dp.callback_query(F.data=="activity")
async def activity_page(c:CallbackQuery):
    if not await allowed(c.from_user.id): return await c.answer("دسترسی ندارید",show_alert=True)
    async with Session() as s: u=await get_user(s,c.from_user.id)
    body=f"📈 <b>فعالیت حساب</b>\n\n💬 پیام‌های ثبت‌شده: <b>{u.message_count or 0}</b>\n🕐 آخرین فعالیت: <b>{escape(str(u.last_seen or '—'))}</b>\n🤖 پاسخ خودکار: <b>{'روشن' if u.auto_reply else 'خاموش'}</b>\n👍 ریکت: <b>{escape(u.auto_reaction or 'خاموش')}</b>\n👁 سین خودکار: <b>{'روشن' if u.auto_seen else 'خاموش'}</b>\n📦 بکاپ: <b>{'روشن' if u.backup_enabled else 'خاموش'}</b>"
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 بروزرسانی",callback_data="activity")],[InlineKeyboardButton(text="⬅️ منو",callback_data="main")]])
    await c.message.edit_text(body,reply_markup=kb); await c.answer()

@dp.callback_query(F.data=="health")
async def health_page(c:CallbackQuery):
    if not await allowed(c.from_user.id): return await c.answer("دسترسی ندارید",show_alert=True)
    missing=self_config_status()
    async with Session() as s:
        u=await get_user(s,c.from_user.id); users=(await s.execute(select(func.count(User.id)))).scalar() or 0; active=(await s.execute(select(func.count(User.id)).where(User.self_enabled==True))).scalar() or 0; pending=(await s.execute(select(func.count(User.id)).where(User.access_requested==True,User.approved==False))).scalar() or 0
    cfg='🟢 کامل' if not missing else '🔴 ناقص: '+', '.join(missing)
    await c.message.edit_text(f"🩺 <b>سلامت سیستم</b>\n\n⚙️ تنظیمات سلف: <b>{escape(cfg)}</b>\n🔐 نشست من: <b>{'فعال 🟢' if u and u.self_enabled else 'خاموش 🔴'}</b>\n👥 کاربران: <b>{users}</b>\n🟢 سلف‌های فعال: <b>{active}</b>\n🕐 درخواست‌های در انتظار: <b>{pending}</b>\n🗄️ دیتابیس: <b>قابل دسترس</b>",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 تست دوباره",callback_data="health")],[InlineKeyboardButton(text="⬅️ منو",callback_data="main")]])); await c.answer("🩺 تست انجام شد")

@dp.callback_query(F.data=="privacy")
async def privacy_page(c:CallbackQuery):
    if not await allowed(c.from_user.id): return await c.answer("دسترسی ندارید",show_alert=True)
    async with Session() as s: u=await get_user(s,c.from_user.id)
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"🔒 قفل پیوی {'🟢' if u.private_lock else '🔴'}",callback_data=f"private_lock:{'off' if u.private_lock else 'on'}")],[InlineKeyboardButton(text=f"🔒 قفل رسانه {'🟢' if u.media_lock else '🔴'}",callback_data=f"toggle:media:{'off' if u.media_lock else 'on'}")],[InlineKeyboardButton(text=f"🔎 فیلتر کلمات {'🟢' if u.word_filter else '🔴'}",callback_data="filter"),InlineKeyboardButton(text=f"🛡 ضداسپم {'🟢' if u.spam_protection else '🔴'}",callback_data="spam")],[InlineKeyboardButton(text=f"🔔 اعلان {'🟢' if u.notifications else '🔴'}",callback_data="public:notify"),InlineKeyboardButton(text=f"👁 سین {'🟢' if u.auto_seen else '🔴'}",callback_data="public:seen")],[InlineKeyboardButton(text="📖 راهنما",callback_data="help:public"),InlineKeyboardButton(text="⬅️ منو",callback_data="main")]])
    await c.message.edit_text("🛡 <b>مرکز حریم و قوانین</b>\n\nتنظیمات محافظتی حساب را از اینجا کنترل کن.",reply_markup=kb); await c.answer()

@dp.callback_query(F.data=="reminders_panel")
async def reminders_panel(c:CallbackQuery):
    if not await allowed(c.from_user.id): return await c.answer("دسترسی ندارید",show_alert=True)
    async with Session() as s: rows=(await s.execute(select(Reminder).where(Reminder.user_id==c.from_user.id,Reminder.done==False).order_by(Reminder.due_at).limit(12))).scalars().all()
    lines=["⏰ <b>یادآوری‌ها</b>",""]; kb=[]
    if not rows: lines.append("هیچ یادآوری فعالی نداری.")
    for r in rows:
        lines.append(f"#{r.id} — {escape(r.text[:80])}\n⏱ {escape(r.due_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}"); kb.append([InlineKeyboardButton(text=f"🗑 حذف #{r.id}",callback_data=f"remdel:{r.id}")])
    lines += ["", "➕ ساخت: <code>/remind 10m متن</code>", "📋 فهرست: <code>/reminders</code>"]
    kb += [[InlineKeyboardButton(text="🔄 بروزرسانی",callback_data="reminders_panel")],[InlineKeyboardButton(text="⬅️ منو",callback_data="main")]]
    await c.message.edit_text("\n".join(lines),reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)); await c.answer()

@dp.callback_query(F.data.startswith("remdel:"))
async def reminder_delete_cb(c:CallbackQuery):
    rid=int(c.data.split(":",1)[1])
    async with Session() as s:
        r=await s.scalar(select(Reminder).where(Reminder.id==rid,Reminder.user_id==c.from_user.id))
        if not r: return await c.answer("یادآوری پیدا نشد",show_alert=True)
        r.done=True; await s.commit()
    await c.answer("🗑 حذف شد",show_alert=True); await reminders_panel(c)

@dp.callback_query(F.data=="session_tools")
async def session_tools_page(c:CallbackQuery):
    if not await allowed(c.from_user.id): return await c.answer("دسترسی ندارید",show_alert=True)
    async with Session() as s: u=await get_user(s,c.from_user.id)
    task=AUTOMATION_TASKS.get(c.from_user.id)
    await c.message.edit_text(f"🔐 <b>مرکز نشست سلف</b>\n\nوضعیت نشست: <b>{'فعال 🟢' if u.self_enabled else 'خاموش 🔴'}</b>\nاتصال اتوماسیون: <b>{'متصل 🟢' if task and not task.done() else 'متوقف ⚪'}</b>\n\nاز اینجا اتصال اتوماسیون را دوباره راه‌اندازی یا وضعیت را بررسی کن.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 اتصال مجدد",callback_data="session_reconnect")],[InlineKeyboardButton(text="🔐 وضعیت سلف",callback_data="self_status"),InlineKeyboardButton(text="⏰ ساعت",callback_data="time")],[InlineKeyboardButton(text="⬅️ منو",callback_data="main")]])); await c.answer()

@dp.callback_query(F.data=="session_reconnect")
async def session_reconnect(c:CallbackQuery):
    async with Session() as s: u=await get_user(s,c.from_user.id)
    if not u or not u.self_enabled: return await c.answer("ابتدا سلف را فعال کن.",show_alert=True)
    stop_automation(c.from_user.id); start_automation(c.from_user.id)
    await c.answer("🔄 اتصال مجدد شروع شد",show_alert=True); await session_tools_page(c)

@dp.callback_query(F.data=="help_all")
async def help_all_cb(c:CallbackQuery):
    await c.message.edit_text("📚 <b>راهنمای کامل پنل</b>\n\n🎛 منوی اصلی: داشبورد، سلامت، الماس، کاربران، حریم، بکاپ، ساعت، اتوماسیون، پیام، پروفایل و ابزارها.\n\n🔐 ورود سلف: درخواست دسترسی ← تأیید مدیر ← شماره ← کد متنی ← در صورت نیاز رمز دومرحله‌ای.\n\n⚙️ برای هر قابلیت، دکمه «📖 راهنما» همان صفحه را بزن.\n\n⚠️ کد ورود، رمز دومرحله‌ای، API Hash و Session را برای هیچ‌کس ارسال نکن.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ منو",callback_data="main")]])); await c.answer()

@dp.callback_query(F.data=="utilities")
async def utilities_page(c:CallbackQuery):
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 اتوماسیون",callback_data="automation"),InlineKeyboardButton(text="🧠 هوشمند",callback_data="smart")],
        [InlineKeyboardButton(text="🖼 پروفایل",callback_data="profile"),InlineKeyboardButton(text="✍️ استایل متن",callback_data="text_style")],
        [InlineKeyboardButton(text="🔒 رسانه",callback_data="media"),InlineKeyboardButton(text="🚫 فیلتر کلمات",callback_data="filter")],
        [InlineKeyboardButton(text="😈 دشمنان",callback_data="enemies"),InlineKeyboardButton(text="👍 ریکت",callback_data="reaction")],
        [InlineKeyboardButton(text="🎭 اکشن‌ها",callback_data="actions"),InlineKeyboardButton(text="💬 کامنت",callback_data="comments")],
        [InlineKeyboardButton(text="📌 عمومی",callback_data="public"),InlineKeyboardButton(text="🛡 حفاظت اسم",callback_data="namelock")],
        [InlineKeyboardButton(text="📢 تگ همه",callback_data="tagall"),InlineKeyboardButton(text="🎨 دکمه‌ها",callback_data="buttons")],
        [InlineKeyboardButton(text="🔮 فال",callback_data="fortune"),InlineKeyboardButton(text="🔐 متن رمزی",callback_data="secret")],
        [InlineKeyboardButton(text="💣 ضداسپم",callback_data="spam"),InlineKeyboardButton(text="💰 ارزها",callback_data="currencies")],
        [InlineKeyboardButton(text="📊 فعالیت",callback_data="activity"),InlineKeyboardButton(text="🩺 سلامت",callback_data="health")],
        [InlineKeyboardButton(text="⏰ یادآوری",callback_data="reminders_panel"),InlineKeyboardButton(text="🛡 حریم",callback_data="privacy")],
        [InlineKeyboardButton(text="⬅️ منوی اصلی",callback_data="main")],
    ])
    await c.message.edit_text("🛠 <b>مرکز قابلیت‌ها</b>\n\nاینجا ابزارهای واقعی پنل را یکجا داری؛ هر دکمه یا تنظیم را اجرا می‌کند یا راهنمای دقیق همان عملیات را نشان می‌دهد.",reply_markup=kb)
    await c.answer()

@dp.callback_query(F.data=="self_status")
async def self_status_page(c:CallbackQuery):
    # این callback عمداً self-contained است تا روی هر پیام پنل (گروه یا پیوی) باز شود.
    try:
        async with Session() as s:
            u=await get_user(s,c.from_user.id)
            if not u:
                await c.answer("کاربر پیدا نشد؛ اول /start را بزن.",show_alert=True); return
            status="🟢 فعال" if u.self_enabled else "🔴 خاموش"
            clock="🟢 روشن" if u.clock_enabled else "⚫ خاموش"
            remaining=self_remaining_text(u)
            body=(
                "◼️ <b>وضعیت سلف</b> ◼️\n\n"
                f"🔐 وضعیت: <b>{status}</b>\n"
                f"⏳ زمان باقی‌مانده: <b>{escape(remaining)}</b>\n"
                f"⏰ ساعت پروفایل: <b>{clock}</b>\n"
                f"🎯 مقصد ساعت: <b>{escape(u.clock_target or '—')}</b>\n"
                f"🔤 فونت: <b>{escape(CLOCK_FONTS.get(u.clock_font, ('—',))[0])}</b>\n"
                f"🌍 منطقه زمانی: <b>{escape(u.timezone or DEFAULT_TZ)}</b>\n"
            )
        kb=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◼️ ⏰ تنظیم ساعت",callback_data="time")],
            [InlineKeyboardButton(text="◼️ 🔄 بروزرسانی",callback_data="self_status")],
            [InlineKeyboardButton(text="🔐 ابزار نشست",callback_data="session_tools")],
            [InlineKeyboardButton(text="◻️ ⬅️ منوی اصلی",callback_data="main")],
        ])
        try:
            await c.message.edit_text(body,reply_markup=kb)
        except Exception:
            # اگر پیام قبلی قابل ویرایش نبود، پنل را در همان چت دوباره می‌فرستیم.
            await c.message.answer(body,reply_markup=kb)
        await c.answer("✅ وضعیت سلف")
    except Exception as e:
        await c.answer("❌ خطا در نمایش وضعیت سلف. دوباره امتحان کن.",show_alert=True)

@dp.callback_query(F.data=="time")
async def time_page(c:CallbackQuery): await c.message.edit_text("⏰ <b>ساعت زنده و پروفایل</b>\n\nساعت زنده را می‌توانی روی <b>بیو</b>، <b>نام پروفایل</b> یا <b>هر دو</b> قرار بدهی. ۲۰ مدل فونت ساعت هم برای انتخاب داری.\n\nراهنما: منطقه زمانی را انتخاب کن، فونت را بزن، مقصد نمایش را مشخص کن و بعد ساعت را روشن کن.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📖 راهنمای این بخش",callback_data="help:time")]]+clock_kb().inline_keyboard)); await c.answer()
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
        [InlineKeyboardButton(text="👑 مدیران جانشین",callback_data="delegated_admins")],
        [InlineKeyboardButton(text="📖 راهنمای این بخش",callback_data="help:users_menu"),InlineKeyboardButton(text="⬅️ منو",callback_data="main")]
    ])

@dp.callback_query(F.data=="users_menu")
async def users_menu(c:CallbackQuery):
    if not await is_manager(c.from_user.id):
        await c.answer("این بخش فقط برای مدیر است",show_alert=True); return
    async with Session() as s:
        total=await s.scalar(select(func.count(User.id))) or 0
        pending=await s.scalar(select(func.count(User.id)).where(User.approved==False,User.banned==False)) or 0
        approved=await s.scalar(select(func.count(User.id)).where(User.approved==True,User.banned==False)) or 0
        banned=await s.scalar(select(func.count(User.id)).where(User.banned==True)) or 0
        active=await s.scalar(select(func.count(User.id)).where(User.self_enabled==True)) or 0
    body=(f"👤 <b>مدیریت کامل کاربران</b>\n\n"
          f"👥 کل کاربران: <b>{int(total):,}</b>\n"
          f"✅ تأییدشده: <b>{int(approved):,}</b>\n"
          f"⏳ در انتظار: <b>{int(pending):,}</b>\n"
          f"🚫 مسدود: <b>{int(banned):,}</b>\n"
          f"🔐 سلف فعال: <b>{int(active):,}</b>\n\n"
          "از «همه کاربران» وارد اطلاعات کامل هر نفر شو و از همان‌جا دسترسی، مسدودی، سلف و الماس را مدیریت کن.")
    await c.message.edit_text(body,reply_markup=users_admin_kb()); await c.answer()

@dp.callback_query(F.data=="users_all")
async def users_all(c:CallbackQuery):
    if not await is_manager(c.from_user.id):return
    async with Session() as s:
        rows=(await s.execute(select(User).order_by(User.created_at.desc()).limit(50))).scalars().all()
    if not rows:
        await c.message.edit_text("👥 کاربری ثبت نشده است.",reply_markup=users_admin_kb()); await c.answer(); return
    buttons=[]
    for u in rows:
        status="🚫" if u.banned else ("🟢" if u.self_enabled else ("✅" if u.approved else "⏳"))
        name=(u.first_name or u.username or str(u.id)).replace("\n"," ")[:20]
        buttons.append([InlineKeyboardButton(text=f"{status} {name} | 🔴💎 {'∞' if u.id==OWNER_ID else f"{int(u.diamonds or 0):,}"}",callback_data=f"user_detail:{u.id}")])
    buttons += [[InlineKeyboardButton(text="🔎 جستجوی کاربر",callback_data="users_search")],[InlineKeyboardButton(text="⬅️ کاربران",callback_data="users_menu")]]
    await c.message.edit_text("👥 <b>فهرست کاربران</b>\n\nروی هر نفر بزن تا اطلاعات کامل و عملیات مدیریتی نمایش داده شود:",reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)); await c.answer()

@dp.callback_query(F.data=="users_search")
async def users_search(c:CallbackQuery):
    if not await is_manager(c.from_user.id):return
    ADMIN_FLOWS[c.from_user.id]={"type":"user_search"}
    await c.message.edit_text("🔎 <b>جستجوی کاربر</b>\n\nآیدی عددی یا @username یا بخشی از نام را همینجا بفرست.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ لغو",callback_data="users_menu")]])); await c.answer()

@dp.callback_query(F.data.startswith("self_extend:"))
async def self_extend(c:CallbackQuery):
    if not await is_manager(c.from_user.id):return
    _,uid_s,hours_s=c.data.split(":")
    uid=int(uid_s); hours=int(hours_s)
    async with Session() as s:
        u=await get_user(s,uid)
        if not u: await c.answer("کاربر پیدا نشد",show_alert=True); return
        now=datetime.now(timezone.utc)
        exp=u.self_expires_at
        if exp and exp.tzinfo is None: exp=exp.replace(tzinfo=timezone.utc)
        base=exp if exp and exp>now else now
        u.self_expires_at=base+timedelta(hours=hours)
        await s.commit()
    if u.self_enabled: start_billing(uid)
    await c.answer(f"{hours} ساعت تمدید شد",show_alert=True); await user_detail(c)

@dp.callback_query(F.data.startswith("self_reset_billing:"))
async def self_reset_billing(c:CallbackQuery):
    if not await is_manager(c.from_user.id):return
    uid=int(c.data.split(":",1)[1])
    async with Session() as s:
        u=await get_user(s,uid)
        if not u: await c.answer("کاربر پیدا نشد",show_alert=True); return
        u.diamond_billing_remainder=0.0; u.diamond_last_billed_at=datetime.now(timezone.utc) if u.self_enabled else None
        await s.commit()
    await c.answer("محاسبه مصرف از نو تنظیم شد",show_alert=True); await user_detail(c)

@dp.callback_query(F.data.startswith("self_disable:"))
async def self_disable(c:CallbackQuery):
    if not await is_manager(c.from_user.id):return
    uid=int(c.data.split(":",1)[1])
    await deactivate_self(uid)
    try: await c.bot.send_message(uid,"🚫 سلف شما توسط مدیر غیرفعال شد.")
    except Exception: pass
    await c.answer("سلف غیرفعال شد",show_alert=True); await user_detail(c)

@dp.callback_query(F.data.startswith("user_detail:"))
async def user_detail(c:CallbackQuery):
    if not await is_manager(c.from_user.id):return
    uid=int(c.data.split(":",1)[1])
    async with Session() as s:u=await get_user(s,uid)
    if not u: await c.answer("کاربر پیدا نشد",show_alert=True); return
    buttons=[]
    if u.approved and not u.banned: buttons.append([InlineKeyboardButton(text="🚫 مسدود کردن",callback_data=f"user_ban:{uid}")])
    else: buttons.append([InlineKeyboardButton(text="✅ رفع مسدودی / تأیید",callback_data=f"user_unban:{uid}")])
    if not u.approved and not u.banned: buttons.append([InlineKeyboardButton(text="✅ تأیید دسترسی",callback_data=f"approve:{uid}")])
    if u.self_enabled:
        buttons.append([InlineKeyboardButton(text="🧹 حذف سلف",callback_data=f"user_revoke:{uid}"),InlineKeyboardButton(text="🚫 غیرفعال‌سازی",callback_data=f"self_disable:{uid}")])
        buttons.append([InlineKeyboardButton(text="➕ ۱ ساعت",callback_data=f"self_extend:{uid}:1"),InlineKeyboardButton(text="➕ ۲۴ ساعت",callback_data=f"self_extend:{uid}:24"),InlineKeyboardButton(text="➕ ۷ روز",callback_data=f"self_extend:{uid}:168")])
        buttons.append([InlineKeyboardButton(text="🔄 ریست محاسبه مصرف",callback_data=f"self_reset_billing:{uid}")])
    buttons += [[InlineKeyboardButton(text="🔴💎 مدیریت الماس",callback_data=f"diamond_user:{uid}"),InlineKeyboardButton(text="📜 گردش الماس",callback_data=f"diamond_user_history:{uid}")],
                [InlineKeyboardButton(text="🚫 مسدودسازی در تلگرام",callback_data=f"tg_block:{uid}"),InlineKeyboardButton(text="♻️ رفع مسدودی تلگرام",callback_data=f"tg_unblock:{uid}")],
                [InlineKeyboardButton(text="🔄 بروزرسانی",callback_data=f"user_detail:{uid}"),InlineKeyboardButton(text="⬅️ همه کاربران",callback_data="users_all")]]
    await c.message.edit_text(await admin_user_card(u),reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)); await c.answer()

@dp.callback_query(F.data.startswith("user_ban:"))
async def user_ban(c:CallbackQuery):
    if not await is_manager(c.from_user.id):return
    uid=int(c.data.split(":",1)[1])
    if uid==OWNER_ID: await c.answer("مدیر اصلی قابل مسدود کردن نیست.",show_alert=True); return
    stop_clock(uid); stop_billing(uid); stop_automation(uid)
    async with Session() as s:
        u=await get_user(s,uid)
        if not u: await c.answer("کاربر پیدا نشد",show_alert=True); return
        u.banned=True; u.approved=False; u.access_requested=False; u.pending_phone=None; u.self_enabled=False; u.self_session=None; u.phone_masked=None; u.clock_enabled=False; u.self_expires_at=None; u.diamond_billing_remainder=0.0; u.diamond_last_billed_at=None
        await s.commit()
    try: await c.bot.send_message(uid,"🚫 دسترسی شما توسط مدیر مسدود شد.")
    except Exception: pass
    await c.answer("کاربر مسدود شد",show_alert=True); await user_detail(c)

@dp.callback_query(F.data.startswith("user_unban:"))
async def user_unban(c:CallbackQuery):
    if not await is_manager(c.from_user.id):return
    uid=int(c.data.split(":",1)[1])
    async with Session() as s:
        u=await get_user(s,uid)
        if not u: await c.answer("کاربر پیدا نشد",show_alert=True); return
        u.banned=False; u.approved=True; u.access_requested=False; await s.commit()
    try: await c.bot.send_message(uid,"✅ دسترسی شما توسط مدیر فعال شد. برای ورود سلف /start را بزن.")
    except Exception: pass
    await c.answer("دسترسی فعال شد",show_alert=True); await user_detail(c)

@dp.callback_query(F.data.startswith("user_revoke:"))
async def user_revoke(c:CallbackQuery):
    if not await is_manager(c.from_user.id):return
    uid=int(c.data.split(":",1)[1]); stop_clock(uid); stop_billing(uid); stop_automation(uid)
    async with Session() as s:
        u=await get_user(s,uid)
        if not u: await c.answer("کاربر پیدا نشد",show_alert=True); return
        u.self_enabled=False; u.self_session=None; u.phone_masked=None; u.pending_phone=None; u.clock_enabled=False; u.self_expires_at=None; u.diamond_billing_remainder=0.0; u.diamond_last_billed_at=None; await s.commit()
    try: await c.bot.send_message(uid,"🚫 نشست سلف شما توسط مدیر حذف شد.")
    except Exception: pass
    await c.answer("سلف حذف شد",show_alert=True); await user_detail(c)

@dp.callback_query(F.data.startswith("tg_block:"))
async def tg_block_user(c:CallbackQuery):
    if not await is_manager(c.from_user.id): return
    uid=int(c.data.split(":",1)[1])
    if uid==OWNER_ID:
        await c.answer("مدیر اصلی قابل مسدودسازی نیست.",show_alert=True); return
    client=None
    try:
        client=await get_self_client(OWNER_ID)
        if not client:
            await c.answer("سلف مدیر فعال نیست یا نشست معتبر نیست.",show_alert=True); return
        await client(BlockRequest(uid))
        await c.answer("کاربر در سلف مدیر مسدود شد.",show_alert=True)
    except RPCError as e:
        log.warning("tg block failed: %s",e)
        await c.answer("تلگرام اجازه این عملیات را نداد یا کاربر پیدا نشد.",show_alert=True)
    except Exception as e:
        log.warning("tg block error: %s",e)
        await c.answer("انجام عملیات ناموفق بود.",show_alert=True)
    finally:
        if client:
            try: await client.disconnect()
            except Exception: pass

@dp.callback_query(F.data.startswith("tg_unblock:"))
async def tg_unblock_user(c:CallbackQuery):
    if not await is_manager(c.from_user.id): return
    uid=int(c.data.split(":",1)[1])
    client=None
    try:
        client=await get_self_client(OWNER_ID)
        if not client:
            await c.answer("سلف مدیر فعال نیست یا نشست معتبر نیست.",show_alert=True); return
        await client(UnblockRequest(uid))
        await c.answer("رفع مسدودی انجام شد.",show_alert=True)
    except RPCError as e:
        log.warning("tg unblock failed: %s",e)
        await c.answer("تلگرام اجازه این عملیات را نداد یا کاربر پیدا نشد.",show_alert=True)
    except Exception as e:
        log.warning("tg unblock error: %s",e)
        await c.answer("انجام عملیات ناموفق بود.",show_alert=True)
    finally:
        if client:
            try: await client.disconnect()
            except Exception: pass

@dp.callback_query(F.data=="user_stats")
async def user_stats(c:CallbackQuery):
    if not await is_manager(c.from_user.id):return
    async with Session() as s:
        total=await s.scalar(select(func.count(User.id))) or 0
        approved=await s.scalar(select(func.count(User.id)).where(User.approved==True,User.banned==False)) or 0
        banned=await s.scalar(select(func.count(User.id)).where(User.banned==True)) or 0
        active=await s.scalar(select(func.count(User.id)).where(User.self_enabled==True)) or 0
        messages=await s.scalar(select(func.coalesce(func.sum(User.message_count),0))) or 0
        rows=(await s.execute(select(User).order_by(User.message_count.desc()).limit(10))).scalars().all()
    lines=[f"📊 <b>آمار کاربران</b>\n\n👥 کل: {int(total):,}\n✅ فعال: {int(approved):,}\n🚫 مسدود: {int(banned):,}\n🔐 سلف فعال: {int(active):,}\n💬 مجموع پیام ثبت‌شده: {int(messages):,}\n\n<b>۱۰ کاربر برتر از نظر پیام:</b>"]
    for i,u in enumerate(rows,1): lines.append(f"{i}. {escape(u.first_name or str(u.id))} — <code>{u.id}</code> — {int(u.message_count or 0):,} پیام")
    await c.message.edit_text("\n".join(lines),reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ کاربران",callback_data="users_menu")]])); await c.answer()

@dp.callback_query(F.data=="pending_users")
async def pending_users(c:CallbackQuery):
    if not await is_manager(c.from_user.id): return
    async with Session() as s:
        rows=(await s.execute(select(User).where(User.approved==False,User.banned==False).order_by(User.created_at.desc()).limit(30))).scalars().all()
    if not rows:
        await c.message.edit_text("📥 <b>درخواست معلقی وجود ندارد.</b>",reply_markup=users_admin_kb()); await c.answer(); return
    buttons=[]
    for u in rows:
        buttons.append([InlineKeyboardButton(text=f"👤 {u.first_name or u.id}",callback_data=f"user_detail:{u.id}"),InlineKeyboardButton(text="✅",callback_data=f"approve:{u.id}"),InlineKeyboardButton(text="❌",callback_data=f"reject:{u.id}")])
    buttons.append([InlineKeyboardButton(text="⬅️ کاربران",callback_data="users_menu")])
    await c.message.edit_text("📥 <b>درخواست‌های در انتظار</b>\n\nبرای اطلاعات کامل روی نام کاربر بزن:",reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)); await c.answer()

@dp.callback_query(F.data.startswith("pending:"))
async def pending_detail(c:CallbackQuery):
    if not await is_manager(c.from_user.id): return
    uid=int(c.data.split(":",1)[1])
    async with Session() as s: u=await get_user(s,uid)
    if not u: await c.answer("کاربر پیدا نشد",show_alert=True); return
    buttons=[]
    if not u.approved and not u.banned: buttons.append([InlineKeyboardButton(text="✅ تأیید",callback_data=f"approve:{uid}"),InlineKeyboardButton(text="❌ رد",callback_data=f"reject:{uid}")])
    buttons.append([InlineKeyboardButton(text="⬅️ کاربران",callback_data="users_menu")])
    await c.message.edit_text(await admin_user_card(u,"📥 <b>جزئیات درخواست</b>"),reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)); await c.answer()

@dp.callback_query(F.data=="delegated_admins")
async def delegated_admins_page(c:CallbackQuery):
    if c.from_user.id != OWNER_ID:
        await c.answer("فقط مدیر اصلی می‌تواند مدیر جانشین تعیین کند.",show_alert=True); return
    ids=await delegated_admin_ids()
    lines=["👑 <b>مدیریت مدیران جانشین</b>","","مدیر جانشین می‌تواند بیشتر عملیات مدیریتی بات را انجام دهد؛ اما خودش نمی‌تواند مدیر جانشین دیگری تعیین کند.",""]
    kb=[]
    if ids:
        async with Session() as s:
            users=[await get_user(s,i) for i in ids]
        for u in users:
            if u:
                lines.append(f"🛡 {escape(u.first_name or 'کاربر')} — <code>{u.id}</code>")
                kb.append([InlineKeyboardButton(text=f"❌ حذف {u.first_name or u.id}",callback_data=f"delegate_remove:{u.id}")])
    else:
        lines.append("هنوز مدیر جانشینی تعیین نشده است.")
    kb += [[InlineKeyboardButton(text="➕ تعیین مدیر جانشین",callback_data="delegate_add")],[InlineKeyboardButton(text="⬅️ کاربران",callback_data="users_menu")]]
    await c.message.edit_text("\n".join(lines),reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)); await c.answer()

@dp.callback_query(F.data=="delegate_add")
async def delegate_add(c:CallbackQuery):
    if c.from_user.id != OWNER_ID:
        await c.answer("⛔ فقط مدیر اصلی می‌تواند مدیر جانشین تعیین کند.",show_alert=True); return
    ids=set(await delegated_admin_ids())
    async with Session() as s:
        rows=(await s.execute(select(User).where(User.id!=OWNER_ID, User.banned==False).order_by(User.created_at.desc()).limit(40))).scalars().all()
    kb=[]
    for u in rows:
        if u.id in ids: continue
        name=(u.first_name or u.username or str(u.id)).replace("\n"," ")[:22]
        tag=f"@{u.username}" if u.username else str(u.id)
        kb.append([InlineKeyboardButton(text=f"👤 {name} | {tag}",callback_data=f"delegate_pick:{u.id}")])
    kb.append([InlineKeyboardButton(text="🔢 وارد کردن آیدی دستی",callback_data="delegate_add_manual")])
    kb.append([InlineKeyboardButton(text="⬅️ مدیران جانشین",callback_data="delegated_admins")])
    await c.message.edit_text("➕ <b>تعیین مدیر جانشین</b>\n\nاز لیست روی کاربر بزن تا همان لحظه مدیر جانشین شود.\n\nاگر کاربر در لیست نیست، گزینه وارد کردن آیدی دستی را بزن.",reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)); await c.answer()

@dp.callback_query(F.data.startswith("delegate_pick:"))
async def delegate_pick(c:CallbackQuery):
    if c.from_user.id != OWNER_ID:
        await c.answer("⛔ فقط مدیر اصلی مجاز است.",show_alert=True); return
    uid=int(c.data.split(":",1)[1])
    if uid==OWNER_ID:
        await c.answer("مدیر اصلی نیازی به جانشین ندارد.",show_alert=True); return
    async with Session() as s:
        u=await get_user(s,uid)
        if not u:
            await c.answer("❌ کاربر پیدا نشد.",show_alert=True); return
        if u.banned:
            await c.answer("❌ کاربر مسدود است.",show_alert=True); return
        ids=await delegated_admin_ids()
        if uid not in ids: ids.append(uid)
        await set_delegated_admin_ids(ids)
    MANAGER_MENU_IDS.add(uid)
    await c.answer("✅ مدیر جانشین تعیین شد.",show_alert=True)
    await delegated_admins_page(c)

@dp.callback_query(F.data=="delegate_add_manual")
async def delegate_add_manual(c:CallbackQuery):
    if c.from_user.id != OWNER_ID:
        await c.answer("⛔ فقط مدیر اصلی مجاز است.",show_alert=True); return
    ADMIN_FLOWS[c.from_user.id]={"type":"delegate_add"}
    await c.message.edit_text("🔢 <b>تعیین مدیر با آیدی</b>\n\nآیدی عددی کاربر را بفرست. کاربر باید حداقل یک‌بار بات را باز کرده باشد.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ انتخاب از لیست",callback_data="delegate_add")],[InlineKeyboardButton(text="❌ لغو",callback_data="delegated_admins")]])); await c.answer()

@dp.callback_query(F.data.startswith("delegate_remove:"))
async def delegate_remove(c:CallbackQuery):
    if c.from_user.id != OWNER_ID: return
    uid=int(c.data.split(":",1)[1]); ids=[x for x in await delegated_admin_ids() if x != uid]
    await set_delegated_admin_ids(ids)
    await c.answer("مدیر جانشین حذف شد",show_alert=True); await delegated_admins_page(c)

@dp.callback_query(F.data=="manager_status")
async def manager_status(c:CallbackQuery):
    if not await is_manager(c.from_user.id): return
    await c.answer("🛡 شما دسترسی مدیریتی دارید.",show_alert=True)

def diamond_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 کاربران",callback_data="diamond_users"),InlineKeyboardButton(text="🔎 جستجو",callback_data="diamond_search")],
        [InlineKeyboardButton(text="📊 آمار",callback_data="diamond_stats"),InlineKeyboardButton(text="🧾 گردش حساب",callback_data="diamond_history")],
        [InlineKeyboardButton(text="➕ شارژ سریع",callback_data="diamond_quickadd"),InlineKeyboardButton(text="➖ کسر سریع",callback_data="diamond_quicksub")],
        [InlineKeyboardButton(text="⚙️ مدیریت پیشرفته",callback_data="diamond_advanced"),InlineKeyboardButton(text="🔄 انتقال",callback_data="diamond_transfer_help")],
        [InlineKeyboardButton(text="📖 راهنمای مدیریت",callback_data="help:diamonds_admin")],
        [InlineKeyboardButton(text="⬅️ منو",callback_data="main")],
    ])

@dp.callback_query(F.data=="diamond_stats")
async def diamond_stats(c:CallbackQuery):
    if not await is_manager(c.from_user.id): return
    async with Session() as s:
        total_users=await s.scalar(select(func.count(User.id)).where(User.id!=OWNER_ID)) or 0
        total_d=await s.scalar(select(func.coalesce(func.sum(User.diamonds),0)).where(User.id!=OWNER_ID)) or 0
        active=await s.scalar(select(func.count(User.id)).where(User.self_enabled==True,User.id!=OWNER_ID)) or 0
        spent=await s.scalar(select(func.coalesce(func.sum(-DiamondTransaction.amount),0)).where(DiamondTransaction.kind.in_(["hourly","activation"]),DiamondTransaction.amount<0)) or 0
    body=f"📊 <b>مرکز آمار الماس</b>\n\n👥 کاربران: <b>{total_users:,}</b>\n🔴💎 الماس در گردش کاربران: <b>{int(total_d):,}</b>\n🔐 سلف فعال: <b>{active:,}</b>\n📉 مصرف ثبت‌شده سلف: <b>{int(spent):,}</b>\n👑 موجودی مدیر: <b>∞</b>"
    await c.message.edit_text(body,reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🧾 گردش حساب",callback_data="diamond_history")],[InlineKeyboardButton(text="⬅️ مدیریت الماس",callback_data="diamonds")]])); await c.answer()

@dp.callback_query(F.data=="diamond_history")
async def diamond_history(c:CallbackQuery):
    if not await is_manager(c.from_user.id): return
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
    if not await is_manager(c.from_user.id):return
    await c.message.edit_text("➕ <b>شارژ سریع</b>\n\nبرای امنیت، عملیات مالی از طریق دستور فارسی انجام می‌شود.\n\n<code>/الماس 123456789 100</code>\n\nیا برای کم‌کردن:\n<code>/الماس 123456789 -100</code>",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👥 انتخاب کاربر",callback_data="diamond_users")],[InlineKeyboardButton(text="⬅️ مدیریت الماس",callback_data="diamonds")]])); await c.answer()

@dp.callback_query(F.data=="diamond_quicksub")
async def diamond_quicksub(c:CallbackQuery):
    if not await is_manager(c.from_user.id):return
    await c.message.edit_text("➖ <b>کسر سریع</b>\n\nنمونه:\n<code>/الماس 123456789 -100</code>\n\nموجودی هیچ کاربری منفی نمی‌شود و همه تغییرات در گردش حساب ثبت می‌شوند.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👥 انتخاب کاربر",callback_data="diamond_users")],[InlineKeyboardButton(text="⬅️ مدیریت الماس",callback_data="diamonds")]])); await c.answer()

def diamond_advanced_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ نرخ مصرف/ساعت",callback_data="diamond_setting:rate"),InlineKeyboardButton(text="⚙️ هزینه فعال‌سازی",callback_data="diamond_setting:activation")],
        [InlineKeyboardButton(text="🎁 الماس هدیه",callback_data="diamond_setting:welcome"),InlineKeyboardButton(text="⏳ سقف ساعت سلف",callback_data="diamond_setting:maxhours")],
        [InlineKeyboardButton(text="💰 قیمت بسته",callback_data="diamond_setting:pack"),InlineKeyboardButton(text="📦 اندازه بسته",callback_data="diamond_setting:packsize")],
        [InlineKeyboardButton(text="📖 راهنما",callback_data="help:diamonds_admin"),InlineKeyboardButton(text="⬅️ الماس",callback_data="diamonds")],
    ])

@dp.callback_query(F.data=="diamond_advanced")
async def diamond_advanced(c:CallbackQuery):
    if not await is_manager(c.from_user.id):return
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
    if not await is_manager(c.from_user.id):return
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
    if not await is_manager(c.from_user.id):return
    await c.message.edit_text(f"🔄 <b>انتقال {DIAMOND_ICON}</b>\n\nبرای انتقال به آیدی یا یوزرنیم:\n<code>انتقال 100 123456789</code>\n<code>انتقال 100 @username</code>\n\nیا روی پیام کاربر ریپلای کن و فقط بفرست:\n<code>انتقال 100</code>\n\nمدیر می‌تواند مقدار دلخواه را تعیین کند.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ الماس",callback_data="diamonds")]])); await c.answer()

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
        kb=[[InlineKeyboardButton(text="🛒 خرید الماس",url="https://t.me/jokm7")],[InlineKeyboardButton(text="📖 راهنمای الماس",callback_data="help:diamonds")],[InlineKeyboardButton(text="⬅️ منو",callback_data="main")]]
        await c.message.edit_text(body,reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()

@dp.callback_query(F.data=="diamond_users")
async def diamond_users(c:CallbackQuery):
    if not await is_manager(c.from_user.id): return
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
    if not await is_manager(c.from_user.id):return
    uid=int(c.data.split(":",1)[1])
    async with Session() as s:u=await get_user(s,uid)
    if not u: await c.answer("کاربر پیدا نشد",show_alert=True); return
    settings=await get_settings_map()
    available_time=diamond_time_from_balance(u, settings)
    body=(f"🔴💎 <b>مدیریت کیف پول</b>\n\n👤 {escape(u.first_name or '—')}\n"
          f"🆔 <code>{u.id}</code>\n🔗 @{escape(u.username or 'ندارد')}\n"
          f"🔴💎 موجودی: <b>{int(u.diamonds or 0):,}</b>\n"
          f"🔐 سلف: <b>{'فعال' if u.self_enabled else 'خاموش'}</b>\n"
          f"⏳ زمان باقی‌مانده سلف: <b>{self_remaining_text(u)}</b>\n"
          f"⏱ زمان قابل استفاده با موجودی فعلی: <b>{escape(available_time)}</b>")
    kb=[[InlineKeyboardButton(text="➕ ۱۰۰",callback_data=f"diamond_adj:{uid}:100"),InlineKeyboardButton(text="➕ ۱۰۰۰",callback_data=f"diamond_adj:{uid}:1000")],
        [InlineKeyboardButton(text="➖ ۱۰۰",callback_data=f"diamond_adj:{uid}:-100"),InlineKeyboardButton(text="➖ ۱۰۰۰",callback_data=f"diamond_adj:{uid}:-1000")],
        [InlineKeyboardButton(text="➕ مقدار دلخواه",callback_data=f"diamond_custom:{uid}:add"),InlineKeyboardButton(text="➖ مقدار دلخواه",callback_data=f"diamond_custom:{uid}:sub")],
        [InlineKeyboardButton(text="🧹 صفر کردن موجودی",callback_data=f"diamond_zero:{uid}")],
        [InlineKeyboardButton(text="📜 گردش این کاربر",callback_data=f"diamond_user_history:{uid}"),InlineKeyboardButton(text="📋 مشخصات کاربری",callback_data=f"user_detail:{uid}")],
        [InlineKeyboardButton(text="⬅️ کاربران",callback_data="diamond_users")]]
    await c.message.edit_text(body,reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)); await c.answer()

@dp.callback_query(F.data.startswith("diamond_adj:"))
async def diamond_adjust(c:CallbackQuery):
    if not await is_manager(c.from_user.id):return
    _,uid_s,amount_s=c.data.split(":")
    uid=int(uid_s); amount=int(amount_s)
    async with Session() as s:
        ok=await change_diamonds(s,uid,c.from_user.id,amount,"admin_add" if amount>0 else "admin_sub", "تغییر از پنل مدیر")
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
    if not await is_manager(c.from_user.id):return
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
    if not await is_manager(c.from_user.id):return
    ADMIN_FLOWS[c.from_user.id]={"type":"diamond_search"}
    await c.message.edit_text("🔎 <b>جستجوی الماس</b>\n\nآیدی عددی یا @username یا بخشی از نام کاربر را بفرست.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ لغو",callback_data="diamonds")]])); await c.answer()

@dp.callback_query(F.data.startswith("diamond_custom:"))
async def diamond_custom(c:CallbackQuery):
    if not await is_manager(c.from_user.id):return
    _,uid_s,mode=c.data.split(":")
    uid=int(uid_s)
    ADMIN_FLOWS[c.from_user.id]={"type":"diamond_adjust","uid":uid,"mode":mode}
    await c.message.edit_text(("➕ <b>شارژ سفارشی</b>\n\nمقدار الماس مثبت را بفرست:" if mode=="add" else "➖ <b>کسر سفارشی</b>\n\nمقدار الماس مثبت را بفرست:"),reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ لغو",callback_data=f"diamond_user:{uid}")]])); await c.answer()

@dp.callback_query(F.data.startswith("diamond_zero:"))
async def diamond_zero(c:CallbackQuery):
    if not await is_manager(c.from_user.id):return
    uid=int(c.data.split(":",1)[1])
    async with Session() as s:
        u=await get_user(s,uid)
        if not u: await c.answer("کاربر پیدا نشد",show_alert=True); return
        old=int(u.diamonds or 0)
        if old:
            await change_diamonds(s,uid,c.from_user.id,-old,"admin_zero","صفر کردن موجودی از پنل")
            await s.commit()
    await c.answer("موجودی صفر شد",show_alert=True)
    await diamond_user_detail(c)

@dp.callback_query(F.data=="self_users")
async def self_users(c:CallbackQuery):
    if not await is_manager(c.from_user.id): await c.answer("این بخش فقط برای مدیر است",show_alert=True); return
    async with Session() as s: rows=(await s.execute(select(User).where(User.self_enabled==True).order_by(User.last_seen.desc()).limit(30))).scalars().all()
    if not rows: text="👥 <b>سلف فعال</b>\n\nهیچ کاربری در حال حاضر سلف فعال ندارد."; kb=[[InlineKeyboardButton(text="⬅️ منو",callback_data="main")]]
    else:
        text="👥 <b>کاربران دارای سلف فعال</b>\n\nالماس و زمان باقی‌مانده هر کاربر نمایش داده می‌شود. برای مدیریت روی کاربر بزن:"; kb=[[InlineKeyboardButton(text=f"👤 {u.first_name or u.id} | 🔴💎 {u.diamonds or 0} | ⏳ {self_remaining_text(u)}",callback_data=f"self_revoke:{u.id}")] for u in rows]; kb += [[InlineKeyboardButton(text="📖 راهنمای این بخش",callback_data="help:self_users"),InlineKeyboardButton(text="⬅️ منو",callback_data="main")]]
    await c.message.edit_text(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)); await c.answer()

@dp.callback_query(F.data.startswith("self_revoke:"))
async def self_revoke(c:CallbackQuery):
    if not await is_manager(c.from_user.id): return
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
    rows=[]
    for k,v in MESSAGE_FONTS.items():
        sample=apply_message_font("Abc 123",k)
        rows.append([InlineKeyboardButton(text=f"{v[0]}  {sample[:24]}",callback_data=f"msgfont:{k}")])
    rows += [[InlineKeyboardButton(text="🔐 رمزی",callback_data="secret")],[InlineKeyboardButton(text="⬅️ منو",callback_data="main")],[InlineKeyboardButton(text="📖 راهنما",callback_data="help:text_style")]]
    await c.message.edit_text("✍️ <b>استایل و فونت پیام</b>\n\nیک فونت را انتخاب کن؛ بعد متن را بفرست تا نسخه استایل‌شده‌اش را تحویل بگیری.\n\n⚠️ فونت‌های Unicode بیشتر روی حروف لاتین و اعداد اثر می‌گذارند؛ متن فارسی معمولاً بدون تغییر می‌ماند.",reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)); await c.answer()

@dp.callback_query(F.data.startswith("msgfont:"))
async def msgfont_select(c:CallbackQuery):
    font=c.data.split(":",1)[1]
    if font not in MESSAGE_FONTS: await c.answer("فونت پیدا نشد",show_alert=True); return
    MESSAGE_FONT_FLOWS[c.from_user.id]=font
    await c.message.edit_text(f"✍️ <b>{MESSAGE_FONTS[font][0]}</b> انتخاب شد.\n\nحالا متن خودت را بفرست تا با همین فونت تبدیل شود.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ لغو",callback_data="text_style")]])); await c.answer()

@dp.callback_query(F.data=="tools")
async def tools_page(c:CallbackQuery):
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏰ ساعت",callback_data="time"),InlineKeyboardButton(text="👤 پروفایل",callback_data="profile")],[InlineKeyboardButton(text="🎲 تاس",callback_data="tool:dice")],[InlineKeyboardButton(text="📚 راهنما",callback_data="help_all"),InlineKeyboardButton(text="⬅️ منو",callback_data="main")],[InlineKeyboardButton(text="📖 راهنمای ابزارها",callback_data="help:tools")]])
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


def simple_page(title, body, rows=None):
    rows = rows or []
    rows.append([InlineKeyboardButton(text="📖 راهنمای این بخش", callback_data=f"help:{title.split(' ',1)[-1]}")])
    rows.append([InlineKeyboardButton(text="⬅️ منوی اصلی", callback_data="main")])
    return InlineKeyboardMarkup(inline_keyboard=rows), f"<b>{title}</b>\n\n{body}"

@dp.callback_query(F.data=="comments")
async def comments_page(c:CallbackQuery):
    if not await allowed(c.from_user.id): return await c.answer("دسترسی ندارید",show_alert=True)
    async with Session() as s:
        u=await get_user(s,c.from_user.id); enabled=bool(u and u.comments_mode)
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 کامنت روشن" if enabled else "🟢 روشن کردن کامنت",callback_data="comments:on"),InlineKeyboardButton(text="🔴 خاموش",callback_data="comments:off")],
        [InlineKeyboardButton(text="📖 راهنما",callback_data="help:comments"),InlineKeyboardButton(text="⬅️ منو",callback_data="main")]
    ])
    await c.message.edit_text(f"💬 <b>کامنت</b>\n\nوضعیت: <b>{'روشن ✅' if enabled else 'خاموش ❌'}</b>\n\nتنظیم ذخیره می‌شود و برای مدیریت کامنت‌های کانال/گروه باید حساب سلف دسترسی لازم را داشته باشد.",reply_markup=kb); await c.answer()

@dp.callback_query(F.data.startswith("comments:"))
async def comments_toggle(c:CallbackQuery):
    val=c.data.endswith(":on")
    async with Session() as s:
        u=await get_user(s,c.from_user.id); u.comments_mode=val; await s.commit()
    await c.answer("💬 کامنت روشن شد" if val else "💬 کامنت خاموش شد",show_alert=True); await comments_page(c)

@dp.callback_query(F.data=="public")
async def public_page(c:CallbackQuery):
    if not await allowed(c.from_user.id): return await c.answer("دسترسی ندارید",show_alert=True)
    async with Session() as s:
        u=await get_user(s,c.from_user.id); notify=bool(u and u.notifications); seen=bool(u and u.auto_seen)
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔔 اعلان روشن" if notify else "🔔 اعلان خاموش",callback_data="public:notify"),InlineKeyboardButton(text="👁 آخرین بازدید روشن" if seen else "👁 آخرین بازدید خاموش",callback_data="public:seen")],
        [InlineKeyboardButton(text="📖 راهنما",callback_data="help:public"),InlineKeyboardButton(text="⬅️ منو",callback_data="main")]
    ])
    await c.message.edit_text(f"📌 <b>عمومی</b>\n\n🔔 اعلان‌ها: {'روشن' if notify else 'خاموش'}\n👁 خواندن خودکار: {'روشن' if seen else 'خاموش'}\n\nاین گزینه‌ها برای تنظیم رفتار عمومی حساب هستند.",reply_markup=kb); await c.answer()

@dp.callback_query(F.data.startswith("public:"))
async def public_toggle(c:CallbackQuery):
    what=c.data.split(":",1)[1]
    async with Session() as s:
        u=await get_user(s,c.from_user.id)
        if what=="notify": u.notifications=not u.notifications; msg="اعلان‌ها"
        else: u.auto_seen=not u.auto_seen; msg="خواندن خودکار"
        value=u.notifications if what=="notify" else u.auto_seen; await s.commit()
    await c.answer(f"{msg} {'روشن شد' if value else 'خاموش شد'}",show_alert=True); await public_page(c)

@dp.callback_query(F.data=="media")
async def media_page(c:CallbackQuery):
    if not await allowed(c.from_user.id): return await c.answer("دسترسی ندارید",show_alert=True)
    async with Session() as s:
        u=await get_user(s,c.from_user.id); enabled=bool(u and u.media_lock)
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔒 روشن",callback_data="toggle:media:on"),InlineKeyboardButton(text="🔓 خاموش",callback_data="toggle:media:off")],
        [InlineKeyboardButton(text="📖 راهنما",callback_data="help:media"),InlineKeyboardButton(text="⬅️ منو",callback_data="main")]
    ])
    await c.message.edit_text(f"🔒 <b>قفل رسانه</b>\n\nوضعیت: <b>{'روشن 🔒' if enabled else 'خاموش 🔓'}</b>\n\nدر حالت روشن، رسانه‌های ورودی که بات بتواند مدیریت کند حذف می‌شوند. برای حساب سلف، اجرای کامل وابسته به دسترسی تلگرام است.",reply_markup=kb); await c.answer()

@dp.callback_query(F.data.startswith("toggle:media:"))
async def media_toggle_new(c:CallbackQuery):
    val=c.data.endswith(":on")
    async with Session() as s:
        u=await get_user(s,c.from_user.id); u.media_lock=val; await s.commit()
    await c.answer("🔒 قفل رسانه روشن شد" if val else "🔓 قفل رسانه خاموش شد",show_alert=True); await media_page(c)

@dp.callback_query(F.data=="actions")
async def actions_page(c:CallbackQuery):
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👍 ریکت",callback_data="reaction"),InlineKeyboardButton(text="✉️ پاسخ خودکار",callback_data="secretary")],
        [InlineKeyboardButton(text="🎲 تاس",callback_data="tool:dice"),InlineKeyboardButton(text="🔮 فال",callback_data="fortune")],
        [InlineKeyboardButton(text="📖 راهنما",callback_data="help:actions"),InlineKeyboardButton(text="⬅️ منو",callback_data="main")]
    ])
    await c.message.edit_text("🎭 <b>اکشن</b>\n\nعملیات سریع روی پیام‌ها از اینجا در دسترس است. برای ریکت، روی پیام ریپلای کن و ریکت را تنظیم کن.",reply_markup=kb); await c.answer()

@dp.callback_query(F.data=="spam")
async def spam_page(c:CallbackQuery):
    if not await allowed(c.from_user.id): return await c.answer("دسترسی ندارید",show_alert=True)
    async with Session() as s:
        u=await get_user(s,c.from_user.id); enabled=bool(u and u.spam_protection)
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 ضداسپم روشن",callback_data="spam:on"),InlineKeyboardButton(text="🔴 خاموش",callback_data="spam:off")],
        [InlineKeyboardButton(text="📖 راهنما",callback_data="help:spam"),InlineKeyboardButton(text="⬅️ منو",callback_data="main")]
    ])
    await c.message.edit_text(f"💣 <b>اسپم / ضداسپم</b>\n\nوضعیت: <b>{'روشن 🛡' if enabled else 'خاموش'}</b>\n\nحالت روشن فقط رفتارهای تکراری پنل را محدود می‌کند و برای ارسال انبوه یا آزار دیگران طراحی نشده است.",reply_markup=kb); await c.answer()

@dp.callback_query(F.data.startswith("spam:"))
async def spam_toggle(c:CallbackQuery):
    val=c.data.endswith(":on")
    async with Session() as s:
        u=await get_user(s,c.from_user.id); u.spam_protection=val; await s.commit()
    await c.answer("🛡 ضداسپم روشن شد" if val else "ضداسپم خاموش شد",show_alert=True); await spam_page(c)

@dp.callback_query(F.data=="smart")
async def smart_page(c:CallbackQuery):
    if not await allowed(c.from_user.id): return await c.answer("دسترسی ندارید",show_alert=True)
    async with Session() as s:
        u=await get_user(s,c.from_user.id); enabled=bool(u and u.smart_mode)
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 هوش در پی‌وی روشن",callback_data="smart:toggle"),InlineKeyboardButton(text="🤖 هوش خاموش",callback_data="smart:off")],
        [InlineKeyboardButton(text="💬 چت ساده",callback_data="smart:chat"),InlineKeyboardButton(text="🔊 متن/صدا",callback_data="smart:media")],
        [InlineKeyboardButton(text="📖 راهنما",callback_data="help:smart"),InlineKeyboardButton(text="⬅️ منو",callback_data="main")]
    ])
    await c.message.edit_text(f"🤖 <b>هوش مصنوعی</b>\n\nوضعیت: <b>{'روشن ✅' if enabled else 'خاموش ❌'}</b>\n\nنسخه پایه بدون سرویس خارجی، پاسخ‌های ساده و تنظیمات را مدیریت می‌کند. برای مدل خارجی باید API آن سرویس را جداگانه تنظیم کنی.",reply_markup=kb); await c.answer()

@dp.callback_query(F.data=="smart:toggle")
async def smart_toggle(c:CallbackQuery):
    async with Session() as s:
        u=await get_user(s,c.from_user.id); u.smart_mode=True; await s.commit()
    await c.answer("🤖 هوش روشن شد",show_alert=True); await smart_page(c)

@dp.callback_query(F.data=="smart:off")
async def smart_off(c:CallbackQuery):
    async with Session() as s:
        u=await get_user(s,c.from_user.id); u.smart_mode=False; await s.commit()
    await c.answer("🤖 هوش خاموش شد",show_alert=True); await smart_page(c)

@dp.callback_query(F.data=="smart:chat")
async def smart_chat(c:CallbackQuery):
    await c.message.edit_text("💬 <b>چت با هوش ساده</b>\n\nپیام متنی بعدی را بفرست؛ اگر حالت هوشمند روشن باشد، پاسخ کوتاه داخلی تولید می‌شود.\n\nبرای مدل واقعی خارجی، کلید API را فقط در متغیر محیطی سرویس خودت قرار بده.",reply_markup=back()); await c.answer()

@dp.callback_query(F.data=="smart:media")
async def smart_media(c:CallbackQuery):
    await c.answer("🔊 امکانات تبدیل متن/صدا به سرویس جانبی نیاز دارد.",show_alert=True)

@dp.callback_query(F.data=="secretary")
async def secretary_page(c:CallbackQuery):
    if not await allowed(c.from_user.id): return await c.answer("دسترسی ندارید",show_alert=True)
    async with Session() as s:
        u=await get_user(s,c.from_user.id); reply=u.auto_reply if u else None
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 تنظیم پاسخ",callback_data="secretary:set"),InlineKeyboardButton(text="🗑 خاموش",callback_data="secretary:off")],
        [InlineKeyboardButton(text="📖 راهنما",callback_data="help:secretary"),InlineKeyboardButton(text="⬅️ منو",callback_data="main")]
    ])
    await c.message.edit_text(f"🧠 <b>منشی هوشمند</b>\n\nوضعیت: <b>{'فعال ✅' if reply else 'خاموش ❌'}</b>\n\nپاسخ فعلی: <code>{escape(reply or 'تنظیم نشده')}</code>\n\nبرای تنظیم، دستور <code>/autoreply متن</code> را هم می‌توانی بفرستی.",reply_markup=kb); await c.answer()

@dp.callback_query(F.data=="secretary:set")
async def secretary_set(c:CallbackQuery):
    await c.message.edit_text("📝 <b>تنظیم منشی</b>\n\nدر پیام بعدی این دستور را بفرست:\n<code>/autoreply متن پاسخ</code>\n\nبرای خاموش کردن: <code>/autoreply</code>",reply_markup=back()); await c.answer()

@dp.callback_query(F.data=="secretary:off")
async def secretary_off(c:CallbackQuery):
    async with Session() as s:
        u=await get_user(s,c.from_user.id); u.auto_reply=None; await s.commit()
    await c.answer("🧠 منشی خاموش شد",show_alert=True); await secretary_page(c)

@dp.callback_query(F.data=="currencies")
async def currencies_page(c:CallbackQuery):
    rows=[
        [InlineKeyboardButton(text="💵 دلار",url="https://www.google.com/search?q=USD+to+IRR"),InlineKeyboardButton(text="💷 پوند",url="https://www.google.com/search?q=GBP+to+IRR")],
        [InlineKeyboardButton(text="💶 یورو",url="https://www.google.com/search?q=EUR+to+IRR"),InlineKeyboardButton(text="💴 یوان چین",url="https://www.google.com/search?q=CNY+to+IRR")],
        [InlineKeyboardButton(text="₺ لیر",url="https://www.google.com/search?q=TRY+to+IRR"),InlineKeyboardButton(text="دینار عراق",url="https://www.google.com/search?q=IQD+to+IRR")],
        [InlineKeyboardButton(text="🥇 طلا",url="https://www.google.com/search?q=gold+price+iran"),InlineKeyboardButton(text="₿ بیت‌کوین",url="https://www.google.com/search?q=BTC+price")],
        [InlineKeyboardButton(text="📊 نرخ همه",url="https://www.google.com/finance/"),InlineKeyboardButton(text="📖 راهنما",callback_data="help:currencies")],
        [InlineKeyboardButton(text="⬅️ منو",callback_data="main")]
    ]
    await c.message.edit_text("💰 <b>ارزها</b>\n\nبرای نرخ لحظه‌ای، روی ارز موردنظر بزن. نرخ‌ها از صفحه رسمی/عمومی جست‌وجو باز می‌شوند و عدد داخل پنل ثابت نگه داشته نمی‌شود.",reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)); await c.answer()

@dp.callback_query(F.data=="messages")
async def messages_page(c:CallbackQuery):
    if not await allowed(c.from_user.id): return await c.answer("دسترسی ندارید",show_alert=True)
    async with Session() as s:
        u=await get_user(s,c.from_user.id); seen=bool(u and u.auto_seen)
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧹 حذف ۱۰",callback_data="msgdel:10"),InlineKeyboardButton(text="🧹 حذف ۵۰",callback_data="msgdel:50")],
        [InlineKeyboardButton(text="🧹 حذف کامل",callback_data="msgdel:all"),InlineKeyboardButton(text="👁 فعال‌سازی اوتوسین",callback_data="msgseen")],
        [InlineKeyboardButton(text="📖 راهنما",callback_data="help:messages"),InlineKeyboardButton(text="⬅️ منو",callback_data="main")]
    ])
    await c.message.edit_text(f"✉️ <b>مدیریت پیام</b>\n\n👁 اوتوسین: <b>{'روشن' if seen else 'خاموش'}</b>\n\nحذف ۱۰/۵۰/کامل فقط روی پیام‌هایی که حساب/بات واقعاً اجازه حذفشان را دارد اجرا می‌شود. برای جلوگیری از حذف اشتباهی، اجرای حذف نیاز به ریپلای یا انتخاب چت دارد.",reply_markup=kb); await c.answer()

@dp.callback_query(F.data=="msgseen")
async def msgseen(c:CallbackQuery):
    async with Session() as s:
        u=await get_user(s,c.from_user.id); u.auto_seen=not u.auto_seen; v=u.auto_seen; await s.commit()
    await c.answer("👁 اوتوسین روشن شد" if v else "👁 اوتوسین خاموش شد",show_alert=True); await messages_page(c)

@dp.callback_query(F.data.startswith("msgdel:"))
async def msgdel(c:CallbackQuery):
    amount=c.data.split(":",1)[1]
    await c.answer("🧹 برای حذف واقعی، این دکمه فقط تنظیم را نشان می‌دهد؛ چت هدف را مشخص کن و روی پیام موردنظر ریپلای کن تا حذف انجام شود.",show_alert=True)

@dp.callback_query(F.data=="tagall")
async def tagall_page(c:CallbackQuery):
    await c.message.edit_text("📢 <b>تگ همه</b>\n\nبرای جلوگیری از اسپم، تگ‌کردن انبوه خودکار در این نسخه محدود شده است. می‌توانی در گروه با ریپلای/دستور، حداکثر تعداد محدودی از کاربران را خطاب کنی؛ ارسال بی‌وقفه یا مزاحم انجام نمی‌شود.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📖 راهنما",callback_data="help:tagall")],[InlineKeyboardButton(text="⬅️ منو",callback_data="main")]])); await c.answer()

@dp.callback_query(F.data=="buttons")
async def buttons_page(c:CallbackQuery):
    if not await allowed(c.from_user.id): return await c.answer("دسترسی ندارید",show_alert=True)
    async with Session() as s:
        u=await get_user(s,c.from_user.id); theme=u.button_theme if u else "orange"
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔵 آبی",callback_data="theme:blue"),InlineKeyboardButton(text="🟢 سبز",callback_data="theme:green"),InlineKeyboardButton(text="🔴 قرمز",callback_data="theme:red"),InlineKeyboardButton(text="🟠 نارنجی",callback_data="theme:orange")],
        [InlineKeyboardButton(text="🔄 ریست همه",callback_data="theme:reset")],
        [InlineKeyboardButton(text="◀️ قبلی",callback_data="buttons:prev"),InlineKeyboardButton(text="1/8",callback_data="buttons:none"),InlineKeyboardButton(text="▶️ بعد",callback_data="buttons:next")],
        [InlineKeyboardButton(text="📖 راهنما",callback_data="help:buttons"),InlineKeyboardButton(text="⬅️ منو",callback_data="main")]
    ])
    await c.message.edit_text(f"🎨 <b>تنظیم دکمه‌ها</b>\n\nتم ذخیره‌شده: <b>{escape(theme)}</b>\n\nتلگرام رنگ واقعی InlineKeyboard را به ربات نمی‌دهد؛ بنابراین تم در تنظیمات ذخیره می‌شود و نشانه‌ها/چیدمان پنل از آن استفاده می‌کنند.",reply_markup=kb); await c.answer()

@dp.callback_query(F.data.startswith("theme:"))
async def theme_cb(c:CallbackQuery):
    theme=c.data.split(":",1)[1]
    if theme=="reset": theme="orange"
    if theme not in {"blue","green","red","orange"}: return await c.answer("از گزینه‌های موجود انتخاب کن.")
    async with Session() as s:
        u=await get_user(s,c.from_user.id); u.button_theme=theme; await s.commit()
    await c.answer(f"تم {theme} ذخیره شد",show_alert=True); await buttons_page(c)

@dp.callback_query(F.data.startswith("buttons:"))
async def buttons_nav(c:CallbackQuery):
    action=c.data.split(":",1)[1]
    pages={
        "prev":("📋 صفحه ۱/۳ — چیدمان","حالت‌های نمایش پنل: فشرده، استاندارد و راهنما. در نسخه فعلی چیدمان خودکار و خوانا استفاده می‌شود."),
        "none":("📋 صفحه ۲/۳ — وضعیت","تم ذخیره‌شده روی حساب نگه داشته می‌شود؛ رنگ واقعی InlineKeyboard را تلگرام به ربات نمی‌دهد."),
        "next":("📋 صفحه ۳/۳ — راهنمای تم","تم‌های آبی، سبز، قرمز و نارنجی در دیتابیس ذخیره می‌شوند و برای چیدمان/نشانه‌گذاری پنل قابل استفاده‌اند."),
    }
    title,body=pages.get(action,pages["none"])
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ قبلی",callback_data="buttons:prev"),InlineKeyboardButton(text="2/3",callback_data="buttons:none"),InlineKeyboardButton(text="بعد ▶️",callback_data="buttons:next")],[InlineKeyboardButton(text="🎨 تم‌ها",callback_data="buttons")],[InlineKeyboardButton(text="⬅️ منو",callback_data="main")]])
    await c.message.edit_text(f"<b>{title}</b>\n\n{body}",reply_markup=kb); await c.answer()


@dp.callback_query(F.data=="reaction")
async def reaction_page(c:CallbackQuery):
    if not await allowed(c.from_user.id): return await c.answer("دسترسی ندارید",show_alert=True)
    async with Session() as s:
        u=await get_user(s,c.from_user.id); emoji=u.auto_reaction if u else None
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👍 ریکت",callback_data="reaction:set"),InlineKeyboardButton(text="❌ حذف ریکت",callback_data="reaction:off")],
        [InlineKeyboardButton(text="📖 راهنما",callback_data="help:reaction"),InlineKeyboardButton(text="⬅️ منو",callback_data="main")]
    ])
    await c.message.edit_text(f"👍 <b>ریکت</b>\n\nریکت فعلی: <b>{escape(emoji or 'تنظیم نشده')}</b>\n\nبرای تنظیم روی پیام موردنظر ریپلای کن و <code>/reaction ❤️</code> بفرست. برای حذف: <code>/reaction</code>.",reply_markup=kb); await c.answer()

@dp.callback_query(F.data=="reaction:set")
async def reaction_set_hint(c:CallbackQuery):
    await c.message.edit_text("👍 <b>تنظیم ریکت</b>\n\nروی پیام هدف ریپلای کن و بفرست:\n<code>/reaction ❤️</code>\n\nهر ایموجی مجاز تلگرام را می‌توانی امتحان کنی.",reply_markup=back()); await c.answer()

@dp.callback_query(F.data=="reaction:off")
async def reaction_off(c:CallbackQuery):
    async with Session() as s:
        u=await get_user(s,c.from_user.id); u.auto_reaction=None; await s.commit()
    await c.answer("👍 ریکت خودکار حذف شد",show_alert=True); await reaction_page(c)

@dp.callback_query(F.data=="enemies")
async def enemies_page(c:CallbackQuery):
    if not await allowed(c.from_user.id): return await c.answer("دسترسی ندارید",show_alert=True)
    await c.message.edit_text("😈 <b>دشمنان / مسدودی‌ها</b>\n\n👤 مسدود کردن کاربر: <code>/block @username</code>\n🔓 رفع مسدودی: <code>/unblock @username</code>\n🚫 مسدودی مدیریتی پنل: <code>/ban ID</code>\n\nعملیات بلاک حساب سلف فقط وقتی انجام می‌شود که تلگرام اجازه بدهد.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📖 راهنما",callback_data="help:enemies")],[InlineKeyboardButton(text="⬅️ منو",callback_data="main")]])); await c.answer()

@dp.callback_query(F.data=="edit")
async def edit_page(c:CallbackQuery):
    if not await allowed(c.from_user.id): return await c.answer("دسترسی ندارید",show_alert=True)
    await c.message.edit_text("✏️ <b>تغییر پروفایل</b>\n\nنام:\n<code>/setname نام جدید</code>\n\nبیو:\n<code>/setbio متن بیو</code>\n\nیوزرنیم:\n<code>/setusername username</code>\n\nاین تغییرها مستقیم روی حساب سلف اعمال می‌شوند.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📖 راهنما",callback_data="help:edit")],[InlineKeyboardButton(text="⬅️ منو",callback_data="main")]])); await c.answer()

@dp.callback_query(F.data=="filter")
async def filter_page(c:CallbackQuery):
    if not await allowed(c.from_user.id): return await c.answer("دسترسی ندارید",show_alert=True)
    async with Session() as s:
        u=await get_user(s,c.from_user.id); enabled=bool(u and u.word_filter); words=u.word_filter_text or ""
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 فیلتر روشن",callback_data="filter:on"),InlineKeyboardButton(text="🔴 فیلتر خاموش",callback_data="filter:off")],
        [InlineKeyboardButton(text="📜 لیست کلمات",callback_data="filter:list"),InlineKeyboardButton(text="➕ افزودن (راهنما)",callback_data="filter:add")],
        [InlineKeyboardButton(text="📖 راهنما",callback_data="help:filter"),InlineKeyboardButton(text="⬅️ منو",callback_data="main")]
    ])
    await c.message.edit_text(f"🚫 <b>فیلتر کلمات</b>\n\nوضعیت: <b>{'روشن ✅' if enabled else 'خاموش ❌'}</b>\nکلمات ذخیره‌شده: <code>{escape(words or 'خالی')}</code>",reply_markup=kb); await c.answer()

@dp.callback_query(F.data.startswith("filter:"))
async def filter_action(c:CallbackQuery):
    action=c.data.split(":",1)[1]
    if action in {"on","off"}:
        async with Session() as s:
            u=await get_user(s,c.from_user.id); u.word_filter=(action=="on"); await s.commit()
        await c.answer("🚫 فیلتر روشن شد" if action=="on" else "فیلتر خاموش شد",show_alert=True); return await filter_page(c)
    if action=="list":
        async with Session() as s: u=await get_user(s,c.from_user.id); words=u.word_filter_text or ""
        await c.message.edit_text(f"📜 <b>لیست کلمات</b>\n\n{escape(words or 'هنوز کلمه‌ای ثبت نشده است.') }\n\nبرای تنظیم سریع از <code>/filter کلمه1,کلمه2</code> استفاده کن.",reply_markup=back()); return await c.answer()
    await c.message.edit_text("➕ <b>افزودن کلمات</b>\n\nمثال:\n<code>/filter کلمه1,کلمه2,کلمه3</code>\n\nکلمات را با کاما جدا کن.",reply_markup=back()); await c.answer()

@dp.callback_query(F.data=="namelock")
async def namelock_page(c:CallbackQuery):
    if not await allowed(c.from_user.id): return await c.answer("دسترسی ندارید",show_alert=True)
    async with Session() as s:
        u=await get_user(s,c.from_user.id); enabled=bool(u and u.name_lock); name=u.locked_name or "ثبت نشده"
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛡 روشن",callback_data="namelock:on"),InlineKeyboardButton(text="🔓 خاموش",callback_data="namelock:off")],[InlineKeyboardButton(text="✏️ ثبت نام پایه",callback_data="namelock:set")],[InlineKeyboardButton(text="📖 راهنما",callback_data="help:namelock"),InlineKeyboardButton(text="⬅️ منو",callback_data="main")]])
    await c.message.edit_text(f"🛡 <b>حفاظت اسم</b>\n\nوضعیت: <b>{'روشن ✅' if enabled else 'خاموش ❌'}</b>\nنام پایه: <b>{escape(name)}</b>\n\nوقتی ساعت روی نام فعال باشد، نام پایه برای ساخت نام زمان‌دار استفاده می‌شود.",reply_markup=kb); await c.answer()

@dp.callback_query(F.data.startswith("namelock:"))
async def namelock_action(c:CallbackQuery):
    action=c.data.split(":",1)[1]
    if action=="set":
        await c.message.edit_text("✏️ <b>ثبت نام پایه</b>\n\nدستور زیر را با نام دلخواه بفرست:\n<code>/lockname نام پایه</code>",reply_markup=back()); return await c.answer()
    async with Session() as s:
        u=await get_user(s,c.from_user.id); u.name_lock=(action=="on"); await s.commit()
    await c.answer("🛡 حفاظت اسم روشن شد" if action=="on" else "حفاظت اسم خاموش شد",show_alert=True); await namelock_page(c)

@dp.callback_query(F.data=="fortune")
async def fortune_page(c:CallbackQuery):
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔮 گرفتن فال",callback_data="fortune:go")],[InlineKeyboardButton(text="📖 راهنما",callback_data="help:fortune"),InlineKeyboardButton(text="⬅️ منو",callback_data="main")]])
    await c.message.edit_text("🔮 <b>فال</b>\n\nیک فال کاملاً سرگرمی و تصادفی بگیر.",reply_markup=kb); await c.answer()

@dp.callback_query(F.data=="fortune:go")
async def fortune_go(c:CallbackQuery):
    await c.answer("🔮 "+random.choice(["یک فرصت تازه نزدیک است.","امروز برای شروع یک کار کوچک خوب است.","کمی صبر نتیجه بهتری می‌دهد.","به برنامه‌ای که شروع کرده‌ای پایبند بمان."]),show_alert=True)

@dp.callback_query(F.data=="secret")
async def secret_page(c:CallbackQuery):
    await c.message.edit_text("🔐 <b>متن رمزی</b>\n\nکدگذاری ساده:\n<code>/secret متن</code>\n\nبازکردن:\n<code>/unsecret کد</code>\n\n⚠️ این Base64 است و برای اطلاعات حساس یا رمز عبور مناسب نیست.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📖 راهنما",callback_data="help:secret")],[InlineKeyboardButton(text="⬅️ منو",callback_data="main")]])); await c.answer()

@dp.callback_query(F.data=="info")
async def info_page(c:CallbackQuery):
    if not await allowed(c.from_user.id): return await c.answer("دسترسی ندارید",show_alert=True)
    async with Session() as s:
        u=await get_user(s,c.from_user.id)
    await c.message.edit_text(f"ℹ️ <b>اطلاعات</b>\n\n👤 {escape(u.first_name or '—')}\n🆔 <code>{u.id}</code>\n🔗 @{escape(u.username or 'ندارد')}\n🔐 سلف: <b>{'فعال' if u.self_enabled else 'خاموش'}</b>\n⏳ زمان: <b>{self_remaining_text(u)}</b>\n🔴💎 الماس: <b>{'∞' if u.id==OWNER_ID else u.diamonds}</b>\n💬 تعداد پیام ثبت‌شده: <b>{u.message_count or 0}</b>",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 بروزرسانی",callback_data="info"),InlineKeyboardButton(text="📖 راهنما",callback_data="help:info")],[InlineKeyboardButton(text="⬅️ منو",callback_data="main")]])); await c.answer()

@dp.callback_query(F.data=="games")
async def games_page(c:CallbackQuery):
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎲 تاس",callback_data="tool:dice"),InlineKeyboardButton(text="🔮 فال",callback_data="fortune")],[InlineKeyboardButton(text="📖 راهنما",callback_data="help:games"),InlineKeyboardButton(text="⬅️ منو",callback_data="main")]])
    await c.message.edit_text("🎮 <b>بازی‌ها</b>\n\nبازی‌های سبک داخلی: تاس و فال سرگرمی.",reply_markup=kb); await c.answer()

@dp.callback_query(F.data.in_(list(FEATURES.keys())))
async def feature(c:CallbackQuery):
    if not await allowed(c.from_user.id): await c.answer("ابتدا دسترسی را بگیر",show_alert=True); return
    title,body=FEATURES[c.data]
    rows=[[InlineKeyboardButton(text="📖 راهنمای این بخش",callback_data=f"help:{c.data}")]]
    if c.data=="media": rows.insert(0,[InlineKeyboardButton(text="🔒 روشن/خاموش",callback_data="toggle:media")])
    elif c.data=="games": rows.insert(0,[InlineKeyboardButton(text="🎲 تاس",callback_data="tool:dice")])
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
      "spam":"💣 <b>راهنمای ضداسپم</b>\n\nاین گزینه فقط رفتارهای تکراری پنل را محدود می‌کند و برای ارسال انبوه طراحی نشده است.",
      "currencies":"💰 <b>راهنمای ارزها</b>\n\nبا زدن هر ارز، نرخ لحظه‌ای از صفحه وب باز می‌شود. عدد ثابت داخل دیتابیس ذخیره نمی‌شود.",
      "tagall":"📢 <b>راهنمای تگ همه</b>\n\nتگ انبوه در این نسخه محدود شده تا مزاحمت و اسپم ایجاد نشود.",
      "buttons":"🎨 <b>راهنمای تنظیم دکمه‌ها</b>\n\nتم و وضعیت دکمه‌ها ذخیره می‌شود. محدودیت تلگرام این است که رنگ واقعی InlineKeyboard از سمت ربات قابل تغییر نیست.",
      "smart":"🤖 <b>راهنمای هوشمند</b>\n\nحالت هوشمند برای پاسخ‌های خودکار ساده است. پاسخ‌ها را کنترل کن و اگر سرویس هوش مصنوعی جداگانه‌ای نداری، انتظار پاسخ مدل خارجی نداشته باش.",
      "report":"📣 <b>راهنمای گزارش</b>\n\nمشکل را با <code>/report توضیح مشکل</code> برای مدیر ارسال کن. اطلاعات غیرضروری یا رمز ورود را داخل گزارش نفرست.",
      "tools":"🛠 <b>راهنمای ابزارها</b>\n\nاز این بخش به ساعت، پروفایل، تاس و فال دسترسی سریع داری. هر ابزار صفحه یا دکمه راهنمای مخصوص خودش را دارد.",
      "secretary":"🧠 <b>راهنمای منشی</b>\n\nبرای پاسخ خودکار از <code>/autoreply متن</code> استفاده کن. برای خاموش کردن، همان دستور را بدون متن بفرست.",
      "broadcast":"📢 <b>راهنمای اطلاعیه</b>\n\nمدیر می‌تواند روی یک پیام ریپلای کند و دستور <code>/broadcast</code> را بفرستد تا اطلاعیه برای کاربران تأییدشده ارسال شود. ارسال محدود و کنترل‌شده است.",
      "fortune":"🔮 <b>راهنمای فال</b>\n\nاز دکمه فال یا دستور مربوط به آن استفاده کن. نتیجه کاملاً سرگرمی و تصادفی است.",
      "secret":"🔐 <b>راهنمای متن رمزی</b>\n\nبرای تبدیل متن از <code>/secret متن</code> و برای بازکردن متن تولیدشده از <code>/unsecret کد</code> استفاده کن. برای اطلاعات حساس مناسب نیست.",
      "widgets":"🧩 <b>راهنمای ابزارک‌ها</b>\n\nابزارک‌های سریع مثل ساعت، پروفایل و تنظیمات پرکاربرد را از این بخش اجرا کن.",
      "backup":"📦 <b>راهنمای بکاپ</b>\n\nبکاپ روشن یک گروه خصوصی با نام «بکاپ» می‌سازد. پیام‌های پیوی بعد از فعال‌سازی در آن ذخیره می‌شوند. داخل هر چت از حساب سلف بنویس <code>بکاپ ۱۰</code> تا ۱۰ پیام آخر همان چت کپی شود؛ عدد را می‌توانی تا ۵۰ تغییر بده.\n\nنکته: گروه بکاپ را خصوصی نگه دار چون ممکن است پیام‌های دیگران داخل آن ذخیره شوند.",
      "self_users":"👤 <b>راهنمای کاربران</b>\n\n😈 دشمن = مسدود کردن فرستنده از قوانین اتوماسیون.\n💚 دوست = مجاز کردن فرستنده.\n😈 دشمن گروه / 💚 دوست گروه = قانون برای همان چت.\n🔒 قفل پیوی = پیام‌های خصوصی افراد غیرِ دوست در صورت امکان بلاک و حذف می‌شوند.\n🚫 بلاک = بلاک واقعی تلگرام.",
      "buttons":"🎨 <b>راهنمای دکمه‌ها</b>\n\nدکمه‌ها برای دسترسی سریع به قابلیت‌ها هستند. هر صفحه یک دکمه «📖 راهنمای این بخش» دارد؛ با زدن آن، آموزش همان قابلیت نمایش داده می‌شود.",
      "diamonds":f"🔴💎 <b>راهنمای الماس</b>\n\n🎁 بعد از اولین تأیید مدیر، {WELCOME_DIAMONDS} الماس هدیه می‌گیری.\n🔑 هنگام شروع موفق سلف، {SELF_ACTIVATION_COST} الماس کم می‌شود.\n📅 اعتبار دوره ۳۰ روز است و مصرف دوره به‌صورت ساعتی انجام می‌شود؛ موجودی یکجا صفر نمی‌شود.\n💰 هر ۱۰۰ الماس = ۱۰٬۰۰۰ تومان.\n📩 خرید الماس: {DIAMOND_ADMIN_USERNAME}\n👑 مدیر موجودی نامحدود دارد.",
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
    if not await is_manager(c.from_user.id):return
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
            await log_diamond(s, uid, c.from_user.id, welcome, "welcome", "هدیه تأیید مدیر")
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
    if not await is_manager(c.from_user.id):return
    uid=int(c.data.split(":")[1])
    async with Session() as s:u=await get_user(s,uid); u.approved=False; u.access_requested=False; u.pending_phone=None; await s.commit()
    await c.answer("رد شد"); await c.message.edit_reply_markup(reply_markup=None)


@dp.message(Command("filter"))
async def filter_cmd(m:Message):
    if not await allowed(m.from_user.id): return
    p=(m.text or "").split(maxsplit=1)
    async with Session() as s:
        u=await get_user(s,m.from_user.id)
        if len(p)==1:
            await m.answer(f"🚫 فیلتر: {'روشن' if u.word_filter else 'خاموش'}\n📜 {escape(u.word_filter_text or 'خالی')}")
            return
        u.word_filter_text=p[1].strip()[:2000]; u.word_filter=True; await s.commit()
    await m.answer("✅ فیلتر روشن شد و لیست کلمات ذخیره شد.")

@dp.message(Command("lockname"))
async def lockname_cmd(m:Message):
    if not await allowed(m.from_user.id): return
    p=(m.text or "").split(maxsplit=1)
    if len(p)!=2: await m.answer("مثال: /lockname نام پایه"); return
    async with Session() as s:
        u=await get_user(s,m.from_user.id); u.locked_name=p[1][:64]; u.name_lock=True; await s.commit()
    await m.answer("🛡 نام پایه ذخیره و حفاظت اسم روشن شد.")

@dp.message(Command("block"))
async def self_block(m:Message):
    if not await allowed(m.from_user.id): return
    p=(m.text or "").split(maxsplit=1)
    if len(p)!=2: await m.answer("مثال: /block @username"); return
    c=await get_self_client(m.from_user.id)
    if not c: await m.answer("❌ اتصال سلف فعال نیست."); return
    try:
        entity=await c.get_entity(p[1])
        await c(BlockRequest(entity))
        await m.answer("🚫 کاربر بلاک شد.")
    except Exception as e:
        log.warning("block failed: %s",e); await m.answer("❌ بلاک انجام نشد؛ یوزرنیم/شناسه را بررسی کن.")
    finally: await c.disconnect()

@dp.message(Command("unblock"))
async def self_unblock(m:Message):
    if not await allowed(m.from_user.id): return
    p=(m.text or "").split(maxsplit=1)
    if len(p)!=2: await m.answer("مثال: /unblock @username"); return
    c=await get_self_client(m.from_user.id)
    if not c: await m.answer("❌ اتصال سلف فعال نیست."); return
    try:
        entity=await c.get_entity(p[1])
        await c(UnblockRequest(entity))
        await m.answer("✅ رفع بلاک شد.")
    except Exception as e:
        log.warning("unblock failed: %s",e); await m.answer("❌ رفع بلاک انجام نشد.")
    finally: await c.disconnect()

@dp.message(Command("ban"))
async def ban(m:Message):
    if not await is_manager(m.from_user.id):return
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
    if not await is_manager(m.from_user.id):return
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
        await m.answer(f"🔴💎 موجودی شما: <b>{'∞' if m.from_user.id==OWNER_ID else (u.diamonds if u else 0)}</b>\n\n🎁 هدیه اولین تأیید: {WELCOME_DIAMONDS} الماس\n🔑 هزینه شروع سلف: {SELF_ACTIVATION_COST} الماس\n📅 اعتبار: ۳۰ روز\n📉 مصرف دوره: تا {SELF_MONTH_DIAMONDS:,} الماس به‌صورت ساعتی\n💰 هر ۱۰۰ الماس: ۱۰٬۰۰۰ تومان\n📩 خرید الماس: {DIAMOND_ADMIN_USERNAME}")
        return
    if not await is_manager(m.from_user.id): await m.answer("⛔ فقط مدیر می‌تواند موجودی کاربران را مدیریت کند."); return
    if len(p)!=3 or not p[1].lstrip("@").isdigit() or not p[2].lstrip("-").isdigit():
        await m.answer("نمونه: <code>/الماس 123456789 10</code>\nعدد منفی برای کم‌کردن الماس است.\n\n💰 هر ۱۰۰ الماس = ۱۰٬۰۰۰ تومان\n📩 خرید: <b>@jokm7</b>"); return
    uid=int(p[1].lstrip("@")); amount=int(p[2])
    async with Session() as s:
        u=await get_user(s,uid)
        if not u: await m.answer("❌ کاربر پیدا نشد."); return
        ok=await change_diamonds(s,uid,m.from_user.id,amount,"admin_add" if amount>0 else "admin_sub", "دستور مدیر");
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
    if not await is_manager(m.from_user.id) or not m.reply_to_message: await m.answer("📢 برای اطلاعیه، روی پیام ریپلای کن و /broadcast بزن."); return
    async with Session() as s: users=(await s.execute(select(User).where(User.approved==True,User.banned==False))).scalars().all()
    status=await m.answer(f"📢 ارسال کنترل‌شده برای {len(users)} کاربر…"); ok=bad=0
    for u in users:
        try: await m.bot.copy_message(u.id,m.chat.id,m.reply_to_message.message_id); ok+=1
        except Exception: bad+=1
        await asyncio.sleep(.08)
    await status.edit_text(f"✅ تمام شد\n📨 موفق: {ok}\n❌ ناموفق: {bad}")

@dp.message(F.text.func(lambda x: isinstance(x,str) and bool(ADMIN_FLOWS)))
async def admin_flow_message(m:Message):
    if m.from_user.id!=OWNER_ID or not m.text:
        return
    flow=ADMIN_FLOWS.get(m.from_user.id)
    if not flow: return
    value=m.text.strip()
    if value.startswith(".") and value in {".پنل"}:
        ADMIN_FLOWS.pop(m.from_user.id,None)
        await m.answer(await panel_text(m.from_user.id),reply_markup=main_kb(m.from_user.id)); return
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
    if flow["type"]=="delegate_add":
        if m.from_user.id != OWNER_ID:
            ADMIN_FLOWS.pop(m.from_user.id,None); return
        if not value.isdigit():
            await m.answer("❌ فقط آیدی عددی بفرست."); return
        target=int(value)
        if target==OWNER_ID:
            await m.answer("❌ مدیر اصلی خودش مدیر جانشین است."); return
        async with Session() as s:
            u=await get_user(s,target)
        if not u:
            await m.answer("❌ این کاربر هنوز در بات ثبت نشده است."); return
        ids=await delegated_admin_ids()
        if target not in ids: ids.append(target)
        await set_delegated_admin_ids(ids)
        ADMIN_FLOWS.pop(m.from_user.id,None)
        await m.answer(f"✅ <b>{escape(u.first_name or str(target))}</b> به‌عنوان مدیر جانشین تعیین شد.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👑 مدیریت مدیران",callback_data="delegated_admins")]])); return
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
            ok=await change_diamonds(s,uid,m.from_user.id,amount,"admin_add" if amount>0 else "admin_sub","مقدار سفارشی از پنل")
            u=await get_user(s,uid)
            if ok is None: await m.answer("❌ کاربر پیدا نشد."); return
            if ok is False: await m.answer("❌ موجودی کافی نیست."); return
            await s.commit(); bal=int(u.diamonds or 0)
        try: await m.bot.send_message(uid,f"🔴💎 موجودی الماس شما توسط مدیر تغییر کرد.\nموجودی جدید: <b>{bal:,}</b>")
        except Exception: pass
        await m.answer(f"✅ انجام شد. موجودی جدید: <b>{bal:,}</b>",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📋 مشخصات کاربری",callback_data=f"user_detail:{uid}"),InlineKeyboardButton(text="🔴💎 کیف پول",callback_data=f"diamond_user:{uid}")]])); return


def parse_duration(raw: str):
    import re
    m=re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", raw.lower())
    if not m: return None
    n=int(m.group(1)); unit=m.group(2)
    return n*{"s":1,"m":60,"h":3600,"d":86400}[unit]


def _target_from_command(m:Message):
    parts=(m.text or "").split(maxsplit=1)
    if len(parts)>1: return parts[1].strip()
    if m.reply_to_message and m.reply_to_message.from_user: return str(m.reply_to_message.from_user.id)
    return ""

async def _set_sender_rule_cmd(m:Message, mode:str):
    if not await allowed(m.from_user.id): return
    target=_target_from_command(m)
    if not target:
        await m.answer("روی پیام کاربر ریپلای کن یا @username / ID بده."); return
    async with Session() as s:
        try: entity=target if target.startswith("@") else int(target)
        except ValueError: entity=target
        sid=None
        # Bot can resolve usernames only through Telethon; for numeric IDs use directly.
        if isinstance(entity,int): sid=entity
        else:
            client=await get_self_client(m.from_user.id)
            if not client: await m.answer("❌ سلف فعال نیست."); return
            try: sid=int((await client.get_entity(entity)).id)
            except Exception: sid=None
            finally:
                try: await client.disconnect()
                except Exception: pass
        if not sid: await m.answer("❌ کاربر پیدا نشد."); return
        r=await s.scalar(select(SenderRule).where(SenderRule.user_id==m.from_user.id,SenderRule.sender_id==sid))
        if r: r.mode=mode
        else: s.add(SenderRule(user_id=m.from_user.id,sender_id=sid,mode=mode))
        await s.commit()
    await m.answer("💚 کاربر به دوست‌ها اضافه شد." if mode=="allow" else "😈 کاربر به دشمن‌ها اضافه شد.")

@dp.message(Command("دشمن"))
async def enemy_cmd(m:Message): await _set_sender_rule_cmd(m,"block")
@dp.message(Command("دوست"))
async def friend_cmd(m:Message): await _set_sender_rule_cmd(m,"allow")

async def _set_chat_rule_cmd(m:Message, mode:str):
    if not await allowed(m.from_user.id): return
    cid=int(m.chat.id)
    async with Session() as s:
        r=await s.scalar(select(ChatRule).where(ChatRule.user_id==m.from_user.id,ChatRule.chat_id==cid))
        if r: r.mode=mode
        else: s.add(ChatRule(user_id=m.from_user.id,chat_id=cid,mode=mode))
        await s.commit()
    await m.answer("💚 گروه به دوست‌ها اضافه شد." if mode=="allow" else "😈 گروه به دشمن‌ها اضافه شد.")

@dp.message(Command("دشمن_گروه"))
async def enemy_chat_cmd(m:Message): await _set_chat_rule_cmd(m,"block")
@dp.message(Command("دوست_گروه"))
async def friend_chat_cmd(m:Message): await _set_chat_rule_cmd(m,"allow")

@dp.message(Command("قفل_پیوی"))
async def private_lock_cmd(m:Message):
    if not await allowed(m.from_user.id): return
    async with Session() as s:
        u=await get_user(s,m.from_user.id); u.private_lock=True; await s.commit()
    await m.answer("🔒 قفل پیوی روشن شد. پیام‌های خصوصی از افراد غیرِ دوست نادیده گرفته و در صورت امکان بلاک می‌شوند.")

@dp.message(Command("باز_پیوی"))
async def private_unlock_cmd(m:Message):
    if not await allowed(m.from_user.id): return
    async with Session() as s:
        u=await get_user(s,m.from_user.id); u.private_lock=False; await s.commit()
    await m.answer("🔓 پیوی باز شد.")

@dp.message(Command("allowchat"))
async def allowchat_cmd(m: Message):
    if not await allowed(m.from_user.id): return
    parts=(m.text or "").split()
    if len(parts)!=2 or not parts[1].lstrip("-").isdigit():
        await m.answer("فرمت: <code>/allowchat -1001234567890</code>"); return
    cid=int(parts[1])
    async with Session() as s:
        r=await s.scalar(select(ChatRule).where(ChatRule.user_id==m.from_user.id,ChatRule.chat_id==cid))
        if r: r.mode="allow"
        else: s.add(ChatRule(user_id=m.from_user.id,chat_id=cid,mode="allow"))
        await s.commit()
    await m.answer("✅ این چت برای اتوماسیون مجاز شد.")

@dp.message(Command("denychat"))
async def denychat_cmd(m: Message):
    if not await allowed(m.from_user.id): return
    parts=(m.text or "").split(); cid=int(parts[1]) if len(parts)==2 and parts[1].lstrip("-").isdigit() else m.chat.id
    async with Session() as s:
        r=await s.scalar(select(ChatRule).where(ChatRule.user_id==m.from_user.id,ChatRule.chat_id==cid))
        if r: r.mode="block"
        else: s.add(ChatRule(user_id=m.from_user.id,chat_id=cid,mode="block"))
        await s.commit()
    await m.answer(f"🚫 چت <code>{cid}</code> از اتوماسیون مستثنی شد.")

@dp.message(Command("allowuser"))
async def allowuser_cmd(m: Message):
    if not await allowed(m.from_user.id): return
    parts=(m.text or "").split(); target=parts[1] if len(parts)>1 else (str(m.reply_to_message.from_user.id) if m.reply_to_message and m.reply_to_message.from_user else "")
    if not target.lstrip("-").isdigit(): await m.answer("فرمت: <code>/allowuser ID</code> یا روی پیام کاربر ریپلای کن."); return
    sid=int(target)
    async with Session() as s:
        r=await s.scalar(select(SenderRule).where(SenderRule.user_id==m.from_user.id,SenderRule.sender_id==sid))
        if r:r.mode="allow"
        else:s.add(SenderRule(user_id=m.from_user.id,sender_id=sid,mode="allow"))
        await s.commit()
    await m.answer("✅ این کاربر برای اتوماسیون مجاز شد.")

@dp.message(Command("denyuser"))
async def denyuser_cmd(m: Message):
    if not await allowed(m.from_user.id): return
    parts=(m.text or "").split(); target=parts[1] if len(parts)>1 else (str(m.reply_to_message.from_user.id) if m.reply_to_message and m.reply_to_message.from_user else "")
    if not target.lstrip("-").isdigit(): await m.answer("فرمت: <code>/denyuser ID</code> یا روی پیام کاربر ریپلای کن."); return
    sid=int(target)
    async with Session() as s:
        r=await s.scalar(select(SenderRule).where(SenderRule.user_id==m.from_user.id,SenderRule.sender_id==sid))
        if r:r.mode="block"
        else:s.add(SenderRule(user_id=m.from_user.id,sender_id=sid,mode="block"))
        await s.commit()
    await m.answer("🚫 این کاربر از اتوماسیون مستثنی شد.")

@dp.message(Command("rules"))
async def rules_cmd(m: Message):
    if not await allowed(m.from_user.id): return
    async with Session() as s:
        cr=(await s.execute(select(ChatRule).where(ChatRule.user_id==m.from_user.id))).scalars().all()
        sr=(await s.execute(select(SenderRule).where(SenderRule.user_id==m.from_user.id))).scalars().all()
    lines=["🛡 <b>قوانین اتوماسیون</b>",""]
    lines += [f"💬 چت {r.chat_id}: {'اجازه' if r.mode=='allow' else 'ممنوع'}" for r in cr[:20]]
    lines += [f"👤 کاربر {r.sender_id}: {'اجازه' if r.mode=='allow' else 'ممنوع'}" for r in sr[:20]]
    await m.answer("\n".join(lines) if len(lines)>2 else "قانونی ثبت نشده است.")

@dp.message(Command("remind"))
async def remind_cmd(m: Message):
    if not await allowed(m.from_user.id): return
    parts=(m.text or "").split(maxsplit=2)
    if len(parts)<3:
        await m.answer("فرمت: <code>/remind 30m متن یادآوری</code>\nواحدها: s/m/h/d"); return
    seconds=parse_duration(parts[1])
    if seconds is None or seconds<5 or seconds>31536000:
        await m.answer("⏱ زمان نامعتبر است. نمونه: 20m، 2h، 1d"); return
    due=datetime.now(timezone.utc)+timedelta(seconds=seconds)
    async with Session() as s:
        r=Reminder(user_id=m.from_user.id,chat_id=m.chat.id,text=parts[2],due_at=due)
        s.add(r); await s.commit(); rid=r.id
    await m.answer(f"⏰ یادآوری #{rid} ثبت شد و در {parts[1]} اجرا می‌شود.")

@dp.message(Command("reminders"))
async def reminders_cmd(m: Message):
    if not await allowed(m.from_user.id): return
    async with Session() as s:
        rows=(await s.execute(select(Reminder).where(Reminder.user_id==m.from_user.id,Reminder.done==False).order_by(Reminder.due_at).limit(20))).scalars().all()
    if not rows: await m.answer("⏰ یادآوری فعالی نداری."); return
    await m.answer("\n".join([f"#{r.id} — {escape(r.text[:100])}\n⏱ {r.due_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}" for r in rows]))

@dp.message(Command("reminddel"))
async def reminddel_cmd(m: Message):
    if not await allowed(m.from_user.id): return
    parts=(m.text or "").split()
    if len(parts)!=2 or not parts[1].isdigit(): await m.answer("فرمت: <code>/reminddel ID</code>"); return
    async with Session() as s:
        r=await s.scalar(select(Reminder).where(Reminder.id==int(parts[1]),Reminder.user_id==m.from_user.id))
        if not r: await m.answer("❌ پیدا نشد."); return
        r.done=True; await s.commit()
    await m.answer("🗑 یادآوری حذف شد.")

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
    global BOT_USERNAME
    await ensure_schema()
    bot=Bot(TOKEN,default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    BOT_USERNAME = me.username or ""
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
