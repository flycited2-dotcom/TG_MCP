"""Import finished media from the owner's Grok computer bridge into Telegram staging, without manual downloads.

PUSH design (the Grok site keeps media behind the owner's login, so this server cannot pull it):
  1. An agent calls telegram_import_grok_media(asset_id): a request is recorded here.
  2. The owner's computer (relay_client.py of the Grok bridge) polls /media-bridge/requests with a SEPARATE
     bridge token, finds the original file it downloaded for that asset and streams it to /media-bridge/upload.
  3. Size, SHA-256 and the real format (MP4/JPEG/PNG/WebP by signature) are verified; the file gets a normal
     staged file_id, ready for telegram_prepare_send. Sending still needs the usual reviewed confirmation.

The bridge token can only list requests and deliver bytes for them: it cannot read Telegram or send anything.
Browser cookies or Grok credentials never travel between the services.
"""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from typing import Annotated,Any

from pydantic import Field
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse
from starlette.routing import Route

from telegram_backend import BackendError

ASSET=re.compile(r'^[A-Za-z0-9_-]{1,128}$')
IMPORT_ID=re.compile(r'^imp_[A-Za-z0-9_-]{16,40}$')
REQUEST_TTL=1800
SIGNATURES='video/mp4, image/jpeg, image/png, image/webp'
LOG=logging.getLogger('personal_telegram.media_import')
FAILURE_TEXT={
 'SOURCE_UNAVAILABLE':'The Grok computer bridge did not deliver the file in time. Check that the computer is on and the Grok relay is running, then ask for the status again; nothing was sent.',
 'SOURCE_EXPIRED':'The Grok computer no longer has this file. Download it again in Grok, then import it again.',
 'TRANSFER_UNAUTHORIZED':'The Grok bridge is not authorised for media transfer on this server.',
 'FILE_TOO_LARGE':'The file is larger than the configured transfer limit.',
 'INVALID_MEDIA_TYPE':'Only MP4 video and JPEG, PNG or WebP images can be imported.',
 'CHECKSUM_MISMATCH':'The received bytes do not match the expected size or SHA-256; the file was discarded.',
 'TRANSFER_INCOMPLETE':'The file arrived incompletely and was discarded.',
}


def sniff(header):
    """Real type from the first bytes: (mime, telegram media kind, extension) or None."""
    if len(header)>=12 and header[4:8]==b'ftyp':
        return 'video/mp4','video','.mp4'
    if header.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg','photo','.jpg'
    if header.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png','photo','.png'
    if header.startswith(b'RIFF') and header[8:12]==b'WEBP':
        return 'image/webp','photo','.webp'
    return None


