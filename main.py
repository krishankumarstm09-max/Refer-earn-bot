import asyncio, hashlib, hmac, html, ipaddress, json, logging, os, time
from urllib.parse import parse_qsl
from decimal import Decimal, InvalidOperation
from typing import Optional
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, ChatMemberUpdated, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('telegram_bot')
TOKEN=os.getenv('BOT_TOKEN','').strip(); DBURL=os.getenv('DATABASE_URL','').strip()
if DBURL.startswith('postgres://'): DBURL='postgresql://'+DBURL[11:]
ADMINS={int(x.strip()) for x in os.getenv('ADMIN_IDS','').split(',') if x.strip().isdigit()}
WEBAPP_URL=os.getenv('WEBAPP_URL','').strip().rstrip('/'); PORT=int(os.getenv('PORT','8080') or 8080)
if not TOKEN or not DBURL or not ADMINS: raise RuntimeError('Set BOT_TOKEN, DATABASE_URL and ADMIN_IDS')
bot=Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML)); dp=Dispatcher(); pool:Optional[asyncpg.Pool]=None

SCHEMA='''
CREATE TABLE IF NOT EXISTS users(id BIGINT PRIMARY KEY,username TEXT,first_name TEXT DEFAULT '',last_name TEXT DEFAULT '',wallet NUMERIC(18,2) DEFAULT 0,referrals INT DEFAULT 0,referred_by BIGINT REFERENCES users(id) ON DELETE SET NULL,referral_rewarded BOOLEAN DEFAULT FALSE,verified BOOLEAN DEFAULT FALSE,verification_token TEXT,created_at TIMESTAMPTZ DEFAULT NOW(),updated_at TIMESTAMPTZ DEFAULT NOW());
CREATE TABLE IF NOT EXISTS referrals(id BIGSERIAL PRIMARY KEY,referrer_id BIGINT REFERENCES users(id) ON DELETE CASCADE,referred_id BIGINT UNIQUE REFERENCES users(id) ON DELETE CASCADE,reward NUMERIC(18,2) DEFAULT 0,created_at TIMESTAMPTZ DEFAULT NOW());
CREATE TABLE IF NOT EXISTS withdrawals(id BIGSERIAL PRIMARY KEY,user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,amount NUMERIC(18,2),upi_id TEXT,attachment_file_id TEXT,status TEXT DEFAULT 'pending',issue TEXT,admin_id BIGINT,created_at TIMESTAMPTZ DEFAULT NOW(),updated_at TIMESTAMPTZ DEFAULT NOW());
CREATE TABLE IF NOT EXISTS required_channels(id BIGSERIAL PRIMARY KEY,chat_id BIGINT UNIQUE,title TEXT,invite_link TEXT,request_join BOOLEAN DEFAULT FALSE,enabled BOOLEAN DEFAULT TRUE,invite_style TEXT DEFAULT 'primary',invite_icon_custom_emoji_id TEXT);
CREATE TABLE IF NOT EXISTS buttons(key TEXT PRIMARY KEY,label TEXT,style TEXT DEFAULT 'default',enabled BOOLEAN DEFAULT TRUE,sort_order INT DEFAULT 0,icon_custom_emoji_id TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
ALTER TABLE required_channels ADD COLUMN IF NOT EXISTS invite_style TEXT DEFAULT 'primary';
ALTER TABLE required_channels ADD COLUMN IF NOT EXISTS invite_icon_custom_emoji_id TEXT;
ALTER TABLE buttons ADD COLUMN IF NOT EXISTS icon_custom_emoji_id TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS device_verified BOOLEAN DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS access_blocked BOOLEAN DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS penalty_total NUMERIC(18,2) DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN DEFAULT FALSE;
CREATE TABLE IF NOT EXISTS wallet_transactions(id BIGSERIAL PRIMARY KEY,user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,amount NUMERIC(18,2) NOT NULL,balance_after NUMERIC(18,2) NOT NULL,type TEXT NOT NULL,reason TEXT,admin_id BIGINT,created_at TIMESTAMPTZ DEFAULT NOW());
CREATE INDEX IF NOT EXISTS idx_wallet_tx_user ON wallet_transactions(user_id,created_at DESC);
ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS channel_message_id BIGINT;
ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS channel_id TEXT;
CREATE TABLE IF NOT EXISTS channel_leave_penalties(id BIGSERIAL PRIMARY KEY,user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,channel_id BIGINT REFERENCES required_channels(id) ON DELETE CASCADE,chat_id BIGINT NOT NULL,amount NUMERIC(18,2) NOT NULL DEFAULT 2,created_at TIMESTAMPTZ DEFAULT NOW());
CREATE INDEX IF NOT EXISTS idx_leave_penalty_user ON channel_leave_penalties(user_id,channel_id,created_at);
CREATE TABLE IF NOT EXISTS device_logs(id BIGSERIAL PRIMARY KEY,user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,device_id TEXT,fingerprint TEXT,ip TEXT,user_agent TEXT,status TEXT,reason TEXT,created_at TIMESTAMPTZ DEFAULT NOW());
CREATE INDEX IF NOT EXISTS idx_dl_device ON device_logs(device_id);
CREATE INDEX IF NOT EXISTS idx_dl_fp_ip ON device_logs(fingerprint,ip);
CREATE INDEX IF NOT EXISTS idx_dl_ip ON device_logs(ip);
CREATE TABLE IF NOT EXISTS states(user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,state TEXT,data JSONB DEFAULT '{}',updated_at TIMESTAMPTZ DEFAULT NOW());
CREATE TABLE IF NOT EXISTS join_requests(chat_id BIGINT NOT NULL,user_id BIGINT NOT NULL,created_at TIMESTAMPTZ DEFAULT NOW(),PRIMARY KEY(chat_id,user_id));
'''
DEFAULTS={'referral_amount':'5','min_withdrawal':'50','min_referrals':'0','device_check':'on','device_strict':'on','device_text':'✅ <b>Sabhi Channels Verified!</b>\n\n🛡 <b>Step 2: Device Verification</b>\n\nDuplicate/fake referral rokne ke liye aapke device aur network (IP) ki ek baar jaanch hogi. Neeche <b>Verify Device</b> button dabayein:','device_button':'🛡 Verify Device','ip_limit':'2','device_limit':'1','withdrawal_channel_id':'','penalty_amount':'2','penalty_text':'⚠️ <b>Channel Leave Penalty</b>\n\nAapne required channel chhod diya hai. ₹{penalty} penalty lagayi gayi hai.\n\nBot access tab tak blocked rahega jab tak aap sabhi required channels dobara join karke Verify Now nahi karte.','welcome':'🔥 <b>Welcome!</b>\n\nRefer friends, earn rewards and withdraw your balance.'}
BUTTONS=[('withdraw','💸 Withdraw','success',10),('referral','🔗 My Referral Link','primary',20),('wallet','💰 My Wallet','default',30),('leaderboard','🏆 Leaderboard','primary',40)]
STYLE={'#r':'danger','#g':'success','#b':'primary','#d':'default'}

def ibtn(text,style='default',icon_custom_emoji_id=None,**kw):
    args=dict(text=text,**kw)
    if style and style!='default':args['style']=style
    if icon_custom_emoji_id:args['icon_custom_emoji_id']=str(icon_custom_emoji_id)
    try:return InlineKeyboardButton(**args)
    except (TypeError,ValueError):
        args.pop('icon_custom_emoji_id',None)
        try:return InlineKeyboardButton(**args)
        except (TypeError,ValueError):
            args.pop('style',None);return InlineKeyboardButton(**args)
def btn(text,data,style='default',icon_custom_emoji_id=None):return ibtn(text,style,icon_custom_emoji_id,callback_data=data)
def kb(rows):return InlineKeyboardMarkup(inline_keyboard=[[btn(*x) for x in r] for r in rows])

def custom_emoji_id(m):
    for e in (getattr(m,'entities',None) or []):
        et=getattr(e,'type',None)
        if et is None and isinstance(e,dict): et=e.get('type')
        eid=getattr(e,'custom_emoji_id',None)
        if eid is None and isinstance(e,dict): eid=e.get('custom_emoji_id')
        if et=='custom_emoji' and eid:
            return str(eid)
    return None

def strip_custom_emoji(m,text):
    if not text:return text
    for e in reversed([e for e in (getattr(m,'entities',None) or []) if getattr(e,'type',None)=='custom_emoji']):
        try:
            off=getattr(e,'offset',0);ln=getattr(e,'length',0);b=text.encode('utf-16-le');text=(b[:off*2]+b[(off+ln)*2:]).decode('utf-16-le')
        except Exception:pass
    return text.strip()

def parse_button_input(m):
    raw=(m.text or '').strip();icon=custom_emoji_id(m);clean=strip_custom_emoji(m,raw);label,sty=parse_label(clean);return label,sty,icon
def has_tag(s):
    s=(s or '').strip().lower();return any(s.endswith(k) for k in STYLE)
def jd(v):
    if isinstance(v,dict):return v
    try:return json.loads(v or '{}')
    except Exception:return {}

TXT={
'welcome':('Welcome (/start) text',DEFAULTS['welcome'],''),
'verify_required':('Channel verification text','🔐 <b>Verification required</b>\n\nJoin the required channels and tap Verify Now.\n\nStep 2 me device/IP verification hogi taaki duplicate referral na ho sake.',''),
'verify_ok':('Verification success text','✅ <b>Verification successful.</b>',''),
'device_text':('Device verification text (Step 2)',DEFAULTS['device_text'],''),
'wallet':('Wallet text','💰 <b>Wallet</b>\n\n👤 Name: <b>{user_name}</b>\n🆔 User ID: <code>{user_id}</code>\n💰 Wallet Balance: <b>₹{user_wallet_balance}</b>\n👥 Referrals: <b>{referrals}</b>','{user_name} {user_id} {user_wallet_balance} {user_referral_link} {referrals}'),
'referral':('Referral link text','🔗 <b>Your Referral Link</b>\n\n<code>{user_referral_link}</code>\n\n💰 Reward per referral: <b>₹{reward}</b>\n👥 Total referrals: <b>{referrals}</b>','{user_name} {user_id} {user_wallet_balance} {user_referral_link} {reward} {referrals}'),
'referral_history':('Referral history text','👥 <b>Your Referral History</b>\n\nTotal referrals: <b>{referrals}</b>\nTotal earned: <b>₹{referral_earned}</b>\n\n{history}','{user_name} {user_id} {user_wallet_balance} {user_referral_link} {referrals} {referral_earned} {history}'),
'lb_title':('Leaderboard title','🏆 <b>TOP 10 LEADERBOARD</b>\n',''),
'lb_row':('Leaderboard row','{medal} {name}{username}\n   👥 {referrals} referrals | 💰 ₹{wallet}','{medal} {rank} {name} {username} {referrals} {wallet}'),
'lb_you':('Leaderboard "your rank" line','📍 Your rank: <b>#{rank}</b> | 👥 {referrals} referrals','{rank} {referrals}'),
'lb_empty':('Leaderboard empty text','🏆 No users yet.',''),
'wd_min':('Withdraw: low balance text','❌ Minimum withdrawal is ₹{min}. Your balance: ₹{balance}','{min} {balance}'),
'wd_refs':('Withdraw: referrals needed text','❌ You need {need} referrals. You have {have}.','{need} {have}'),
'wd_amount':('Withdraw: ask amount','💸 Send withdrawal amount (minimum ₹{min}).','{min}'),
'wd_upi':('Withdraw: ask UPI ID','💳 Send your UPI ID.',''),
'wd_attach':('Withdraw: ask screenshot','📎 Send screenshot/document or type SKIP.',''),
'wd_done':('Withdraw: submitted text','✅ Withdrawal #{id} submitted.\nStatus: <b>{status}</b>','{id} {status}'),
'wd_paid':('Withdraw: payment done text','✅ <b>Payment Done</b>\nWithdrawal #{id}\nAmount: ₹{amount}','{id} {amount}'),
'wd_rej':('Withdraw: rejected text','❌ <b>Withdrawal Rejected</b>\n#{id}\nRefunded: ₹{amount}\nIssue: {issue}','{id} {amount} {issue}'),
'penalty_text':('Channel leave penalty text',DEFAULTS['penalty_text'],'{penalty}'),
'ref_notify':('New referral notification','🎉 <b>New Referral!</b>\n\n👤 Name: <b>{name}</b>\n💰 Reward: <b>+₹{reward}</b>\n💳 Updated Balance: <b>₹{balance}</b>','{name} {reward} {balance}'),
}
BTN={'btn_verify_now':('Verify Now button','🔐 Verify Now #g'),'device_button':('Verify Device button',DEFAULTS['device_button'])}
SAMPLE={'balance':'125.00','referrals':'3','link':'https://t.me/yourbot?start=ref_123456','user_name':'Rahul','user_id':'123456789','user_wallet_balance':'125.00','user_referral_link':'https://t.me/yourbot?start=ref_123456','referral_earned':'30.00','history':'1. Rahul — ₹10.00','reward':'10','min':'50','need':'3','have':'1','id':'12','status':'Pending','name':'Rahul','username':' @rahul','rank':'5','medal':'🥇','wallet':'125.00','amount':'100.00','issue':'Invalid UPI ID','penalty':'2','amount':'100.00'}
COL={'r':('danger','🔴 Red'),'g':('success','🟢 Green'),'b':('primary','🔵 Blue'),'d':('default','⚪ Default')}
STNAME={'danger':'🔴 Red','success':'🟢 Green','primary':'🔵 Blue','default':'⚪ Default'}
TAG={'r':' #r','g':' #g','b':' #b','d':''}
def colrow(prefix):return [(COL[c][1],f'{prefix}:c:{c}') for c in 'rgbd']

