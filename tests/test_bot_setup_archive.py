import asyncio
from unittest.mock import AsyncMock
import pytest
from fastapi.testclient import TestClient
from app.store import Store
from app.runtime import Runtime
from app.ma import MAError

@pytest.mark.asyncio
async def test_initial_session_once_and_unbound(tmp_path, monkeypatch):
    monkeypatch.setenv('DEFAULT_AGENT_ID','agent_default')
    monkeypatch.setenv('DEFAULT_ENVIRONMENT_ID','env_default')
    db=Store(tmp_path/'test.db')
    db.execute("INSERT INTO bots(id,name,memory_store_id) VALUES('b','Bot','mem_b')")
    ma=AsyncMock();ma.create_session.return_value={'id':'sesn_initial'}
    rt=Runtime(db,ma)
    await rt.upgrade_memory('b')
    ma.create_session.assert_not_awaited()
    db.update('bots','id','b',token='token',status='bound')
    await asyncio.gather(rt.upgrade_memory('b'),rt.upgrade_memory('b'))
    ma.create_session.assert_awaited_once()
    bot=db.one('SELECT * FROM bots')
    assert bot['session_id']=='sesn_initial' and bot['status']=='ready'
    assert bot['session_memory_id']=='mem_b' and bot['agent_id']=='agent_default'
    assert len(db.rows('SELECT * FROM bot_sessions'))==1
    db.db.close()

@pytest.mark.asyncio
async def test_initial_failure_reconcile_and_defaults(tmp_path,monkeypatch):
    monkeypatch.delenv('DEFAULT_AGENT_ID',raising=False)
    monkeypatch.delenv('DEFAULT_ENVIRONMENT_ID',raising=False)
    db=Store(tmp_path/'test.db')
    db.execute("INSERT INTO bots(id,name,token,status,memory_store_id) VALUES('b','Bot','token','bound','mem_b')")
    ma=AsyncMock();rt=Runtime(db,ma)
    ma.list_all.side_effect=lambda path: [{'id':'agent_a'}] if path=='/agents' else [{'id':'env_a'}] if path=='/environments' else []
    ma.create_session.side_effect=MAError('invalid',400)
    await rt.upgrade_memory('b')
    bot=db.one('SELECT * FROM bots')
    assert bot['memory_upgrade_state']=='' and bot['token']=='token' and bot['memory_error']
    ma.create_session.side_effect=MAError('network timeout')
    await rt.upgrade_memory('b')
    bot=db.one('SELECT * FROM bots');marker=bot['memory_upgrade_state']
    assert marker
    await rt.upgrade_memory('b')
    assert ma.create_session.await_count==2
    ma.list_all.side_effect=None
    ma.list_all.return_value=[{'id':'sesn_recovered','metadata':{'claw_bot_id':'b','claw_memory_upgrade':marker}}]
    await rt.upgrade_memory('b')
    assert db.one('SELECT * FROM bots')['session_id']=='sesn_recovered'
    assert ma.create_session.await_count==2
    db.db.close()


def test_archive_api_persists_and_keeps_history(tmp_path,monkeypatch):
    from app.main import app
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'api.db'))
    monkeypatch.setenv('ADMIN_PASSWORD','test');monkeypatch.setenv('COOKIE_SECRET','test')
    with TestClient(app) as c:
        h={'X-Claw-Request':'1'}
        assert c.post('/api/bots/b/archive',json={'archived':True},headers=h).status_code==401
        c.post('/api/login',json={'password':'test'},headers=h)
        db=app.state.runtime.db
        db.execute("INSERT INTO bots(id,name,enabled) VALUES('b','Bot',1)")
        db.execute("INSERT INTO bot_sessions(session_id,bot_id,agent_id,environment_id) VALUES('s','b','a','e')")
        assert len(c.get('/api/overview').json()['bots'])==1
        assert c.post('/api/bots/b/archive',json={'archived':True},headers=h).status_code==200
        assert c.get('/api/overview').json()['bots']==[]
        archived=c.get('/api/overview?archived=true').json()['bots'][0]
        assert archived['enabled']==1 and archived['session_count']==1
        assert c.post('/api/bots/missing/archive',json={'archived':True},headers=h).status_code==404
        assert c.post('/api/bots/b/archive',json={'archived':False},headers=h).status_code==200
        assert len(c.get('/api/overview').json()['bots'])==1
    reopened=Store(tmp_path/'api.db')
    assert reopened.one('SELECT * FROM bots')['archived']==0
    assert len(reopened.rows('SELECT * FROM bot_sessions'))==1
    reopened.db.close()
