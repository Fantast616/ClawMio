import os
import secrets
import mimetypes
import json
import re
import httpx
from datetime import datetime, timezone


def normalize_event(event):
    event = dict(event)
    event['id'] = event.get('id') or event.get('eventId')
    event['thread_id'] = event.get('thread_id') or event.get('threadId') or (event.get('metadata') or {}).get('thread_id','')
    timestamp = event.get('created_at') or event.get('createdAt')
    if isinstance(timestamp,(int,float)):
        timestamp = datetime.fromtimestamp(timestamp/1000,timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
    event['created_at'] = timestamp or ''
    return event


class MAError(RuntimeError):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class ManagedAgent:
    supports_sse = True
    def __init__(self, client=None):
        self.base = f"https://{os.environ['BAILIAN_WORKSPACE_ID']}.{os.getenv('BAILIAN_REGION', 'cn-beijing')}.maas.aliyuncs.com/api/v1/agentstudio"
        self.headers = {
            'Authorization': 'Bearer ' + os.getenv('DASHSCOPE_API_KEY', ''),
            'User-Agent': f'AlibabaCloud-Agent-Skills/alibabacloud-bailian-managed-agent/{secrets.token_hex(16)} skill-version/0.0.1-beta.1',
        }
        self.client = client or httpx.AsyncClient(timeout=45)
        self.stream_client = httpx.AsyncClient(timeout=httpx.Timeout(90,connect=15),
            limits=httpx.Limits(max_connections=None,max_keepalive_connections=20))

    async def stream_events(self, session_id, connected):
        async with self.stream_client.stream('GET',self.base+f'/sessions/{session_id}/events/stream',
                headers={**self.headers,'Accept':'text/event-stream'}) as response:
            if response.status_code!=200 or 'text/event-stream' not in response.headers.get('content-type',''):
                raise MAError(f'MA SSE HTTP {response.status_code}',response.status_code)
            connected.set()
            data = []
            async for line in response.aiter_lines():
                if line.startswith('data:'):
                    data.append(line[5:].lstrip(' '))
                elif not line and data:
                    raw='\n'.join(data);data=[]
                    if raw=='[DONE]':
                        return
                    event=json.loads(raw)
                    if isinstance(event,dict):
                        event=normalize_event(event)
                    if isinstance(event,dict) and event.get('id') and event.get('type') not in ('event_start','event_delta') and event.get('status','completed')=='completed':
                        yield event

    async def request(self, method, path, **kwargs):
        try:
            response = await self.client.request(method, self.base + path, headers=self.headers, **kwargs)
        except httpx.HTTPError as exc:
            raise MAError(f'MA 网络请求失败：{type(exc).__name__}') from exc
        if response.is_error:
            # Do not include response bodies or credentials in client-facing errors.
            payload = response.json() if 'json' in response.headers.get('content-type','') else {}
            request_id = response.headers.get('x-request-id') or payload.get('request_id','unknown')
            raise MAError(f'MA HTTP {response.status_code}; request-id={request_id}',response.status_code)
        return response.json()

    async def list_all(self, path, **params):
        result = []
        while True:
            payload = await self.request('GET', path, params={'limit': 100, **params})
            result.extend(payload.get('data', []))
            if not payload.get('next_page'):
                return result
            params['page'] = payload['next_page']

    async def create_session(self, bot):
        if not bot.get('memory_store_id'):
            raise MAError('Bot 专属记忆库尚未准备好')
        if not bot.get('api_token'):
            raise MAError('Bot API 凭证尚未准备好')
        return await self.request('POST', '/sessions', json={
            'agent': bot['agent_id'], 'environment_id': bot['environment_id'],
            'title': f"WeChat / {bot['name']}",
            'environment_variables': {
                'CLAW_BOT_TOKEN':bot.get('api_token',''),
                'CLAW_API_BASE_URL':os.getenv('CLAW_API_BASE_URL','').rstrip('/'),
            },
            'resources':[{'type':'memory_store','memory_store_id':bot['memory_store_id'],
                'access':'read_write','instructions': MEMORY_INSTRUCTIONS}],
            'metadata': {'claw_bot_id': bot['id'], 'memory_store_id':bot['memory_store_id'],
                'claw_scheduled_job':bot.get('scheduled_job_id',''),
                'claw_reset_request':bot.get('reset_request_id',''),
                'claw_memory_upgrade':bot.get('memory_upgrade_state','')}})

    async def upload_file(self, filename, content):
        mime = {'jpg':'image/jpeg','jpeg':'image/jpeg','png':'image/png','webp':'image/webp','gif':'image/gif'}.get(filename.rsplit('.',1)[-1].lower()) or mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        return await self.request('POST', '/files', files={'file':(filename,content,mime)})

    async def interrupt(self, session_id):
        return await self.request('POST', f'/sessions/{session_id}/events',
                                  json={'input':[{'role':'user','type':'interrupt'}]})

    async def mount_file(self, session_id, file_id, mount_path):
        # A lost POST response must not create another copy on restart.
        existing = await self.list_all(f'/sessions/{session_id}/resources')
        for resource in existing:
            if resource.get('mount_path') in (mount_path, '/mnt/session'+mount_path):
                return resource
        return await self.request('POST',f'/sessions/{session_id}/resources',json={
            'type':'file','file_id':file_id,'mount_path':mount_path})

    async def download_artifact(self, session_id, file_id, limit=10*1024*1024):
        files = await self.list_all('/files',scope_id=session_id,scope_type='session')
        meta = next((f for f in files if f.get('id')==file_id),None)
        if not meta or meta.get('downloadable') is not True:
            raise ValueError('该产物不属于当前会话或暂不可下载')
        if (meta.get('size_bytes') or 0)>limit:
            raise ValueError('产物超过本地微信回传限制 10 MB')
        async with self.client.stream('GET',self.base+f'/files/{file_id}/content',headers=self.headers,follow_redirects=False) as response:
            if response.status_code!=200:
                raise MAError(f'产物下载 HTTP {response.status_code}',response.status_code)
            chunks, size = [],0
            async for chunk in response.aiter_bytes():
                size+=len(chunk)
                if size>limit:
                    raise ValueError('产物超过本地微信回传限制 10 MB')
                chunks.append(chunk)
        return b''.join(chunks)

    async def send(self, session_id, text, attachments=None):
        text += '\n\n[微信交互约定] '+ WECHAT_INSTRUCTIONS
        content = [{'type':'text','text':text}]
        for attachment in attachments or []:
            if attachment.get('status') != 'available' or not attachment.get('file_id'):
                raise ValueError('附件尚未通过 MA 审核')
            block = {'type':attachment['type'], 'file_id':attachment['file_id']}
            if attachment['type'] == 'file':
                block['filename'] = attachment['filename']
            content.append(block)
        return await self.request('POST', f'/sessions/{session_id}/events', json={'input': [
            {'role': 'user', 'type': 'message', 'content': content}]})

    async def events(self, session_id, since):
        return [normalize_event(e) for e in await self.list_all(f'/sessions/{session_id}/events', order='asc', **{'created_at[gte]': since})]


def result_from_events(events, event_id):
    # Anchor to the server-issued user event. Never mix an earlier turn into this reply.
    started = False
    texts, seen = [], set()
    state, reason, approval, error = None, {}, {}, ''
    for ev in events:
        if ev.get('id') == event_id:
            started = True
            continue
        if not started or ev.get('id') in seen:
            continue
        kind = ev.get('type')
        if ev.get('status','completed') != 'completed':
            continue
        seen.add(ev.get('id'))
        if started and kind == 'message' and ev.get('role') == 'user':
            break
        thread = ev.get('thread_id') or (ev.get('metadata') or {}).get('thread_id', '')
        if thread.startswith('sthr_'):
            continue
        message_parts = []
        if kind == 'error':
            error = ev.get('error', {}).get('code', 'MA execution error')
        for block in ev.get('content') or []:
            data = block.get('data') or {}
            if kind == 'session_status':
                state = data.get('status') or data.get('session_status')
                reason = data.get('stop_reason') or {}
            if kind == 'tool_approval_request':
                approval = data
            thread = ev.get('thread_id') or (ev.get('metadata') or {}).get('thread_id', '')
            if kind == 'message' and ev.get('role') == 'assistant' and not thread.startswith('sthr_'):
                if block.get('type') == 'text' and block.get('text'):
                    message_parts.append(block['text'])
        if message_parts:
            texts.append('\n'.join(message_parts))
    return {'text': '\n\n'.join(texts), 'messages': texts, 'state': state, 'reason': reason, 'approval': approval, 'error': error,
            'artifacts': marked_artifacts(events,event_id)}


def marked_artifacts(events, event_id):
    started, calls, found = False,set(),{}
    for event in events:
        if event.get('id')==event_id:
            started=True
            continue
        if not started:
            continue
        if event.get('type')=='message' and event.get('role')=='user':
            break
        for block in event.get('content') or []:
            data=block.get('data') or {}
            if event.get('type')=='tool_call' and data.get('name')=='mark_artifacts':
                calls.add(data.get('call_id'))
            if event.get('type')!='tool_call_output' or event.get('is_error') or not data.get('call_id') or data['call_id'] not in calls:
                continue
            output=data.get('output')
            if isinstance(output,str):
                try: output=json.loads(output)
                except (ValueError,TypeError): continue
            if not isinstance(output,dict):
                continue
            for item in output.get('marked') or []:
                fid=item.get('file_id','')
                if re.fullmatch(r'file_[A-Za-z0-9_-]+',fid):
                    filename=str(item.get('path','')).replace('\\','/').split('/')[-1] or fid
                    filename=re.sub(r'[\x00-\x1f<>:"|?*]','_',filename)[:180]
                    found.setdefault(fid,{'file_id':fid,'filename':filename,'status':'pending','attempts':0})
    return list(found.values())


MEMORY_INSTRUCTIONS = ('这是当前微信用户的专属长期记忆，其他用户不可访问。回答涉及用户偏好、约定或历史事项时，先用 Glob/Read 检索相关笔记；'
    '将用户明确告知且值得跨会话保留的偏好、持续项目和约定，用 Write/Edit 更新到简洁分类笔记中。'
    '用户更正时更新旧事实；不要把推测、一次性请求、完整对话或凭证写入记忆。不要通过 shell 访问记忆路径。'
    '新会话仍沿用这些记忆，但不要假称记得没有保存的内容。')
COMMAND_INSTRUCTIONS = (
    '【用户自助服务】\n'
    '用户单独发送 /usage 时，桥接后端直接返回该 Bot 的总额度、已用金额和剩余额度；单独发送 /clear 时，后端新开聊天 Session，保留专属长期记忆。字面指令无需你重复执行。\n'
    '用户自然语言询问“还剩多少钱”“用了多少 Token”“搜索用了几次”时，使用 claw-bot-self-service Skill 的 usage 脚本查询，直接回答实际结果，不要只让用户改发 /usage。'
    '用户明确说“清空上下文”“重新开始一个会话”时，按 Skill 查询当前 Session 并提交 reset。若返回 queued，只说明已申请、本轮结束后切换，然后结束本轮；不要循环等待，也不要声称已经完成。清空上下文不删除长期记忆。\n'
    '用户要求定时执行、查看或取消定时任务时，按同一 Skill 调用 schedule-create/list/get/runs/delete。创建前明确执行内容、频率、具体时间；默认北京时间，只有“早上”等模糊时间时先问几点。取消前核对任务 ID，目标不明确时询问。'
    '接口成功后再确认创建或删除；执行记录 queued 不等于已经执行。定时任务使用独立 Session，完成后由系统回传微信，不替换当前聊天。\n'
    '身份与地址从环境变量读取，不展示 Token、不写入长期记忆、不请求用户发送凭证，不访问其他 Bot 数据。Skill 或环境变量缺失时说明需要管理员配置；额度查询和新会话可介绍 /usage、/clear 作为替代。'
    '任何余额、用量、任务状态均以接口结果为准，不猜测。余额是已结算金额，运行中费用稍后扣除；欠费后端拒绝新执行，/usage 仍可查询。\n'
)
WECHAT_INSTRUCTIONS = (COMMAND_INSTRUCTIONS +
    '【微信对话与交付】\n'
    '自然、简洁地承接上下文，直接回答当前意图，不反复自我介绍。优先给用户有用的结果，确需等待时简短告知进度；不要逐步播报工具调用、重复计划和总结。'
    '每条 assistant message 都会作为一条微信消息，必要时可以分多条，避免重复或把一句话拆碎。工具内部事件不需要复述。'
    '使用纯文本、短段落或简短列表，不使用 Markdown 表格、图片语法或本地下载链接；需要注明新闻或检索来源时可提供真实网页 URL。\n'
    '只收到图片或文件时结合前文判断需求；意图不清楚时简短询问，不强制分析。读取用户附件后再据实作答。'
    '交付文件写入 /mnt/session/outputs/，调用 mark_artifacts 注册。注册成功后简短说明产物，系统负责微信回传；不重复贴文件链接、内部路径，也不把注册成功说成微信已送达。'
    '工具或接口失败时说明已知结果与未完成部分，不伪造成功。涉及跨会话偏好或约定时使用专属记忆库；不把凭证、推测或一次性请求存为长期事实。'
)
