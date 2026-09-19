import json
from unittest.mock import AsyncMock
import pytest
from app.store import Store
from app.runtime import Runtime
from app.ma import MAError


def setup(tmp_path, status='running'):
    db=Store(tmp_path/'interrupt.db')
    db.execute("INSERT INTO bots(id,name,user_id,token,session_id) VALUES('b','bot','u','t','sesn_x')")
    db.execute("INSERT INTO jobs(bot_id,message_id,user_id,text,status,session_id,event_id,since) VALUES('b','1','u','first',?,'sesn_x','e1','2026-09-19')",(status,))
    db.execute("INSERT INTO jobs(bot_id,message_id,user_id,text) VALUES('b','2','u','second')")
    ma=AsyncMock()
    ma.interrupt.return_value={'data':[{'id':'interrupt1','created_at':'2026-09-19'}]}
    ma.request.return_value={'status':'running'}
    ma.events.return_value=[]
    ma.send.return_value={'data':[{'id':'e2','created_at':'2026-09-19'}]}
    return db,ma


@pytest.mark.asyncio
@pytest.mark.parametrize('status',['running','requires_action'])
async def test_interrupt_before_next_message_and_resume(tmp_path,status):
    db,ma=setup(tmp_path,status)
    rt=Runtime(db,ma)
    await rt.step('b')
    assert db.one('SELECT * FROM jobs WHERE id=1')['status']=='interrupting'
    ma.interrupt.assert_awaited_once_with('sesn_x')
    await rt.step('b')
    ma.send.assert_not_awaited()
    rt=Runtime(db,ma) # restart does not resend interrupt
    ma.events.return_value=[{'id':'interrupt1'}, {'id':'idle','type':'session_status','content':[{'data':{'session_status':'idle','stop_reason':{'type':'end_turn'}}}]}]
    ma.request.return_value={'status':'idle','stop_reason':{'type':'end_turn'}}
    await rt.step('b')
    assert db.one('SELECT * FROM jobs WHERE id=1')['status']=='interrupted'
    await rt.step('b')
    ma.send.assert_awaited_once_with('sesn_x','second')
    ma.interrupt.assert_awaited_once()
    db.db.close()


@pytest.mark.asyncio
async def test_interrupt_failure_blocks_new_message(tmp_path):
    db,ma=setup(tmp_path)
    ma.interrupt.side_effect=TimeoutError()
    rt=Runtime(db,ma)
    await rt.step('b');await rt.step('b')
    assert db.one('SELECT * FROM jobs WHERE id=1')['status']=='interrupt_uncertain'
    ma.send.assert_not_awaited()
    db.db.close()


@pytest.mark.asyncio
async def test_new_message_suppresses_old_reply(tmp_path):
    db,ma=setup(tmp_path,'reply_pending')
    db.update('bots','id','b',context_token='ctx')
    db.update('jobs','id',1,result='old result')
    rt=Runtime(db,ma);rt.wx=AsyncMock()
    await rt.step('b')
    rt.wx.send_message.assert_not_awaited()
    assert db.one('SELECT * FROM jobs WHERE id=1')['status']=='interrupted'
    db.db.close()


@pytest.mark.asyncio
async def test_explicit_rejection_is_failure_not_uncertain(tmp_path):
    db,ma=setup(tmp_path,'queued')
    ma.request.return_value={'status':'idle'}
    ma.send.side_effect=MAError('MA HTTP 400',400)
    await Runtime(db,ma).step('b')
    assert db.one('SELECT * FROM jobs WHERE id=1')['status']=='reply_pending'
    assert '400' in db.one('SELECT * FROM jobs WHERE id=1')['error']
    db.db.close()
