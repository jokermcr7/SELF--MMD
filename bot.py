import asyncio, logging, os, re, time, json, unicodedata, html
from pathlib import Path
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from aiogram.exceptions import TelegramBadRequest
import aiosqlite
from cryptography.fernet import Fernet, InvalidToken
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.account import UpdateProfileRequest
from io import BytesIO
from telethon import events, functions
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError, PasswordHashInvalidError

VERSION='Rebuild 3'
logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO'))
log=logging.getLogger('mmd-self')
TOKEN=os.getenv('BOT_TOKEN','').strip()
OWNER_ID=int(os.getenv('OWNER_ID','0') or 0)
if not TOKEN or OWNER_ID <= 0:
    raise RuntimeError('BOT_TOKEN and a numeric OWNER_ID are required environment variables.')
API_ID=int(os.getenv('TG_API_ID','0'))
API_HASH=os.getenv('TG_API_HASH','').strip()
DB_PATH=os.getenv('DB_PATH','/data/mmd_self.db')
ASSET=Path(__file__).parent/'assets'/'mmd_self_menu.png'
secret=os.getenv('SESSION_SECRET','').strip()
FERNET=Fernet(secret.encode()) if secret else None
bot=Bot(TOKEN,default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp=Dispatcher()
flows={}
FLOW_TTL=600
active_clients={}
feature_tasks={}
translation_cooldowns={}
CLOCK_INTERVAL=int(os.getenv('CLOCK_INTERVAL_SECONDS','900'))
TRANSLATION_ENABLED_DEFAULT=False

# This rebuild intentionally starts with a small, understandable core. Sessions are encrypted at rest.

STYLE_PRESETS = {
    "decorative": ("✨ تزئینی", lambda s: f"『{s}』"),
    "bold": ("𝗕𝗼𝗹𝗱 (لاتین)", None),
    "italic": ("𝘐𝘵𝘢𝘭𝘪𝘤 (لاتین)", None),
    "bolditalic": ("𝙱𝙤𝙡𝙙 (لاتین)", None),
    "monospace": ("𝙼𝚘𝚗𝚘 (لاتین)", None),
    "double": ("𝔻𝕠𝕦𝕓𝕝𝕖 (لاتین)", None),
    "fraktur": ("𝔉𝔯𝔞𝔨𝔱𝔲𝔯 (لاتین)", None),
    "bubble": ("ⓑⓤⓑⓑⓛⓔ (لاتین)", None),
    "brackets": ("【 قاب‌دار 】", lambda s: f"【 {s} 】"),
    "stars": ("★ ستاره‌ای ★", lambda s: f"★彡 {s} 彡★"),
    "minimal": ("— مینیمال —", lambda s: f"— {s} —"),
}
def _map_latin(text, start_upper, start_lower, start_digits=None):
    out=[]
    for ch in text:
        n=ord(ch)
        if 'A'<=ch<='Z': out.append(chr(start_upper+n-65))
        elif 'a'<=ch<='z': out.append(chr(start_lower+n-97))
        elif ch.isdigit() and start_digits is not None: out.append(chr(start_digits+int(ch)))
        else: out.append(ch)
    return ''.join(out)
def style_text(text, style):
    if style in ("bold", "bolditalic", "double", "fraktur", "bubble", "monospace"):
        maps = {
            "bold": (0x1D400,0x1D41A,None),
            "bolditalic": (0x1D468,0x1D482,None),
            "double": (0x1D538,0x1D552,0x1D7D8),
            "fraktur": (0x1D504,0x1D51E,None),
            "bubble": (0x24B6,0x24D0,0x2460),
            "monospace": (0x1D670,0x1D68A,0x1D7F6),
        }
        u,l,d=maps[style]
        return _map_latin(text,u,l,d)
    if style=="italic":
        # Unicode mathematical italic has a few legacy holes.
        return _map_latin(text,0x1D434,0x1D44E,None)
    return STYLE_PRESETS.get(style, STYLE_PRESETS["decorative"])[1](text) if STYLE_PRESETS.get(style, STYLE_PRESETS["decorative"])[1] else text

def features_kb(settings):
    return InlineKeyboardMarkup(inline_keyboard=[
      [InlineKeyboardButton(text=('🌐 ترجمه خودکار: روشن' if settings['translate_on'] else '🌐 ترجمه خودکار: خاموش'),callback_data='feature:translate')],
      [InlineKeyboardButton(text=('✍️ استایل خودکار متن: روشن' if settings['style_on'] else '✍️ استایل خودکار متن: خاموش'),callback_data='feature:style')],
      [InlineKeyboardButton(text=f'🔤 انتخاب استایل: {STYLE_PRESETS.get(settings["style_name"], STYLE_PRESETS["decorative"])[0]}',callback_data='feature:styles')],
      [InlineKeyboardButton(text=('🕒 ساعت زنده پروفایل: روشن' if settings['clock_on'] else '🕒 ساعت زنده پروفایل: خاموش'),callback_data='feature:clock')],
      [InlineKeyboardButton(text='ℹ️ راهنمای قابلیت‌ها',callback_data='feature:help')],
      [InlineKeyboardButton(text='↩️ بازگشت به پنل اصلی',callback_data='main')]
    ])
async def get_settings(uid):
    c=await db(); c.row_factory=aiosqlite.Row
    await c.execute('INSERT OR IGNORE INTO settings(uid,updated) VALUES(?,?)',(uid,int(time.time())))
    await c.commit()
    cur=await c.execute('SELECT * FROM settings WHERE uid=?',(uid,)); row=dict(await cur.fetchone()); await c.close()
    return row
async def update_settings(uid, **kwargs):
    allowed={'translate_on','style_on','style_name','clock_on','base_first_name','base_last_name','base_bio','updated'}
    kwargs={k:v for k,v in kwargs.items() if k in allowed}
    if not kwargs: return
    kwargs['updated']=int(time.time())
    c=await db()
    await c.execute('INSERT OR IGNORE INTO settings(uid,updated) VALUES(?,?)',(uid,int(time.time())))
    await c.execute('UPDATE settings SET '+','.join(f'{k}=?' for k in kwargs)+' WHERE uid=?',(*kwargs.values(),uid))
    await c.commit(); await c.close()
async def features_screen(target,uid):
    s=await get_settings(uid)
    await answer_screen(target,'✨ <b>قابلیت‌های سلف</b>\n\nقابلیت‌ها را جداگانه روشن و خاموش کن. برای کارکرد امکانات حساب، باید حساب خودت متصل باشد.',reply_markup=features_kb(s))
async def ensure_user_client(uid):
    """Reconnect only the user's own encrypted session, then attach opt-in features."""
    if uid in active_clients and active_clients[uid].is_connected():
        return active_clients[uid]
    if not API_ID or not API_HASH or not FERNET:
        raise RuntimeError('تنظیمات API یا SESSION_SECRET ناقص است')
    c=await db(); cur=await c.execute('SELECT encrypted FROM sessions WHERE uid=?',(uid,)); row=await cur.fetchone(); await c.close()
    if not row: raise RuntimeError('ابتدا حساب خودت را متصل کن')
    try: session=FERNET.decrypt(row[0].encode()).decode()
    except (InvalidToken, Exception) as exc: raise RuntimeError('نشست ذخیره‌شده قابل بازگشایی نیست') from exc
    client=TelegramClient(StringSession(session),API_ID,API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError('نشست منقضی شده؛ دوباره حساب را متصل کن')
    active_clients[uid]=client
    client.add_event_handler(lambda e, _uid=uid: outgoing_style_handler(_uid,e), events.NewMessage(outgoing=True))
    client.add_event_handler(lambda e, _uid=uid: incoming_translate_handler(_uid,e), events.NewMessage(incoming=True))
    return client

async def translate_to_persian(text):
    # MyMemory supports automatic source-language detection; it is rate-limited and may return an error.
    import aiohttp
    from urllib.parse import urlencode
    query=urlencode({'q':text[:450], 'langpair':'autodetect|fa'})
    timeout=aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get('https://api.mymemory.translated.net/get?'+query) as response:
            if response.status != 200: raise RuntimeError(f'translation HTTP {response.status}')
            data=await response.json(content_type=None)
    result=(data.get('responseData') or {}).get('translatedText','').strip()
    if not result or (data.get('responseStatus',200) not in (200,'200')):
        raise RuntimeError('translation service returned no result')
    return result

async def outgoing_style_handler(uid,event):
    try:
        s=await get_settings(uid)
        if not s['style_on']: return
        txt=event.raw_text or ''
        if not txt or txt.startswith('/') or txt.startswith('✨') or len(txt)>3500: return
        styled=style_text(txt,s['style_name'])
        if styled != txt: await event.edit(styled)
    except Exception as exc:
        log.debug('style transform skipped for %s: %s',uid,type(exc).__name__)

async def incoming_translate_handler(uid,event):
    try:
        s=await get_settings(uid)
        if not s['translate_on']: return
        msg=(event.raw_text or '').strip()
        if not msg or msg.startswith('/') or len(msg)>450: return
        now=time.time()
        key=(uid,event.chat_id)
        if now-translation_cooldowns.get(key,0)<1.5: return
        translation_cooldowns[key]=now
        translated=await translate_to_persian(msg)
        sender=await event.get_sender()
        sender_name=getattr(sender,'first_name',None) or getattr(sender,'title',None) or 'مخاطب'
        await bot.send_message(uid,f'🌐 <b>ترجمه به فارسی</b>\nگفتگو: {html.escape(str(sender_name))}\n\n{html.escape(translated)}')
    except Exception as exc:
        log.debug('translation skipped for %s: %s',uid,type(exc).__name__)

def main_kb(is_owner=False):
    rows=[[InlineKeyboardButton(text='🔐 اتصال حساب خودم',callback_data='login:start')],
          [InlineKeyboardButton(text='📊 وضعیت حساب',callback_data='account:status'),InlineKeyboardButton(text='🛡 حریم خصوصی',callback_data='privacy')],
          [InlineKeyboardButton(text='✨ قابلیت‌های سلف',callback_data='features:home')]]
    if is_owner: rows.append([InlineKeyboardButton(text='👑 مدیریت درخواست‌ها',callback_data='admin:requests')])
    rows.append([InlineKeyboardButton(text='❌ بستن پنل',callback_data='close')])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def login_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='📱 ارسال شماره با دکمه',callback_data='login:phone_button')],[InlineKeyboardButton(text='✍️ واردکردن شماره دستی',callback_data='login:phone_manual')],[InlineKeyboardButton(text='↩️ بازگشت به پنل',callback_data='main')]])

