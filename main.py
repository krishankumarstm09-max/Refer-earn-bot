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
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
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
CREATE TABLE IF NOT EXISTS required_channels(id BIGSERIAL PRIMARY KEY,chat_id BIGINT UNIQUE,title TEXT,invite_link TEXT,request_join BOOLEAN DEFAULT FALSE,enabled BOOLEAN DEFAULT TRUE);
CREATE TABLE IF NOT EXISTS buttons(key TEXT PRIMARY KEY,label TEXT,style TEXT DEFAULT 'default',enabled BOOLEAN DEFAULT TRUE,sort_order INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
ALTER TABLE users ADD COLUMN IF NOT EXISTS device_verified BOOLEAN DEFAULT FALSE;
CREATE TABLE IF NOT EXISTS device_logs(id BIGSERIAL PRIMARY KEY,user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,device_id TEXT,fingerprint TEXT,ip TEXT,user_agent TEXT,status TEXT,reason TEXT,created_at TIMESTAMPTZ DEFAULT NOW());
CREATE INDEX IF NOT EXISTS idx_dl_device ON device_logs(device_id);
CREATE INDEX IF NOT EXISTS idx_dl_fp_ip ON device_logs(fingerprint,ip);
CREATE INDEX IF NOT EXISTS idx_dl_ip ON device_logs(ip);
CREATE TABLE IF NOT EXISTS states(user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,state TEXT,data JSONB DEFAULT '{}',updated_at TIMESTAMPTZ DEFAULT NOW());
'''
DEFAULTS={'referral_amount':'10','min_withdrawal':'50','min_referrals':'0','device_check':'on','device_strict':'on','device_text':'✅ <b>Sabhi Channels Verified!</b>\n\n🛡 <b>Step 2: Device Verification</b>\n\nDuplicate/fake referral rokne ke liye aapke device aur network (IP) ki ek baar jaanch hogi. Neeche <b>Verify Device</b> button dabayein:','device_button':'🛡 Verify Device','ip_limit':'2','device_limit':'1','withdrawal_channel_id':'','welcome':'🔥 <b>Welcome!</b>\n\nRefer friends, earn rewards and withdraw your balance.'}
BUTTONS=[('withdraw','💸 Withdraw','success',10),('referral','🔗 My Referral Link','primary',20),('wallet','💰 My Wallet','default',30),('leaderboard','🏆 Leaderboard','primary',40)]
STYLE={'#r':'danger','#g':'success','#b':'primary','#d':'default'}

def ibtn(text,style='default',**kw):
    if style and style!='default':
        try:return InlineKeyboardButton(text=text,style=style,**kw)
        except TypeError:pass
    return InlineKeyboardButton(text=text,**kw)
def btn(text,data,style='default'):return ibtn(text,style,callback_data=data)
def kb(rows):return InlineKeyboardMarkup(inline_keyboard=[[btn(*x) for x in r] for r in rows])
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
'wallet':('Wallet text','💰 <b>Wallet</b>\n\nBalance: <b>₹{balance}</b>\nReferrals: <b>{referrals}</b>','{balance} {referrals}'),
'referral':('Referral link text','🔗 <b>Your Referral Link</b>\n\n<code>{link}</code>\n\nReward: <b>₹{reward}</b>','{link} {reward}'),
'lb_title':('Leaderboard title','🏆 <b>TOP 10 LEADERBOARD</b>\n',''),
'lb_row':('Leaderboard row','{medal} {name}{username}\n   👥 {referrals} referrals | 💰 ₹{wallet}','{medal} {rank} {name} {username} {referrals} {wallet}'),
'lb_you':('Leaderboard "your rank" line','📍 Your rank: <b>#{rank}</b> | 👥 {referrals} referrals','{rank} {referrals}'),
'lb_empty':('Leaderboard empty text','🏆 No users yet.',''),
'wd_min':('Withdraw: low balance text','❌ Minimum withdrawal is ₹{min}. Your balance: ₹{balance}','{min} {balance}'),
'wd_refs':('Withdraw: referrals needed text','❌ You need {need} referrals. You have {have}.','{need} {have}'),
'wd_amount':('Withdraw: ask amount','💸 Send withdrawal amount (minimum ₹{min}).','{min}'),
'wd_upi':('Withdraw: ask UPI ID','💳 <b>Send your UPI ID.</b>',''),
'wd_attach':('Withdraw: ask screenshot','📎 Send screenshot/document or type SKIP.',''),
'wd_done':('Withdraw: submitted text','✅ Withdrawal #{id} submitted.\nStatus: <b>{status}</b>','{id} {status}'),
'wd_paid':('Withdraw: payment done text','✅ <b>Payment Done</b>\nWithdrawal #{id}\nAmount: ₹{amount}','{id} {amount}'),
'wd_rej':('Withdraw: rejected text','❌ <b>Withdrawal Rejected</b>\n#{id}\nRefunded: ₹{amount}\nIssue: {issue}','{id} {amount} {issue}'),
'ref_notify':('New referral notification','🎉 <b>New Referral!</b>\n\n👤 Name: <b>{name}</b>\n💰 Reward: <b>+₹{reward}</b>\n💳 Updated Balance: <b>₹{balance}</b>','{name} {reward} {balance}'),
}
BTN={'btn_verify_now':('Verify Now button','🔐 Verify Now #g'),'device_button':('Verify Device button',DEFAULTS['device_button'])}
SAMPLE={'balance':'125.00','referrals':'3','link':'https://t.me/yourbot?start=ref_123456','reward':'10','min':'50','need':'3','have':'1','id':'12','status':'Pending','name':'Rahul','username':' @rahul','rank':'5','medal':'🥇','wallet':'125.00','amount':'100.00','issue':'Invalid UPI ID'}
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

async def required_ok(uid):
    async with pool.acquire() as c: rows=await c.fetch('SELECT * FROM required_channels WHERE enabled')
    for r in rows:
        try:
            m=await bot.get_chat_member(r['chat_id'],uid)
            if m.status in ('left','kicked'): return False
        except Exception:return False
    return True
async def verify(uid):
    async with pool.acquire() as c: await c.execute('UPDATE users SET verified=TRUE,updated_at=NOW() WHERE id=$1',uid)
async def txt(key,**kw):return fmt(await setting(key,TXT[key][1]),**kw)
async def jget(key,default):
    try:
        v=json.loads(await setting(key,'') or 'null');return v if v else default
    except Exception:return default
def url_kb(btns):
    rows=[[ibtn(x['t'],x.get('s','default'),url=x['u'])] for x in btns]
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
    return await send_rich(uid,await txt('welcome'),await setting('welcome_media',''),await menu())

async def credit_ref(uid):
    async with pool.acquire() as c:
        async with c.transaction():
            r=await c.fetchrow('SELECT referred_by,referral_rewarded,verified FROM users WHERE id=$1 FOR UPDATE',uid)
            if not r or not r['verified'] or not r['referred_by'] or r['referral_rewarded']: return
            reward=Decimal(await setting('referral_amount','10')); rid=r['referred_by']
            if rid==uid:return
            newbal=await c.fetchval('UPDATE users SET wallet=wallet+$1,referrals=referrals+1 WHERE id=$2 RETURNING wallet',reward,rid)
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
    return InlineKeyboardMarkup(inline_keyboard=[[ibtn(label,st,web_app=WebAppInfo(url=WEBAPP_URL+'/verify'))]])
async def after_channels(uid,send):
    """Channels join ho chuke hain. Device verification baaki ho to Step 2 dikhao, warna verify+referral credit."""
    u=await getuser(uid)
    if u and not u['device_verified'] and await device_enabled():
        await send(await txt('device_text'),reply_markup=await device_kb());return False
    await verify(uid);await credit_ref(uid);return True

async def menu():
    # User-side menu intentionally stays limited to the original four actions.
    async with pool.acquire() as c:
        rows=await c.fetch("SELECT * FROM buttons WHERE enabled AND key IN ('withdraw','referral','wallet','leaderboard') ORDER BY sort_order,key")
    marks={'danger':'🔴 ','success':'🟢 ','primary':'🔵 ','default':''}
    bs=[btn(marks.get(r['style'],'')+r['label'],f'ui:{r["key"]}') for r in rows]
    return InlineKeyboardMarkup(inline_keyboard=[bs[i:i+2] for i in range(0,len(bs),2)]) if bs else None
async def verify_kb():
    async with pool.acquire() as c: rows=await c.fetch('SELECT * FROM required_channels WHERE enabled ORDER BY id')
    b=InlineKeyboardBuilder()
    for r in rows:
        if r['invite_link']:b.add(ibtn(f'📢 {(r["title"] or "Channel")[:28]}',url=r['invite_link']))
    l,s=parse_label(await setting('btn_verify_now',BTN['btn_verify_now'][1]));b.add(btn(l,'verify_now',s));b.adjust(1);return b.as_markup()

@dp.message(CommandStart())
async def start(m:Message):
    arg=(m.text.split(maxsplit=1)[1] if m.text and len(m.text.split(maxsplit=1))>1 else '')
    ref=int(arg[4:]) if arg.startswith('ref_') and arg[4:].isdigit() else None
    await user(m,ref)
    if not await required_ok(m.from_user.id):
        await m.answer(await txt('verify_required'),reply_markup=await verify_kb());return
    if not await after_channels(m.from_user.id,m.answer):return
    await send_welcome(m.from_user.id)

@dp.callback_query(F.data=='verify_now')
async def verify_now(q:CallbackQuery):
    if not await required_ok(q.from_user.id): await q.answer('❌ Complete required channel verification first.',show_alert=True);return
    if not await after_channels(q.from_user.id,q.message.answer):await q.answer('✅ Channels verified! Ab device verify karein.');return
    await q.answer('✅ Verified!',show_alert=True);await q.message.answer(await txt('verify_ok'),reply_markup=await menu())

@dp.callback_query(F.data.startswith('ui:'))
async def ui(q:CallbackQuery):
    uid=q.from_user.id;r=await getuser(uid)
    if not r: await q.answer('Start the bot first.',show_alert=True);return
    if not r['verified']: await q.answer('Verify first.',show_alert=True);await q.message.answer(await txt('verify_required'),reply_markup=await verify_kb());return
    k=q.data[3:];await q.answer()
    if k=='wallet':await q.message.answer(await txt('wallet',balance=f'{Decimal(r["wallet"]):.2f}',referrals=str(r['referrals'])))
    elif k=='referral':
        me=await bot.get_me();await q.message.answer(await txt('referral',link=f'https://t.me/{me.username}?start=ref_{uid}',reward=await setting('referral_amount','10')))
    elif k=='leaderboard':await leaderboard(q.message,uid)
    elif k=='verify':await q.message.answer(await txt('verify_required'),reply_markup=await verify_kb())
    elif k=='withdraw':await withdraw_start(q.message,uid)

async def leaderboard(m,uid=None):
    async with pool.acquire() as c:
        r=await c.fetch('SELECT first_name,username,referrals,wallet FROM users WHERE verified ORDER BY referrals DESC,wallet DESC,id LIMIT 10')
        me=await c.fetchrow('SELECT u.verified,u.referrals,(SELECT COUNT(*)+1 FROM users x WHERE x.verified AND (x.referrals>u.referrals OR (x.referrals=u.referrals AND x.wallet>u.wallet))) AS rank FROM users u WHERE u.id=$1',uid) if uid else None
    if not r:return await m.answer(await txt('lb_empty'))
    row=await setting('lb_row',TXT['lb_row'][1]);out=[await txt('lb_title')];medals=['🥇','🥈','🥉']
    for i,x in enumerate(r):
        name=html.escape(x['first_name'] or x['username'] or 'User');un=(' @'+html.escape(x['username'])) if x['username'] else ''
        out.append(fmt(row,medal=medals[i] if i<3 else f'{i+1}.',rank=str(i+1),name=name,username=un,referrals=str(x['referrals']),wallet=f'{Decimal(x["wallet"]):.2f}'))
    if me and me['verified']:out.append('\n'+await txt('lb_you',rank=str(me['rank']),referrals=str(me['referrals'])))
    await m.answer('\n'.join(out))

async def withdraw_start(m,uid):
    r=await getuser(uid);mn=Decimal(await setting('min_withdrawal','50'));mr=int(await setting('min_referrals','0'))
    if Decimal(r['wallet'])<mn:return await m.answer(await txt('wd_min',min=f'{mn:.2f}',balance=f'{Decimal(r["wallet"]):.2f}'))
    if r['referrals']<mr:return await m.answer(await txt('wd_refs',need=str(mr),have=str(r['referrals'])))
    await state(uid,'w_upi')
    await m.answer(await txt('wd_upi'))

async def create_withdraw(uid,amount,upi,attachment):
    async with pool.acquire() as c:
        async with c.transaction():
            r=await c.fetchrow('SELECT wallet FROM users WHERE id=$1 FOR UPDATE',uid);mn=Decimal(await setting('min_withdrawal','50'))
            if amount<mn:raise ValueError(f'Minimum withdrawal is ₹{mn:.2f}')
            if amount>Decimal(r['wallet']):raise ValueError('Insufficient wallet balance')
            await c.execute('UPDATE users SET wallet=wallet-$1 WHERE id=$2',amount,uid)
            return await c.fetchval('INSERT INTO withdrawals(user_id,amount,upi_id,attachment_file_id) VALUES($1,$2,$3,$4) RETURNING id',uid,amount,upi,attachment)
async def post_withdraw(wid):
    ch=await setting('withdrawal_channel_id','')
    if not ch:return False
    async with pool.acquire() as c:r=await c.fetchrow('SELECT w.*,u.first_name,u.username FROM withdrawals w JOIN users u ON u.id=w.user_id WHERE w.id=$1',wid)
    t=f'🚨 <b>WITHDRAWAL REQUEST DETECTED</b>\n\n🆔 Request: <code>#{wid}</code>\n👤 User: {html.escape(r["first_name"] or "User")} (@{html.escape(r["username"] or "no_username")})\n🆔 User ID: <code>{r["user_id"]}</code>\n💰 Amount: <b>₹{Decimal(r["amount"]):.2f}</b>\n💳 UPI ID: <code>{html.escape(r["upi_id"])}</code>\n📌 Status: <b>PENDING</b>'
    kb=InlineKeyboardMarkup(inline_keyboard=[[btn('✅ Payment Done',f'wd:paid:{wid}','success'),btn('❌ Reject with Issue',f'wd:reject:{wid}','danger')]])
    try:
        await bot.send_message(int(ch),t,reply_markup=kb)
        if r['attachment_file_id']:await bot.send_document(int(ch),r['attachment_file_id'],caption=f'Attachment for withdrawal #{wid}')
        return True
    except Exception as e:log.error('withdraw channel: %s',e);return False

@dp.message(Command('withdraw'))
async def wd_cmd(m:Message):
    await user(m);r=await getuser(m.from_user.id)
    if not r['verified']:return await m.answer(await txt('verify_required'),reply_markup=await verify_kb())
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
    await panel(t,'🛠 <b>ADMIN PANEL</b>',kb([[('🚀 Start Settings','ad:st'),('🎛 Menu Buttons','ad:mb')],[('📝 Texts','ad:tx'),('🔘 Other Buttons','ad:bt')],[('📢 Channels','ad:ch'),('💸 Withdrawals','ad:wds')],[('👥 Stats','ad:stats'),('🏆 Leaderboard','ad:lb')],[('🛡 Device Verify','ad:dv'),('📣 Broadcast','ad:bc')],[('⚙️ Settings','ad:set')]]))

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
        await put_rich(sc,media='');return await rich_screen(q,sc)
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
        await state(uid,'a_rbtn',{'sc':sc});await q.message.answer('➕ Button bhejein (ek line me ek button):\n\n<code>Button Text | https://link.com #g</code>\n\nColor: #r red, #g green, #b blue, #d default');return
    if act=='bdel':
        i=int(rest[0])
        if 0<=i<len(d['btns']):d['btns'].pop(i);await put_rich(sc,btns=d['btns'])
        return await rich_btns(q,sc)
    if act=='full':
        if not d['text'] and not d['media']:return 'Text ya media set karein.'
        await send_rich(uid,d['text'] or '',d['media'],await menu() if sc=='st' else url_kb(d['btns']));return
    if act=='pin' and sc=='bc':
        await put_rich('bc',pin=not d['pin']);return await rich_screen(q,sc)
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
                    msg=await send_rich(x['id'],d['text'] or '',d['media'],mk);sent+=1
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
    act=p[1]
    if act=='ren':
        await state(q.from_user.id,'a_mbren',{'key':k});await q.message.answer('✏️ Naya button naam bhejein.\nColor ke liye end me #r (red) #g (green) #b (blue) #d (default) likhein.');return
    async with pool.acquire() as c:
        if act=='tog':await c.execute('UPDATE buttons SET enabled=NOT enabled WHERE key=$1',k)
        elif act=='c':await c.execute('UPDATE buttons SET style=$1 WHERE key=$2',COL[p[2]][0],k)
        elif act in('up','dn'):
            ks=[x['key'] for x in await c.fetch('SELECT key FROM buttons ORDER BY sort_order,key')]
            if k in ks:
                i=ks.index(k);j=i-1 if act=='up' else i+1
                if 0<=j<len(ks):ks[i],ks[j]=ks[j],ks[i]
                for n,x in enumerate(ks):await c.execute('UPDATE buttons SET sort_order=$1 WHERE key=$2',(n+1)*10,x)
    await mb_one(q,k)

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
        if act=='rs':await set_setting(k,TXT[k][1])
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
            label,_=parse_label(await setting(k,BTN[k][1]));await set_setting(k,label+TAG[p[2]])
        elif act=='rs':await set_setting(k,BTN[k][1])
    label,st=parse_label(await setting(k,BTN[k][1]))
    await panel(q,f'🔘 <b>{BTN[k][0]}</b>\n\nLabel: {html.escape(label)}\nColor: {STNAME.get(st,st)}',kb([[(label,'ad:noop',st)],[('✏️ Rename',f'ad:bt:{k}:ed'),('♻️ Reset',f'ad:bt:{k}:rs')],colrow(f'ad:bt:{k}'),[('⬅ Back','ad:bt')]]))

# ---- Channels ----
async def ch_screen(t):
    async with pool.acquire() as c:r=await c.fetch('SELECT * FROM required_channels ORDER BY id')
    lines=['📢 <b>Required Channels</b>\n'];rows=[]
    for x in r:
        lines.append(f'{x["id"]}. {html.escape(x["title"] or "")} <code>{x["chat_id"]}</code>\n    {"🟢 ON" if x["enabled"] else "🔴 OFF"} | Join request: {"YES" if x["request_join"] else "NO"}')
        rows.append([(f'{"🟢" if x["enabled"] else "🔴"} #{x["id"]}',f'ad:ch:on:{x["id"]}'),(f'🙋 Request {"✅" if x["request_join"] else "❌"} #{x["id"]}',f'ad:ch:rj:{x["id"]}'),(f'🗑 #{x["id"]}',f'ad:ch:del:{x["id"]}','danger')])
    if not r:lines.append('Koi channel nahi hai.')
    rows.append([('➕ Add Channel','ad:ch:add','success')]);rows.append([('⬅ Back','ad:home')])
    await panel(t,'\n'.join(lines),kb(rows))
async def ch_route(q,p):
    if p:
        act=p[0]
        if act=='add':
            await state(q.from_user.id,'a_ch');await q.message.answer('➕ Bhejein:\n<code>CHAT_ID | Title | InviteLink</code>\n\nBot us channel me admin hona chahiye.');return
        i=int(p[1])
        async with pool.acquire() as c:
            if act=='del':await c.execute('DELETE FROM required_channels WHERE id=$1',i)
            elif act=='on':await c.execute('UPDATE required_channels SET enabled=NOT enabled WHERE id=$1',i)
            elif act=='rj':await c.execute('UPDATE required_channels SET request_join=NOT request_join WHERE id=$1',i)
    await ch_screen(q)

# ---- Withdrawals / Stats ----
async def wds_screen(t):
    async with pool.acquire() as c:r=await c.fetch('SELECT w.id,w.amount,w.status,u.first_name FROM withdrawals w JOIN users u ON u.id=w.user_id ORDER BY w.id DESC LIMIT 20')
    body='\n'.join(f'#{x["id"]} ₹{Decimal(x["amount"]):.2f} — {x["status"]} — {html.escape(x["first_name"] or "User")}' for x in r) if r else 'No withdrawals.'
    await panel(t,'💸 <b>Recent Withdrawals</b>\n\n'+body,kb([[('⬅ Back','ad:home')]]))
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
        if a=='tog':await set_setting('device_check','off' if (await setting('device_check','on'))=='on' else 'on')
        elif a=='strict':await set_setting('device_strict','off' if (await setting('device_strict','on'))=='on' else 'on')
        elif a=='prev':
            if not WEBAPP_URL:return '❌ WEBAPP_URL set nahi hai.'
            await q.message.answer(await txt('device_text'),reply_markup=await device_kb());return
    await dev_screen(q)

# ---- Numeric settings ----
SETS={'referral_amount':'Referral reward (₹)','min_withdrawal':'Min withdrawal (₹)','min_referrals':'Min referrals to withdraw','withdrawal_channel_id':'Withdrawal channel ID','ip_limit':'IP limit (accounts per IP)','device_limit':'Device limit (accounts per device)'}
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
    async with pool.acquire() as c:await c.execute('UPDATE buttons SET label=$1,style=$2 WHERE key=$3',label,st,k.strip())
    await m.answer('✅ Button updated.')
@dp.message(Command('setref'))
async def sr(m):
    if not admin(m.from_user.id):return
    try:v=Decimal(m.text.split()[1]);assert v>=0;await set_setting('referral_amount',str(v));await m.answer('✅ Referral reward updated.')
    except:await m.answer('Usage: /setref 10')
@dp.message(Command('setminwithdraw'))
async def smw(m):
    if not admin(m.from_user.id):return
    try:v=Decimal(m.text.split()[1]);assert v>0;await set_setting('min_withdrawal',str(v));await m.answer('✅ Minimum withdrawal updated.')
    except:await m.answer('Usage: /setminwithdraw 50')
@dp.message(Command('setminrefs'))
async def smr(m):
    if not admin(m.from_user.id):return
    try:v=int(m.text.split()[1]);assert v>=0;await set_setting('min_referrals',str(v));await m.answer('✅ Minimum referrals updated.')
    except:await m.answer('Usage: /setminrefs 3')
@dp.message(Command('setwithdrawchannel'))
async def swc(m):
    if not admin(m.from_user.id):return
    try:v=m.text.split()[1];int(v);await set_setting('withdrawal_channel_id',v);await m.answer('✅ Withdrawal channel saved.')
    except:await m.answer('Usage: /setwithdrawchannel -100123456789')
@dp.message(Command('setwelcome'))
async def sw(m):
    if not admin(m.from_user.id):return
    x=m.text.partition(' ')[2]
    if x:await set_setting('welcome',x);await m.answer('✅ Welcome updated.')
@dp.message(Command('addchannel'))
async def addch(m):
    if not admin(m.from_user.id):return
    p=[x.strip() for x in m.text.partition(' ')[2].split('|')]
    if len(p)<2:return await m.answer('Usage: /addchannel CHAT_ID | Title | InviteLink')
    try:cid=int(p[0])
    except:return await m.answer('Invalid chat ID')
    link=p[2] if len(p)>2 else None
    async with pool.acquire() as c:await c.execute('INSERT INTO required_channels(chat_id,title,invite_link) VALUES($1,$2,$3) ON CONFLICT(chat_id) DO UPDATE SET title=EXCLUDED.title,invite_link=EXCLUDED.invite_link,enabled=TRUE',cid,p[1],link)
    await m.answer('✅ Channel added.')

@dp.message(Command('devicecheck'))
async def dchk(m):
    if not admin(m.from_user.id):return
    p=m.text.split()
    if len(p)<2 or p[1] not in('on','off'):return await m.answer('Usage: /devicecheck on|off')
    await set_setting('device_check',p[1]);await m.answer(f'✅ Device check: {p[1]}')
@dp.message(Command('setiplimit'))
async def sil(m):
    if not admin(m.from_user.id):return
    try:v=int(m.text.split()[1]);assert v>=1;await set_setting('ip_limit',str(v));await m.answer('✅ IP limit updated (ek IP par max accounts).')
    except:await m.answer('Usage: /setiplimit 2')
@dp.message(Command('setdevicelimit'))
async def sdl(m):
    if not admin(m.from_user.id):return
    try:v=int(m.text.split()[1]);assert v>=1;await set_setting('device_limit',str(v));await m.answer('✅ Device limit updated (ek device par max accounts).')
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
    if not await required_ok(uid):return out(False,'Pehle sabhi channels join karein.',403)
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
    _,act,wid=q.data.split(':');wid=int(wid)
    async with pool.acquire() as c:r=await c.fetchrow('SELECT * FROM withdrawals WHERE id=$1 FOR UPDATE',wid)
    if not r or r['status']!='pending':return await q.answer('Already processed/not found.',show_alert=True)
    if act=='paid':
        async with pool.acquire() as c:await c.execute("UPDATE withdrawals SET status='paid',admin_id=$1,updated_at=NOW() WHERE id=$2 AND status='pending'",q.from_user.id,wid)
        await bot.send_message(r['user_id'],await txt('wd_paid',id=str(wid),amount=f'{Decimal(r["amount"]):.2f}'));await q.answer('Payment marked done.');await q.message.edit_reply_markup(reply_markup=None)
    else:
        await state(q.from_user.id,'a_reject',{'wid':wid});await q.answer();await q.message.answer(f'❌ Send rejection issue for withdrawal #{wid}.')

async def admin_flow(m,s):
    d=jd(s['data']);st=s['state'];uid=m.from_user.id
    if st=='a_reject':
        reason=(m.text or '').strip();wid=int(d['wid'])
        async with pool.acquire() as c:
            async with c.transaction():
                r=await c.fetchrow('SELECT * FROM withdrawals WHERE id=$1 FOR UPDATE',wid)
                if not r or r['status']!='pending':await clear(uid);return await m.answer('Already processed.')
                await c.execute("UPDATE withdrawals SET status='rejected',issue=$1,admin_id=$2,updated_at=NOW() WHERE id=$3",reason,uid,wid)
                await c.execute('UPDATE users SET wallet=wallet+$1 WHERE id=$2',r['amount'],r['user_id'])
        await clear(uid)
        try:await bot.send_message(r['user_id'],await txt('wd_rej',id=str(wid),amount=f'{Decimal(r["amount"]):.2f}',issue=html.escape(reason)))
        except Exception:log.warning('reject notify failed')
        return await m.answer('✅ Rejected and refunded.')
    if st=='a_media':
        med=media_of(m)
        if not med:return await m.answer('❌ Photo / video / GIF / document bhejein.')
        await put_rich(d['sc'],media=med);await clear(uid);await m.answer('✅ Media saved.');return await rich_screen(m,d['sc'])
    if st=='a_rtext':
        t=(m.html_text or '').strip()
        if not t or not m.text:return await m.answer('❌ Text bhejein.')
        old=(await get_rich(d['sc']))['text'];await put_rich(d['sc'],text=t)
        try:await m.answer(t)
        except TelegramBadRequest as e:
            await put_rich(d['sc'],text=old);return await m.answer('❌ HTML error, text save nahi hua: '+html.escape(str(e)[:150]))
        await clear(uid);await m.answer('✅ Text saved.');return await rich_screen(m,d['sc'])
    if st=='a_rbtn':
        new=[]
        for line in (m.text or '').splitlines():
            if '|' not in line:continue
            lab,sty=parse_label(line);t,u=[x.strip() for x in lab.split('|',1)]
            if t and u.startswith(('http://','https://','tg://')):new.append({'t':t[:60],'u':u,'s':sty})
        if not new:return await m.answer('❌ Format galat. Aise bhejein:\n<code>Button Text | https://link.com #g</code>')
        cur=(await get_rich(d['sc']))['btns']+new;await put_rich(d['sc'],btns=cur);await clear(uid)
        await m.answer(f'✅ {len(new)} button add hue.');return await rich_btns(m,d['sc'])
    if st=='a_mbren':
        raw=(m.text or '').strip()
        if not raw:return await m.answer('❌ Naam bhejein.')
        label,sty=parse_label(raw)
        async with pool.acquire() as c:
            if has_tag(raw):await c.execute('UPDATE buttons SET label=$1,style=$2 WHERE key=$3',label,sty,d['key'])
            else:await c.execute('UPDATE buttons SET label=$1 WHERE key=$2',label,d['key'])
        await clear(uid);await m.answer('✅ Button updated.');return await mb_one(m,d['key'])
    if st=='a_tx':
        k=d['key'];t=(m.html_text or '').strip()
        if not t or not m.text:return await m.answer('❌ Text bhejein.')
        old=await setting(k,TXT[k][1]);await set_setting(k,t)
        try:await m.answer(await txt(k,**SAMPLE))
        except TelegramBadRequest as e:
            await set_setting(k,old);return await m.answer('❌ HTML error, text save nahi hua: '+html.escape(str(e)[:150]))
        await clear(uid);await m.answer('✅ Text saved (upar preview hai).');return await tx_route(m,[k])
    if st=='a_bt':
        raw=(m.text or '').strip()
        if not raw:return await m.answer('❌ Naam bhejein.')
        await set_setting(d['key'],raw);await clear(uid);await m.answer('✅ Button updated.');return await bt_route(m,[d['key']])
    if st=='a_ch':
        p=[x.strip() for x in (m.text or '').split('|')]
        if len(p)<2:return await m.answer('CHAT_ID | Title | InviteLink')
        try:cid=int(p[0])
        except:return await m.answer('Invalid chat ID')
        async with pool.acquire() as c:await c.execute('INSERT INTO required_channels(chat_id,title,invite_link) VALUES($1,$2,$3) ON CONFLICT(chat_id) DO UPDATE SET title=EXCLUDED.title,invite_link=EXCLUDED.invite_link,enabled=TRUE',cid,p[1],p[2] if len(p)>2 else None)
        await clear(uid);await m.answer('✅ Channel added.');return await ch_screen(m)
    if st=='a_set':
        k=d['key'];v=(m.text or '').strip()
        try:
            if k=='referral_amount':x=Decimal(v);assert x>=0
            elif k=='min_withdrawal':x=Decimal(v);assert x>0
            elif k=='min_referrals':x=int(v);assert x>=0
            elif k in('ip_limit','device_limit'):x=int(v);assert x>=1
            else:x=int(v)
        except Exception:return await m.answer('❌ Valid value bhejein.')
        await set_setting(k,str(x));await clear(uid);await m.answer('✅ Saved.');return await set_screen(m)

@dp.message(F.content_type.in_({'text','photo','document','video','animation'}))
async def flow(m:Message):
    uid=m.from_user.id;s=await state(uid)
    if not s:return
    if uid in ADMINS and str(s['state']).startswith('a_'):await admin_flow(m,s);return
    d=jd(s['data'])
    if s['state']=='w_upi':
        if not m.text:return await m.answer('❌ Send your UPI ID as text.')
        d['upi']=m.text.strip();await state(uid,'w_amount',d);return await m.answer(await txt('wd_amount',min=f'{Decimal(await setting("min_withdrawal","50")):.2f}'))
    if s['state']=='w_amount':
        try:a=Decimal((m.text or '').replace(',','').strip());assert a>0
        except Exception:return await m.answer('❌ Send a valid amount, e.g. 100.')
        d['amount']=str(a)
        try:wid=await create_withdraw(uid,a,d['upi'],None)
        except ValueError as e:return await m.answer('❌ '+html.escape(str(e)))
        await clear(uid);ok=await post_withdraw(wid)
        await m.answer(await txt('wd_done',id=str(wid),status='Pending' if ok else 'Created - admin channel not configured'))

@dp.chat_join_request()
async def join_req(e):
    async with pool.acquire() as c:r=await c.fetchrow('SELECT request_join FROM required_channels WHERE chat_id=$1 AND enabled',e.chat.id)
    if r and r['request_join']:
        try:await bot.approve_chat_join_request(e.chat.id,e.from_user.id)
        except Exception:log.exception('join request approval failed')

async def main():
    await db_init();runner=await start_web();me=await bot.get_me();log.info('Started @%s',me.username)
    try:await dp.start_polling(bot,allowed_updates=dp.resolve_used_update_types())
    finally:await runner.cleanup();await pool.close();await bot.session.close()
if __name__=='__main__':asyncio.run(main())
