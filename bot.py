import asyncio
import json
import logging
import os
import secrets
import hashlib
import hmac
import time
from urllib.parse import parse_qsl
from datetime import datetime, timezone

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, FSInputFile, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("diamond-games")

TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
# Accept either Railway variable name; default to the group ID supplied by the user.
_raw_game_chat_id = (
    os.getenv("GAME_CHAT_ID", "").strip()
    or os.getenv("GAME_GROUP_ID", "").strip()
    or "-1003816728822"
)
try:
    GAME_CHAT_ID = int(_raw_game_chat_id)
except ValueError as exc:
    raise RuntimeError("GAME_CHAT_ID must be a numeric Telegram group ID, e.g. -1003816728822") from exc
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
RANDOM_ORG_API_KEY = os.getenv("RANDOM_ORG_API_KEY", "").strip()
JOIN_TIMEOUT_SECONDS = max(30, int(os.getenv("JOIN_TIMEOUT_SECONDS", "180")))
STARTING_DIAMONDS = max(0, int(os.getenv("STARTING_DIAMONDS", "100")))
MAX_STAKE = 2_000_000_000  # practical DB integer limit; user-facing stake has no configured cap
SUPPORT_CHAT = "@MMD_SELFF"
CARD_NUMBER = "6219861455298157"
CARD_HOLDER_DISPLAY = "دلیر"
PACK_PRICE_TOMAN = 40000
REFERRAL_REWARD = 100
REFERRAL_MILESTONE_COUNT = 6
REFERRAL_MILESTONE_BONUS = 1000
MIN_TRANSFER = 40
MIN_STAKE = 20
TAX_BRACKETS = ((10000, 10), (5000, 7), (1000, 4), (500, 2), (0, 0))

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is required in Railway Variables")
if not OWNER_ID:
    raise RuntimeError("OWNER_ID is required in Railway Variables")

def normalize_db_url(url: str) -> str:
    if not url:
        return "sqlite+aiosqlite:///./diamonds.db"
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    if url.startswith("postgresql+asyncpg://") or url.startswith("sqlite+aiosqlite:///"):
        return url
    if url.startswith("sqlite:///"):
        return "sqlite+aiosqlite:///" + url[len("sqlite:///"):]
    raise RuntimeError("Unsupported DATABASE_URL. Use Railway PostgreSQL or SQLite URL.")

engine = create_async_engine(normalize_db_url(DATABASE_URL), pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)
router = Router()
bot: Bot

class Base(DeclarativeBase):
    pass

class Player(Base):
    __tablename__ = "players"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str] = mapped_column(String(128), default="")
    diamonds: Mapped[int] = mapped_column(Integer, default=STARTING_DIAMONDS)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class Game(Base):
    __tablename__ = "games"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    creator_id: Mapped[int] = mapped_column(BigInteger)
    stake: Mapped[int] = mapped_column(Integer)
    seats: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open/finished/cancelled
    mode: Mapped[str] = mapped_column(String(16), default="users")  # bot/users
    players_json: Mapped[str] = mapped_column(Text, default="[]")
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    result_text: Mapped[str | None] = mapped_column(Text, nullable=True)

class Ledger(Base):
    __tablename__ = "ledger"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    amount: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(128))
    game_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class Referral(Base):
    __tablename__ = "referrals"
    invitee_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    inviter_id: Mapped[int] = mapped_column(BigInteger, index=True)
    reward: Mapped[int] = mapped_column(Integer, default=REFERRAL_REWARD)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class SelfPreference(Base):
    __tablename__ = "self_preferences"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class Purchase(Base):
    __tablename__ = "purchases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    diamonds: Mapped[int] = mapped_column(Integer)
    amount_toman: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    receipt_file_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

def game_kb(game_id: str, seats: int, joined: int):
    # Compact VIP-style game card; all actions are real callbacks.
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💚 پیوستن به بازی ({joined}/{seats})", callback_data=f"join:{game_id}"),
         InlineKeyboardButton(text="❤️ لغو بازی", callback_data=f"cancel:{game_id}")],
        [InlineKeyboardButton(text="💎 جایزه و مبلغ", callback_data=f"gameinfo:prize:{game_id}"),
         InlineKeyboardButton(text="👥 بازیکنان", callback_data=f"gameinfo:players:{game_id}")]
    ])

def mode_kb(prompt_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 دونفره", callback_data=f"mode:2:{prompt_id}"),
         InlineKeyboardButton(text="👥 سه‌نفره", callback_data=f"mode:3:{prompt_id}")]
    ])

def opponent_kb(prompt_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 بازی با ربات", callback_data=f"kind:bot:{prompt_id}"),
         InlineKeyboardButton(text="👥 بازی با کاربران", callback_data=f"kind:users:{prompt_id}")]
    ])


def account_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛠 مدیریت سلف", callback_data="panel:self"), InlineKeyboardButton(text="✨ قابلیت‌های سلف", callback_data="panel:capabilities")],
        [InlineKeyboardButton(text="👤 حساب کاربری", callback_data="panel:account"), InlineKeyboardButton(text="💎 خرید الماس", callback_data="shop:open")],
        [InlineKeyboardButton(text="👥 زیرمجموعه‌گیری", callback_data="referral:open"), InlineKeyboardButton(text="🆘 پشتیبانی", callback_data="support:open")],
        [InlineKeyboardButton(text="🔗 گپ پشتیبانی", url="https://t.me/MMD_SELFF")]
    ])

async def branded_answer(message: Message, text: str, reply_markup=None):
    """Show the project image on main bot pages when the asset is available."""
    try:
        photo = FSInputFile(os.path.join(os.path.dirname(__file__), "mmd_self_menu.png"))
        await message.reply_photo(photo, caption=text, reply_markup=reply_markup)
    except Exception:
        await message.reply(text, reply_markup=reply_markup)

async def branded_callback(call: CallbackQuery, text: str, reply_markup=None):
    try:
        photo = FSInputFile(os.path.join(os.path.dirname(__file__), "mmd_self_menu.png"))
        await call.message.answer_photo(photo, caption=text, reply_markup=reply_markup)
    except Exception:
        await call.message.answer(text, reply_markup=reply_markup)

support_waiting: set[int] = set()
purchase_waiting_receipt: set[int] = set()


async def get_or_create_player(tg_user):
    return await ensure_player(tg_user)

async def account_status_text(uid: int) -> str:
    async with Session() as s:
        p = await s.get(Player, uid)
    if not p:
        return "📡 وضعیت سلف: ثبت‌نشده"
    if not p.active:
        return "📡 وضعیت سلف: غیرفعال"
    return "📡 وضعیت سلف: حساب بازی فعال است. اتصال نشست اکانت تلگرام در این نسخه ارائه نمی‌شود."

async def ensure_player(tg_user):
    async with Session.begin() as s:
        p = await s.get(Player, tg_user.id)
        if p is None:
            p = Player(
                user_id=tg_user.id, username=tg_user.username,
                first_name=tg_user.full_name, diamonds=STARTING_DIAMONDS
            )
            s.add(p)
        else:
            p.username = tg_user.username
            p.first_name = tg_user.full_name
    async with Session() as s:
        return await s.get(Player, tg_user.id)

OWNER_UNLIMITED_BALANCE = 10**18

async def balance_of(uid: int) -> int:
    # Owner has unlimited virtual game diamonds.
    if uid == OWNER_ID:
        return OWNER_UNLIMITED_BALANCE
    async with Session() as s:
        p = await s.get(Player, uid)
        return p.diamonds if p else 0

def balance_label(uid: int, amount: int | None = None) -> str:
    if uid == OWNER_ID:
        return "∞ (نامحدود)"
    return str(amount if amount is not None else 0)

async def ledger_add(s, uid: int, amount: int, reason: str, game_id: str | None = None):
    s.add(Ledger(user_id=uid, amount=amount, reason=reason, game_id=game_id))

async def owner_only(message: Message) -> bool:
    if message.from_user and message.from_user.id == OWNER_ID:
        return True
    await message.reply("⛔ این دستور فقط برای مالک است.")
    return False

async def random_index(max_value: int) -> tuple[int, str]:
    """Return a zero-based winner index and provenance. Fail closed if configured API fails."""
    if RANDOM_ORG_API_KEY:
        payload = {
            "jsonrpc": "2.0",
            "method": "generateIntegers",
            "params": {
                "apiKey": RANDOM_ORG_API_KEY,
                "n": 1,
                "min": 0,
                "max": max_value - 1,
                "replacement": True
            },
            "id": secrets.randbelow(1_000_000_000)
        }
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post("https://api.random.org/json-rpc/4/invoke", json=payload) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200 or "error" in data or "result" not in data:
                    raise RuntimeError("RANDOM.ORG request failed")
                result = data["result"]
                values = result.get("random", {}).get("data", [])
                if len(values) != 1 or not isinstance(values[0], int) or not 0 <= values[0] < max_value:
                    raise RuntimeError("RANDOM.ORG returned invalid result")
                serial = result.get("random", {}).get("serialNumber", "n/a")
                return values[0], f"RANDOM.ORG serial #{serial}"
    # If no API key is configured, use Python's cryptographic system RNG and disclose it.
    return secrets.randbelow(max_value), "قرعه‌کشی تصادفی امن انجام شد"