def phone_reply_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='📱 ارسال شماره تماس',request_contact=True)],[KeyboardButton(text='❌ لغو ورود')]],resize_keyboard=True,one_time_keyboard=True)

async def db():
    Path(DB_PATH).parent.mkdir(parents=True,exist_ok=True)
    return await aiosqlite.connect(DB_PATH)

async def init_db():
    c=await db()
    await c.executescript("""CREATE TABLE IF NOT EXISTS users(uid INTEGER PRIMARY KEY, username TEXT, first_name TEXT, approved INTEGER NOT NULL DEFAULT 0, blocked INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL); CREATE TABLE IF NOT EXISTS sessions(uid INTEGER PRIMARY KEY, encrypted TEXT NOT NULL, phone TEXT NOT NULL, updated INTEGER NOT NULL); CREATE TABLE IF NOT EXISTS requests(uid INTEGER PRIMARY KEY, created INTEGER NOT NULL); CREATE TABLE IF NOT EXISTS settings(uid INTEGER PRIMARY KEY, translate_on INTEGER NOT NULL DEFAULT 0, style_on INTEGER NOT NULL DEFAULT 0, style_name TEXT NOT NULL DEFAULT 'decorative', clock_on INTEGER NOT NULL DEFAULT 0, base_first_name TEXT NOT NULL DEFAULT '', base_last_name TEXT NOT NULL DEFAULT '', base_bio TEXT NOT NULL DEFAULT '', updated INTEGER NOT NULL DEFAULT 0);""")
    await c.commit(); await c.close()

