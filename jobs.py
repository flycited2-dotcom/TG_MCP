"""Daily limits and background jobs for joining, leaving and posting to many chats.

Limits come from the server environment only, so an agent cannot raise them. A job lives as long as the
server process: after a restart its unfinished steps are reported as not attempted and are never resumed
automatically (a step whose outcome is unknown is never repeated).
"""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import random
import secrets
import sqlite3
import time

from telegram_backend import BackendError,_safe_error

# A step failing with one of these codes ends the whole job: continuing could extend a restriction or repeat an unknown send.
STOP_CODES={'rate_limited','privacy_restricted','authorization_required','timeout','connection_error','telegram_error',
            'flood_wait','too_many_chats','daily_limit_reached','result_unconfirmed'}
DAY=86400


def _int(env,name,default,low,high):
    try:
        value=int(env.get(name,default))
    except (TypeError,ValueError):
        value=default
    return max(low,min(high,value))


class Limits:
    """Conservative defaults; only the server owner can change them through the environment."""

    def __init__(self,env=None):
        env=os.environ if env is None else env
        self.join_daily=_int(env,'TG_JOIN_DAILY_LIMIT',10,0,200)
        self.leave_daily=_int(env,'TG_LEAVE_DAILY_LIMIT',30,0,500)
        self.message_daily=_int(env,'TG_CAMPAIGN_DAILY_LIMIT',20,0,500)
        self.campaign_max_chats=_int(env,'TG_CAMPAIGN_MAX_CHATS',30,1,200)
        self.min_delay=_int(env,'TG_CAMPAIGN_MIN_DELAY',45,0,3600)
        self.dedupe_days=_int(env,'TG_CAMPAIGN_DEDUPE_DAYS',14,0,365)

    def as_dict(self):
        return {'joins_per_day':self.join_daily,'leaves_per_day':self.leave_daily,'campaign_messages_per_day':self.message_daily,
                'campaign_max_chats':self.campaign_max_chats,'min_pause_seconds':self.min_delay,'same_text_pause_days':self.dedupe_days}


class Ledger:
    """What this account already did recently: joins, leaves and campaign messages."""

    def __init__(self,path,clock=time.time):
        self.path=Path(path)
        self.clock=clock

    def _db(self):
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        db=sqlite3.connect(self.path,timeout=10)
        db.execute('CREATE TABLE IF NOT EXISTS events(kind TEXT,ts REAL,chat_id INTEGER,digest TEXT)')
        db.execute('DELETE FROM events WHERE ts<?',(self.clock()-90*DAY,))
        return db

    def used(self,kind):
        db=self._db()
        try:
            return db.execute('SELECT count(*) FROM events WHERE kind=? AND ts>=?',(kind,self.clock()-DAY)).fetchone()[0]
        finally:
            db.close()

    def add(self,kind,chat_id=None,digest=None):
        db=self._db()
        try:
            db.execute('INSERT INTO events VALUES (?,?,?,?)',(kind,self.clock(),chat_id,digest))
            db.commit()
        finally:
            db.close()

    def sent_recently(self,chat_id,digest,days):
        if days<=0:
            return False
        db=self._db()
        try:
            return db.execute("SELECT 1 FROM events WHERE kind='message' AND chat_id=? AND digest=? AND ts>=?",(chat_id,digest,self.clock()-days*DAY)).fetchone() is not None
        finally:
            db.close()


