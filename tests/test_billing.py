import json
from unittest.mock import AsyncMock
import pytest
from fastapi.testclient import TestClient
from app.store import Store
from app.billing import Billing,micros
from app.runtime import Runtime


def snapshot(inp=0,cache=0,out=0,seconds=0,creation=0):
    return {'status':'idle','usage':{'input_tokens':inp,'cache_read_input_tokens':cache,
        'cache_creation_input_tokens':creation,'output_tokens':out},'stats':{'active_seconds':seconds}}


@pytest.fixture
def db(tmp_path):
    store=Store(tmp_path/'billing.db')
    store.execute("INSERT INTO bots(id,name,user_id,token,context_token,session_id,agent_id,budget_micro) VALUES('b','B','u','t','ctx','sesn_1','agent_1',1000000)")
    store.execute("INSERT INTO agent_prices(agent_id,input_micro,cache_micro,output_micro) VALUES('agent_1',2000000,500000,8000000)")
    yield store
    store.db.close()


def job(db,mid='m'):
    jid=db.execute("INSERT INTO jobs(bot_id,message_id,user_id,text) VALUES('b',?,'u','hello')",(mid,))
    return db.one('SELECT * FROM jobs WHERE id=?',(jid,))


@pytest.mark.asyncio
async def test_delta_price_snapshot_time_debit_and_exactly_once(db):
    ma=AsyncMock();billing=Billing(db,ma);j=job(db)
    ma.request.return_value=snapshot(1000,200,100,20)
    assert await billing.prepare(db.one('SELECT * FROM bots'),j)
    # Price changes while a task runs do not change that task's invoice.
    db.execute('UPDATE agent_prices SET input_micro=999000000')
    db.update('jobs','id',j['id'],status='done',event_id='event1',session_id='sesn_1')
    ma.request.return_value=snapshot(11000,2200,600,1820)
    ma.events.return_value=[{'id':'event1','type':'message','role':'user'}]
    assert await billing.settle_pending('b')
    c=db.one('SELECT * FROM charges')
    assert c['normal_input_tokens']==8000
    assert c['token_micro']==21000  # 0.016 + 0.001 + 0.004
    assert c['active_micro']==250000  # 1800s / 3600 * 0.5
    assert c['amount_micro']==271000
    assert db.one('SELECT * FROM bots')['spent_micro']==271000
    # Simulate a recovered pending flag with an existing ledger entry.
    db.update('jobs','id',j['id'],billing_state='pending')
    assert await billing.settle_pending('b')
    assert len(db.rows('SELECT * FROM charges'))==1
    assert db.one('SELECT * FROM bots')['spent_micro']==271000
    j2=job(db,'m2');ma.request.reset_mock()
    assert await billing.prepare(db.one('SELECT * FROM bots'),j2)
    ma.request.assert_not_awaited()
    assert json.loads(db.one('SELECT * FROM jobs WHERE id=?',(j2['id'],))['billing_before'])['input_tokens']==11000


@pytest.mark.asyncio
async def test_missing_or_regressed_usage_never_silently_charges_zero(db):
    ma=AsyncMock();b=Billing(db,ma);j=job(db)
    ma.request.return_value=snapshot(100,10,20,4)
    await b.prepare(db.one('SELECT * FROM bots'),j)
    db.update('jobs','id',j['id'],status='interrupted',event_id='event',session_id='sesn_1')
    for invalid in ({'status':'idle','usage':{},'stats':{}},snapshot(1,1,1,1)):
        ma.request.return_value=invalid
        assert not await b.settle_pending('b')
        assert db.rows('SELECT * FROM charges')==[]
    ma.request.return_value=snapshot(200,20,40,8)
    ma.events.return_value=[{'id':'event','type':'message','role':'user'}]
    assert await b.settle_pending('b')
    assert db.one('SELECT * FROM charges')['amount_micro']>0