async def upsert_user(m):
    u=m.from_user; c=await db()
    await c.execute('INSERT INTO users(uid,username,first_name,created) VALUES(?,?,?,?) ON CONFLICT(uid) DO UPDATE SET username=excluded.username,first_name=excluded.first_name',(u.id,u.username or '',u.first_name or '',int(time.time())))
    await c.commit(); await c.close()

async def user_row(uid):
    c=await db(); c.row_factory=aiosqlite.Row
    cur=await c.execute('SELECT * FROM users WHERE uid=?',(uid,)); row=await cur.fetchone(); await c.close(); return row

async def approved(uid):
    if uid==OWNER_ID: return True
    r=await user_row(uid); return bool(r and r['approved'] and not r['blocked'])

async def send_panel(target,uid):
    text=('👑 <b>MMD SELF · پنل مدیریت</b>\n\nسلام مدیر! از اینجا می‌توانی وضعیت سرویس و درخواست‌ها را مدیریت کنی.\n\n<i>نسخهٔ بازسازی‌شده · هستهٔ سبک و قابل توسعه</i>' if uid==OWNER_ID else '⚡ <b>MMD SELF</b>\n\nپنل حساب شخصی تو آماده است. اتصال فقط با رضایت خودت انجام می‌شود و نشست حساب به‌صورت رمزگذاری‌شده ذخیره می‌شود.')
    await answer_screen(target, text, reply_markup=main_kb(uid==OWNER_ID))