async def refund_game(game_id: str, reason: str):
    async with Session.begin() as s:
        game = await s.get(Game, game_id)
        if not game or game.status != "open":
            return False
        players = json.loads(game.players_json or "[]")
        game.status = "cancelled"
        game.result_text = reason
        for uid in players:
            p = await s.get(Player, int(uid))
            if p:
                p.diamonds += game.stake
                await ledger_add(s, int(uid), game.stake, "game_refund", game_id)
    return True

async def render_game(game_id: str, extra: str = ""):
    async with Session() as s:
        g = await s.get(Game, game_id)
        if not g:
            return
        ids = json.loads(g.players_json or "[]")
        players = []
        for uid in ids:
            p = await s.get(Player, int(uid))
            players.append(p.first_name if p else str(uid))
        status = {"open": "⏳ منتظر بازیکن", "finished": "🏁 پایان‌یافته", "cancelled": "❌ لغوشده"}.get(g.status, g.status)
        mode_label = '🤖 مقابل ربات' if g.mode == 'bot' else ('👥 دونفره' if g.seats == 2 else '👥 سه‌نفره')
        body = (
            f"👑 <b>VIP-SELF</b> · <code>#{g.id}</code>\n"
            f"━━━━━━━━━━━━━━\n"
            f"🎮 حالت: {mode_label}\n"
            f"💎 مبلغ ورود: <b>{g.stake:,}</b> الماس\n"
            f"🏆 جایزهٔ سیستمی: <b>{g.stake * 2:,}</b> الماس\n"
            f"👥 بازیکنان: <b>{len(ids)}/{g.seats}</b>\n"
            f"👤 شرکت‌کنندگان: " + ("، ".join(players) if players else "—") + "\n"
            f"📌 وضعیت: {status}"
        )
        if extra:
            body += "\n\n" + extra
        kb = game_kb(g.id, g.seats, len(ids)) if g.status == "open" and g.mode == "users" else None
        chat_id, msg_id = g.chat_id, g.message_id
    if not msg_id:
        return
    try:
        await bot.edit_message_caption(caption=body, chat_id=chat_id, message_id=msg_id, reply_markup=kb)
    except TelegramBadRequest as exc:
        # Compatibility fallback for legacy text game messages from earlier deployments.
        try:
            await bot.edit_message_text(body, chat_id=chat_id, message_id=msg_id, reply_markup=kb)
        except TelegramBadRequest:
            log.debug("Game card was not editable: %s", exc)

@router.callback_query(F.data.startswith("gameinfo:"))
async def game_info_callback(call: CallbackQuery):
    _, kind, game_id = call.data.split(":", 2)
    async with Session() as s:
        g = await s.get(Game, game_id)
        if not g:
            await call.answer("این بازی پیدا نشد.", show_alert=True)
            return
        ids = [int(x) for x in json.loads(g.players_json or "[]")]
        players = []
        for uid in ids:
            player = await s.get(Player, uid)
            players.append(player.first_name if player else str(uid))
        if kind == "prize":
            text = f"💎 مبلغ ورود هر نفر: {g.stake:,}\n🏆 پاداش سیستمی احتمالی: {g.stake * 2:,}\nتوجه: نتیجه با قرعه‌کشی امن تعیین می‌شود؛ برد تضمین‌شده نیست."
        else:
            text = f"👥 بازیکنان ({len(ids)}/{g.seats}):\n" + ("\n".join(f"• {name}" for name in players) if players else "هنوز کسی وارد نشده.")
    await call.answer(text, show_alert=True)

async def finish_game(game_id: str):
    # State transition and reward are committed once. The winner receives a fixed system prize;
    # losing players' entry fees are not transferred to the winner.
    async with Session.begin() as s:
        g = await s.get(Game, game_id)
        if not g or g.status != "open":
            return
        ids = [int(x) for x in json.loads(g.players_json or "[]")]
        if len(ids) != g.seats:
            return
        g.status = "finished"
        try:
            winner_index, source = await random_index(len(ids))
        except Exception as e:
            log.exception("Random source failed for game %s", game_id)
            g.status = "open"
            return
        winner_id = ids[winner_index]
        winner = await s.get(Player, winner_id)
        if not winner:
            g.status = "cancelled"
            for uid in ids:
                p = await s.get(Player, uid)
                if p:
                    p.diamonds += g.stake
                    await ledger_add(s, uid, g.stake, "game_refund", game_id)
            return
        prize = g.stake * 2
        winner.diamonds += prize
        await ledger_add(s, winner_id, prize, "game_prize", game_id)
        winner_name = winner.first_name
        balance_lines = [f"💎 موجودی برنده ({winner_name}): {balance_label(winner_id, winner.diamonds)}"]
        for uid in ids:
            if uid == winner_id:
                continue
            loser = await s.get(Player, uid)
            if loser:
                balance_lines.append(f"💎 موجودی بازنده ({loser.first_name}): {balance_label(uid, loser.diamonds)}")
        g.result_text = (f"🏆 برنده: {winner_name}\n🎁 پاداش سیستمی: {prize} 💎\n" +
                        "\n".join(balance_lines) + f"\n🎲 {source}")
        chat_id = g.chat_id
        msg_id = g.message_id
        result_text = g.result_text
    await render_game(game_id, "🎉 <b>نتیجه نهایی</b>\n" + result_text)

async def expire_games_loop():
    while True:
        try:
            cutoff = datetime.now(timezone.utc).timestamp() - JOIN_TIMEOUT_SECONDS
            async with Session() as s:
                games = (await s.execute(select(Game).where(Game.status == "open", Game.mode == "users"))).scalars().all()
                stale = [g.id for g in games if g.created_at and g.created_at.timestamp() < cutoff]
            for gid in stale:
                if await refund_game(gid, "زمان انتظار تمام شد؛ هزینه ورود به بازیکنان برگشت."):
                    await render_game(gid, "⌛ زمان انتظار تمام شد؛ هزینه ورود به بازیکنان برگشت.")
        except Exception:
            log.exception("expiry loop error")
        await asyncio.sleep(15)

@router.message(CommandStart())
async def start(message: Message):
    if message.chat.type != ChatType.PRIVATE:
        return
    await ensure_player(message.from_user)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("ref_"):
        try:
            inviter_id = int(parts[1][4:])
        except ValueError:
            inviter_id = 0
        if inviter_id and inviter_id != message.from_user.id:
            async with Session.begin() as s:
                existing = await s.get(Referral, message.from_user.id)
                inviter = await s.get(Player, inviter_id)
                if existing is None and inviter is not None:
                    # One-time referral credit for each new invitee.
                    s.add(Referral(invitee_id=message.from_user.id, inviter_id=inviter_id, reward=REFERRAL_REWARD))
                    inviter.diamonds += REFERRAL_REWARD
                    await ledger_add(s, inviter_id, REFERRAL_REWARD, "referral_reward")
                    # A one-time milestone bonus is granted when the inviter reaches six successful invites.
                    # The ledger marker prevents duplicate bonuses on later /start messages.
                    prior_count = await s.scalar(
                        select(func.count()).select_from(Referral).where(Referral.inviter_id == inviter_id)
                    ) or 0
                    milestone_paid = await s.scalar(
                        select(Ledger.id).where(
                            Ledger.user_id == inviter_id,
                            Ledger.reason == "referral_milestone_6_bonus"
                        ).limit(1)
                    )
                    milestone_bonus = 0
                    if prior_count >= REFERRAL_MILESTONE_COUNT and milestone_paid is None:
                        inviter.diamonds += REFERRAL_MILESTONE_BONUS
                        milestone_bonus = REFERRAL_MILESTONE_BONUS
                        await ledger_add(s, inviter_id, REFERRAL_MILESTONE_BONUS, "referral_milestone_6_bonus")
                    try:
                        message_text = f"🎉 یک نفر با لینک شما وارد شد؛ {REFERRAL_REWARD} 💎 به موجودی‌تان اضافه شد."
                        if milestone_bonus:
                            message_text += f"\n🏆 تبریک! به ۶ دعوت موفق رسیدی و {milestone_bonus} الماس جایزهٔ ویژه گرفتی."
                        await bot.send_message(inviter_id, message_text)
                    except Exception:
                        pass
    await branded_answer(message,
        f"👋 سلام {message.from_user.full_name}!\n\n"
        "🌟 <b>به MMD SELF خوش آمدی!</b>\n"
        "به نسخه بهتر سلف خوش اومدی. 🔔\n\n"
        "لطفاً قبل از استفاده، قوانین گروه و ربات را رعایت کن تا حسابت دچار محدودیت نشود. ⭐\n\n"
        "🛠 از دکمه‌های زیر برای مدیریت حساب، قابلیت‌ها، خرید الماس و پشتیبانی استفاده کن.\n\n"
        "💎 هر ۱۵۰۰ الماس = ۳۰ روز فعال‌سازی (۴۰٬۰۰۰ تومان).",
        reply_markup=account_kb())

