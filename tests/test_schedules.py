import json
import time
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from app.schedules import ScheduleRule, ScheduleInput, Scheduler, create, next_run, remove
from app.runtime import Runtime
from app.store import Store
from app.ma import MAError


def seed(db):
    db.execute("INSERT INTO bots(id,name,token,user_id,context_token,session_id,agent_id,environment_id,memory_store_id,session_memory_id) VALUES('a','A','wx','user','ctx','chat_session','agent','env','mem','mem')")
    return db.ensure_api_token('a')


def task(db, bot):
    return create(db,bot,ScheduleInput(name='每日新闻',prompt='查询今日科技新闻',rule=ScheduleRule(kind='daily',time='08:00')),'task-key-123')


def test_rules_timezone_interval_once_dst():
    at=datetime.fromisoformat('2026-09-20T00:00:00+00:00').timestamp()
    rule=ScheduleRule(kind='daily',time='08:00')
    assert next_run(rule,at)==at+86400
    assert next_run(ScheduleRule(kind='weekly',time='08:00',weekdays=[1]),at)==at+86400
    assert next_run(ScheduleRule(kind='interval',minutes=5),at)==at+300
    assert next_run(ScheduleRule(kind='once',run_at='2026-09-20T08:01:00+08:00'),at)==at+60
    assert next_run(ScheduleRule(kind='once',run_at='2026-09-20T08:00:00+08:00'),at) is None
    with pytest.raises(ValueError):ScheduleRule(kind='daily',time='24:00')
    with pytest.raises(ValueError):ScheduleRule(kind='weekly',time='08:00',weekdays=[8])
    with pytest.raises(ValueError):ScheduleRule(kind='once',run_at='2026-09-20T08:00:00')
    # Spring-forward nonexistent local hour is skipped, not run at another time.
    rule=ScheduleRule(kind='daily',time='02:30',timezone='America/New_York')
    stamp=next_run(rule,datetime.fromisoformat('2026-03-08T00:00:00-05:00').timestamp())
    assert datetime.fromtimestamp(stamp, __import__('zoneinfo').ZoneInfo('America/New_York')).day==9


def test_scan_claims_once_coalesces_and_delete_preserves_history(tmp_path):
    db=Store(tmp_path/'scan.db');bot=seed(db);ma=AsyncMock();rt=Runtime(db,ma)
    rt.start=lambda key,coro:coro.close()
    item=task(db,bot);now=time.time()
    db.execute('UPDATE schedules SET next_run_at=?',(now-86400*3,))
    scheduler=Scheduler(rt);scheduler.scan(now);scheduler.scan(now)
    assert len(db.rows('SELECT * FROM jobs'))==1
    assert db.one('SELECT * FROM bots')['session_id']=='chat_session'
    scheduler.scan(now+86400)
    assert [j['status'] for j in db.rows('SELECT * FROM jobs ORDER BY id')]==['queued','cancelled']
    remove(db,db.one('SELECT * FROM schedules'))
    assert len(db.rows('SELECT * FROM jobs'))==2
    assert all(j['status']=='cancelled' for j in db.rows('SELECT * FROM jobs'))
    scheduler.scan(now+86400*2)
    assert len(db.rows('SELECT * FROM jobs'))==2
    db.db.close()


@pytest.mark.asyncio
async def test_scheduled_session_isolated_output_and_billing(tmp_path):
    db=Store(tmp_path/'run.db');bot=seed(db);item=task(db,bot)
    rt=Runtime(db,AsyncMock());rt.wx=AsyncMock();rt.start=lambda key,coro:coro.close()
    db.execute('UPDATE schedules SET next_run_at=?',(time.time()-1,));Scheduler(rt).scan(time.time())
    jid=db.one('SELECT * FROM jobs')['id']
    # A foreground message is pending; neither execution interrupts the other.
    db.execute("INSERT INTO jobs(bot_id,message_id,user_id,text,status) VALUES('a','chat','user','hello','queued')")
    db.execute("INSERT INTO agent_prices(agent_id,input_micro,cache_micro,output_micro) VALUES('agent',1000000,100000,2000000)")
    snapshot={'status':'idle','usage':{'input_tokens':0,'cache_read_input_tokens':0,'cache_creation_input_tokens':0,'output_tokens':0},'stats':{'active_seconds':0}}
    rt.ma.request.return_value=snapshot
    rt.ma.create_session.return_value={'id':'scheduled_session'}
    rt.ma.send.return_value={'data':[{'id':'start','created_at':'2026-09-19T00:00:00Z'}]}
    await rt.scheduled_step(bot,db.one('SELECT * FROM jobs WHERE id=?',(jid,)))
    assert db.one('SELECT * FROM bots')['session_id']=='chat_session'
    assert db.one('SELECT * FROM bot_sessions')['source']=='scheduled'
    rt.ma.send.assert_awaited_once_with('scheduled_session','查询今日科技新闻')
    events=[{'id':'start','type':'message','role':'user'},
        {'id':'answer','type':'message','role':'assistant','content':[{'type':'text','text':'今日新闻摘要'}]},
        {'id':'end','type':'session_status','content':[{'type':'data','data':{'status':'idle','stop_reason':{'type':'end_turn'}}}]}]
    rt.ma.events.return_value=events
    await rt.scheduled_step(bot,db.one('SELECT * FROM jobs WHERE id=?',(jid,)))
    assert db.one('SELECT status FROM jobs WHERE id=?',(jid,))['status']=='reply_pending'
    rt.wx.send_message.assert_not_awaited()
    await rt.scheduled_step(bot,db.one('SELECT * FROM jobs WHERE id=?',(jid,)))
    assert rt.wx.send_message.call_args.args[2]['to_user_id']=='user'
    assert db.one('SELECT status FROM jobs WHERE id=?',(jid,))['status']=='done'
    rt.ma.request.return_value={**snapshot,'usage':{**snapshot['usage'],'input_tokens':100}}
    assert await rt.billing.settle_pending('a')
    assert db.one('SELECT * FROM charges')['session_id']=='scheduled_session'
    assert db.one('SELECT * FROM bots')['spent_micro']==100
    assert db.one('SELECT * FROM bots')['session_id']=='chat_session'
    rt.ma.interrupt.assert_not_awaited()
    db.db.close()