async def panel_image_for(uid):
    """Create a per-user banner with their own Telegram avatar when a session is available."""
    if not ASSET.exists(): return None
    try:
        from PIL import Image, ImageDraw
        client=active_clients.get(uid)
        if client is None or not client.is_connected():
            # Don't force-connect for merely viewing the panel; only use an already active client.
            return ASSET
        photo=BytesIO()
        await client.download_profile_photo('me',file=photo)
        if not photo.getvalue(): return ASSET
        photo.seek(0)
        base=Image.open(ASSET).convert('RGBA')
        avatar=Image.open(photo).convert('RGBA')
        size=max(90,min(base.width//7,190))
        avatar=avatar.resize((size,size),Image.Resampling.LANCZOS)
        mask=Image.new('L',(size,size),0); ImageDraw.Draw(mask).ellipse((0,0,size-1,size-1),fill=255)
        avatar.putalpha(mask)
        # Position above the large MMD SELF title without obscuring the main title.
        x=int(base.width*0.72-size/2); y=int(base.height*0.105-size/2)
        draw=ImageDraw.Draw(base)
        draw.ellipse((x-8,y-8,x+size+8,y+size+8),fill=(255,178,45,255))
        base.alpha_composite(avatar,(x,y))
        out=Path('/tmp')/f'mmd_self_banner_{uid}.png'
        base.save(out,'PNG')
        return out
    except Exception as exc:
        log.debug('profile banner fallback for %s: %s',uid,type(exc).__name__)
        return ASSET

async def answer_screen(target, text, reply_markup=None):
    """Show branded image on major screens; overlay the connected user's avatar when available."""
    if ASSET.exists():
        from aiogram.types import FSInputFile
        try:
            uid=getattr(getattr(target,'chat',None),'id',0) or getattr(getattr(target,'from_user',None),'id',0)
            img=await panel_image_for(int(uid)) if uid else ASSET
            return await target.answer_photo(FSInputFile(img or ASSET), caption=text, reply_markup=reply_markup)
        except Exception:
            log.exception('screen image send failed')
    return await target.answer(text, reply_markup=reply_markup)

def active_flow(uid):
    flow = flows.get(uid)
    if not flow:
        return None
    if time.time() - flow.get('created', time.time()) > FLOW_TTL:
        flows.pop(uid, None)
        client = flow.get('client')
        if client:
            asyncio.create_task(client.disconnect())
        return None
    return flow

async def require_access_callback(c: CallbackQuery):
    if not await approved(c.from_user.id):
        await c.answer('دسترسی فعال نیست؛ /start را بزن.', show_alert=True)
        return False
    return True

@dp.message(Command('help'))
async def help_cmd(m: Message):
    await answer_screen(m, '📘 <b>راهنمای MMD SELF</b>\n\n/start — شروع یا بازکردن پنل\n/panel — بازکردن پنل\n/cancel — لغو ورود\n\nبرای اتصال حساب، فقط حساب متعلق به خودت را استفاده کن و کد ورود را با هیچ‌کس به اشتراک نگذار.')

@dp.message(Command('cancel'))
async def cancel_cmd(m: Message):
    flow = flows.pop(m.from_user.id, None)
    if flow and flow.get('client'):
        try: await flow['client'].disconnect()
        except Exception: log.exception('client disconnect during cancel failed')
    await m.answer('ورود لغو شد.', reply_markup=ReplyKeyboardRemove())

@dp.message(CommandStart())
async def start(m:Message):
    await upsert_user(m); uid=m.from_user.id
    if uid==OWNER_ID or await approved(uid):
        await send_panel(m,uid); return
    c=await db(); cur=await c.execute('SELECT uid FROM requests WHERE uid=?',(uid,)); existed=await cur.fetchone()
    await c.execute('INSERT OR IGNORE INTO requests(uid,created) VALUES(?,?)',(uid,int(time.time())))
    await c.commit(); await c.close()
    await m.answer('👋 به <b>MMD SELF</b> خوش آمدی.\n\nدرخواست دسترسی برای مدیر ارسال شد. پس از تأیید، دوباره /start را بزن.')
    if not existed:
        try: await bot.send_message(OWNER_ID,f'🔔 <b>درخواست دسترسی جدید</b>\nکاربر: {m.from_user.full_name}\nشناسه: <code>{uid}</code>\nنام کاربری: @{m.from_user.username or "ندارد"}',reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='✅ تأیید',callback_data=f'approve:{uid}'),InlineKeyboardButton(text='🚫 رد',callback_data=f'reject:{uid}')]]))
        except Exception: log.exception('notify owner failed')

@dp.message(Command('panel'))
async def panel_cmd(m:Message):
    if await approved(m.from_user.id): await send_panel(m,m.from_user.id)
    else: await m.answer('دسترسی فعال نیست. /start را بزن تا درخواستت ثبت شود.')

@dp.callback_query(F.data=='main')
async def main_cb(c:CallbackQuery):
    if await approved(c.from_user.id):
        await answer_screen(c.message, '🎛 <b>منوی اصلی</b>', reply_markup=main_kb(c.from_user.id==OWNER_ID))
    else: await c.answer('دسترسی فعال نیست.',show_alert=True); return
    await c.answer()