@router.message(F.new_chat_members)
async def welcome_new_group_members(message: Message):
    """Welcome new members to the configured game group with the MMD SELF image."""
    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return
    members = [member for member in (message.new_chat_members or []) if not member.is_bot]
    if not members:
        return
    names = ", ".join(member.full_name for member in members)
    welcome_text = (
        f"👋 سلام {names}!\n"
        "🌟 <b>به گروه MMD SELF خوش آمدید!</b> 📣\n\n"
        "قبل از انجام هر کاری، لطفاً قوانین گروه را رعایت کنید تا دچار محدودیت نشوید. ⭐\n\n"
        "برای استفاده از قابلیت‌های ربات، وارد گفت‌وگوی خصوصی ربات شوید و دستور /start را بفرستید.\n"
        "💎 برای راهنمایی و پشتیبانی: @jokm7"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆘 پشتیبانی", url="https://t.me/jokm7"),
         InlineKeyboardButton(text="💬 گپ پشتیبانی", url="https://t.me/MMD_SELFF")]
    ])
    try:
        photo = FSInputFile(os.path.join(os.path.dirname(__file__), "mmd_self_menu.png"))
        await message.answer_photo(photo, caption=welcome_text, reply_markup=keyboard)
    except Exception:
        await message.answer(welcome_text, reply_markup=keyboard)


@router.message(F.text.regexp(r"(?i)^\s*(?:پنل(?:\s+مدیریت)?|مدیریت\s+سلف|/panel(?:@\w+)?|/start(?:@\w+)?)(?:\s*)$"))
async def open_panel_text(message: Message):
    # Global panel entry point: private chat, group, and supergroup.
    # Game handlers separately enforce GAME_CHAT_ID.
    await branded_answer(
        message,
        "🌟 <b>پنل مدیریت MMD SELF</b>\nانتخاب کن؛ این پنل از پی‌وی و گروه‌ها قابل بازشدن است. بازی فقط در گروه اختصاصی اجرا می‌شود:",
        reply_markup=account_kb(),
    )

@router.callback_query(F.data == "panel:account")
async def panel_account(call: CallbackQuery):
    p = await ensure_player(call.from_user)
    uname = f"@{call.from_user.username}" if call.from_user.username else "تنظیم نشده"
    await branded_callback(call, f"👤 <b>حساب کاربری</b>\nنام: {call.from_user.full_name}\nآیدی عددی: <code>{call.from_user.id}</code>\nنام کاربری: {uname}\n💎 موجودی: {balance_label(call.from_user.id, p.diamonds)}\n🛠 وضعیت سلف: در این نسخه اتصال حساب فعال نیست.", account_kb())
    await call.answer()

# Only commands that have executable handlers are shown in the active capabilities UI.
# Deferred/incomplete items are documented in DEFERRED_FEATURES.md instead of being presented as working.
FEATURE_CATEGORIES = {
    "transfer": ("💎 انتقال الماس", "فعال: انتقال [عدد] [آیدی/یوزرنیم] یا روی پیام کاربر ریپلای کن و بنویس انتقال [عدد]. انتقال فقط از موجودی الماس ربات انجام می‌شود؛ انتقال پول یا دارایی تلگرام نیست."),
    "textstyle": ("🔤 استایل متن", "فعال: /format bold متن، /format italic متن، /format code متن، /format spoiler متن، /format underline متن، /format strike متن. خروجی با قالب‌بندی رسمی تلگرام فرستاده می‌شود."),
    "clock": ("🕒 ساعت", "فعال: /clock یا /time برای نمایش زمان محلی محیط اجرای بات. این دستور نام یا بیوی حساب شخصی را تغییر نمی‌دهد."),
    "user": ("👤 اطلاعات کاربر", "فعال: /userinfo یا /id؛ با ریپلای روی پیام، اطلاعات عمومی همان کاربر نمایش داده می‌شود. اطلاعات خصوصی استخراج نمی‌شود."),
    "math": ("🧮 ماشین‌حساب", "فعال: /math (12+8)*3 یا /calc 4×7. فقط عبارت‌های عددی امن پشتیبانی می‌شوند و کد دلخواه اجرا نمی‌شود."),
    "translate": ("🌐 ترجمه", "فعال: /translate hello برای ترجمه به فارسی؛ /translate en سلام برای ترجمه به انگلیسی؛ یا روی پیام ریپلای کن و /translate یا «ترجمه» بفرست. نیازمند اینترنت خروجی است."),
}


async def get_self_enabled(uid: int) -> bool:
    async with Session() as s:
        pref = await s.get(SelfPreference, uid)
        return pref.enabled if pref else True

@router.callback_query(F.data == "panel:self")
async def panel_self(call: CallbackQuery):
    enabled = await get_self_enabled(call.from_user.id)
    status = "🟢 روشن" if enabled else "🔴 خاموش"
    await branded_callback(call, "🛠 <b>مدیریت MMD SELF</b>\n\n<b>وضعیت نشست حساب شخصی:</b> متصل نیست\n<b>وضعیت ربات:</b> فعال در صورت روشن‌بودن سرویس\n\nاین صفحه دیگر دکمهٔ روشن/خاموش نمایشی ندارد. فقط قابلیت‌هایی در فهرست فعال دیده می‌شوند که هندلر اجرایی دارند. موارد ناتمام در فایل <code>DEFERRED_FEATURES.md</code> نگهداری شده‌اند.\n\nبرای راهنمای ورود امن از دکمهٔ زیر استفاده کن؛ کد ورود را داخل چت ربات نفرست.", InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 راهنمای اتصال امن حساب", callback_data="panel:connect_help")],
        [InlineKeyboardButton(text="✨ قابلیت‌های فعال", callback_data="panel:capabilities")],
        [InlineKeyboardButton(text="⬅️ منوی اصلی", callback_data="panel:home")]
    ]))
    await call.answer()

@router.callback_query(F.data == "panel:self_toggle")
async def panel_self_toggle(call: CallbackQuery):
    async with Session.begin() as s:
        pref = await s.get(SelfPreference, call.from_user.id)
        if pref is None:
            pref = SelfPreference(user_id=call.from_user.id, enabled=False)
            s.add(pref)
        else:
            pref.enabled = not pref.enabled
            pref.updated_at = datetime.now(timezone.utc)
    await panel_self(call)

@router.callback_query(F.data == "panel:self_delete")
async def panel_self_delete(call: CallbackQuery):
    async with Session.begin() as s:
        pref = await s.get(SelfPreference, call.from_user.id)
        if pref:
            await s.delete(pref)
    await branded_callback(call, "تنظیمات محلی پنل پاک شد. نشست حساب تلگرام در این نسخه‌ای ذخیره نشده که حذف شود.", account_kb())
    await call.answer()

@router.callback_query(F.data == "panel:connect_help")
async def panel_connect_help(call: CallbackQuery):
    # Telegram's native contact-request button shares the account's own contact.
    # It does not authenticate a Telegram user session and we do not store the number.
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 ارسال شمارهٔ خودم", request_contact=True)],
                  [KeyboardButton(text="❌ بستن",)]],
        resize_keyboard=True, one_time_keyboard=True,
        input_field_placeholder="برای اشتراک شماره، دکمه را بزن")
    await call.message.answer(
        "📱 <b>ارسال شماره</b>\nدکمهٔ پایین صفحه را بزن تا شمارهٔ متصل به حساب تلگرامت را از طریق گزینهٔ رسمی تلگرام بفرستی. شماره فقط برای همین مرحله دریافت می‌شود و ذخیره نمی‌شود.\n\n⚠️ این کار به‌تنهایی وارد حساب شخصی نمی‌شود. برای محافظت از حسابت، کد ورود و رمز دومرحله‌ای را داخل چت بات وارد نکن؛ کد باید فقط در فرایند امنی که روی دستگاه خودت اجرا می‌شود وارد شود.",
        reply_markup=keyboard)
    await call.answer()

@router.message(F.contact)
async def handle_shared_contact(message: Message):
    contact = message.contact
    if not contact or contact.user_id != message.from_user.id:
        await message.answer("❌ لطفاً فقط شمارهٔ خودت را با دکمهٔ رسمی تلگرام بفرست.", reply_markup=ReplyKeyboardRemove())
        return
    # Accept Persian/Arabic digit variants and common punctuation for display/validation.
    raw = contact.phone_number or ""
    translation = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    normalized = "".join(ch for ch in raw.translate(translation) if ch.isdigit() or ch == "+")
    digits = "".join(ch for ch in normalized if ch.isdigit())
    if not 7 <= len(digits) <= 15:
        await message.answer("شماره دریافتی معتبر به نظر نمی‌رسد؛ دوباره از دکمهٔ ارسال شماره استفاده کن.", reply_markup=ReplyKeyboardRemove())
        return
    await message.answer(
        "✅ شماره از طریق دکمهٔ رسمی تلگرام دریافت شد و ذخیره نشد.\n\nبرای ادامهٔ ورود امن، فایل <code>secure_login.py</code> را روی کامپیوتر خودت اجرا کن؛ شماره، کد (حتی با ارقام فارسی/عربی) و رمز دومرحله‌ای فقط همان‌جا وارد می‌شوند. کد ورود را در این چت نفرست. راهنمای کامل در README آمده است.",
        reply_markup=ReplyKeyboardRemove())

@router.message(F.text == "❌ بستن")
async def close_contact_keyboard(message: Message):
    await message.answer("صفحهٔ ارسال شماره بسته شد.", reply_markup=ReplyKeyboardRemove())