class MediaImports:
    def __init__(self,files,path,token_path,clock=time.time):
        self.files=files
        self.path=Path(path)
        self.token_path=Path(token_path)
        self.clock=clock

    def token(self):
        try:
            value=self.token_path.read_text(encoding='utf-8').strip()
        except OSError:
            return None
        return value if len(value)>=32 and not any(c.isspace() for c in value) else None

    def authorised(self,request):
        expected=self.token()
        supplied=request.headers.get('authorization','')
        return bool(expected) and supplied.startswith('Bearer ') and hmac.compare_digest(supplied[7:].encode(),expected.encode())

    def _db(self):
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        db=sqlite3.connect(self.path,timeout=10)
        db.row_factory=sqlite3.Row
        db.execute('''CREATE TABLE IF NOT EXISTS imports(id TEXT PRIMARY KEY,source TEXT NOT NULL,asset_id TEXT NOT NULL,idem TEXT NOT NULL,state TEXT NOT NULL,
                      filename TEXT,mime TEXT,kind TEXT,expected_size INTEGER,expected_sha TEXT,file_id TEXT,size INTEGER,sha256 TEXT,error TEXT,client TEXT,created REAL,updated REAL)''')
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS imports_key ON imports(source,asset_id,idem)')
        db.execute("UPDATE imports SET state='failed',error='SOURCE_UNAVAILABLE',updated=? WHERE state IN ('pending','downloading','verifying') AND created<?",(self.clock(),self.clock()-REQUEST_TTL))
        db.execute('DELETE FROM imports WHERE created<?',(self.clock()-7*86400,))
        db.commit()
        if os.name!='nt':
            os.chmod(self.path,0o600)
        return db

    def _row(self,identifier):
        db=self._db()
        try:
            row=db.execute('SELECT * FROM imports WHERE id=?',(identifier,)).fetchone()
        finally:
            db.close()
        return dict(row) if row else None

    def _set(self,identifier,**fields):
        db=self._db()
        try:
            db.execute('UPDATE imports SET '+','.join(k+'=?' for k in fields)+',updated=? WHERE id=?',(*fields.values(),self.clock(),identifier))
            db.commit()
        finally:
            db.close()

    def request(self,source,asset_id,filename,expected_size,expected_sha,idem,client):
        db=self._db()
        try:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM imports WHERE source=? AND asset_id=? AND idem=?',(source,asset_id,idem)).fetchone()
            if row is not None:
                row=dict(row)
                stale=row['state']=='ready' and not self._file_alive(row['file_id'])
                retry=row['state']=='failed' and row['error'] in ('SOURCE_UNAVAILABLE','TRANSFER_INCOMPLETE')
                if stale or retry:
                    db.execute("UPDATE imports SET state='pending',error=NULL,file_id=NULL,created=?,updated=? WHERE id=?",(self.clock(),self.clock(),row['id']))
                    row.update(state='pending',error=None,file_id=None)
                db.commit()
                return row
            identifier='imp_'+secrets.token_urlsafe(18)
            db.execute('INSERT INTO imports(id,source,asset_id,idem,state,filename,expected_size,expected_sha,client,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (identifier,source,asset_id,idem,'pending',filename,expected_size,expected_sha,client,self.clock(),self.clock()))
            db.commit()
        finally:
            db.close()
        LOG.warning('MEDIA import=%s source=%s state=pending',identifier,source)
        return self._row(identifier)

    def _file_alive(self,file_id):
        try:
            self.files.metadata(file_id)
            return True
        except Exception:
            return False

    def pending(self,source):
        db=self._db()
        try:
            rows=db.execute("SELECT id,asset_id,filename,expected_size,expected_sha FROM imports WHERE source=? AND state='pending' ORDER BY created LIMIT 10",(source,)).fetchall()
        finally:
            db.close()
        return [{'import_id':r['id'],'asset_id':r['asset_id'],'filename':r['filename'],'expected_size':r['expected_size'],'expected_sha256':r['expected_sha']} for r in rows]

    def fail(self,identifier,code):
        self._set(identifier,state='failed',error=code)
        LOG.warning('MEDIA import=%s state=failed code=%s',identifier,code)

    def view(self,row):
        if row is None:
            return {'success':False,'error':'NOT_FOUND','message':'No such import. Start it with telegram_import_grok_media.'}
        result={'success':row['state']=='ready','import_id':row['id'],'correlation_id':row['id'],'state':row['state'],'asset_id':row['asset_id'],'source':row['source']}
        if row['state']=='ready':
            result.update({'file_id':row['file_id'],'filename':row['filename'],'mime_type':row['mime'],'media_kind':row['kind'],'size_bytes':row['size'],'sha256':row['sha256'],
                           'ready_for_send':True,
                           'next_step':'Show the user the recipient, file and caption, then call telegram_prepare_send (chat_id null = Saved Messages) with media=[{"file_id": "'+row['file_id']+'", "kind": "'+row['kind']+'", "original": false}] and execute only after approval.'})
        elif row['state']=='failed':
            result.update({'error':row['error'],'message':FAILURE_TEXT.get(row['error'],'The transfer failed; nothing was sent.')})
        else:
            result['message']='Waiting for the Grok computer to deliver the original file. Ask for telegram_import_status in a few seconds; do not start a second import.'
        return result

    # ---- HTTP side used by the owner's computer ---------------------------------------------------------------

    async def http_requests(self,request):
        if not self.authorised(request):
            return JSONResponse({'error':'TRANSFER_UNAUTHORIZED'},status_code=401,headers={'Cache-Control':'no-store'})
        source=request.query_params.get('source','grok')
        return JSONResponse({'requests':self.pending(source)},headers={'Cache-Control':'no-store'})

    async def http_fail(self,request):
        if not self.authorised(request):
            return JSONResponse({'error':'TRANSFER_UNAUTHORIZED'},status_code=401)
        identifier=request.path_params['import_id']
        row=self._row(identifier) if IMPORT_ID.fullmatch(identifier) else None
        if row is None or row['state'] not in ('pending','downloading'):
            return JSONResponse({'error':'not_pending'},status_code=409)
        try:
            body=json.loads(await request.body() or b'{}')
        except ValueError:
            body={}
        code=body.get('code') if body.get('code') in ('SOURCE_EXPIRED','SOURCE_UNAVAILABLE','INVALID_MEDIA_TYPE','FILE_TOO_LARGE') else 'SOURCE_UNAVAILABLE'
        self.fail(identifier,code)
        return JSONResponse({'ok':True})

    async def http_upload(self,request):
        if not self.authorised(request):
            return JSONResponse({'error':'TRANSFER_UNAUTHORIZED'},status_code=401)
        identifier=request.path_params['import_id']
        row=self._row(identifier) if IMPORT_ID.fullmatch(identifier) else None
        if row is None or row['state'] not in ('pending','downloading'):
            return JSONResponse({'error':'not_pending'},status_code=409)
        try:
            length=int(request.headers.get('content-length',''))
        except ValueError:
            return JSONResponse({'error':'TRANSFER_INCOMPLETE','message':'Content-Length is required.'},status_code=411)
        declared=request.headers.get('x-content-sha256','').lower()
        if not re.fullmatch(r'[0-9a-f]{64}',declared):
            return JSONResponse({'error':'CHECKSUM_MISMATCH','message':'X-Content-SHA256 is required.'},status_code=400)
        if length<=0 or length>self.files.max_bytes:
            self.fail(identifier,'FILE_TOO_LARGE')
            return JSONResponse({'error':'FILE_TOO_LARGE'},status_code=413)
        if (row['expected_size'] and row['expected_size']!=length) or (row['expected_sha'] and row['expected_sha']!=declared):
            self.fail(identifier,'CHECKSUM_MISMATCH')
            return JSONResponse({'error':'CHECKSUM_MISMATCH'},status_code=409)
        self._set(identifier,state='downloading')
        try:
            async with self.files._lock:
                file_id=self.files._reserve(row['filename'] or 'grok-media','application/octet-stream',length,declared)
        except BackendError as error:
            self.fail(identifier,'FILE_TOO_LARGE' if error.code in ('file_too_large','file_quota_exceeded') else 'TRANSFER_INCOMPLETE')
            return JSONResponse({'error':error.code},status_code=413)
        target=self.files.path(file_id)
        digest,size,header=hashlib.sha256(),0,b''
        try:
            with target.open('wb') as stream:
                async for chunk in request.stream():
                    size+=len(chunk)
                    if size>length:
                        raise ValueError('TRANSFER_INCOMPLETE')
                    if len(header)<32:
                        header+=chunk[:32-len(header)]
                    digest.update(chunk)
                    stream.write(chunk)
            if size!=length:
                raise ValueError('TRANSFER_INCOMPLETE')
            self._set(identifier,state='verifying')
            if digest.hexdigest()!=declared:
                raise ValueError('CHECKSUM_MISMATCH')
            kind=sniff(header)
            if kind is None:
                raise ValueError('INVALID_MEDIA_TYPE')
            mime,media_kind,extension=kind
            stem=re.sub(r'[^\w.() -]','_',Path(row['filename'] or 'grok-media').stem)[:80] or 'grok-media'
            filename=stem+extension
            with self.files._db() as db:
                db.execute("UPDATE files SET filename=?,mime=?,size=?,received=?,sha256=?,state='ready',expires=? WHERE id=?",
                           (filename,mime,size,size,declared,self.files.clock()+86400,file_id))
            db.close()
            self._set(identifier,state='ready',file_id=file_id,filename=filename,mime=mime,kind=media_kind,size=size,sha256=declared)
            LOG.warning('MEDIA import=%s state=ready kind=%s size=%s',identifier,media_kind,size)
            return JSONResponse({'ok':True,'import_id':identifier,'file_id':file_id,'sha256':declared,'size_bytes':size})
        except Exception as error:
            # Includes a dropped connection mid-stream: the partial file is removed and the import is marked failed.
            code=str(error) if isinstance(error,ValueError) and str(error) in FAILURE_TEXT else 'TRANSFER_INCOMPLETE'
            target.unlink(missing_ok=True)
            with self.files._db() as db:
                db.execute('DELETE FROM files WHERE id=?',(file_id,))
            db.close()
            self.fail(identifier,code)
            return JSONResponse({'error':code},status_code=400 if code!='TRANSFER_INCOMPLETE' else 409)

    @property
    def routes(self):
        return [Route('/media-bridge/requests',self.http_requests,methods=['GET']),
                Route('/media-bridge/upload/{import_id}',self.http_upload,methods=['PUT']),
                Route('/media-bridge/fail/{import_id}',self.http_fail,methods=['POST'])]


def register_media_import(mcp,backend,call):
    files=backend.files
    data=files.root.parent
    store=MediaImports(files,data/'media_imports.sqlite',os.environ.get('TG_MEDIA_BRIDGE_TOKEN_FILE',str(data/'media-bridge-token')),files.clock)
    backend.media_imports=store
    # Staging only, like telegram_begin_upload: nothing reaches Telegram until a reviewed send.
    act=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False)
    read=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=True)
    read_meta={'securitySchemes':[{'type':'oauth2','scopes':['telegram.read']}]}

    async def wait(identifier,seconds):
        deadline=time.monotonic()+seconds
        while True:
            row=store._row(identifier)
            if row is None or row['state'] in ('ready','failed') or time.monotonic()>=deadline:
                return store.view(row)
            await asyncio.sleep(1)

    @mcp.tool(name='telegram_import_grok_media',annotations=act,meta=read_meta)
    async def import_grok_media(asset_id:Annotated[str,Field(min_length=1,max_length=128)],filename:Annotated[str,Field(max_length=120)]|None=None,mime_type:str|None=None,
                                expected_size:Annotated[int,Field(gt=0)]|None=None,expected_sha256:Annotated[str,Field(pattern=r'^[0-9a-fA-F]{64}$')]|None=None,
                                idempotency_key:Annotated[str,Field(max_length=64)]|None=None,wait_s:Annotated[int,Field(ge=0,le=55)]=45)->dict[str,Any]:
        """Bring a media file that Grok Imagine (the owner's Chrome bridge) already downloaded into Telegram staging, with NO manual download: pass the asset_id from grok_browser_download_status (its media.asset_id). The owner's computer delivers the original bytes; size, SHA-256 and real type (MP4, JPEG, PNG, WebP) are checked. Returns a file_id ready for telegram_prepare_send (kind video for MP4, photo for images); nothing is sent to Telegram here. Repeating the call with the same asset_id and idempotency_key returns the same import instead of a copy. If state is still pending, call telegram_import_status later; never start a duplicate."""
        if not ASSET.fullmatch(asset_id):
            return {'success':False,'error':'INVALID_INPUT','message':'asset_id must be the media.asset_id returned by the Grok bridge.'}
        if store.token() is None:
            return {'success':False,'error':'TRANSFER_UNAUTHORIZED','message':FAILURE_TEXT['TRANSFER_UNAUTHORIZED']+' The server owner must install the bridge token.'}
        from request_context import current_client
        row=store.request('grok',asset_id,filename,expected_size,expected_sha256.lower() if expected_sha256 else None,idempotency_key or 'default',current_client.get())
        return await wait(row['id'],wait_s)

    @mcp.tool(name='telegram_import_status',annotations=read,meta=read_meta)
    async def import_status(import_id:Annotated[str,Field(min_length=4,max_length=48)],wait_s:Annotated[int,Field(ge=0,le=55)]=0)->dict[str,Any]:
        """Status of a media import (pending, downloading, verifying, ready or failed) with the file_id once ready. wait_s waits up to 55 seconds for it to finish."""
        if not IMPORT_ID.fullmatch(import_id):
            return store.view(None)
        return await wait(import_id,wait_s)

    return store.routes
