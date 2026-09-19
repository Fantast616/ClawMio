import asyncio
import json
import time
import secrets
import os
from urllib.parse import urlparse

from wechatbot.protocol import ILinkApi, DEFAULT_BASE_URL
from wechatbot.errors import ApiError
from .ma import result_from_events, MAError
from .media import download_attachment, upload_wechat_file
from .replies import reply_chunks
from .billing import Billing
from .schedules import Scheduler


def wechat_url(url):
    p = urlparse(url)
    if p.scheme != 'https' or not p.hostname or not (p.hostname == 'weixin.qq.com' or p.hostname.endswith('.weixin.qq.com')) or p.username or p.port not in (None, 443):
        raise ValueError('微信返回了不受信任的服务地址')
    return url.rstrip('/')


class Runtime:
    def __init__(self, db, ma):
        self.db, self.ma = db, ma
        self.wx = ILinkApi(bot_agent='ClawMA/0.1.0')
        self.tasks = {}
        self.locks = {}
        self.wakeups = {}
        self.streams = {}
        self.billing = Billing(db,ma)

    def lock(self, bot_id):
        return self.locks.setdefault(bot_id, asyncio.Lock())

    def start(self, key, coro):
        old = self.tasks.get(key)
        if old and not old.done():
            coro.close()
            return
        self.tasks[key] = asyncio.create_task(coro)

    async def boot(self):
        # POST may have been accepted before a crash. Never blindly resubmit it.
        self.db.execute("UPDATE jobs SET status='uncertain',error='服务在提交时中断，请在 MA 控制台核对，避免重复执行' WHERE status IN ('submitting','approving')")
        self.db.execute("UPDATE jobs SET status='interrupt_uncertain',error='服务在提交中断时重启，请核对 MA 会话' WHERE status='interrupt_sending'")
        self.db.execute("UPDATE jobs SET status='uncertain',error='服务在创建新 Session 时重启，请核对远端会话；原会话关联已保留' WHERE status='session_creating'")
        for bot in self.db.rows('SELECT * FROM bots'):
            self.ensure(bot['id'])
            self.start('memory:' + bot['id'], self.upgrade_memory(bot['id']))
        for row in self.db.rows("SELECT DISTINCT bot_id FROM jobs WHERE schedule_id!='' AND status NOT IN ('done','failed','cancelled','interrupted')"):
            self.start('scheduled:'+row['bot_id'],self.scheduled_work(row['bot_id']))
        self.start('schedule-scan',Scheduler(self).loop())
        for binding in self.db.rows("SELECT * FROM bindings WHERE status IN ('wait','scaned','need_verifycode','scaned_but_redirect')"):
            self.start('bind:' + binding['bot_id'], self.poll_binding(binding['bot_id']))

    def ensure(self, bot_id):
        self.wakeups.setdefault(bot_id, asyncio.Event())
        self.start('recv:' + bot_id, self.receive(bot_id))
        self.start('work:' + bot_id, self.work(bot_id))

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        await self.ma.client.aclose()
        if getattr(self.ma,'supports_sse',False) is True:
            await self.ma.stream_client.aclose()

    async def open_stream(self, bot_id, session_id):
        if getattr(self.ma,'supports_sse',False) is not True:
            return None
        state=self.streams.get(bot_id)
        if state and state['session_id']==session_id:
            return state
        if state:
            state['task'].cancel()
            await asyncio.gather(state['task'],return_exceptions=True)
        state={'session_id':session_id,'ready':asyncio.Event(),'events':{},'sync':True,'last_sync':0,'anchor':''}
        self.streams[bot_id]=state
        self.start('sse:'+bot_id,self.listen_stream(bot_id,state))
        state['task']=self.tasks['sse:'+bot_id]
        try:
            await asyncio.wait_for(state['ready'].wait(),timeout=4)
        except TimeoutError:
            pass  # Bounded fallback; history reconciliation covers the subscription gap.
        return state

    async def listen_stream(self, bot_id, state):
        failures=0
        while True:
            state['ready'].clear()
            state['sync']=True
            try:
                async for event in self.ma.stream_events(state['session_id'],state['ready']):
                    failures=0
                    state['events'][event['id']]=event
                    self.wakeups.setdefault(bot_id,asyncio.Event()).set()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            state['ready'].clear()
            state['sync']=True
            failures+=1
            self.wakeups.setdefault(bot_id,asyncio.Event()).set()
            await asyncio.sleep(min(30,2**min(failures,5)))

    async def job_events(self, job):
        state=await self.open_stream('scheduled:'+str(job['id']) if job.get('schedule_id') else job['bot_id'],job['session_id'])
        if state is None:
            return await self.ma.events(job['session_id'],job['since'])
        # Initial load, reconnect and a 30s safety reconciliation. Connected idle
        # scheduler passes otherwise read only the in-memory SSE buffer.
        if state['sync'] or state['anchor']!=job['event_id'] or time.monotonic()-state['last_sync']>=30:
            if not state['ready'].is_set() and state['anchor']==job['event_id'] and time.monotonic()-state['last_sync']<5:
                return None
            state['sync']=False
            try:
                history=await self.ma.events(job['session_id'],job['since'])
            except Exception:
                state['sync']=True
                raise
            # The historical page is canonical; include later live events received
            # during pagination. Sorting uses server timestamp + stable event ID.
            live=list(state['events'].values())
            state['events']={e['id']:e for e in history}
            for event in live:
                if event.get('created_at','')>=job['since']:
                    state['events'].setdefault(event['id'],event)
            state['anchor']=job['event_id']
            state['last_sync']=time.monotonic()
        events=list(state['events'].values())
        events.sort(key=lambda e:e.get('created_at',''))
        if not any(e['id']==job['event_id'] for e in events):
            state['sync']=True
            return None
        return events

    async def stop_stream(self, bot_id):
        state=self.streams.pop(bot_id,None)
        if state:
            state['task'].cancel()
            await asyncio.gather(state['task'],return_exceptions=True)
        self.tasks.pop('sse:'+bot_id,None)
        if bot_id.startswith('scheduled:'):
            self.wakeups.pop(bot_id,None)

    async def ensure_memory(self, bot):
        """Called under the bot lock; persist identity before any cloud mutation."""
        bot = self.db.ensure_api_token(bot['id'])
        if bot.get('memory_store_id'):
            return bot
        key = bot.get('memory_key') or secrets.token_hex(16)
        self.db.update('bots','id',bot['id'],memory_key=key)
        try:
            stores = await self.ma.list_all('/memory_stores')
            found = next((m for m in stores if (m.get('metadata') or {}).get('claw_memory_key') == key
                          and (m.get('metadata') or {}).get('claw_bot_id') == bot['id']), None)
            if not found:
                if bot.get('memory_state') in ('creating','uncertain'):
                    raise MAError('记忆库创建结果待确认；暂未查到对应资源，请稍后刷新重试')
                self.db.update('bots','id',bot['id'],memory_state='creating')
                found = await self.ma.request('POST','/memory_stores',json={
                    'name':f"wechat-{bot['id']}-{key[:8]}",
                    'description':f"{bot['name']} 的专属微信用户记忆",
                    'metadata':{'claw_bot_id':bot['id'],'claw_memory_key':key}})
            self.db.update('bots','id',bot['id'],memory_store_id=found['id'],memory_state='ready',memory_error='')
        except Exception as exc:
            current = self.db.one('SELECT memory_state FROM bots WHERE id=?',(bot['id'],))
            uncertain = current['memory_state'] in ('creating','uncertain')
            if isinstance(exc, MAError) and exc.status_code and 400<=exc.status_code<500 and exc.status_code!=408:
                uncertain = False
            self.db.update('bots','id',bot['id'],memory_state='uncertain' if uncertain else 'error',memory_error=str(exc) if isinstance(exc,MAError) else '记忆库暂时不可用')
            raise
        return self.db.one('SELECT * FROM bots WHERE id=?',(bot['id'],))

    async def initial_config(self, bot):
        for field, variable, path, label in (
            ('agent_id','DEFAULT_AGENT_ID','/agents','Agent'),
            ('environment_id','DEFAULT_ENVIRONMENT_ID','/environments','运行环境'),
        ):
            value = bot[field] or os.getenv(variable,'')
            if not value:
                items = await self.ma.list_all(path)
                if len(items) != 1:
                    raise MAError(f'请配置 {variable} 或在后台选择{label}后创建会话')
                value = items[0]['id']
            bot[field] = value
        self.db.update('bots','id',bot['id'],agent_id=bot['agent_id'],environment_id=bot['environment_id'])
        return bot

    async def provision_session(self, bot):
        # A persisted attempt marker prevents duplicate creation after a lost response.
        if bot['memory_upgrade_state']:
            sessions = await self.ma.list_all('/sessions')
            created = next((s for s in sessions if (s.get('metadata') or {}).get('claw_memory_upgrade')==bot['memory_upgrade_state']
                and (s.get('metadata') or {}).get('claw_bot_id')==bot['id']),None)
            if not created:
                raise MAError('会话创建结果待确认，稍后重试查询')
        else:
            bot['memory_upgrade_state']=secrets.token_hex(16)
            self.db.update('bots','id',bot['id'],memory_upgrade_state=bot['memory_upgrade_state'])
            try:
                created = await self.ma.create_session(bot)
            except MAError as exc:
                if exc.status_code and 400<=exc.status_code<500 and exc.status_code!=408:
                    self.db.update('bots','id',bot['id'],memory_upgrade_state='')
                raise
        self.db.switch_session(bot,created['id'],bot['agent_id'],bot['environment_id'])
        self.db.update('bots','id',bot['id'],memory_error='')
        self.wakeups.setdefault(bot['id'],asyncio.Event()).set()
        return created

    async def upgrade_memory(self, bot_id):
        # One migration per existing bot. No running turn is moved to another session.
        while True:
            try:
                async with self.lock(bot_id):
                    bot = self.db.one('SELECT * FROM bots WHERE id=?',(bot_id,))
                    if not bot:
                        return
                    bot = await self.ensure_memory(bot)
                    if not bot['session_id']:
                        if bot['token']:
                            bot = await self.initial_config(bot)
                            await self.provision_session(bot)
                        return
                    if bot['session_memory_id']==bot['memory_store_id']:
                        return
                    active = self.db.one("SELECT id FROM jobs WHERE bot_id=? AND schedule_id='' AND status NOT IN ('done','failed','cancelled','interrupted','queued','preparing','checking') LIMIT 1",(bot_id,))
                    if not active:
                        old = await self.ma.request('GET',f"/sessions/{bot['session_id']}")
                        if old.get('status') in ('idle','terminated') and (old.get('stop_reason') or {}).get('type')!='requires_action':
                            await self.provision_session(bot)
                            return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.db.update('bots','id',bot_id,memory_error=str(exc) if isinstance(exc,MAError) else '专属记忆初始化暂时失败，请重试')
                return
            await asyncio.sleep(10)

    async def poll_binding(self, bot_id):
        while True:
            binding = self.db.one('SELECT * FROM bindings WHERE bot_id=?', (bot_id,))
            if not binding or binding['status'] in ('confirmed', 'expired', 'verify_code_blocked', 'error'):
                return
            if time.time() > binding['expires_at']:
                self.db.update('bindings', 'bot_id', bot_id, status='expired')
                return
            try:
                r = await asyncio.wait_for(self.wx.poll_qr_status(binding['poll_base'], binding['qr'], binding['verify_code'] or None), 45)
                status = r['status']
                self.db.update('bindings', 'bot_id', bot_id, status=status)
                if status == 'scaned_but_redirect':
                    self.db.update('bindings', 'bot_id', bot_id, poll_base=wechat_url('https://' + r['redirect_host']))
                elif status == 'need_verifycode':
                    self.db.update('bindings', 'bot_id', bot_id, verify_code='')
                    while True:
                        await asyncio.sleep(2)
                        current = self.db.one('SELECT * FROM bindings WHERE bot_id=?', (bot_id,))
                        if not current or time.time() > current['expires_at'] or current['verify_code']:
                            break
                elif status == 'confirmed':
                    if not all(r.get(k) for k in ('bot_token','ilink_bot_id','ilink_user_id')):
                        raise ValueError('绑定响应缺少凭证')
                    duplicate = self.db.one('SELECT id FROM bots WHERE account_id=? AND id!=?', (r['ilink_bot_id'], bot_id))
                    if duplicate:
                        raise ValueError('该微信 Bot 已被另一个记录绑定')
                    async with self.lock(bot_id):
                        previous = self.db.one('SELECT * FROM bots WHERE id=?', (bot_id,))
                        if previous['user_id'] and previous['user_id'] != r['ilink_user_id']:
                            self.db.update('bots','id',bot_id,session_id='',session_memory_id='',memory_store_id='',
                                memory_key=secrets.token_hex(16),memory_state='',memory_error='',memory_upgrade_state='')
                            self.db.execute("UPDATE jobs SET status='cancelled',error='绑定用户已变更，旧任务已隔离' WHERE bot_id=? AND schedule_id='' AND status NOT IN ('done','failed','cancelled','interrupted')", (bot_id,))
                        self.db.update('bots','id',bot_id,token=r['bot_token'],account_id=r['ilink_bot_id'],user_id=r['ilink_user_id'],
                            base_url=wechat_url(r.get('baseurl') or DEFAULT_BASE_URL),cursor='',context_token='',
                            status='ready' if previous['session_id'] and previous['user_id']==r['ilink_user_id'] else 'bound',error='')
                    self.ensure(bot_id)
                    self.start('memory:' + bot_id, self.upgrade_memory(bot_id))
                    return
                elif status == 'binded_redirect':
                    raise ValueError('微信提示已绑定，请使用已有 Bot 或在微信解除旧绑定后重试')
            except (TimeoutError, OSError):
                pass
            except Exception as e:
                self.db.update('bindings','bot_id',bot_id,status='error')
                self.db.update('bots','id',bot_id,error=str(e) if isinstance(e, ValueError) else f'二维码查询失败：{type(e).__name__}')
                return
            await asyncio.sleep(2)

    def expired(self, bot_id):
        self.db.update('bots','id',bot_id,status='expired',token='',context_token='',cursor='',error='微信登录已过期，请重新扫码')

    async def receive(self, bot_id):
        failures = 0
        while True:
            bot = self.db.one('SELECT * FROM bots WHERE id=?', (bot_id,))
            if not bot:
                return
            if not bot['token'] or not bot['enabled']:
                await asyncio.sleep(2)
                continue
            try:
                r = await self.wx.get_updates(bot['base_url'], bot['token'], bot['cursor'])
                current = self.db.one('SELECT * FROM bots WHERE id=?', (bot_id,))
                if current['token'] == bot['token']:
                    self.db.ingest(current, r)
                    if r.get('msgs'):
                        self.wakeups.setdefault(bot_id,asyncio.Event()).set()
                failures = 0
                self.db.update('bots','id',bot_id,error='')
            except ApiError as e:
                if e.errcode == -14:
                    self.expired(bot_id)
                else:
                    self.db.update('bots','id',bot_id,error=f'微信连接错误 {e.errcode} / HTTP {e.http_status}')
                failures += 1
            except Exception as e:
                self.db.update('bots','id',bot_id,error=f'微信连接中断：{type(e).__name__}')
                failures += 1
            await asyncio.sleep(min(30, 2 ** min(failures, 5)) if failures else 0.2)

    async def work(self, bot_id):
        wakeup = self.wakeups.setdefault(bot_id,asyncio.Event())
        while True:
            wakeup.clear()
            try:
                async with self.lock(bot_id):
                    await self.step(bot_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.db.update('bots','id',bot_id,error=f'任务处理暂时失败：{type(e).__name__}')
            try:
                await asyncio.wait_for(wakeup.wait(),timeout=2)
            except TimeoutError:
                pass

    def has_next(self, job):
        if job.get('schedule_id'):
            return False
        return self.db.one("SELECT id FROM jobs WHERE bot_id=? AND schedule_id='' AND id>? AND status IN ('queued','preparing','checking') LIMIT 1",
                           (job['bot_id'],job['id'])) is not None

    async def interrupt_job(self, job):
        if job['status'] != 'interrupting':
            self.db.update('jobs','id',job['id'],status='interrupt_sending')
            try:
                response = await self.ma.interrupt(job['session_id'])
                event = response['data'][0]
                info = {'event_id':event['id'],'since':event['created_at'],'deadline':time.time()+60}
                self.db.update('jobs','id',job['id'],status='interrupting',interruption=json.dumps(info),error='')
            except Exception:
                self.db.update('jobs','id',job['id'],status='interrupt_uncertain',error='中断提交结果不确定，请核对 MA；下一条消息尚未发送')
            return
        info = json.loads(job['interruption'])
        if time.time()>info['deadline']:
            self.db.update('jobs','id',job['id'],status='interrupt_uncertain',error='中断确认超过 60 秒，请核对 MA；下一条消息尚未发送')
            return
        events = await self.ma.events(job['session_id'],info['since'])
        result = result_from_events(events,info['event_id'])
        session = await self.ma.request('GET',f"/sessions/{job['session_id']}")
        if result['state']=='idle' and session.get('status')=='idle' and (session.get('stop_reason') or {}).get('type')!='requires_action':
            self.db.update('jobs','id',job['id'],status='interrupted',error='新消息到达，已中断本轮执行',result='')
            self.wakeups.setdefault(job['bot_id'],asyncio.Event()).set()

    async def step(self, bot_id):
        bot = self.db.one('SELECT * FROM bots WHERE id=?', (bot_id,))
        if bot and bot['enabled'] and bot['token']:
            query=self.db.one("SELECT * FROM jobs WHERE bot_id=? AND status='usage_pending' ORDER BY id LIMIT 1",(bot_id,))
            if query:
                def yuan(n): return f'{n/1000000:.6f}'.rstrip('0').rstrip('.')
                total='未设上限' if bot['budget_micro'] is None else '¥'+yuan(bot['budget_micro'])
                remaining='未设上限' if bot['budget_micro'] is None else '¥'+yuan(bot['budget_micro']-bot['spent_micro'])
                result=f"总额度：{total}\n已用金额：¥{yuan(bot['spent_micro'])}\n剩余额度：{remaining}\n以上为已结算金额，运行中任务将在结束后扣费。"
                self.db.update('jobs','id',query['id'],result=result,billing_state='local_command')
                if await self.send_text_messages(bot,query,[result],'usage_pending'):
                    self.db.update('jobs','id',query['id'],status='done')
                return
        if not await self.billing.settle_pending(bot_id):
            return
        bot = self.db.one('SELECT * FROM bots WHERE id=?', (bot_id,))
        if not bot or not bot['enabled'] or not bot['token']:
            await self.stop_stream(bot_id)
            return
        active = self.db.one("SELECT id FROM jobs WHERE bot_id=? AND schedule_id='' AND status NOT IN ('done','failed','cancelled','interrupted','queued','preparing','checking') LIMIT 1",(bot_id,))
        if not active and await self.process_reset(bot):
            return
        job = self.db.one("SELECT * FROM jobs WHERE bot_id=? AND schedule_id='' AND status NOT IN ('done','failed','cancelled','interrupted') ORDER BY id LIMIT 1", (bot_id,))
        if not job:
            await self.stop_stream(bot_id)
            return
        await self.execute_job(bot,job)

    async def execute_job(self, bot, job):
        bot_id=bot['id']
        stream_key='scheduled:'+str(job['id']) if job.get('schedule_id') else bot_id
        if job['status']=='interrupting' or (job['status'] in ('running','requires_action') and self.has_next(job)):
            await self.interrupt_job(job)
            return
        if job['status'] in ('requires_action','uncertain','reply_uncertain','interrupt_uncertain','interrupt_sending'):
            return
        if job['status'] in ('queued','preparing','checking'):
            if self.billing.reject_if_exhausted(bot,job):
                return
            if not job.get('schedule_id') and job['text'].strip() == '/clear' and not json.loads(job['attachments']):
                await self.clear_session(bot,job)
                return
            if not bot['session_id']:
                return
            attachments = json.loads(job['attachments'])
            if attachments:
                if not await self.prepare_attachments(job,attachments):
                    return
            session = await self.ma.request('GET', f"/sessions/{bot['session_id']}")
            if session.get('status') != 'idle' or (session.get('stop_reason') or {}).get('type') == 'requires_action':
                return
            if not await self.billing.prepare(bot,job):
                return
            if attachments and not await self.mount_attachments(bot,job,attachments):
                return
            await self.open_stream(stream_key,bot['session_id'])
            prompt = job['text']
            if attachments:
                attachment_notice = '用户附件已挂载到当前会话，路径如下：\n'+'\n'.join(a['mounted_path'] for a in attachments)
                prompt = '\n\n'.join(part for part in (prompt,attachment_notice) if part)
            self.db.update('jobs','id',job['id'],status='submitting',session_id=bot['session_id'],billing_attempted='1')
            try:
                sent = await self.ma.send(bot['session_id'], prompt)
                event = sent['data'][0]
                state=self.streams.get(stream_key)
                if state and state['session_id']==bot['session_id']:
                    state['events'][event['id']]=event
                self.db.update('jobs','id',job['id'],status='running',event_id=event['id'],since=event['created_at'],error='')
            except MAError as exc:
                if exc.status_code is not None and 400 <= exc.status_code < 500 and exc.status_code != 408:
                    self.db.update('jobs','id',job['id'],status='reply_pending',billing_attempted='',error=str(exc),result=f'本次消息被 MA 拒绝（HTTP {exc.status_code}），请联系管理员检查模型及消息配置。')
                else:
                    self.db.update('jobs','id',job['id'],status='uncertain',error=f'提交结果不确定：{exc}；系统不会自动重复提交')
            except Exception as exc:
                self.db.update('jobs','id',job['id'],status='uncertain',error=f'提交响应无法确认（{type(exc).__name__}），请核对 MA 会话')
            return
        if job['status'] == 'running':
            events = await self.job_events(job)
            if events is None:
                return
            self.billing.record_tools(job,events)
            r = result_from_events(events,job['event_id'])
            if getattr(self.ma,'supports_sse',False) is True and not r['state'] in ('idle','terminated') and not r['error']:
                session={'status':'running'}
            else:
                session = await self.ma.request('GET', f"/sessions/{job['session_id']}")
            if self.has_next(job):
                await self.interrupt_job(job)
                return
            # Events are complete messages (not token deltas). Relay each message
            # while the Agent works; persist a stable cursor across polls/restarts.
            self.db.update('jobs','id',job['id'],result=r['text'],reply_messages=json.dumps(r['messages'],ensure_ascii=False))
            if not job.get('schedule_id') and not await self.send_text_messages(bot,job,r['messages'],'running'):
                return
            reason = session.get('stop_reason') or r['reason']
            if reason.get('type') == 'requires_action':
                if json.loads(job['approval']).get('resolved_batch') == reason.get('pending_batch_id'):
                    return
                self.db.update('jobs','id',job['id'],status='requires_action',approval=json.dumps({'reason':reason,'request':r['approval']},ensure_ascii=False),result=r['text'])
            elif r['error'] or reason.get('type') == 'retries_exhausted' or session.get('status') == 'terminated':
                failure = '本次 MA 执行失败，请联系管理员查看任务记录。'
                self.db.update('jobs','id',job['id'],status='reply_pending',error=r['error'] or reason.get('type') or 'terminated',result=failure,
                    reply_messages=json.dumps(r['messages']+[failure],ensure_ascii=False))
            elif r['state'] == 'idle' and r['reason'].get('type') == 'end_turn' and session.get('status') == 'idle':
                self.db.update('jobs','id',job['id'],status='reply_pending',result=r['text'] or ('' if r['artifacts'] else '任务已结束，没有可返回的文本结果。'),
                    reply_messages=json.dumps(r['messages'],ensure_ascii=False),
                    artifacts=json.dumps(r['artifacts'],ensure_ascii=False))
            return
        if job['status'] == 'reply_pending':
            if not bot['context_token']:
                return
            messages = json.loads(job['reply_messages'] or '[]') or [job['result']]
            if not await self.send_text_messages(bot,job,messages,'reply_pending'):
                return
            if not await self.deliver_artifacts(bot,job):
                return
            latest=self.db.one('SELECT error FROM jobs WHERE id=?',(job['id'],))
            self.db.update('jobs','id',job['id'],status='failed' if latest['error'] else 'done')

    async def send_text_messages(self, bot, job, messages, resume_status):
        chunks = [chunk for message in messages for chunk in reply_chunks(message)]
        if not chunks:
            return True
        if not bot['context_token']:
            return False
        for part in range(job['reply_part'],len(chunks)):
            if self.has_next(job) and resume_status!='usage_pending' and job.get('billing_state')!='rejected':
                if resume_status=='running':
                    current = self.db.one('SELECT * FROM jobs WHERE id=?',(job['id'],))
                    await self.interrupt_job(current)
                else:
                    self.db.update('jobs','id',job['id'],status='interrupted',error='新消息到达，停止旧结果回传')
                return False
            current = self.db.one('SELECT * FROM bots WHERE id=?',(bot['id'],))
            if current['user_id']!=job['user_id'] or not current['token'] or not current['context_token']:
                return False
            self.db.update('jobs','id',job['id'],status='reply_uncertain',reply_resume_status=resume_status)
            try:
                await self.wx.send_message(current['base_url'],current['token'],{
                    'from_user_id':'','to_user_id':job['user_id'],'client_id':f"claw-{bot['id']}-{job['id']}-{part}",
                    'message_type':2,'message_state':2,'context_token':current['context_token'],
                    'item_list':[{'type':1,'text_item':{'text':chunks[part]}}]})
            except ApiError as exc:
                if exc.errcode==-14:
                    self.expired(bot['id'])
                    self.db.update('jobs','id',job['id'],status=resume_status)
                raise
            self.db.update('jobs','id',job['id'],status=resume_status,reply_part=part+1,reply_resume_status='')
        return True

    async def mount_attachments(self, bot, job, attachments):
        for index,a in enumerate(attachments):
            if a.get('mounted_session')==bot['session_id']:
                continue
            a['mount_attempts']=a.get('mount_attempts',0)+1
            try:
                path=f"/uploads/wechat/{job['id']}/{index}/{a['filename']}"
                resource=await self.ma.mount_file(bot['session_id'],a['file_id'],path)
                mounted=resource['mount_path']
                if not mounted.startswith('/mnt/session/'):
                    mounted='/mnt/session'+mounted
                a.update(mounted_session=bot['session_id'],mounted_path=mounted,resource_id=resource['id'])
                self.db.update('jobs','id',job['id'],attachments=json.dumps(attachments,ensure_ascii=False),error='')
            except Exception:
                fields={'error':'会话资源挂载暂时失败，正在重试'}
                if a['mount_attempts']>=3:
                    fields.update(status='reply_pending',error='会话资源挂载失败',result='附件审核通过，但挂载到会话失败，请稍后重新发送。')
                self.db.update('jobs','id',job['id'],attachments=json.dumps(attachments,ensure_ascii=False),**fields)
                return False
        return True

    async def deliver_artifacts(self, bot, job):
        artifacts=json.loads(job['artifacts'])
        def save(**fields):
            self.db.update('jobs','id',job['id'],artifacts=json.dumps(artifacts,ensure_ascii=False),**fields)
        for index,a in enumerate(artifacts):
            if a['status']=='sent':
                continue
            if self.has_next(job):
                save(status='interrupted',error='新消息到达，停止旧产物回传')
                return False
            if not a.get('wechat_item'):
                try:
                    a['attempts']=a.get('attempts',0)+1
                    save()
                    content=await self.ma.download_artifact(job['session_id'],a['file_id'])
                    a['wechat_item']=await upload_wechat_file(self.wx,bot,content,a['filename'])
                    a['status']='uploaded'
                    save()
                except Exception:
                    if a['attempts']>=3:
                        save(status='failed',error=f"产物 {a['filename']} 下载或微信上传失败，可在 MA 控制台获取文件")
                    else:
                        save(error=f"产物 {a['filename']} 正在重试下载或上传")
                    return False
            # Recheck after network I/O; a new message may have arrived meanwhile.
            if self.has_next(job):
                save(status='interrupted',error='新消息到达，停止旧产物回传')
                return False
            current=self.db.one('SELECT * FROM bots WHERE id=?',(bot['id'],))
            if current['user_id']!=job['user_id'] or not current['token'] or not current['context_token']:
                return False
            a['status']='sending'
            save(status='reply_uncertain',reply_resume_status='reply_pending')
            try:
                await self.wx.send_message(current['base_url'],current['token'],{
                    'from_user_id':'','to_user_id':job['user_id'],'client_id':f"claw-{bot['id']}-{job['id']}-file-{index}",
                    'message_type':2,'message_state':2,'context_token':current['context_token'],'item_list':[a['wechat_item']]})
            except ApiError as exc:
                if exc.errcode==-14:
                    self.expired(bot['id'])
                    a['status']='uploaded'
                    save(status='reply_pending')
                raise
            a['status']='sent'
            save(status='reply_pending',error='')
        return True

    async def scheduled_work(self, bot_id):
        stream_key=None
        try:
            while True:
                try:
                    async with self.lock(bot_id):
                        settled=await self.billing.settle_pending(bot_id)
                        job=self.db.one("SELECT * FROM jobs WHERE bot_id=? AND schedule_id!='' AND status NOT IN ('done','failed','cancelled','interrupted') ORDER BY id LIMIT 1",(bot_id,))
                        if not job:
                            return
                        key='scheduled:'+str(job['id'])
                        if stream_key and key!=stream_key:await self.stop_stream(stream_key)
                        stream_key=key
                        bot=self.db.one('SELECT * FROM bots WHERE id=?',(bot_id,))
                        if settled and bot and bot['enabled'] and bot['token']:
                            await self.scheduled_step(bot,job)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.db.update('bots','id',bot_id,error='定时执行暂时失败，正在重试')
                await asyncio.sleep(2)
        finally:
            if stream_key:await self.stop_stream(stream_key)

    async def scheduled_step(self, bot, job):
        if bot['user_id']!=job['user_id']:
            self.db.update('jobs','id',job['id'],status='cancelled',error='Bot 绑定用户已变化')
            return
        if job.get('workspace_id') and job['workspace_id']!=os.getenv('BAILIAN_WORKSPACE_ID',''):
            self.db.update('jobs','id',job['id'],status='failed',error='定时任务属于其他工作空间')
            return
        if not job['session_id'] and job['status'] in ('queued','scheduled_creating'):
            if job['status']=='queued' and self.billing.reject_if_exhausted(bot,job):
                return
            if job['status']=='queued' and bot['budget_micro'] is not None and not self.db.one('SELECT agent_id FROM agent_prices WHERE agent_id=?',(job['scheduled_agent_id'],)):
                self.db.update('jobs','id',job['id'],error='请先配置该 Agent 的计费单价')
                return
            try:
                configured=await self.ensure_memory(bot)
                configured.update(agent_id=job['scheduled_agent_id'],environment_id=job['scheduled_environment_id'],
                    scheduled_job_id=str(job['id']))
                if job['status']=='scheduled_creating':
                    sessions=await self.ma.list_all('/sessions')
                    created=next((s for s in sessions if (s.get('metadata') or {}).get('claw_scheduled_job')==str(job['id'])
                        and (s.get('metadata') or {}).get('claw_bot_id')==bot['id']),None)
                    if not created:
                        self.db.update('jobs','id',job['id'],error='定时会话创建结果待确认，正在查询远端记录')
                        return
                else:
                    self.db.update('jobs','id',job['id'],status='scheduled_creating')
                    created=await self.ma.create_session(configured)
                with self.db.db:
                    self.db.db.execute('INSERT OR IGNORE INTO bot_sessions(session_id,bot_id,agent_id,environment_id,memory_store_id,source,workspace_id) VALUES(?,?,?,?,?,?,?)',
                        (created['id'],bot['id'],configured['agent_id'],configured['environment_id'],configured['memory_store_id'],'scheduled',os.getenv('BAILIAN_WORKSPACE_ID','')))
                    self.db.db.execute("UPDATE jobs SET session_id=?,status='queued',error='' WHERE id=?",(created['id'],job['id']))
                job=self.db.one('SELECT * FROM jobs WHERE id=?',(job['id'],))
            except Exception as exc:
                definitive=isinstance(exc,MAError) and exc.status_code and 400<=exc.status_code<500 and exc.status_code!=408
                self.db.update('jobs','id',job['id'],**({'status':'failed'} if definitive else {}),error='定时会话创建失败' if definitive else '定时会话暂未就绪，正在恢复')
                return
        if job['status'] in ('uncertain','reply_uncertain','requires_action','interrupt_uncertain','scheduled_creating'):
            return
        # Only this job sees the temporary Session; bots.session_id is never modified.
        isolated={**bot,'session_id':job['session_id'],'agent_id':job['scheduled_agent_id'],'environment_id':job['scheduled_environment_id']}
        await self.execute_job(isolated,job)

    async def process_reset(self, bot):
        reset=self.db.one("SELECT * FROM bot_reset_requests WHERE bot_id=? AND status IN ('queued','creating','uncertain') ORDER BY created_at,id LIMIT 1",(bot['id'],))
        if not reset:
            return False
        if reset['retry_at']>time.time():
            return True
        if bot['session_id']!=reset['from_session_id']:
            self.db.execute("UPDATE bot_reset_requests SET status='superseded',session_id=?,error='' WHERE id=?",(bot['session_id'],reset['id']))
            return True
        if reset['status']=='queued' and bot['budget_micro'] is not None and bot['spent_micro']>=bot['budget_micro']:
            self.db.execute("UPDATE bot_reset_requests SET status='failed',error='您已欠费，暂时无法开启新会话' WHERE id=?",(reset['id'],))
            return True
        try:
            old=await self.ma.request('GET',f"/sessions/{bot['session_id']}")
            if old.get('status') not in ('idle','terminated') or (old.get('status')!='terminated' and (old.get('stop_reason') or {}).get('type')=='requires_action'):
                self.db.execute('UPDATE bot_reset_requests SET retry_at=? WHERE id=?',(time.time()+5,reset['id']))
                return True
            bot=await self.ensure_memory(bot)
            if reset['status'] in ('creating','uncertain'):
                sessions=await self.ma.list_all('/sessions')
                created=next((s for s in sessions if (s.get('metadata') or {}).get('claw_reset_request')==reset['id']
                    and (s.get('metadata') or {}).get('claw_bot_id')==bot['id']),None)
                if not created:
                    self.db.execute("UPDATE bot_reset_requests SET status='uncertain',error='创建结果待确认，正在查询远端记录',retry_at=? WHERE id=?",(time.time()+30,reset['id']))
                    return True
            else:
                self.db.execute("UPDATE bot_reset_requests SET status='creating',error='' WHERE id=?",(reset['id'],))
                created=await self.ma.create_session({**bot,'reset_request_id':reset['id']})
            self.db.switch_session(bot,created['id'],bot['agent_id'],bot['environment_id'],reset_request_id=reset['id'])
            await self.stop_stream(bot['id'])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            current=self.db.one('SELECT status FROM bot_reset_requests WHERE id=?',(reset['id'],))['status']
            definitive=isinstance(exc,MAError) and exc.status_code and 400<=exc.status_code<500 and exc.status_code!=408
            state='failed' if definitive else 'uncertain' if current in ('creating','uncertain') else 'queued'
            self.db.execute('UPDATE bot_reset_requests SET status=?,error=?,retry_at=? WHERE id=?',
                (state,'会话创建被拒绝，请联系管理员' if definitive else '暂时无法确认会话状态，稍后自动查询',time.time()+30,reset['id']))
        return True

    async def clear_session(self, bot, job):
        if not bot['agent_id'] or not bot['environment_id']:
            self.db.update('jobs','id',job['id'],status='reply_pending',error='尚未配置 Agent 和环境',result='请先在管理后台选择 Agent 和环境，创建 MA 会话后再使用 /clear。')
            return
        if bot['session_id']:
            current = await self.ma.request('GET',f"/sessions/{bot['session_id']}")
            if current.get('status') not in ('idle','terminated') or (current.get('status')!='terminated' and (current.get('stop_reason') or {}).get('type')=='requires_action'):
                return
        try:
            bot = await self.ensure_memory(bot)
        except Exception:
            self.db.update('jobs','id',job['id'],status='reply_pending',error='专属记忆库暂时不可用',result='暂时无法开启新会话，原会话和记忆已保留，请稍后重试。')
            return
        self.db.update('jobs','id',job['id'],status='session_creating',session_id=bot['session_id'])
        try:
            created = await self.ma.create_session(bot)
            # Persist the switch and command completion together, so restart cannot replay /clear.
            self.db.switch_session(bot,created['id'],bot['agent_id'],bot['environment_id'],clear_job_id=job['id'])
        except MAError as exc:
            if exc.status_code is not None and 400<=exc.status_code<500 and exc.status_code!=408:
                self.db.update('jobs','id',job['id'],status='reply_pending',error=str(exc),result='新会话创建失败，原会话已保留，请稍后重试 /clear。')
            else:
                self.db.update('jobs','id',job['id'],status='uncertain',error='创建 Session 结果不确定，原会话关联已保留；请核对远端会话')
        except Exception:
            self.db.update('jobs','id',job['id'],status='uncertain',error='创建 Session 响应无法确认，请核对远端会话')

    async def prepare_attachments(self, job, attachments):
        # One small step per scheduler pass. IDs and review deadlines survive restarts.
        def save(**fields):
            self.db.update('jobs','id',job['id'],attachments=json.dumps(attachments,ensure_ascii=False),**fields)
        for a in attachments:
            if a['status'] == 'available':
                continue
            try:
                if a.get('status') in ('rejected','type_rejected'):
                    raise ValueError('MA 文件审核未通过或文件类型不支持，请检查后重新发送')
                if not a.get('file_id'):
                    a['attempts'] = a.get('attempts',0)+1
                    if a['attempts'] > 3:
                        raise ValueError('附件上传连续失败，请稍后重新发送')
                    save(status='preparing')
                    content = await download_attachment(a)
                    uploaded = await self.ma.upload_file(a['filename'],content)
                    a.update(file_id=uploaded['id'],status=uploaded.get('status','checking'),
                             deadline=time.time()+300)
                    save(status='checking',error='')
                    return False
                if time.time() > a['deadline']:
                    raise ValueError('MA 文件审核等待超过 5 分钟，请稍后重新发送')
                state = await self.ma.request('GET',f"/files/{a['file_id']}")
                a['status'] = state.get('status','checking')
                save(status='checking',error='')
                if a['status'] in ('rejected','type_rejected'):
                    raise ValueError('MA 文件审核未通过或文件类型不支持，请检查后重新发送')
                if a['status'] != 'available':
                    return False
            except ValueError as exc:
                save(status='reply_pending',error=str(exc),result=str(exc))
                return False
            except Exception:
                save(error='附件传输或审核查询暂时失败，正在重试')
                return False
        save(status='queued',error='')
        return True
