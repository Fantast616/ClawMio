import asyncio
import hashlib
import hmac
import io
import json
import mimetypes
import os
import secrets
import time
from decimal import Decimal
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path

import qrcode
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .store import Store
from .ma import ManagedAgent, MAError
from .runtime import Runtime
from .billing import micros
from .bot_api import router as bot_router
from . import schedules
from clawmio.paths import assets
from clawmio.locking import exclusive

ROOT = Path(__file__).resolve().parent.parent
ASSETS = assets()
mimetypes.add_type('application/javascript', '.js')
mimetypes.add_type('text/css', '.css')
load_dotenv(Path(os.getenv('CLAWMIO_HOME', str(ROOT))) / '.env')


@asynccontextmanager
async def lifespan(app):
    if not os.getenv('ADMIN_PASSWORD') or not os.getenv('COOKIE_SECRET'):
        raise RuntimeError('请先配置 .env 中的 ADMIN_PASSWORD 和 COOKIE_SECRET')
    db_path = Path(os.getenv('DATABASE_PATH', str(ROOT / 'data/claw.db'))).resolve()
    with exclusive(str(db_path) + '.worker.lock'):
        db = Store(str(db_path))
        app.state.db = db
        app.state.runtime = Runtime(db, ManagedAgent())
        try:
            await app.state.runtime.boot()
            yield
        finally:
            await app.state.runtime.close()
            db.db.close()


app = FastAPI(title='Claw × Managed Agent', lifespan=lifespan, docs_url=None, redoc_url=None)
app.include_router(bot_router)
app.mount('/static', StaticFiles(directory=ASSETS / 'static'), name='static')
attempts = {}


def cookie_value():
    expiry = str(int(time.time()) + 43200)
    sig = hmac.new(os.environ['COOKIE_SECRET'].encode(), expiry.encode(), hashlib.sha256).hexdigest()
    return expiry + '.' + sig


def authorized(request):
    try:
        expiry, sig = request.cookies.get('claw_admin', '').split('.')
        expect = hmac.new(os.environ['COOKIE_SECRET'].encode(), expiry.encode(), hashlib.sha256).hexdigest()
        return int(expiry) > time.time() and hmac.compare_digest(sig, expect)
    except (ValueError, KeyError):
        return False


@app.middleware('http')
async def security(request, call_next):
    if request.url.path.startswith('/api/') and not request.url.path.startswith('/api/bot/'):
        if request.method != 'GET' and request.headers.get('x-claw-request') != '1':
            return JSONResponse({'detail':'请求验证失败'}, status_code=403)
        if not request.url.path.startswith(('/api/public/', '/api/login')) and not authorized(request):
            return JSONResponse({'detail':'请先登录管理后台'}, status_code=401)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'"
    return response


@app.exception_handler(MAError)
async def ma_error(request, exc):
    return JSONResponse({'detail':str(exc)}, status_code=502)


def db(): return app.state.db
def rt(): return app.state.runtime
def bot_or_404(bot_id):
    bot = db().one('SELECT * FROM bots WHERE id=?',(bot_id,))
    if not bot: raise HTTPException(404,'Bot 不存在')
    return bot


@app.get('/')
@app.get('/bind/{ticket}')
async def home(ticket=None):
    return FileResponse(ASSETS / 'static/index.html')


class Login(BaseModel):
    password: str = Field(max_length=256)


@app.post('/api/login')
async def login(body: Login, request: Request):
    address = request.client.host
    now = time.time()
    history = [t for t in attempts.get(address,[]) if now-t < 300]
    attempts[address] = history
    if len(history) >= 10: raise HTTPException(429,'尝试过多，请 5 分钟后重试')
    if not secrets.compare_digest(body.password,os.environ['ADMIN_PASSWORD']):
        history.append(now)
        raise HTTPException(401,'管理密码不正确')
    attempts.pop(address,None)
    response = JSONResponse({'ok':True})
    response.set_cookie('claw_admin',cookie_value(),httponly=True,samesite='strict',secure=request.url.scheme=='https',max_age=43200)
    return response


@app.post('/api/logout')
async def logout():
    response = JSONResponse({'ok':True})
    response.delete_cookie('claw_admin')
    return response