@dp.callback_query(F.data.startswith('approve:'))
async def approve_cb(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID: await c.answer('فقط مدیر اصلی.',show_alert=True); return
    uid=int(c.data.split(':')[1]); conn=await db(); await conn.execute('UPDATE users SET approved=1,blocked=0 WHERE uid=?',(uid,)); await conn.execute('DELETE FROM requests WHERE uid=?',(uid,)); await conn.commit(); await conn.close()
    await c.answer('تأیید شد'); await c.message.edit_reply_markup(reply_markup=None)
    try: await bot.send_message(uid,'✅ دسترسی شما فعال شد. برای شروع /start را بزنید.')
    except Exception: pass

@dp.callback_query(F.data.startswith('reject:'))
async def reject_cb(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID: await c.answer('فقط مدیر اصلی.',show_alert=True); return
    uid=int(c.data.split(':')[1]); conn=await db(); await conn.execute('DELETE FROM requests WHERE uid=?',(uid,)); await conn.execute('UPDATE users SET approved=0 WHERE uid=?',(uid,)); await conn.commit(); await conn.close()
    await c.answer('رد شد'); await c.message.edit_reply_markup(reply_markup=None)
    try: await bot.send_message(uid,'درخواست دسترسی فعلاً تأیید نشد.')
    except Exception: pass

@dp.callback_query(F.data=='admin:requests')
async def requests_cb(c:CallbackQuery):
    if c.from_user.id!=OWNER_ID: await c.answer('فقط مدیر اصلی.',show_alert=True); return
    conn=await db(); conn.row_factory=aiosqlite.Row; cur=await conn.execute('SELECT r.uid,r.created,u.username,u.first_name FROM requests r LEFT JOIN users u ON u.uid=r.uid ORDER BY r.created DESC LIMIT 20'); rows=await cur.fetchall(); await conn.close()
    if not rows: await c.message.answer('درخواست در انتظار نداریم.')
    else:
        for r in rows:
            await c.message.answer(f'👤 {r["first_name"] or "کاربر"} · @{r["username"] or "ندارد"}\nشناسه: <code>{r["uid"]}</code>',reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='✅ تأیید',callback_data=f'approve:{r["uid"]}'),InlineKeyboardButton(text='🚫 رد',callback_data=f'reject:{r["uid"]}')]]))
    await c.answer()

@dp.callback_query(F.data=='login:start')
async def login_start(c:CallbackQuery):
    if not await approved(c.from_user.id): await c.answer('دسترسی فعال نیست.',show_alert=True); return
    if not API_ID or not API_HASH or not FERNET:
        await c.message.answer('⚙️ ورود حساب هنوز تنظیم نشده است. مدیر باید TG_API_ID، TG_API_HASH و SESSION_SECRET را در Railway تنظیم کند.')
        await c.answer(); return
    await answer_screen(c.message, '🔐 <b>اتصال حساب تلگرام</b>\n\nاین کار نشست ورود حساب خودت را ایجاد می‌کند. کد و رمز دومرحله‌ای را فقط برای حساب خودت وارد کن؛ هرگز آن‌ها را برای افراد دیگر نفرست. نشست ذخیره‌شده رمزگذاری می‌شود.',reply_markup=login_kb())
    await c.answer()

@dp.callback_query(F.data=='login:phone_button')
async def phone_button(c:CallbackQuery):
    if not await approved(c.from_user.id): await c.answer('دسترسی فعال نیست.',show_alert=True); return
    flows[c.from_user.id]={'stage':'phone'}
    await c.message.answer('برای ارسال شماره با دکمهٔ تماس، از صفحه‌کلید زیر استفاده کن. اگر دکمه در دستگاهت پشتیبانی نشد، گزینهٔ ورود دستی را بزن.',reply_markup=phone_reply_kb()); await c.answer()

@dp.callback_query(F.data=='login:phone_manual')
async def phone_manual(c:CallbackQuery):
    if not await approved(c.from_user.id): await c.answer('دسترسی فعال نیست.',show_alert=True); return
    flows[c.from_user.id]={'stage':'phone'}
    await c.message.answer('شماره را با کد کشور بفرست؛ مثال: <code>+491234567890</code>\nفاصله و خط تیره پذیرفته می‌شود.',reply_markup=ReplyKeyboardRemove()); await c.answer()

def normalize_phone(raw):
    s=raw.translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩','01234567890123456789'))
    s=re.sub(r'[\s()\-–.]','',s)
    if s.startswith('00'): s='+'+s[2:]
    if not s.startswith('+') or not s[1:].isdigit() or not 8<=len(s[1:])<=15: return None
    return s

