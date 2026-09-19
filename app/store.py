import sqlite3
import json
import os
import secrets
import hashlib
from pathlib import Path
from .media import attachments_from_items, MAX_ATTACHMENTS


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS bots (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT DEFAULT 'draft',
          agent_id TEXT DEFAULT '', environment_id TEXT DEFAULT '', session_id TEXT DEFAULT '',
          user_id TEXT DEFAULT '', account_id TEXT DEFAULT '', token TEXT DEFAULT '',
          base_url TEXT DEFAULT 'https://ilinkai.weixin.qq.com', cursor TEXT DEFAULT '',
          context_token TEXT DEFAULT '', enabled INTEGER DEFAULT 1,
          error TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS bindings (
          bot_id TEXT PRIMARY KEY REFERENCES bots(id), ticket TEXT UNIQUE NOT NULL,
          qr TEXT NOT NULL, url TEXT NOT NULL, status TEXT DEFAULT 'wait',
          poll_base TEXT DEFAULT 'https://ilinkai.weixin.qq.com', verify_code TEXT DEFAULT '',
          expires_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT REFERENCES bots(id),
          message_id TEXT NOT NULL, user_id TEXT NOT NULL, text TEXT NOT NULL,
          status TEXT DEFAULT 'queued', session_id TEXT DEFAULT '',
          event_id TEXT DEFAULT '', since TEXT DEFAULT '', result TEXT DEFAULT '',
          error TEXT DEFAULT '', approval TEXT DEFAULT '{}', reply_part INTEGER DEFAULT 0,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(bot_id,message_id)
        );
        CREATE TABLE IF NOT EXISTS bot_sessions (
          session_id TEXT PRIMARY KEY, bot_id TEXT NOT NULL REFERENCES bots(id),
          agent_id TEXT NOT NULL, environment_id TEXT NOT NULL,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_bot_sessions_bot ON bot_sessions(bot_id,created_at);
        CREATE INDEX IF NOT EXISTS idx_jobs_bot_status ON jobs(bot_id,status,id);
        CREATE TABLE IF NOT EXISTS agent_prices (
          agent_id TEXT PRIMARY KEY, input_micro INTEGER NOT NULL, cache_micro INTEGER NOT NULL,
          output_micro INTEGER NOT NULL, input_includes_cache INTEGER NOT NULL DEFAULT 1,active_hour_micro INTEGER NOT NULL DEFAULT 500000,
          updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS session_billing (session_id TEXT PRIMARY KEY,usage TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS charges (
          id INTEGER PRIMARY KEY AUTOINCREMENT,bot_id TEXT NOT NULL REFERENCES bots(id),
          job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id),session_id TEXT NOT NULL,agent_id TEXT NOT NULL,
          amount_micro INTEGER NOT NULL,normal_input_tokens INTEGER NOT NULL,token_micro INTEGER NOT NULL,active_micro INTEGER NOT NULL,
          usage_before TEXT NOT NULL,usage_after TEXT NOT NULL,usage_delta TEXT NOT NULL,price_snapshot TEXT NOT NULL,components TEXT NOT NULL DEFAULT '{}',
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_charges_bot ON charges(bot_id,id);
        CREATE TABLE IF NOT EXISTS job_tool_calls (
          workspace_id TEXT NOT NULL, session_id TEXT NOT NULL, call_id TEXT NOT NULL,
          job_id INTEGER NOT NULL REFERENCES jobs(id), event_id TEXT NOT NULL,
          name TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT '',
          PRIMARY KEY(workspace_id,session_id,call_id)
        );
        CREATE INDEX IF NOT EXISTS idx_job_tool_calls_job ON job_tool_calls(job_id);
        CREATE TABLE IF NOT EXISTS bot_reset_requests (
          id TEXT PRIMARY KEY, bot_id TEXT NOT NULL REFERENCES bots(id), request_key TEXT NOT NULL,
          from_session_id TEXT NOT NULL, session_id TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'queued', error TEXT NOT NULL DEFAULT '', retry_at REAL NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(bot_id,request_key)
        );
        CREATE TABLE IF NOT EXISTS schedules (
          id TEXT PRIMARY KEY, bot_id TEXT NOT NULL REFERENCES bots(id), user_id TEXT NOT NULL,
          name TEXT NOT NULL, prompt TEXT NOT NULL, rule TEXT NOT NULL,
          request_key TEXT NOT NULL, next_run_at REAL, deleted INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(bot_id,request_key)
        );
        CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(deleted,next_run_at);
        ''')
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(jobs)')}
        if 'attachments' not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'")
        if 'interruption' not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN interruption TEXT NOT NULL DEFAULT '{}'")
        if 'artifacts' not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN artifacts TEXT NOT NULL DEFAULT '[]'")
        for table, fields in {
            'bots': ['memory_store_id','memory_key','memory_state','memory_error','session_memory_id','memory_upgrade_state','billing_error','api_token','api_token_hash'],
            'bot_sessions': ['memory_store_id','source','workspace_id'],
            'jobs': ['reply_messages','reply_resume_status','billing_state','billing_price','billing_before','billing_error','billing_attempted','billing_session_id','workspace_id','schedule_id','scheduled_for','scheduled_agent_id','scheduled_environment_id'],
            'charges': ['components','workspace_id'],
        }.items():
            existing = {r[1] for r in self.db.execute(f'PRAGMA table_info({table})')}
            for field in fields:
                if field not in existing:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")
        existing = {r[1] for r in self.db.execute('PRAGMA table_info(bots)')}
        price_columns = {r[1] for r in self.db.execute('PRAGMA table_info(agent_prices)')}
        if 'web_search_micro' not in price_columns:
            self.db.execute('ALTER TABLE agent_prices ADD COLUMN web_search_micro INTEGER DEFAULT 30000')
        if 'cache_mode' not in price_columns:
            self.db.execute("ALTER TABLE agent_prices ADD COLUMN cache_mode TEXT NOT NULL DEFAULT 'implicit'")
        if 'cache_creation_micro' not in price_columns:
            self.db.execute('ALTER TABLE agent_prices ADD COLUMN cache_creation_micro INTEGER DEFAULT NULL')
        if 'archived' not in existing:
            self.db.execute('ALTER TABLE bots ADD COLUMN archived INTEGER NOT NULL DEFAULT 0')
        if 'budget_micro' not in existing:
            self.db.execute('ALTER TABLE bots ADD COLUMN budget_micro INTEGER DEFAULT NULL')
            self.db.execute('ALTER TABLE bots ADD COLUMN spent_micro INTEGER NOT NULL DEFAULT 0')
        # Recover all session IDs that the local installation still knows about.
        self.db.execute("INSERT OR IGNORE INTO bot_sessions(session_id,bot_id,agent_id,environment_id,created_at,source) SELECT session_id,id,agent_id,environment_id,created_at,'current' FROM bots WHERE session_id!=''")
        self.db.execute("INSERT OR IGNORE INTO bot_sessions(session_id,bot_id,agent_id,environment_id,created_at,source) SELECT session_id,bot_id,'','',MIN(created_at),'jobs' FROM jobs WHERE session_id!='' GROUP BY session_id,bot_id")
        for table in ('bot_sessions','jobs','charges'):
            self.db.execute(f"UPDATE {table} SET workspace_id=? WHERE workspace_id=''",(os.getenv('BAILIAN_WORKSPACE_ID',''),))
        self.db.commit()
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_bot_api_token ON bots(api_token_hash) WHERE api_token_hash!=''")
        for row in self.rows("SELECT id FROM bots WHERE api_token='' OR api_token_hash=''"):
            self.ensure_api_token(row['id'])

    def ensure_api_token(self, bot_id):
        with self.db:
            row=self.db.execute('SELECT api_token FROM bots WHERE id=?',(bot_id,)).fetchone()
            if not row:
                raise ValueError('Bot 不存在')
            token=row[0] or 'cb_'+secrets.token_urlsafe(32)
            self.db.execute('UPDATE bots SET api_token=?,api_token_hash=? WHERE id=?',
                (token,hashlib.sha256(token.encode()).hexdigest(),bot_id))
        return self.one('SELECT * FROM bots WHERE id=?',(bot_id,))

    def rows(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        cur = self.db.execute(sql, args)
        self.db.commit()
        return cur.lastrowid

    def update(self, table, key, value, **fields):
        assert table in {'bots', 'bindings', 'jobs'}
        self.execute(f"UPDATE {table} SET " + ','.join(f'{k}=?' for k in fields) + f' WHERE {key}=?', (*fields.values(), value))

    def switch_session(self, bot, session_id, agent_id, environment_id, clear_job_id=None, reset_request_id=None):
        with self.db:
            if bot['session_id']:
                self.db.execute('INSERT OR IGNORE INTO bot_sessions(session_id,bot_id,agent_id,environment_id) VALUES(?,?,?,?)',
                    (bot['session_id'],bot['id'],bot['agent_id'],bot['environment_id']))
            self.db.execute('INSERT OR IGNORE INTO bot_sessions(session_id,bot_id,agent_id,environment_id,memory_store_id,source,workspace_id) VALUES(?,?,?,?,?,?,?)',
                (session_id,bot['id'],agent_id,environment_id,bot.get('memory_store_id',''),'created',os.getenv('BAILIAN_WORKSPACE_ID','')))
            self.db.execute("UPDATE bots SET agent_id=?,environment_id=?,session_id=?,session_memory_id=?,memory_upgrade_state='',status='ready',error='' WHERE id=?",
                (agent_id,environment_id,session_id,bot.get('memory_store_id',''),bot['id']))
            if clear_job_id is not None:
                self.db.execute("UPDATE jobs SET status='reply_pending',session_id=?,result=?,error='' WHERE id=?",
                    (session_id,'已开启新会话，长期记忆会保留。',clear_job_id))
            if reset_request_id is not None:
                self.db.execute("UPDATE bot_reset_requests SET status='completed',session_id=?,error='' WHERE id=? AND bot_id=?",
                    (session_id,reset_request_id,bot['id']))

    def ingest(self, bot, response):
        # Inbox writes and cursor advancement are atomic: a crash cannot lose a batch.
        with self.db:
            for msg in response.get('msgs', []):
                if msg.get('message_type') != 1 or msg.get('from_user_id') != bot['user_id']:
                    continue
                context = msg.get('context_token')
                if context:
                    self.db.execute('UPDATE bots SET context_token=? WHERE id=?', (context, bot['id']))
                text = '\n'.join(i.get('text_item', {}).get('text', '') for i in msg.get('item_list', []) if i.get('type') == 1).strip()
                mid = msg.get('message_id') or msg.get('client_id')
                if not mid or not context:
                    continue
                items = msg.get('item_list', [])
                attachments = attachments_from_items(items)
                error = ''
                if any(i.get('type') not in (1,2,4) for i in items):
                    error = '当前支持文字、图片和文件，暂不支持语音或视频，请转换后重发。'
                elif len(attachments) > MAX_ATTACHMENTS:
                    error = f'单条消息最多支持 {MAX_ATTACHMENTS} 个附件，请分批发送。'
                elif not text and not attachments:
                    error = '消息内容为空，请重新发送文字、图片或文件。'
                prompt = text if attachments else (text or '[非文本消息]')
                self.db.execute('INSERT OR IGNORE INTO jobs(bot_id,message_id,user_id,text,status,result,attachments,error,workspace_id) VALUES(?,?,?,?,?,?,?,?,?)',
                    (bot['id'], str(mid), bot['user_id'], prompt, 'reply_pending' if error else ('usage_pending' if text=='/usage' and not attachments else 'queued'), error,
                     json.dumps(attachments,ensure_ascii=False),error,os.getenv('BAILIAN_WORKSPACE_ID','')))
            self.db.execute('UPDATE bots SET cursor=? WHERE id=?', (response.get('get_updates_buf', bot['cursor']), bot['id']))
