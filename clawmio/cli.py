"""ClawMio interactive configuration and single-process service management."""
import argparse
from collections import deque
from contextlib import contextmanager
import getpass
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import webbrowser

from dotenv import dotenv_values, set_key

from . import __version__, provision
from .locking import Locked, exclusive, is_locked
from .paths import assets, home_path

DEFAULTS = {
    'DASHSCOPE_API_KEY':'', 'BAILIAN_WORKSPACE_ID':'', 'BAILIAN_REGION':'cn-beijing',
    'DEFAULT_AGENT_ID':'', 'DEFAULT_ENVIRONMENT_ID':'', 'CLAW_API_BASE_URL':'',
    'PUBLIC_BASE_URL':'', 'HOST':'127.0.0.1', 'PORT':'8000', 'MA_MODEL':'auto',
    'ADMIN_PASSWORD':'', 'COOKIE_SECRET':'', 'DATABASE_PATH':'data/claw.db',
    'SETUP_SKILL_FILE_ID':'', 'SETUP_SKILL_ID':'',
}
SECRET_KEYS = {'DASHSCOPE_API_KEY','ADMIN_PASSWORD','COOKIE_SECRET'}


def read_config(home):
    return {**DEFAULTS, **{k:v or '' for k,v in dotenv_values(home/'.env').items()}}


def write_config(home, values):
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    path=home/'.env'
    if not path.exists():
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        os.close(fd)
    for key,value in values.items():
        set_key(str(path),key,str(value))
    if os.name!='nt':
        path.chmod(0o600)


def validate(key, value):
    value=value.strip()
    if any(ord(c)<32 for c in value):
        raise ValueError('不能包含换行或控制字符。')
    if key=='BAILIAN_WORKSPACE_ID' and not re.fullmatch(r'ws-[A-Za-z0-9]+',value):
        raise ValueError('请输入 MA 工作空间 ID（ws- 开头，可在控制台复制）。')
    if key=='BAILIAN_REGION' and not re.fullmatch(r'[a-z0-9-]+',value):
        raise ValueError('区域只能包含小写字母、数字和连字符。')
    if key=='PORT' and (not value.isdigit() or not 1<=int(value)<=65535):
        raise ValueError('端口必须为 1–65535。')
    if key=='HOST':
        try:
            ipaddress.ip_address(value)
        except ValueError:
            if value!='localhost':
                raise ValueError('监听地址填写 IP 地址或 localhost。') from None
    if key in ('PUBLIC_BASE_URL','CLAW_API_BASE_URL') and value:
        try:
            url=urlsplit(value)
            port=url.port
            valid=(url.scheme in ('http','https') and url.hostname and not url.username
                and not url.password and not url.query and not url.fragment and url.path in ('','/')
                and not any(c.isspace() for c in url.netloc) and port!=0)
            if key=='CLAW_API_BASE_URL':
                if url.hostname.lower()=='localhost':
                    valid=False
                try:
                    address=ipaddress.ip_address(url.hostname)
                    if address.is_loopback or address.is_unspecified or address.is_link_local:
                        valid=False
                except ValueError:
                    pass
        except (ValueError,AttributeError):
            valid=False
        if not valid:
            raise ValueError('请填写 http(s)://主机[:端口]，不带路径、凭证或查询参数；云端 Base URL 不能用本机回环地址。')
        value=value.rstrip('/')
    if key in SECRET_KEYS and (not value or value.startswith('replace-')):
        raise ValueError('此项必填，不能使用模板占位值。')
    if key in ('DEFAULT_AGENT_ID','DEFAULT_ENVIRONMENT_ID','SETUP_SKILL_ID','SETUP_SKILL_FILE_ID') and value:
        if not re.fullmatch(r'[A-Za-z0-9_-]+',value):
            raise ValueError('资源 ID 格式不正确。')
    if key in ('DATABASE_PATH','MA_MODEL') and not value:
        raise ValueError('此项不能留空。')
    return value


def ask(key, label, old='', optional=False):
    while True:
        hint=(' [已保存；回车保留]' if old else '') if key in SECRET_KEYS else (f' [{old}]' if old else '')
        reader=getpass.getpass if key in SECRET_KEYS else input
        value=reader(label+hint+('（输入 - 清空）' if optional else '')+': ').strip()
        value='' if optional and value=='-' else value or old
        try:
            return validate(key,value)
        except ValueError as exc:
            print(exc)


@contextmanager
def editing(home):
    with exclusive(home/'control.lock'):
        with exclusive(home/'run.lock'):
            yield


