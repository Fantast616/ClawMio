from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from app.ma import MAError
import pytest
from app.store import Store
from app.runtime import Runtime


def test_session_switch_preserves_history_and_guards_duplicates(tmp_path,monkeypatch):
    from app.main import app
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'switch.db'))
    monkeypatch.setenv('ADMIN_PASSWORD','password')
    monkeypatch.setenv('COOKIE_SECRET','secret')
    headers={'X-Claw-Request':'1'}
    with TestClient(app) as c:
        c.post('/api/login',json={'password':'password'},headers=headers)
        db=app.state.db
        db.execute("INSERT INTO bots(id,name,token,session_id,agent_id,environment_id) VALUES('b','bot','token','sesn_old','agent_old','env_old')")
        db.execute("INSERT INTO jobs(bot_id,message_id,user_id,text,status,session_id) VALUES('b','m','u','old task','done','sesn_old')")
        db.update('bots','id','b',memory_store_id='memstore_b')
        ma=app.state.runtime.ma
        ma.request=AsyncMock(return_value={'status':'idle'})
        ma.create_session=AsyncMock(return_value={'id':'sesn_new'})
        body={'agent_id':'agent_new','environment_id':'env_new','new_session':True,'expected_session_id':'sesn_old'}
        ma.create_session.side_effect=MAError('MA unavailable')
        assert c.post('/api/bots/b/session',json=body,headers=headers).status_code==502
        assert db.one('SELECT * FROM bots')['session_id']=='sesn_old'
        ma.create_session.side_effect=None
        db.update('jobs','id',1,status='running')
        assert c.post('/api/bots/b/session',json=body,headers=headers).status_code==409
        db.update('jobs','id',1,status='done')
        r=c.post('/api/bots/b/session',json=body,headers=headers)
        assert r.status_code==200
        assert db.one('SELECT * FROM bots')['session_id']=='sesn_new'
        assert db.one('SELECT * FROM jobs')['session_id']=='sesn_old'
        assert {r['session_id'] for r in db.rows('SELECT * FROM bot_sessions')}=={'sesn_new','sesn_old'}
        count=ma.create_session.await_count
        assert c.post('/api/bots/b/session',json=body,headers=headers).status_code==409
        assert ma.create_session.await_count==count


@pytest.mark.asyncio
async def test_clear_creates_once_and_next_message_uses_new_session(tmp_path):
    db=Store(tmp_path/'clear.db')
    db.execute("INSERT INTO bots(id,name,token,user_id,session_id,agent_id,environment_id) VALUES('b','bot','token','u','sesn_old','agent_old','env_old')")
    message={'message_type':1,'message_id':'clear1','from_user_id':'u','context_token':'ctx','item_list':[{'type':1,'text_item':{'text':' /clear '}}]}
    bot=db.one('SELECT * FROM bots')
    db.ingest(bot,{'msgs':[message]});db.ingest(bot,{'msgs':[message]})
    db.update('bots','id','b',memory_store_id='memstore_b')
    ma=AsyncMock();ma.request.return_value={'status':'idle'}
    ma.create_session.return_value={'id':'sesn_new'}
    ma.send.return_value={'data':[{'id':'msg1','created_at':'2026-09-19'}]}
    rt=Runtime(db,ma);rt.wx=AsyncMock()
    await rt.step('b')
    assert db.one('SELECT * FROM bots')['session_id']=='sesn_new'
    assert db.one('SELECT * FROM jobs')['status']=='reply_pending'
    ma.send.assert_not_awaited()
    await rt.step('b')
    assert '已开启新会话' in rt.wx.send_message.call_args.args[2]['item_list'][0]['text_item']['text']
    db.execute("INSERT INTO jobs(bot_id,message_id,user_id,text) VALUES('b','m2','u','hello')")
    await rt.step('b')
    ma.send.assert_awaited_once_with('sesn_new','hello')
    ma.create_session.assert_awaited_once()
    db.db.close()


@pytest.mark.asyncio
async def test_clear_failed_creation_keeps_original_session(tmp_path):
    db=Store(tmp_path/'failed.db')
    db.execute("INSERT INTO bots(id,name,token,session_id,agent_id,environment_id) VALUES('b','bot','token','sesn_old','agent_old','env_old')")
    db.execute("INSERT INTO jobs(bot_id,message_id,user_id,text) VALUES('b','clear','u','/clear')")
    db.update('bots','id','b',memory_store_id='memstore_b')
    ma=AsyncMock();ma.request.return_value={'status':'idle'}
    ma.create_session.side_effect=MAError('HTTP 400',400)
    await Runtime(db,ma).step('b')
    assert db.one('SELECT * FROM bots')['session_id']=='sesn_old'
    assert db.one('SELECT * FROM jobs')['status']=='reply_pending'
    ma.send.assert_not_awaited()
    db.db.close()