@router.callback_query(F.data == "panel:capabilities")
async def panel_capabilities(call: CallbackQuery):
    keys = list(FEATURE_CATEGORIES.keys())
    rows = []
    for i in range(0, len(keys), 2):
        row = [InlineKeyboardButton(text=FEATURE_CATEGORIES[keys[i]][0], callback_data=f"feature:{keys[i]}")]
        if i + 1 < len(keys):
            row.append(InlineKeyboardButton(text=FEATURE_CATEGORIES[keys[i+1]][0], callback_data=f"feature:{keys[i+1]}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ منوی اصلی", callback_data="panel:home")])
    await branded_callback(call, f"✨ <b>راهنمای قابلیت‌های MMD SELF</b>\nتعداد بخش‌ها: {len(keys)}\nفقط ابزارهایی نمایش داده می‌شوند که در این نسخه هندلر اجرایی دارند. قابلیت‌های ناقص از منوی فعال حذف شده‌اند و در فایل DEFERRED_FEATURES.md فهرست شده‌اند.", InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()

@router.callback_query(F.data.startswith("feature:"))
async def feature_detail(call: CallbackQuery):
    key = call.data.split(":", 1)[1]
    item = FEATURE_CATEGORIES.get(key)
    if not item:
        await call.answer("این بخش پیدا نشد.", show_alert=True)
        return
    title, help_text = item
    await branded_callback(call, f"<b>{title}</b>\n\n{help_text}", InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ بازگشت به قابلیت‌ها", callback_data="panel:capabilities")],
        [InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="panel:home")]
    ]))
    await call.answer()

@router.callback_query(F.data.startswith("font:"))
async def font_preview(call: CallbackQuery):
    await call.answer("پیش‌نمایش فونت انتخاب شد؛ اعمال روی نام و بیو در این نسخه فعال نیست.", show_alert=True)

@router.callback_query(F.data == "panel:home")
async def panel_home(call: CallbackQuery):
    await branded_callback(call, "🌟 <b>پنل MMD SELF</b>", account_kb())
    await call.answer()

@router.callback_query(F.data == "referral:open")
async def referral_open(call: CallbackQuery):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{call.from_user.id}"
    async with Session() as s:
        count = await s.scalar(select(func.count()).select_from(Referral).where(Referral.inviter_id == call.from_user.id))
    remaining = max(0, REFERRAL_MILESTONE_COUNT - count)
    milestone_status = "✅ جایزهٔ ویژه دریافت شده" if count >= REFERRAL_MILESTONE_COUNT else f"🎯 تا جایزهٔ ویژه {remaining} دعوت دیگر"
    await branded_callback(call, f"👥 <b>زیرمجموعه‌گیری</b>\nبا لینک اختصاصی خودت دوستت را دعوت کن. برای هر دعوت موفق ۱۰۰ الماس می‌گیری؛ با رسیدن به ۶ دعوت موفق، ۱۰۰۰ الماس جایزهٔ ویژه هم فقط یک‌بار دریافت می‌کنی.\n\n🔗 لینک دعوت:\n<code>{link}</code>\n👥 دعوت‌های ثبت‌شده: {count}\n💎 پاداش‌های عادی: {count * REFERRAL_REWARD} الماس\n🏆 {milestone_status}\n🎁 جایزهٔ ویژه: {REFERRAL_MILESTONE_BONUS} الماس", InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 منوی اصلی", callback_data="panel:home")]
    ]))
    await call.answer()

@router.callback_query(F.data == "support:open")
async def support_open(call: CallbackQuery):
    await branded_callback(call, "🆘 <b>پشتیبانی MMD SELF</b>\nیکی از روش‌ها را انتخاب کن:", InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 پیام خصوصی", url="https://t.me/jokm7")],
        [InlineKeyboardButton(text="📨 ارسال پیام از طریق بات", callback_data="support:send")],
        [InlineKeyboardButton(text="👥 گپ پشتیبانی", url="https://t.me/MMD_SELFF")]
    ]))
    await call.answer()

@router.callback_query(F.data == "support:send")
async def support_send_start(call: CallbackQuery):
    support_waiting.add(call.from_user.id)
    await branded_callback(call, "پیامت را در همین پی‌وی بفرست؛ بات آن را برای پشتیبانی ارسال می‌کند. برای لغو، /cancel بفرست.")
    await call.answer()

@router.message(Command("cancel"))
async def cancel_waiting(message: Message):
    support_waiting.discard(message.from_user.id)
    purchase_waiting_receipt.discard(message.from_user.id)
    await message.reply("درخواست لغو شد.")

@router.callback_query(F.data == "shop:open")
async def shop_open(call: CallbackQuery):
    await branded_callback(call, f"💎 <b>خرید الماس</b>\nهر الماس ≈ ۲۶٫۶۷ تومان\n۱۵۰۰ الماس = ۳۰ روز فعال‌سازی = {PACK_PRICE_TOMAN:,} تومان\n\nشماره کارت: <code>{CARD_NUMBER}</code>\nبه نام: {CARD_HOLDER_DISPLAY}\n\nپس از انتخاب بسته، مبلغ نهایی نمایش داده می‌شود.", InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 ۱۵۰۰ الماس — ۴۰٬۰۰۰ تومان", callback_data="shop:buy:1500")],
        [InlineKeyboardButton(text="📤 ارسال رسید پرداخت", callback_data="shop:receipt")],
        [InlineKeyboardButton(text="⬅️ منوی اصلی", callback_data="panel:home")]
    ]))
    await call.answer()

