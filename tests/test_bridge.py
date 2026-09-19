import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.store import Store
from app.ma import ManagedAgent, result_from_events
from app.runtime import Runtime, wechat_url


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path/'test.db')
    db.execute("INSERT INTO bots(id,name,user_id,token,session_id,status) VALUES('b','Bot','u','secret','sesn_test','ready')")
    yield db
    db.db.close()


def incoming(mid='m1',user='u',context='ctx'):
    return {'message_type':1,'message_id':mid,'from_user_id':user,'context_token':context,'item_list':[{'type':1,'text_item':{'text':'calculate'}}]}


def event(eid,kind='message',role='assistant',text='',data=None,thread=''):
    return {'id':eid,'type':kind,'role':role,'metadata':{'thread_id':thread},'content':[{'type':'data','data':data}] if data else [{'type':'text','text':text}]}


def test_inbox_dedupe_isolation_cursor_and_context(store):
    bot = store.one('SELECT * FROM bots')
    response = {'msgs':[incoming(),incoming(user='other'),incoming('bot',user='other')],'get_updates_buf':'next'}
    store.ingest(bot,response)
    store.ingest(bot,response)
    assert len(store.rows('SELECT * FROM jobs')) == 1
    assert store.one('SELECT * FROM bots')['cursor'] == 'next'
    store.ingest(bot,{'msgs':[incoming('m2',context='new')],'get_updates_buf':'last'})
    assert store.one('SELECT * FROM bots')['context_token'] == 'new'


def test_events_turn_boundary_nested_status_and_subthread():
    events = [event('old',text='previous'),event('user',role='user'),event('sub',text='private',thread='sthr_1'),event('a',text='result'),event('a',text='result'),event('s','session_status',data={'session_status':'idle','stop_reason':{'type':'end_turn'}})]
    r = result_from_events(events,'user')
    assert r['text'] == 'result'
    assert r['state'] == 'idle'
    assert result_from_events(events,'missing')['text'] == ''


@pytest.mark.asyncio
async def test_ma_pagination_headers_and_schema(monkeypatch):
    monkeypatch.setenv('BAILIAN_WORKSPACE_ID','ws-test')
    monkeypatch.setenv('DASHSCOPE_API_KEY','test-secret')
    requests=[]
    def handler(r):
        requests.append(r)
        if r.method=='POST':
            assert json.loads(r.content)['input'][0]['content'][0]['text'].startswith('hi')
            return httpx.Response(200,json={'data':[]})
        return httpx.Response(200,json={'data':[{'id':'2'}]} if r.url.params.get('page') else {'data':[{'id':'1'}],'next_page':'p2'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ma=ManagedAgent(client)
        assert len(await ma.list_all('/agents'))==2
        await ma.send('sesn_1','hi')
    assert all(r.headers['user-agent'].startswith('AlibabaCloud-Agent-Skills/alibabacloud-bailian-managed-agent/') for r in requests)
    assert all(r.headers['authorization']=='Bearer test-secret' for r in requests)


@pytest.mark.asyncio
async def test_complete_message_roundtrip_and_restart(store):
    store.ingest(store.one('SELECT * FROM bots'),{'msgs':[incoming()],'get_updates_buf':'next'})
    ma=AsyncMock()
    ma.request.return_value={'status':'idle','stop_reason':{'type':'end_turn'}}
    ma.send.return_value={'data':[{'id':'user','created_at':'2026-09-19T00:00:00Z'}]}
    ma.events.return_value=[event('user',role='user'),event('answer',text='42'),event('s','session_status',data={'session_status':'idle','stop_reason':{'type':'end_turn'}})]
    rt=Runtime(store,ma)
    rt.wx=AsyncMock()
    await rt.step('b')
    assert store.one('SELECT * FROM jobs')['status']=='running'
    # New runtime resumes the persisted event anchor without sending a second MA message.
    rt=Runtime(store,ma)
    rt.wx=AsyncMock()
    await rt.step('b')
    assert store.one('SELECT * FROM jobs')['status']=='reply_pending'
    await rt.step('b')
    assert store.one('SELECT * FROM jobs')['status']=='done'
    sent=rt.wx.send_message.call_args.args[2]
    assert sent['context_token']=='ctx' and sent['to_user_id']=='u'
    assert sent['item_list'][0]['text_item']['text']=='42'
    ma.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_ambiguous_submission_blocks_next_job(store):
    store.ingest(store.one('SELECT * FROM bots'),{'msgs':[incoming(),incoming('m2')]})
    ma=AsyncMock()
    ma.request.return_value={'status':'idle'}
    ma.send.side_effect=TimeoutError()
    rt=Runtime(store,ma)
    await rt.step('b')
    await rt.step('b')
    assert store.rows('SELECT status FROM jobs ORDER BY id')==[{'status':'uncertain'},{'status':'queued'}]
    ma.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_requires_action_not_success(store):
    store.execute("INSERT INTO jobs(bot_id,message_id,user_id,text,status,session_id,event_id,since) VALUES('b','m','u','task','running','sesn_test','user','2026-09-19')")
    ma=AsyncMock()
    ma.events.return_value=[event('user',role='user')]
    ma.request.return_value={'status':'idle','stop_reason':{'type':'requires_action','pending_batch_id':'batch','pending_call_ids':['call']}}
    rt=Runtime(store,ma)
    await rt.step('b')
    assert store.one('SELECT * FROM jobs')['status']=='requires_action'


def test_wechat_redirect_validation():
    assert wechat_url('https://ilinkai.weixin.qq.com')=='https://ilinkai.weixin.qq.com'
    for url in ['http://ilinkai.weixin.qq.com','https://evil.com','https://weixin.qq.com.evil.com','https://localhost','https://user@ilinkai.weixin.qq.com']:
        with pytest.raises(ValueError): wechat_url(url)


def test_admin_auth_csrf_and_no_secrets(tmp_path,monkeypatch):
    from app.main import app
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'api.db'))
    monkeypatch.setenv('ADMIN_PASSWORD','test-password')
    monkeypatch.setenv('COOKIE_SECRET','test-cookie-secret')
    with TestClient(app) as client:
        assert client.get('/static/app.js').headers['content-type'].startswith('application/javascript')
        assert client.get('/api/overview').status_code==401
        assert client.post('/api/login',json={'password':'test-password'}).status_code==403
        headers={'X-Claw-Request':'1'}
        assert client.post('/api/login',json={'password':'test-password'},headers=headers).status_code==200
        app.state.runtime.ma.list_all=AsyncMock(return_value=[])
        app.state.runtime.ma.request=AsyncMock(return_value={'id':'memstore_test'})
        r=client.post('/api/bots',json={'name':'Test Bot'},headers=headers)
        assert r.status_code==200
        overview=client.get('/api/overview').json()
        assert overview['bots'][0]['name']=='Test Bot'
        assert 'token' not in overview['bots'][0]
        assert 'context_token' not in overview['bots'][0]
        assert client.get('/api/public/bind/invalid').status_code==404


