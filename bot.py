import asyncio, logging, os, re, time
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
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError, PasswordHashInvalidError

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

# This rebuild intentionally starts with a small, understandable core. Sessions are encrypted at rest.
def main_kb(is_owner=False):
    rows=[[InlineKeyboardButton(text='🔐 اتصال حساب خودم',callback_data='login:start')],
          [InlineKeyboardButton(text='📊 وضعیت حساب',callback_data='account:status'),InlineKeyboardButton(text='🛡 حریم خصوصی',callback_data='privacy')]]
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
    await c.executescript("""CREATE TABLE IF NOT EXISTS users(uid INTEGER PRIMARY KEY, username TEXT, first_name TEXT, approved INTEGER NOT NULL DEFAULT 0, blocked INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL); CREATE TABLE IF NOT EXISTS sessions(uid INTEGER PRIMARY KEY, encrypted TEXT NOT NULL, phone TEXT NOT NULL, updated INTEGER NOT NULL); CREATE TABLE IF NOT EXISTS requests(uid INTEGER PRIMARY KEY, created INTEGER NOT NULL);""")
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

async def answer_screen(target, text, reply_markup=None):
    """Show the brand image on each major panel screen, with text fallback."""
    if ASSET.exists():
        from aiogram.types import FSInputFile
        try:
            return await target.answer_photo(FSInputFile(ASSET), caption=text, reply_markup=reply_markup)
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

@dp.callback_query(F.data=='close')
async def close_cb(c:CallbackQuery):
    try: await c.message.delete()
    except TelegramBadRequest: pass
    await c.answer()

async def main():
    await init_db()
    me=await bot.get_me(); log.info('MMD SELF started as @%s',me.username)
    await dp.start_polling(bot,allowed_updates=dp.resolve_used_update_types())

if __name__=='__main__':
    try: asyncio.run(main())
    finally:
        for f in list(flows.values()):
            if f.get('client'):
                try: asyncio.run(f['client'].disconnect())
                except Exception: pass