class SD(dict):
    def __missing__(s,k):return '{'+k+'}'
def fmt(t,**kw):
    try:return t.format_map(SD(**kw))
    except Exception:return t

def parse_label(s):
    s=s.strip(); st='default'
    for k,v in STYLE.items():
        if s.lower().endswith(k): s=s[:-2].rstrip(); st=v; break
    return s or 'Button',st

async def db_init():
    global pool
    pool=await asyncpg.create_pool(DBURL,min_size=1,max_size=10)
    async with pool.acquire() as c:
        await c.execute(SCHEMA)
        await c.execute('UPDATE users SET device_verified=TRUE WHERE verified AND NOT COALESCE(device_verified,FALSE)')  # purane verified users grandfathered
        for k,v in DEFAULTS.items(): await c.execute('INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT DO NOTHING',k,v)
        for x in BUTTONS: await c.execute('INSERT INTO buttons(key,label,style,sort_order) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING',*x)
        await c.execute("UPDATE buttons SET enabled=FALSE WHERE key='verify'")
async def setting(k,d=''):
    async with pool.acquire() as c:
        r=await c.fetchval('SELECT value FROM settings WHERE key=$1',k); return r if r is not None else d
async def set_setting(k,v):
    async with pool.acquire() as c: await c.execute('INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value',k,v)
async def user(m,ref=None):
    u=m.from_user
    async with pool.acquire() as c:
        await c.execute('''INSERT INTO users(id,username,first_name,last_name) VALUES($1,$2,$3,$4) ON CONFLICT(id) DO UPDATE SET username=EXCLUDED.username,first_name=EXCLUDED.first_name,last_name=EXCLUDED.last_name,updated_at=NOW()''',u.id,u.username,u.first_name or '',u.last_name or '')
        if ref and ref!=u.id: await c.execute('UPDATE users SET referred_by=COALESCE(referred_by,$1) WHERE id=$2',ref,u.id)
async def getuser(uid):
    async with pool.acquire() as c:return await c.fetchrow('SELECT * FROM users WHERE id=$1',uid)
async def state(uid,s=None,data=None):
    async with pool.acquire() as c:
        if s is None:
            r=await c.fetchrow('SELECT state,data FROM states WHERE user_id=$1',uid); return r
        await c.execute('INSERT INTO states(user_id,state,data,updated_at) VALUES($1,$2,$3::jsonb,NOW()) ON CONFLICT(user_id) DO UPDATE SET state=EXCLUDED.state,data=EXCLUDED.data,updated_at=NOW()',uid,s,json.dumps(data or {}))
async def clear(uid):
    async with pool.acquire() as c: await c.execute('DELETE FROM states WHERE user_id=$1',uid)

async def has_join_request(chat_id,uid):
    async with pool.acquire() as c:return bool(await c.fetchval('SELECT 1 FROM join_requests WHERE chat_id=$1 AND user_id=$2',chat_id,uid))

async def required_ok(uid):
    async with pool.acquire() as c: rows=await c.fetch('SELECT * FROM required_channels WHERE enabled ORDER BY id')
    missing=[]
    for r in rows:
        try:
            m=await bot.get_chat_member(r['chat_id'],uid)
            if m.status in ('left','kicked') and not await has_join_request(r['chat_id'],uid): missing.append(r)
        except Exception:
            if not await has_join_request(r['chat_id'],uid): missing.append(r)
    return missing

async def channel_access_ok(uid):
    missing=await required_ok(uid)
    if missing:return False
    async with pool.acquire() as c: await c.execute('UPDATE users SET access_blocked=FALSE WHERE id=$1',uid)
    return True

async def verify_markup(rows):
    # Channel buttons: admin ne jo title diya wahi text (koi extra emoji nahi), ek line me 2.
    chs=[ibtn((r['title'] or 'Channel')[:64],(r['invite_style'] or 'primary'),r['invite_icon_custom_emoji_id'],url=r['invite_link']) for r in rows if r['invite_link']]
    b=InlineKeyboardBuilder()
    for i in range(0,len(chs),2):b.row(*chs[i:i+2])
    l,st=parse_label(await setting('btn_verify_now',BTN['btn_verify_now'][1]))
    b.row(btn(l,'verify_now',st,await setting('btn_verify_now_icon_custom_emoji_id','')));return b.as_markup()

async def required_join_kb(uid):
    rows=await required_ok(uid)
    return await verify_markup(rows)

async def verify(uid):
    async with pool.acquire() as c: await c.execute('UPDATE users SET verified=TRUE,updated_at=NOW() WHERE id=$1',uid)
async def txt(key,**kw):return fmt(await setting(key,TXT[key][1]),**kw)
async def jget(key,default):
    try:
        v=json.loads(await setting(key,'') or 'null');return v if v else default
    except Exception:return default
def url_kb(btns):
    rows=[[ibtn(x['t'],x.get('s','default'),x.get('icon_custom_emoji_id'),url=x['u'])] for x in btns]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
def media_of(m):
    if m.photo:return 'photo:'+m.photo[-1].file_id
    if m.video:return 'video:'+m.video.file_id
    if m.animation:return 'animation:'+m.animation.file_id
    if m.document:return 'document:'+m.document.file_id
    return ''
async def send_rich(chat_id,text,media='',markup=None):
    if media and ':' in media:
        kind,fid=media.split(':',1)
        fn,arg={'photo':(bot.send_photo,'photo'),'video':(bot.send_video,'video'),'animation':(bot.send_animation,'animation'),'document':(bot.send_document,'document')}[kind]
        if len(text or '')<=1024:return await fn(chat_id,**{arg:fid},caption=text or None,reply_markup=markup)
        await fn(chat_id,**{arg:fid})
    return await bot.send_message(chat_id,text,reply_markup=markup)
async def send_welcome(uid):
    return await send_rich(uid,await user_txt('welcome',uid),await setting('welcome_media',''),await menu())

async def credit_ref(uid):
    async with pool.acquire() as c:
        async with c.transaction():
            r=await c.fetchrow('SELECT referred_by,referral_rewarded,verified FROM users WHERE id=$1 FOR UPDATE',uid)
            if not r or not r['verified'] or not r['referred_by'] or r['referral_rewarded']: return
            reward=Decimal(await setting('referral_amount','10')); rid=r['referred_by']
            if rid==uid:return
            newbal=await c.fetchval('UPDATE users SET wallet=wallet+$1,referrals=referrals+1 WHERE id=$2 RETURNING wallet',reward,rid)
            await c.execute("INSERT INTO wallet_transactions(user_id,amount,balance_after,type,reason) VALUES($1,$2,$3,'referral_reward',$4)",rid,reward,newbal,f'Referral reward from user {uid}')
            await c.execute('INSERT INTO referrals(referrer_id,referred_id,reward) VALUES($1,$2,$3) ON CONFLICT DO NOTHING',rid,uid,reward)
            await c.execute('UPDATE users SET referral_rewarded=TRUE WHERE id=$1',uid)
            nm=await c.fetchrow('SELECT first_name,username FROM users WHERE id=$1',uid)
    name=html.escape((nm['first_name'] or nm['username'] or 'User') if nm else 'User')
    try:await bot.send_message(rid,await txt('ref_notify',name=name,reward=f'{reward:.2f}',balance=f'{Decimal(newbal):.2f}'))
    except Exception:log.warning('referral notify failed for %s',rid)

async def device_enabled():
    return bool(WEBAPP_URL) and (await setting('device_check','on'))=='on'
async def device_kb():
    label,st=parse_label(await setting('device_button',DEFAULTS['device_button']))
    return InlineKeyboardMarkup(inline_keyboard=[[ibtn(label,st,await setting('device_button_icon_custom_emoji_id',''),web_app=WebAppInfo(url=WEBAPP_URL+'/verify'))]])
async def after_channels(uid,send):
    """Channels join ho chuke hain. Device verification baaki ho to Step 2 dikhao, warna verify+referral credit."""
    u=await getuser(uid)
    if u and u['is_banned']:
        await send('🚫 Your account is banned. Please contact admin.');return False
    await channel_access_ok(uid)
    if u and not u['device_verified'] and await device_enabled():
        await send(await user_txt('device_text',uid),reply_markup=await device_kb());return False
    await verify(uid);await credit_ref(uid);return True

if 'icon_custom_emoji_id' not in KeyboardButton.model_fields:log.warning('aiogram purana hai: KeyboardButton me icon_custom_emoji_id field nahi. `pip install -U aiogram` karein.')
async def menu():
    async with pool.acquire() as c: rows=await c.fetch('SELECT * FROM buttons WHERE enabled ORDER BY sort_order,key')
    rows_kb=[]
    for i in range(0,len(rows),2):
        row=[]
        for r in rows[i:i+2]:
            style=(r['style'] or 'default').lower(); icon=r.get('icon_custom_emoji_id')
            # Bot API 9.4+: ReplyKeyboardButton supports both style and
            # icon_custom_emoji_id. Do NOT silently discard the icon here;
            # otherwise a Premium/custom-emoji configuration failure becomes
            # invisible and the user only sees a normal button.
            args={'text':r['label']}
            if style!='default': args['style']=style
            if icon: args['icon_custom_emoji_id']=str(icon)
            try:
                row.append(KeyboardButton(**args))
            except (TypeError,ValueError) as e:
                log.warning('KeyboardButton build failed key=%s style=%s icon=%s: %s',r['key'],style,icon,e)
                # Compatibility fallback only when the installed aiogram
                # package itself cannot construct the field. The stored icon
                # is preserved in DB and will work after upgrading aiogram.
                if icon:
                    base={'text':r['label']}
                    if style!='default': base['style']=style
                    try: row.append(KeyboardButton(**base))
                    except (TypeError,ValueError): row.append(KeyboardButton(text=r['label']))
                else:
                    row.append(KeyboardButton(text=r['label']))
        rows_kb.append(row)
    return ReplyKeyboardMarkup(keyboard=rows_kb,resize_keyboard=True,is_persistent=False,one_time_keyboard=False,selective=False)

async def user_button_by_text(text):
    if not text:return None
    async with pool.acquire() as c:
        return await c.fetchrow('SELECT * FROM buttons WHERE enabled AND label=$1 LIMIT 1',text.strip())

async def user_context(uid,**kw):
    u=await getuser(uid)
    if not u:return kw
    me=await bot.get_me();link=f'https://t.me/{me.username}?start=ref_{uid}' if me.username else f'tg://resolve?domain=bot&start=ref_{uid}'
    kw.setdefault('user_name',html.escape(u['first_name'] or u['username'] or 'User'));kw.setdefault('user_id',str(uid));kw.setdefault('user_wallet_balance',f'{Decimal(u["wallet"]):.2f}');kw.setdefault('balance',f'{Decimal(u["wallet"]):.2f}');kw.setdefault('referrals',str(u['referrals']));kw.setdefault('user_referral_link',link);kw.setdefault('link',link)
    return kw
async def format_user_template(template,uid,**kw):return fmt(template,**(await user_context(uid,**kw)))
async def user_txt(key,uid,**kw):return await format_user_template(await setting(key,TXT[key][1]),uid,**kw)

