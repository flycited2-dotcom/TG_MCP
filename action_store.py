"""Single-use confirmed actions, durable even across worker crashes/restarts."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

from telegram_backend import BackendError
from file_store import ClosingConnection


def fingerprint(payload):
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


class ActionStore:
    def __init__(self,path,clock=time.time):
        self.path=Path(path)
        self.clock=clock

    def db(self):
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        db=sqlite3.connect(self.path,timeout=10,factory=ClosingConnection)
        db.row_factory=sqlite3.Row
        db.execute("CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL,preview TEXT NOT NULL,expires REAL NOT NULL,state TEXT NOT NULL,result TEXT)")
        db.execute("UPDATE actions SET payload='' WHERE expires < ? AND state NOT IN ('sending')",(self.clock(),))
        db.execute("UPDATE actions SET preview=json_object('action',json_extract(preview,'$.action')) WHERE expires < ? AND state != 'sending'",(self.clock(),))
        db.commit()
        if os.name!='nt':
            os.chmod(self.path,0o600)
        return db

    def prepare(self,payload,preview):
        identifier=secrets.token_urlsafe(32)
        digest=fingerprint(payload)
        expires=self.clock()+600
        with self.db() as db:
            db.execute("INSERT INTO actions VALUES (?,?,?,?,?,'prepared',NULL)",
                       (identifier,json.dumps(payload,ensure_ascii=False),digest,json.dumps(preview,ensure_ascii=False),expires))
        db.close()
        return {'draft_id':identifier,'confirmation_hash':digest,'preview':preview,
                'expires_at':expires,'requires_explicit_confirmation':True,'published':False}

    def inspect(self,identifier):
        with self.db() as db:
            row=db.execute('SELECT * FROM actions WHERE id=?',(identifier,)).fetchone()
        db.close()
        if not row:
            raise BackendError('draft_not_found','Prepare an action and review its full preview first.')
        if row['state']=='prepared' and row['expires']<=self.clock():
            raise BackendError('draft_expired','Prepare and review a new draft.')
        return dict(row)

    def claim(self,identifier,digest):
        db=self.db()
        try:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM actions WHERE id=?',(identifier,)).fetchone()
            if not row or not isinstance(digest,str) or not hmac.compare_digest(row['digest'],digest):
                raise BackendError('draft_mismatch','The confirmation hash must match the exact reviewed action.')
            if row['state']=='done':
                result=json.loads(row['result'])
                result['already_executed']=True
                return None,result
            if row['state'] in ('sending','uncertain'):
                raise BackendError('delivery_uncertain','Already submitted. Check Telegram before preparing a replacement; no retry was made.')
            if row['expires']<=self.clock():
                raise BackendError('draft_expired','Prepare and review a new action.')
            db.execute("UPDATE actions SET state='sending' WHERE id=?",(identifier,))
            db.commit()
            return json.loads(row['payload']),None
        finally:
            db.close()

    def finish(self,identifier,result=None):
        with self.db() as db:
            db.execute("UPDATE actions SET state=?,payload='',result=? WHERE id=?",
                       ('done' if result is not None else 'uncertain',json.dumps(result,ensure_ascii=False) if result is not None else None,identifier))
        db.close()
