"""Preview or provision ClawMio MA resources; never executes a model task."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import sys
import io
import time
from zipfile import ZipFile, ZIP_DEFLATED

import httpx
from dotenv import dotenv_values, set_key
from .paths import assets
from .locking import exclusive

ROOT=Path(__file__).resolve().parents[1]


class SetupError(RuntimeError):
    def __init__(self,message,status=None):
        super().__init__(message)
        self.status=status


def save_state(path,state):
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(state,indent=2),encoding='utf-8')
    temp.replace(path)


class Provisioner:
    def __init__(self,client,env_path,state_path,workspace,region='cn-beijing'):
        self.client=client
        self.env_path=env_path
        self.state_path=state_path
        self.state=json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
        if self.state and self.state['workspace']!=workspace:
            raise SetupError('Bootstrap state belongs to another workspace. Use a separate checkout/configuration.')
        if self.state and self.state.get('region','cn-beijing')!=region:
            raise SetupError('Bootstrap state belongs to another region. Use a separate configuration directory.')
        if not self.state:
            self.state={'workspace':workspace,'region':region,'installation':secrets.token_hex(16)}
            save_state(state_path,self.state)

    def request(self,method,path,**kwargs):
        try:
            response=self.client.request(method,path,**kwargs)
        except httpx.HTTPError:
            raise SetupError('Network error; creation may have succeeded. Re-run to reconcile; do not delete bootstrap state.') from None
        if response.is_error:
            raise SetupError(f'MA HTTP {response.status_code}; check workspace, permissions and model in console. Response body omitted.',response.status_code)
        try:
            result=response.json()
            if not isinstance(result,dict):
                raise ValueError('Expected object')
            return result
        except ValueError:
            raise SetupError('Invalid MA response; preserve bootstrap state and reconcile before retrying.') from None

    def ensure(self,kind,config_key,payload,configured_id=''):
        resource_id=configured_id or self.state.get(kind,{}).get('id')
        if resource_id:
            if not re.fullmatch(r'[A-Za-z0-9_-]+',resource_id):
                raise SetupError('Invalid resource ID.')
            resource=self.request('GET',f'{kind}/{resource_id}')
            if resource.get('archived_at'):
                raise SetupError(f'{kind}: configured resource is archived.')
        else:
            matches=[]
            params={'limit':100}
            while True:
                page=self.request('GET',kind,params=params)
                matches.extend(r for r in page.get('data',[]) if
                    (r.get('metadata') or {}).get('clawbridge_installation')==self.state['installation'] and not r.get('archived_at'))
                if not page.get('next_page'):
                    break
                params['page']=page['next_page']
            if len(matches)>1:
                raise SetupError(f'{kind}: multiple matching resources; set an explicit ID in .env.')
            if matches:
                resource=matches[0]
            elif self.state.get(kind,{}).get('pending'):
                raise SetupError(f'{kind}: previous creation result is uncertain. Check console and set its ID in .env; no duplicate POST sent.')
            else:
                self.state[kind]={'pending':True}
                save_state(self.state_path,self.state)
                try:
                    resource=self.request('POST',kind,json={**payload,'metadata':{'clawbridge_installation':self.state['installation']}})
                except SetupError as exc:
                    if exc.status in (400,401,403,404,422):
                        self.state.pop(kind,None)
                        save_state(self.state_path,self.state)
                    raise
        if not resource.get('id'):
            raise SetupError(f'{kind}: missing resource ID; preserve state for reconciliation.')
        self.state[kind]={'id':resource['id']}
        save_state(self.state_path,self.state)
        set_key(str(self.env_path),config_key,resource['id'])
        print(f'{config_key} saved to .env')
        return resource['id']


def skill_zip():
    buffer=io.BytesIO()
    source=assets()/'skills/claw-bot-self-service'
    with ZipFile(buffer,'w',ZIP_DEFLATED) as archive:
        for name in ('SKILL.md','scripts/bot_api.py'):
            archive.writestr(name,(source/name).read_bytes())
    return buffer.getvalue()


def checked_id(value):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+',str(value or '')):
        raise SetupError('Invalid resource ID/version in bootstrap state.')
    return str(value)


def create_once(p,stage,path,**kwargs):
    if p.state.get(stage,{}).get('pending'):
        raise SetupError(f'{stage}: previous creation is uncertain; check console, then use '
            'clawmio config set SETUP_SKILL_FILE_ID <file_id> or SETUP_SKILL_ID <skill_id> to reconcile. No duplicate POST sent.')
    p.state[stage]={'pending':True}
    save_state(p.state_path,p.state)
    try:
        result=p.request('POST',path,**kwargs)
    except SetupError as exc:
        if exc.status in (400,401,403,404,422):
            p.state.pop(stage,None)
            save_state(p.state_path,p.state)
        raise
    if not result.get('id'):
        raise SetupError(f'{stage}: response missing ID; preserve state and reconcile in console.')
    p.state[stage]={'id':checked_id(result['id'])}
    save_state(p.state_path,p.state)
    return result


def wait_review(p,path,ready,deadline):
    while True:
        result=p.request('GET',path)
        if result.get('status')==ready:
            return result
        if result.get('status') not in ('checking','processing','pending'):
            raise SetupError('Skill/file review rejected or returned an unknown state. Check the MA console; nothing mounted.')
        if time.monotonic()>=deadline:
            raise SetupError('Skill review is still pending. Progress saved; run clawmio provision --apply again later.')
        print('Skill 正在安全审核，进度已保存…',flush=True)
        time.sleep(min(5,max(0,deadline-time.monotonic())))


def ensure_skill(p,wait_seconds):
    config=dotenv_values(p.env_path)
    skill_id=config.get('SETUP_SKILL_ID') or p.state.get('skill',{}).get('id')
    deadline=time.monotonic()+max(0,wait_seconds)
    if not skill_id:
        file_id=config.get('SETUP_SKILL_FILE_ID') or p.state.get('skill_file',{}).get('id')
        if not file_id:
            file_id=create_once(p,'skill_file','files',files={
                'file':('claw-bot-self-service.zip',skill_zip(),'application/zip')})['id']
        wait_review(p,'files/'+checked_id(file_id),'available',deadline)
        skill_id=create_once(p,'skill','skills',json={'file_id':file_id})['id']
    skill=p.request('GET','skills/'+checked_id(skill_id))
    version=p.state.get('skill',{}).get('version') or skill.get('latest_version')
    if not version:
        raise SetupError('Skill version not available yet; retry provision later. No duplicate upload sent.')
    p.state['skill']={'id':checked_id(skill_id),'version':checked_id(version)}
    save_state(p.state_path,p.state)
    wait_review(p,f'skills/{skill_id}/versions/{version}','active',deadline)
    return {'type':'customer','skill_id':skill_id,'version':version}


def sync_skill(p,agent_id,desired):
    agent=p.request('GET','agents/'+checked_id(agent_id))
    managed_id=p.state.get('skill',{}).get('id')
    if not managed_id:
        return
    current=agent.get('skills') or []
    wanted=[s for s in current if s.get('skill_id')!=managed_id]+desired
    def mounts(items):
        return sorted((s.get('type',''),s.get('skill_id',''),str(s.get('version',''))) for s in items)
    pending=p.state.get('skill_mount')
    if pending and (pending.get('agent_id')!=agent_id or pending.get('mounts')!=[list(m) for m in mounts(wanted)]):
        raise SetupError('Previous Skill update has a different target. Reconcile it in console before changing the plan.')
    if mounts(current)==mounts(wanted):
        if pending:
            p.state.pop('skill_mount')
            save_state(p.state_path,p.state)
        return
    if pending:
        raise SetupError('Previous Skill mount update is uncertain. Check Agent in console before changing bootstrap skill_mount state.')
    if agent.get('version') is None:
        raise SetupError('Agent version is missing; cannot safely update its Skill mounts.')
    p.state['skill_mount']={'pending':True,'agent_id':agent_id,'version':agent['version'],
        'mounts':[list(m) for m in mounts(wanted)]}
    save_state(p.state_path,p.state)
    try:
        p.request('POST','agents/'+agent_id,json={'version':agent['version'],'skills':wanted})
    except SetupError as exc:
        if exc.status in (400,401,403,404,409,422):
            p.state.pop('skill_mount',None)
            save_state(p.state_path,p.state)
        raise
    p.state.pop('skill_mount',None)
    save_state(p.state_path,p.state)
    print('Skill 挂载已同步；已有 Bot 需要新建 Session 才能使用新配置。')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true',help='Create missing resources and write IDs to .env (default: offline preview)')
    parser.add_argument('--model',default='auto',help='MA model ID available in your workspace (default: auto)')
    parser.add_argument('--name',default='clawmio',help='Resource name prefix')
    parser.add_argument('--skill-id',help='Optional existing approved customer Skill')
    parser.add_argument('--skill-version',help='Exact Skill version, required with --skill-id')
    parser.add_argument('--home',type=Path,default=ROOT,help='Configuration directory')
    parser.add_argument('--auto-skill',action='store_true',help='Upload/reconcile bundled Skill when Base URL is configured')
    parser.add_argument('--skill-wait',type=int,default=300,help='Maximum seconds to wait for Skill review')
    args=parser.parse_args(argv)
    if bool(args.skill_id)!=bool(args.skill_version):
        parser.error('--skill-id and --skill-version must be specified together')
    env_path=args.home/'.env'
    if not env_path.exists():
        raise SetupError('Run python scripts/setup_env.py and fill .env first.')
    config=dotenv_values(env_path)
    workspace=config.get('BAILIAN_WORKSPACE_ID','')
    region=config.get('BAILIAN_REGION') or 'cn-beijing'
    if not re.fullmatch(r'ws-[a-zA-Z0-9]+',workspace or '') or not re.fullmatch(r'[a-z0-9-]+',region):
        raise SetupError('Fill BAILIAN_WORKSPACE_ID and BAILIAN_REGION in .env; the key alone does not identify the API endpoint.')
    agent={'name':args.name+'-agent','model':{'id':args.model},
        'system':(assets()/'docs/agent-system-prompt.txt').read_text(encoding='utf-8'),
        'tools':[{'type':'builtin_toolkit','default_config':{'enabled':False},
            'configs':[{'name':name,'enabled':True,'permission_policy':{'type':'always_allow'}}
                for name in ('bash','read','write','edit','glob','grep','web_search','mark_artifacts')]}]}
    if args.skill_id:
        agent['skills']=[{'type':'customer','skill_id':args.skill_id,'version':args.skill_version}]
    environment={'name':args.name+'-environment','config':{'type':'cloud','networking':{'type':'unrestricted'}}}
    print('Plan: reuse configured IDs; create missing Environment and Agent; save IDs to .env.')
    print(f'Model for NEW Agent: {args.model}; tools: bash/read/write/edit/glob/grep/web_search/mark_artifacts (always_allow).')
    print('Environment permits outbound network. No Session/model task is executed.')
    if args.auto_skill:
        print('With Base URL: upload/reconcile bundled Skill and synchronize its mount on the configured Agent; otherwise skip upload and remove its managed mount.')
    else:
        print('No Skill upload or existing resource update is performed.')
    if not args.apply:
        print('Offline preview only. Re-run with the same options plus --apply to provision cloud resources.')
        return 0
    key=config.get('DASHSCOPE_API_KEY') or ''
    if not key or key.startswith('replace-'):
        raise SetupError('Fill DASHSCOPE_API_KEY in .env.')
    state_path=args.home/'data/bootstrap-ma.json'
    state_path.parent.mkdir(exist_ok=True)
    with exclusive(state_path.with_suffix('.lock')):
        with httpx.Client(base_url=f'https://{workspace}.{region}.maas.aliyuncs.com/api/v1/agentstudio/',
                timeout=60,headers={'Authorization':'Bearer '+key,
                    'User-Agent':f'AlibabaCloud-Agent-Skills/alibabacloud-bailian-managed-agent/{secrets.token_hex(16)} skill-version/0.0.1-beta.1'}) as client:
            provision=Provisioner(client,env_path,state_path,workspace,region)
            if args.auto_skill and config.get('CLAW_API_BASE_URL'):
                skill=ensure_skill(provision,args.skill_wait)
                agent['skills']=[skill]
            provision.ensure('environments','DEFAULT_ENVIRONMENT_ID',environment,config.get('DEFAULT_ENVIRONMENT_ID') or '')
            agent_id=provision.ensure('agents','DEFAULT_AGENT_ID',agent,config.get('DEFAULT_AGENT_ID') or '')
            if args.auto_skill:
                sync_skill(provision,agent_id,agent.get('skills',[]))
                current=dotenv_values(env_path)
                provision.state['cli_config']={key:current.get(key) or '' for key in
                    ('CLAW_API_BASE_URL','DEFAULT_AGENT_ID','DEFAULT_ENVIRONMENT_ID')}
                save_state(state_path,provision.state)
        print('Ready. Start ClawMio and bind a Bot. Configure billing rates separately in the admin UI.')
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except SetupError as exc:
        print(str(exc),file=sys.stderr)
        raise SystemExit(1)