@dp.callback_query(F.data=='ref:history')
async def referral_history_cb(q:CallbackQuery):
    uid=q.from_user.id
    u=await getuser(uid)
    if not u:
        return await q.answer('Start the bot first.',show_alert=True)
    async with pool.acquire() as c:
        rows=await c.fetch("""SELECT r.created_at,r.reward,u.first_name,u.username,u.id
                             FROM referrals r JOIN users u ON u.id=r.referred_id
                             WHERE r.referrer_id=$1 ORDER BY r.created_at DESC LIMIT 50""",uid)
        earned=await c.fetchval('SELECT COALESCE(SUM(reward),0) FROM referrals WHERE referrer_id=$1',uid)
    if rows:
        lines=[]
        for i,x in enumerate(rows,1):
            nm=html.escape(x['first_name'] or (('@'+x['username']) if x['username'] else 'User'))
            lines.append(f'{i}. 👤 <b>{nm}</b> — ID <code>{x["id"]}</code>\n   💰 Reward: ₹{Decimal(x["reward"]):.2f} | {x["created_at"]:%d-%m-%Y %H:%M}')
        hist='\n\n'.join(lines)
    else:
        hist='No referrals yet.'
    text=await user_txt('referral_history',uid,referral_earned=f'{Decimal(earned):.2f}',history=hist)
    await q.answer()
    await q.message.answer(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn('🔗 My Referral Link','ui:referral','primary')]]))

async def handle_user_button(m:Message,r):
    uid=m.from_user.id;u=await getuser(uid)
    if not u:return await m.answer('Start the bot first.')
    if u['is_banned']:return await m.answer('🚫 Your account is banned. Please contact admin.')
    if not await channel_access_ok(uid):
        return await m.answer(await user_txt('penalty_text',uid,penalty=await setting('penalty_amount','2')),reply_markup=await required_join_kb(uid))
    if not u['verified'] or (await device_enabled() and not u['device_verified']):
        return await m.answer(await user_txt('verify_required',uid),reply_markup=await verify_kb())
    k=r['key']
    if k=='wallet':
        return await m.answer(await user_txt('wallet',uid))
    if k=='referral':
        return await m.answer(await user_txt('referral',uid,reward=await setting('referral_amount','10')),reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn('📊 Track Referrals','ref:history','primary')]]))
    if k=='leaderboard':return await leaderboard(m,uid)
    if k=='withdraw':return await withdraw_start(m,uid)
    if k=='verify':return await m.answer(await user_txt('verify_required',uid),reply_markup=await verify_kb())
    return await m.answer('❌ This option is not configured.')
async def verify_kb():
    async with pool.acquire() as c: rows=await c.fetch('SELECT * FROM required_channels WHERE enabled ORDER BY id')
    return await verify_markup(rows)

@dp.message(CommandStart())
async def start(m:Message):
    arg=(m.text.split(maxsplit=1)[1] if m.text and len(m.text.split(maxsplit=1))>1 else '')
    ref=int(arg[4:]) if arg.startswith('ref_') and arg[4:].isdigit() else None
    await user(m,ref)
    if not await channel_access_ok(m.from_user.id):
        await m.answer(await user_txt('verify_required',m.from_user.id),reply_markup=await verify_kb());return
    if not await after_channels(m.from_user.id,m.answer):return
    await send_welcome(m.from_user.id)

@dp.callback_query(F.data=='verify_now')
async def verify_now(q:CallbackQuery):
    if not await channel_access_ok(q.from_user.id): await q.answer('❌ Pehle sabhi required channels join karein.',show_alert=True); await q.message.answer(await user_txt('verify_required',q.from_user.id),reply_markup=await required_join_kb(q.from_user.id)); return
    if not await after_channels(q.from_user.id,q.message.answer):await q.answer('✅ Channels verified! Ab device verify karein.');return
    await q.answer('✅ Verified!',show_alert=True);await q.message.answer(await user_txt('verify_ok',q.from_user.id),reply_markup=await menu())

@dp.callback_query(F.data.startswith('ui:'))
async def ui(q:CallbackQuery):
    uid=q.from_user.id;r=await getuser(uid)
    if not r: await q.answer('Start the bot first.',show_alert=True);return
    if not await channel_access_ok(uid): await q.answer('❌ Access blocked: required channel join karein.',show_alert=True); await q.message.answer(await user_txt('penalty_text',uid,penalty=await setting('penalty_amount','2')),reply_markup=await required_join_kb(uid)); return
    if not r['verified'] or (await device_enabled() and not r['device_verified']): await q.answer('Verify first.',show_alert=True);await q.message.answer(await user_txt('verify_required',uid),reply_markup=await verify_kb());return
    k=q.data[3:];await q.answer()
    if k=='wallet':await q.message.answer(await user_txt('wallet',uid))
    elif k=='referral':
        await q.message.answer(await user_txt('referral',uid,reward=await setting('referral_amount','10')),reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn('📊 Track Referrals','ref:history','primary')]]))
    elif k=='leaderboard':await leaderboard(q.message,uid)
    elif k=='verify':await q.message.answer(await user_txt('verify_required',uid),reply_markup=await verify_kb())
    elif k=='withdraw':await withdraw_start(q.message,uid)

async def leaderboard(m,uid=None):
    async with pool.acquire() as c:
        r=await c.fetch('SELECT first_name,username,referrals,wallet FROM users WHERE verified ORDER BY referrals DESC,wallet DESC,id LIMIT 10')
        me=await c.fetchrow('SELECT u.verified,u.referrals,(SELECT COUNT(*)+1 FROM users x WHERE x.verified AND (x.referrals>u.referrals OR (x.referrals=u.referrals AND x.wallet>u.wallet))) AS rank FROM users u WHERE u.id=$1',uid) if uid else None
    if not r:return await m.answer(await user_txt('lb_empty',uid))
    row=await setting('lb_row',TXT['lb_row'][1]);out=[await user_txt('lb_title',uid)];medals=['🥇','🥈','🥉']
    for i,x in enumerate(r):
        name=html.escape(x['first_name'] or x['username'] or 'User');un=(' @'+html.escape(x['username'])) if x['username'] else ''
        out.append(await format_user_template(row,x['id'],medal=medals[i] if i<3 else f'{i+1}.',rank=str(i+1),name=name,username=un,referrals=str(x['referrals']),wallet=f'{Decimal(x["wallet"]):.2f}'))
    if me and me['verified']:out.append('\n'+await user_txt('lb_you',uid,rank=str(me['rank']),referrals=str(me['referrals'])))
    await m.answer('\n'.join(out))

async def withdraw_start(m,uid):
    r=await getuser(uid);mn=Decimal(await setting('min_withdrawal','50'));mr=int(await setting('min_referrals','0'))
    if Decimal(r['wallet'])<mn:return await m.answer(await user_txt('wd_min',uid,min=f'{mn:.2f}',balance=f'{Decimal(r["wallet"]):.2f}'))
    if r['referrals']<mr:return await m.answer(await user_txt('wd_refs',uid,need=str(mr),have=str(r['referrals'])))
    await state(uid,'w_amount');await m.answer(await user_txt('wd_amount',uid,min=f'{mn:.2f}'))

async def create_withdraw(uid,amount,upi,attachment):
    async with pool.acquire() as c:
        async with c.transaction():
            r=await c.fetchrow('SELECT wallet,is_banned FROM users WHERE id=$1 FOR UPDATE',uid)
            if not r: raise ValueError('User not found')
            if r['is_banned']: raise ValueError('Your account is banned.')
            mn=Decimal(await setting('min_withdrawal','50'))
            if amount<mn: raise ValueError(f'Minimum withdrawal is ₹{mn:.2f}')
            if amount>Decimal(r['wallet']): raise ValueError('Insufficient wallet balance')
            newbal=Decimal(r['wallet'])-amount
            await c.execute('UPDATE users SET wallet=$1,updated_at=NOW() WHERE id=$2',newbal,uid)
            wid=await c.fetchval('INSERT INTO withdrawals(user_id,amount,upi_id,attachment_file_id) VALUES($1,$2,$3,$4) RETURNING id',uid,amount,upi,attachment)
            await c.execute("INSERT INTO wallet_transactions(user_id,amount,balance_after,type,reason) VALUES($1,$2,$3,'withdrawal',$4)",uid,-amount,newbal,f'Withdrawal #{wid} submitted')
            return wid

async def withdrawal_channel_message(wid,status='PENDING',issue=''):
    async with pool.acquire() as c:r=await c.fetchrow('SELECT w.*,u.first_name,u.username FROM withdrawals w JOIN users u ON u.id=w.user_id WHERE w.id=$1',wid)
    if not r:return None
    name=html.escape(r['first_name'] or 'User'); un=('@'+html.escape(r['username'])) if r['username'] else 'No username'
    t=(f'🚨 <b>WITHDRAWAL REQUEST</b>\n\n🆔 Request: <code>#{wid}</code>\n👤 User: <b>{name}</b> ({un})\n'
       f'🆔 User ID: <code>{r["user_id"]}</code>\n💰 Withdrawal Amount: <b>₹{Decimal(r["amount"]):.2f}</b>\n'
       f'💳 UPI ID: <code>{html.escape(r["upi_id"] or "-")}</code>\n')
    if status=='PAID': t+='\n🟢 <b>PAYMENT DONE</b>'
    elif status=='REJECTED': t+=f'\n🔴 <b>REJECTED</b>\n📝 Issue: {html.escape(issue or r["issue"] or "Not specified")}'
    else:t+='\n🟡 <b>PENDING PAYMENT</b>'
    return t

async def post_withdraw(wid):
    ch=await setting('withdrawal_channel_id','')
    if not ch:return False
    ch_id=int(ch) if ch.lstrip('-').isdigit() else ch
    t=await withdrawal_channel_message(wid,'PENDING')
    actions=InlineKeyboardMarkup(inline_keyboard=[[btn('✅ Payment Done',f'wd:paid:{wid}','success'),btn('❌ Reject with Issue',f'wd:reject:{wid}','danger')]])
    try:
        msg=await bot.send_message(ch_id,t,reply_markup=actions)
        async with pool.acquire() as c: await c.execute('UPDATE withdrawals SET channel_message_id=$1,channel_id=$2 WHERE id=$3',msg.message_id,ch_id,wid)
        async with pool.acquire() as c:r=await c.fetchrow('SELECT attachment_file_id FROM withdrawals WHERE id=$1',wid)
        if r and r['attachment_file_id']:await bot.send_document(ch_id,r['attachment_file_id'],caption=f'📎 Proof/Attachment for withdrawal #{wid}')
        return True
    except Exception as e:log.error('withdraw channel: %s',e);return False

async def update_withdrawal_channel(wid,status,issue=''):
    async with pool.acquire() as c:r=await c.fetchrow('SELECT channel_id,channel_message_id FROM withdrawals WHERE id=$1',wid)
    if not r or not r['channel_id'] or not r['channel_message_id']:return
    try:await bot.edit_message_text(await withdrawal_channel_message(wid,status,issue),chat_id=r['channel_id'],message_id=int(r['channel_message_id']),reply_markup=None)
    except Exception as e:log.warning('withdrawal channel update failed: %s',e)

@dp.message(Command('withdraw'))
async def wd_cmd(m:Message):
    await user(m);r=await getuser(m.from_user.id)
    if r and r['is_banned']:return await m.answer('🚫 Your account is banned. Please contact admin.')
    if not r['verified']:return await m.answer(await user_txt('verify_required',uid),reply_markup=await verify_kb())
    await withdraw_start(m,m.from_user.id)
@dp.message(Command('leaderboard'))
async def lb_cmd(m:Message):await user(m);await leaderboard(m,m.from_user.id)
@dp.message(Command('cancel'))
async def cancel(m:Message):await clear(m.from_user.id);await m.answer('✅ Cancelled.',reply_markup=await menu())

# ---------------- ADMIN (full button panel) ----------------
def admin(uid):return uid in ADMINS
BC_RUNNING=False
async def panel(t,text,markup=None):
    if isinstance(t,CallbackQuery):
        try:return await t.message.edit_text(text,reply_markup=markup)
        except TelegramBadRequest as e:
            if 'not modified' in str(e).lower():return None
        return await t.message.answer(text,reply_markup=markup)
    return await t.answer(text,reply_markup=markup)

@dp.message(Command('admin'))
async def admin_cmd(m:Message):
    if not admin(m.from_user.id):return await m.answer('⛔ Access denied.')
    await user(m);await clear(m.from_user.id);await admin_home(m)
async def admin_home(t):
    await panel(t,'🛠 <b>ADMIN PANEL</b>',kb([[('🚀 Start Settings','ad:st'),('🎛 Menu Buttons','ad:mb')],[('📝 Texts','ad:tx'),('🔘 Other Buttons','ad:bt')],[('📢 Channels','ad:ch'),('💸 Withdrawals','ad:wds')],[('👤 Users','ad:users'),('👥 Stats','ad:stats')],[('🏆 Leaderboard','ad:lb'),('🛡 Device Verify','ad:dv')],[('📣 Broadcast','ad:bc'),('⚙️ Settings','ad:set')]]))