@app.get('/api/overview')
async def overview(archived: bool = False):
    bots = db().rows('''SELECT id,name,status,agent_id,environment_id,session_id,user_id,enabled,error,created_at,
        memory_store_id,memory_state,memory_error,session_memory_id,budget_micro,spent_micro,billing_error,archived,
        (SELECT COUNT(*) FROM bot_sessions s WHERE s.bot_id=bots.id) AS session_count
        FROM bots WHERE archived=? ORDER BY created_at DESC''',(int(archived),))
    jobs = db().rows('SELECT j.*, b.name AS bot_name,c.id AS charge_id,c.amount_micro AS charge_micro FROM jobs j JOIN bots b ON b.id=j.bot_id LEFT JOIN charges c ON c.job_id=j.id ORDER BY j.id DESC LIMIT 100')
    for job in jobs:
        job['web_search_calls'] = db().one('SELECT COUNT(*) AS n FROM job_tool_calls WHERE job_id=?',(job['id'],))['n']
        job['attachments'] = [{k:a.get(k) for k in ('type','filename','status','file_id','mounted_path')} for a in json.loads(job['attachments'])]
        job['artifacts'] = [{k:a.get(k) for k in ('filename','status','file_id')} for a in json.loads(job['artifacts'])]
    return {'bots':bots,'jobs':jobs,
        'workspace':os.getenv('BAILIAN_WORKSPACE_ID'),'configured':bool(os.getenv('DASHSCOPE_API_KEY'))}


@app.get('/api/resources')
async def resources():
    agents,environments = await asyncio.gather(rt().ma.list_all('/agents'),rt().ma.list_all('/environments'))
    return {'agents':[{'id':a['id'],'name':a.get('name',a['id'])} for a in agents],
        'environments':[{'id':e['id'],'name':e.get('name',e['id'])} for e in environments]}


@app.get('/api/jobs/{job_id}')
async def job_detail(job_id: int):
    job=db().one('SELECT j.*,b.name AS bot_name,c.id AS charge_id,c.amount_micro AS charge_micro FROM jobs j JOIN bots b ON b.id=j.bot_id LEFT JOIN charges c ON c.job_id=j.id WHERE j.id=?',(job_id,))
    if not job:raise HTTPException(404,'执行记录不存在')
    job['attachments']=[{k:a.get(k) for k in ('type','filename','status','file_id','mounted_path')} for a in json.loads(job['attachments'])]
    job['artifacts']=[{k:a.get(k) for k in ('filename','status','file_id')} for a in json.loads(job['artifacts'])]
    job['web_search_calls']=db().one('SELECT COUNT(*) AS n FROM job_tool_calls WHERE job_id=?',(job_id,))['n']
    return job


@app.get('/api/schedules')
async def admin_schedules(bot_id: str='', include_deleted: bool=False):
    rows=db().rows('''SELECT s.*,b.name AS bot_name FROM schedules s JOIN bots b ON b.id=s.bot_id
        WHERE (?='' OR s.bot_id=?) AND (? OR s.deleted=0) ORDER BY s.created_at DESC''',(bot_id,bot_id,include_deleted))
    items=[]
    for row in rows:
        item=schedules.view(row)
        item['last_run']=db().one('SELECT id,status,session_id,error,created_at FROM jobs WHERE schedule_id=? ORDER BY id DESC LIMIT 1',(row['id'],))
        items.append(item)
    return {'items':items,'bots':db().rows('SELECT id,name FROM bots ORDER BY name')}


@app.get('/api/schedules/{schedule_id}/runs')
async def admin_schedule_runs(schedule_id: str, offset: int=0):
    row=db().one('SELECT * FROM schedules WHERE id=?',(schedule_id,))
    if not row:raise HTTPException(404,'定时任务不存在')
    return {'schedule':schedules.view(row),**schedules.runs(db(),schedule_id,offset)}


@app.delete('/api/schedules/{schedule_id}')
async def admin_delete_schedule(schedule_id: str):
    row=db().one('SELECT * FROM schedules WHERE id=?',(schedule_id,))
    if not row:raise HTTPException(404,'定时任务不存在')
    async with rt().lock(row['bot_id']):
        return schedules.remove(db(),row)


