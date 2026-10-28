import asyncio
import json
import logging
import os
import secrets
from datetime import datetime, timezone

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, BufferedInputFile
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
MAX_STAKE = max(1, int(os.getenv("MAX_STAKE", "1000000")))

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

def game_kb(game_id: str, seats: int, joined: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🙋 پیوستن ({joined}/{seats})", callback_data=f"join:{game_id}")],
        [InlineKeyboardButton(text="❌ لغو بازی", callback_data=f"cancel:{game_id}")]
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

async def balance_of(uid: int) -> int:
    async with Session() as s:
        p = await s.get(Player, uid)
        return p.diamonds if p else 0

async def ledger_add(s, uid: int, amount: int, reason: str, game_id: str | None = None):
    s.add(Ledger(user_id=uid, amount=amount, reason=reason, game_id=game_id))

async def owner_only(message: Message) -> bool:
    if message.from_user and message.from_user.id == OWNER_ID:
        return True
    await message.answer("⛔ این دستور فقط برای مالک است.")
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
    return secrets.randbelow(max_value), "مولد تصادفی امن سیستم (RANDOM.ORG تنظیم نشده)"

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
        body = (
            f"💎 <b>بازی #{g.id}</b>\n"
            f"🎮 حالت: {'با ربات' if g.mode == 'bot' else ('دونفره' if g.seats == 2 else 'سه‌نفره')}\n"
            f"💎 هزینه ورود هر نفر: {g.stake}\n"
            f"👥 بازیکنان: {len(ids)}/{g.seats}\n"
            f"👤 " + ("، ".join(players) if players else "—") + "\n"
            f"📌 وضعیت: {status}"
        )
        if extra:
            body += "\n\n" + extra
        if g.status == "open" and g.mode == "users":
            kb = game_kb(g.id, g.seats, len(ids))
        else:
            kb = None
        try:
            await bot.edit_message_text(body, chat_id=g.chat_id, message_id=g.message_id, reply_markup=kb)
        except TelegramBadRequest:
            pass

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
        g.result_text = f"🏆 برنده: {winner_name}\n🎁 پاداش سیستمی: {prize} 💎\n🎲 منبع: {source}"
        chat_id = g.chat_id
        msg_id = g.message_id
        result_text = g.result_text
    await render_game(game_id, result_text)
    try:
        await bot.send_message(chat_id, f"🎉 <b>نتیجه بازی #{game_id}</b>\n{result_text}")
    except Exception:
        pass

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
    await message.answer(
        "سلام! حساب بازی شما ثبت شد. 💎\n"
        "بازی فقط در گروه اختصاصی انجام می‌شود؛ اینجا منوی بازی وجود ندارد.\n"
        "برای شرکت، وارد گروه بازی شوید و دستور «بازی 20» را بفرستید."
    )

@router.message(Command("help"))
async def help_cmd(message: Message):
    if message.chat.type == ChatType.PRIVATE:
        await message.answer("بازی فقط در گروه اختصاصی فعال است. در گروه بازی دستور «بازی 20» را بفرست.")
    elif message.chat.id == GAME_CHAT_ID:
        await message.answer(
            "🎮 <b>راهنما</b>\n"
            "ساخت بازی: <code>بازی 20</code>\n"
            "موجودی: /balance\n"
            "مالک می‌تواند از پی‌وی ربات موجودی‌ها را مدیریت کند.\n"
            "هر بازیکن هزینه ورود خودش را می‌پردازد؛ پاداش برنده از سیستم پرداخت می‌شود."
        )
    else:
        await message.answer("⛔ این ربات فقط در گروه اختصاصی بازی فعال است.")

@router.message(Command("balance"))
async def balance(message: Message):
    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP} or message.chat.id != GAME_CHAT_ID:
        if message.chat.type == ChatType.PRIVATE and message.from_user.id == OWNER_ID:
            args = message.text.split()
            if len(args) == 2 and args[1].isdigit():
                await message.answer(f"💎 موجودی <code>{args[1]}</code>: {await balance_of(int(args[1]))}")
                return
        await message.answer("موجودی بازیکن را در گروه اختصاصی بازی با /balance ببین.")
        return
    p = await ensure_player(message.from_user)
    await message.answer(f"💎 موجودی {message.from_user.full_name}: <b>{p.diamonds}</b>")