@router.callback_query(F.data.startswith("shop:buy:"))
async def shop_buy(call: CallbackQuery):
    try: diamonds = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer("بسته نامعتبر است.", show_alert=True); return
    if diamonds != 1500:
        await call.answer("این بسته در دسترس نیست.", show_alert=True); return
    async with Session.begin() as s:
        purchase = Purchase(user_id=call.from_user.id, diamonds=diamonds, amount_toman=PACK_PRICE_TOMAN)
        s.add(purchase)
        await s.flush()
        pid = purchase.id
    await branded_callback(call, f"🧾 <b>سفارش خرید #{pid}</b>\n💎 مقدار: {diamonds} الماس\n💰 مبلغ قابل پرداخت: {PACK_PRICE_TOMAN:,} تومان\n💳 شماره کارت: <code>{CARD_NUMBER}</code>\nبه نام: {CARD_HOLDER_DISPLAY}\n\nپس از واریز، «ارسال رسید پرداخت» را بزن و عکس رسید را بفرست.", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📤 ارسال رسید پرداخت", callback_data=f"shop:receipt:{pid}")]]))
    await call.answer("سفارش ساخته شد.")

@router.callback_query(F.data.startswith("shop:receipt"))
async def shop_receipt_start(call: CallbackQuery):
    purchase_waiting_receipt.add(call.from_user.id)
    await branded_callback(call, "📸 حالا عکس رسید پرداخت را همین‌جا ارسال کن. بعد از بررسی ادمین، الماس‌ها اضافه می‌شوند. برای لغو /cancel را بفرست.")
    await call.answer()

@router.callback_query(F.data.startswith("purchase:approve:"))
async def purchase_approve(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        await call.answer("فقط ادمین مجاز است.", show_alert=True); return
    try: pid = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer("سفارش نامعتبر است.", show_alert=True); return
    async with Session.begin() as s:
        purchase = await s.get(Purchase, pid, with_for_update=True)
        if not purchase or purchase.status != "pending":
            await call.answer("این سفارش قبلاً بررسی شده یا وجود ندارد.", show_alert=True); return
        player = await s.get(Player, purchase.user_id, with_for_update=True)
        if not player:
            await call.answer("کاربر پیدا نشد.", show_alert=True); return
        player.diamonds += purchase.diamonds
        purchase.status = "approved"
        await ledger_add(s, purchase.user_id, purchase.diamonds, "purchase_approved")
        uid, diamonds = purchase.user_id, purchase.diamonds
    try: await bot.send_message(uid, f"✅ پرداخت شما تأیید شد؛ {diamonds} 💎 به حسابت اضافه شد.")
    except Exception: pass
    await call.message.reply(f"تأیید شد: سفارش #{pid}، کاربر {uid}، {diamonds} الماس")
    await call.answer("تأیید شد")

@router.callback_query(F.data.startswith("purchase:reject:"))
async def purchase_reject(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        await call.answer("فقط ادمین مجاز است.", show_alert=True); return
    try: pid = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer("سفارش نامعتبر است.", show_alert=True); return
    async with Session.begin() as s:
        purchase = await s.get(Purchase, pid, with_for_update=True)
        if not purchase or purchase.status != "pending":
            await call.answer("این سفارش قبلاً بررسی شده یا وجود ندارد.", show_alert=True); return
        purchase.status = "rejected"
    await call.message.reply(f"❌ سفارش #{pid} رد شد.")
    await call.answer("رد شد")

@router.message(Command("help"))
async def help_cmd(message: Message):
    if message.chat.type == ChatType.PRIVATE:
        await message.reply("بازی فقط در گروه اختصاصی فعال است. در گروه بازی دستور «بازی 20» را بفرست.")
    elif message.chat.id == GAME_CHAT_ID:
        await message.reply(
            "🎮 <b>راهنما</b>\n"
            "ساخت بازی: <code>بازی 20</code>\n"
            "موجودی: موجودی یا /balance\n"
            "انتقال: روی پیام کاربر ریپلای کن و بنویس انتقال 20\n"
            "مالک می‌تواند از پی‌وی ربات موجودی‌ها را مدیریت کند.\n"
            "هر بازیکن هزینه ورود خودش را می‌پردازد؛ پاداش برنده از سیستم پرداخت می‌شود."
        )
    else:
        await message.reply("⛔ این ربات فقط در گروه اختصاصی بازی فعال است.")

@router.message(Command("balance"))
async def balance(message: Message):
    if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP} and message.chat.id == GAME_CHAT_ID:
        target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
        if not target or target.is_bot:
            await message.reply("برای دیدن موجودی، به پیام یک کاربر واقعی ریپلای کن.")
            return
        p = await ensure_player(target)
        amount = balance_label(target.id, p.diamonds)
        await message.reply(f"💎 موجودی {target.full_name}: <b>{amount}</b>")
        return

    if message.chat.type == ChatType.PRIVATE and message.from_user.id == OWNER_ID:
        args = (message.text or "").split()
        if len(args) == 2 and args[1].isdigit():
            uid = int(args[1])
            await message.reply(f"💎 موجودی <code>{uid}</code>: {balance_label(uid, await balance_of(uid))}")
            return
    await message.reply("موجودی را در گروه اختصاصی بازی با /balance ببین.")

@router.message(F.text.regexp(r"^(?:موجودی|الماس)$"))
async def balance_persian(message: Message):
    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP} or message.chat.id != GAME_CHAT_ID:
        return
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    if not target or target.is_bot:
        await message.reply("برای دیدن موجودی، به پیام یک کاربر واقعی ریپلای کن.")
        return
    p = await ensure_player(target)
    await message.reply(f"💎 موجودی {target.full_name}: <b>{balance_label(target.id, p.diamonds)}</b>")

@router.callback_query(F.data == "account:balance")
async def account_balance_button(call: CallbackQuery):
    p = await ensure_player(call.from_user)
    await call.message.answer(f"💎 موجودی شما: <b>{balance_label(call.from_user.id, p.diamonds)}</b>")
    await call.answer()

@router.callback_query(F.data == "account:info")
async def account_info_button(call: CallbackQuery):
    p = await ensure_player(call.from_user)
    username_status = f"✅ @{call.from_user.username}" if call.from_user.username else "❌ نام کاربری تنظیم نشده"
    await call.message.answer(
        "ℹ️ <b>اطلاعات حساب شما</b>\n"
        f"👤 نام: {call.from_user.full_name}\n"
        f"🆔 آیدی عددی: <code>{call.from_user.id}</code>\n"
        f"🔗 نام کاربری: {username_status}\n"
        f"💎 موجودی الماس: <b>{balance_label(call.from_user.id, p.diamonds)}</b>"
    )
    await call.answer()

@router.callback_query(F.data == "account:status")
async def account_status_button(call: CallbackQuery):
    await call.message.answer(await account_status_text(call.from_user.id))
    await call.answer()

@router.message(Command("admin_help"))
async def admin_help(message: Message):
    if message.chat.type != ChatType.PRIVATE or message.from_user.id != OWNER_ID:
        return
    await message.reply(
        "👑 دستورات مالک:\n"
        "/give USER_ID AMOUNT — افزودن الماس\n"
        "/take USER_ID AMOUNT — کم‌کردن الماس\n"
        "/setdiamonds USER_ID AMOUNT — تنظیم موجودی\n"
        "/balance USER_ID — مشاهده موجودی\n"
        "/ledger USER_ID — تاریخچه تراکنش‌ها\n"
        "/stats — آمار کلی"
    )

async def adjust_balance(message: Message, action: str):
    if message.chat.type != ChatType.PRIVATE or message.from_user.id != OWNER_ID:
        return
    parts = message.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.reply(f"فرمت صحیح: /{action} USER_ID AMOUNT")
        return
    uid, amount = int(parts[1]), int(parts[2])
    if amount < 0 or amount > 2_000_000_000:
        await message.reply("مقدار نامعتبر است.")
        return
    async with Session.begin() as s:
        p = await s.get(Player, uid)
        if not p:
            await message.reply("این کاربر هنوز ربات را استارت نکرده است.")
            return
        if action == "give":
            p.diamonds += amount
            delta, reason = amount, "owner_give"
        elif action == "take":
            if p.diamonds < amount:
                await message.reply("موجودی کاربر کافی نیست.")
                return
            p.diamonds -= amount
            delta, reason = -amount, "owner_take"
        else:
            delta, p.diamonds, reason = amount - p.diamonds, amount, "owner_set"
        await ledger_add(s, uid, delta, reason)
    await message.reply(f"✅ موجودی {uid}: {balance_label(uid, await balance_of(uid))} 💎")

@router.message(Command("give"))
async def give(message: Message): await adjust_balance(message, "give")
@router.message(Command("take"))
async def take(message: Message): await adjust_balance(message, "take")
@router.message(Command("setdiamonds"))
async def setdiamonds(message: Message): await adjust_balance(message, "setdiamonds")

@router.message(Command("ledger"))
async def ledger(message: Message):
    if message.chat.type != ChatType.PRIVATE or message.from_user.id != OWNER_ID:
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.reply("فرمت: /ledger USER_ID")
        return
    async with Session() as s:
        rows = (await s.execute(select(Ledger).where(Ledger.user_id == int(parts[1])).order_by(Ledger.id.desc()).limit(15))).scalars().all()
    if not rows:
        await message.reply("تراکنشی ثبت نشده.")
        return
    await message.reply("🧾 <b>تراکنش‌های اخیر</b>\n" + "\n".join(
        f"#{r.id}: {r.amount:+} 💎 — {r.reason} — {r.created_at:%Y-%m-%d %H:%M}" for r in rows
    ))

@router.message(F.text.regexp(r"^/مدیریت[ ]+کاربران(?:@\\w+)?$"))
async def hidden_user_management(message: Message):
    # Intentionally owner-only and only available in the bot's private chat.
    if message.chat.type != ChatType.PRIVATE or not message.from_user or message.from_user.id != OWNER_ID:
        return
    async with Session() as s:
        users = (await s.execute(select(Player).order_by(Player.created_at.desc()).limit(100))).scalars().all()
        total = await s.scalar(select(func.count()).select_from(Player))
    if not users:
        await message.reply("هنوز کاربری ثبت نشده است.")
        return
    lines = ["👑 <b>مدیریت کاربران</b>", f"تعداد کل: {total}", "برای انتقال: <code>انتقال USER_ID AMOUNT</code>", ""]
    for p in users:
        uname = f"@{p.username}" if p.username else "بدون‌نام‌کاربری"
        amount = balance_label(p.user_id, p.diamonds)
        lines.append(f"• {p.first_name} | {uname} | <code>{p.user_id}</code> | 💎 {amount} | {'فعال' if p.active else 'غیرفعال'}")
    text_out = "\\n".join(lines)
    # Telegram message limit is 4096 chars.
    for i in range(0, len(text_out), 3800):
        await message.reply(text_out[i:i+3800])

@router.message(F.text.regexp(r"^انتقال\\s+\\d+\\s+\\d+$"))
async def owner_transfer_persian(message: Message):
    if message.chat.type != ChatType.PRIVATE or not message.from_user or message.from_user.id != OWNER_ID:
        return
    parts = (message.text or "").split()
    uid, amount = int(parts[1]), int(parts[2])
    if amount <= 0 or amount > 2_000_000_000:
        await message.reply("مقدار انتقال باید بین ۱ و ۲٬۰۰۰٬۰۰۰٬۰۰۰ باشد.")
        return
    async with Session.begin() as s:
        p = await s.get(Player, uid)
        if not p:
            await message.reply("کاربر پیدا نشد؛ باید ابتدا ربات را استارت کرده باشد.")
            return
        p.diamonds += amount
        await ledger_add(s, uid, amount, "owner_transfer")
    await message.reply(f"✅ {amount} 💎 به کاربر <code>{uid}</code> اضافه شد. موجودی: {balance_label(uid, await balance_of(uid))}")

@router.message(F.text.regexp(r"^انتقال\s+\d+$"))
async def player_reply_transfer_start(message: Message):
    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP} or message.chat.id != GAME_CHAT_ID:
        return
    if not message.from_user or not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("برای انتقال، به پیام کاربر موردنظر ریپلای کن و بنویس: انتقال 20")
        return
    sender = message.from_user
    target = message.reply_to_message.from_user
    if sender.is_bot or target.is_bot or sender.id == target.id:
        await message.reply("انتقال به خودت یا ربات مجاز نیست.")
        return
    try:
        amount = int((message.text or "").split()[1])
    except (ValueError, IndexError):
        await message.reply("مقدار انتقال نامعتبر است.")
        return
    if amount < MIN_TRANSFER or amount > 2_000_000_000:
        await message.reply(f"مقدار انتقال باید حداقل {MIN_TRANSFER} الماس باشد.")
        return
    tax_percent = next(rate for threshold, rate in TAX_BRACKETS if amount >= threshold)
    tax = (amount * tax_percent + 99) // 100
    received = amount - tax
    await ensure_player(sender)
    await ensure_player(target)
    if sender.id != OWNER_ID and await balance_of(sender.id) < amount:
        await message.reply(f"💎 موجودی کافی نیست؛ موجودی فعلی شما: {await balance_of(sender.id)}")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👍 بله", callback_data=f"ptrans_yes:{sender.id}:{target.id}:{amount}:{tax}"),
        InlineKeyboardButton(text="👎 نه", callback_data=f"ptrans_no:{sender.id}")
    ]])
    await message.reply(
        f"آیا می‌خواهید {amount} 💎 به {target.full_name} (شناسه <code>{target.id}</code>) انتقال دهید؟\nمالیات: {tax} 💎 ({tax_percent}٪)\nدریافتی گیرنده: {received} 💎",
        reply_markup=kb
    )

