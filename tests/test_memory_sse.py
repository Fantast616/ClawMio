import asyncio
import io
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image
from app.ma import ManagedAgent, MAError, result_from_events
from app.runtime import Runtime
from app.store import Store
from app.media import upload_wechat_file
from app.replies import reply_chunks
from wechatbot.protocol import ILinkApi


def event(eid, text='', kind='message', role='assistant', data=None):
    return {'id':eid,'created_at':f'2026-09-19T00:00:{eid[-2:]}Z','type':kind,'role':role,'status':'completed',
            'content':[{'type':'text','text':text}] if data is None else [{'type':'data','data':data}]}


@pytest.mark.asyncio
async def test_private_memory_creation_recovery_and_session_mount(tmp_path,monkeypatch):
    db=Store(tmp_path/'memory.db')
    for bid in ('a','b'):
        db.execute('INSERT INTO bots(id,name) VALUES(?,?)',(bid,bid))
    ma=AsyncMock();ma.list_all.return_value=[]
    ma.request.side_effect=[{'id':'memstore_a'},{'id':'memstore_b'}]
    rt=Runtime(db,ma)
    a=await rt.ensure_memory(db.one("SELECT * FROM bots WHERE id='a'"))
    b=await rt.ensure_memory(db.one("SELECT * FROM bots WHERE id='b'"))
    assert a['memory_store_id']!=b['memory_store_id'] and a['memory_key']!=b['memory_key']
    await rt.ensure_memory(a)
    assert ma.request.await_count==2
    db.update('bots','id','a',memory_store_id='',memory_state='uncertain')
    ma.list_all.return_value=[{'id':'memstore_a','metadata':{'claw_bot_id':'a','claw_memory_key':a['memory_key']}}]
    assert (await rt.ensure_memory(db.one("SELECT * FROM bots WHERE id='a'")))['memory_store_id']=='memstore_a'
    assert ma.request.await_count==2
    monkeypatch.setenv('BAILIAN_WORKSPACE_ID','ws_test')
    def handler(request):
        body=json.loads(request.content)
        assert body['resources'][0]['memory_store_id']=='memstore_a'
        assert body['resources'][0]['access']=='read_write'
        return httpx.Response(200,json={'id':'sesn_new'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        actual=ManagedAgent(client)
        await actual.create_session({**a,'agent_id':'agent_test','environment_id':'env_test'})
        await actual.stream_client.aclose()
    db.db.close()


@pytest.mark.asyncio
async def test_memory_ambiguous_create_never_blindly_repeats(tmp_path):
    db=Store(tmp_path/'ambiguous.db');db.execute("INSERT INTO bots(id,name) VALUES('a','A')")
    ma=AsyncMock();ma.list_all.return_value=[];ma.request.side_effect=MAError('network timeout')
    rt=Runtime(db,ma)
    for _ in range(2):
        with pytest.raises(MAError):
            await rt.ensure_memory(db.one('SELECT * FROM bots'))
    ma.request.assert_awaited_once()
    assert db.one('SELECT * FROM bots')['memory_state']=='uncertain'
    db.db.close()


def test_all_agent_messages_retained_but_not_tools_deltas_or_other_turns():
    events=[event('ev00',role='user'),event('ev01','正在生成'),
            event('ev02',kind='tool_call',data={'name':'bash'}),event('ev03',kind='tool_call_output',data={'output':'private log'}),
            event('ev04','完成。'),event('ev04','完成。'),event('ev05',role='user'),event('ev06','下一轮')]
    result=result_from_events(events,'ev00')
    assert result['messages']==['正在生成','完成。']
    assert 'private' not in result['text']
    assert reply_chunks('已生成。\n\n![图片](sandbox:/mnt/session/outputs/a.png)')==['已生成。']


@pytest.mark.asyncio
async def test_progressive_messages_resume_without_repetition(tmp_path):
    db=Store(tmp_path/'progress.db')
    db.execute("INSERT INTO bots(id,name,token,user_id,context_token,session_id) VALUES('b','B','token','u','ctx','sesn_1')")
    db.execute("INSERT INTO jobs(bot_id,message_id,user_id,text,status,session_id,event_id,since) VALUES('b','m','u','hi','running','sesn_1','ev00','2026-09-19')")
    events=[event('ev00',role='user'),event('ev01','正在生成')]
    ma=AsyncMock();ma.events.return_value=events;ma.request.return_value={'status':'running'}
    rt=Runtime(db,ma);rt.wx=AsyncMock()
    await rt.step('b');await rt.step('b')
    rt.wx.send_message.assert_awaited_once()
    assert db.one('SELECT * FROM jobs')['status']=='running'
    events.extend([event('ev02',kind='tool_call',data={'name':'bash'}),event('ev03','已完成'),
                   event('ev04',kind='session_status',data={'session_status':'idle','stop_reason':{'type':'end_turn'}})])
    ma.request.return_value={'status':'idle','stop_reason':{'type':'end_turn'}}
    # Simulate process restart; only the new message is sent.
    rt2=Runtime(db,ma);rt2.wx=rt.wx
    await rt2.step('b');await rt2.step('b')
    assert rt.wx.send_message.await_count==2
    assert db.one('SELECT * FROM jobs')['status']=='done'
    assert [c.args[2]['item_list'][0]['text_item']['text'] for c in rt.wx.send_message.call_args_list]==['正在生成','已完成']
    db.db.close()


@pytest.mark.asyncio
async def test_sse_parser_accepts_complete_frames_and_skips_deltas(monkeypatch):
    monkeypatch.setenv('BAILIAN_WORKSPACE_ID','ws_test')
    frames=[{'type':'event_delta','event_id':'ev01','delta':{}},event('ev01','hello'),{'eventId':'ev02','type':'tool_call','role':'assistant','createdAt':1789811971107,'threadId':'thrd_main','content':[{'type':'data','status':'completed','delta':False,'data':{'name':'web_search','call_id':'call_1'}}]}]
    payload=': heartbeat\n\n'+'\n\n'.join('event: message\ndata: '+json.dumps(f) for f in frames)+'\n\n'
    def handler(request):
        assert request.headers['accept']=='text/event-stream'
        assert 'skill-version/' in request.headers['user-agent']
        return httpx.Response(200,headers={'Content-Type':'text/event-stream'},text=payload)
    ma=ManagedAgent();await ma.stream_client.aclose()
    ma.stream_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ready=asyncio.Event()
    received=[e async for e in ma.stream_events('sesn_1',ready)]
    assert [e['id'] for e in received]==['ev01','ev02']
    assert received[1]['thread_id']=='thrd_main' and received[1]['created_at'].endswith('Z')
    assert ready.is_set()
    await ma.client.aclose();await ma.stream_client.aclose()


@pytest.mark.asyncio
async def test_sse_reconnect_backfills_and_deduplicates(tmp_path):
    db=Store(tmp_path/'sse.db');ma=AsyncMock()
    rt=Runtime(db,ma)
    state={'session_id':'sesn_1','ready':asyncio.Event(),'events':{'ev02':event('ev02','new')},'sync':True,'last_sync':0,'anchor':'ev00'}
    state['ready'].set()
    rt.open_stream=AsyncMock(return_value=state)
    ma.events.return_value=[event('ev00',role='user'),event('ev01','old'),event('ev02','new')]
    job={'bot_id':'b','session_id':'sesn_1','since':'2026-09-19','event_id':'ev00'}
    assert [e['id'] for e in await rt.job_events(job)]==['ev00','ev01','ev02']
    await rt.job_events(job)
    ma.events.assert_awaited_once()
    state['sync']=True
    await rt.job_events(job)
    assert ma.events.await_count==2
    db.db.close()


@pytest.mark.asyncio
async def test_generated_image_is_a_wechat_image():
    image=io.BytesIO();Image.new('RGB',(2,2)).save(image,'PNG')
    wx=AsyncMock();wx.build_cdn_upload_url=ILinkApi.build_cdn_upload_url
    wx.get_upload_url.return_value={'upload_param':'upload'};wx.upload_to_cdn.return_value='query'
    item=await upload_wechat_file(wx,{'base_url':'https://ilinkai.weixin.qq.com','token':'t','user_id':'u'},image.getvalue(),'photo.png')
    assert item['type']==2 and item['image_item']['mid_size']>0
    assert wx.get_upload_url.call_args.kwargs['media_type']==1


def test_history_backfills_known_sessions_idempotently(tmp_path):
    path=tmp_path/'history.db';db=Store(path)
    db.execute("INSERT INTO bots(id,name,session_id) VALUES('b','B','sesn_current')")
    db.execute("INSERT INTO jobs(bot_id,message_id,user_id,text,session_id) VALUES('b','m','u','x','sesn_old')")
    db.db.close()
    for _ in range(2):
        db=Store(path)
        assert {r['session_id'] for r in db.rows('SELECT * FROM bot_sessions')}=={'sesn_current','sesn_old'}
        db.db.close()


@pytest.mark.asyncio
async def test_runtime_opens_stream_before_submit_and_relays_live_message(tmp_path):
    db=Store(tmp_path/'live.db')
    db.execute("INSERT INTO bots(id,name,token,user_id,context_token,session_id) VALUES('b','B','token','u','ctx','sesn_1')")
    db.execute("INSERT INTO jobs(bot_id,message_id,user_id,text) VALUES('b','m','u','hi')")
    ma=AsyncMock();ma.supports_sse=True
    queue=asyncio.Queue();connected=asyncio.Event();anchor=event('ev00',role='user')
    async def stream(session_id,ready):
        connected.set();ready.set()
        while True:
            yield await queue.get()
    async def send(session_id,text):
        assert connected.is_set()
        return {'data':[anchor]}
    ma.stream_events=stream;ma.send.side_effect=send
    ma.events.return_value=[anchor];ma.request.return_value={'status':'idle'}
    rt=Runtime(db,ma);rt.wx=AsyncMock()
    await rt.step('b')
    await rt.step('b')
    await queue.put(event('ev01','实时回复'))
    await asyncio.wait_for(rt.wakeups.setdefault('b',asyncio.Event()).wait(),1)
    await rt.step('b')
    assert rt.wx.send_message.call_args.args[2]['item_list'][0]['text_item']['text']=='实时回复'
    # Idle scheduler passes no longer issue history requests every two seconds.
    await rt.step('b')
    ma.events.assert_awaited_once()
    rt.wx.send_message.assert_awaited_once()
    await rt.close();db.db.close()


@pytest.mark.asyncio
async def test_rebinding_to_another_user_rotates_private_memory(tmp_path):
    import time
    db=Store(tmp_path/'rebind.db')
    db.execute("INSERT INTO bots(id,name,user_id,memory_store_id,memory_key,session_id) VALUES('b','B','old','memstore_old','oldkey','sesn_old')")
    db.execute("INSERT INTO bindings(bot_id,ticket,qr,url,expires_at) VALUES('b','ticket','qr','url',?)",(time.time()+100,))
    db.execute("INSERT INTO jobs(bot_id,message_id,user_id,text,status) VALUES('b','m','old','x','running')")
    rt=Runtime(db,AsyncMock());rt.wx=AsyncMock()
    rt.wx.poll_qr_status.return_value={'status':'confirmed','bot_token':'token','ilink_bot_id':'account','ilink_user_id':'new'}
    rt.ensure=lambda _:None
    # Avoid background provisioning; inspect the isolated identity transition.
    rt.start=lambda _,coro:coro.close()
    await rt.poll_binding('b')
    bot=db.one('SELECT * FROM bots')
    assert bot['session_id']=='' and bot['memory_store_id']=='' and bot['memory_key']!='oldkey'
    assert bot['user_id']=='new'
    assert db.one('SELECT * FROM jobs')['status']=='cancelled'
    db.db.close()
