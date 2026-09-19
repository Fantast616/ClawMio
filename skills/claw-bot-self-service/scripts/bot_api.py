"""Bot-scoped client; no credentials in arguments or printed output."""
import argparse
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def invoke(path, payload=None, request_id=None, method=None):
    base=os.environ.get('CLAW_API_BASE_URL','').rstrip('/')
    token=os.environ.get('CLAW_BOT_TOKEN','')
    parsed=urlsplit(base)
    if not token or parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('会话缺少有效的 CLAW_API_BASE_URL / CLAW_BOT_TOKEN，请联系管理员新建会话')
    headers={'Authorization':'Bearer '+token,'Accept':'application/json'}
    body=None
    if payload is not None:
        body=json.dumps(payload).encode()
        headers['Content-Type']='application/json'
    if request_id:
        headers['Idempotency-Key']=request_id
    req=Request(base+path,data=body,headers=headers,method=method or ('POST' if body is not None else 'GET'))
    # Never forward the bearer credential to a redirected destination.
    with build_opener(NoRedirect()).open(req,timeout=60) as response:
        return json.load(response)


def identifier(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',value):
        raise argparse.ArgumentTypeError('ID 只能包含字母、数字、下划线和连字符')
    return value


def main(argv=None):
    parser=argparse.ArgumentParser(description='当前 Bot 的额度查询和会话管理')
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('usage')
    reset=commands.add_parser('reset')
    reset.add_argument('--session-id',required=True,type=identifier)
    reset.add_argument('--request-id',required=True,type=identifier)
    status=commands.add_parser('status')
    status.add_argument('--operation-id',required=True,type=identifier)
    commands.add_parser('schedule-list')
    add=commands.add_parser('schedule-create')
    add.add_argument('--file',required=True,help='定时任务 JSON 文件路径')
    add.add_argument('--request-id',required=True,type=identifier)
    for command in ('schedule-get','schedule-delete','schedule-runs'):
        p=commands.add_parser(command)
        p.add_argument('--schedule-id',required=True,type=identifier)
        if command=='schedule-runs':p.add_argument('--offset',type=int,default=0)
    args=parser.parse_args(argv)
    try:
        if args.command=='usage':
            result=invoke('/api/bot/usage')
        elif args.command=='reset':
            if len(args.request_id)<8:
                raise ValueError('request-id 至少 8 位，请使用 UUID')
            result=invoke('/api/bot/sessions/reset',{'expected_session_id':args.session_id},args.request_id)
        elif args.command=='status':
            result=invoke('/api/bot/session-requests/'+args.operation_id)
        elif args.command=='schedule-list':
            result=invoke('/api/bot/schedules')
        elif args.command=='schedule-create':
            with open(args.file,encoding='utf-8') as file:payload=json.load(file)
            if len(args.request_id)<8:raise ValueError('请求键至少 8 位')
            result=invoke('/api/bot/schedules',payload,args.request_id)
        elif args.command=='schedule-delete':
            result=invoke('/api/bot/schedules/'+args.schedule_id,method='DELETE')
        elif args.command=='schedule-runs':
            if args.offset<0:raise ValueError('offset 不能为负数')
            result=invoke('/api/bot/schedules/'+args.schedule_id+'/runs?offset='+str(args.offset))
        else:
            result=invoke('/api/bot/schedules/'+args.schedule_id)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    except HTTPError as exc:
        messages={401:'Bot 凭证无效',402:'您已欠费',409:'会话已变化或已有请求，请查询状态',422:'请求参数无效'}
        print(json.dumps({'error':messages.get(exc.code,'接口请求失败，请联系管理员'),'http_status':exc.code},ensure_ascii=False))
    except (URLError,TimeoutError,OSError):
        print(json.dumps({'error':'网络请求失败；新会话请求可能已受理，只能复用原请求键重试'},ensure_ascii=False))
    except (ValueError,TypeError):
        print(json.dumps({'error':'环境变量、请求参数或响应格式无效，请联系管理员'},ensure_ascii=False))
    return 1


if __name__=='__main__':
    sys.exit(main())
