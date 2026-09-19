import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import zipfile

from dotenv import dotenv_values
import httpx
import pytest

from clawmio import cli, provision
from clawmio.locking import exclusive, Locked, is_locked


def configured(home, **changes):
    values={**cli.DEFAULTS,'DASHSCOPE_API_KEY':'offline-key','BAILIAN_WORKSPACE_ID':'ws-test',
        'ADMIN_PASSWORD':'offline-password','COOKIE_SECRET':'offline-cookie',
        'DEFAULT_AGENT_ID':'agent_test','DEFAULT_ENVIRONMENT_ID':'env_test',**changes}
    cli.write_config(home,values)
    return values


def test_first_wizard_key_first_optional_skill_and_preserve(tmp_path,monkeypatch,capsys):
    monkeypatch.setattr(sys.stdin,'isatty',lambda:True)
    calls=[]
    monkeypatch.setattr(cli.getpass,'getpass',lambda prompt:calls.append(prompt) or 'offline-secret-key')
    values=iter(['ws-test','','','','',''])
    monkeypatch.setattr('builtins.input',lambda _:next(values))
    monkeypatch.setattr(provision,'main',lambda _:pytest.fail('no-provision must be offline'))
    assert cli.main(['--home',str(tmp_path),'init','--no-provision'])==0
    result=dotenv_values(tmp_path/'.env')
    assert 'API Key' in calls[0]
    assert result['CLAW_API_BASE_URL']==''
    assert result['ADMIN_PASSWORD'] and result['COOKIE_SECRET']
    assert 'offline-secret-key' not in capsys.readouterr().out
    saved_password=result['ADMIN_PASSWORD']
    monkeypatch.setattr(cli.getpass,'getpass',lambda _:'')
    values=iter(['','','','','',''])
    assert cli.main(['--home',str(tmp_path),'configure'])==0
    assert dotenv_values(tmp_path/'.env')['ADMIN_PASSWORD']==saved_password


@pytest.mark.parametrize('value',['http://localhost:8000','http://127.0.0.1','http://[::]','ftp://a.test',
    'https://a.test/api/bot','https://user:password@a.test','https://a.test?q=x','https://a.test:bad'])
def test_callback_rejects_unusable_origins(value):
    with pytest.raises(ValueError):
        cli.validate('CLAW_API_BASE_URL',value)


def test_config_redacts_secrets_and_rejects_cli_secret(tmp_path,capsys):
    configured(tmp_path)
    assert cli.main(['--home',str(tmp_path),'config','show'])==0
    output=capsys.readouterr().out
    assert 'offline-key' not in output and 'offline-password' not in output and 'offline-cookie' not in output
    assert cli.main(['--home',str(tmp_path),'config','set','DASHSCOPE_API_KEY','dont-log-this'])==1
    assert 'dont-log-this' not in capsys.readouterr().err
    assert dotenv_values(tmp_path/'.env')['DASHSCOPE_API_KEY']=='offline-key'


def test_config_locked_while_running_and_region_protected(tmp_path):
    configured(tmp_path)
    with exclusive(tmp_path/'run.lock'):
        assert cli.main(['--home',str(tmp_path),'config','set','PORT','9999'])==1
    assert dotenv_values(tmp_path/'.env')['PORT']=='8000'
    (tmp_path/'data').mkdir()
    (tmp_path/'data/bootstrap-ma.json').write_text('{}')
    assert cli.main(['--home',str(tmp_path),'config','set','BAILIAN_REGION','cn-shanghai'])==1


def test_lock_released_after_exception(tmp_path):
    lock=tmp_path/'worker.lock'
    with pytest.raises(ValueError):
        with exclusive(lock):
            assert is_locked(lock)
            with pytest.raises(Locked):
                with exclusive(lock):
                    pass
            raise ValueError()
    assert not is_locked(lock)


def test_provision_preview_offline(tmp_path,monkeypatch):
    configured(tmp_path)
    monkeypatch.setattr(httpx,'Client',lambda **kwargs:pytest.fail('no network in preview'))
    assert cli.main(['--home',str(tmp_path),'provision'])==0
    assert not (tmp_path/'data/bootstrap-ma.json').exists()


def test_skill_package_allowlist():
    with zipfile.ZipFile(io.BytesIO(provision.skill_zip())) as z:
        assert set(z.namelist())=={'SKILL.md','scripts/bot_api.py'}
        assert b'name: claw-bot-self-service' in z.read('SKILL.md')


def p_for(home,handler):
    configured(home)
    client=httpx.Client(base_url='https://example.test/',transport=httpx.MockTransport(handler))
    return provision.Provisioner(client,home/'.env',home/'state.json','ws-test')


