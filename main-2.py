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
BUTTONS=[('withdraw','💸 Withdraw','success',10),('referral','🔗 My Referral Link','primary',20),('wallet','💰 My Wallet','default',30),('leaderboard','🏆 Leaderboard','primary',40),('verify','✅ Verify Account','success',50)]
STYLE={'#r':'danger','#g':'success','#b':'primary','#d':'default'}

def btn(text,data,style='default'):
    try:return InlineKeyboardButton(text=text,callback_data=data,style=style)
    except TypeError:return InlineKeyboardButton(text=text,callback_data=data)

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
    try:await bot.send_message(rid,f'🎉 <b>New Referral!</b>\n\n👤 Name: <b>{name}</b>\n💰 Reward: <b>+₹{reward:.2f}</b>\n💳 Updated Balance: <b>₹{Decimal(newbal):.2f}</b>')
    except Exception:log.warning('referral notify failed for %s',rid)

async def device_enabled():
    return bool(WEBAPP_URL) and (await setting('device_check','on'))=='on'
async def device_kb():
    label,st=parse_label(await setting('device_button',DEFAULTS['device_button']));url=WEBAPP_URL+'/verify'
    try:b=InlineKeyboardButton(text=label,web_app=WebAppInfo(url=url),style=st)
    except TypeError:b=InlineKeyboardButton(text=label,web_app=WebAppInfo(url=url))
    return InlineKeyboardMarkup(inline_keyboard=[[b]])
async def after_channels(uid,send):
    """Channels join ho chuke hain. Device verification baaki ho to Step 2 dikhao, warna verify+referral credit."""
    u=await getuser(uid)
    if u and not u['device_verified'] and await device_enabled():
        await send(await setting('device_text',DEFAULTS['device_text']),reply_markup=await device_kb());return False
    await verify(uid);await credit_ref(uid);return True

async def menu():
    async with pool.acquire() as c: rows=await c.fetch('SELECT * FROM buttons WHERE enabled ORDER BY sort_order')
    b=InlineKeyboardBuilder()
    for r in rows:b.add(btn(r['label'],f'ui:{r["key"]}',r['style']))
    b.adjust(2);return b.as_markup()
async def verify_kb():
    async with pool.acquire() as c: rows=await c.fetch('SELECT * FROM required_channels WHERE enabled')
    b=InlineKeyboardBuilder()
    for r in rows:
        if r['invite_link']:b.add(InlineKeyboardButton(text=f'📢 {r["title"][:28]}',url=r['invite_link']))
    b.add(btn('🔐 Verify Now','verify_now','success'));b.adjust(1);return b.as_markup()

@dp.message(CommandStart())
async def start(m:Message):
    arg=(m.text.split(maxsplit=1)[1] if m.text and len(m.text.split(maxsplit=1))>1 else '')
    ref=int(arg[4:]) if arg.startswith('ref_') and arg[4:].isdigit() else None
    await user(m,ref)
    if not await required_ok(m.from_user.id):
        await m.answer('🔐 <b>Verification required</b>\n\nJoin the required channels and tap Verify Now.\n\nStep 2 me device/IP verification hogi taaki duplicate referral na ho sake.',reply_markup=await verify_kb());return
    if not await after_channels(m.from_user.id,m.answer):return
    await m.answer(await setting('welcome',DEFAULTS['welcome']),reply_markup=await menu())

@dp.callback_query(F.data=='verify_now')
async def verify_now(q:CallbackQuery):
    if not await required_ok(q.from_user.id): await q.answer('❌ Complete required channel verification first.',show_alert=True);return
    if not await after_channels(q.from_user.id,q.message.answer):await q.answer('✅ Channels verified! Ab device verify karein.');return
    await q.answer('✅ Verified!',show_alert=True);await q.message.answer('✅ <b>Verification successful.</b>',reply_markup=await menu())

@dp.callback_query(F.data.startswith('ui:'))
async def ui(q:CallbackQuery):
    if not await getuser(q.from_user.id): await q.answer('Start the bot first.',show_alert=True);return
    if not (await getuser(q.from_user.id))['verified']: await q.answer('Verify first.',show_alert=True);await q.message.answer('🔐 Verification required.',reply_markup=await verify_kb());return
    k=q.data[3:];await q.answer()
    if k=='wallet':
        r=await getuser(q.from_user.id);await q.message.answer(f'💰 <b>Wallet</b>\n\nBalance: <b>₹{Decimal(r["wallet"]):.2f}</b>\nReferrals: <b>{r["referrals"]}</b>')
    elif k=='referral':
        me=await bot.get_me();await q.message.answer(f'🔗 <b>Your Referral Link</b>\n\n<code>https://t.me/{me.username}?start=ref_{q.from_user.id}</code>\n\nReward: <b>₹{await setting("referral_amount","10")}</b>')
    elif k=='leaderboard':await leaderboard(q.message)
    elif k=='verify':await q.message.answer('🔐 Verify your Telegram account membership.',reply_markup=await verify_kb())
    elif k=='withdraw':await withdraw_start(q.message)