@app.get('/api/session-usage')
async def session_usage():
    # Explicit refresh only. Never expose the session's environment or credentials.
    bots = db().rows("SELECT id,session_id FROM bots WHERE session_id!=''")
    semaphore = asyncio.Semaphore(5)

    async def query(bot):
        result = {'bot_id':bot['id'], 'session_id':bot['session_id']}
        async with semaphore:
            try:
                session = await rt().ma.request('GET',f"/sessions/{bot['session_id']}")
                usage, stats = session.get('usage') or {}, session.get('stats') or {}
                def number(source, name):
                    value = source.get(name)
                    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None
                result.update(
                    usage={k:number(usage,k) for k in ('input_tokens','output_tokens','cache_read_input_tokens','cache_creation_input_tokens')},
                    stats={k:number(stats,k) for k in ('active_seconds','duration_seconds')},
                    fetched_at=datetime.now(timezone.utc).isoformat(), error=None)
            except MAError as exc:
                result['error'] = str(exc)
        return result

    return {'items':await asyncio.gather(*(query(bot) for bot in bots))}


class NewBot(BaseModel):
    name: str = Field(min_length=1,max_length=80)
    agent_id: str = Field(default='',max_length=100,pattern=r'^[a-zA-Z0-9_-]*$')
    environment_id: str = Field(default='',max_length=100,pattern=r'^[a-zA-Z0-9_-]*$')
    budget: Decimal | None = Field(default=None,ge=0,le=1000000,decimal_places=6)


@app.post('/api/bots')
async def new_bot(body: NewBot):
    bot_id = secrets.token_hex(12)
    db().execute('INSERT INTO bots(id,name,agent_id,environment_id) VALUES(?,?,?,?)',(bot_id,body.name,body.agent_id,body.environment_id))
    if body.budget is not None:
        db().update('bots','id',bot_id,budget_micro=micros(body.budget))
    rt().ensure(bot_id)
    async with rt().lock(bot_id):
        try:
            await rt().ensure_memory(bot_or_404(bot_id))
        except Exception:
            pass  # Keep the local Bot visible with its actionable memory status.
    return {'id':bot_id,'memory_error':bot_or_404(bot_id)['memory_error']}


class BudgetConfig(BaseModel):
    budget: Decimal | None = Field(default=None,ge=0,le=1000000,decimal_places=6)


@app.post('/api/bots/{bot_id}/budget')
async def set_budget(bot_id: str, body: BudgetConfig):
    async with rt().lock(bot_id):
        bot_or_404(bot_id)
        db().update('bots','id',bot_id,budget_micro=micros(body.budget) if body.budget is not None else None,billing_error='')
        rt().wakeups.setdefault(bot_id,asyncio.Event()).set()
    return {'ok':True}


@app.get('/api/bots/{bot_id}/access')
async def bot_access(bot_id: str):
    bot_or_404(bot_id)
    bot=db().ensure_api_token(bot_id)
    return {'token':bot['api_token'],'base_url':os.getenv('CLAW_API_BASE_URL','').rstrip('/'),'expires_at':None}


class AgentPrice(BaseModel):
    cache_mode: str = Field(default='implicit',pattern=r'^(implicit|explicit)$')
    cache_creation_price: Decimal | None = Field(default=None,ge=0,le=1000000,decimal_places=6)
    web_search_price: Decimal | None = Field(default=Decimal('0.03'),ge=0,le=1000000,decimal_places=6)
    input_price: Decimal = Field(ge=0,le=1000000,decimal_places=6)
    cache_price: Decimal = Field(ge=0,le=1000000,decimal_places=6)
    output_price: Decimal = Field(ge=0,le=1000000,decimal_places=6)
    active_hour_price: Decimal = Field(default=Decimal('0.5'),ge=0,le=1000000,decimal_places=6)


@app.get('/api/pricing')
async def pricing():
    return {'items':db().rows('SELECT * FROM agent_prices ORDER BY agent_id')}


@app.post('/api/pricing/{agent_id}')
async def set_price(agent_id: str, body: AgentPrice):
    if len(agent_id)>100 or not all(c.isalnum() or c in '_-' for c in agent_id):
        raise HTTPException(400,'Agent ID 无效')
    if body.cache_mode=='explicit' and body.cache_creation_price is None:
        raise HTTPException(422,'显式缓存必须设置缓存创建单价，可填写 0 表示免费')
    db().execute('''INSERT INTO agent_prices(agent_id,input_micro,cache_micro,output_micro,input_includes_cache,active_hour_micro,web_search_micro,cache_mode,cache_creation_micro)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(agent_id) DO UPDATE SET input_micro=excluded.input_micro,
        cache_micro=excluded.cache_micro,output_micro=excluded.output_micro,input_includes_cache=excluded.input_includes_cache,
        active_hour_micro=excluded.active_hour_micro,web_search_micro=excluded.web_search_micro,
        cache_mode=excluded.cache_mode,cache_creation_micro=excluded.cache_creation_micro,updated_at=CURRENT_TIMESTAMP''',
        (agent_id,micros(body.input_price),micros(body.cache_price),micros(body.output_price),1,micros(body.active_hour_price),
         micros(body.web_search_price) if body.web_search_price is not None else None,body.cache_mode,
         micros(body.cache_creation_price) if body.cache_mode=='explicit' and body.cache_creation_price is not None else None))
    return {'ok':True}