@dp.callback_query(F.data=='ad:confirm')
async def admin_confirm(q:CallbackQuery):
    if not admin(q.from_user.id):return await q.answer('Denied',show_alert=True)
    st=await state(q.from_user.id)
    if not st or st['state']!='a_confirm':return await q.answer('No pending preview.',show_alert=True)
    d=jd(st['data']);kind=d['kind'];p=d['payload']
    try:
        if kind=='rich':
            v=p['value'];sc=p['key'].replace('_rich','');await put_rich(sc,media=v.get('media',''),text=v.get('text',''),btns=v.get('btns',[]),pin=v.get('pin',False))
        else:await apply_draft(q.from_user.id,d)
        await clear(q.from_user.id);await q.answer('Saved ✅',show_alert=True);await q.message.answer('✅ <b>Saved successfully.</b>')
        if kind=='text':await tx_route(q,[p['key']])
        elif kind=='button':await mb_one(q,p['key'])
        elif kind=='other_button':await bt_route(q,[p['key']])
        elif kind=='channel':await ch_screen(q)
        elif kind=='setting':await set_screen(q)
        elif kind=='rich':await rich_screen(q,p['key'].replace('_rich',''))
        elif kind in ('user_ban','wallet_adjust'):await show_user(q,p['user_id'])
    except Exception as e:log.exception('draft save failed');await q.message.answer('❌ Save failed: '+html.escape(str(e)[:200]))

@dp.callback_query(F.data=='ad:cancel')
async def admin_cancel(q:CallbackQuery):
    if not admin(q.from_user.id):return await q.answer('Denied',show_alert=True)
    await clear(q.from_user.id);await q.answer('Cancelled');await q.message.answer('❌ Changes cancelled. Purani setting unchanged.')

@dp.callback_query(F.data.startswith('ad:'))
async def ad(q:CallbackQuery):
    if not admin(q.from_user.id):return await q.answer('Denied',show_alert=True)
    p=q.data.split(':')[1:];a=p[0];uid=q.from_user.id;res=None
    try:
        if a=='home':await clear(uid);await admin_home(q)
        elif a in('st','bc'):
            if len(p)==1:await clear(uid);await rich_screen(q,a)
            else:res=await rich_action(q,a,p[1],p[2:])
        elif a=='mb':res=await mb_route(q,p[1:])
        elif a=='tx':res=await tx_route(q,p[1:])
        elif a=='bt':res=await bt_route(q,p[1:])
        elif a=='ch':res=await ch_route(q,p[1:])
        elif a=='wds':await wds_screen(q)
        elif a=='users':res=await users_route(q,p[1:])
        elif a=='stats':await stats_screen(q)
        elif a=='lb':await leaderboard(q.message,None)
        elif a=='dv':res=await dv_route(q,p[1:])
        elif a=='set':res=await set_route(q,p[1:])
    except Exception as e:
        log.exception('admin callback');res='⚠️ Error: '+str(e)[:150]
    await q.answer(res if isinstance(res,str) else None,show_alert=isinstance(res,str))

# ---- Start Settings / Broadcast (Media + Text + Buttons, each with See) ----
async def get_rich(sc):
    if sc=='st':return {'media':await setting('welcome_media',''),'text':await setting('welcome',DEFAULTS['welcome']),'btns':await jget('welcome_buttons',[]),'pin':False}
    d=await jget('bc_draft',{});return {'media':d.get('media',''),'text':d.get('text',''),'btns':d.get('btns',[]),'pin':bool(d.get('pin',False))}
async def put_rich(sc,**kw):
    if sc=='st':
        if 'media' in kw:await set_setting('welcome_media',kw['media'])
        if 'text' in kw:await set_setting('welcome',kw['text'])
        if 'btns' in kw:await set_setting('welcome_buttons',json.dumps(kw['btns']))
    else:
        d=await get_rich('bc');d.update(kw);await set_setting('bc_draft',json.dumps(d))
async def rich_screen(t,sc):
    d=await get_rich(sc)
    rows=[[('🖼 Media'+(' ✅' if d['media'] else ''),f'ad:{sc}:media'),('👀 See',f'ad:{sc}:mediasee')],
          [('abc Text'+(' ✅' if d['text'] else ''),f'ad:{sc}:text'),('👀 See',f'ad:{sc}:textsee')],
          [(f'🔘 Buttons ({len(d["btns"])})',f'ad:{sc}:btns'),('👀 See',f'ad:{sc}:btnsee')]]
    if sc=='bc':rows.append([('📌 Pin',f'ad:bc:pin'),('✅ YES' if d['pin'] else '❌ NO','ad:bc:pin')])
    rows.append([('👀 Full preview',f'ad:{sc}:full')])
    if sc=='bc':rows.append([('🚀 Send Broadcast','ad:bc:send','success')])
    rows.append([('🗑 Remove Media',f'ad:{sc}:mediadel'),('⬅ Back','ad:home')])
    title='🚀 <b>Start Settings</b>\n\nYe /start par user ko dikhne wala welcome message hai.' if sc=='st' else '📣 <b>Broadcast Settings</b>\n\nVerified users ko bhejne ka message tayyar karein.'
    await panel(t,title+'\n\nMedia, Text aur Buttons set karein. 👀 See se preview dekhein.',kb(rows))
async def rich_btns(t,sc):
    d=await get_rich(sc);rows=[];lines=['🔘 <b>Buttons</b>\n']
    for i,x in enumerate(d['btns']):
        lines.append(f'{i+1}. {html.escape(x["t"])} → {html.escape(x["u"])} [{STNAME.get(x.get("s","default"))}]')
        rows.append([(f'🗑 {i+1}. {x["t"][:22]}',f'ad:{sc}:bdel:{i}','danger')])
    if not d['btns']:lines.append('Abhi koi button nahi hai.')
    rows.append([('➕ Add Button',f'ad:{sc}:badd','success')]);rows.append([('⬅ Back',f'ad:{sc}')])
    await panel(t,'\n'.join(lines),kb(rows))
async def rich_action(q,sc,act,rest):
    uid=q.from_user.id;d=await get_rich(sc)
    if act=='media':
        await state(uid,'a_media',{'sc':sc});await q.message.answer('🖼 Photo / video / GIF / document bhejein.');return
    if act=='mediasee':
        if not d['media']:return 'Media set nahi hai.'
        await send_rich(uid,'🖼 Media preview',d['media']);return
    if act=='mediadel':
        nd=dict(d);nd['media']='';return await draft(uid,'rich',{'key':sc+'_rich','value':nd},'🗑 Media remove preview')
    if act=='text':
        await state(uid,'a_rtext',{'sc':sc});await q.message.answer('abc Naya text bhejein (bold/italic/link formatting chalegi).');return
    if act=='textsee':
        if not d['text']:return 'Text set nahi hai.'
        await q.message.answer(d['text']);return
    if act=='btns':return await rich_btns(q,sc)
    if act=='btnsee':
        if not d['btns']:return 'Koi button set nahi hai.'
        await q.message.answer('🔘 Buttons preview',reply_markup=url_kb(d['btns']));return
    if act=='badd':
        await state(uid,'a_rbtn',{'sc':sc});await q.message.answer('➕ Button bhejein (ek line me ek button):\n\n<code>✨ PremiumEmoji Button Text | https://link.com #g</code>\n\nColor: #r red, #g green, #b blue, #d default');return
    if act=='bdel':
        i=int(rest[0]);nd=dict(d)
        if 0<=i<len(nd['btns']):nd['btns'].pop(i)
        return await draft(uid,'rich',{'key':sc+'_rich','value':nd},'🗑 Button delete preview')
    if act=='full':
        if not d['text'] and not d['media']:return 'Text ya media set karein.'
        await send_rich(uid,d['text'] or '',d['media'],await menu() if sc=='st' else url_kb(d['btns']));return
    if act=='pin' and sc=='bc':
        nd=dict(d);nd['pin']=not d['pin'];return await draft(uid,'rich',{'key':'bc_rich','value':nd},'📌 Pin: '+('YES' if nd['pin'] else 'NO'))
    if act=='send' and sc=='bc':
        if not d['text'] and not d['media']:return 'Pehle text ya media set karein.'
        async with pool.acquire() as c:n=await c.fetchval('SELECT COUNT(*) FROM users WHERE verified')
        return await panel(q,f'📣 <b>Confirm</b>\n\n<b>{n}</b> verified users ko broadcast bheja jayega.'+('\n📌 Pin: YES' if d['pin'] else ''),kb([[('✅ Yes, Send','ad:bc:go','success'),('❌ Cancel','ad:bc','danger')]]))
    if act=='go' and sc=='bc':
        global BC_RUNNING
        if BC_RUNNING:return 'Broadcast pehle se chal raha hai.'
        BC_RUNNING=True;asyncio.create_task(do_broadcast(uid));return await panel(q,'📣 Broadcast shuru ho gaya. Poora hone par result bheja jayega.',kb([[('⬅ Back','ad:bc')]]))
async def do_broadcast(admin_id):
    global BC_RUNNING
    try:
        d=await get_rich('bc')
        async with pool.acquire() as c:ids=await c.fetch('SELECT id FROM users WHERE verified')
        mk=url_kb(d['btns']);sent=fail=0
        for x in ids:
            for _ in range(2):
                try:
                    msg=await send_rich(x['id'],await format_user_template(d['text'] or '',x['id']),d['media'],mk);sent+=1
                    if d['pin']:
                        try:await bot.pin_chat_message(x['id'],msg.message_id,disable_notification=True)
                        except Exception:pass
                    break
                except TelegramRetryAfter as e:await asyncio.sleep(e.retry_after)
                except Exception:fail+=1;break
            await asyncio.sleep(.05)
        await bot.send_message(admin_id,f'📣 Broadcast done.\nSent: {sent}\nFailed: {fail}')
    except Exception:log.exception('broadcast failed')
    finally:BC_RUNNING=False

# ---- Menu buttons (user ke main buttons) ----
async def mb_list(t):
    async with pool.acquire() as c:r=await c.fetch('SELECT * FROM buttons ORDER BY sort_order,key')
    rows=[[(('🟢 ' if x['enabled'] else '🔴 ')+x['label'][:30],f'ad:mb:{x["key"]}',x['style'])] for x in r]+[[('⬅ Back','ad:home')]]
    await panel(t,'🎛 <b>Menu Buttons</b>\n\nUser ke main menu ke buttons. Kisi par tap karke naam, color, ON/OFF aur position badlein.',kb(rows))
async def mb_one(t,k):
    async with pool.acquire() as c:x=await c.fetchrow('SELECT * FROM buttons WHERE key=$1',k)
    if not x:return await mb_list(t)
    await panel(t,f'🎛 <b>{html.escape(x["label"])}</b>\n\nKey: <code>{k}</code>\nColor: {STNAME.get(x["style"],x["style"])}\nStatus: {"ON 🟢" if x["enabled"] else "OFF 🔴"}',kb([[(x['label'],'ad:noop',x['style'])],[('✏️ Rename',f'ad:mb:{k}:ren'),('👁 ON/OFF',f'ad:mb:{k}:tog')],colrow(f'ad:mb:{k}'),[('⬆ Up',f'ad:mb:{k}:up'),('⬇ Down',f'ad:mb:{k}:dn')],[('⬅ Back','ad:mb')]]))
async def mb_route(q,p):
    if not p:return await mb_list(q)
    k=p[0]
    if len(p)==1:return await mb_one(q,k)
    async with pool.acquire() as c:x=await c.fetchrow('SELECT * FROM buttons WHERE key=$1',k)
    if not x:return await mb_list(q)
    act=p[1]
    if act=='ren':await state(q.from_user.id,'a_mbren',{'key':k});await q.message.answer('✏️ Naya button naam bhejein. Color ke liye end me #r #g #b #d likhein.');return
    label=x['label'];sty=x['style'];enabled=x['enabled'];order=x['sort_order']
    if act=='tog':enabled=not enabled
    elif act=='c':sty=COL[p[2]][0]
    elif act in ('up','dn'):
        async with pool.acquire() as c:ks=[z['key'] for z in await c.fetch('SELECT key FROM buttons ORDER BY sort_order,key')]
        if k in ks:
            i=ks.index(k);j=i-1 if act=='up' else i+1
            if 0<=j<len(ks):
                other=ks[j]
                async with pool.acquire() as c:a=await c.fetchrow('SELECT sort_order FROM buttons WHERE key=$1',k);b=await c.fetchrow('SELECT sort_order FROM buttons WHERE key=$1',other)
                return await draft(q.from_user.id,'channel_action',{'sql':'UPDATE buttons SET sort_order=CASE WHEN key=$1 THEN $2 WHEN key=$3 THEN $4 ELSE sort_order END WHERE key IN ($1,$3)','args':[k,b['sort_order'],other,a['sort_order']]},f'Position change: <b>{html.escape(label)}</b> {i+1} → {j+1}')
    return await draft(q.from_user.id,'button',{'key':k,'label':label,'style':sty,'enabled':enabled,'sort_order':order},f'Button: <b>{html.escape(label)}</b>\nColor: {STNAME[sty]}\nStatus: {"ON" if enabled else "OFF"}')

