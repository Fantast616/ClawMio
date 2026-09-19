"""Local budget ledger; money is stored as integer millionths of a yuan."""
import json
import os
from decimal import Decimal, ROUND_HALF_UP
from .ma import normalize_event

FIELDS = ('input_tokens','cache_read_input_tokens','cache_creation_input_tokens','output_tokens')
TERMINAL = ('done','failed','cancelled','interrupted')


def micros(value):
    return int((Decimal(str(value))*1000000).quantize(Decimal('1'),rounding=ROUND_HALF_UP))


def usage_of(session, cache_mode='implicit'):
    usage = dict(session.get('usage') or {})
    if cache_mode=='implicit' and usage.get('cache_creation_input_tokens') is None:
        usage['cache_creation_input_tokens']=0
    if any(type(usage.get(k)) is not int or usage[k]<0 for k in FIELDS):
        raise ValueError('Session 用量字段缺失或无效，保留待结算，稍后重试')
    seconds=(session.get('stats') or {}).get('active_seconds')
    if type(seconds) not in (int,float) or not Decimal(str(seconds)).is_finite() or seconds<0:
        raise ValueError('Session 活跃时间缺失或无效，保留待结算')
    return {**{k:usage[k] for k in FIELDS},'active_microseconds':micros(seconds)}