async def leaderboard(m):
    async with pool.acquire() as c:r=await c.fetch('SELECT first_name,username,referrals,wallet FROM users WHERE verified ORDER BY referrals DESC,wallet DESC LIMIT 10')
    if not r:return await m.answer('🏆 No users yet.')
    out=['🏆 <b>TOP 10</b>\n']
    for i,x in enumerate(r,1):
        n=html.escape(x['first_name'] or x['username'] or 'User');u=f' @{html.escape(x["username"])}' if x['username'] else ''
        out.append(f'{i}. {n}{u}\n   👥 {x["referrals"]} referrals | 💰 ₹{Decimal(x["wallet"]):.2f}')
    await m.answer('\n'.join(out))

async def withdraw_start(m):
    r=await getuser(m.from_user.id);mn=Decimal(await setting('min_withdrawal','50'));mr=int(await setting('min_referrals','0'))
    if Decimal(r['wallet'])<mn:return await m.answer(f'❌ Minimum withdrawal is ₹{mn:.2f}. Your balance: ₹{Decimal(r["wallet"]):.2f}')
    if r['referrals']<mr:return await m.answer(f'❌ You need {mr} referrals. You have {r["referrals"]}.')
    await state(m.from_user.id,'w_amount');await m.answer(f'💸 Send withdrawal amount (minimum ₹{mn:.2f}).')

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
    await user(m)
    r=await getuser(m.from_user.id)
    if not r['verified']:return await m.answer('🔐 Verify first.',reply_markup=await verify_kb())
    await withdraw_start(m)
@dp.message(Command('leaderboard'))
async def lb_cmd(m:Message):await user(m);await leaderboard(m)
@dp.message(Command('cancel'))
async def cancel(m:Message):await clear(m.from_user.id);await m.answer('✅ Cancelled.',reply_markup=await menu())

# ---------------- ADMIN ----------------

def admin(uid):return uid in ADMINS
@dp.message(Command('admin'))
async def admin_cmd(m:Message):
    if not admin(m.from_user.id):return await m.answer('⛔ Access denied.')
    await admin_panel(m)
async def admin_panel(m):
    b=InlineKeyboardBuilder()
    for t,d in [('⚙️ Settings','adm:set'),('🎛 Buttons','adm:buttons'),('📢 Channels','adm:channels'),('💸 Withdrawals','adm:wds'),('👥 Stats','adm:stats'),('🏆 Leaderboard','adm:lb'),('🛡 Device Verify','adm:device'),('📣 Broadcast','adm:broadcast')]:b.add(btn(t,d))
    b.adjust(2);await m.answer('🛠 <b>ADMIN PANEL</b>',reply_markup=b.as_markup())
@dp.callback_query(F.data.startswith('adm:'))
async def adm(q:CallbackQuery):
    if not admin(q.from_user.id):return await q.answer('Denied',show_alert=True)
    a=q.data[4:];await q.answer()
    if a=='set':await admin_settings(q.message)
    elif a=='buttons':await admin_buttons(q.message)
    elif a=='channels':await admin_channels(q.message)
    elif a=='wds':await admin_wds(q.message)
    elif a=='stats':await stats(q.message)
    elif a=='lb':await leaderboard(q.message)
    elif a=='device':await admin_device(q.message)
    elif a=='broadcast':await state(q.from_user.id,'a_broadcast');await q.message.answer('📣 Send broadcast text now.')