@app.get('/api/charges')
async def charges(bot_id: str = '', job_id: int | None = None, offset: int = 0):
    if offset<0: raise HTTPException(400,'分页参数无效')
    where,args=['1=1'],[]
    if bot_id:
        bot_or_404(bot_id);where.append('c.bot_id=?');args.append(bot_id)
    if job_id is not None:
        where.append('c.job_id=?');args.append(job_id)
    clause=' AND '.join(where)
    items=db().rows(f'''SELECT c.*,b.name AS bot_name,j.text AS job_text,j.status AS job_status,j.result AS job_result
        FROM charges c JOIN bots b ON b.id=c.bot_id JOIN jobs j ON j.id=c.job_id WHERE {clause} ORDER BY c.id DESC LIMIT 50 OFFSET ?''',(*args,offset))
    for item in items:
        item['tool_calls']=db().rows('SELECT call_id,event_id,name,created_at FROM job_tool_calls WHERE job_id=? ORDER BY created_at,call_id',(item['job_id'],))
        for key in ('usage_before','usage_after','usage_delta','price_snapshot','components'):
            item[key]=json.loads(item[key] or '{}')
    return {'items':items,'total':db().one(f'SELECT COUNT(*) AS n FROM charges c WHERE {clause}',args)['n']}


@app.post('/api/bots/{bot_id}/billing/retry')
async def retry_billing(bot_id: str):
    bot_or_404(bot_id)
    async with rt().lock(bot_id):
        ok=await rt().billing.settle_pending(bot_id)
    return {'ok':ok}


@app.post('/api/bots/{bot_id}/memory/retry')
async def retry_memory(bot_id: str):
    bot_or_404(bot_id)
    rt().start('memory:'+bot_id,rt().upgrade_memory(bot_id))
    return {'ok':True}


@app.get('/api/bots/{bot_id}/sessions')
async def bot_sessions(bot_id: str, offset: int = 0):
    bot = bot_or_404(bot_id)
    if offset < 0:
        raise HTTPException(400,'分页参数无效')
    rows = db().rows('''SELECT s.*, (SELECT COUNT(*) FROM jobs j WHERE j.bot_id=s.bot_id AND j.session_id=s.session_id) AS job_count
        FROM bot_sessions s WHERE s.bot_id=? ORDER BY (s.session_id=?) DESC,s.created_at DESC,s.session_id DESC LIMIT 50 OFFSET ?''',
        (bot_id,bot['session_id'],offset))
    return {'items':rows,'current_session_id':bot['session_id'],
        'total':db().one('SELECT COUNT(*) AS n FROM bot_sessions WHERE bot_id=?',(bot_id,))['n']}


@app.post('/api/bots/{bot_id}/sessions/sync')
async def sync_sessions(bot_id: str):
    bot_or_404(bot_id)
    sessions = await rt().ma.list_all('/sessions')
    with db().db:
        for session in sessions:
            if (session.get('metadata') or {}).get('claw_bot_id') != bot_id:
                continue
            agent = session.get('agent') or {}
            memory = next((r.get('memory_store_id','') for r in session.get('resources') or [] if r.get('type')=='memory_store'),'')
            db().db.execute('''INSERT INTO bot_sessions(session_id,bot_id,agent_id,environment_id,memory_store_id,created_at,source,workspace_id)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET
                agent_id=excluded.agent_id,environment_id=excluded.environment_id,
                memory_store_id=CASE WHEN excluded.memory_store_id!='' THEN excluded.memory_store_id ELSE bot_sessions.memory_store_id END,
                created_at=excluded.created_at,source='remote' ''',
                (session['id'],bot_id,agent.get('id','') if isinstance(agent,dict) else agent,
                 session.get('environment_id',''),memory,session.get('created_at') or '', 'remote',os.getenv('BAILIAN_WORKSPACE_ID','')))
    return await bot_sessions(bot_id)