def configure(home):
    if not sys.stdin.isatty():
        raise ValueError('首次配置需要交互终端。请运行 clawmio init；自动化可用 config set 或预先准备 .env。')
    with editing(home):
        config=read_config(home)
        print('ClawMio 配置向导；API Key 隐藏输入，仅保存在本机配置目录。')
        config['DASHSCOPE_API_KEY']=ask('DASHSCOPE_API_KEY','百炼 API Key',config['DASHSCOPE_API_KEY'])
        print('控制台：https://agent.console.aliyun.com/managed-agent；Key 不能自动推导工作空间地址。')
        config['BAILIAN_WORKSPACE_ID']=ask('BAILIAN_WORKSPACE_ID','工作空间 ID',config['BAILIAN_WORKSPACE_ID'])
        config['BAILIAN_REGION']=ask('BAILIAN_REGION','区域',config['BAILIAN_REGION'])
        print('可选：云端可访问的后台 Base URL，用于自然语言查额度、管理定时任务。')
        print('留空会跳过 Skill；基础聊天、/usage、/clear 仍可用。CLI 不会自动配置域名或公网隧道。')
        config['CLAW_API_BASE_URL']=ask('CLAW_API_BASE_URL','Base URL（首次可直接回车跳过）',config['CLAW_API_BASE_URL'],True)
        config['HOST']=ask('HOST','监听地址（本机用 127.0.0.1，局域网/反向代理可用 0.0.0.0）',config['HOST'])
        config['PORT']=ask('PORT','监听端口',config['PORT'])
        config['PUBLIC_BASE_URL']=ask('PUBLIC_BASE_URL','分享绑定页面的地址（可留空使用本机地址）',config['PUBLIC_BASE_URL'],True)
        if not config['ADMIN_PASSWORD'] or config['ADMIN_PASSWORD'].startswith('replace-'):
            config['ADMIN_PASSWORD']=secrets.token_urlsafe(24)
        if not config['COOKIE_SECRET'] or config['COOKIE_SECRET'].startswith('replace-'):
            config['COOKIE_SECRET']=secrets.token_hex(32)
        state=home/'data/bootstrap-ma.json'
        if state.exists():
            previous=json.loads(state.read_text(encoding='utf-8'))
            if previous['workspace']!=config['BAILIAN_WORKSPACE_ID'] or previous.get('region','cn-beijing')!=config['BAILIAN_REGION']:
                raise ValueError('此目录已有云资源状态；切换工作空间或区域请使用另一个 --home 目录。原配置未更改。')
        write_config(home,config)
    print(f'配置已保存：{home / ".env"}')
    print('管理员密码已生成/保留；运行 clawmio password 查看（不要分享）。')


def check_config(home, resources=True):
    if not (home/'.env').exists():
        raise ValueError('尚未配置，请运行 clawmio init。')
    config=read_config(home)
    for key in DEFAULTS:
        validate(key,config[key])
    if resources and (not config['DEFAULT_AGENT_ID'] or not config['DEFAULT_ENVIRONMENT_ID']):
        raise ValueError('缺少云资源 ID，请运行 clawmio provision --apply。')
    return config


def provision_resources(home, apply=False, yes=False, wait=300):
    with editing(home):
        config=check_config(home,False)
        argv=['--home',str(home),'--model',config['MA_MODEL'],'--auto-skill','--skill-wait',str(wait)]
        provision.main(argv)
        if not apply:
            return False
        if not yes:
            if not sys.stdin.isatty():
                raise ValueError('非交互执行请显式使用 --yes 接受上述资源计划。')
            print('将创建/复用云端资源；启用自动工具和出站网络，后续使用可能计费。')
            if input('按上述计划配置？[Y/n]: ').strip().lower() not in ('','y','yes'):
                print('已保存本地配置，未执行云端操作。')
                return False
        provision.main([*argv,'--apply'])
        return True


def runtime_state(home):
    try:
        return json.loads((home/'run.json').read_text(encoding='utf-8'))
    except (FileNotFoundError,ValueError):
        return {}


def access_url(host,port):
    host='127.0.0.1' if host in ('0.0.0.0','localhost') else '::1' if host=='::' else host
    return f'http://[{host}]:{port}' if ':' in host else f'http://{host}:{port}'


