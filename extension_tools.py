"""Typed MCP v2 tools and actual media/resource content, no invented file handles."""
from __future__ import annotations
import asyncio
import base64
import json
from pathlib import Path
from typing import Annotated,Any,Literal
from urllib.parse import urlsplit
from pydantic import Field
from mcp.types import CallToolResult,TextContent,ImageContent,AudioContent,ResourceLink,EmbeddedResource,BlobResourceContents,ToolAnnotations

from tool_models import OpenAIFile,MediaItem,MessageOptions,ArticleBlock,ContactChange,GroupChange,StoryOptions
from telegram_backend import BackendError
from file_downloads import DownloadStore
from upload_routes import UploadRoutes
from media_processing import image_preview,read_document,probe_media,transcribe,synthesize,audio_preview,video_frames


def register_extensions(mcp,backend,oauth,scopes,call):
    files=backend.files
    read=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=True)
    write=ToolAnnotations(readOnlyHint=False,destructiveHint=True,openWorldHint=True)
    read_meta={'securitySchemes':[{'type':'oauth2','scopes':['telegram.read']}]}
    write_meta={'securitySchemes':[{'type':'oauth2','scopes':['telegram.read','telegram.send']}]}
    local=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False)
    viewer_uri='ui://telegram/original-media-v1.html'
    viewer_meta={**read_meta,'ui':{'resourceUri':viewer_uri},'openai/outputTemplate':viewer_uri}
    viewer_origin=urlsplit(oauth.public_url).scheme+'://'+urlsplit(oauth.public_url).netloc
    viewer_html=r'''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><style>
    :root{color-scheme:light dark}body{margin:0;padding:16px;font:15px/1.5 system-ui;background:var(--color-background-primary,Canvas);color:var(--color-text-primary,CanvasText)}
    main{max-height:680px;overflow:auto}h1{font-size:22px}h2{font-size:18px}p{white-space:pre-wrap}figure{margin:16px 0}img{display:block;max-width:100%;max-height:560px;margin:auto;object-fit:contain;border-radius:10px}figcaption{font-size:12px;opacity:.7;margin-top:8px}.status{font-size:13px;opacity:.8}
    </style><main id="viewer" aria-live="polite"><p>Получаю предпросмотр…</p></main><script>
    const BASE=__BASE__, root=document.getElementById('viewer'), pending=new Map();let sequence=0;
    function note(method,params){parent.postMessage({jsonrpc:'2.0',method,params},'*')}
    let lastSize='';function fit(){const height=Math.min(720,Math.ceil(root.getBoundingClientRect().height)+32),width=document.body.clientWidth,key=width+':'+height;if(key!==lastSize){lastSize=key;note('ui/notifications/size-changed',{height,width})}}
    function request(method,params){const id=++sequence;return new Promise((resolve,reject)=>{const timer=setTimeout(()=>{pending.delete(id);reject(new Error('host_unavailable'))},8000);pending.set(id,{resolve,reject,timer});parent.postMessage({jsonrpc:'2.0',id,method,params},'*')})}
    function text(tag,value){const el=document.createElement(tag);el.textContent=String(value??'');root.append(el);return el}
    function image(info){try{const url=new URL(info.download_url);if(url.origin!==new URL(BASE).origin||!url.href.startsWith(BASE+'/file-downloads/'))throw new Error('invalid_original');const figure=document.createElement('figure'),img=document.createElement('img'),caption=document.createElement('figcaption');img.src=url.href;img.alt=info.filename||'Оригинальное изображение';img.referrerPolicy='no-referrer';caption.textContent=info.filename||'Оригинальное изображение';img.onerror=()=>{img.remove();caption.textContent='Не удалось загрузить оригинал. Запросите фотографию ещё раз в чате.'};figure.append(img,caption);root.append(figure)}catch(e){text('p','Оригинал недоступен для предпросмотра.')}}
    function render(value){const data=value?.structuredContent??value;if(!data||typeof data!=='object')return;root.replaceChildren();if(data.error){text('p',data.message||'Не удалось получить файл.');return}if(data.preview){const p=data.preview;text('p','Получатель: '+(p.recipient?.title||'' )).className='status';text('h1',p.title);const originals=new Map((data.preview_images||[]).map(i=>[i.file_id,i]));for(const block of p.blocks||[]){if(block.type==='photo'){const info=originals.get(block.file_id);if(info)image(info);else text('p','Оригинальная фотография недоступна.')}else if(block.type==='list'){const list=document.createElement('ul');for(const item of block.items||[]){const li=document.createElement('li');li.textContent=item;list.append(li)}root.append(list)}else if(block.type==='divider'){root.append(document.createElement('hr'))}else{text(block.type==='heading'?'h2':block.type==='quote'?'blockquote':'p',block.text||(block.runs||[]).map(r=>r.text).join(''))}}text('p',data.state==='done'?'Опубликованная статья.':'Черновик. Отправка выполняется отдельно после подтверждения.').className='status'}else if(data.mime_type?.startsWith('image/')){text('h2','Оригинал из Telegram');image(data)}else{text('p',data.filename||'Предпросмотр недоступен.')}note('ui/notifications/size-changed',{height:Math.min(720,document.body.scrollHeight),width:document.body.clientWidth})}
    addEventListener('message',event=>{if(event.source!==parent)return;const message=event.data;if(message?.jsonrpc!=='2.0')return;if(pending.has(message.id)){const entry=pending.get(message.id);pending.delete(message.id);clearTimeout(entry.timer);message.error?entry.reject(message.error):entry.resolve(message.result);return}if(message.method==='ui/notifications/tool-result')render(message.params);else if(message.method==='ui/resource-teardown'&&message.id!==undefined)parent.postMessage({jsonrpc:'2.0',id:message.id,result:{}},'*')});
    addEventListener('openai:set_globals',event=>render(event.detail?.globals?.toolOutput));
    request('ui/initialize',{appInfo:{name:'Telegram original viewer',version:'1.0.1'},appCapabilities:{availableDisplayModes:['inline']},protocolVersion:'2026-01-26'}).then(()=>{note('ui/notifications/initialized',{});new ResizeObserver(fit).observe(root);fit()}).catch(()=>{});
    if(window.openai?.toolOutput)render(window.openai.toolOutput);
    </script></html>'''.replace('__BASE__',json.dumps(oauth.public_url.rstrip('/')))

    @mcp.resource(viewer_uri,name='Telegram original media viewer',mime_type='text/html;profile=mcp-app',
                  meta={'ui':{'prefersBorder':True,'csp':{'resourceDomains':[viewer_origin],'connectDomains':[]}},
                        'openai/widgetDescription':'Shows the actual original Telegram photos or native article draft. Images are never searched or generated. Publication remains a separate confirmed tool call.',
                        'openai/widgetCSP':{'resource_domains':[viewer_origin],'connect_domains':[]}})
    async def original_viewer()->str:
        return viewer_html
    routes=UploadRoutes(files,oauth.public_url)
    downloads=DownloadStore(oauth.public_url,files.root,files.root/'links.sqlite',delete_expired_files=False,route_prefix='/file-downloads')

    def artifact(info,inline=False):
        path=files.verified_path(info['file_id'])
        link=downloads.issue(str(path),info['filename'])
        info={**info,'download_url':link,'download_expires_in_seconds':600}
        if info['mime_type'] in ('image/jpeg','image/png','image/webp'):
            info['display_markdown']=f"![Original Telegram image]({link})"
            info['display_policy']='Render this exact original using display_markdown. Never use image_query/image_group, internet illustrations or generated substitutes. If the client blocks inline display, say so and provide this original resource.'
        content=[TextContent(type='text',text=json.dumps(info,ensure_ascii=False)),
                 ResourceLink(type='resource_link',uri=link,name=info['filename'],mimeType=info['mime_type'],size=info['size_bytes'],description='Original bytes. Private link expires in ten minutes; refresh via telegram_get_file.')]
        if inline and info['mime_type'] in ('image/jpeg','image/png','image/webp'):
            try:
                data,preview=image_preview(path)
                info.update(preview)
                if preview['preview_uses_original_bytes'] and info['mime_type'] != preview['preview_mime_type']:
                    info['declared_mime_type'] = info['mime_type']
                    info['mime_type'] = preview['preview_mime_type']
                    content[-1] = ResourceLink(type='resource_link',uri=link,name=info['filename'],mimeType=info['mime_type'],size=info['size_bytes'],description='Original bytes. Private link expires in ten minutes; refresh via telegram_get_file.')
                content.insert(1,ImageContent(type='image',data=base64.b64encode(data).decode(),mimeType=preview['preview_mime_type']))
            except BackendError as error:
                info['inline_preview_error']=error.as_dict()
        elif inline and info['size_bytes']<=4*1024*1024:
            content.insert(1,EmbeddedResource(type='resource',resource=BlobResourceContents(uri=info['resource_uri'],mimeType=info['mime_type'],blob=base64.b64encode(path.read_bytes()).decode())))
        content[0]=TextContent(type='text',text=json.dumps(info,ensure_ascii=False))
        return CallToolResult(content=content,structuredContent=info,
                              _meta={'ui':{'resourceUri':viewer_uri},'openai/outputTemplate':viewer_uri})

    async def safe_artifact(operation,*args,**kwargs):
        try:
            info=await operation(*args,**kwargs)
            return artifact(info,True)
        except BackendError as error:
            return CallToolResult(isError=True,content=[TextContent(type='text',text=json.dumps(error.as_dict(),ensure_ascii=False))],structuredContent=error.as_dict())

    @mcp.tool(name='telegram_get_saved_messages',annotations=read,meta=read_meta)
    async def get_saved(limit:Annotated[int,Field(ge=1,le=100)]=50,offset_id:Annotated[int,Field(ge=0)]=0,query:str='',saved_dialog_id:int|None=None)->dict[str,Any]:
        """Read/search Saved Messages by the authenticated self ID, never by name. Empty query returns history; offset_id paginates. saved_dialog_id selects an available Saved section."""
        return await call(backend.get_saved_messages,limit,offset_id,query,saved_dialog_id)

    @mcp.tool(name='telegram_get_saved_chat',annotations=read,meta=read_meta)
    async def get_saved_chat()->dict[str,Any]:
        """Resolve OWN Saved Messages using the authenticated account ID without reading messages or searching a localized name. Use this when only a destination is needed."""
        return await call(backend.get_saved_chat)

    @mcp.tool(name='telegram_search_saved_messages',annotations=read,meta=read_meta)
    async def search_saved(query:str,limit:Annotated[int,Field(ge=1,le=100)]=50,offset_id:Annotated[int,Field(ge=0)]=0,saved_dialog_id:int|None=None)->dict[str,Any]:
        """Search OWN Saved Messages or one accessible Saved section, using inputPeerSelf and pagination. Never search by the name Saved Messages/Избранное."""
        return await call(backend.get_saved_messages,limit,offset_id,query,saved_dialog_id)

    @mcp.tool(name='telegram_capabilities',annotations=read,meta=read_meta)
    async def capabilities()->dict[str,Any]:
        """Describe this deployed server's real transport, feature/format/size limits and processor readiness. Client preview and native Telegram feature eligibility must be verified, never assumed."""
        import os,shutil
        from telethon.tl.alltlobjects import LAYER
        article_evidence=backend.article_publication_evidence()
        return {'version':'2.0.6','telegram_layer':LAYER,'saved_messages_by_self_id':True,'native_rich_articles':True,
                'native_article_api_implemented':True,'native_article_publication_verified':article_evidence['verified'],
                'native_article_acceptance_evidence':article_evidence,
                'article_workflow':'Use create_article_draft and preview_article for one native rich message with original photos between blocks, then publish_article after approval. Check acceptance evidence separately from API implementation. Report the actual publication error; never substitute an HTML file, album or message series without separate user approval.',
                'mcp_images':True,'mcp_file_resources':True,'chatgpt_file_params':True,'native_stories_api':True,
                'max_transfer_bytes':files.max_bytes,'staging_quota_bytes':files.quota,'file_lifetime_seconds':86400,
                'inline_binary_resource_limit_bytes':4*1024*1024,'draft_lifetime_seconds':600,
                'ffmpeg_available':bool(shutil.which('ffmpeg')),'legacy_office_available':bool(shutil.which('libreoffice') or shutil.which('soffice')),
                'local_transcription_ready':all((Path(os.environ.get('TG_WHISPER_MODEL','/not-configured'))/name).is_file() for name in ('model.bin','config.json','tokenizer.json')) and any((Path(os.environ.get('TG_WHISPER_MODEL','/not-configured'))/name).is_file() for name in ('vocabulary.txt','vocabulary.json')),
                'local_speech_synthesis_available':bool(shutil.which('espeak-ng')),
                'client_limits':'File params, resource download and native previews depend on the ChatGPT client. Video has resources/frames, not an invented MCP video block; scanned PDF OCR is not implemented.'}

    @mcp.tool(name='telegram_get_saved_sections',annotations=read,meta=read_meta)
    async def get_sections(limit:Annotated[int,Field(ge=1,le=100)]=30,offset_id:int=0,offset_date:str|None=None,offset_peer_id:int|None=None)->dict[str,Any]:
        """List Saved Messages sections and reaction tags using official Telegram API. Named tags require Telegram Premium."""
        return await call(backend.get_saved_sections,limit,offset_id,offset_date,offset_peer_id)

    @mcp.tool(name='telegram_find_users',annotations=read,meta=read_meta)
    async def find_users(query:str,limit:Annotated[int,Field(ge=1,le=50)]=20)->dict[str,Any]:
        """Find visible contacts/users by name, @username or accessible phone. Never bypass privacy or silently import a new contact."""
        return await call(backend.find_users,query,limit)

    @mcp.tool(name='telegram_get_chat',annotations=read,meta=read_meta)
    async def get_chat(chat_id:int)->dict[str,Any]:
        """Inspect the selected chat, forum status, accessible account permissions and restrictions."""
        return await call(backend.get_chat,chat_id)

    @mcp.tool(name='telegram_get_topics',annotations=read,meta=read_meta)
    async def topics(chat_id:int,query:str='',limit:Annotated[int,Field(ge=1,le=100)]=30,offset_id:int=0,offset_topic:int=0,offset_date:str|None=None)->dict[str,Any]:
        """Find/list accessible forum topic IDs and titles with pagination. Use topic_id in message options/search; never guess a topic name's ID."""
        return await call(backend.get_topics,chat_id,query,limit,offset_id,offset_topic,offset_date)

    @mcp.tool(name='telegram_get_album',annotations=read,meta=read_meta)
    async def get_album(chat_id:int,message_id:Annotated[int,Field(gt=0)],limit:Annotated[int,Field(ge=1,le=100)]=20)->dict[str,Any]:
        """Inspect all accessible neighboring messages in the selected message's album, preserving Telegram order. Fetch each attachment by its message_id."""
        return await call(backend.get_album,chat_id,message_id,limit)

    @mcp.tool(name='telegram_upload_file',annotations=read,meta={**read_meta,'openai/fileParams':['file']})
    async def upload_file(file:OpenAIFile)->dict[str,Any]:
        """Stage ORIGINAL bytes of a file uploaded/generated in this ChatGPT conversation. ChatGPT must pass a real file reference (download_url,file_id,mime_type,file_name). Does not send. Internal sandbox paths/file IDs alone cannot be downloaded."""
        return await call(files.upload_reference,file.model_dump())

    @mcp.tool(name='telegram_upload_files',annotations=read,meta={**read_meta,'openai/fileParams':['files']})
    async def upload_files(files:Annotated[list[OpenAIFile],Field(min_length=1,max_length=10)])->dict[str,Any]:
        """Stage up to ten actual ChatGPT file references in exact order. No Telegram send. Returns per-file successes/errors so a failed upload is never silently replaced."""
        results=[]
        for index,file in enumerate(files):
            results.append({'index':index,**await call(backend.files.upload_reference,file.model_dump())})
        return {'files':results,'all_uploaded':all('error' not in x for x in results)}

    @mcp.tool(name='telegram_begin_upload',annotations=local,meta=read_meta)
    async def begin_upload(filename:str,mime_type:str,size_bytes:Annotated[int,Field(gt=0,le=200*1024*1024)],sha256:str|None=None)->dict[str,Any]:
        """Safe fallback when the ChatGPT client cannot pass file bytes: issue a private browser upload URL or bounded base64 chunk session. Explain fallback difference; never pretend a missing reference was uploaded."""
        result=await call(files.begin,filename,mime_type,size_bytes,sha256)
        if 'error' not in result:
            result['private_upload_url']=oauth.public_url+'/upload/'+result['upload_id']
        return result

    @mcp.tool(name='telegram_create_text_file',annotations=local,meta=read_meta)
    async def create_text_file(filename:str,text:Annotated[str,Field(min_length=1,max_length=500000)],mime_type:str='text/plain')->dict[str,Any]:
        """Create a real UTF-8 text/Markdown/CSV/JSON file from complete known or newly authored text, ready for sending as a DOCUMENT. Do not use to replace or reconstruct an unavailable original binary attachment."""
        data=text.encode('utf-8')
        result=await call(files.begin,filename,mime_type,len(data))
        if 'error' in result:
            return result
        identifier=result['upload_id']
        for offset in range(0,len(data),512*1024):
            chunk=data[offset:offset+512*1024]
            result=await call(files.append,identifier,offset,base64.b64encode(chunk).decode(),offset+len(chunk)==len(data))
            if 'error' in result:
                return result
        return result

    @mcp.tool(name='telegram_upload_file_chunk',annotations=local,meta=read_meta)
    async def upload_chunk(upload_id:str,offset:Annotated[int,Field(ge=0)],base64_data:Annotated[str,Field(max_length=1024*1024)],final:bool=False)->dict[str,Any]:
        """Append known bytes to a private upload; offset must match received_bytes, final verifies size/SHA-256. Never base64-encode or invent the contents of a file unavailable to this client."""
        return await call(files.append,upload_id,offset,base64_data,final)

    @mcp.tool(name='telegram_get_attachment',annotations=read,meta=viewer_meta)
    async def get_attachment(chat_id:int,message_id:Annotated[int,Field(gt=0)])->CallToolResult:
        """Return selected Telegram attachment with actual MCP image content for photos, embedded resources for small files, metadata/SHA-256 and an original download resource. Larger originals use a safe expiring link; client preview support varies."""
        return await safe_artifact(backend.attachment,chat_id,message_id)

    @mcp.tool(name='telegram_get_file',annotations=read,meta=viewer_meta)
    async def get_file(file_id:str,inline:bool=True)->CallToolResult:
        """Read a privately staged original by file_id or refresh its download link. Returns actual image content when requested; does not claim client support for every binary format."""
        try:
            return artifact(files.metadata(file_id),inline)
        except BackendError as error:
            return CallToolResult(isError=True,content=[TextContent(type='text',text=json.dumps(error.as_dict()))],structuredContent=error.as_dict())

    @mcp.resource('telegram-file://{file_id}',name='Private staged file',mime_type='application/octet-stream')
    async def staged_resource(file_id:str)->bytes:
        """Authenticated MCP resource with original bytes; use get_file for actual MIME/filename. Embedded reads are bounded to 4 MiB."""
        info=files.metadata(file_id)
        if info['size_bytes']>4*1024*1024:
            raise BackendError('resource_too_large','Use get_file to obtain a bounded private download resource.')
        return files.verified_path(file_id).read_bytes()

    @mcp.tool(name='telegram_read_document',annotations=read,meta=read_meta)
    async def document(file_id:str,offset:Annotated[int,Field(ge=0)]=0,max_characters:Annotated[int,Field(ge=1,le=100000)]=40000)->dict[str,Any]:
        """Extract paginated text/cell values from staged XLS/XLSX/XLSM/CSV/ODS, DOC/DOCX/RTF/ODT, PPT/PPTX/PPSX/ODP, PDF and text. Archives return member listings; scanned PDFs need OCR. Does not execute document macros."""
        try:
            info=files.metadata(file_id)
            return await asyncio.to_thread(read_document,files.verified_path(file_id),info['filename'],offset,max_characters)
        except BackendError as error:
            return error.as_dict()

    @mcp.tool(name='telegram_media_metadata',annotations=read,meta=read_meta)
    async def metadata(file_id:str)->dict[str,Any]:
        """Inspect staged video, voice note, GIF/animation or audio duration/codecs/dimensions using a local bounded processor."""
        try:
            return {**files.metadata(file_id),**await asyncio.to_thread(probe_media,files.verified_path(file_id))}
        except BackendError as error:
            return error.as_dict()

    @mcp.tool(name='telegram_transcribe_voice',annotations=read,meta=read_meta)
    async def transcribe_voice(file_id:str,language:Literal['ru','en']|None='ru')->dict[str,Any]:
        """Transcribe actual staged voice/audio bytes locally (up to ten minutes), without sending audio to a paid API. Clearly returns unavailable if the owner has not provisioned a Whisper model."""
        try:
            return await asyncio.to_thread(transcribe,files.verified_path(file_id),language)
        except BackendError as error:
            return error.as_dict()

    @mcp.tool(name='telegram_get_audio',annotations=read,meta=read_meta)
    async def get_audio(file_id:str)->CallToolResult:
        """Return an actual MCP WAV audio preview (first 60 seconds, explicitly labeled), original file resource and metadata. Full transcription uses transcribe_voice; client audio rendering may vary."""
        try:
            result=artifact(files.metadata(file_id),False)
            data,meta=await asyncio.to_thread(audio_preview,files.verified_path(file_id))
            result.content.insert(1,AudioContent(type='audio',data=base64.b64encode(data).decode(),mimeType='audio/wav'))
            result.structured_content.update(meta)
            result.content[0]=TextContent(type='text',text=json.dumps(result.structured_content,ensure_ascii=False))
            return result
        except BackendError as error:
            return CallToolResult(isError=True,content=[TextContent(type='text',text=json.dumps(error.as_dict()))],structuredContent=error.as_dict())

    @mcp.tool(name='telegram_video_frames',annotations=read,meta=read_meta)
    async def frames(file_id:str,count:Annotated[int,Field(ge=1,le=6)]=4)->CallToolResult:
        """Return timestamped actual image frames from a staged video/GIF/video note plus its original resource. Frames are samples for visual analysis, not a claim of native full-video playback in ChatGPT."""
        try:
            result=artifact(files.metadata(file_id),False)
            samples,meta=await asyncio.to_thread(video_frames,files.verified_path(file_id),count)
            result.structured_content.update(meta)
            result.structured_content['sampled_timestamps_seconds']=[x['timestamp_seconds'] for x in samples]
            result.content[0]=TextContent(type='text',text=json.dumps(result.structured_content,ensure_ascii=False))
            for item in samples:
                result.content.extend([TextContent(type='text',text=f"Frame at {item['timestamp_seconds']:.2f} seconds"),ImageContent(type='image',data=base64.b64encode(item['data']).decode(),mimeType='image/jpeg')])
            return result
        except BackendError as error:
            return CallToolResult(isError=True,content=[TextContent(type='text',text=json.dumps(error.as_dict()))],structuredContent=error.as_dict())

    @mcp.tool(name='telegram_synthesize_voice',annotations=local,meta=read_meta)
    async def synthesize_voice(text:Annotated[str,Field(min_length=1,max_length=3000)],language:Literal['ru','en']='ru')->dict[str,Any]:
        """Create an OGG/Opus voice asset using local synthetic eSpeak speech (robotic voice, not voice cloning). Does not send it; prepare/send a reviewed voice attachment separately."""
        try:
            data=await asyncio.to_thread(synthesize,text,language)
            pending=await files.begin('speech.ogg','audio/ogg',len(data))
            for offset in range(0,len(data),512*1024):
                chunk=data[offset:offset+512*1024]
                result=await files.append(pending['upload_id'],offset,base64.b64encode(chunk).decode(),offset+len(chunk)==len(data))
            return result
        except BackendError as error:
            return error.as_dict()

    @mcp.tool(name='telegram_prepare_send',annotations=local,meta=read_meta)
    async def prepare_send(chat_id:int|None=None,text:str='',media:Annotated[list[MediaItem],Field(max_length=10)]|None=None,options:MessageOptions|None=None)->dict[str,Any]:
        """Prepare text/reply or file/photo/audio/video/GIF/sticker/album with formatting or scheduling. Null chat_id means OWN Saved Messages. Show full recipient/text/attachment order; this only prepares a single-use draft. original=true sends photos as documents to preserve bytes."""
        return await call(backend.prepare_send,chat_id,text,[x.model_dump() for x in media or []],options.model_dump() if options else None)

    @mcp.tool(name='telegram_send_to_saved_messages',annotations=local,meta=read_meta)
    async def saved_draft(text:str='',media:Annotated[list[MediaItem],Field(max_length=10)]|None=None)->dict[str,Any]:
        """Prepare text/files for OWN Saved Messages automatically by self identity, with no manual chat_id. Show preview and then execute the exact draft only after user confirmation."""
        return await call(backend.prepare_send,None,text,[x.model_dump() for x in media or []])

    @mcp.tool(name='telegram_send_file',annotations=local,meta=read_meta)
    async def send_file(file_id:str,chat_id:int|None=None,caption:str='',options:MessageOptions|None=None)->dict[str,Any]:
        """Prepare sending a staged original as an actual Telegram DOCUMENT, preserving filename/bytes; null target is Saved Messages. Execute only after exact preview confirmation."""
        return await call(backend.prepare_send,chat_id,caption,[MediaItem(file_id=file_id).model_dump()],options.model_dump() if options else None)

    @mcp.tool(name='telegram_send_media',annotations=local,meta=read_meta)
    async def send_media(item:MediaItem,chat_id:int|None=None,options:MessageOptions|None=None)->dict[str,Any]:
        """Prepare one photo/video/audio/voice/GIF/sticker/video-note from real staged bytes. original=false permits a native photo; original=true preserves photo bytes as a document. Does not send before confirmation."""
        return await call(backend.prepare_send,chat_id,'',[item.model_dump()],options.model_dump() if options else None)

    @mcp.tool(name='telegram_send_album',annotations=local,meta=read_meta)
    async def send_album(items:Annotated[list[MediaItem],Field(min_length=2,max_length=10)],chat_id:int|None=None,options:MessageOptions|None=None)->dict[str,Any]:
        """Prepare a supported album preserving exact item order and captions. This is an album, not a native article. Show every attachment before confirming execution."""
        return await call(backend.prepare_send,chat_id,'',[x.model_dump() for x in items],options.model_dump() if options else None)

    @mcp.tool(name='telegram_schedule_post',annotations=local,meta=read_meta)
    async def schedule(chat_id:int,text:str,schedule_at:str,media:Annotated[list[MediaItem],Field(max_length=10)]|None=None)->dict[str,Any]:
        """Prepare a scheduled Telegram text/media post with explicit ISO time/timezone. Show target, content and publication time; execution still requires confirmation."""
        return await call(backend.prepare_send,chat_id,text,[x.model_dump() for x in media or []],MessageOptions(schedule_at=schedule_at).model_dump())

    @mcp.tool(name='telegram_reply',annotations=local,meta=read_meta)
    async def reply(chat_id:int,message_id:Annotated[int,Field(gt=0)],text:str,options:MessageOptions|None=None)->dict[str,Any]:
        """Prepare a reply to one selected message; returns recipient and full text for confirmation, does not send."""
        return await call(backend.prepare_message_action,'reply',chat_id,[message_id],text,options=options.model_dump() if options else None)

    @mcp.tool(name='telegram_forward',annotations=local,meta=read_meta)
    async def forward(chat_id:int,message_ids:Annotated[list[int],Field(min_length=1,max_length=100)],target_chat_id:int|None=None)->dict[str,Any]:
        """Prepare a forward of selected messages. Null target means OWN Saved Messages. Protected content remains subject to Telegram restrictions; confirmation required."""
        return await call(backend.prepare_message_action,'forward',chat_id,message_ids,target_chat_id=target_chat_id)

    @mcp.tool(name='telegram_edit',annotations=local,meta=read_meta)
    async def edit(chat_id:int,message_id:int,text:str,options:MessageOptions|None=None)->dict[str,Any]:
        """Prepare editing one message: preview contains original and replacement text. Telegram decides edit eligibility; execute only after confirmation."""
        return await call(backend.prepare_message_action,'edit',chat_id,[message_id],text,options=options.model_dump() if options else None)

    @mcp.tool(name='telegram_delete',annotations=local,meta=read_meta)
    async def delete(chat_id:int,message_ids:Annotated[list[int],Field(min_length=1,max_length=100)],revoke:bool=True)->dict[str,Any]:
        """Prepare destructive deletion of selected messages, with an exact preview and revoke scope. Requires confirmation; does not delete yet."""
        return await call(backend.prepare_message_action,'delete',chat_id,message_ids,revoke=revoke)

    @mcp.tool(name='telegram_react',annotations=local,meta=read_meta)
    async def react(chat_id:int,message_id:int,emoji:str|None=None,custom_emoji_id:Annotated[int,Field(gt=0)]|None=None)->dict[str,Any]:
        """Prepare an emoji/custom-emoji reaction to a selected message; empty emoji clears reactions. Saved reactions serve as available tags. Execute after confirmation."""
        return await call(backend.prepare_message_action,'react',chat_id,[message_id],emoji=emoji,custom_emoji_id=custom_emoji_id)

    @mcp.tool(name='telegram_pin',annotations=local,meta=read_meta)
    async def pin(chat_id:int,message_id:int,unpin:bool=False)->dict[str,Any]:
        """Prepare pin/unpin of one selected message; Telegram administrator permissions apply."""
        return await call(backend.prepare_message_action,'unpin' if unpin else 'pin',chat_id,[message_id])

    @mcp.tool(name='telegram_execute_action',annotations=write,meta=write_meta)
    async def execute(draft_id:str,confirmation_hash:Annotated[str,Field(pattern=r'^[a-f0-9]{64}$')])->dict[str,Any]:
        """Execute ONLY an exact draft whose full recipient, text, files/order or destructive changes the user has explicitly approved. Never infer approval from draft preparation. Persistent replay guard prevents duplicates and forbids retry of uncertain outcomes."""
        try:
            needed=backend.required_scope(draft_id)
            if needed not in scopes.get():
                return {'error':'insufficient_scope','message':'Reconnect and authorize '+needed+' for this action.'}
            return await call(backend.execute_action,draft_id,confirmation_hash)
        except BackendError as error:
            return error.as_dict()

    @mcp.tool(name='telegram_preview_action',annotations=local,meta=read_meta)
    async def preview(draft_id:str)->dict[str,Any]:
        """Show the complete prepared message/article/Story/management action for review without execution."""
        try:
            return backend.preview_action(draft_id)
        except BackendError as error:
            return error.as_dict()

    @mcp.tool(name='telegram_execute_management_action',annotations=write,meta={'securitySchemes':[{'type':'oauth2','scopes':['telegram.read','telegram.manage']}]})
    async def execute_management(draft_id:str,confirmation_hash:Annotated[str,Field(pattern=r'^[a-f0-9]{64}$')])->dict[str,Any]:
        """Execute ONLY a contact/group/channel management draft explicitly approved after reviewing every change. Requires separate telegram.manage consent; cannot use message-sending permission to alter administrators or contacts."""
        try:
            if backend.required_scope(draft_id)!='telegram.manage':
                return {'error':'invalid_action','message':'Use telegram_execute_action for this message/media action.'}
            if 'telegram.manage' not in scopes.get():
                return {'error':'insufficient_scope','message':'Reconnect and authorize telegram.manage.'}
            return await call(backend.execute_action,draft_id,confirmation_hash)
        except BackendError as error:
            return error.as_dict()

    @mcp.tool(name='telegram_create_article_draft',annotations=local,meta=read_meta)
    async def article(title:str,blocks:Annotated[list[ArticleBlock],Field(min_length=1,max_length=200)],chat_id:int|None=None)->dict[str,Any]:
        """Prepare ONE native Telegram Rich Message/article with headings, paragraphs, links, lists and original uploaded photos BETWEEN blocks in exact order. Never substitute an album or message series. Null chat_id means Saved Messages. Publication requires a compatible Telegram API/client/account and explicit confirmation."""
        return await call(backend.create_article,title,[b.model_dump() for b in blocks],chat_id)

    @mcp.tool(name='telegram_update_article',annotations=local,meta=read_meta)
    async def update_article(draft_id:str,title:str,blocks:Annotated[list[ArticleBlock],Field(min_length=1,max_length=200)],chat_id:int|None=None)->dict[str,Any]:
        """Replace a still-unpublished article draft with newly reviewed blocks. Returns a new confirmation hash; the old draft cannot be published after this update."""
        try:
            row=backend.actions.inspect(draft_id)
            if row['state']!='prepared' or json.loads(row['preview']).get('action')!='publish_article':
                raise BackendError('draft_not_editable','Only a prepared unpublished article can be changed.')
            target=chat_id if chat_id is not None else json.loads(row['payload'])['chat_id']
            result=await call(backend.create_article,title,[b.model_dump() for b in blocks],target)
            if 'error' not in result:
                with backend.actions.db() as db:
                    db.execute("UPDATE actions SET state='superseded',payload='',expires=0 WHERE id=?",(draft_id,))
                db.close()
            return result
        except BackendError as error:
            return error.as_dict()

    @mcp.tool(name='telegram_preview_article',annotations=local,meta=viewer_meta)
    async def preview_article(draft_id:str)->CallToolResult:
        """Return the complete native article draft in exact text/photo block order, with real MCP image previews for the originally supplied images. Does not publish anything."""
        try:
            result=backend.preview_action(draft_id)
            preview=result['preview']
            if preview.get('action')!='publish_article':
                raise BackendError('invalid_action','Choose a native article draft.')
            content=[TextContent(type='text',text=preview['title'])]
            result['preview_images']=[]
            image_bytes=0
            for block in preview['blocks']:
                content.append(TextContent(type='text',text=json.dumps(block,ensure_ascii=False)))
                if block['type']=='photo':
                    original=artifact(files.metadata(block['file_id']),False)
                    result['preview_images'].append(original.structured_content)
                    data,meta=image_preview(files.verified_path(block['file_id']),original_limit=0)
                    if image_bytes+len(data)<=8*1024*1024:
                        content.append(ImageContent(type='image',data=base64.b64encode(data).decode(),mimeType=meta['preview_mime_type']))
                        image_bytes+=len(data)
                    else:
                        content.extend(original.content)
                        content.append(TextContent(type='text',text='Inline article preview byte budget reached; original photo remains available through this resource.'))
            result['block_count']=len(preview['blocks'])
            result['original_photo_count']=len(result['preview_images'])
            result['presentation']={'type':'mcp_app','resource_uri':viewer_uri,'rendering_owner':'host','model_can_observe_rendering':False}
            result['display_policy']='The attached MCP App is rendered by the host independently of assistant text. Tell the user the preview is attached. The model cannot inspect the host screen: never claim that the component failed to display merely because it is absent from the model-visible text. Report display failure only if the client explicitly reports it. Show only the exact originals from preview_images in block order. Never use image_query/image_group, internet illustrations or generated substitutes. Use block_count and original_photo_count rather than estimating counts.'
            content[0]=TextContent(type='text',text=json.dumps(result,ensure_ascii=False))
            return CallToolResult(content=content,structuredContent=result,
                                  _meta={'ui':{'resourceUri':viewer_uri},'openai/outputTemplate':viewer_uri})
        except BackendError as error:
            return CallToolResult(isError=True,content=[TextContent(type='text',text=json.dumps(error.as_dict()))],structuredContent=error.as_dict())

    @mcp.tool(name='telegram_publish_article',annotations=write,meta=write_meta)
    async def publish_article(draft_id:str,confirmation_hash:Annotated[str,Field(pattern=r'^[a-f0-9]{64}$')])->dict[str,Any]:
        """Publish ONE native article ONLY after explicit user approval of the exact recipient and full text/photo block sequence returned by preview_article. Never substitute an album or Telegraph silently."""
        try:
            if backend.preview_action(draft_id)['preview'].get('action')!='publish_article':
                raise BackendError('invalid_action','Choose an article draft.')
            if 'telegram.send' not in scopes.get():
                return {'error':'insufficient_scope','message':'Authorize telegram.send.'}
            return await call(backend.execute_action,draft_id,confirmation_hash)
        except BackendError as error:
            return error.as_dict()

    @mcp.tool(name='telegram_create_story',annotations=local,meta=read_meta)
    async def story(file_id:str,caption:str='',chat_id:int|None=None,options:StoryOptions|None=None)->dict[str,Any]:
        """Prepare a real PHOTO/VIDEO PROFILE STORY, not a chat video note. Default audience contacts, expiry 24h; preview includes privacy and media. Null chat_id is this personal profile. Publish only with explicit confirmation and account/channel eligibility."""
        return await call(backend.prepare_story,file_id,caption,chat_id,options.model_dump() if options else None)

    @mcp.tool(name='telegram_get_stories',annotations=read,meta=read_meta)
    async def stories(chat_id:int|None=None,archive:bool=False,offset_id:int=0,limit:Annotated[int,Field(ge=1,le=100)]=30)->dict[str,Any]:
        """Read own/selected peer Stories, or accessible archive and view counts. Null selects the authenticated personal profile."""
        return await call(backend.get_stories,chat_id,archive,offset_id,limit)

    @mcp.tool(name='telegram_publish_story',annotations=write,meta=write_meta)
    async def publish_story(draft_id:str,confirmation_hash:Annotated[str,Field(pattern=r'^[a-f0-9]{64}$')])->dict[str,Any]:
        """Publish a real profile/channel Story only after explicit confirmation of its exact image/video, caption, audience and lifetime. Returns actual API IDs or an explicit feature/rights error."""
        try:
            if backend.preview_action(draft_id)['preview'].get('action')!='publish_story':
                raise BackendError('invalid_action','Choose a Story draft.')
            if 'telegram.send' not in scopes.get():
                return {'error':'insufficient_scope','message':'Authorize telegram.send.'}
            return await call(backend.execute_action,draft_id,confirmation_hash)
        except BackendError as error:
            return error.as_dict()

    @mcp.tool(name='telegram_edit_story',annotations=local,meta=read_meta)
    async def edit_story(story_id:int,caption:str,chat_id:int|None=None,file_id:str|None=None)->dict[str,Any]:
        """Prepare editing an accessible own/channel Story's caption and optional replacement media. Preview contains original caption and changes; execute only after approval."""
        return await call(backend.prepare_story_edit,story_id,caption,chat_id,file_id)

    @mcp.tool(name='telegram_delete_story',annotations=local,meta=read_meta)
    async def delete_story(story_ids:Annotated[list[int],Field(min_length=1,max_length=100)],chat_id:int|None=None)->dict[str,Any]:
        """Prepare deletion of exact own/channel Story IDs; does not delete until the user confirms the preview."""
        return await call(backend.prepare_story_delete,story_ids,chat_id)

    @mcp.tool(name='telegram_manage_contacts',annotations=local,meta=read_meta)
    async def contacts(change:ContactChange)->dict[str,Any]:
        """Prepare adding/updating/deleting one contact. Visible phone/name are required to import/update; no privacy bypass. Execute requires explicit review and telegram.manage scope."""
        return await call(backend.prepare_contact,change.model_dump())

    @mcp.tool(name='telegram_manage_groups',annotations=local,meta=read_meta)
    async def groups(change:GroupChange,chat_id:int|None=None)->dict[str,Any]:
        """Prepare group/channel creation or exact title/description/member/admin/ban/topic changes, strictly within account rights. Preview and telegram.manage consent required before execution."""
        return await call(backend.prepare_group,change.model_dump(),chat_id)

    return routes.routes+downloads.routes
