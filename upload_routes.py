"""Owner-issued, expiring fallback for clients unable to pass uploaded file refs."""
import base64
import html
import json
import secrets
from starlette.responses import HTMLResponse,JSONResponse
from starlette.routing import Route
from urllib.parse import urlsplit

from file_store import ID
from telegram_backend import BackendError


class UploadRoutes:
    def __init__(self,files,public_url):
        self.files=files
        self.url=public_url.rstrip('/')
        parsed=urlsplit(self.url)
        self.origin=parsed.scheme+'://'+parsed.netloc

    def pending(self,identifier):
        if not ID.fullmatch(identifier):
            raise BackendError('upload_expired','Invalid private upload link.')
        with self.files._db() as db:
            row=db.execute('SELECT * FROM files WHERE id=?',(identifier,)).fetchone()
        db.close()
        if not row or row['state']!='uploading' or row['expires']<=self.files.clock():
            raise BackendError('upload_expired','The private upload link expired or was already consumed.')
        return row

    async def get(self,request):
        try:
            identifier=request.path_params['upload_id']
            row=self.pending(identifier)
            nonce=secrets.token_urlsafe(24)
            url=self.url+'/upload/'+identifier
            script="""document.getElementById('submit').onclick=async()=>{const f=document.getElementById('file').files[0];const out=document.getElementById('out');if(!f){out.textContent='Выберите файл';return;}if(f.size!==EXPECTED){out.textContent='Размер отличается от ожидаемого';return;}out.textContent='Загрузка…';try{const r=await fetch(URL,{method:'PUT',headers:{'Content-Type':'application/octet-stream'},body:f});const data=await r.json();out.textContent=JSON.stringify(data,null,2);}catch(e){out.textContent='Ошибка загрузки. Создайте новую ссылку в ChatGPT.';}};""".replace('EXPECTED',str(row['size'])).replace('URL',json.dumps(url))
            body=f'<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Личная загрузка Telegram</title><h1>Загрузить файл для Telegram</h1><p>{html.escape(row["filename"])} — {row["size"]} байт</p><p>Файл сохраняется приватно. Отправка в Telegram выполняется отдельно после просмотра черновика.</p><input id="file" type="file"><button id="submit">Загрузить</button><pre id="out"></pre><script nonce="{nonce}">{script}</script></html>'
            return HTMLResponse(body,headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Frame-Options':'DENY',
                'Content-Security-Policy':f"default-src 'none'; script-src 'nonce-{nonce}'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"})
        except BackendError as error:
            return JSONResponse(error.as_dict(),status_code=404)

    async def put(self,request):
        try:
            identifier=request.path_params['upload_id']
            row=self.pending(identifier)
            origin=request.headers.get('origin')
            if origin and origin!=self.origin:
                return JSONResponse({'error':'access_denied'},status_code=403)
            if request.headers.get('content-type','').split(';')[0]!='application/octet-stream':
                return JSONResponse({'error':'unsupported_content_type'},status_code=415)
            length=request.headers.get('content-length')
            if length and int(length)!=row['size']:
                raise BackendError('upload_size_mismatch','Selected file size differs from the declared size.')
            received=row['received']
            async for chunk in request.stream():
                for start in range(0,len(chunk),512*1024):
                    part=chunk[start:start+512*1024]
                    result=await self.files.append(identifier,received,base64.b64encode(part).decode(),False)
                    received=result['received_bytes']
            result=await self.files.append(identifier,received,'',True)
            return JSONResponse(result,headers={'Cache-Control':'no-store'})
        except BackendError as error:
            return JSONResponse(error.as_dict(),status_code=400)
        except (ValueError,TypeError):
            return JSONResponse({'error':'invalid_input'},status_code=400)

    @property
    def routes(self):
        return [Route('/upload/{upload_id}',self.get,methods=['GET']),Route('/upload/{upload_id}',self.put,methods=['PUT'])]