async def begin_phone(uid,phone,reply):
    if not FERNET or not API_ID or not API_HASH:
        await reply.answer('تنظیمات ورود کامل نیست.'); flows.pop(uid,None); return
    old=flows.pop(uid,None)
    if old and old.get('client'):
        try: await old['client'].disconnect()
        except Exception: pass
    client=TelegramClient(StringSession(),API_ID,API_HASH)
    try:
        await client.connect()
        sent=await client.send_code_request(phone)
        flows[uid]={'stage':'code','phone':phone,'client':client,'phone_code_hash':sent.phone_code_hash,'created':time.time()}
        await reply.answer('📩 کد تلگرام ارسال شد. کد را همین‌جا وارد کن. برای امنیت، کد در لاگ ذخیره نمی‌شود.',reply_markup=ReplyKeyboardRemove())
    except Exception as e:
        log.warning('send code failed (%s)',type(e).__name__)
        await client.disconnect(); await reply.answer('ارسال کد انجام نشد. شماره و تنظیمات API را بررسی کن و دوباره تلاش کن.')

@dp.message(F.contact)
async def contact_received(m:Message):
    uid=m.from_user.id; f=active_flow(uid)
    if not await approved(uid):
        flows.pop(uid, None); await m.answer('دسترسی فعال نیست.'); return
    if not f or f.get('stage')!='phone': return
    if m.contact.user_id and m.contact.user_id!=uid:
        await m.answer('لطفاً فقط شمارهٔ متعلق به خودت را ارسال کن.'); return
    phone=normalize_phone(m.contact.phone_number)
    if not phone: await m.answer('شماره معتبر نیست؛ شماره را دستی با + و کد کشور بفرست.'); return
    await begin_phone(uid,phone,m)