def show_addresses(home,state):
    print(f'监听：{state["host"]}:{state["port"]}')
    print(f'管理后台：{access_url(state["host"],state["port"])}')
    if state['host'] in ('0.0.0.0','::'):
        try:
            addresses=sorted({x[4][0] for x in socket.getaddrinfo(socket.gethostname(),None)})
            for address in addresses:
                ip=ipaddress.ip_address(address)
                if not ip.is_loopback and not ip.is_link_local:
                    print('网络候选地址：'+access_url(address,state['port']))
        except OSError:
            pass
        print('跨设备访问需网络路由和防火墙放行；0.0.0.0 / :: 是监听地址，不是浏览器地址。')
    config=read_config(home)
    if config['PUBLIC_BASE_URL']:
        print('绑定分享地址：'+config['PUBLIC_BASE_URL'])
    if not config['CLAW_API_BASE_URL']:
        print('自助 Skill 未启用（未填 Base URL）；基础聊天、/usage、/clear 可用。')
    else:
        print('云端回调地址：'+config['CLAW_API_BASE_URL']+'（需自行确保 MA 可达）')
    print(f'配置目录：{home}\n日志：{home / "logs/server.log"}')
    print('登录密码：运行 clawmio password 查看。')


def serve(home,host,port,open_browser=False,launch_token=None):
    import uvicorn
    with exclusive(home/'run.lock'):
        config=check_config(home)
        os.environ.update({k:config[k] for k in DEFAULTS})
        os.environ['CLAWMIO_HOME']=str(home)
        database=Path(config['DATABASE_PATH']).expanduser()
        os.environ['DATABASE_PATH']=str((home/database).resolve())
        os.environ['PUBLIC_BASE_URL']=config['PUBLIC_BASE_URL'] or access_url(host,port)
        token=launch_token or secrets.token_hex(16)
        state={'pid':os.getpid(),'token':token,'host':host,'port':port,'ready':False}
        provision.save_state(home/'run.json',state)
        server=uvicorn.Server(uvicorn.Config('app.main:app',host=host,port=port,workers=1,log_level='info'))
        finished=threading.Event()

        def monitor():
            announced=False
            while not finished.wait(0.2):
                if server.started and not announced:
                    state['ready']=True
                    provision.save_state(home/'run.json',state)
                    show_addresses(home,state)
                    sys.stdout.flush()
                    if open_browser:
                        webbrowser.open(access_url(host,port))
                    announced=True
                try:
                    stop=(home/'stop.request').read_text(encoding='utf-8')
                except FileNotFoundError:
                    stop=''
                if stop==token:
                    server.should_exit=True

        watcher=threading.Thread(target=monitor,daemon=True)
        watcher.start()
        try:
            server.run()
            if not server.started:
                raise ValueError('服务启动失败，请查看日志。')
        finally:
            finished.set()
            watcher.join(timeout=2)
            (home/'run.json').unlink(missing_ok=True)
            (home/'stop.request').unlink(missing_ok=True)


def start(home,host=None,port=None,foreground=False,open_browser=False):
    if not (home/'.env').exists():
        configure(home)
        if not provision_resources(home,True):
            return
    config=check_config(home,False)
    state_path=home/'data/bootstrap-ma.json'
    setup_state=json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    desired={key:config[key] for key in ('CLAW_API_BASE_URL','DEFAULT_AGENT_ID','DEFAULT_ENVIRONMENT_ID')}
    needs_sync=(config['CLAW_API_BASE_URL'] or 'cli_config' in setup_state) and setup_state.get('cli_config')!=desired
    if not config['DEFAULT_AGENT_ID'] or not config['DEFAULT_ENVIRONMENT_ID'] or needs_sync or setup_state.get('skill_mount'):
        if not provision_resources(home,True):
            return
    host=validate('HOST',host or config['HOST'])
    port=int(validate('PORT',str(port or config['PORT'])))
    if foreground:
        serve(home,host,port,open_browser)
        return
    with exclusive(home/'control.lock'):
        if is_locked(home/'run.lock'):
            raise Locked('服务已经运行或正在启动；使用 status 查看，使用 restart 重启。')
        (home/'logs').mkdir(parents=True,exist_ok=True)
        launch_token=secrets.token_hex(16)
        command=[sys.executable,'-m','clawmio','--home',str(home),'_serve','--host',host,'--port',str(port),'--launch-token',launch_token]
        options={'cwd':str(home), 'stdin':subprocess.DEVNULL}
        if os.name=='nt':
            options['creationflags']=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options['start_new_session']=True
        # Also support a source checkout without relying on the child's cwd.
        env=os.environ.copy()
        env['PYTHONPATH']=str(Path(__file__).resolve().parent.parent)+os.pathsep+env.get('PYTHONPATH','')
        env['PYTHONUNBUFFERED']='1'
        env['PYTHONUTF8']='1'
        with open(home/'logs/server.log','ab') as log:
            child=subprocess.Popen(command,stdout=log,stderr=log,env=env,**options)
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            state=runtime_state(home)
            if child.poll() is not None:
                raise ValueError('服务启动失败，请运行 clawmio logs 查看原因。')
            if state.get('token')==launch_token and state.get('ready') and is_locked(home/'run.lock'):
                show_addresses(home,state)
                if open_browser:
                    webbrowser.open(access_url(host,port))
                return
            time.sleep(0.2)
        raise ValueError('服务仍在启动；运行 clawmio status / logs 核对，不要重复启动。')