@app.post('/api/bots/{bot_id}/binding')
async def binding(bot_id: str):
    async with rt().lock(bot_id):
        bot = bot_or_404(bot_id)
        if bot['token']: raise HTTPException(409,'Bot 已绑定，无需重复扫码')
        previous = db().one('SELECT * FROM bindings WHERE bot_id=?',(bot_id,))
        if previous and previous['expires_at'] > time.time() and previous['status'] not in ('expired','error','verify_code_blocked','confirmed'):
            return {'ticket':previous['ticket']}
        try:
            qr = await asyncio.wait_for(rt().wx.get_qr_code('https://ilinkai.weixin.qq.com'),30)
        except Exception as e:
            raise HTTPException(502,f'微信二维码获取失败：{type(e).__name__}')
        ticket = secrets.token_urlsafe(32)
        old = rt().tasks.get('bind:'+bot_id)
        if old and not old.done():
            old.cancel()
            await asyncio.gather(old,return_exceptions=True)
        db().execute('INSERT OR REPLACE INTO bindings(bot_id,ticket,qr,url,expires_at) VALUES(?,?,?,?,?)',
            (bot_id,ticket,qr['qrcode'],qr['qrcode_img_content'],time.time()+300))
        rt().start('bind:'+bot_id,rt().poll_binding(bot_id))
        return {'ticket':ticket}


def public_binding(ticket):
    binding = db().one('SELECT * FROM bindings WHERE ticket=?',(ticket,))
    if not binding: raise HTTPException(404,'绑定链接不存在')
    if time.time() > binding['expires_at'] and binding['status'] != 'confirmed':
        binding['status'] = 'expired'
    return binding


@app.get('/api/public/bind/{ticket}')
async def binding_state(ticket: str):
    b = public_binding(ticket)
    bot = bot_or_404(b['bot_id'])
    base = os.getenv('PUBLIC_BASE_URL','http://127.0.0.1:8000').rstrip('/')
    return {'name':bot['name'],'status':b['status'],'url': b['url'] if b['status'] not in ('confirmed','expired','error') else '',
        'share_url':base+'/bind/'+ticket,'expires_at':b['expires_at'],
        'session_ready':bool(bot['session_id']),'setup_failed':bool(bot['memory_error'])}


@app.get('/api/public/bind/{ticket}/qr')
async def qr_image(ticket: str):
    b = public_binding(ticket)
    if b['status'] in ('expired','confirmed','error'): raise HTTPException(410,'二维码已失效')
    output = io.BytesIO()
    qrcode.make(b['url']).save(output,format='PNG')
    return Response(output.getvalue(),media_type='image/png')


class VerifyCode(BaseModel):
    code: str = Field(pattern=r'^\d{4,12}$')


@app.post('/api/public/bind/{ticket}/verify')
async def verify(ticket: str,body: VerifyCode):
    b = public_binding(ticket)
    if b['status'] != 'need_verifycode': raise HTTPException(409,'当前无需配对码')
    db().update('bindings','bot_id',b['bot_id'],verify_code=body.code)
    return {'ok':True}


class SessionConfig(BaseModel):
    agent_id: str = Field(min_length=1,max_length=100,pattern=r'^[a-zA-Z0-9_-]+$')
    environment_id: str = Field(min_length=1,max_length=100,pattern=r'^[a-zA-Z0-9_-]+$')
    new_session: bool = False
    expected_session_id: str = Field(default='',max_length=100,pattern=r'^[a-zA-Z0-9_-]*$')