@pytest.mark.asyncio
async def test_unpriced_schedule_runs_with_exhausted_budget(tmp_path):
    db=Store(tmp_path/'unpriced.db');bot=seed(db)
    db.update('bots','id','a',budget_micro=0)
    bot=db.one('SELECT * FROM bots');task(db,bot)
    rt=Runtime(db,AsyncMock());rt.ma.supports_sse=False
    rt.start=lambda key,coro:coro.close()
    db.execute('UPDATE schedules SET next_run_at=?',(time.time()-1,));Scheduler(rt).scan(time.time())
    rt.ma.create_session.return_value={'id':'unpriced_session'}
    rt.ma.request.return_value={'status':'idle'}
    rt.ma.send.return_value={'data':[{'id':'start','created_at':'2026-09-19T00:00:00Z'}]}
    await rt.scheduled_step(bot,db.one('SELECT * FROM jobs'))
    rt.ma.send.assert_awaited_once_with('unpriced_session','查询今日科技新闻')
    assert db.one('SELECT * FROM jobs')['billing_state']=='unpriced'
    assert db.one('SELECT * FROM bots')['session_id']=='chat_session'
    db.db.close()


@pytest.mark.asyncio
async def test_schedule_creation_timeout_reconcile_and_debt(tmp_path):
    db=Store(tmp_path/'recover.db');bot=seed(db);task(db,bot)
    rt=Runtime(db,AsyncMock());rt.wx=AsyncMock();rt.start=lambda key,coro:coro.close()
    db.execute('UPDATE schedules SET next_run_at=?',(time.time()-1,));Scheduler(rt).scan(time.time())
    job=db.one('SELECT * FROM jobs');rt.ma.create_session.side_effect=MAError('timeout')
    await rt.scheduled_step(bot,job)
    job=db.one('SELECT * FROM jobs');assert job['status']=='scheduled_creating'
    rt.ma.list_all.return_value=[]
    await rt.scheduled_step(bot,job);rt.ma.create_session.assert_awaited_once()
    rt.ma.list_all.return_value=[{'id':'recovered','metadata':{'claw_bot_id':'a','claw_scheduled_job':str(job['id'])}}]
    rt.ma.request.return_value={'status':'running'}
    await rt.scheduled_step(bot,job)
    assert db.one('SELECT * FROM jobs')['session_id']=='recovered'
    assert db.one('SELECT * FROM bots')['session_id']=='chat_session'
    db.update('jobs','id',job['id'],session_id='',status='queued')
    db.update('bots','id','a',budget_micro=0)
    db.execute("INSERT INTO agent_prices(agent_id,input_micro,cache_micro,output_micro) VALUES('agent',1000000,100000,2000000)")
    await rt.scheduled_step(db.one('SELECT * FROM bots'),db.one('SELECT * FROM jobs'))
    assert db.one('SELECT * FROM jobs')['billing_state']=='rejected'
    rt.ma.create_session.assert_awaited_once()
    db.db.close()


def test_schedule_api_scope_and_admin_history(tmp_path,monkeypatch):
    from app.main import app
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'api.db'))
    monkeypatch.setenv('ADMIN_PASSWORD','test');monkeypatch.setenv('COOKIE_SECRET','test')
    with TestClient(app) as c:
        db=app.state.db;bot=seed(db)
        db.execute("INSERT INTO bots(id,name) VALUES('b','B')");other=db.ensure_api_token('b')
        h={'Authorization':'Bearer '+bot['api_token'],'Idempotency-Key':'create-schedule-123'}
        body={'name':'每日新闻','prompt':'查询新闻','rule':{'kind':'daily','time':'08:00'}}
        assert c.post('/api/bot/schedules',json=body).status_code==401
        r=c.post('/api/bot/schedules',json=body,headers=h);assert r.status_code==201
        sid=r.json()['id']
        assert c.post('/api/bot/schedules',json=body,headers=h).json()['id']==sid
        assert len(c.get('/api/bot/schedules',headers=h).json()['items'])==1
        other_h={'Authorization':'Bearer '+other['api_token']}
        for method,path in [('GET',f'/api/bot/schedules/{sid}'),('DELETE',f'/api/bot/schedules/{sid}'),('GET',f'/api/bot/schedules/{sid}/runs')]:
            assert c.request(method,path,headers=other_h).status_code==404
        assert c.get('/api/schedules',headers=h).status_code==401
        c.post('/api/login',json={'password':'test'},headers={'X-Claw-Request':'1'})
        assert c.get('/api/schedules').json()['items'][0]['id']==sid
        assert c.get(f'/api/schedules/{sid}/runs').json()['total']==0
        assert c.delete(f'/api/bot/schedules/{sid}',headers=h).status_code==200
        assert c.get('/api/bot/schedules',headers=h).json()['items']==[]
        assert len(c.get('/api/schedules?include_deleted=true').json()['items'])==1