# ---- Texts ----
async def tx_route(q,p):
    uid=q.from_user.id
    if not p:
        return await panel(q,'📝 <b>Texts</b>\n\nUser ko dikhne wale sabhi messages yahan se badal sakte hain.',kb([[(v[0],f'ad:tx:{k}')] for k,v in TXT.items()]+[[('⬅ Back','ad:home')]]))
    k=p[0]
    if k not in TXT:return 'Unknown text.'
    if len(p)>1:
        act=p[1]
        if act=='ed':
            await state(uid,'a_tx',{'key':k});await q.message.answer(f'✏️ Naya text bhejein (HTML formatting chalegi).\nPlaceholders: {TXT[k][2] or "none"}');return
        if act=='see':
            try:await q.message.answer(await txt(k,**SAMPLE))
            except TelegramBadRequest as e:return 'HTML error: '+str(e)[:150]
            return
        if act=='rs':return await draft(uid,'text',{'key':k,'value':TXT[k][1]},'♻️ <b>Reset Preview</b>\n\n'+fmt(TXT[k][1],**SAMPLE))
    cur=(await setting(k,TXT[k][1]))[:900]
    await panel(q,f'📝 <b>{TXT[k][0]}</b>\n\nPlaceholders: {TXT[k][2] or "none"}\n\n<b>Current:</b>\n<code>{html.escape(cur)}</code>',kb([[('✏️ Edit',f'ad:tx:{k}:ed'),('👀 See',f'ad:tx:{k}:see')],[('♻️ Reset Default',f'ad:tx:{k}:rs'),('⬅ Back','ad:tx')]]))

# ---- Other buttons (Verify Now / Verify Device) ----
async def bt_route(q,p):
    uid=q.from_user.id
    if not p:
        return await panel(q,'🔘 <b>Other Buttons</b>\n\nVerify Now / Verify Device buttons ka naam aur color badlein.',kb([[(v[0],f'ad:bt:{k}')] for k,v in BTN.items()]+[[('⬅ Back','ad:home')]]))
    k=p[0]
    if k not in BTN:return 'Unknown button.'
    if len(p)>1:
        act=p[1]
        if act=='ed':
            await state(uid,'a_bt',{'key':k});await q.message.answer('✏️ Naya button naam bhejein.\nColor ke liye end me #r #g #b #d likhein.');return
        if act=='c':
            label,_=parse_label(await setting(k,BTN[k][1]));return await draft(uid,'other_button',{'key':k,'value':label+TAG[p[2]]},f'Button: <b>{html.escape(label)}</b>\nColor: {COL[p[2]][1]}')
        elif act=='rs':return await draft(uid,'other_button',{'key':k,'value':BTN[k][1]},f'♻️ Reset: <b>{html.escape(BTN[k][1])}</b>')
    label,st=parse_label(await setting(k,BTN[k][1]))
    await panel(q,f'🔘 <b>{BTN[k][0]}</b>\n\nLabel: {html.escape(label)}\nColor: {STNAME.get(st,st)}',kb([[(label,'ad:noop',st)],[('✏️ Rename',f'ad:bt:{k}:ed'),('♻️ Reset',f'ad:bt:{k}:rs')],colrow(f'ad:bt:{k}'),[('⬅ Back','ad:bt')]]))

# ---- Channels ----
async def ch_screen(t):
    async with pool.acquire() as c:r=await c.fetch('SELECT * FROM required_channels ORDER BY id')
    lines=['📢 <b>Required Channels</b>\n'];rows=[]
    for x in r:
        link=x['invite_link'] or 'No link'
        style=STNAME.get((x['invite_style'] or 'primary'),(x['invite_style'] or 'primary'))
        icon='YES' if x['invite_icon_custom_emoji_id'] else 'NO'
        lines.append(f'{x["id"]}. {html.escape(x["title"] or "")} <code>{x["chat_id"]}</code>\n    {"🟢 ON" if x["enabled"] else "🔴 OFF"} | Join request: {"YES" if x["request_join"] else "NO"}\n    🔗 {html.escape(link)} | Link color: {style} | Premium icon: {icon}')
        rows.append([(f'{"🟢" if x["enabled"] else "🔴"} #{x["id"]}',f'ad:ch:on:{x["id"]}'),(f'🙋 Request {"✅" if x["request_join"] else "❌"} #{x["id"]}',f'ad:ch:rj:{x["id"]}')])
        rows.append([('🎨 Link Style',f'ad:ch:style:{x["id"]}'),('✨ Link Emoji',f'ad:ch:emoji:{x["id"]}'),('🗑 Delete',f'ad:ch:del:{x["id"]}','danger')])
    if not r:lines.append('Koi channel nahi hai.')
    rows.append([('➕ Add Channel','ad:ch:add','success')]);rows.append([('⬅ Back','ad:home')])
    await panel(t,'\n'.join(lines),kb(rows))

async def ch_route(q,p):
    if p:
        act=p[0]
        if act=='add':
            await state(q.from_user.id,'a_ch');await q.message.answer('➕ Bhejein:\n<code>@channel | Title | InviteLink</code>\n\nPublic channel ho to InviteLink optional hai; bot uska public t.me link nikaal lega. Private channel ke liye invite link dein. Bot ko channel me admin hona chahiye.');return
        if len(p)<2:return await ch_screen(q)
        i=int(p[1])
        async with pool.acquire() as c:x=await c.fetchrow('SELECT * FROM required_channels WHERE id=$1',i)
        if not x:return await ch_screen(q)
        if act=='del':return await draft(q.from_user.id,'channel_action',{'sql':'DELETE FROM required_channels WHERE id=$1','args':[i]},f'🗑 Delete <b>{html.escape(x["title"] or "Channel")}</b>?')
        if act=='on':return await draft(q.from_user.id,'channel_action',{'sql':'UPDATE required_channels SET enabled=NOT enabled WHERE id=$1','args':[i]},f'Change status to <b>{"OFF" if x["enabled"] else "ON"}</b>?')
        if act=='rj':return await draft(q.from_user.id,'channel_action',{'sql':'UPDATE required_channels SET request_join=NOT request_join WHERE id=$1','args':[i]},f'Change request join for <b>{html.escape(x["title"] or "Channel")}</b>?')
        if act=='style':
            rows=[[(COL[c][1],f'ad:ch:stylec:{i}:{c}') for c in 'rgbd'],[('⬅ Back','ad:ch')]]
            return await panel(q,f'🎨 <b>Channel Link Color</b>\n\n{html.escape(x["title"] or "Channel")}\nCurrent: {STNAME.get(x.get("invite_style") or "primary")}',kb(rows))
        if act=='stylec':
            c=p[2];return await draft(q.from_user.id,'channel_action',{'sql':'UPDATE required_channels SET invite_style=$1 WHERE id=$2','args':[COL[c][0],i]},f'🎨 Set link color to <b>{COL[c][1]}</b>?')
        if act=='emoji':
            await state(q.from_user.id,'a_chemoji',{'id':i});await q.message.answer('✨ Ab ek <b>Premium Custom Emoji</b> bhejein.\n\nEmoji ko Telegram ke custom emoji picker se bhejo. Main uska emoji ID channel ke link button me attach kar dunga.');return
    await ch_screen(q)

# ---- User management ----
async def users_screen(t,notice=''):
    body='👤 <b>User Management</b>\n\nSearch by Telegram ID, @username or name.\nWallet changes, ban/unban and complete history are available per user.'
    if notice:body+='\n\n'+notice
    await panel(t,body,kb([[('🔎 Search User','ad:users:search')],[('💳 Wallet History','ad:users:txsearch')],[('⬅ Back','ad:home')]]))

async def show_user(q,uid):
    async with pool.acquire() as c:
        u=await c.fetchrow('SELECT * FROM users WHERE id=$1',uid)
        if not u:return '❌ User not found.'
        tx=await c.fetchval('SELECT COUNT(*) FROM wallet_transactions WHERE user_id=$1',uid); wd=await c.fetchval('SELECT COUNT(*) FROM withdrawals WHERE user_id=$1',uid)
    name=html.escape(u['first_name'] or 'User');un='@'+html.escape(u['username']) if u['username'] else 'No username'
    text=(f'👤 <b>User</b>\n\n🆔 ID: <code>{uid}</code>\n👤 Name: <b>{name}</b>\n🔗 Username: {un}\n'
          f'💰 Wallet: <b>₹{Decimal(u["wallet"]):.2f}</b>\n👥 Referrals: <b>{u["referrals"]}</b>\n'
          f'✅ Verified: {"YES" if u["verified"] else "NO"}\n🛡 Device: {"YES" if u["device_verified"] else "NO"}\n🚫 Banned: {"YES" if u["is_banned"] else "NO"}\n'
          f'💸 Withdrawals: {wd} | 💳 Wallet transactions: {tx}')
    rows=[[('💳 Wallet History',f'ad:users:history:{uid}'),('💸 Withdrawals',f'ad:users:wds:{uid}')],
          [('➕ Credit Wallet',f'ad:users:credit:{uid}','success'),('➖ Debit Wallet',f'ad:users:debit:{uid}','danger')],
          [('🚫 Ban User' if not u['is_banned'] else '🟢 Unban User',f'ad:users:ban:{uid}')],[('⬅ Users','ad:users')]]
    await panel(q,text,kb(rows));return None

async def wallet_history(q,uid):
    async with pool.acquire() as c:
        u=await c.fetchrow('SELECT first_name,wallet FROM users WHERE id=$1',uid); rows=await c.fetch('SELECT * FROM wallet_transactions WHERE user_id=$1 ORDER BY id DESC LIMIT 50',uid)
    if not u:return 'User not found.'
    lines=[f'💳 <b>Wallet History — {html.escape(u["first_name"] or "User")}</b>\nBalance: ₹{Decimal(u["wallet"]):.2f}\n']
    for x in rows:
        sign='+' if Decimal(x['amount'])>=0 else ''
        lines.append(f'{x["created_at"]:%d-%m %H:%M} — <b>{sign}₹{Decimal(x["amount"]):.2f}</b> — {html.escape(x["type"])}\n{html.escape(x["reason"] or "")}')
    if not rows:lines.append('No wallet history.')
    await panel(q,'\n'.join(lines),kb([[('⬅ User','ad:users:view:'+str(uid))]]))

async def user_withdrawals(q,uid):
    async with pool.acquire() as c:rows=await c.fetch('SELECT * FROM withdrawals WHERE user_id=$1 ORDER BY id DESC LIMIT 30',uid)
    lines=[f'💸 <b>Withdrawal History — User {uid}</b>']
    for x in rows:lines.append(f'#{x["id"]} ₹{Decimal(x["amount"]):.2f} — <b>{x["status"].upper()}</b> — UPI: <code>{html.escape(x["upi_id"] or "-")}</code>\n{html.escape(x["issue"] or "")}')
    if len(lines)==1:lines.append('No withdrawals.')
    await panel(q,'\n\n'.join(lines),kb([[('⬅ User','ad:users:view:'+str(uid))]]))

async def users_route(q,p):
    uid=q.from_user.id
    if not p:return await users_screen(q)
    act=p[0]
    if act in ('search','txsearch'):
        await state(uid,'a_user_search',{'mode':act});await q.message.answer('🔎 User ID, @username, username ya name bhejein.');return
    if act=='view':return await show_user(q,int(p[1]))
    if act=='history':return await wallet_history(q,int(p[1]))
    if act=='wds':return await user_withdrawals(q,int(p[1]))
    if act in ('credit','debit'):
        await state(uid,'a_wallet_adj',{'user_id':int(p[1]),'mode':act});await q.message.answer(('➕ Credit' if act=='credit' else '➖ Debit')+' ke liye amount aur reason bhejein:\n<code>100 | Bonus</code>');return
    if act=='ban':
        x=await getuser(int(p[1]))
        if not x:return 'User not found.'
        new=not x['is_banned'];return await draft(uid,'user_ban',{'user_id':int(p[1]),'banned':new},('🚫 BAN' if new else '🟢 UNBAN')+f' User <code>{p[1]}</code>?')
    return 'Unknown user action.'