@router.message(Command("admin_help"))
async def admin_help(message: Message):
    if message.chat.type != ChatType.PRIVATE or message.from_user.id != OWNER_ID:
        return
    await message.answer(
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
        await message.answer(f"فرمت صحیح: /{action} USER_ID AMOUNT")
        return
    uid, amount = int(parts[1]), int(parts[2])
    if amount < 0 or amount > 2_000_000_000:
        await message.answer("مقدار نامعتبر است.")
        return
    async with Session.begin() as s:
        p = await s.get(Player, uid)
        if not p:
            await message.answer("این کاربر هنوز ربات را استارت نکرده است.")
            return
        if action == "give":
            p.diamonds += amount
            delta, reason = amount, "owner_give"
        elif action == "take":
            if p.diamonds < amount:
                await message.answer("موجودی کاربر کافی نیست.")
                return
            p.diamonds -= amount
            delta, reason = -amount, "owner_take"
        else:
            delta, p.diamonds, reason = amount - p.diamonds, amount, "owner_set"
        await ledger_add(s, uid, delta, reason)
    await message.answer(f"✅ موجودی {uid}: {await balance_of(uid)} 💎")

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
        await message.answer("فرمت: /ledger USER_ID")
        return
    async with Session() as s:
        rows = (await s.execute(select(Ledger).where(Ledger.user_id == int(parts[1])).order_by(Ledger.id.desc()).limit(15))).scalars().all()
    if not rows:
        await message.answer("تراکنشی ثبت نشده.")
        return
    await message.answer("🧾 <b>تراکنش‌های اخیر</b>\n" + "\n".join(
        f"#{r.id}: {r.amount:+} 💎 — {r.reason} — {r.created_at:%Y-%m-%d %H:%M}" for r in rows
    ))

@router.message(Command("stats"))
async def stats(message: Message):
    if message.chat.type != ChatType.PRIVATE or message.from_user.id != OWNER_ID:
        return
    async with Session() as s:
        users = await s.scalar(select(func.count()).select_from(Player))
        diamonds = await s.scalar(select(func.coalesce(func.sum(Player.diamonds), 0)))
        games = await s.scalar(select(func.count()).select_from(Game))
        active = await s.scalar(select(func.count()).select_from(Game).where(Game.status == "open"))
    await message.answer(f"📊 کاربران: {users}\n💎 مجموع الماس: {diamonds}\n🎮 کل بازی‌ها: {games}\n⏳ بازی‌های باز: {active}")

@router.message(F.text.regexp(r"^بازی\s+\d+$"))
async def create_game(message: Message):
    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP} or message.chat.id != GAME_CHAT_ID:
        await message.answer("⛔ بازی فقط در گروه اختصاصی بازی فعال است.")
        return
    stake = int(message.text.split()[1])
    if not 1 <= stake <= MAX_STAKE:
        await message.answer(f"مبلغ باید بین ۱ تا {MAX_STAKE:,} الماس باشد.")
        return
    await ensure_player(message.from_user)
    if await balance_of(message.from_user.id) < stake:
        await message.answer(f"💎 موجودی کافی نیست. موجودی فعلی: {await balance_of(message.from_user.id)}")
        return
    prompt_id = secrets.token_urlsafe(5).replace("-", "").replace("_", "")[:8]
    # Bind prompt to creator/stake in an in-memory map. A restart simply invalidates old prompts.
    pending_prompts[prompt_id] = (message.from_user.id, stake, time_now())
    await message.answer(
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
        # Lock the player row where supported; check and deduct in the same transaction.
        p = await s.get(Player, creator_id, with_for_update=True)
        if not p or not p.active or p.diamonds < stake:
            raise ValueError("موجودی برای شروع بازی کافی نیست.")
        p.diamonds -= stake
        await ledger_add(s, creator_id, -stake, "game_entry", game_id)
        g = Game(
            id=game_id, chat_id=call.message.chat.id, creator_id=creator_id,
            stake=stake, seats=seats, status="open", mode=mode,
            players_json=json.dumps(ids), message_id=None
        )
        s.add(g)
    if mode == "bot":
        # The bot is the opponent; settle immediately using a fair two-outcome draw.
        async with Session.begin() as s:
            g = await s.get(Game, game_id)
            p = await s.get(Player, creator_id, with_for_update=True)
            try:
                winner_index, source = await random_index(2)
            except Exception:
                # Random service error: refund entry fee and cancel.
                p.diamonds += stake
                await ledger_add(s, creator_id, stake, "game_refund_random_error", game_id)
                g.status = "cancelled"
                g.result_text = "منبع تصادفی در دسترس نبود؛ الماس برگشت داده شد."
                result = g.result_text
            else:
                if winner_index == 0:
                    prize = stake * 2
                    p.diamonds += prize
                    await ledger_add(s, creator_id, prize, "game_prize", game_id)
                    result = f"🏆 {p.first_name} برنده شد!\n🎁 پاداش سیستمی: {prize} 💎\n🎲 {source}"
                else:
                    result = f"🤖 ربات این دور را برد؛ هزینه ورود پرداخت شد.\n🎲 {source}"
                g.status = "finished"
                g.result_text = result
        await call.message.edit_text(f"🎮 <b>بازی #{game_id}</b>\n{result}")
        await call.answer("بازی تمام شد.")
        return
    # User-vs-user game: keep entry fees deducted, refund on cancellation/timeout.
    async with Session.begin() as s:
        g = await s.get(Game, game_id)
        g.message_id = call.message.message_id
    await call.message.edit_text(
        f"💎 <b>بازی #{game_id}</b>\n👤 سازنده: {call.from_user.full_name}\n"
        f"🎮 حالت: {'دونفره' if seats == 2 else 'سه‌نفره'}\n💎 هزینه ورود هر نفر: {stake}\n"
        f"👥 بازیکنان: 1/{seats}\n⏳ منتظر بازیکنان...",
        reply_markup=game_kb(game_id, seats, 1)
    )
    pending_prompts.pop(prompt_id, None)
    await call.answer("بازی ساخته شد.")

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
                elif p.diamonds < g.stake:
                    error = f"موجودی کافی نیست؛ موجودی شما {p.diamonds} 💎 است."
                else:
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

@router.message()
async def outside_group_guard(message: Message):
    if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP} and message.chat.id != GAME_CHAT_ID:
        if message.text and message.text.startswith("بازی "):
            await message.reply("⛔ بازی فقط در گروه اختصاصی بازی فعال است.")

async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    global bot
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="start", description="ثبت حساب بازی"),
        BotCommand(command="help", description="راهنما"),
        BotCommand(command="balance", description="موجودی الماس"),
    ])
    asyncio.create_task(expire_games_loop())
    me = await bot.get_me()
    log.info("Started as @%s; dedicated game group id=%s", me.username, GAME_CHAT_ID)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