@pytest.mark.asyncio
async def test_debt_rejects_without_ma_and_usage_does_not_interrupt(db):
    db.update('bots','id','b',spent_micro=1500000)
    ma=AsyncMock();rt=Runtime(db,ma);rt.wx=AsyncMock()
    bot=db.one('SELECT * FROM bots')
    def inbound(mid,text):return {'message_type':1,'message_id':mid,'from_user_id':'u','context_token':'ctx','item_list':[{'type':1,'text_item':{'text':text}}]}
    db.ingest(bot,{'msgs':[inbound('usage',' /usage '),inbound('task','hello')]})
    await rt.step('b')
    text=rt.wx.send_message.call_args.args[2]['item_list'][0]['text_item']['text']
    assert '总额度：¥1' in text and '剩余额度：¥-0.5' in text
    await rt.step('b');await rt.step('b')
    assert '您已欠费' in rt.wx.send_message.call_args.args[2]['item_list'][0]['text_item']['text']
    ma.send.assert_not_awaited();ma.interrupt.assert_not_awaited()
    assert db.rows('SELECT * FROM charges')==[]


@pytest.mark.asyncio
async def test_usage_while_running_is_free_and_non_interrupting(db):
    ma=AsyncMock();rt=Runtime(db,ma);rt.wx=AsyncMock()
    j=job(db);db.update('jobs','id',j['id'],status='running',event_id='event',session_id='sesn_1')
    db.ingest(db.one('SELECT * FROM bots'),{'msgs':[{'message_type':1,'message_id':'usage','from_user_id':'u','context_token':'ctx','item_list':[{'type':1,'text_item':{'text':'/usage'}}]}]})
    assert not rt.has_next(db.one('SELECT * FROM jobs WHERE id=?',(j['id'],)))
    await rt.step('b')
    assert db.one('SELECT * FROM jobs WHERE id=?',(j['id'],))['status']=='running'
    ma.interrupt.assert_not_awaited();ma.send.assert_not_awaited();ma.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_session_replaces_unsent_baseline(db):
    ma=AsyncMock();b=Billing(db,ma);j=job(db)
    ma.request.return_value=snapshot(500,200,100,50)
    await b.prepare(db.one('SELECT * FROM bots'),j)
    db.update('bots','id','b',session_id='sesn_2')
    ma.request.return_value=snapshot()
    await b.prepare(db.one('SELECT * FROM bots'),db.one('SELECT * FROM jobs'))
    saved=db.one('SELECT * FROM jobs')
    assert saved['billing_session_id']=='sesn_2'
    assert json.loads(saved['billing_before'])['input_tokens']==0


@pytest.mark.asyncio
async def test_total_input_excludes_cache_read_without_adding_cache_write(db):
    ma=AsyncMock();b=Billing(db,ma);j=job(db);ma.request.return_value=snapshot()
    await b.prepare(db.one('SELECT * FROM bots'),j)
    db.update('jobs','id',j['id'],status='failed',session_id='sesn_1',event_id='event')
    ma.request.return_value=snapshot(51824,40064,832,3.6,creation=50)
    ma.events.return_value=[{'id':'event','type':'message','role':'user'}]
    assert await b.settle_pending('b')
    c=db.one('SELECT * FROM charges')
    assert c['normal_input_tokens']==11760 and c['token_micro']==50208 and c['active_micro']==500


def test_billing_api_and_validation(tmp_path,monkeypatch):
    from app.main import app
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'api.db'))
    monkeypatch.setenv('ADMIN_PASSWORD','test');monkeypatch.setenv('COOKIE_SECRET','test')
    with TestClient(app) as c:
        h={'X-Claw-Request':'1'}
        assert c.get('/api/charges').status_code==401
        c.post('/api/login',json={'password':'test'},headers=h)
        app.state.runtime.ma.list_all=AsyncMock(return_value=[])
        app.state.runtime.ma.request=AsyncMock(return_value={'id':'memstore_test'})
        bot=c.post('/api/bots',json={'name':'Budget Bot','budget':'12.345678'},headers=h).json()
        b=c.get('/api/overview').json()['bots'][0]
        assert b['budget_micro']==12345678
        p={'input_price':'2','cache_price':'0.5','output_price':'8','active_hour_price':'0.5'}
        assert c.post('/api/pricing/agent_a',json=p,headers=h).status_code==200
        assert c.get('/api/pricing').json()['items'][0]['active_hour_micro']==500000
        assert c.get('/api/pricing').json()['items'][0]['web_search_micro']==30000
        assert c.post('/api/pricing/agent_a',json={**p,'web_search_price':'0.04'},headers=h).status_code==200
        assert c.get('/api/pricing').json()['items'][0]['web_search_micro']==40000
        assert c.post('/api/pricing/agent_a',json={**p,'input_price':'-1'},headers=h).status_code==422
        assert c.post('/api/bots/'+bot['id']+'/budget',json={'budget':None},headers=h).status_code==200
        assert c.get('/api/charges?bot_id='+bot['id']).json()=={'items':[],'total':0}
        assert c.get('/api/charges?bot_id=missing').status_code==404