def test_skill_upload_review_resume_no_duplicate_and_mount(tmp_path):
    posts=[]
    approved=False
    def handler(r):
        path=r.url.path
        if r.method=='POST':
            posts.append(path)
            if path=='/files':
                assert b'claw-bot-self-service.zip' in r.content
                return httpx.Response(200,json={'id':'file_test'})
            if path=='/skills':
                assert json.loads(r.content)=={'file_id':'file_test'}
                return httpx.Response(200,json={'id':'skill_test','latest_version':'1.0'})
        if path=='/files/file_test':
            return httpx.Response(200,json={'status':'available'})
        if path=='/skills/skill_test':
            return httpx.Response(200,json={'latest_version':'1.0'})
        return httpx.Response(200,json={'status':'active' if approved else 'checking'})
    p=p_for(tmp_path,handler)
    with pytest.raises(provision.SetupError,match='pending'):
        provision.ensure_skill(p,0)
    approved=True
    p=provision.Provisioner(p.client,p.env_path,p.state_path,'ws-test')
    assert provision.ensure_skill(p,0)=={'type':'customer','skill_id':'skill_test','version':'1.0'}
    assert posts==['/files','/skills']


@pytest.mark.parametrize('stage',['files','skills'])
def test_uncertain_skill_creation_never_reposts(tmp_path,stage):
    posts=[]
    def handler(r):
        if r.method=='POST':
            posts.append(r.url.path)
            if r.url.path=='/'+stage:
                raise httpx.ReadTimeout('secret body must not leak',request=r)
            return httpx.Response(200,json={'id':'file_test'})
        return httpx.Response(200,json={'status':'available'})
    p=p_for(tmp_path,handler)
    with pytest.raises(provision.SetupError,match='Network'):
        provision.ensure_skill(p,0)
    with pytest.raises(provision.SetupError,match='uncertain'):
        provision.ensure_skill(p,0)
    assert posts.count('/'+stage)==1


def test_mount_reconcile_preserves_other_skills_then_disable(tmp_path):
    other={'type':'customer','skill_id':'skill_other','version':'2.0'}
    wanted={'type':'customer','skill_id':'skill_test','version':'1.0'}
    agent={'version':3,'skills':[other]}
    posts=[]
    def handler(r):
        if r.method=='POST':
            body=json.loads(r.content)
            posts.append(body)
            assert body['version']==agent['version']
            agent.update(body)
            agent['version']+=1
            if len(posts)==1:
                raise httpx.ReadTimeout('lost',request=r)
        return httpx.Response(200,json=agent)
    p=p_for(tmp_path,handler)
    p.state['skill']={'id':'skill_test'}
    with pytest.raises(provision.SetupError):
        provision.sync_skill(p,'agent_test',[wanted])
    provision.sync_skill(p,'agent_test',[wanted])
    assert len(posts)==1 and 'skill_mount' not in p.state
    provision.sync_skill(p,'agent_test',[])
    assert agent['skills']==[other] and len(posts)==2


def test_no_base_url_skips_skill_upload(tmp_path,monkeypatch):
    configured(tmp_path,DEFAULT_AGENT_ID='',DEFAULT_ENVIRONMENT_ID='')
    seen=[]
    def handler(r):
        seen.append((r.method,r.url.path))
        kind=r.url.path.rstrip('/').split('/')[-1]
        if r.method=='POST':
            body=json.loads(r.content)
            assert not body.get('skills')
            return httpx.Response(200,json={'id':kind+'_test'})
        return httpx.Response(200,json={'id':'agents_test','data':[]})
    client=httpx.Client(base_url='https://example.test/',transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx,'Client',lambda **kw:client)
    assert cli.main(['--home',str(tmp_path),'provision','--apply','--yes'])==0
    assert not any('skill' in path or 'files' in path for _,path in seen)
    state=json.loads((tmp_path/'data/bootstrap-ma.json').read_text())
    assert state['cli_config']['CLAW_API_BASE_URL']==''


def test_background_lifecycle_isolated_database(tmp_path):
    """Local HTTP only: empty database cannot start any cloud/WeChat work."""
    configured(tmp_path)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]
    root=Path(__file__).resolve().parents[1]
    command=[sys.executable,'-m','clawmio','--home',str(tmp_path)]
    env={**os.environ,'PYTHONPATH':str(root),'PYTHONUTF8':'1'}
    def run(*args):
        return subprocess.run([*command,*args],cwd=tmp_path,env=env,capture_output=True,text=True,encoding='utf-8',timeout=45)
    try:
        result=run('start','--port',str(port))
        assert result.returncode==0,result.stdout+result.stderr
        assert str(port) in result.stdout
        assert run('status').returncode==0
        assert run('start','--port',str(port)).returncode==1
        with httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False) as client:
            assert client.get('/').status_code==200
            assert client.get('/static/app.js').status_code==200
            assert client.get('/api/overview').status_code==401
            assert client.post('/api/login',json={'password':'offline-password'},headers={'x-claw-request':'1'}).status_code==200
            assert client.get('/api/overview').status_code==200
        assert (tmp_path/'data/claw.db').is_file()
        # A different configuration directory still cannot start a second DB worker.
        other=tmp_path/'other'
        configured(other,DATABASE_PATH=str(tmp_path/'data/claw.db'))
        duplicate=run('--home',str(other),'start','--port',str(port+1 if port<65535 else port-1))
        assert duplicate.returncode==1
        assert not is_locked(other/'run.lock')
        restarted=run('restart','--port',str(port))
        assert restarted.returncode==0,restarted.stdout+restarted.stderr
    finally:
        stopped=run('stop')
        assert stopped.returncode==0,stopped.stdout+stopped.stderr
    assert run('status').returncode==1