@router.callback_query(F.data.startswith("ptrans_no:"))
async def player_reply_transfer_cancel(call: CallbackQuery):
    try: sender_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("درخواست نامعتبر است.", show_alert=True); return
    if call.from_user.id != sender_id:
        await call.answer("این دکمه برای شخص درخواست‌دهنده است.", show_alert=True); return
    await call.message.edit_text("❌ انتقال لغو شد.")
    await call.answer("لغو شد")

@router.callback_query(F.data.startswith("ptrans_yes:"))
async def player_reply_transfer_apply(call: CallbackQuery):
    try:
        _, sender_raw, target_raw, amount_raw, tax_raw = call.data.split(":")
        sender_id, target_id, amount, tax = int(sender_raw), int(target_raw), int(amount_raw), int(tax_raw)
    except (ValueError, IndexError):
        await call.answer("درخواست نامعتبر است.", show_alert=True); return
    if call.from_user.id != sender_id:
        await call.answer("فقط درخواست‌دهنده می‌تواند انتقال را تأیید کند.", show_alert=True); return
    expected_percent = next(rate for threshold, rate in TAX_BRACKETS if amount >= threshold) if amount > 0 else 0
    expected_tax = (amount * expected_percent + 99) // 100 if amount > 0 else 0
    if sender_id == target_id or amount < MIN_TRANSFER or tax != expected_tax or tax < 0 or tax >= amount:
        await call.answer("انتقال نامعتبر است.", show_alert=True); return
    async with Session.begin() as s:
        sender = await s.get(Player, sender_id, with_for_update=True)
        target = await s.get(Player, target_id, with_for_update=True)
        if not sender or not target:
            await call.answer("حساب یکی از کاربران پیدا نشد.", show_alert=True); return
        if sender_id != OWNER_ID and sender.diamonds < amount:
            await call.answer("موجودی کافی نیست؛ انتقال انجام نشد.", show_alert=True); return
        if sender_id != OWNER_ID:
            sender.diamonds -= amount
            await ledger_add(s, sender_id, -amount, "user_transfer_out")
        received = amount - tax
        target.diamonds += received
        await ledger_add(s, target_id, received, "user_transfer_in")
        if tax:
            await ledger_add(s, sender_id, 0, f"transfer_tax:{tax}")
        sender_balance = balance_label(sender_id, sender.diamonds)
        target_balance = balance_label(target_id, target.diamonds)
    await call.message.edit_text(
        "✅ انتقال انجام شد\n"
        f"👤 فرستنده: <code>{sender_id}</code> | موجودی: {sender_balance} 💎\n"
        f"👤 گیرنده: <code>{target_id}</code> | موجودی: {target_balance} 💎\n🧾 مالیات: {tax} 💎 | دریافتی: {amount-tax} 💎"
    )
    await call.answer("انتقال انجام شد")

@router.message(Command("stats"))
async def stats(message: Message):
    if message.chat.type != ChatType.PRIVATE or message.from_user.id != OWNER_ID:
        return
    async with Session() as s:
        users = await s.scalar(select(func.count()).select_from(Player))
        diamonds = await s.scalar(select(func.coalesce(func.sum(Player.diamonds), 0)))
        games = await s.scalar(select(func.count()).select_from(Game))
        active = await s.scalar(select(func.count()).select_from(Game).where(Game.status == "open"))
    await message.reply(f"📊 کاربران: {users}\n💎 مجموع الماس: {diamonds}\n🎮 کل بازی‌ها: {games}\n⏳ بازی‌های باز: {active}")

@router.message(F.text.regexp(r"^بازی\s+\d+$"))
async def create_game(message: Message):
    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP} or message.chat.id != GAME_CHAT_ID:
        await message.reply("⛔ بازی فقط در گروه اختصاصی بازی فعال است.")
        return
    stake = int(message.text.split()[1])
    if not MIN_STAKE <= stake <= MAX_STAKE:
        await message.reply(f"مبلغ بازی باید حداقل {MIN_STAKE} الماس باشد. سقف نرم‌افزاری فعلی: {MAX_STAKE:,} الماس.")
        return
    await ensure_player(message.from_user)
    if await balance_of(message.from_user.id) < stake:
        await message.reply(f"💎 موجودی کافی نیست. موجودی فعلی: {await balance_of(message.from_user.id)}")
        return
    prompt_id = secrets.token_urlsafe(5).replace("-", "").replace("_", "")[:8]
    # Bind prompt to creator/stake in an in-memory map. A restart simply invalidates old prompts.
    pending_prompts[prompt_id] = (message.from_user.id, stake, time_now())
    await message.reply(
        f"🎮 <b>ساخت بازی</b>\n👤 سازنده: {message.from_user.full_name}\n💎 هزینه ورود: {stake}\n\nنوع بازی را انتخاب کن:",
        reply_markup=mode_kb(prompt_id)
    )

pending_prompts: dict[str, tuple[int, int, float]] = {}

def time_now():
    import time
    return time.monotonic()

@router.callback_query(F.data.startswith("mode:"))
async def choose_mode(call: CallbackQuery):
    if call.message.chat.id != GAME_CHAT_ID:
        await call.answer("فقط در گروه اختصاصی بازی.", show_alert=True)
        return
    _, seats_raw, prompt_id = call.data.split(":")
    prompt = pending_prompts.get(prompt_id)
    if not prompt or prompt[0] != call.from_user.id:
        await call.answer("این منو متعلق به شما نیست یا منقضی شده.", show_alert=True)
        return
    if time_now() - prompt[2] > 300:
        pending_prompts.pop(prompt_id, None)
        await call.answer("این منو منقضی شده؛ دوباره بازی را بساز.", show_alert=True)
        return
    await call.message.edit_reply_markup(reply_markup=opponent_kb(prompt_id) if seats_raw == "2" else InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 بازی سه‌نفره با کاربران", callback_data=f"kind:users3:{prompt_id}")]
    ]))
    await call.answer()

async def create_game_record(call: CallbackQuery, prompt_id: str, mode: str, seats: int, stake: int, creator_id: int):
    game_id = secrets.token_hex(4)
    ids = [creator_id]
    async with Session.begin() as s:
        p = await s.get(Player, creator_id, with_for_update=True)
        if not p or not p.active:
            raise ValueError("حساب بازی فعال نیست.")
        if creator_id != OWNER_ID:
            if p.diamonds < stake:
                raise ValueError("موجودی برای شروع بازی کافی نیست.")
            p.diamonds -= stake
            await ledger_add(s, creator_id, -stake, "game_entry", game_id)
        g = Game(
            id=game_id, chat_id=call.message.chat.id, creator_id=creator_id,
            stake=stake, seats=seats, status="open", mode=mode,
            players_json=json.dumps(ids), message_id=None
        )
        s.add(g)
    # Send a single branded game card, then keep updating its caption as the game progresses.
    mode_label = '🤖 مقابل ربات' if mode == 'bot' else ('👥 دونفره' if seats == 2 else '👥 سه‌نفره')
    caption = (
        f"👑 <b>VIP-SELF</b> · <code>#{game_id}</code>\n"
        f"━━━━━━━━━━━━━━\n"
        f"🎮 حالت: {mode_label}\n"
        f"👤 سازنده: {html.escape(call.from_user.full_name)}\n"
        f"💎 مبلغ ورود: <b>{stake:,}</b> الماس\n"
        f"🏆 جایزهٔ سیستمی احتمالی: <b>{stake * 2:,}</b> الماس\n"
        f"👥 بازیکنان: <b>1/{seats}</b>\n"
        f"⏳ منتظر شروع بازی..."
    )
    try:
        photo = FSInputFile(os.path.join(os.path.dirname(__file__), "mmd_self_menu.png"))
        sent = await bot.send_photo(
            chat_id=call.message.chat.id, photo=photo, caption=caption,
            reply_markup=game_kb(game_id, seats, 1) if mode == 'users' else None
        )
    except Exception:
        log.exception("Could not send branded game card; using text fallback")
        sent = await bot.send_message(
            chat_id=call.message.chat.id, text=caption,
            reply_markup=game_kb(game_id, seats, 1) if mode == 'users' else None
        )
    async with Session.begin() as s:
        g = await s.get(Game, game_id)
        g.message_id = sent.message_id
    if mode == "bot":
        async with Session.begin() as s:
            g = await s.get(Game, game_id)
            p = await s.get(Player, creator_id, with_for_update=True)
            try:
                winner_index, source = await random_index(2)
            except Exception:
                p.diamonds += stake
                await ledger_add(s, creator_id, stake, "game_refund_random_error", game_id)
                g.status = "cancelled"
                result = "⚠️ منبع تصادفی در دسترس نبود؛ الماس ورود برگشت داده شد."
                g.result_text = result
            else:
                if winner_index == 0:
                    prize = stake * 2
                    p.diamonds += prize
                    await ledger_add(s, creator_id, prize, "game_prize", game_id)
                    result = (f"🏆 برنده: {p.first_name}\n🎁 پاداش سیستمی: {prize:,} 💎\n"
                              f"💎 موجودی فعلی: {balance_label(creator_id, p.diamonds)}\n🤖 حریف: ربات\n🎲 {source}")
                else:
                    result = (f"🏆 برنده: ربات 🤖\n👤 بازیکن: {p.first_name}\n"
                              f"💎 موجودی فعلی: {balance_label(creator_id, p.diamonds)}\n🎲 {source}")
                g.status = "finished"
                g.result_text = result
        await render_game(game_id, result)
        await call.answer("بازی تمام شد.")
        pending_prompts.pop(prompt_id, None)
        return
    pending_prompts.pop(prompt_id, None)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await render_game(game_id)
    await call.answer("کارت بازی ساخته شد.")