class JobStore:
    def __init__(self,path,clock=time.time):
        self.path=Path(path)
        self.clock=clock
        self._interrupt_running()

    def _db(self):
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        db=sqlite3.connect(self.path,timeout=10)
        db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT,state TEXT,results TEXT,note TEXT,client TEXT,created REAL,updated REAL)')
        return db

    def _interrupt_running(self):
        db=self._db()
        try:
            for row in db.execute("SELECT id,results FROM jobs WHERE state='running'").fetchall():
                results=[({**item,'status':'not_attempted'} if item.get('status')=='pending' else item) for item in json.loads(row['results'])]
                db.execute("UPDATE jobs SET state='interrupted',results=?,note=?,updated=? WHERE id=?",
                           (json.dumps(results,ensure_ascii=False),'The server restarted. Steps listed as not_attempted were NOT run; nothing is resumed automatically.',self.clock(),row['id']))
            db.commit()
        finally:
            db.close()

    def create(self,kind,results,client):
        identifier='j_'+secrets.token_urlsafe(9)
        db=self._db()
        try:
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)",(identifier,kind,'running',json.dumps(results,ensure_ascii=False),None,client,self.clock(),self.clock()))
            db.commit()
        finally:
            db.close()
        return identifier

    def record(self,identifier,results,state=None,note=None):
        db=self._db()
        try:
            db.execute('UPDATE jobs SET results=?,state=COALESCE(?,state),note=COALESCE(?,note),updated=? WHERE id=?',
                       (json.dumps(results,ensure_ascii=False),state,note,self.clock(),identifier))
            db.commit()
        finally:
            db.close()

    def get(self,identifier):
        db=self._db()
        try:
            row=db.execute('SELECT * FROM jobs WHERE id=?',(identifier,)).fetchone()
        finally:
            db.close()
        if row is None:
            raise BackendError('job_not_found','No such job.')
        return {**dict(row),'results':json.loads(row['results'])}

    def recent(self,limit=20):
        db=self._db()
        try:
            rows=db.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT ?',(limit,)).fetchall()
        finally:
            db.close()
        return [{**dict(row),'results':json.loads(row['results'])} for row in rows]

    def running(self):
        db=self._db()
        try:
            row=db.execute("SELECT id FROM jobs WHERE state='running' ORDER BY created DESC LIMIT 1").fetchone()
        finally:
            db.close()
        return row['id'] if row else None


def view(job):
    counts={}
    for item in job['results']:
        counts[item.get('status','pending')]=counts.get(item.get('status','pending'),0)+1
    return {'job_id':job['id'],'kind':job['kind'],'state':job['state'],'total':len(job['results']),'counts':counts,'steps':job['results'],'note':job['note'],
            'started_at':job['created'],'updated_at':job['updated']}