async def admin_device(m):
    on=(await setting('device_check','on'))=='on';stt=(await setting('device_strict','on'))=='on'
    b=InlineKeyboardBuilder()
    b.add(btn('✏️ Edit Text','dv:text'),btn('✏️ Edit Button','dv:btn'),btn(('🟢 ' if on else '🔴 ')+'Device Check','dv:toggle'),btn(('🟢 ' if stt else '🔴 ')+'Strict Match','dv:strict'),btn('👁 Preview','dv:preview'),btn('♻️ Reset Default','dv:reset'));b.adjust(2)
    await m.answer(f'🛡 <b>Device Verification</b>\n\nStatus: <b>{"ON" if on else "OFF"}</b> | Strict match: <b>{"ON" if stt else "OFF"}</b> | WEBAPP_URL: {"set" if WEBAPP_URL else "NOT SET"}\nIP limit: {await setting("ip_limit")} | Device limit: {await setting("device_limit")}\n\n<b>Text:</b>\n{html.escape(await setting("device_text",DEFAULTS["device_text"]))}\n\n<b>Button:</b> {html.escape(await setting("device_button",DEFAULTS["device_button"]))}',reply_markup=b.as_markup())
@dp.callback_query(F.data.startswith('dv:'))
async def dv(q):
    if not admin(q.from_user.id):return await q.answer('Denied',show_alert=True)
    a=q.data[3:];uid=q.from_user.id
    if a=='text':await state(uid,'a_devtext');await q.answer();return await q.message.answer('✏️ Naya device verification text bhejein (HTML allowed: <b>, <i>, <code>).')
    if a=='btn':await state(uid,'a_devbtn');await q.answer();return await q.message.answer('✏️ Naya button label bhejein. Color: #r red, #g green, #b blue, #d default. Eg: 🛡 Verify Now #g')
    if a=='toggle':await set_setting('device_check','off' if (await setting('device_check','on'))=='on' else 'on')
    elif a=='strict':await set_setting('device_strict','off' if (await setting('device_strict','on'))=='on' else 'on')
    elif a=='reset':await set_setting('device_text',DEFAULTS['device_text']);await set_setting('device_button',DEFAULTS['device_button'])
    elif a=='preview':
        await q.answer()
        if not WEBAPP_URL:return await q.message.answer('❌ WEBAPP_URL set nahi hai.')
        return await q.message.answer(await setting('device_text',DEFAULTS['device_text']),reply_markup=await device_kb())
    await q.answer('Updated');await admin_device(q.message)
async def admin_settings(m):await m.answer(f'⚙️ <b>Settings</b>\n\nReferral: ₹{await setting("referral_amount")}\nMin withdrawal: ₹{await setting("min_withdrawal")}\nMin referrals: {await setting("min_referrals")}\nWithdrawal channel: <code>{html.escape(await setting("withdrawal_channel_id","NOT SET"))}</code>\nDevice check: {await setting("device_check")} | IP limit: {await setting("ip_limit")} | Device limit: {await setting("device_limit")} | WEBAPP_URL: {"set" if WEBAPP_URL else "NOT SET"}\n\n/devicecheck on|off\n/setiplimit 2\n/setdevicelimit 1\n/resetdevice USER_ID\n/setref 10\n/setminwithdraw 50\n/setminrefs 3\n/setwithdrawchannel -100123456789\n/setwelcome text')
async def admin_buttons(m):
    async with pool.acquire() as c:r=await c.fetch('SELECT * FROM buttons ORDER BY sort_order')
    b=InlineKeyboardBuilder();lines=['🎛 <b>Button Manager</b>\n']
    for x in r:
        lines.append(f'<code>{x["key"]}</code> → {html.escape(x["label"])} [{x["style"]}]')
        b.add(btn('✏️ '+x['key'],f'eb:{x["key"]}'),btn(('🟢 ' if x['enabled'] else '🔴 ')+x['key'],f'tb:{x["key"]}'))
    b.adjust(2);lines.append('\n/setbutton key|New Name #g');await m.answer('\n'.join(lines),reply_markup=b.as_markup())
@dp.callback_query(F.data.startswith('eb:'))
async def eb(q):
    if not admin(q.from_user.id):return
    k=q.data[3:];await state(q.from_user.id,'a_button',{'key':k});await q.answer();await q.message.answer('Send new label. #r red, #g green, #b blue, #d default.')
@dp.callback_query(F.data.startswith('tb:'))
async def tb(q):
    if not admin(q.from_user.id):return
    async with pool.acquire() as c:await c.execute('UPDATE buttons SET enabled=NOT enabled WHERE key=$1',q.data[3:])
    await q.answer('Updated');await admin_buttons(q.message)
@dp.message(Command('setbutton'))
async def setbutton(m):
    if not admin(m.from_user.id):return
    raw=m.text.partition(' ')[2]
    if '|' not in raw:return await m.answer('Usage: /setbutton key|Name #g')
    k,v=raw.split('|',1);label,st=parse_label(v)
    async with pool.acquire() as c:await c.execute('UPDATE buttons SET label=$1,style=$2 WHERE key=$3',label,st,k.strip())
    await m.answer('✅ Button updated.')
