"""Preview or provision ClawBridge MA resources; never executes a model task."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import sys

import httpx
from dotenv import dotenv_values, set_key

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
    def __init__(self,client,env_path,state_path,workspace):
        self.client=client
        self.env_path=env_path
        self.state_path=state_path
        self.state=json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
        if self.state and self.state['workspace']!=workspace:
            raise SetupError('Bootstrap state belongs to another workspace. Use a separate checkout/configuration.')
        if not self.state:
            self.state={'workspace':workspace,'installation':secrets.token_hex(16)}
            save_state(state_path,self.state)

    def request(self,method,path,**kwargs):
        try:
            response=self.client.request(method,path,**kwargs)
        except httpx.HTTPError:
            raise SetupError('Network error; creation may have succeeded. Re-run to reconcile; do not delete bootstrap state.') from None
        if response.is_error:
            raise SetupError(f'MA HTTP {response.status_code}; check workspace, permissions and model in console. Response body omitted.',response.status_code)
        try:
            return response.json()
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


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true',help='Create missing resources and write IDs to .env (default: offline preview)')
    parser.add_argument('--model',default='auto',help='MA model ID available in your workspace (default: auto)')
    parser.add_argument('--name',default='clawbridge',help='Resource name prefix')
    parser.add_argument('--skill-id',help='Optional existing approved customer Skill')
    parser.add_argument('--skill-version',help='Exact Skill version, required with --skill-id')
    args=parser.parse_args(argv)
    if bool(args.skill_id)!=bool(args.skill_version):
        parser.error('--skill-id and --skill-version must be specified together')
    env_path=ROOT/'.env'
    if not env_path.exists():
        raise SetupError('Run python scripts/setup_env.py and fill .env first.')
    config=dotenv_values(env_path)
    workspace=config.get('BAILIAN_WORKSPACE_ID','')
    region=config.get('BAILIAN_REGION') or 'cn-beijing'
    if not re.fullmatch(r'ws-[a-zA-Z0-9]+',workspace or '') or not re.fullmatch(r'[a-z0-9-]+',region):
        raise SetupError('Fill BAILIAN_WORKSPACE_ID and BAILIAN_REGION in .env; the key alone does not identify the API endpoint.')
    agent={'name':args.name+'-agent','model':{'id':args.model},
        'system':(ROOT/'docs/agent-system-prompt.txt').read_text(encoding='utf-8'),
        'tools':[{'type':'builtin_toolkit','default_config':{'enabled':False},
            'configs':[{'name':name,'enabled':True,'permission_policy':{'type':'always_allow'}}
                for name in ('bash','read','write','edit','glob','grep','web_search','mark_artifacts')]}]}
    if args.skill_id:
        agent['skills']=[{'type':'customer','skill_id':args.skill_id,'version':args.skill_version}]
    environment={'name':args.name+'-environment','config':{'type':'cloud','networking':{'type':'unrestricted'}}}
    print('Plan: reuse configured IDs; create missing Environment and Agent; save IDs to .env.')
    print(f'Model for NEW Agent: {args.model}; tools: bash/read/write/edit/glob/grep/web_search/mark_artifacts (always_allow).')
    print('Environment permits outbound network. No Session/model task, Skill upload or existing resource update is performed.')
    if not args.apply:
        print('Offline preview only. Re-run with the same options plus --apply to provision cloud resources.')
        return 0
    key=config.get('DASHSCOPE_API_KEY') or ''
    if not key or key.startswith('replace-'):
        raise SetupError('Fill DASHSCOPE_API_KEY in .env.')
    state_path=ROOT/'data/bootstrap-ma.json'
    state_path.parent.mkdir(exist_ok=True)
    lock=state_path.with_suffix('.lock')
    try:
        fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    except FileExistsError:
        raise SetupError('Bootstrap lock exists. Ensure no bootstrap is running before removing data/bootstrap-ma.lock.') from None
    try:
        os.close(fd)
        with httpx.Client(base_url=f'https://{workspace}.{region}.maas.aliyuncs.com/api/v1/agentstudio/',
                timeout=60,headers={'Authorization':'Bearer '+key,
                    'User-Agent':f'AlibabaCloud-Agent-Skills/alibabacloud-bailian-managed-agent/{secrets.token_hex(16)} skill-version/0.0.1-beta.1'}) as client:
            provision=Provisioner(client,env_path,state_path,workspace)
            provision.ensure('environments','DEFAULT_ENVIRONMENT_ID',environment,config.get('DEFAULT_ENVIRONMENT_ID') or '')
            provision.ensure('agents','DEFAULT_AGENT_ID',agent,config.get('DEFAULT_AGENT_ID') or '')
        print('Ready. Start ClawBridge and bind a Bot. Configure billing rates separately in the admin UI.')
    finally:
        lock.unlink()
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except SetupError as exc:
        print(str(exc),file=sys.stderr)
        raise SystemExit(1)
