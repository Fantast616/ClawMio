import json

import httpx
import pytest
from dotenv import dotenv_values

from clawmio import provision as bootstrap_ma
from scripts.bootstrap_ma import Provisioner, SetupError


def provisioner(tmp_path,handler):
    env=tmp_path/'.env'
    if not env.exists():
        env.write_text('DASHSCOPE_API_KEY=offline-key\n',encoding='utf-8')
    client=httpx.Client(base_url='https://example.com/api/',transport=httpx.MockTransport(handler))
    return Provisioner(client,env,tmp_path/'state.json','ws-test')


def test_repeated_setup_reuses_and_preserves_key(tmp_path):
    requests=[]
    def handler(request):
        requests.append(request.method)
        if request.method=='POST':
            return httpx.Response(200,json={'id':'agent_test'})
        if request.url.path.endswith('/agent_test'):
            return httpx.Response(200,json={'id':'agent_test'})
        return httpx.Response(200,json={'data':[]})
    p=provisioner(tmp_path,handler)
    p.ensure('agents','DEFAULT_AGENT_ID',{'name':'test'})
    p=provisioner(tmp_path,handler)
    p.ensure('agents','DEFAULT_AGENT_ID',{'name':'changed'})
    assert requests==['GET','POST','GET']
    assert dotenv_values(tmp_path/'.env')=={'DASHSCOPE_API_KEY':'offline-key','DEFAULT_AGENT_ID':'agent_test'}


def test_lost_response_does_not_post_again_then_recovers(tmp_path):
    calls=[]
    def lost(request):
        calls.append(request.method)
        if request.method=='POST':
            raise httpx.ReadTimeout('lost',request=request)
        return httpx.Response(200,json={'data':[]})
    p=provisioner(tmp_path,lost)
    with pytest.raises(SetupError,match='Network'):
        p.ensure('agents','DEFAULT_AGENT_ID',{})
    with pytest.raises(SetupError,match='uncertain'):
        p.ensure('agents','DEFAULT_AGENT_ID',{})
    assert calls.count('POST')==1
    installation=p.state['installation']
    def recovered(request):
        assert request.method=='GET'
        return httpx.Response(200,json={'data':[{'id':'agent_recovered','metadata':{'clawbridge_installation':installation}}]})
    p=provisioner(tmp_path,recovered)
    assert p.ensure('agents','DEFAULT_AGENT_ID',{})=='agent_recovered'


def test_definitive_rejection_allows_corrected_retry(tmp_path):
    def handler(request):
        return httpx.Response(400,json={}) if request.method=='POST' else httpx.Response(200,json={'data':[]})
    p=provisioner(tmp_path,handler)
    with pytest.raises(SetupError,match='400'):
        p.ensure('agents','DEFAULT_AGENT_ID',{})
    assert 'agents' not in json.loads((tmp_path/'state.json').read_text())


def test_workspace_change_cannot_reuse_state(tmp_path):
    p=provisioner(tmp_path,lambda r:httpx.Response(200,json={}))
    with pytest.raises(SetupError,match='another workspace'):
        Provisioner(p.client,tmp_path/'.env',tmp_path/'state.json','ws-other')


def test_preview_is_offline_and_does_not_write(tmp_path,monkeypatch):
    (tmp_path/'.env').write_text('BAILIAN_WORKSPACE_ID=ws-test\n',encoding='utf-8')
    (tmp_path/'docs').mkdir()
    (tmp_path/'docs/agent-system-prompt.txt').write_text('test',encoding='utf-8')
    monkeypatch.setattr(bootstrap_ma,'ROOT',tmp_path)
    def forbidden(*a,**kw):
        pytest.fail('Preview must not access network')
    monkeypatch.setattr(httpx,'Client',forbidden)
    assert bootstrap_ma.main([])==0
    assert not (tmp_path/'data').exists()


def test_existing_archived_resource_is_rejected(tmp_path):
    p=provisioner(tmp_path,lambda r:httpx.Response(200,json={'id':'agent_test','archived_at':'2026-01-01'}))
    with pytest.raises(SetupError,match='archived'):
        p.ensure('agents','DEFAULT_AGENT_ID',{},'agent_test')