@router.callback_query(F.data.startswith("kind:"))
async def choose_kind(call: CallbackQuery):
    if call.message.chat.id != GAME_CHAT_ID:
        await call.answer("فقط در گروه اختصاصی بازی.", show_alert=True)
        return
    _, kind, prompt_id = call.data.split(":")
    prompt = pending_prompts.get(prompt_id)
    if not prompt or prompt[0] != call.from_user.id:
        await call.answer("این منو متعلق به شما نیست یا منقضی شده.", show_alert=True)
        return
    creator_id, stake, created = prompt
    if time_now() - created > 300:
        pending_prompts.pop(prompt_id, None)
        await call.answer("منو منقضی شده؛ دوباره بازی را بساز.", show_alert=True)
        return
    seats = 3 if kind == "users3" else 2
    mode = "bot" if kind == "bot" else "users"
    try:
        await create_game_record(call, prompt_id, mode, seats, stake, creator_id)
    except ValueError as e:
        await call.answer(str(e), show_alert=True)

@router.callback_query(F.data.startswith("join:"))
async def join_game(call: CallbackQuery):
    if call.message.chat.id != GAME_CHAT_ID:
        await call.answer("فقط در گروه اختصاصی بازی.", show_alert=True)
        return
    game_id = call.data.split(":", 1)[1]
    uid = call.from_user.id
    await ensure_player(call.from_user)
    should_finish = False
    error = None
    async with Session.begin() as s:
        g = await s.get(Game, game_id, with_for_update=True)
        if not g or g.status != "open" or g.mode != "users":
            error = "این بازی دیگر باز نیست."
        else:
            ids = [int(x) for x in json.loads(g.players_json or "[]")]
            if uid in ids:
                error = "شما قبلاً در این بازی هستید."
            elif uid == g.creator_id:
                error = "سازنده نمی‌تواند به بازی خودش بپیوندد."
            elif len(ids) >= g.seats:
                error = "ظرفیت بازی تکمیل شده."
            else:
                p = await s.get(Player, uid, with_for_update=True)
                if not p or not p.active:
                    error = "حساب بازی شما فعال نیست."
                elif uid != OWNER_ID and p.diamonds < g.stake:
                    error = f"موجودی کافی نیست؛ موجودی شما {p.diamonds} 💎 است."
                else:
                    if uid != OWNER_ID:
                        p.diamonds -= g.stake
                        await ledger_add(s, uid, -g.stake, "game_entry", game_id)
                    ids.append(uid)
                    g.players_json = json.dumps(ids)
                    should_finish = len(ids) == g.seats
    if error:
        await call.answer(error, show_alert=True)
        return
    await call.answer("به بازی پیوستی!")
    await render_game(game_id)
    if should_finish:
        await finish_game(game_id)

@router.callback_query(F.data.startswith("cancel:"))
async def cancel_game(call: CallbackQuery):
    if call.message.chat.id != GAME_CHAT_ID:
        await call.answer("فقط در گروه اختصاصی بازی.", show_alert=True)
        return
    game_id = call.data.split(":", 1)[1]
    async with Session() as s:
        g = await s.get(Game, game_id)
        allowed = g and g.creator_id == call.from_user.id
    if not allowed:
        await call.answer("فقط سازنده می‌تواند بازی را لغو کند.", show_alert=True)
        return
    if await refund_game(game_id, "سازنده بازی را لغو کرد؛ هزینه ورود برگشت داده شد."):
        await call.answer("بازی لغو شد و موجودی‌ها برگشتند.")
        await render_game(game_id, "❌ سازنده بازی را لغو کرد؛ الماس‌ها برگشت داده شدند.")
    else:
        await call.answer("این بازی دیگر قابل لغو نیست.", show_alert=True)

@router.message(F.photo)
async def receive_photo(message: Message):
    uid = message.from_user.id
    if uid in purchase_waiting_receipt and message.chat.type == ChatType.PRIVATE:
        photo_id = message.photo[-1].file_id
        async with Session.begin() as s:
            purchase = (await s.execute(select(Purchase).where(Purchase.user_id == uid, Purchase.status == "pending").order_by(Purchase.id.desc()).limit(1))).scalars().first()
            if not purchase:
                purchase_waiting_receipt.discard(uid)
                await message.reply("سفارش در انتظار پرداخت پیدا نشد. ابتدا از بخش خرید الماس سفارش بساز.")
                return
            purchase.receipt_file_id = photo_id
            pid, diamonds, amount = purchase.id, purchase.diamonds, purchase.amount_toman
        purchase_waiting_receipt.discard(uid)
        try:
            await bot.send_photo(OWNER_ID, photo_id, caption=f"🧾 رسید خرید الماس\nسفارش: #{pid}\nکاربر: {uid}\nالماس: {diamonds}\nمبلغ: {amount:,} تومان", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ تأیید و واریز", callback_data=f"purchase:approve:{pid}"), InlineKeyboardButton(text="❌ رد رسید", callback_data=f"purchase:reject:{pid}")]]))
            await message.reply("✅ رسید برای بررسی ادمین ارسال شد. پس از تأیید، الماس‌ها واریز می‌شوند.")
        except Exception:
            await message.reply("ارسال رسید به ادمین ناموفق بود؛ شناسه ادمین و امکان دریافت پیام خصوصی را بررسی کنید.")
        return
    await message.reply("برای ارسال رسید، ابتدا از منوی خرید الماس گزینه «ارسال رسید پرداخت» را بزن.")


# ---- Working utility commands (implemented, not placeholders) ----
import ast
import operator
import html

_SAFE_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_SAFE_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}

def safe_calculate(expression: str):
    if len(expression) > 120:
        raise ValueError('عبارت خیلی طولانی است.')
    expression = expression.replace('×', '*').replace('÷', '/').replace('−', '-')
    tree = ast.parse(expression, mode='eval')
    def walk(node):
        if isinstance(node, ast.Expression): return walk(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_BINOPS:
            a, b = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Pow) and abs(b) > 8: raise ValueError('توان بیش از حد بزرگ است.')
            value = _SAFE_BINOPS[type(node.op)](a, b)
            if abs(value) > 10**100: raise ValueError('نتیجه بیش از حد بزرگ است.')
            return value
        if isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_UNARY:
            return _SAFE_UNARY[type(node.op)](walk(node.operand))
        raise ValueError('فقط محاسبات عددی ساده مجاز است.')
    result = walk(tree)
    if isinstance(result, float) and result.is_integer(): result = int(result)
    return result

@router.message(Command('math', 'calc'))
async def calculator_command(message: Message):
    expr = (message.text or '').partition(' ')[2].strip()
    if not expr and message.reply_to_message and message.reply_to_message.text:
        expr = message.reply_to_message.text.strip()
    if not expr:
        await message.reply('🧮 مثال: /math (12+8)*3')
        return
    try:
        result = safe_calculate(expr)
        await message.reply(f'🧮 <code>{html.escape(expr)}</code> = <b>{result}</b>')
    except Exception as e:
        await message.reply(f'❌ عبارت قابل محاسبه نیست: {html.escape(str(e))}')


@router.message(Command("list"))
async def deferred_list_command(message: Message):
    """Show the intentionally deferred features list stored with the project."""
    body = (
        "📋 <b>فهرست قابلیت‌های ناتمام MMD SELF</b>\n"
        "این قابلیت‌ها از منوی فعال حذف شده‌اند تا گزینهٔ نمایشی به‌جای قابلیت واقعی نباشد:\n"
        "• اتصال نشست شخصی به بات/WebApp\n"
        "• ساعت زندهٔ نام/بیو و فونت‌ها\n"
        "• سین/حضور، ریکشن، پاسخ خودکار و منشی\n"
        "• بلاک/آنبلاک از نشست شخصی و بکاپ چت\n"
        "• آب‌وهوا و پردازش رسانه\n"
        "• ابزارهای پیشرفتهٔ مدیریت گروه و حذف پیام\n"
        "• پخش موسیقی/لایو و مدیریت صف\n"
        "• فیلترها، محتوا و فهرست‌های ذخیره‌شده\n"
        "• تصویرسازی چت/اسکرین\n"
        "• API واقعی WebApp و اعتبارسنجی initData\n\n"
        "فهرست کامل و توضیح هر مورد در فایل <code>DEFERRED_FEATURES.md</code> داخل ZIP است.\n"
        "کد ورود، رمز دومرحله‌ای، فوروارد کد و اسپم انبوه عمداً پشتیبانی نمی‌شوند."
    )
    await message.reply(body)

