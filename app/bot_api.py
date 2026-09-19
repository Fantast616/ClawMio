"""Self-service API. Identity comes exclusively from the bot bearer token."""
import asyncio
import hashlib
import json
import secrets
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from . import schedules

router=APIRouter(prefix='/api/bot')


def own_schedule(db, bot, sid):
    row=db.one('SELECT * FROM schedules WHERE id=? AND bot_id=?',(sid,bot['id']))
    if not row:raise HTTPException(404,'定时任务不存在')
    return row


def identity(request: Request):
    scheme,_,token=request.headers.get('authorization','').partition(' ')
    if scheme.lower()!='bearer' or not token or len(token)>200:
        raise HTTPException(401,'需要有效的 Bot Bearer Token')
    db=request.app.state.db
    bot=db.one('SELECT * FROM bots WHERE api_token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))
    if not bot or not secrets.compare_digest(bot['api_token'],token):
        raise HTTPException(401,'需要有效的 Bot Bearer Token')
    return bot


def yuan(value):
    return None if value is None else format(Decimal(value)/1000000,'.6f')


@router.get('/usage')
async def usage(request: Request, bot=Depends(identity)):
    db=request.app.state.db
    totals={key:0 for key in ('input_tokens','cache_read_input_tokens','cache_creation_input_tokens','output_tokens','active_microseconds','web_search_calls')}
    for row in db.rows('SELECT usage_delta FROM charges WHERE bot_id=?',(bot['id'],)):
        delta=json.loads(row['usage_delta'])
        for key in totals:
            totals[key]+=delta.get(key,0)
    current=None
    error=None
    if bot['session_id']:
        try:
            session=await request.app.state.runtime.ma.request('GET',f"/sessions/{bot['session_id']}")
            u=session.get('usage') or {};stats=session.get('stats') or {}
            current={'session_id':bot['session_id'],'status':session.get('status'),
                'usage':{k:u.get(k) for k in ('input_tokens','cache_read_input_tokens','cache_creation_input_tokens','output_tokens')},
                'active_seconds':stats.get('active_seconds')}
        except Exception:
            error='当前 Session 用量查询暂时失败；额度与已结算用量仍可用'
    return {'bot_id':bot['id'],'name':bot['name'],'session_id':bot['session_id'],
        'currency':'CNY','budget':{'total':yuan(bot['budget_micro']),'spent':yuan(bot['spent_micro']),
            'remaining':yuan(None if bot['budget_micro'] is None else bot['budget_micro']-bot['spent_micro']),
            'unlimited':bot['budget_micro'] is None},
        'settled_usage':totals,'current_session':current,'current_session_error':error,
        'pending_billing_count':db.one("SELECT COUNT(*) AS n FROM jobs WHERE bot_id=? AND billing_state='pending'",(bot['id'],))['n'],
        'note':'金额和 settled_usage 为已结算数据；current_session 为当前会话累计用量，勿与 settled_usage 相加。'}


class ResetInput(BaseModel):
    expected_session_id: str = Field(min_length=1,max_length=100,pattern=r'^[a-zA-Z0-9_-]+$')


def public_reset(row):
    return {k:row[k] for k in ('id','status','from_session_id','session_id','error','created_at')}


@router.post('/sessions/reset',status_code=202)
async def reset_session(request: Request, body: ResetInput, bot=Depends(identity)):
    key=request.headers.get('idempotency-key','')
    if not 8<=len(key)<=100 or not all(c.isascii() and (c.isalnum() or c in '_-') for c in key):
        raise HTTPException(422,'Idempotency-Key 必须为 8–100 位字母、数字、下划线或连字符')
    db=request.app.state.db;rt=request.app.state.runtime
    async with rt.lock(bot['id']):
        prior=db.one('SELECT * FROM bot_reset_requests WHERE bot_id=? AND request_key=?',(bot['id'],key))
        if prior:
            if prior['from_session_id']!=body.expected_session_id:
                raise HTTPException(409,'相同 Idempotency-Key 不能用于不同会话')
            return public_reset(prior)
        bot=db.one('SELECT * FROM bots WHERE id=?',(bot['id'],))
        if not bot['enabled'] or not bot['token'] or not bot['session_id']:
            raise HTTPException(409,'Bot 未绑定、已暂停或尚未创建初始会话')
        if bot['session_id']!=body.expected_session_id:
            raise HTTPException(409,'当前会话已变化，请先重新查询用量')
        if bot['budget_micro'] is not None and bot['spent_micro']>=bot['budget_micro']:
            raise HTTPException(402,'您已欠费，暂时无法开启新会话')
        pending=db.one("SELECT id FROM bot_reset_requests WHERE bot_id=? AND status IN ('queued','creating','uncertain')",(bot['id'],))
        if pending:
            raise HTTPException(409,{'message':'已有新会话请求待处理','request_id':pending['id']})
        rid=secrets.token_hex(16)
        db.execute('INSERT INTO bot_reset_requests(id,bot_id,request_key,from_session_id) VALUES(?,?,?,?)',
            (rid,bot['id'],key,body.expected_session_id))
        rt.wakeups.setdefault(bot['id'],asyncio.Event()).set()
        return public_reset(db.one('SELECT * FROM bot_reset_requests WHERE id=?',(rid,)))


@router.get('/session-requests/{request_id}')
async def reset_status(request_id: str, request: Request, bot=Depends(identity)):
    row=request.app.state.db.one('SELECT * FROM bot_reset_requests WHERE id=? AND bot_id=?',(request_id,bot['id']))
    if not row:
        raise HTTPException(404,'请求不存在')
    return public_reset(row)


@router.get('/schedules')
async def list_schedules(request: Request, bot=Depends(identity)):
    return {'items':[schedules.view(r) for r in request.app.state.db.rows('SELECT * FROM schedules WHERE bot_id=? AND deleted=0 ORDER BY created_at DESC',(bot['id'],))]}


@router.post('/schedules',status_code=201)
async def create_schedule(body: schedules.ScheduleInput, request: Request, bot=Depends(identity)):
    return schedules.create(request.app.state.db,bot,body,request.headers.get('idempotency-key',''))


@router.get('/schedules/{schedule_id}')
async def get_schedule(schedule_id: str, request: Request, bot=Depends(identity)):
    return schedules.view(own_schedule(request.app.state.db,bot,schedule_id))


@router.delete('/schedules/{schedule_id}')
async def delete_schedule(schedule_id: str, request: Request, bot=Depends(identity)):
    async with request.app.state.runtime.lock(bot['id']):
        return schedules.remove(request.app.state.db,own_schedule(request.app.state.db,bot,schedule_id))


@router.get('/schedules/{schedule_id}/runs')
async def schedule_runs(schedule_id: str, request: Request, offset: int=0, bot=Depends(identity)):
    own_schedule(request.app.state.db,bot,schedule_id)
    return schedules.runs(request.app.state.db,schedule_id,offset)