class Billing:
    def __init__(self, db, ma):
        self.db,self.ma=db,ma

    def record_tools(self, job, events):
        started = False
        with self.db.db:
            for raw in events:
                ev = normalize_event(raw)
                if ev['id']==job['event_id']:
                    started=True
                    continue
                if not started:
                    continue
                if ev.get('type')=='message' and ev.get('role')=='user' and not ev['thread_id'].startswith('sthr_'):
                    break
                if ev.get('type')!='tool_call' or ev.get('role')!='assistant' or ev.get('status','completed')!='completed':
                    continue
                for block in ev.get('content') or []:
                    data=block.get('data') or {}
                    if block.get('delta') or block.get('status','completed')!='completed' or not isinstance(data,dict):
                        continue
                    if data.get('name')!='web_search' or not data.get('call_id') or not ev['id']:
                        continue
                    self.db.db.execute('''INSERT OR IGNORE INTO job_tool_calls
                        (workspace_id,session_id,call_id,job_id,event_id,name,created_at) VALUES(?,?,?,?,?,?,?)''',
                        (job.get('workspace_id') or os.getenv('BAILIAN_WORKSPACE_ID',''),job['session_id'],data['call_id'],job['id'],ev['id'],'web_search',ev['created_at']))
        return started

    def is_exhausted(self, bot, agent_id=None):
        priced=self.db.one('SELECT agent_id FROM agent_prices WHERE agent_id=?',(agent_id or bot['agent_id'],))
        return bool(priced and bot['budget_micro'] is not None and bot['spent_micro']>=bot['budget_micro'])

    def reject_if_exhausted(self, bot, job):
        if not self.is_exhausted(bot,job.get('scheduled_agent_id')):
            return False
        self.db.update('jobs','id',job['id'],status='reply_pending',billing_state='rejected',
            error='预算不足，未提交 MA',result='您已欠费，暂时无法执行，请补充预算后重试。')
        self.db.update('bots','id',bot['id'],billing_error='预算已用尽，请调整预算后继续')
        return True

    async def prepare(self, bot, job):
        if job.get('billing_state')=='pending' and job.get('billing_session_id')==bot['session_id']:
            return True
        price=self.db.one('SELECT * FROM agent_prices WHERE agent_id=?',(bot['agent_id'],))
        if not price:
            self.db.update('jobs','id',job['id'],billing_state='unpriced')
            self.db.update('bots','id',bot['id'],billing_error='')
            return True  # No Agent rate means local billing and budget enforcement are disabled.
        if price.get('cache_mode')=='explicit' and price.get('cache_creation_micro') is None:
            self.db.update('bots','id',bot['id'],billing_error='请先配置该 Agent 的缓存创建单价')
            return False
        if bot['budget_micro'] is not None and bot['spent_micro']>=bot['budget_micro']:
            self.db.update('bots','id',bot['id'],billing_error='预算已用尽，请调整预算后继续')
            return False
        previous=self.db.one('SELECT * FROM session_billing WHERE session_id=?',(bot['session_id'],))
        if previous:
            before=json.loads(previous['usage'])
        else:
            before=usage_of(await self.ma.request('GET',f"/sessions/{bot['session_id']}"),price.get('cache_mode','implicit'))
        with self.db.db:
            price['tool_tracking']=1
            self.db.db.execute('INSERT OR IGNORE INTO session_billing(session_id,usage) VALUES(?,?)',
                (bot['session_id'],json.dumps(before)))
            self.db.db.execute("UPDATE jobs SET billing_state='pending',billing_price=?,billing_before=?,billing_session_id=?,billing_error='' WHERE id=?",
                (json.dumps(price),json.dumps(before),bot['session_id'],job['id']))
            self.db.db.execute("UPDATE bots SET billing_error='' WHERE id=?",(bot['id'],))
        return True

    async def settle_pending(self, bot_id):
        jobs=self.db.rows("SELECT * FROM jobs WHERE bot_id=? AND billing_state='pending' ORDER BY id",(bot_id,))
        for job in jobs:
            if job['status'] not in TERMINAL:
                continue
            if job.get('workspace_id') and job['workspace_id']!=os.getenv('BAILIAN_WORKSPACE_ID',''):
                self.db.update('jobs','id',job['id'],billing_error='任务属于其他工作空间，请使用原凭证完成结算')
                return False
            if not job['event_id']:
                if job['billing_attempted']:
                    self.db.update('jobs','id',job['id'],billing_error='提交结果未确认，无法确定用量归属；请核对远端事件')
                    self.db.update('bots','id',bot_id,billing_error='存在未确认提交的计费任务，暂缓后续执行')
                    return False
                self.db.update('jobs','id',job['id'],billing_state='not_submitted')
                continue
            try:
                session=await self.ma.request('GET',f"/sessions/{job['session_id']}")
                if session.get('status') not in ('idle','terminated'):
                    raise ValueError('远端会话尚未结束，暂不结算')
                before=json.loads(job['billing_before']);price=json.loads(job['billing_price'])
                after=usage_of(session,price.get('cache_mode','implicit'))
                if price.get('tool_tracking'):
                    events=await self.ma.events(job['session_id'],job['since'])
                    if not self.record_tools(job,events):
                        raise ValueError('尚未查到本轮起始事件，等待补查工具用量后结算')
                search_count=self.db.one('SELECT COUNT(*) AS n FROM job_tool_calls WHERE job_id=? AND name=?',(job['id'],'web_search'))['n']
                delta={k:after[k]-before[k] for k in (*FIELDS,'active_microseconds')}
                if any(v<0 for v in delta.values()):
                    raise ValueError('Session 累计用量回退，暂不扣费，请核对用量')
                normal=delta['input_tokens']-delta['cache_read_input_tokens']
                explicit=price.get('cache_mode','implicit')=='explicit'
                if explicit:
                    if price.get('cache_creation_micro') is None:
                        raise ValueError('本轮显式缓存缺少缓存创建单价，暂不结算')
                    normal-=delta['cache_creation_input_tokens']
                if normal<0:
                    raise ValueError('缓存用量超过总输入用量，暂不扣费，请核对 MA 用量')
                def cost(tokens, rate):
                    return int((Decimal(tokens*rate)/1000000).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
                components={'input_micro':cost(normal,price['input_micro']),
                    'cache_micro':cost(delta['cache_read_input_tokens'],price['cache_micro']),
                    'output_micro':cost(delta['output_tokens'],price['output_micro'])}
                if explicit:
                    components['cache_creation_micro']=cost(delta['cache_creation_input_tokens'],price['cache_creation_micro'])
                token_amount=sum(components.values())
                active_amount=int((Decimal(delta['active_microseconds']*price['active_hour_micro'])/3600000000).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
                search_rate=price.get('web_search_micro')
                search_amount=search_count*(search_rate or 0)
                amount=token_amount+active_amount+search_amount
                components['active_micro']=active_amount
                components.update(web_search_count=search_count,web_search_micro=search_amount,
                    web_search_priced=search_rate is not None)
                delta['web_search_calls']=search_count
                if amount>9000000000000000:
                    raise ValueError('计费金额异常，请核对单价和用量')
                with self.db.db:
                    # Ledger, debit, usage checkpoint and job state are one transaction.
                    exists=self.db.db.execute('SELECT id FROM charges WHERE job_id=?',(job['id'],)).fetchone()
                    if not exists:
                        self.db.db.execute('''INSERT INTO charges(bot_id,job_id,session_id,agent_id,amount_micro,normal_input_tokens,
                            usage_before,usage_after,usage_delta,price_snapshot,token_micro,active_micro,components,workspace_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                            (bot_id,job['id'],job['session_id'],price['agent_id'],amount,normal,
                             json.dumps(before),json.dumps(after),json.dumps(delta),json.dumps(price),token_amount,active_amount,json.dumps(components),os.getenv('BAILIAN_WORKSPACE_ID','')))
                        self.db.db.execute('UPDATE bots SET spent_micro=spent_micro+?,billing_error=\'\' WHERE id=?',(amount,bot_id))
                        self.db.db.execute('UPDATE session_billing SET usage=? WHERE session_id=?',(json.dumps(after),job['session_id']))
                    self.db.db.execute("UPDATE jobs SET billing_state='settled',billing_error='' WHERE id=?",(job['id'],))
            except Exception as exc:
                message=str(exc) if isinstance(exc,ValueError) else '用量查询暂时失败，等待重试结算'
                self.db.update('jobs','id',job['id'],billing_error=message)
                self.db.update('bots','id',bot_id,billing_error=message)
                return False
        return True