def test_session_usage_manual_refresh_partial_errors_and_missing_fields(tmp_path,monkeypatch):
    from app.main import app
    from app.ma import MAError
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'usage.db'))
    monkeypatch.setenv('ADMIN_PASSWORD','password')
    monkeypatch.setenv('COOKIE_SECRET','secret')
    with TestClient(app) as client:
        assert client.get('/api/session-usage').status_code == 401
        client.post('/api/login',json={'password':'password'},headers={'X-Claw-Request':'1'})
        for bid,sid in [('a','sesn_a'),('b','sesn_b'),('c','')]:
            app.state.db.execute('INSERT INTO bots(id,name,session_id) VALUES(?,?,?)',(bid,bid,sid))
        async def request(method,path):
            if path.endswith('sesn_b'): raise MAError('MA HTTP 404')
            return {'usage':{'input_tokens':6142,'output_tokens':9,'cache_read_input_tokens':0},
                    'stats':{'active_seconds':2.183}, 'environment_variables':{'secret':'do-not-expose'}}
        app.state.runtime.ma.request = AsyncMock(side_effect=request)
        client.get('/api/overview')
        app.state.runtime.ma.request.assert_not_awaited()
        result = client.get('/api/session-usage').json()['items']
        assert len(result)==2
        assert result[0]['usage']['input_tokens']==6142
        assert result[0]['usage']['cache_read_input_tokens']==0
        assert result[0]['usage']['cache_creation_input_tokens'] is None
        assert result[0]['stats']['duration_seconds'] is None
        assert result[0]['fetched_at']
        assert result[1]['error']=='MA HTTP 404'
        assert 'do-not-expose' not in str(result)
        assert app.state.runtime.ma.request.await_count==2


@pytest.mark.asyncio
async def test_expired_wechat_clears_context_and_preserves_ma(store):
    store.update('bots','id','b',context_token='old',cursor='old')
    rt=Runtime(store,AsyncMock())
    rt.expired('b')
    bot=store.one('SELECT * FROM bots')
    assert bot['status']=='expired'
    assert bot['token']==bot['cursor']==bot['context_token']==''
    assert bot['session_id']=='sesn_test'


@pytest.mark.asyncio
async def test_approved_batch_does_not_reprompt_on_stale_status(store):
    store.execute("INSERT INTO jobs(bot_id,message_id,user_id,text,status,session_id,event_id,since,approval) VALUES('b','m','u','task','running','sesn_test','user','2026-09-19',?)",(json.dumps({'resolved_batch':'batch'}),))
    ma=AsyncMock()
    ma.events.return_value=[event('user',role='user')]
    ma.request.return_value={'status':'idle','stop_reason':{'type':'requires_action','pending_batch_id':'batch','pending_call_ids':['call']}}
    await Runtime(store,ma).step('b')
    assert store.one('SELECT * FROM jobs')['status']=='running'


@pytest.mark.asyncio
async def test_binding_changed_user_isolates_old_session(store):
    import time
    store.execute("INSERT INTO bindings(bot_id,ticket,qr,url,expires_at) VALUES('b','ticket','qr','url',?)",(time.time()+300,))
    store.execute("INSERT INTO jobs(bot_id,message_id,user_id,text) VALUES('b','m','u','old task')")
    rt=Runtime(store,AsyncMock())
    rt.wx=AsyncMock()
    rt.wx.poll_qr_status.return_value={'status':'confirmed','bot_token':'new-token','ilink_bot_id':'account','ilink_user_id':'new-user','baseurl':'https://ilinkai.weixin.qq.com'}
    rt.ensure=lambda bot_id: None
    await rt.poll_binding('b')
    assert store.one('SELECT * FROM bots')['session_id']==''
    assert store.one('SELECT * FROM bots')['user_id']=='new-user'
    assert store.one('SELECT * FROM jobs')['status']=='cancelled'