def stop(home):
    with exclusive(home/'control.lock'):
        if not is_locked(home/'run.lock'):
            print('服务未运行。')
            return
        state=runtime_state(home)
        if not state.get('token'):
            raise ValueError('服务状态尚未就绪，请稍后重试 stop。')
        (home/'stop.request').write_text(state['token'],encoding='utf-8')
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            if not is_locked(home/'run.lock'):
                print('服务已停止。')
                return
            time.sleep(0.2)
        raise ValueError('服务正在等待任务退出；请稍后查看 status / logs。本命令不会强杀或误杀其他进程。')


def logs(home,lines,follow):
    path=home/'logs/server.log'
    if not path.exists():
        print('暂无后台日志；run 前台模式直接向终端输出。')
        return
    with path.open(encoding='utf-8',errors='replace') as file:
        print(''.join(deque(file,maxlen=lines)),end='')
        while follow:
            line=file.readline()
            if line:
                print(line,end='',flush=True)
            else:
                time.sleep(0.3)


def parser():
    result=argparse.ArgumentParser(prog='clawmio',description='ClawMio：交互配置并启动微信 × 百炼管理后台。无子命令时前台启动。')
    result.add_argument('--version',action='version',version='ClawMio '+__version__)
    result.add_argument('--home',help='配置/数据目录（默认 CLAWMIO_HOME 或 ~/.clawmio）')
    commands=result.add_subparsers(dest='command')
    def command(name,help):
        sub=commands.add_parser(name,help=help,description=help)
        sub.add_argument('--home',default=argparse.SUPPRESS,help='配置/数据目录')
        return sub
    init=command('init','首次配置向导，默认自动准备云端资源')
    init.add_argument('--no-provision',action='store_true',help='只保存本地配置，不访问云端')
    init.add_argument('--yes',action='store_true',help='接受向导后的云端资源计划')
    command('configure','重新交互配置；云端变更另运行 provision --apply')
    setup=command('provision','预览/创建资源，上传并同步可选 Skill；可安全续跑')
    setup.add_argument('--apply',action='store_true',help='执行计划，默认仅离线预览')
    setup.add_argument('--yes',action='store_true',help='接受资源计划，适合非交互执行')
    setup.add_argument('--skill-wait',type=int,default=300,help='审核等待秒数，默认 300；超时后可重跑')
    for name,help in [('start','后台启动；首次运行进入向导'),('run','前台启动；Ctrl+C 停止'),('restart','停止后后台启动'),('_serve','内部服务入口，请使用 start / run')]:
        sub=command(name,help)
        sub.add_argument('--host',help='本次监听 IP（不改持久配置）')
        sub.add_argument('--port',type=int,help='本次监听端口（不改持久配置）')
        sub.add_argument('--open',action='store_true',help='就绪后打开浏览器')
        if name=='_serve':
            sub.add_argument('--launch-token',help=argparse.SUPPRESS)
    command('stop','优雅停止当前配置目录中的 CLI 服务')
    command('status','显示 CLI 服务状态和访问地址')
    command('open','在浏览器打开正在运行的管理后台')
    tail=command('logs','查看后台日志')
    tail.add_argument('-n','--lines',type=int,default=80,help='最后 N 行，默认 80')
    tail.add_argument('-f','--follow',action='store_true',help='持续跟踪，Ctrl+C 退出')
    command('doctor','离线检查配置、资源 ID、静态文件和数据库进程锁')
    password=command('password','在交互终端查看管理员密码或重置密码')
    password.add_argument('--reset',action='store_true',help='生成新密码并使旧 Cookie 失效，需先 stop')
    config=command('config','查看脱敏配置，或逐项设置；不自动修改云端资源')
    config_sub=config.add_subparsers(dest='action',required=True)
    config_sub.add_parser('show',help='查看配置（密钥隐藏）')
    config_sub.add_parser('path',help='显示 .env 路径')
    set_parser=config_sub.add_parser('set',help='设置一项；敏感值省略 VALUE，通过隐藏输入填写')
    set_parser.add_argument('key',choices=sorted(DEFAULTS))
    set_parser.add_argument('value',nargs='?')
    skill=command('skill-package','导出自助 Skill ZIP（不上传）')
    skill.add_argument('--output',type=Path,help='输出路径，默认配置目录下 artifacts/')
    return result


