import asyncio
import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.ma import MAError, ManagedAgent
from app.runtime import Runtime
from app.store import Store


def seed(db, bid='a'):
    db.execute('''INSERT INTO bots(id,name,token,user_id,context_token,session_id,agent_id,environment_id,memory_store_id,session_memory_id,budget_micro,spent_micro)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',(bid,bid,'wechat-token','user','ctx','sesn_'+bid,'agent','env','mem_'+bid,'mem_'+bid,1000000,250000))
    return db.ensure_api_token(bid)


def test_token_stability_and_migration(tmp_path):
    path=tmp_path/'tokens.db';db=Store(path)
    a=seed(db);b=seed(db,'b')
    assert a['api_token']!=b['api_token'] and len(a['api_token'])>40
    token=a['api_token'];db.db.close();db=Store(path)
    assert db.one("SELECT api_token FROM bots WHERE id='a'")['api_token']==token
    db.execute("INSERT INTO bots(id,name) VALUES('old','old')")
    db.db.close();db=Store(path)
    assert db.one("SELECT api_token FROM bots WHERE id='old'")['api_token'].startswith('cb_')
    db.db.close()


def test_bearer_scope_usage_and_idempotent_reset(tmp_path,monkeypatch):
    from app.main import app
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'api.db'))
    monkeypatch.setenv('ADMIN_PASSWORD','test');monkeypatch.setenv('COOKIE_SECRET','test')
    with TestClient(app) as c:
        db=app.state.db;rt=app.state.runtime
        a=seed(db);b=seed(db,'b')
        rt.ma.request=AsyncMock(return_value={'status':'running','usage':{'input_tokens':12},'environment_variables':{'secret':'hidden'}})
        h={'Authorization':'Bearer '+a['api_token']}
        assert c.get('/api/bot/usage').status_code==401
        assert c.get('/api/bot/usage',headers={'Authorization':'Bearer wrong'}).status_code==401
        assert c.get('/api/overview',headers=h).status_code==401
        u=c.get('/api/bot/usage?bot_id=b',headers=h).json()
        assert u['bot_id']=='a' and u['budget']['remaining']=='0.750000'
        assert 'secret' not in json.dumps(u) and a['api_token'] not in json.dumps(u)
        # Without an Agent price even a zero budget must allow a new session.
        db.update('bots','id','a',budget_micro=0)
        payload={'expected_session_id':'sesn_a'}
        assert c.post('/api/bot/sessions/reset',json=payload,headers=h).status_code==422
        h['Idempotency-Key']='same-request-123'
        first=c.post('/api/bot/sessions/reset',json=payload,headers=h)
        assert first.status_code==202
        rid=first.json()['id']
        assert c.post('/api/bot/sessions/reset',json=payload,headers=h).json()['id']==rid
        assert c.post('/api/bot/sessions/reset',json={'expected_session_id':'sesn_b'},headers=h).status_code==409
        assert c.get('/api/bot/session-requests/'+rid,headers={'Authorization':'Bearer '+b['api_token']}).status_code==404
        assert c.get('/api/bot/session-requests/'+rid,headers=h).json()['status']=='queued'
        db.update('bots','id','a',budget_micro=1000000)
        rt.ma.request.side_effect=MAError('unavailable')
        u=c.get('/api/bot/usage',headers=h).json()
        assert u['current_session_error'] and u['budget']['remaining']=='0.750000'
        c.post('/api/login',json={'password':'test'},headers={'X-Claw-Request':'1'})
        overview=c.get('/api/overview').json()
        assert a['api_token'] not in json.dumps(overview)
        assert c.get('/api/bots/a/access').json()['token']==a['api_token']
        assert c.get('/api/bot/usage').status_code==401  # admin cookie isn't bot identity


@pytest.mark.asyncio
async def test_reset_waits_for_turn_and_recovers_lost_response(tmp_path):
    db=Store(tmp_path/'reset.db');a=seed(db);ma=AsyncMock();rt=Runtime(db,ma)
    db.update('bots','id','a',budget_micro=0)
    rt.wx=AsyncMock()
    db.execute("INSERT INTO bot_reset_requests(id,bot_id,request_key,from_session_id) VALUES('request','a','key-1234','sesn_a')")
    jid=db.execute("INSERT INTO jobs(bot_id,message_id,user_id,text,status,event_id,session_id) VALUES('a','m','user','reset please','running','ev','sesn_a')")
    # A live Agent tool invocation must not trigger an interrupt or immediate switch.
    rt.job_events=AsyncMock(return_value=None)
    await rt.step('a')
    ma.create_session.assert_not_awaited();ma.interrupt.assert_not_awaited()
    db.update('jobs','id',jid,status='done')
    ma.request.return_value={'status':'idle'}
    ma.create_session.side_effect=MAError('timeout')
    await rt.step('a')
    assert db.one('SELECT * FROM bot_reset_requests')['status']=='uncertain'
    assert db.one('SELECT * FROM bots')['session_id']=='sesn_a'
    db.execute('UPDATE bot_reset_requests SET retry_at=0')
    ma.list_all.return_value=[{'id':'sesn_new','metadata':{'claw_bot_id':'a','claw_reset_request':'request'}}]
    await rt.step('a')
    assert db.one('SELECT * FROM bot_reset_requests')['status']=='completed'
    assert db.one('SELECT * FROM bots')['session_id']=='sesn_new'
    assert db.one('SELECT * FROM bots')['api_token']==a['api_token']
    assert len(db.rows('SELECT * FROM bot_sessions'))==2
    ma.create_session.assert_awaited_once()
    await rt.step('a');ma.create_session.assert_awaited_once()
    db.db.close()


@pytest.mark.asyncio
async def test_every_session_injects_bot_token(tmp_path,monkeypatch):
    monkeypatch.setenv('BAILIAN_WORKSPACE_ID','ws_test')
    monkeypatch.setenv('CLAW_API_BASE_URL','https://bots.example.com/')
    db=Store(tmp_path/'inject.db');a=seed(db)
    def respond(req):
        body=json.loads(req.content)
        assert body['environment_variables']=={'CLAW_BOT_TOKEN':a['api_token'],'CLAW_API_BASE_URL':'https://bots.example.com'}
        assert a['api_token'] not in json.dumps(body['metadata'])
        return httpx.Response(200,json={'id':'sesn_new'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        ma=ManagedAgent(client)
        await ma.create_session(a)
        await ma.stream_client.aclose()
    db.db.close()


def test_skill_client_paths_credentials_and_failure(monkeypatch,capsys):
    path=Path(__file__).resolve().parents[1]/'skills/claw-bot-self-service/scripts/bot_api.py'
    spec=importlib.util.spec_from_file_location('skill_client',path)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    monkeypatch.setenv('CLAW_API_BASE_URL','http://example.test:8000/')
    monkeypatch.setenv('CLAW_BOT_TOKEN','secret-token')
    captured=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self):return b'{"status":"queued"}'
    class Opener:
        def open(self,req,timeout):captured.append(req);return Response()
    monkeypatch.setattr(mod,'build_opener',lambda *args:Opener())
    assert mod.main(['usage'])==0
    assert captured[-1].full_url=='http://example.test:8000/api/bot/usage'
    assert captured[-1].get_header('Authorization')=='Bearer secret-token'
    assert mod.main(['reset','--session-id','sesn_a','--request-id','request-123'])==0
    assert json.loads(captured[-1].data)=={'expected_session_id':'sesn_a'}
    assert captured[-1].get_header('Idempotency-key')=='request-123'
    assert mod.main(['schedule-list'])==0
    assert captured[-1].full_url.endswith('/api/bot/schedules')
    assert mod.main(['schedule-delete','--schedule-id','schedule_1'])==0
    assert captured[-1].method=='DELETE' and captured[-1].full_url.endswith('/api/bot/schedules/schedule_1')
    assert mod.main(['schedule-runs','--schedule-id','schedule_1','--offset','50'])==0
    assert captured[-1].full_url.endswith('/api/bot/schedules/schedule_1/runs?offset=50')
    assert 'secret-token' not in capsys.readouterr().out
    monkeypatch.delenv('CLAW_BOT_TOKEN')
    assert mod.main(['usage'])==1
