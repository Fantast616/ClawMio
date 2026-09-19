import json
from unittest.mock import AsyncMock
import httpx
import pytest
from app.ma import ManagedAgent,marked_artifacts
from app.store import Store
from app.runtime import Runtime
from app.media import upload_wechat_file
from wechatbot.crypto import decode_aes_key,decrypt_aes_ecb
from wechatbot.protocol import ILinkApi


def ev(eid,kind,data):
    return {'id':eid,'type':kind,'content':[{'type':'data','data':data}]}


def test_only_current_successful_mark_artifacts_are_delivered():
    output={'call_id':'call1','output':json.dumps({'marked':[{'file_id':'file_one','path':'/mnt/session/outputs/report.pdf'}],'failed':[]})}
    events=[ev('old','tool_call_output',output),{'id':'start'},
        ev('call','tool_call',{'name':'mark_artifacts','call_id':'call1'}),ev('out','tool_call_output',output),
        ev('out','tool_call_output',output),ev('other','tool_call_output',{'call_id':'forged','output':output['output']})]
    assert [x['file_id'] for x in marked_artifacts(events,'start')]==['file_one']
    events[3]['is_error']=True;events[4]['is_error']=True
    assert marked_artifacts(events,'start')==[]


@pytest.mark.asyncio
async def test_mount_reuses_existing_path_and_download_checks_scope(monkeypatch):
    monkeypatch.setenv('BAILIAN_WORKSPACE_ID','ws_test')
    requests=[]
    def handler(r):
        requests.append(r)
        if r.url.path.endswith('/resources'):
            return httpx.Response(200,json={'data':[{'id':'r1','mount_path':'/mnt/session/uploads/j1/a.txt'}]})
        if r.url.path.endswith('/files'):
            assert r.url.params['scope_id']=='sesn_1'
            return httpx.Response(200,json={'data':[{'id':'file_one','downloadable':True,'size_bytes':3}]})
        return httpx.Response(200,content=b'abc')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ma=ManagedAgent(client)
        assert (await ma.mount_file('sesn_1','file_input','/uploads/j1/a.txt'))['id']=='r1'
        assert all(r.method=='GET' for r in requests)
        assert await ma.download_artifact('sesn_1','file_one')==b'abc'
        with pytest.raises(ValueError):await ma.download_artifact('sesn_1','file_foreign')


@pytest.mark.asyncio
async def test_wechat_file_encryption_and_payload():
    wx=AsyncMock();wx.build_cdn_upload_url=ILinkApi.build_cdn_upload_url
    wx.get_upload_url.return_value={'upload_param':'param'}
    wx.upload_to_cdn.return_value='cdn-query'
    bot={'base_url':'https://ilinkai.weixin.qq.com','token':'token','user_id':'u'}
    item=await upload_wechat_file(wx,bot,b'file bytes','report.pdf')
    assert item['type']==4 and item['file_item']['len']=='10'
    assert wx.get_upload_url.call_args.kwargs['media_type']==3
    ciphertext=wx.upload_to_cdn.call_args.args[1]
    assert decrypt_aes_ecb(ciphertext,decode_aes_key(item['file_item']['media']['aes_key']))==b'file bytes'


@pytest.mark.asyncio
async def test_artifact_resume_does_not_resend_successful_files(tmp_path,monkeypatch):
    db=Store(tmp_path/'files.db')
    db.execute("INSERT INTO bots(id,name,token,user_id,context_token,session_id) VALUES('b','B','t','u','ctx','sesn_1')")
    arts=[{'file_id':'file_done','filename':'done.pdf','status':'sent'},
          {'file_id':'file_new','filename':'new.pdf','status':'pending','attempts':0}]
    db.execute("INSERT INTO jobs(bot_id,message_id,user_id,text,status,session_id,result,reply_part,artifacts) VALUES('b','m','u','hi','reply_pending','sesn_1','summary',1,?)",(json.dumps(arts),))
    ma=AsyncMock();ma.download_artifact.return_value=b'file'
    upload=AsyncMock(return_value={'type':4,'file_item':{'file_name':'new.pdf'}})
    monkeypatch.setattr('app.runtime.upload_wechat_file',upload)
    rt=Runtime(db,ma);rt.wx=AsyncMock()
    await rt.step('b')
    assert db.one('SELECT * FROM jobs')['status']=='done'
    ma.download_artifact.assert_awaited_once_with('sesn_1','file_new')
    rt.wx.send_message.assert_awaited_once()
    sent=rt.wx.send_message.call_args.args[2]
    assert sent['context_token']=='ctx' and sent['client_id'].endswith('-file-1')
    await rt.step('b')
    rt.wx.send_message.assert_awaited_once()
    db.db.close()
