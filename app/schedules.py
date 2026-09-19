"""Persistent schedule rules and minute-level claiming, independent of chat sessions."""
import asyncio
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator


class ScheduleRule(BaseModel):
    kind: Literal['daily','weekly','interval','once']
    timezone: str = Field(default='Asia/Shanghai',max_length=80)
    time: str | None = Field(default=None,pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    weekdays: list[int] = Field(default_factory=list,max_length=7)
    minutes: int | None = Field(default=None,ge=1,le=525600)
    run_at: datetime | None = None

    @model_validator(mode='after')
    def validate_rule(self):
        try: ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError,ValueError): raise ValueError('无效时区')
        if self.kind in ('daily','weekly') and not self.time:
            raise ValueError('每天/每周任务需要 HH:MM 时间')
        if self.kind=='weekly' and (not self.weekdays or any(d<1 or d>7 for d in self.weekdays)):
            raise ValueError('每周任务需要 weekdays：1=周一，7=周日')
        if self.kind=='interval' and self.minutes is None:
            raise ValueError('间隔任务需要 minutes')
        if self.kind=='once' and (not self.run_at or self.run_at.tzinfo is None):
            raise ValueError('单次任务需要包含时区的 run_at')
        return self


class ScheduleInput(BaseModel):
    name: str = Field(min_length=1,max_length=80)
    prompt: str = Field(min_length=1,max_length=20000)
    rule: ScheduleRule


def next_run(rule, after):
    if rule.kind=='once':
        value=rule.run_at.timestamp()
        return value if value>after else None
    if rule.kind=='interval':
        return after+rule.minutes*60
    zone=ZoneInfo(rule.timezone)
    local=datetime.fromtimestamp(after,zone)
    hour,minute=map(int,rule.time.split(':'))
    for offset in range(9):
        date=local.date()+timedelta(days=offset)
        if rule.kind=='weekly' and date.isoweekday() not in rule.weekdays:
            continue
        candidate=datetime(date.year,date.month,date.day,hour,minute,tzinfo=zone)
        stamp=candidate.timestamp()
        # Skip nonexistent DST times; ambiguous local times run once (fold=0).
        if datetime.fromtimestamp(stamp,zone).replace(tzinfo=None)!=candidate.replace(tzinfo=None):
            continue
        if stamp>after:return stamp
    raise ValueError('无法计算下一次执行时间')


def view(row):
    return {**{k:row[k] for k in ('id','bot_id','name','prompt','created_at','deleted')},
        'rule':json.loads(row['rule']),
        'next_run_at':datetime.fromtimestamp(row['next_run_at'],timezone.utc).isoformat() if row['next_run_at'] else None,
        **({'bot_name':row['bot_name']} if 'bot_name' in row else {})}


def create(db, bot, body, key):
    if not 8<=len(key)<=100 or not all(c.isascii() and (c.isalnum() or c in '_-') for c in key):
        raise HTTPException(422,'需要 8–100 位 Idempotency-Key')
    rule=body.rule.model_dump_json()
    existing=db.one('SELECT * FROM schedules WHERE bot_id=? AND request_key=?',(bot['id'],key))
    if existing:
        if (existing['name'],existing['prompt'],existing['rule'])!=(body.name,body.prompt,rule):
            raise HTTPException(409,'同一请求键不能用于不同定时任务')
        return view(existing)
    if not bot['token'] or not bot['agent_id'] or not bot['environment_id']:
        raise HTTPException(409,'请先绑定 Bot 并配置 Agent 与环境')
    due=next_run(body.rule,time.time())
    if due is None:raise HTTPException(422,'单次任务时间必须在未来')
    sid=secrets.token_hex(16)
    db.execute('INSERT INTO schedules(id,bot_id,user_id,name,prompt,rule,request_key,next_run_at) VALUES(?,?,?,?,?,?,?,?)',
        (sid,bot['id'],bot['user_id'],body.name,body.prompt,rule,key,due))
    return view(db.one('SELECT * FROM schedules WHERE id=?',(sid,)))


def remove(db, row):
    with db.db:
        db.db.execute('UPDATE schedules SET deleted=1,next_run_at=NULL WHERE id=?',(row['id'],))
        db.db.execute("UPDATE jobs SET status='cancelled',error='定时任务已删除，取消尚未执行的记录' WHERE schedule_id=? AND status='queued' AND session_id=''",(row['id'],))
    return {'ok':True,'id':row['id'],'note':'已停止后续触发；已开始的执行继续完成，历史记录保留'}


def runs(db, sid, offset=0):
    if offset<0:raise HTTPException(422,'offset 不能为负数')
    items=db.rows('''SELECT j.id,j.bot_id,j.schedule_id,j.scheduled_for,j.status,j.session_id,j.text,j.result,j.error,
        j.created_at,j.billing_state,j.billing_error,c.id AS charge_id,c.amount_micro AS charge_micro
        FROM jobs j LEFT JOIN charges c ON c.job_id=j.id WHERE schedule_id=? ORDER BY j.id DESC LIMIT 50 OFFSET ?''',(sid,offset))
    return {'items':items,'total':db.one('SELECT COUNT(*) AS n FROM jobs WHERE schedule_id=?',(sid,))['n']}


class Scheduler:
    def __init__(self, runtime):
        self.rt=runtime
        self.db=runtime.db

    async def loop(self):
        while True:
            try:self.scan(time.time())
            except asyncio.CancelledError:raise
            except Exception:
                import logging
                logging.getLogger(__name__).exception('定时任务扫描失败')
            await asyncio.sleep(60)

    def scan(self, now):
        for item in self.db.rows("SELECT s.* FROM schedules s JOIN bots b ON b.id=s.bot_id WHERE s.deleted=0 AND s.next_run_at<=? AND b.enabled=1 AND b.token!='' ORDER BY s.next_run_at LIMIT 500",(now,)):
            bot=self.db.one('SELECT * FROM bots WHERE id=?',(item['bot_id'],))
            if not bot or not bot['enabled'] or not bot['token']:
                continue
            if bot['user_id']!=item['user_id']:
                self.db.execute('UPDATE schedules SET deleted=1,next_run_at=NULL WHERE id=?',(item['id'],))
                continue
            due=item['next_run_at'];rule=ScheduleRule.model_validate_json(item['rule'])
            # Coalesce missed ticks on restart; do not flood users with old reports.
            following=(due+(int((now-due)//(rule.minutes*60))+1)*rule.minutes*60) if rule.kind=='interval' else next_run(rule,now)
            with self.db.db:
                claimed=self.db.db.execute('UPDATE schedules SET next_run_at=? WHERE id=? AND deleted=0 AND next_run_at=?',(following,item['id'],due)).rowcount
                if not claimed:continue
                active=self.db.db.execute("SELECT id FROM jobs WHERE schedule_id=? AND status NOT IN ('done','failed','cancelled','interrupted') LIMIT 1",(item['id'],)).fetchone()
                status='cancelled' if active else 'queued'
                self.db.db.execute('''INSERT OR IGNORE INTO jobs(bot_id,message_id,user_id,text,status,error,schedule_id,scheduled_for,scheduled_agent_id,scheduled_environment_id,workspace_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(bot['id'],f"schedule:{item['id']}:{due}",bot['user_id'],item['prompt'],status,
                    '上一轮尚未结束，跳过本次触发' if active else '',item['id'],str(due),bot['agent_id'],bot['environment_id'],os.getenv('BAILIAN_WORKSPACE_ID','')))
            self.rt.start('scheduled:'+bot['id'],self.rt.scheduled_work(bot['id']))
