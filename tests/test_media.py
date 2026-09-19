import json
import time
from unittest.mock import AsyncMock
import httpx
import pytest
from app.store import Store
from app.runtime import Runtime
from app.ma import ManagedAgent
from app.media import attachments_from_items


def make_store(tmp_path):
    db=Store(tmp_path/'media.db')
    db.execute("INSERT INTO bots(id,name,user_id,token,session_id) VALUES('b','Bot','u','t','sesn_test')")
    items=[{'type':1,'text_item':{'text':'分析这些附件'}},
        {'type':2,'image_item':{'aeskey':'0'*32,'media':{'encrypt_query_param':'query'}}},
        {'type':4,'file_item':{'file_name':'../../report.pdf','media':{'aes_key':'key','encrypt_query_param':'query2'}}}]
    msg={'message_type':1,'message_id':'m','from_user_id':'u','context_token':'ctx','item_list':items}
    db.ingest(db.one('SELECT * FROM bots'),{'msgs':[msg],'get_updates_buf':'next'})
    return db,msg


@pytest.mark.asyncio
async def test_upload_wait_all_scan_then_send_and_restart(tmp_path,monkeypatch):
    db,msg=make_store(tmp_path)
    db.ingest(db.one('SELECT * FROM bots'),{'msgs':[msg]})
    assert len(db.rows('SELECT * FROM jobs'))==1
    ma=AsyncMock()
    ma.upload_file.side_effect=[{'id':'file_image','status':'checking'},{'id':'file_pdf','status':'checking'}]
    state={'file_image':'checking','file_pdf':'checking'}
    async def request(method,path):
        return {'status':state[path.split('/')[-1]]} if '/files/' in path else {'status':'idle'}
    ma.request.side_effect=request
    ma.mount_file.side_effect=lambda sid,fid,path: {'id':'resource_'+fid,'mount_path':'/mnt/session'+path}
    ma.send.return_value={'data':[{'id':'ev','created_at':'2026-09-19T00:00:00Z'}]}
    monkeypatch.setattr('app.runtime.download_attachment',AsyncMock(return_value=b'bytes'))
    rt=Runtime(db,ma)
    await rt.step('b') # upload image
    await rt.step('b') # checking
    ma.send.assert_not_awaited()
    rt=Runtime(db,ma) # persisted file ID prevents reupload
    state['file_image']='available'
    await rt.step('b') # upload PDF
    await rt.step('b') # PDF still checking
    ma.send.assert_not_awaited()
    state['file_pdf']='available'
    await rt.step('b')
    assert ma.upload_file.await_count==2
    assert db.one('SELECT * FROM jobs')['status']=='running'
    attachments=json.loads(db.one('SELECT * FROM jobs')['attachments'])
    assert '/mnt/session/uploads/wechat/' in ma.send.call_args.args[1]
    assert len(ma.send.call_args.args)==2
    assert [a['status'] for a in attachments]==['available','available']
    assert attachments[1]['filename']=='report.pdf'
    db.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('status,deadline', [('rejected',300),('type_rejected',300),('checking',-1)])
async def test_failed_review_never_sends(tmp_path,monkeypatch,status,deadline):
    db,_=make_store(tmp_path)
    attachments=json.loads(db.one('SELECT * FROM jobs')['attachments'])[:1]
    attachments[0].update(file_id='file_1',status='checking',deadline=time.time()+deadline)
    db.update('jobs','id',1,status='checking',attachments=json.dumps(attachments))
    ma=AsyncMock();ma.request.return_value={'status':status}
    await Runtime(db,ma).step('b')
    ma.send.assert_not_awaited()
    assert db.one('SELECT * FROM jobs')['status']=='reply_pending'
    assert db.one('SELECT * FROM jobs')['error']
    db.db.close()


@pytest.mark.asyncio
async def test_ma_multimodal_contract_and_review_guard(monkeypatch):
    monkeypatch.setenv('BAILIAN_WORKSPACE_ID','ws_test')
    calls=[]
    def handler(r):
        calls.append(r)
        return httpx.Response(200,json={'data':[]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ma=ManagedAgent(client)
        attachments=[{'type':'image','file_id':'file_i','status':'available'},
            {'type':'file','file_id':'file_f','filename':'report.pdf','status':'available'}]
        await ma.send('sesn_1','说明',attachments)
        blocks=json.loads(calls[0].content)['input'][0]['content']
        assert blocks[0]['text'].startswith('说明')
        assert blocks[1:]==[{'type':'image','file_id':'file_i'},
            {'type':'file','file_id':'file_f','filename':'report.pdf'}]
        attachments[0]['status']='checking'
        with pytest.raises(ValueError): await ma.send('sesn_1','说明',attachments)
        assert len(calls)==1
        await ma.upload_file('image.jpg',b'jpeg-test-bytes')
        assert b'Content-Type: image/jpeg' in calls[-1].content
        await ma.interrupt('sesn_1')
        assert json.loads(calls[-1].content)=={'input':[{'role':'user','type':'interrupt'}]}


@pytest.mark.asyncio
async def test_download_decrypt_and_bounds(monkeypatch):
    from app import media
    from wechatbot.crypto import encrypt_aes_ecb
    import base64
    key=b'1234567890abcdef'
    ciphertext=encrypt_aes_ecb(b'PDF test',key)
    real_client=httpx.AsyncClient
    def handler(r):
        assert r.url.host=='novac2c.cdn.weixin.qq.com'
        assert 'authorization' not in r.headers
        return httpx.Response(200,content=ciphertext)
    monkeypatch.setattr(media.httpx,'AsyncClient',lambda **kw: real_client(transport=httpx.MockTransport(handler),**kw))
    a={'query':'a+/=', 'key':base64.b64encode(key).decode(),'type':'file','filename':'test.pdf'}
    assert await media.download_attachment(a)==b'PDF test'
    monkeypatch.setattr(media,'MAX_FILE_BYTES',2)
    with pytest.raises(ValueError,match='10 MB'): await media.download_attachment(a)