def search_event(call='call_1',eid='search1',**extra):
    return {'eventId':eid,'type':'tool_call','role':'assistant','threadId':'thrd_main','createdAt':1789811971107,
        'content':[{'type':'data','delta':False,'status':'completed','data':{'name':'web_search','call_id':call}}],**extra}

@pytest.mark.asyncio
async def test_search_reconnect_restart_snapshot_and_turn_boundaries(db):
    ma=AsyncMock();b=Billing(db,ma);j=job(db);ma.request.return_value=snapshot()
    await b.prepare(db.one('SELECT * FROM bots'),j)
    db.update('jobs','id',j['id'],status='running',session_id='sesn_1',event_id='start')
    j=db.one('SELECT * FROM jobs')
    anchor={'id':'start','type':'message','role':'user'}
    partial=search_event('partial','partial',status='in_progress')
    events=[search_event('old','old'),anchor,search_event(),search_event(eid='repeat'),partial,
        {'id':'subuser','type':'message','role':'user','thread_id':'sthr_sub'},
        search_event('call_2','search2',threadId='sthr_sub'),
        {'id':'next','type':'message','role':'user'},search_event('next_call','search3')]
    assert b.record_tools(j,events)
    assert b.record_tools(j,events)
    assert db.one('SELECT COUNT(*) AS n FROM job_tool_calls')['n']==2
    # Restart billing and change price: this turn still costs 2 * original 0.03.
    b=Billing(db,ma);db.execute('UPDATE agent_prices SET web_search_micro=990000')
    db.update('jobs','id',j['id'],status='interrupted')
    ma.events.return_value=events
    assert await b.settle_pending('b')
    assert await b.settle_pending('b')
    c=db.one('SELECT * FROM charges')
    assert c['amount_micro']==60000
    assert json.loads(c['components'])['web_search_count']==2
    assert json.loads(c['price_snapshot'])['web_search_micro']==30000
    assert db.one('SELECT * FROM bots')['spent_micro']==60000
    # Reusing a call ID in another job must not double bill the same remote call.
    j2=job(db,'second');db.update('jobs','id',j2['id'],event_id='start',session_id='sesn_1')
    b.record_tools(db.one('SELECT * FROM jobs WHERE id=?',(j2['id'],)),events)
    assert db.one('SELECT COUNT(*) AS n FROM job_tool_calls')['n']==2

@pytest.mark.asyncio
async def test_search_history_failure_blocks_settlement_and_backfills(db):
    ma=AsyncMock();b=Billing(db,ma);j=job(db);ma.request.return_value=snapshot()
    await b.prepare(db.one('SELECT * FROM bots'),j)
    db.update('jobs','id',j['id'],status='done',session_id='sesn_1',event_id='start')
    ma.events.side_effect=RuntimeError('disconnected')
    assert not await b.settle_pending('b')
    ma.events.side_effect=None;ma.events.return_value=[search_event()]
    assert not await b.settle_pending('b')
    assert db.rows('SELECT * FROM charges')==[]
    ma.events.return_value=[{'id':'start','type':'message','role':'user'},search_event()]
    assert await b.settle_pending('b')
    assert db.one('SELECT * FROM charges')['amount_micro']==30000