async def admin_channels(m):
    async with pool.acquire() as c:r=await c.fetch('SELECT * FROM required_channels ORDER BY id')
    b=InlineKeyboardBuilder();lines=['📢 <b>Required Channels</b>\n']
    for x in r:lines.append(f'{x["id"]}. {html.escape(x["title"])} <code>{x["chat_id"]}</code>');b.add(btn('🗑 '+str(x['id']),f'dc:{x["id"]}','danger'))
    b.add(btn('➕ Add Channel','ac','success'));b.adjust(3);lines.append('\n/addchannel CHAT_ID | Title | InviteLink');await m.answer('\n'.join(lines),reply_markup=b.as_markup())
@dp.callback_query(F.data=='ac')
async def ac(q):
    if not admin(q.from_user.id):return
    await state(q.from_user.id,'a_channel');await q.answer();await q.message.answer('Send CHAT_ID | Title | InviteLink')
@dp.callback_query(F.data.startswith('dc:'))
async def dc(q):
    if not admin(q.from_user.id):return
    async with pool.acquire() as c:await c.execute('DELETE FROM required_channels WHERE id=$1',int(q.data[3:]))
    await q.answer('Deleted');await admin_channels(q.message)
async def admin_wds(m):
    async with pool.acquire() as c:r=await c.fetch('SELECT w.id,w.amount,w.status,u.first_name FROM withdrawals w JOIN users u ON u.id=w.user_id ORDER BY w.id DESC LIMIT 20')
    await m.answer('💸 <b>Recent Withdrawals</b>\n\n'+'\n'.join(f'#{x["id"]} ₹{Decimal(x["amount"]):.2f} — {x["status"]} — {html.escape(x["first_name"] or "User")}' for x in r) if r else 'No withdrawals.')
async def stats(m):
    async with pool.acquire() as c:
        total=await c.fetchval('SELECT COUNT(*) FROM users');ver=await c.fetchval('SELECT COUNT(*) FROM users WHERE verified');pend=await c.fetchval("SELECT COUNT(*) FROM withdrawals WHERE status='pending'");paid=await c.fetchval("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status='paid'");dev=await c.fetchval('SELECT COUNT(*) FROM users WHERE device_verified');blk=await c.fetchval("SELECT COUNT(*) FROM device_logs WHERE status='blocked'")
    await m.answer(f'👥 <b>Stats</b>\n\nUsers: {total}\nVerified: {ver}\nDevice verified: {dev}\nBlocked attempts: {blk}\nPending withdrawals: {pend}\nPaid: ₹{Decimal(paid):.2f}')

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
    try:await bot.send_message(uid,'✅ <b>Device verified!</b>\n\n'+await setting('welcome',DEFAULTS['welcome']),reply_markup=await menu())
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
        await bot.send_message(r['user_id'],f'✅ <b>Payment Done</b>\nWithdrawal #{wid}\nAmount: ₹{Decimal(r["amount"]):.2f}');await q.answer('Payment marked done.');await q.message.edit_reply_markup(reply_markup=None)
    else:
        await state(q.from_user.id,'a_reject',{'wid':wid});await q.answer();await q.message.answer(f'❌ Send rejection issue for withdrawal #{wid}.')

