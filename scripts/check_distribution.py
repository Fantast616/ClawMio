"""Install a wheel into a temporary target and smoke-test it outside the checkout.

Uses a fresh, empty database and fake credentials; performs local HTTP only.
Run after `python -m build` with the development/runtime dependencies installed.
"""
import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from zipfile import ZipFile

import httpx


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wheel',type=Path)
    args=parser.parse_args()
    wheel=args.wheel.resolve()
    with ZipFile(wheel) as archive:
        names=set(archive.namelist())
        required={
            'clawmio/resources/static/index.html',
            'clawmio/resources/static/app.js',
            'clawmio/resources/static/style.css',
            'clawmio/resources/docs/agent-system-prompt.txt',
            'clawmio/resources/skills/claw-bot-self-service/SKILL.md',
            'clawmio/resources/skills/claw-bot-self-service/scripts/bot_api.py',
        }
        assert required<=names, 'Missing packaged assets'
        for name in names:
            path=Path(name)
            assert not (path.name.startswith('.env') or path.suffix in ('.db','.log','.zip') or
                any(part in ('data','logs','artifacts') for part in path.parts)), 'Private file in wheel'
        entry=next(n for n in names if n.endswith('.dist-info/entry_points.txt'))
        assert b'clawmio = clawmio.cli:main' in archive.read(entry)
    with tempfile.TemporaryDirectory(prefix='clawmio-wheel-') as directory:
        root=Path(directory)
        target=root/'installed'
        home=root/'instance'
        home.mkdir()
        subprocess.run([sys.executable,'-m','pip','install','--no-deps','--no-compile',
            '--target',str(target),str(wheel)],check=True,cwd=root)
        env={**os.environ,'PYTHONPATH':str(target),'PYTHONUTF8':'1','CLAWMIO_HOME':str(home)}
        # Don't inherit development environment credentials. CLI file values override these too.
        for key in ('DASHSCOPE_API_KEY','BAILIAN_WORKSPACE_ID','BAILIAN_REGION','ADMIN_PASSWORD',
            'COOKIE_SECRET','DATABASE_PATH','CLAW_API_BASE_URL','PUBLIC_BASE_URL',
            'DEFAULT_AGENT_ID','DEFAULT_ENVIRONMENT_ID'):
            env.pop(key,None)
        (home/'.env').write_text(
            'DASHSCOPE_API_KEY=offline-key\nBAILIAN_WORKSPACE_ID=ws-test\n'
            'ADMIN_PASSWORD=offline-password\nCOOKIE_SECRET=offline-cookie\n'
            'DEFAULT_AGENT_ID=agent_test\nDEFAULT_ENVIRONMENT_ID=env_test\n',encoding='utf-8')
        command=[sys.executable,'-m','clawmio']
        def run(*arguments,expected=0):
            result=subprocess.run([*command,*arguments],cwd=root,env=env,capture_output=True,
                text=True,encoding='utf-8',timeout=45)
            assert result.returncode==expected,result.stdout+result.stderr
            return result
        # Confirm imports come from the installed wheel, not the repository.
        subprocess.run([sys.executable,'-c',
            'import clawmio, app; from pathlib import Path; '
            'assert Path(clawmio.__file__).is_relative_to(Path("installed").resolve()); '
            'assert Path(app.__file__).is_relative_to(Path("installed").resolve())'],
            cwd=root,env=env,check=True)
        run('--version')
        run('doctor')
        run('provision')  # Offline only.
        run('skill-package')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            port=sock.getsockname()[1]
        try:
            run('start','--port',str(port))
            run('status')
            with httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False) as client:
                for path in ('/','/static/app.js','/static/style.css'):
                    assert client.get(path).status_code==200,path
                assert client.get('/api/overview').status_code==401
                assert client.post('/api/login',json={'password':'offline-password'},
                    headers={'x-claw-request':'1'}).status_code==200
                assert client.get('/api/overview').json()['bots']==[]
            assert (home/'data/claw.db').is_file()
        finally:
            run('stop')
        run('status',expected=1)
    print('Wheel assets, isolated installation, local startup/login/status/stop: passed. No cloud/WeChat verification.')


if __name__=='__main__':
    main()