# ---- Withdrawals / Stats ----
async def wds_screen(t):
    async with pool.acquire() as c:r=await c.fetch('SELECT w.id,w.amount,w.status,w.upi_id,u.first_name,u.username FROM withdrawals w JOIN users u ON u.id=w.user_id ORDER BY w.id DESC LIMIT 20')
    rows=[];lines=['💸 <b>Withdrawals</b>\n']
    for x in r:
        lines.append(f'#{x["id"]} | ₹{Decimal(x["amount"]):.2f} | <b>{x["status"].upper()}</b> | {html.escape(x["first_name"] or "User")} | <code>{html.escape(x["upi_id"] or "-")}</code>')
        if x['status']=='pending':rows.append([('✅ Paid',f'wd:paid:{x["id"]}','success'),('❌ Reject',f'wd:reject:{x["id"]}','danger')])
    if not r:lines.append('No withdrawals.')
    rows.append([('⬅ Back','ad:home')])
    await panel(t,'\n'.join(lines),kb(rows))
async def stats_screen(t):
    async with pool.acquire() as c:
        total=await c.fetchval('SELECT COUNT(*) FROM users');ver=await c.fetchval('SELECT COUNT(*) FROM users WHERE verified');pend=await c.fetchval("SELECT COUNT(*) FROM withdrawals WHERE status='pending'");paid=await c.fetchval("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status='paid'");dev=await c.fetchval('SELECT COUNT(*) FROM users WHERE device_verified');blk=await c.fetchval("SELECT COUNT(*) FROM device_logs WHERE status='blocked'")
    await panel(t,f'👥 <b>Stats</b>\n\nUsers: {total}\nVerified: {ver}\nDevice verified: {dev}\nBlocked attempts: {blk}\nPending withdrawals: {pend}\nPaid: ₹{Decimal(paid):.2f}',kb([[('⬅ Back','ad:home')]]))

# ---- Device verification ----
async def dev_screen(t):
    on=(await setting('device_check','on'))=='on';stt=(await setting('device_strict','on'))=='on'
    await panel(t,f'🛡 <b>Device Verification</b>\n\nStatus: <b>{"ON" if on else "OFF"}</b> | Strict match: <b>{"ON" if stt else "OFF"}</b>\nWEBAPP_URL: {"set" if WEBAPP_URL else "NOT SET"}\nIP limit: {await setting("ip_limit")} | Device limit: {await setting("device_limit")}\n\nText aur button badalne ke liye neeche se edit karein.',kb([[('✏️ Edit Text','ad:tx:device_text'),('✏️ Edit Button','ad:bt:device_button')],[(('🟢 ' if on else '🔴 ')+'Device Check','ad:dv:tog'),(('🟢 ' if stt else '🔴 ')+'Strict Match','ad:dv:strict')],[('👁 Preview','ad:dv:prev'),('⚙️ Limits','ad:set')],[('⬅ Back','ad:home')]]))
async def dv_route(q,p):
    if p:
        a=p[0]
        if a=='tog':nv='off' if (await setting('device_check','on'))=='on' else 'on';return await draft(q.from_user.id,'setting',{'key':'device_check','value':nv},f'Device Check: <b>{nv.upper()}</b>')
        elif a=='strict':nv='off' if (await setting('device_strict','on'))=='on' else 'on';return await draft(q.from_user.id,'setting',{'key':'device_strict','value':nv},f'Strict Match: <b>{nv.upper()}</b>')
        elif a=='prev':
            if not WEBAPP_URL:return '❌ WEBAPP_URL set nahi hai.'
            await q.message.answer(await user_txt('device_text',uid),reply_markup=await device_kb());return
    await dev_screen(q)

# ---- Numeric settings ----
SETS={'referral_amount':'Referral reward (₹)','penalty_amount':'Channel leave penalty (₹)','min_withdrawal':'Min withdrawal (₹)','min_referrals':'Min referrals to withdraw','withdrawal_channel_id':'Withdrawal channel ID','ip_limit':'IP limit (accounts per IP)','device_limit':'Device limit (accounts per device)'}
async def set_screen(t):
    lines=['⚙️ <b>Settings</b>\n'];rows=[]
    for k,v in SETS.items():
        lines.append(f'{v}: <b>{html.escape(await setting(k,"") or "NOT SET")}</b>');rows.append([(f'✏️ {v}',f'ad:set:{k}')])
    rows.append([('⬅ Back','ad:home')]);await panel(t,'\n'.join(lines),kb(rows))
async def set_route(q,p):
    if p and p[0] in SETS:
        await state(q.from_user.id,'a_set',{'key':p[0]});await q.message.answer(f'✏️ {SETS[p[0]]} ki nayi value bhejein.');return
    await set_screen(q)

@dp.message(Command('setbutton'))
async def setbutton(m):
    if not admin(m.from_user.id):return
    raw=m.text.partition(' ')[2]
    if '|' not in raw:return await m.answer('Usage: /setbutton key|Name #g')
    k,v=raw.split('|',1);label,st=parse_label(v)
    async with pool.acquire() as c:x=await c.fetchrow('SELECT * FROM buttons WHERE key=$1',k.strip())
    if not x:return await m.answer('Unknown button key.')
    await draft(m.from_user.id,'button',{'key':k.strip(),'label':label,'style':st,'enabled':x['enabled'],'sort_order':x['sort_order']},f'Button: <b>{html.escape(label)}</b>\nColor: {STNAME[st]}')
@dp.message(Command('setref'))
async def sr(m):
    if not admin(m.from_user.id):return
    try:v=Decimal(m.text.split()[1]);assert v>=0;await draft(m.from_user.id,'setting',{'key':'referral_amount','value':str(v)},f'Referral reward: <b>₹{v}</b>')
    except:await m.answer('Usage: /setref 10')
@dp.message(Command('setminwithdraw'))
async def smw(m):
    if not admin(m.from_user.id):return
    try:v=Decimal(m.text.split()[1]);assert v>0;await draft(m.from_user.id,'setting',{'key':'min_withdrawal','value':str(v)},f'Min withdrawal: <b>₹{v}</b>')
    except:await m.answer('Usage: /setminwithdraw 50')
@dp.message(Command('setminrefs'))
async def smr(m):
    if not admin(m.from_user.id):return
    try:v=int(m.text.split()[1]);assert v>=0;await draft(m.from_user.id,'setting',{'key':'min_referrals','value':str(v)},f'Min referrals: <b>{v}</b>')
    except:await m.answer('Usage: /setminrefs 3')
@dp.message(Command('setwithdrawchannel'))
async def swc(m):
    if not admin(m.from_user.id):return
    try:v=m.text.split()[1];int(v);await draft(m.from_user.id,'setting',{'key':'withdrawal_channel_id','value':v},f'Withdrawal channel ID: <code>{v}</code>')
    except:await m.answer('Usage: /setwithdrawchannel -100123456789')
@dp.message(Command('setwelcome'))
async def sw(m):
    if not admin(m.from_user.id):return
    x=m.text.partition(' ')[2]
    if x:await draft(m.from_user.id,'text',{'key':'welcome','value':x},fmt(x,**SAMPLE))
@dp.message(Command('addchannel'))
async def addch(m):
    if not admin(m.from_user.id):return
    p=[x.strip() for x in m.text.partition(' ')[2].split('|')]
    if not p or not p[0]:return await m.answer('Usage: /addchannel @channel | Title | InviteLink')
    ident=p[0];link=p[2] if len(p)>2 and p[2] else None
    try:
        chat=await bot.get_chat(int(ident) if ident.lstrip('-').isdigit() else ident);cid=chat.id;title=p[1] if len(p)>1 and p[1] else (chat.title or chat.full_name or ident)
        if not link and getattr(chat,'username',None):link=f'https://t.me/{chat.username}'
    except Exception:return await m.answer('❌ Channel nahi mila. Valid @username ya -100... ID use karein.')
    await draft(m.from_user.id,'channel',{'chat_id':cid,'title':title,'invite_link':link,'request_join':False,'enabled':True,'invite_style':'primary','invite_icon_custom_emoji_id':None},f'📢 <b>Channel Preview</b>\nTitle: {html.escape(title)}\nChat ID: <code>{cid}</code>\nLink: {html.escape(link or "Not set")}')
@dp.message(Command('devicecheck'))
async def dchk(m):
    if not admin(m.from_user.id):return
    p=m.text.split()
    if len(p)<2 or p[1] not in('on','off'):return await m.answer('Usage: /devicecheck on|off')
    await draft(m.from_user.id,'setting',{'key':'device_check','value':p[1]},f'Device check: <b>{p[1].upper()}</b>')
@dp.message(Command('setiplimit'))
async def sil(m):
    if not admin(m.from_user.id):return
    try:v=int(m.text.split()[1]);assert v>=1;await draft(m.from_user.id,'setting',{'key':'ip_limit','value':str(v)},f'IP limit: <b>{v}</b>')
    except:await m.answer('Usage: /setiplimit 2')
@dp.message(Command('setdevicelimit'))
async def sdl(m):
    if not admin(m.from_user.id):return
    try:v=int(m.text.split()[1]);assert v>=1;await draft(m.from_user.id,'setting',{'key':'device_limit','value':str(v)},f'Device limit: <b>{v}</b>')
    except:await m.answer('Usage: /setdevicelimit 1')
@dp.message(Command('resetdevice'))
async def rdev(m):
    if not admin(m.from_user.id):return
    try:uid=int(m.text.split()[1])
    except:return await m.answer('Usage: /resetdevice USER_ID')
    async with pool.acquire() as c:
        await c.execute('DELETE FROM device_logs WHERE user_id=$1',uid);await c.execute('UPDATE users SET device_verified=FALSE,verified=FALSE WHERE id=$1',uid)
    await m.answer('✅ Device record cleared. User ko dobara verify karna hoga.')