def main(argv=None):
    args=parser().parse_args(argv)
    home=home_path(args.home)
    cmd=args.command or 'run'
    try:
        if cmd in ('init','configure'):
            configure(home)
            if cmd=='init' and not args.no_provision:
                provision_resources(home,True,args.yes)
        elif cmd=='provision':
            provision_resources(home,args.apply,args.yes,args.skill_wait)
        elif cmd in ('start','run','restart','_serve'):
            if cmd=='restart':
                stop(home)
            if cmd=='_serve':
                serve(home,validate('HOST',args.host),int(validate('PORT',str(args.port))),launch_token=args.launch_token)
            else:
                start(home,getattr(args,'host',None),getattr(args,'port',None),cmd=='run',getattr(args,'open',False))
        elif cmd=='stop':
            stop(home)
        elif cmd in ('status','open'):
            if not is_locked(home/'run.lock'):
                print('服务未运行（只检查此配置目录中由 CLI 启动的服务）。')
                return 1
            state=runtime_state(home)
            if not state.get('ready'):
                print('服务正在启动，请查看 logs。')
                return 1
            if cmd=='open':
                webbrowser.open(access_url(state['host'],state['port']))
            else:
                print(f'服务运行中，PID {state["pid"]}')
                show_addresses(home,state)
        elif cmd=='logs':
            if args.lines<1:
                raise ValueError('--lines 必须大于 0。')
            logs(home,args.lines,args.follow)
        elif cmd=='doctor':
            config=check_config(home)
            for name in ('static/index.html','static/app.js','static/style.css','docs/agent-system-prompt.txt'):
                if not (assets()/name).is_file():
                    raise ValueError('软件包缺少资源文件，请重新安装。')
            database=(home/Path(config['DATABASE_PATH']).expanduser()).resolve()
            print(f'本地配置和软件包检查通过。数据库已被进程使用：{is_locked(str(database)+".worker.lock")}')
            print('本检查不访问云端，不验证 API Key 权限、公网地址或微信端到端链路。')
        elif cmd=='password':
            if not sys.stdout.isatty():
                raise ValueError('密码仅允许在交互终端显示，避免写入日志或管道。')
            if args.reset:
                with editing(home):
                    if not (home/'.env').exists():
                        raise ValueError('请先运行 init。')
                    write_config(home,{'ADMIN_PASSWORD':secrets.token_urlsafe(24),'COOKIE_SECRET':secrets.token_hex(32)})
            password=read_config(home)['ADMIN_PASSWORD']
            if not password:
                raise ValueError('尚未配置管理员密码，请运行 init。')
            print('管理员密码：'+password)
        elif cmd=='config':
            if args.action=='path':
                print(home/'.env')
            elif args.action=='show':
                print(f'配置文件：{home / ".env"}')
                for key,value in read_config(home).items():
                    print(f'{key}={"<已设置>" if value else "<未设置>"}' if key in SECRET_KEYS or key not in DEFAULTS else f'{key}={value}')
            else:
                value=args.value
                if args.key in SECRET_KEYS and value is not None:
                    raise ValueError('敏感配置请省略 VALUE，使用隐藏输入，避免进入命令历史。')
                if value is None:
                    if not sys.stdin.isatty():
                        raise ValueError('省略 VALUE 时需要交互终端。')
                    value=getpass.getpass('新值: ') if args.key in SECRET_KEYS else input('新值: ')
                value=validate(args.key,value)
                with editing(home):
                    config=read_config(home)
                    if args.key in ('BAILIAN_WORKSPACE_ID','BAILIAN_REGION') and (home/'data/bootstrap-ma.json').exists() and value!=config[args.key]:
                        raise ValueError('切换工作空间/区域请使用新的 --home 目录。')
                    write_config(home,{args.key:value})
                print('已保存；启动时生效。Base URL 变更后运行 provision --apply 同步 Skill，并为已有 Bot 新建 Session。')
        elif cmd=='skill-package':
            output=(args.output or home/'artifacts/claw-bot-self-service.zip').resolve()
            output.parent.mkdir(parents=True,exist_ok=True)
            with output.open('xb') as file:
                file.write(provision.skill_zip())
            print(f'已生成：{output}')
        return 0
    except (ValueError,OSError,Locked,provision.SetupError) as exc:
        # Avoid raw HTTP bodies, API keys or request headers in error output.
        print(f'错误：{exc}',file=sys.stderr)
        return 1
    except (KeyboardInterrupt,EOFError):
        print('\n已取消。',file=sys.stderr)
        return 130