@dp.message(F.text)
async def text_router(m:Message):
    uid=m.from_user.id; txt=(m.text or '').strip(); f=active_flow(uid)
    if txt=='❌ لغو ورود':
        f=flows.pop(uid,None)
        if f and f.get('client'):
            try: await f['client'].disconnect()
            except Exception: pass
        await m.answer('ورود لغو شد.',reply_markup=ReplyKeyboardRemove()); return
    if not f: return
    if not await approved(uid):
        flows.pop(uid, None); await m.answer('دسترسی فعال نیست.', reply_markup=ReplyKeyboardRemove()); return
    if f.get('stage')=='phone':
        phone=normalize_phone(txt)
        if not phone: await m.answer('شماره درست نیست. نمونه: <code>+491234567890</code>؛ فاصله و خط تیره هم قابل قبول است.'); return
        await begin_phone(uid,phone,m); return
    if f.get('stage')=='code':
        code=re.sub(r'\D','',txt.translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩','01234567890123456789')))
        if not 4<=len(code)<=8: await m.answer('کد باید فقط شامل رقم باشد. دوباره واردش کن.'); return
        client=f['client']
        try:
            await client.sign_in(phone=f['phone'],code=code,phone_code_hash=f['phone_code_hash'])
            await save_session(uid,client,f['phone']); flows.pop(uid,None); await client.disconnect()
            await m.answer('✅ حساب با موفقیت متصل شد. نشست شما رمزگذاری‌شده ذخیره شده است.',reply_markup=ReplyKeyboardRemove())
        except SessionPasswordNeededError:
            f['stage']='password'; f['created']=time.time(); await m.answer('🔒 رمز دومرحله‌ای حساب خودت را وارد کن. این مقدار در لاگ ثبت نمی‌شود.')
        except PhoneCodeInvalidError: await m.answer('کد نادرست است؛ دوباره بررسی و وارد کن.')
        except PhoneCodeExpiredError:
            old=flows.pop(uid, None)
            if old and old.get('client'):
                try: await old['client'].disconnect()
                except Exception: pass
            await m.answer('کد منقضی شده است. ورود را از ابتدا شروع کن.')
        except Exception as e: log.warning('sign in failed (%s)',type(e).__name__); await m.answer('ورود انجام نشد. دوباره تلاش کن یا ورود را لغو کن.')
        return
    if f.get('stage')=='password':
        try:
            await f['client'].sign_in(password=txt)
            await save_session(uid,f['client'],f['phone']); client=f['client']; flows.pop(uid,None); await client.disconnect()
            await m.answer('✅ حساب متصل شد و نشست رمزگذاری‌شده ذخیره شد.',reply_markup=ReplyKeyboardRemove())
        except PasswordHashInvalidError: f['created']=time.time(); await m.answer('رمز درست نیست؛ دوباره وارد کن.')
        except Exception as e: log.warning('2fa login failed (%s)',type(e).__name__); await m.answer('ورود انجام نشد. دوباره تلاش کن یا لغو کن.')
        return

async def save_session(uid,client,phone):
    if not FERNET: raise RuntimeError('SESSION_SECRET missing')
    encrypted=FERNET.encrypt(client.session.save().encode()).decode()
    conn=await db(); await conn.execute('INSERT INTO sessions(uid,encrypted,phone,updated) VALUES(?,?,?,?) ON CONFLICT(uid) DO UPDATE SET encrypted=excluded.encrypted,phone=excluded.phone,updated=excluded.updated',(uid,encrypted,phone,int(time.time()))); await conn.commit(); await conn.close()

@dp.callback_query(F.data=='account:status')
async def status_cb(c:CallbackQuery):
    if not await require_access_callback(c): return
    conn=await db(); cur=await conn.execute('SELECT phone,updated FROM sessions WHERE uid=?',(c.from_user.id,)); row=await cur.fetchone(); await conn.close()
    if row: phone_display=(row[0][:3] + '••••' + row[0][-3:]) if row[0] and len(row[0]) > 7 else 'ثبت‌شده'
    if row: msg=f'✅ حساب متصل است\nشماره: <code>{phone_display}</code>\nآخرین اتصال: {time.strftime("%Y-%m-%d %H:%M UTC",time.gmtime(row[1]))}'
    else: msg='ℹ️ هنوز حسابی متصل نکرده‌ای.'
    await answer_screen(c.message, msg); await c.answer()

@dp.callback_query(F.data=='privacy')
async def privacy_cb(c:CallbackQuery):
    if not await require_access_callback(c): return
    await answer_screen(c.message, '🛡 <b>حریم خصوصی</b>\n\n• کد ورود و رمز دومرحله‌ای در لاگ نوشته نمی‌شود.\n• نشست حساب در پایگاه داده رمزگذاری می‌شود.\n• SESSION_SECRET را محرمانه نگه دار؛ گم‌شدن آن یعنی نشست‌های ذخیره‌شده قابل رمزگشایی نیستند.\n• فقط حساب خودت را متصل کن.\n• برای لغو نشست از تنظیمات Telegram → Devices نشست مربوط را پایان بده.')
    await c.answer()


@dp.callback_query(F.data=='features:home')
async def features_home_cb(c:CallbackQuery):
    if not await require_access_callback(c): return
    await features_screen(c.message,c.from_user.id); await c.answer()

@dp.callback_query(F.data=='feature:translate')
async def feature_translate_cb(c:CallbackQuery):
    if not await require_access_callback(c): return
    s=await get_settings(c.from_user.id); new=0 if s['translate_on'] else 1
    if new:
        try: await ensure_user_client(c.from_user.id)
        except Exception as exc:
            await c.answer(str(exc)[:180],show_alert=True); return
    await update_settings(c.from_user.id,translate_on=new)
    await features_screen(c.message,c.from_user.id); await c.answer('تنظیم ذخیره شد')

@dp.callback_query(F.data=='feature:style')
async def feature_style_cb(c:CallbackQuery):
    if not await require_access_callback(c): return
    s=await get_settings(c.from_user.id); new=0 if s['style_on'] else 1
    if new:
        try: await ensure_user_client(c.from_user.id)
        except Exception as exc:
            await c.answer(str(exc)[:180],show_alert=True); return
    await update_settings(c.from_user.id,style_on=new)
    await features_screen(c.message,c.from_user.id); await c.answer('تنظیم ذخیره شد')

@dp.callback_query(F.data=='feature:styles')
async def feature_styles_cb(c:CallbackQuery):
    if not await require_access_callback(c): return
    s=await get_settings(c.from_user.id)
    rows=[]
    for key,(label,_) in STYLE_PRESETS.items():
        rows.append([InlineKeyboardButton(text=('✅ '+label if s['style_name']==key else label),callback_data='style:set:'+key)])
    rows.append([InlineKeyboardButton(text='↩️ بازگشت',callback_data='features:home')])
    await answer_screen(c.message,'🔤 <b>انتخاب استایل متن</b>\n\nتوجه: تلگرام برای حروف فارسی فونت یونیکد متنوعی ندارد؛ استایل‌های فارسی با قاب و تزئین نمایش داده می‌شوند و استایل‌های خاص حروف بیشتر روی لاتین اثر دارند.',reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await c.answer()

@dp.callback_query(F.data.startswith('style:set:'))
async def style_set_cb(c:CallbackQuery):
    if not await require_access_callback(c): return
    key=c.data.split(':',2)[2]
    if key not in STYLE_PRESETS: await c.answer('استایل نامعتبر است',show_alert=True); return
    await update_settings(c.from_user.id,style_name=key)
    await features_screen(c.message,c.from_user.id); await c.answer('استایل انتخاب شد')

@dp.callback_query(F.data=='feature:clock')
async def feature_clock_cb(c:CallbackQuery):
    if not await require_access_callback(c): return
    uid=c.from_user.id; s=await get_settings(uid); new=0 if s['clock_on'] else 1
    try:
        client=await ensure_user_client(uid)
        if new:
            me=await client.get_me()
            from telethon.tl.functions.users import GetFullUserRequest
            full_user=await client(GetFullUserRequest('me'))
            base_bio=getattr(full_user.full_user,'about','') or ''
            await update_settings(uid,base_first_name=me.first_name or '',base_last_name=me.last_name or '',base_bio=base_bio,clock_on=1)
            if uid not in feature_tasks or feature_tasks[uid].done():
                feature_tasks[uid]=asyncio.create_task(profile_clock_loop(uid))
        else:
            await update_settings(uid,clock_on=0)
            task=feature_tasks.get(uid)
            if task: task.cancel()
            await client(UpdateProfileRequest(first_name=s['base_first_name'] or '',last_name=s['base_last_name'] or '',about=s['base_bio'] or ''))
    except Exception as exc:
        await c.answer(('خطا: '+str(exc))[:180],show_alert=True); return
    await features_screen(c.message,uid); await c.answer('تنظیم ساعت ذخیره شد')

async def profile_clock_loop(uid):
    from telethon.tl.functions.account import UpdateProfileRequest
    try:
        while True:
            s=await get_settings(uid)
            if not s['clock_on']: return
            client=await ensure_user_client(uid)
            clock=time.strftime('%H:%M',time.localtime())
            base=(s['base_first_name'] or 'MMD').strip()
            clean=re.sub(r'\s*⏰\s*\d{2}:\d{2}$','',base)
            name=(clean+' ⏰ '+clock)[:64]
            await client(UpdateProfileRequest(first_name=name,last_name=s['base_last_name'] or '',about=s['base_bio'] or ''))
            await asyncio.sleep(max(600,CLOCK_INTERVAL))
    except asyncio.CancelledError: raise
    except Exception as exc: log.warning('profile clock stopped for %s: %s',uid,type(exc).__name__)

@dp.callback_query(F.data=='feature:help')
async def feature_help_cb(c:CallbackQuery):
    if not await require_access_callback(c): return
    await answer_screen(c.message,'📘 <b>راهنمای قابلیت‌ها</b>\n\n🌐 ترجمه: پیام‌های ورودی کوتاه را با تشخیص خودکار زبان به فارسی ترجمه و نتیجه را در همین ربات برای خودت می‌فرستد. سرویس ترجمهٔ بیرونی استفاده می‌شود؛ اطلاعات حساس نفرست.\n\n✍️ استایل متن: پیام‌های متنی جدیدی که از حساب متصل خودت می‌فرستی را ویرایش می‌کند. برای حروف فارسی استایل تزئینی به‌کار می‌رود؛ فونت واقعی فارسی در یونیکد محدود است.\n\n🕒 ساعت پروفایل: نام نمایشی را حداکثر هر ۱۵ دقیقه به‌روزرسانی می‌کند؛ ممکن است محدودیت تلگرام اعمال شود. خاموش‌کردن قابلیت، نام و بیوی ذخیره‌شده پیشین را برمی‌گرداند.\n\nهر قابلیت را فقط برای حساب خودت روشن کن.')
    await c.answer()


@dp.callback_query(F.data=='close')
async def close_cb(c:CallbackQuery):
    try: await c.message.delete()
    except TelegramBadRequest: pass
    await c.answer()

async def main():
    await init_db()
    me=await bot.get_me(); log.info('MMD SELF started as @%s (%s)',me.username,VERSION)
    # Resume opted-in profile clock tasks after Railway restarts.
    conn=await db(); cur=await conn.execute('SELECT uid,clock_on,translate_on,style_on FROM settings WHERE clock_on=1 OR translate_on=1 OR style_on=1'); enabled_users=await cur.fetchall(); await conn.close()
    for uid,clock_on,translate_on,style_on in enabled_users:
        try:
            await ensure_user_client(uid)
            if clock_on:
                feature_tasks[uid]=asyncio.create_task(profile_clock_loop(uid))
        except Exception as exc:
            log.warning('could not resume features for %s: %s',uid,type(exc).__name__)
    await dp.start_polling(bot,allowed_updates=dp.resolve_used_update_types())

if __name__=='__main__':
    try: asyncio.run(main())
    finally:
        for f in list(flows.values()):
            if f.get('client'):
                try: asyncio.run(f['client'].disconnect())
                except Exception: pass