class Jobs:
    """Mixed into ExtendedBackend. Needs self.limits, self._lock, self._ready and self.audit."""

    def _ledger(self):
        if not hasattr(self,'_ledger_store'):
            self._ledger_store=Ledger(self.drafts_db.parent/'limits.sqlite',self._clock)
        return self._ledger_store

    def _jobs(self):
        if not hasattr(self,'_job_store'):
            self._job_store=JobStore(self.drafts_db.parent/'jobs.sqlite',self._clock)
            self._job_tasks=set()
            self._cancelled=set()
        return self._job_store

    async def _sleep(self,seconds):
        await asyncio.sleep(seconds)

    async def _locked_step(self,factory,timeout=60):
        """One short Telegram operation under the account lock; pauses between steps happen outside it."""
        async with self._lock:
            try:
                return await asyncio.wait_for(factory(),timeout)
            except BackendError:
                raise
            except Exception as error:
                raise _safe_error(error) from None

    async def _step_retry(self,factory):
        """A short flood wait (<=30 s) is waited out once; anything longer stops the job."""
        try:
            return await factory()
        except BackendError as error:
            if error.code=='rate_limited' and (error.retry_after_seconds or 0)<=30:
                await self._sleep((error.retry_after_seconds or 0)+1)
                return await factory()
            raise

    def check_quota(self,kind,wanted,limit):
        used=self._ledger().used(kind)
        if used+wanted>limit:
            raise BackendError('daily_limit_reached',f'The daily limit for this action is {limit} per 24 hours; {used} already used, {wanted} requested. The server owner sets the limit.',
                               diagnostics={'used_24h':used,'limit_24h':limit,'requested':wanted})

    async def start_job(self,kind,steps,runner,delay):
        store=self._jobs()
        running=store.running()
        if running:
            raise BackendError('job_already_running','Another job is still running. Wait for it or cancel it first.',diagnostics={'job_id':running})
        from request_context import current_client
        results=[{'status':'pending',**{key:value for key,value in step.items() if key in ('chat_id','title','target','name','operation')}} for step in steps]
        identifier=store.create(kind,results,current_client.get())
        task=asyncio.get_running_loop().create_task(self._run_job(identifier,steps,runner,delay,results))
        self._job_tasks.add(task)
        task.add_done_callback(self._job_tasks.discard)
        return identifier

    async def _run_job(self,identifier,steps,runner,delay,results):
        store=self._jobs()
        state,note='done',None
        try:
            for index,step in enumerate(steps):
                if index and delay:
                    await self._sleep(random.uniform(*delay))
                if identifier in self._cancelled:
                    for rest in range(index,len(steps)):
                        results[rest]={**results[rest],'status':'cancelled'}
                    state,note='cancelled','Cancelled by the user; steps marked cancelled were not run.'
                    break
                try:
                    outcome=await runner(step)
                    results[index]={**results[index],**outcome}
                except BackendError as error:
                    results[index]={**results[index],'status':'failed','error':error.code,'message':str(error)[:200]}
                    if error.code in STOP_CODES:
                        for rest in range(index+1,len(steps)):
                            results[rest]={**results[rest],'status':'not_attempted'}
                        state,note='stopped',f'Stopped after {error.code}'+(f' (Telegram asks to wait {error.retry_after_seconds} s)' if error.retry_after_seconds else '')+'. Remaining steps were not attempted.'
                        store.record(identifier,results,state,note)
                        return
                store.record(identifier,results)
        except asyncio.CancelledError:
            for rest,item in enumerate(results):
                if item.get('status')=='pending':
                    results[rest]={**item,'status':'not_attempted'}
            store.record(identifier,results,'interrupted','The server stopped; steps listed as not_attempted were not run.')
            raise
        except Exception:
            for rest,item in enumerate(results):
                if item.get('status')=='pending':
                    results[rest]={**item,'status':'not_attempted'}
            state,note='stopped','Unexpected internal error; remaining steps were not attempted.'
        store.record(identifier,results,state,note)
        self._cancelled.discard(identifier)

    async def job_wait(self,identifier,timeout_s=20):
        deadline=time.monotonic()+timeout_s
        while True:
            job=self._jobs().get(identifier)
            if job['state']!='running' or time.monotonic()>=deadline:
                return view(job)
            await self._sleep(0.25)

    def job_status(self,identifier):
        return view(self._jobs().get(identifier))

    def list_jobs(self,limit=10):
        return {'jobs':[{k:v for k,v in view(job).items() if k!='steps'} for job in self._jobs().recent(limit)]}

    def job_cancel(self,identifier):
        job=self._jobs().get(identifier)
        if job['state']!='running':
            return {**view(job),'cancelled':False,'message':'The job is not running.'}
        self._cancelled.add(identifier)
        return {**view(job),'cancelled':True,'message':'Cancellation requested; a step already in progress finishes, the rest are skipped.'}

    def limits_status(self):
        ledger=self._ledger()
        used={'joins':ledger.used('join'),'leaves':ledger.used('leave'),'campaign_messages':ledger.used('message')}
        remaining={'joins':max(0,self.limits.join_daily-used['joins']),'leaves':max(0,self.limits.leave_daily-used['leaves']),
                   'campaign_messages':max(0,self.limits.message_daily-used['campaign_messages'])}
        return {'limits':self.limits.as_dict(),'used_last_24h':used,'remaining_24h':remaining,'running_job':self._jobs().running(),
                'note':'Limits protect the account from Telegram restrictions and can be changed only by the server owner.'}

    async def collect_job(self,result,wait_s=20):
        """Give a short job a moment to finish so one-chat actions return their final outcome."""
        if 'job_id' not in result or result.get('already_executed'):
            return result
        try:
            result['job']=await self.job_wait(result['job_id'],wait_s)
        except BackendError as error:
            result['job_error']=error.as_dict()
        return result