def test_cache_usage_missing_creation_depends_on_mode():
    from app.billing import usage_of
    s=snapshot(100,50,10,1)
    del s['usage']['cache_creation_input_tokens']
    assert usage_of(s,'implicit')['cache_creation_input_tokens']==0
    with pytest.raises(ValueError): usage_of(s,'explicit')
    s['usage']['cache_creation_input_tokens']=None
    assert usage_of(s,'implicit')['cache_creation_input_tokens']==0
    with pytest.raises(ValueError): usage_of(s,'explicit')


def test_cache_mode_pricing_validation(tmp_path,monkeypatch):
    from app.main import app
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'cache-api.db'))
    monkeypatch.setenv('ADMIN_PASSWORD','test');monkeypatch.setenv('COOKIE_SECRET','test')
    with TestClient(app) as c:
        h={'X-Claw-Request':'1'}
        c.post('/api/login',json={'password':'test'},headers=h)
        p={'input_price':'2','cache_price':'0.5','output_price':'8','cache_mode':'explicit'}
        assert c.post('/api/pricing/agent_a',json=p,headers=h).status_code==422
        assert c.post('/api/pricing/agent_a',json={**p,'cache_creation_price':'2.5'},headers=h).status_code==200
        price=c.get('/api/pricing').json()['items'][0]
        assert price['cache_mode']=='explicit' and price['cache_creation_micro']==2500000
        assert c.post('/api/pricing/agent_a',json={**p,'cache_creation_price':'-1'},headers=h).status_code==422
        assert c.post('/api/pricing/agent_a',json={**p,'cache_mode':'implicit'},headers=h).status_code==200
        price=c.get('/api/pricing').json()['items'][0]
        assert price['cache_mode']=='implicit' and price['cache_creation_micro'] is None

@pytest.mark.asyncio
async def test_explicit_cache_delta_and_locked_price(db):
    db.execute("UPDATE agent_prices SET cache_mode='explicit',cache_creation_micro=2500000")
    ma=AsyncMock();b=Billing(db,ma);j=job(db)
    ma.request.return_value=snapshot(1000,200,100,2,creation=300)
    assert await b.prepare(db.one('SELECT * FROM bots'),j)
    db.update('jobs','id',j['id'],status='done',session_id='sesn_1',event_id='start')
    # Rates and mode change while running; this turn keeps its explicit snapshot.
    db.execute("UPDATE agent_prices SET cache_mode='implicit',cache_creation_micro=NULL")
    ma.request.return_value=snapshot(45969,26573,1450,2,creation=18878)
    ma.events.return_value=[{'id':'start','type':'message','role':'user'}]
    assert await b.settle_pending('b')
    c=db.one('SELECT * FROM charges');parts=json.loads(c['components'])
    assert c['normal_input_tokens']==18
    assert parts['input_micro']==36 and parts['cache_micro']==13187
    assert parts['cache_creation_micro']==46445 and parts['output_micro']==10800
    assert c['token_micro']==70468 and c['amount_micro']==70468
    assert json.loads(c['price_snapshot'])['cache_mode']=='explicit'
    assert await b.settle_pending('b')
    assert db.one('SELECT * FROM bots')['spent_micro']==70468

@pytest.mark.asyncio
async def test_explicit_cache_invalid_and_zero_price(db):
    db.execute("UPDATE agent_prices SET cache_mode='explicit',cache_creation_micro=0")
    ma=AsyncMock();b=Billing(db,ma);j=job(db);ma.request.return_value=snapshot()
    assert await b.prepare(db.one('SELECT * FROM bots'),j)
    db.update('jobs','id',j['id'],status='done',session_id='sesn_1',event_id='start')
    ma.events.return_value=[{'id':'start','type':'message','role':'user'}]
    ma.request.return_value=snapshot(100,60,0,0,creation=60)
    assert not await b.settle_pending('b')
    assert db.rows('SELECT * FROM charges')==[]
    ma.request.return_value=snapshot(100,60,0,0,creation=40)
    assert await b.settle_pending('b')
    c=db.one('SELECT * FROM charges')
    assert c['normal_input_tokens']==0 and c['amount_micro']==30
    assert json.loads(c['components'])['cache_creation_micro']==0