@app.post('/api/bots/{bot_id}/session')
async def session(bot_id: str, body: SessionConfig):
    async with rt().lock(bot_id):
        bot = bot_or_404(bot_id)
        if not bot['token']: raise HTTPException(409,'请先完成微信绑定')
        if bot['session_id'] and not body.new_session:
            return {'session_id':bot['session_id']}
        if body.new_session:
            if bot['session_id'] != body.expected_session_id:
                raise HTTPException(409,'当前 Session 已变化，请刷新页面后重试')
            active = db().one("SELECT id FROM jobs WHERE bot_id=? AND schedule_id='' AND status NOT IN ('done','failed','cancelled','interrupted','queued','preparing','checking') LIMIT 1",(bot_id,))
            if active:
                raise HTTPException(409,f"任务 #{active['id']} 尚未结束，请等待执行和回传完成或处理异常后新建 Session")
            if bot['session_id']:
                old = await rt().ma.request('GET',f"/sessions/{bot['session_id']}")
                if old.get('status') not in ('idle','terminated') or (old.get('status') != 'terminated' and (old.get('stop_reason') or {}).get('type')=='requires_action'):
                    raise HTTPException(409,'当前 MA 会话仍在执行或等待审批，请结束后再新建 Session')
        config = {'agent_id':body.agent_id,'environment_id':body.environment_id}
        bot = await rt().ensure_memory(bot)
        if not bot['session_id']:
            if not bot['memory_upgrade_state']:
                db().update('bots','id',bot_id,**config)
                bot.update(config)
            created = await rt().provision_session(bot)
            return {'session_id':created['id']}
        created = await rt().ma.create_session({**bot,**config})
        db().switch_session(bot,created['id'],body.agent_id,body.environment_id)
        rt().wakeups.setdefault(bot_id,asyncio.Event()).set()
        return {'session_id':created['id']}


class ArchiveConfig(BaseModel):
    archived: bool


@app.post('/api/bots/{bot_id}/archive')
async def archive_bot(bot_id: str, body: ArchiveConfig):
    bot_or_404(bot_id)
    db().update('bots','id',bot_id,archived=int(body.archived))
    return {'ok':True,'archived':body.archived}


@app.post('/api/bots/{bot_id}/toggle')
async def toggle(bot_id: str):
    bot = bot_or_404(bot_id)
    db().update('bots','id',bot_id,enabled=0 if bot['enabled'] else 1)
    return {'ok':True}


class Decision(BaseModel):
    allow: bool


@app.post('/api/jobs/{job_id}/approve')
async def approve(job_id: int,body: Decision):
    job = db().one('SELECT * FROM jobs WHERE id=?',(job_id,))
    if not job: raise HTTPException(404,'任务不存在')
    async with rt().lock(job['bot_id']):
        job = db().one('SELECT * FROM jobs WHERE id=?',(job_id,))
        if job['status'] != 'requires_action': raise HTTPException(409,'任务当前不在等待审批')
        reason = json.loads(job['approval']).get('reason',{})
        calls, batch = reason.get('pending_call_ids',[]),reason.get('pending_batch_id')
        if not calls or not batch: raise HTTPException(409,'缺少审批标识，请在 MA 控制台处理')
        events = [{'role':'user','type':'tool_approval_response','content':[{'type':'data','data':{
            'batch_id':batch,'call_id':c,'result':'allow' if body.allow else 'deny',
            **({} if body.allow else {'deny_message':'管理员拒绝此工具调用'})}}]} for c in calls]
        db().update('jobs','id',job_id,status='approving')
        try:
            await rt().ma.request('POST',f"/sessions/{job['session_id']}/events",json={'input':events})
        except Exception:
            db().update('jobs','id',job_id,status='uncertain',error='审批提交结果不确定，请在 MA 控制台核对')
            raise
        db().update('jobs','id',job_id,status='running',approval=json.dumps({'resolved_batch':batch}))
        return {'ok':True}


class Resolve(BaseModel):
    action: str = Field(pattern=r'^(skip|retry_reply)$')


@app.post('/api/jobs/{job_id}/resolve')
async def resolve(job_id: int,body: Resolve):
    job = db().one('SELECT * FROM jobs WHERE id=?',(job_id,))
    if not job: raise HTTPException(404,'任务不存在')
    async with rt().lock(job['bot_id']):
        job = db().one('SELECT * FROM jobs WHERE id=?',(job_id,))
        if job['status'] not in ('uncertain','reply_uncertain','interrupt_uncertain'): raise HTTPException(409,'仅处理结果不确定的任务')
        if body.action == 'retry_reply':
            if job['status'] != 'reply_uncertain': raise HTTPException(409,'仅可重试微信回传')
            db().update('jobs','id',job_id,status=job.get('reply_resume_status') or 'reply_pending')
        else:
            if job['session_id']:
                session = await rt().ma.request('GET',f"/sessions/{job['session_id']}")
                if session.get('status') not in ('idle','terminated') or (session.get('stop_reason') or {}).get('type') == 'requires_action':
                    raise HTTPException(409,'MA 会话仍在执行或等待审批，请先在百炼控制台处理')
            db().update('jobs','id',job_id,status='cancelled',error='管理员核对后跳过；不会重新执行 MA 任务')
        return {'ok':True}