@router.message(Command('translate', 'tr'))
async def translate_command(message: Message):
    args = (message.text or '').partition(' ')[2].strip()
    source_text = args
    lang = 'fa'
    # Supports /translate hello, /translate en hello, or replying with /translate
    if message.reply_to_message and not args:
        source_text = message.reply_to_message.text or message.reply_to_message.caption or ''
    elif args:
        parts = args.split(maxsplit=1)
        if len(parts) == 2 and len(parts[0]) in (2, 5) and parts[0].replace('-', '').isalpha():
            lang, source_text = parts[0], parts[1]
    if not source_text:
        await message.reply('🌐 استفاده: /translate hello یا روی پیام ریپلای کن و /translate بفرست. برای زبان مقصد: /translate en سلام')
        return
    try:
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get('https://api.mymemory.translated.net/get', params={'q': source_text[:4500], 'langpair': f'auto|{lang}'}) as resp:
                if resp.status != 200: raise RuntimeError('سرویس ترجمه پاسخ نداد.')
                data = await resp.json()
        translated = (data.get('responseData') or {}).get('translatedText')
        if not translated: raise RuntimeError('ترجمه‌ای دریافت نشد.')
        await message.reply(f'🌐 <b>ترجمه ({html.escape(lang)})</b>\n{html.escape(html.unescape(translated))}')
    except Exception:
        log.exception('translation failed')
        await message.reply('⚠️ ترجمه فعلاً در دسترس نیست؛ کمی بعد دوباره امتحان کن.')

@router.message(Command('userinfo', 'id'))
async def user_info_command(message: Message):
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    if not target:
        await message.reply('اطلاعات کاربر در دسترس نیست.')
        return
    username = f'@{target.username}' if target.username else 'ندارد'
    await message.reply(
        '👤 <b>اطلاعات کاربر</b>\n'
        f'نام: {html.escape(target.full_name)}\n'
        f'آیدی: <code>{target.id}</code>\n'
        f'نام کاربری: {html.escape(username)}\n'
        f'بات: {"بله" if target.is_bot else "خیر"}'
    )

@router.message(Command('clock', 'time'))
async def clock_command(message: Message):
    now = datetime.now().astimezone()
    await message.reply(f'🕒 ساعت دستگاه میزبان: <b>{now.strftime("%Y-%m-%d %H:%M:%S %Z")}</b>')

@router.message(Command('format'))
async def format_command(message: Message):
    raw = (message.text or '').partition(' ')[2].strip()
    if not raw and message.reply_to_message:
        raw = message.reply_to_message.text or ''
    if not raw:
        await message.reply('🔤 استفاده: /format bold متن یا /format italic متن یا /format code متن یا /format spoiler متن')
        return
    mode, _, body = raw.partition(' ')
    if not body and message.reply_to_message:
        body = raw
        mode = 'bold'
    safe = html.escape(body)
    if mode.lower() in ('bold', 'پررنگ'): out = f'<b>{safe}</b>'
    elif mode.lower() in ('italic', 'کج'): out = f'<i>{safe}</i>'
    elif mode.lower() in ('code', 'کد'): out = f'<code>{safe}</code>'
    elif mode.lower() in ('spoiler', 'اسپویلر'): out = f'<tg-spoiler>{safe}</tg-spoiler>'
    elif mode.lower() in ('underline', 'زیرخط'): out = f'<u>{safe}</u>'
    elif mode.lower() in ('strike', 'خط‌خورده'): out = f'<s>{safe}</s>'
    else:
        await message.reply('حالت‌ها: bold, italic, code, spoiler, underline, strike')
        return
    await message.answer(out)

@router.message(F.text.regexp(r'^(?:ترجمه|translate)\s+.+$'))
async def translate_plain_text(message: Message):
    raw = (message.text or '').strip().split(maxsplit=1)[1]
    try:
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get('https://api.mymemory.translated.net/get', params={'q': raw[:4500], 'langpair': 'auto|fa'}) as resp:
                data = await resp.json() if resp.status == 200 else {}
        translated = (data.get('responseData') or {}).get('translatedText')
        if translated:
            await message.reply(f'🌐 <b>ترجمه فارسی</b>\n{html.escape(html.unescape(translated))}')
        else:
            await message.reply('⚠️ ترجمه فعلاً در دسترس نیست.')
    except Exception:
        log.exception('plain-text translation failed')
        await message.reply('⚠️ ترجمه فعلاً در دسترس نیست؛ کمی بعد دوباره امتحان کن.')

@router.message(F.text.regexp(r'^(?:ترجمه|translate)\s*$'))
async def translate_reply_shortcut(message: Message):
    if message.reply_to_message and (message.reply_to_message.text or message.reply_to_message.caption):
        # Reuse the regular translation command through the same public API logic.
        text_to_translate = message.reply_to_message.text or message.reply_to_message.caption or ''
        try:
            timeout = aiohttp.ClientTimeout(total=12)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get('https://api.mymemory.translated.net/get', params={'q': text_to_translate[:4500], 'langpair': 'auto|fa'}) as resp:
                    data = await resp.json() if resp.status == 200 else {}
            translated = (data.get('responseData') or {}).get('translatedText')
            if translated: await message.reply(f'🌐 <b>ترجمه فارسی</b>\n{html.escape(html.unescape(translated))}')
            else: await message.reply('⚠️ ترجمه فعلاً در دسترس نیست.')
        except Exception:
            await message.reply('⚠️ ترجمه فعلاً در دسترس نیست.')
    else:
        await message.reply('روی پیام موردنظر ریپلای کن و «ترجمه» بفرست؛ یا از /translate متن استفاده کن.')


@router.message(F.text, F.from_user.id.in_(support_waiting))
async def support_text_router(message: Message):
    uid = message.from_user.id if message.from_user else 0
    if message.chat.type == ChatType.PRIVATE and uid in support_waiting:
        if (message.text or "").strip().lower() in {"/cancel", "لغو"}:
            support_waiting.discard(uid)
            await message.reply("درخواست پشتیبانی لغو شد.")
            return
        support_waiting.discard(uid)
        try:
            await bot.send_message(SUPPORT_CHAT, f"📨 پیام پشتیبانی از {message.from_user.full_name}\nآیدی: <code>{uid}</code>\nنام کاربری: @{message.from_user.username or 'ندارد'}\n\n{message.text}")
            await message.reply("✅ پیام شما برای گپ پشتیبانی ارسال شد.")
        except Exception:
            await message.reply("ارسال به گپ پشتیبانی ناموفق بود؛ مطمئن شوید بات عضو گپ @MMD_SELFF است و اجازه ارسال پیام دارد.")
        return

@router.message()
async def outside_group_guard(message: Message):
    if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP} and message.chat.id != GAME_CHAT_ID:
        if message.text and message.text.startswith("بازی "):
            await message.reply("⛔ بازی فقط در گروه اختصاصی بازی فعال است.")


# Minimal, authenticated Telegram WebApp API. It intentionally exposes no personal-account
# session operations; add features only after separate review and authorization checks.
def _validate_telegram_init_data(init_data: str) -> dict | None:
    if not TOKEN or not init_data or len(init_data) > 8192:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
        supplied_hash = parsed.pop("hash", "")
        if not supplied_hash:
            return None
        check_string = "\n".join(f"{key}={value}" for key, value in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        expected_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_hash, supplied_hash):
            return None
        auth_date = int(parsed.get("auth_date", "0"))
        if auth_date <= 0 or abs(int(time.time()) - auth_date) > 86400:
            return None
        user = json.loads(parsed.get("user", "{}"))
        if not isinstance(user, dict) or not isinstance(user.get("id"), int):
            return None
        return {"id": user["id"], "first_name": user.get("first_name", ""), "username": user.get("username")}
    except (ValueError, TypeError, json.JSONDecodeError):
        return None

@web.middleware
async def _api_cors(request, handler):
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        response = await handler(request)
    origin = request.headers.get("Origin", "*")
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

async def _api_options(request):
    return web.Response(status=204)

async def _api_health(request):
    return web.json_response({"ok": True, "service": "mmd-self-api", "auth_required": True})

async def _api_auth(request):
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
    user = _validate_telegram_init_data(str(payload.get("initData", "")))
    if not user:
        return web.json_response({"ok": False, "error": "invalid_or_expired_init_data"}, status=401)
    return web.json_response({"ok": True, "user": user})

async def _start_api_server():
    app = web.Application(middlewares=[_api_cors])
    app.router.add_route("OPTIONS", "/{tail:.*}", _api_options)
    app.router.add_get("/api/health", _api_health)
    app.router.add_post("/api/auth", _api_auth)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("MMD SELF API listening on 0.0.0.0:%s", port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    global bot
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="start", description="منوی اصلی MMD SELF"),
        BotCommand(command="panel", description="باز کردن پنل"),
        BotCommand(command="help", description="راهنما"),
        BotCommand(command="balance", description="موجودی الماس"),
        BotCommand(command="translate", description="ترجمه به فارسی یا زبان دلخواه"),
        BotCommand(command="math", description="ماشین حساب امن"),
        BotCommand(command="userinfo", description="اطلاعات کاربر"),
        BotCommand(command="format", description="قالب‌بندی متن تلگرام"),
        BotCommand(command="clock", description="نمایش ساعت"),
        BotCommand(command="list", description="فهرست قابلیت‌های ناتمام"),
    ])
    asyncio.create_task(expire_games_loop())
    asyncio.create_task(_start_api_server())
    me = await bot.get_me()
    log.info("Started as @%s; dedicated game group id=%s", me.username, GAME_CHAT_ID)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