# ---------------- DEVICE / IP VERIFICATION (Mini App) ----------------
PAGE=r"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Device Verification</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:#070b1a;color:#fff;font-family:-apple-system,Segoe UI,Roboto,sans-serif;display:flex;align-items:center;justify-content:center;padding:18px}
.card{width:100%;max-width:420px;background:#0b0d14;border:1px solid #1c2236;border-radius:28px;padding:26px 20px;text-align:center}
.ico{width:84px;height:84px;margin:0 auto 14px;border-radius:50%;border:1px solid #1f2a44;display:flex;align-items:center;justify-content:center;font-size:36px;background:radial-gradient(circle,#10294a,#0b0d14)}
h1{font-size:26px;margin:6px 0}p{color:#9aa3bd;margin:4px 0 16px;font-size:15px}
.bar{height:8px;background:#161b2c;border-radius:8px;overflow:hidden}.bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,#6366f1,#a855f7);transition:width .5s}
.row{display:flex;justify-content:space-between;font-size:12px;letter-spacing:1px;color:#818cf8;margin:8px 2px 16px;font-weight:600}
.log{text-align:left;background:#07080d;border:1px solid #1c2236;border-radius:14px;padding:12px;font:12px ui-monospace,Menlo,monospace;color:#8b93ff;min-height:96px}
.log div{margin:3px 0}.note{font-size:11px;color:#6b738f;margin-top:14px;line-height:1.5}
.ok h1{color:#34d399}.err h1{color:#f87171}button{margin-top:16px;border:0;border-radius:14px;padding:12px 26px;background:#4f46e5;color:#fff;font-size:15px;display:none}
</style></head><body><div class="card" id="card"><div class="ico" id="ico">🛡</div><h1 id="h">Checking Device</h1><p id="sub">Device aur network ki jaanch ho rahi hai...</p>
<div class="bar"><i id="b"></i></div><div class="row"><span id="st">CHECKING DEVICE</span><span id="pc">0%</span></div>
<div class="log" id="log"></div><button id="close" onclick="Telegram.WebApp.close()">Close</button>
<div class="note">🔒 Anti-fraud: duplicate/fake referral accounts rokne ke liye aapka IP address aur device fingerprint server par record hota hai.</div></div>
<script>
const tg=Telegram.WebApp;tg.ready();try{tg.expand()}catch(e){}
const $=id=>document.getElementById(id);
function prog(p){$('b').style.width=p+'%';$('pc').textContent=p+'%'}
function log(t){const d=document.createElement('div');d.textContent='['+new Date().toLocaleTimeString()+'] '+t;$('log').appendChild(d)}
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function sha(s){try{const b=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(s));return Array.from(new Uint8Array(b)).map(x=>x.toString(16).padStart(2,'0')).join('')}catch(e){let h=5381;for(const c of s)h=((h<<5)+h+c.charCodeAt(0))>>>0;return ('00000000'+h.toString(16)).repeat(4)}}
function canvasFp(){try{const c=document.createElement('canvas');c.width=220;c.height=40;const x=c.getContext('2d');x.textBaseline='top';x.font='16px Arial';x.fillStyle='#f60';x.fillRect(10,5,80,25);x.fillStyle='#069';x.fillText('RJ device check 1.0 ✓',4,12);return c.toDataURL()}catch(e){return 'na'}}
function glFp(){try{const g=document.createElement('canvas').getContext('webgl');const e=g.getExtension('WEBGL_debug_renderer_info');return e?g.getParameter(e.UNMASKED_VENDOR_WEBGL)+'|'+g.getParameter(e.UNMASKED_RENDERER_WEBGL):'na'}catch(e){return 'na'}}
function devId(){try{let v=localStorage.getItem('rj_did');if(!v){v=(crypto.randomUUID?crypto.randomUUID():Math.random().toString(36).slice(2)+Date.now().toString(36));localStorage.setItem('rj_did',v)}return v}catch(e){return ''}}
function fail(msg){$('card').className='card err';$('ico').textContent='⛔';$('h').textContent='Verification Failed';$('sub').textContent=msg;$('st').textContent='FAILED';$('close').style.display='inline-block'}
(async()=>{
 try{
  if(!tg.initData){fail('Ye page sirf Telegram bot ke andar se kholein.');return}
  log('User found: ID '+((tg.initDataUnsafe&&tg.initDataUnsafe.user&&tg.initDataUnsafe.user.id)||'?'));prog(15);await sleep(500)
  log('Reading device details...');
  const parts=[navigator.userAgent,navigator.platform,(navigator.languages||[navigator.language]).join(','),Intl.DateTimeFormat().resolvedOptions().timeZone,screen.width+'x'+screen.height+'x'+screen.colorDepth,window.devicePixelRatio,navigator.hardwareConcurrency,navigator.deviceMemory,navigator.maxTouchPoints,canvasFp(),glFp()];
  const fp=await sha(parts.join('||'));prog(45);await sleep(500)
  log('Sending details to server...');prog(75)
  const r=await fetch('/api/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({initData:tg.initData,fingerprint:fp,deviceId:devId()})});
  const j=await r.json();await sleep(400)
  if(j.ok){prog(100);$('card').className='card ok';$('ico').textContent='✅';$('h').textContent='Verified!';$('sub').textContent='Device verification complete. Bot par wapas jayein.';$('st').textContent='VERIFIED';log('Verification successful');setTimeout(()=>tg.close(),1600)}
  else fail(j.error||'Verification failed')
 }catch(e){fail('Network error. Dobara try karein.')}
})();
</script></body></html>"""

def norm_ip(ip):
    try:
        a=ipaddress.ip_address(ip)
        if a.version==6:return str(ipaddress.ip_network(f'{a}/64',strict=False).network_address)+'/64'
        return str(a)
    except Exception:return ip or ''
def client_ip(req):
    ip=req.headers.get('CF-Connecting-IP') or req.headers.get('X-Forwarded-For','').split(',')[0].strip() or (req.remote or '')
    return norm_ip(ip)
def check_init(init_data):
    try:
        d=dict(parse_qsl(init_data,keep_blank_values=True));h=d.pop('hash',None)
        if not h:return None
        chk='\n'.join(f'{k}={v}' for k,v in sorted(d.items()))
        key=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(key,chk.encode(),hashlib.sha256).hexdigest(),h):return None
        if time.time()-int(d.get('auth_date','0'))>3600:return None
        return json.loads(d['user'])
    except Exception:return None

async def page_verify(req):
    return web.Response(text=PAGE,content_type='text/html',headers={'Cache-Control':'no-store'})
async def api_verify(req):
    def out(ok,err=None,st=200):return web.json_response({'ok':ok,'error':err},status=st)
    try:data=await req.json()
    except Exception:return out(False,'Bad request',400)
    u=check_init(str(data.get('initData','')))
    if not u:return out(False,'Telegram session invalid/expired. Bot se dobara button dabayein.',403)
    uid=int(u['id']);fp=str(data.get('fingerprint',''))[:128];did=str(data.get('deviceId',''))[:64];ip=client_ip(req);ua=req.headers.get('User-Agent','')[:300]
    if len(fp)<16:return out(False,'Device details nahi mile.',400)
    row=await getuser(uid)
    if not row:return out(False,'Pehle bot me /start karein.',404)
    if row['device_verified']:return out(True)
    if not await channel_access_ok(uid):return out(False,'Pehle sabhi channels join karein.',403)
    ipl=int(await setting('ip_limit','2'));dvl=int(await setting('device_limit','1'));reason=None
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute('SELECT pg_advisory_xact_lock(7001)')
            if uid not in ADMINS:
                strict=(await setting('device_strict','on'))=='on'
                cond="(device_id<>'' AND device_id=$2) OR fingerprint=$3" if strict else "(device_id<>'' AND device_id=$2) OR (fingerprint=$3 AND ip=$4)"
                dev_hit=await c.fetchval(f"SELECT COUNT(DISTINCT user_id) FROM device_logs WHERE status='ok' AND user_id<>$1 AND ({cond})",*([uid,did,fp] if strict else [uid,did,fp,ip]))
                ip_hit=await c.fetchval("SELECT COUNT(DISTINCT user_id) FROM device_logs WHERE status='ok' AND user_id<>$1 AND ip=$2",uid,ip)
                if dev_hit>=dvl:reason='device'
                elif ip_hit>=ipl:reason='ip'
            await c.execute('INSERT INTO device_logs(user_id,device_id,fingerprint,ip,user_agent,status,reason) VALUES($1,$2,$3,$4,$5,$6,$7)',uid,did,fp,ip,ua,'blocked' if reason else 'ok',reason)
            if not reason:await c.execute('UPDATE users SET device_verified=TRUE,updated_at=NOW() WHERE id=$1',uid)
    if reason:
        log.warning('device blocked uid=%s reason=%s ip=%s',uid,reason,ip)
        msg='Ye device pehle se kisi aur account se verify ho chuka hai.' if reason=='device' else 'Is network (IP) se allowed accounts ki limit poori ho chuki hai.'
        try:await bot.send_message(uid,'⛔ <b>Device verification failed</b>\n\n'+msg+'\nApne original account ka use karein.')
        except Exception:pass
        return out(False,msg,403)
    await verify(uid);await credit_ref(uid)
    try:
        await bot.send_message(uid,'✅ <b>Device verified!</b>');await send_welcome(uid)
    except Exception:log.exception('post-verify message failed')
    return out(True)
async def health(req):return web.Response(text='ok')
async def start_web():
    app=web.Application();app.router.add_get('/verify',page_verify);app.router.add_post('/api/verify',api_verify);app.router.add_get('/',health)
    runner=web.AppRunner(app);await runner.setup();await web.TCPSite(runner,'0.0.0.0',PORT).start();log.info('Web server on :%s (WEBAPP_URL=%s)',PORT,WEBAPP_URL or 'NOT SET - device check disabled');return runner

@dp.callback_query(F.data.startswith('wd:'))
async def wd_action(q):
    if not admin(q.from_user.id):return await q.answer('Denied',show_alert=True)
    _,act,wid_s=q.data.split(':');wid=int(wid_s)
    async with pool.acquire() as c:r=await c.fetchrow('SELECT * FROM withdrawals WHERE id=$1',wid)
    if not r or r['status']!='pending':return await q.answer('Already processed/not found.',show_alert=True)
    if act=='paid':
        async with pool.acquire() as c:await c.execute("UPDATE withdrawals SET status='paid',admin_id=$1,updated_at=NOW() WHERE id=$2 AND status='pending'",q.from_user.id,wid)
        await update_withdrawal_channel(wid,'PAID')
        await bot.send_message(r['user_id'],await user_txt('wd_paid',r['user_id'],id=str(wid),amount=f'{Decimal(r["amount"]):.2f}'))
        await q.answer('Payment marked done.')
        try:await q.message.edit_reply_markup(reply_markup=None)
        except Exception:pass
    else:
        await state(q.from_user.id,'a_reject',{'wid':wid});await q.answer();await q.message.answer(f'❌ Send rejection issue for withdrawal #{wid}.')

async def draft(uid,kind,payload,preview_text,markup=None):
    await state(uid,'a_confirm',{'kind':kind,'payload':payload})
    await bot.send_message(uid,'👀 <b>PREVIEW</b>\n\n'+preview_text,reply_markup=markup)
    await bot.send_message(uid,'Agar preview sahi hai to <b>Next / Confirm</b> dabayein. Save tabhi hoga.',reply_markup=kb([[('➡️ Next / Confirm','ad:confirm','success'),('❌ Cancel','ad:cancel','danger')]]))

async def apply_draft(uid,d):
    kind=d['kind'];p=d['payload']
    async with pool.acquire() as c:
        if kind=='button': await c.execute('UPDATE buttons SET label=$1,style=$2,enabled=$3,sort_order=$4,icon_custom_emoji_id=COALESCE($5,icon_custom_emoji_id) WHERE key=$6',p['label'],p['style'],p['enabled'],p['sort_order'],p.get('icon_custom_emoji_id'),p['key'])
        elif kind in ('setting','text'): await c.execute('INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value',p['key'],p['value'])
        elif kind=='other_button':
            await c.execute('INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value',p['key'],p['value'])
            if p.get('icon_custom_emoji_id'):await c.execute('INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value',p['key']+'_icon_custom_emoji_id',str(p['icon_custom_emoji_id']))
        elif kind=='user_ban': await c.execute('UPDATE users SET is_banned=$1,updated_at=NOW() WHERE id=$2',p['banned'],p['user_id'])
        elif kind=='wallet_adjust':
            row=await c.fetchrow('SELECT wallet FROM users WHERE id=$1 FOR UPDATE',p['user_id'])
            if not row: raise ValueError('User not found')
            delta=Decimal(p['amount']); newbal=Decimal(row['wallet'])+delta
            if newbal<0: raise ValueError('Wallet cannot be negative')
            await c.execute('UPDATE users SET wallet=$1,updated_at=NOW() WHERE id=$2',newbal,p['user_id'])
            await c.execute("INSERT INTO wallet_transactions(user_id,amount,balance_after,type,reason,admin_id) VALUES($1,$2,$3,$4,$5,$6)",p['user_id'],delta,newbal,'admin_credit' if delta>0 else 'admin_debit',p['reason'],uid)
        elif kind=='channel':
            if p.get('id'): await c.execute('UPDATE required_channels SET chat_id=$1,title=$2,invite_link=$3,request_join=$4,enabled=$5,invite_style=$6,invite_icon_custom_emoji_id=$7 WHERE id=$8',p['chat_id'],p['title'],p.get('invite_link'),p.get('request_join',False),p.get('enabled',True),p.get('invite_style','primary'),p.get('invite_icon_custom_emoji_id'),p['id'])
            else: await c.execute('INSERT INTO required_channels(chat_id,title,invite_link,request_join,enabled,invite_style,invite_icon_custom_emoji_id) VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(chat_id) DO UPDATE SET title=EXCLUDED.title,invite_link=EXCLUDED.invite_link,request_join=EXCLUDED.request_join,enabled=TRUE,invite_style=EXCLUDED.invite_style,invite_icon_custom_emoji_id=EXCLUDED.invite_icon_custom_emoji_id',p['chat_id'],p['title'],p.get('invite_link'),p.get('request_join',False),p.get('enabled',True),p.get('invite_style','primary'),p.get('invite_icon_custom_emoji_id'))
        elif kind=='channel_action': await c.execute(p['sql'],*p.get('args',[]))

async def admin_flow(m,s):
    d=jd(s['data']);st=s['state'];uid=m.from_user.id
    if st=='a_user_search':
        query=(m.text or '').strip();mode=d.get('mode','search')
        async with pool.acquire() as c:
            if query.isdigit():rows=await c.fetch('SELECT * FROM users WHERE id=$1',int(query))
            else:
                qx=query.lstrip('@');rows=await c.fetch("SELECT * FROM users WHERE username ILIKE $1 OR first_name ILIKE $2 OR last_name ILIKE $2 ORDER BY id DESC LIMIT 20",qx+'%','%'+query+'%')
        if not rows:return await m.answer('❌ No users found.')
        await clear(uid)
        if mode=='txsearch':
            for x in rows[:10]:await wallet_history(m,x['id'])
        else:
            await m.answer('🔎 Search results:')
            for x in rows[:10]:await m.answer(f'👤 <b>{html.escape(x["first_name"] or "User")}</b>\nID: <code>{x["id"]}</code>\nWallet: ₹{Decimal(x["wallet"]):.2f}\nBan: {"YES" if x["is_banned"] else "NO"}',reply_markup=kb([[('👁 Open',f'ad:users:view:{x["id"]}')]]))
        return
    if st=='a_wallet_adj':
        raw=(m.text or '').strip()
        if '|' not in raw:return await m.answer('Format: 100 | Bonus')
        a,reason=[x.strip() for x in raw.split('|',1)]
        try:amount=Decimal(a);assert amount>0
        except:return await m.answer('❌ Invalid amount.')
        mode=d['mode'];sign=1 if mode=='credit' else -1
        async with pool.acquire() as c:u=await c.fetchrow('SELECT wallet FROM users WHERE id=$1',int(d['user_id']))
        if not u:return await m.answer('User not found.')
        if sign<0 and amount>Decimal(u['wallet']):return await m.answer('❌ Debit amount exceeds wallet.')
        newbal=Decimal(u['wallet'])+sign*amount
        return await draft(uid,'wallet_adjust',{'user_id':int(d['user_id']),'amount':str(sign*amount),'reason':reason,'new_balance':str(newbal)},f'💳 Wallet Preview\n\nUser: <code>{d["user_id"]}</code>\nChange: <b>{"+" if sign>0 else "-"}₹{amount:.2f}</b>\nNew balance: <b>₹{newbal:.2f}</b>\nReason: {html.escape(reason)}')
    if st=='a_reject':

        reason=(m.text or '').strip();wid=int(d['wid'])
        async with pool.acquire() as c:
            async with c.transaction():
                r=await c.fetchrow('SELECT * FROM withdrawals WHERE id=$1 FOR UPDATE',wid)
                if not r or r['status']!='pending':await clear(uid);return await m.answer('Already processed.')
                await c.execute("UPDATE withdrawals SET status='rejected',issue=$1,admin_id=$2,updated_at=NOW() WHERE id=$3",reason,uid,wid)
                newbal=await c.fetchval('UPDATE users SET wallet=wallet+$1,updated_at=NOW() WHERE id=$2 RETURNING wallet',r['amount'],r['user_id'])
                await c.execute("INSERT INTO wallet_transactions(user_id,amount,balance_after,type,reason,admin_id) VALUES($1,$2,$3,'withdrawal_refund',$4,$5)",r['user_id'],r['amount'],newbal,f'Withdrawal #{wid} rejected: {reason}',uid)
        await clear(uid)
        await update_withdrawal_channel(wid,'REJECTED',reason)
        try:await bot.send_message(r['user_id'],await user_txt('wd_rej',r['user_id'],id=str(wid),amount=f'{Decimal(r["amount"]):.2f}',issue=html.escape(reason)))
        except Exception:log.warning('reject notify failed')
        return await m.answer('✅ Rejected and refunded.')
    if st=='a_mbren':
        raw=(m.text or '').strip()
        if not raw:return await m.answer('❌ Naam bhejein.')
        label,sty,icon=parse_button_input(m)
        async with pool.acquire() as c:x=await c.fetchrow('SELECT * FROM buttons WHERE key=$1',d['key'])
        return await draft(uid,'button',{'key':d['key'],'label':label,'style':sty,'enabled':x['enabled'],'sort_order':x['sort_order'],'icon_custom_emoji_id':icon},f'Button: <b>{html.escape(label)}</b>\nColor: {STNAME[sty]}\nStatus: {"ON" if x["enabled"] else "OFF"}')
    if st=='a_tx':
        k=d['key'];t=(m.html_text or '').strip()
        if not t or not m.text:return await m.answer('❌ Text bhejein.')
        try: preview=fmt(t,**SAMPLE); await draft(uid,'text',{'key':k,'value':t},preview)
        except TelegramBadRequest as e:return await m.answer('❌ HTML error: '+html.escape(str(e)[:200]))
        return
    if st=='a_bt':
        raw=(m.text or '').strip()
        if not raw:return await m.answer('❌ Naam bhejein.')
        label,sty,icon=parse_button_input(m);tag={'danger':'#r','success':'#g','primary':'#b','default':'#d'}[sty]
        return await draft(uid,'other_button',{'key':d['key'],'value':label+((' '+tag) if tag!='#d' else ''),'icon_custom_emoji_id':icon},f'Button: <b>{html.escape(label)}</b>\nColor: {STNAME[sty]}')
    if st=='a_ch':
        p=[x.strip() for x in (m.text or '').split('|')]
        if not p or not p[0]:return await m.answer('@channel or CHAT_ID | Title | InviteLink')
        ident=p[0];link=p[2] if len(p)>2 and p[2] else None
        try:
            chat=await bot.get_chat(int(ident) if ident.lstrip('-').isdigit() else ident);cid=chat.id;title=p[1] if len(p)>1 and p[1] else (chat.title or chat.full_name or ident)
            if not link and getattr(chat,'username',None):link=f'https://t.me/{chat.username}'
        except Exception:return await m.answer('❌ Channel nahi mila. Bot ko channel me add/admin karein aur valid @username ya -100... chat ID bhejein.')
        return await draft(uid,'channel',{'chat_id':cid,'title':title,'invite_link':link,'request_join':False,'enabled':True,'invite_style':'primary','invite_icon_custom_emoji_id':None},f'📢 <b>Channel Preview</b>\n\nTitle: {html.escape(title)}\nChat ID: <code>{cid}</code>\nLink: {html.escape(link or "Not set")}')
    if st=='a_chemoji':
        icon=custom_emoji_id(m)
        if not icon:return await m.answer('❌ Premium custom emoji nahi mila. Telegram Premium custom emoji bhejein.')
        i=int(d['id'])
        await draft(uid,'channel_action',{'sql':'UPDATE required_channels SET invite_icon_custom_emoji_id=$1 WHERE id=$2','args':[icon,i]},f'✨ Premium emoji attach karein?\nEmoji ID: <code>{icon}</code>')
        return
    if st=='a_set':
        k=d['key'];v=(m.text or '').strip()
        try:
            if k in ('referral_amount','min_withdrawal','penalty_amount'):x=Decimal(v);assert x>=0
            elif k=='min_referrals':x=int(v);assert x>=0
            elif k in ('ip_limit','device_limit'):x=int(v);assert x>=1
            else:x=v
        except Exception:return await m.answer('❌ Valid value bhejein.')
        return await draft(uid,'setting',{'key':k,'value':str(x)},f'⚙️ <b>{html.escape(SETS.get(k,k))}</b>\nNew value: <b>{html.escape(str(x))}</b>')
    if st=='a_media':
        med=media_of(m)
        if not med:return await m.answer('❌ Photo / video / GIF / document bhejein.')
        d0=await get_rich(d['sc']);d0['media']=med;await clear(uid);return await draft(uid,'rich',{'key':d['sc']+'_rich','value':d0},'🖼 Media Preview')
    if st=='a_rtext':
        t=(m.html_text or '').strip()
        if not t or not m.text:return await m.answer('❌ Text bhejein.')
        d0=await get_rich(d['sc']);d0['text']=t;await clear(uid);return await draft(uid,'rich',{'key':d['sc']+'_rich','value':d0},fmt(t,**SAMPLE))
    if st=='a_rbtn':
        new=[]
        for line in (m.text or '').splitlines():
            if '|' not in line:continue
            lab,sty=parse_label(line);t,u=[x.strip() for x in lab.split('|',1)]
            if t and u.startswith(('http://','https://','tg://')):new.append({'t':t[:60],'u':u,'s':sty,'icon_custom_emoji_id':custom_emoji_id(m)})
        if not new:return await m.answer('❌ Format galat. Aise bhejein: Button Text | https://link.com #g')
        d0=await get_rich(d['sc']);d0['btns']+=new;await clear(uid);return await draft(uid,'rich',{'key':d['sc']+'_rich','value':d0},'🔘 <b>Buttons Preview</b>\n'+ '\n'.join('• '+html.escape(x['t'])+' ['+STNAME.get(x.get('s','default'))+']' for x in d0['btns']))

@dp.message(F.content_type.in_({'text','photo','document','video','animation'}))
async def flow(m:Message):
    uid=m.from_user.id;s=await state(uid)
    # Admin workflows keep priority so their input is never mistaken for a user button.
    if s and uid in ADMINS and str(s['state']).startswith('a_'):
        await admin_flow(m,s);return

    # ReplyKeyboard buttons arrive as ordinary text messages. Handle them before
    # state-specific input so bottom keyboard buttons actually work in every state.
    r=await user_button_by_text(m.text) if m.text else None
    if r:
        if s: await clear(uid)
        await handle_user_button(m,r)
        return

    if not s:return
    d=jd(s['data'])
    if s['state']=='w_amount':
        try:a=Decimal((m.text or '').replace(',','').strip());assert a>0
        except Exception:return await m.answer('❌ Send a valid amount, e.g. 100.')
        d['amount']=str(a);await state(uid,'w_upi',d);return await m.answer(await user_txt('wd_upi',uid))
    if s['state']=='w_upi':
        if not m.text:return await m.answer('❌ Send UPI ID as text.')
        d['upi']=m.text.strip();await state(uid,'w_attachment',d);return await m.answer(await user_txt('wd_attach',uid))
    if s['state']=='w_attachment':
        f=m.photo[-1].file_id if m.photo else (m.document.file_id if m.document else None)
        if not f and (m.text or '').strip().upper()!='SKIP':return await m.answer('Send photo/document or SKIP.')
        try:wid=await create_withdraw(uid,Decimal(d['amount']),d['upi'],f)
        except ValueError as e:await clear(uid);return await m.answer('❌ '+html.escape(str(e)))
        await clear(uid);ok=await post_withdraw(wid)
        await m.answer(await user_txt('wd_done',uid,id=str(wid),status='Pending' if ok else 'Created - admin channel not configured'))

@dp.chat_member()
async def required_channel_member_update(e:ChatMemberUpdated):
    try:
        if e.new_chat_member.status in ('left','kicked'):
            async with pool.acquire() as c:await c.execute('DELETE FROM join_requests WHERE chat_id=$1 AND user_id=$2',e.chat.id,e.new_chat_member.user.id)
        async with pool.acquire() as c:ch=await c.fetchrow('SELECT * FROM required_channels WHERE chat_id=$1 AND enabled',e.chat.id)
        if not ch:return
        old=e.old_chat_member.status;new=e.new_chat_member.status
        if old not in {'member','administrator','creator','restricted'} or new not in {'left','kicked'}:return
        uid=e.from_user.id;penalty=Decimal(await setting('penalty_amount','2'))
        async with pool.acquire() as c:
            async with c.transaction():
                await c.execute('SELECT pg_advisory_xact_lock(88001,$1)',int(uid%2147483647))
                recent=await c.fetchval("SELECT 1 FROM channel_leave_penalties WHERE user_id=$1 AND channel_id=$2 AND created_at>NOW()-INTERVAL '10 seconds'",uid,ch['id'])
                if recent:return
                await c.execute('INSERT INTO channel_leave_penalties(user_id,channel_id,chat_id,amount) VALUES($1,$2,$3,$4)',uid,ch['id'],e.chat.id,penalty)
                newbal=await c.fetchval('UPDATE users SET wallet=GREATEST(wallet-$1,0),penalty_total=COALESCE(penalty_total,0)+$1,access_blocked=TRUE WHERE id=$2 RETURNING wallet',penalty,uid)
                await c.execute("INSERT INTO wallet_transactions(user_id,amount,balance_after,type,reason) VALUES($1,$2,$3,'channel_penalty',$4)",uid,-penalty,newbal,f'Left required channel: {ch["title"] or ch["chat_id"]}')
        try:await bot.send_message(uid,await user_txt('penalty_text',uid,penalty=f'{penalty:.2f}'),reply_markup=await required_join_kb(uid))
        except Exception:log.warning('penalty notify failed for %s',uid)
    except Exception:log.exception('channel leave handler failed')

@dp.chat_join_request()
async def join_req(e):
    async with pool.acquire() as c:
        r=await c.fetchrow('SELECT request_join FROM required_channels WHERE chat_id=$1 AND enabled',e.chat.id)
        if r:await c.execute('INSERT INTO join_requests(chat_id,user_id) VALUES($1,$2) ON CONFLICT DO NOTHING',e.chat.id,e.from_user.id)
    if r and r['request_join']:
        try:await bot.approve_chat_join_request(e.chat.id,e.from_user.id)
        except Exception:log.exception('join request approval failed')

async def main():
    await db_init();runner=await start_web();me=await bot.get_me();log.info('Started @%s',me.username)
    try:await dp.start_polling(bot,allowed_updates=dp.resolve_used_update_types())
    finally:await runner.cleanup();await pool.close();await bot.session.close()
if __name__=='__main__':asyncio.run(main())