async def admin_flow(m,s):
    d=dict(s['data'] or {});st=s['state']
    if st=='a_reject':
        reason=(m.text or '').strip();wid=int(d['wid'])
        async with pool.acquire() as c:
            async with c.transaction():
                r=await c.fetchrow('SELECT * FROM withdrawals WHERE id=$1 FOR UPDATE',wid)
                if not r or r['status']!='pending':await clear(m.from_user.id);return await m.answer('Already processed.')
                await c.execute("UPDATE withdrawals SET status='rejected',issue=$1,admin_id=$2,updated_at=NOW() WHERE id=$3",reason,m.from_user.id,wid)
                await c.execute('UPDATE users SET wallet=wallet+$1 WHERE id=$2',r['amount'],r['user_id'])
        await clear(m.from_user.id);await bot.send_message(r['user_id'],f'❌ <b>Withdrawal Rejected</b>\n#{wid}\nRefunded: ₹{Decimal(r["amount"]):.2f}\nIssue: {html.escape(reason)}');return await m.answer('✅ Rejected and refunded.')
    if st=='a_devtext':
        t=(m.html_text or m.text or '').strip()
        if not t:return await m.answer('❌ Text bhejein.')
        await set_setting('device_text',t);await clear(m.from_user.id);await m.answer('✅ Device verification text updated.');return await admin_device(m)
    if st=='a_devbtn':
        label,style=parse_label(m.text or '')
        if not (m.text or '').strip():return await m.answer('❌ Label bhejein.')
        raw=(m.text or '').strip();await set_setting('device_button',raw);await clear(m.from_user.id);await m.answer('✅ Button updated.');return await admin_device(m)
    if st=='a_button':
        label,style=parse_label(m.text or 'Button')
        async with pool.acquire() as c:await c.execute('UPDATE buttons SET label=$1,style=$2 WHERE key=$3',label,style,d['key'])
        await clear(m.from_user.id);await m.answer('✅ Button updated.');return await admin_buttons(m)
    if st=='a_channel':
        p=[x.strip() for x in (m.text or '').split('|')]
        if len(p)<2:return await m.answer('CHAT_ID | Title | InviteLink')
        try:cid=int(p[0])
        except:return await m.answer('Invalid chat ID')
        async with pool.acquire() as c:await c.execute('INSERT INTO required_channels(chat_id,title,invite_link) VALUES($1,$2,$3) ON CONFLICT(chat_id) DO UPDATE SET title=EXCLUDED.title,invite_link=EXCLUDED.invite_link,enabled=TRUE',cid,p[1],p[2] if len(p)>2 else None)
        await clear(m.from_user.id);return await m.answer('✅ Channel added.')
    if st=='a_broadcast':
        text=m.html_text or m.text or '';await clear(m.from_user.id)
        async with pool.acquire() as c:ids=await c.fetch('SELECT id FROM users WHERE verified')
        sent=fail=0
        for x in ids:
            try:await bot.send_message(x['id'],text);sent+=1;await asyncio.sleep(.04)
            except TelegramRetryAfter as e:await asyncio.sleep(e.retry_after)
            except (TelegramForbiddenError,TelegramBadRequest):fail+=1
            except Exception:fail+=1
        return await m.answer(f'📣 Broadcast done. Sent: {sent}, Failed: {fail}')

@dp.chat_join_request()
async def join_req(e):
    async with pool.acquire() as c:r=await c.fetchrow('SELECT request_join FROM required_channels WHERE chat_id=$1 AND enabled',e.chat.id)
    if r and r['request_join']:
        try:await bot.approve_chat_join_request(e.chat.id,e.from_user.id)
        except Exception:log.exception('join request approval failed')

@dp.message(F.content_type.in_({'text','photo','document'}))
async def flow(m:Message):
    if m.from_user.id in ADMINS:
        s=await state(m.from_user.id)
        if s and str(s['state']).startswith('a_'):await admin_flow(m,s);return
    s=await state(m.from_user.id)
    if not s:return
    d=dict(s['data'] or {})
    if s['state']=='w_amount':
        try:a=Decimal((m.text or '').replace(',','').strip());assert a>0
        except Exception:return await m.answer('❌ Send a valid amount, e.g. 100.')
        d['amount']=str(a);await state(m.from_user.id,'w_upi',d);return await m.answer('💳 Send your UPI ID.')
    if s['state']=='w_upi':
        if not m.text:return await m.answer('❌ Send UPI ID as text.')
        d['upi']=m.text.strip();await state(m.from_user.id,'w_attachment',d);return await m.answer('📎 Send screenshot/document or type SKIP.')
    if s['state']=='w_attachment':
        f=m.photo[-1].file_id if m.photo else (m.document.file_id if m.document else None)
        if not f and (m.text or '').strip().upper()!='SKIP':return await m.answer('Send photo/document or SKIP.')
        try:wid=await create_withdraw(m.from_user.id,Decimal(d['amount']),d['upi'],f)
        except ValueError as e:await clear(m.from_user.id);return await m.answer('❌ '+html.escape(str(e)))
        await clear(m.from_user.id);ok=await post_withdraw(wid)
        await m.answer(f'✅ Withdrawal #{wid} submitted.\nStatus: <b>{"Pending" if ok else "Created - admin channel not configured"}</b>')

async def main():
    await db_init();runner=await start_web();me=await bot.get_me();log.info('Started @%s',me.username)
    try:await dp.start_polling(bot,allowed_updates=dp.resolve_used_update_types())
    finally:await runner.cleanup();await pool.close();await bot.session.close()
if __name__=='__main__':asyncio.run(main())
