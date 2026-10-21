"""Telegram chat folders: list, inspect, create, rename, delete, add and remove chats (prepare -> preview -> execute)."""
from __future__ import annotations

from telegram_backend import BackendError,_operation,_text
from bot_interaction import UNTRUSTED

OPERATIONS=('create','rename','delete','add_chats','remove_chats')


def _tl():
    from telethon.tl import functions,types
    from telethon import utils
    return functions,types,utils


def _folder_title(folder):
    title=getattr(folder,'title',None)
    return getattr(title,'text',title) or ''


class Folders:
    """Mixed into ExtendedBackend."""

    async def _filters(self):
        f,t,_=_tl()
        client=await self._ready()
        found=await client(f.messages.GetDialogFiltersRequest())
        items=getattr(found,'filters',found)
        return [item for item in items if type(item).__name__ in ('DialogFilter','DialogFilterChatlist')]

    @staticmethod
    def _one_id(peer):
        _,_,u=_tl()
        try:
            return u.get_peer_id(peer)
        except Exception:
            return None

    @classmethod
    def _peer_ids(cls,folder):
        return [identifier for identifier in (cls._one_id(peer) for peer in getattr(folder,'include_peers',None) or []) if identifier is not None]

    @staticmethod
    def _find_folder(filters,reference):
        if isinstance(reference,bool) or reference is None:
            raise BackendError('invalid_input','Select a folder by name or id.')
        if isinstance(reference,int) or (isinstance(reference,str) and reference.strip().isdigit()):
            found=[item for item in filters if item.id==int(reference)]
        else:
            wanted=str(reference).strip().casefold()
            found=[item for item in filters if _folder_title(item).strip().casefold()==wanted]
        if not found:
            raise BackendError('folder_not_found','No folder with this name or id.',diagnostics={'folders':[{'id':item.id,'title':_folder_title(item)} for item in filters]})
        if len(found)>1:
            raise BackendError('ambiguous_folder','Several folders have this name; use the id.',diagnostics={'folders':[{'id':item.id,'title':_folder_title(item)} for item in found]})
        return found[0]

    async def _folder_chat_ids(self,reference):
        return set(self._peer_ids(self._find_folder(await self._filters(),reference)))

    @_operation
    async def list_folders(self):
        folders=[]
        for item in await self._filters():
            ids=self._peer_ids(item)
            rules=[name for name in ('contacts','non_contacts','groups','broadcasts','bots') if getattr(item,name,False)]
            folders.append({'id':item.id,'title':_folder_title(item),'emoticon':getattr(item,'emoticon',None),'chat_count':len(ids),
                            'shared_folder':type(item).__name__=='DialogFilterChatlist','also_includes_by_rule':rules})
        return {'folders':folders,'count':len(folders),'note':'Telegram limits the number of folders and chats per folder (more with Premium).'}

    @_operation
    async def get_folder(self,folder):
        item=self._find_folder(await self._filters(),folder)
        dialogs=await self._dialog_map()
        chats=[]
        for chat_id in self._peer_ids(item):
            dialog=dialogs.get(chat_id)
            chats.append(self._row(dialog.entity,dialog) if dialog else {'chat_id':chat_id,'title':None,'note':'not in your chat list'})
            if dialog:
                chats[-1]['chat_id']=chat_id
        return {'folder':{'id':item.id,'title':_folder_title(item),'shared_folder':type(item).__name__=='DialogFilterChatlist'},'chats':chats,'count':len(chats),'data_trust':UNTRUSTED}

    @_operation
    async def prepare_folder_change(self,operation,folder=None,title=None,chat_ids=None,emoticon=None):
        if operation not in OPERATIONS:
            raise BackendError('invalid_input','operation must be one of: '+', '.join(OPERATIONS)+'.')
        filters=await self._filters()
        dialogs=await self._dialog_map()
        existing=self._find_folder(filters,folder) if operation!='create' else None
        if existing is not None and type(existing).__name__=='DialogFilterChatlist':
            raise BackendError('unsupported_operation','Shared folders (links to join a folder) cannot be changed here; change them in Telegram.')
        chat_ids=list(dict.fromkeys(chat_ids or []))
        for chat_id in chat_ids:
            if isinstance(chat_id,bool) or not isinstance(chat_id,int):
                raise BackendError('invalid_input','chat_ids must be numeric chat IDs.')
            if chat_id not in dialogs:
                raise BackendError('chat_not_found','Chat '+str(chat_id)+' is not in your chat list; join it first.')
        if operation in ('create','rename'):
            name=_text(title,32,'title').strip()
            clash=[item for item in filters if _folder_title(item).strip().casefold()==name.casefold() and (existing is None or item.id!=existing.id)]
            if clash:
                raise BackendError('folder_exists','A folder with this name already exists.',diagnostics={'folder_id':clash[0].id})
            if existing is not None and name==_folder_title(existing).strip():
                raise BackendError('no_change','The folder already has this name.')
        if operation in ('add_chats','remove_chats') and not chat_ids:
            raise BackendError('invalid_input','Pass at least one chat ID.')
        before=self._peer_ids(existing) if existing is not None else []
        if operation=='create':
            after=chat_ids
        elif operation=='add_chats':
            after=before+[chat_id for chat_id in chat_ids if chat_id not in before]
            if after==before:
                raise BackendError('no_change','All these chats are already in the folder.')
        elif operation=='remove_chats':
            missing=[chat_id for chat_id in chat_ids if chat_id not in before]
            if len(missing)==len(chat_ids):
                raise BackendError('no_change','None of these chats is in the folder.')
            after=[chat_id for chat_id in before if chat_id not in chat_ids]
        elif operation=='delete':
            after=[]
        else:
            after=before
        name_of=lambda chat_id:(dialogs[chat_id].name if chat_id in dialogs else None)
        preview={'action':'folder_change','operation':operation,'folder':{'id':existing.id if existing is not None else None,'title':_folder_title(existing) if existing is not None else (title or None)},
                 'new_title':title if operation in ('create','rename') else None,
                 'chats_before':[{'chat_id':i,'title':name_of(i)} for i in before],'chats_after':[{'chat_id':i,'title':name_of(i)} for i in after],
                 'added':[{'chat_id':i,'title':name_of(i)} for i in after if i not in before],'removed':[{'chat_id':i,'title':name_of(i)} for i in before if i not in after],
                 'destructive':operation in ('delete','remove_chats'),'requires_manage_scope':True,
                 'note':'Chats themselves are not touched; only the folder changes.' if operation!='delete' else 'The folder is deleted; the chats stay in your chat list.','data_trust':UNTRUSTED}
        payload={'action':'folder_change','operation':operation,'folder_id':existing.id if existing is not None else None,'title':title,'chat_ids':chat_ids,'emoticon':emoticon}
        return self.actions.prepare(payload,preview)

    async def _folder_input_peers(self,chat_ids):
        _,_,u=_tl()
        dialogs=await self._dialog_map()
        peers=[]
        for chat_id in chat_ids:
            if chat_id not in dialogs:
                raise BackendError('chat_not_found','Chat '+str(chat_id)+' is no longer in your chat list.')
            peers.append(u.get_input_peer(dialogs[chat_id].entity))
        return peers

    @staticmethod
    def _rebuilt(source,identifier,title,include,emoticon=None):
        _,t,_=_tl()
        keep={name:getattr(source,name,False) for name in ('contacts','non_contacts','groups','broadcasts','bots','exclude_muted','exclude_read','exclude_archived')} if source is not None else {}
        pinned=[peer for peer in (getattr(source,'pinned_peers',None) or [])] if source is not None else []
        pinned=[peer for peer in pinned if peer in include]
        return t.DialogFilter(id=identifier,title=t.TextWithEntities(text=title,entities=[]),pinned_peers=pinned,include_peers=include,
                              exclude_peers=[peer for peer in (getattr(source,'exclude_peers',None) or []) if peer not in include] if source is not None else [],
                              emoticon=emoticon if emoticon is not None else (getattr(source,'emoticon',None) if source is not None else None),
                              color=getattr(source,'color',None) if source is not None else None,**keep)

    async def _folder_apply(self,payload):
        f,t,u=_tl()
        client=self._client
        filters=await self._filters()
        operation=payload['operation']
        try:
            if operation=='create':
                identifier=max([item.id for item in filters]+[1])+1
                folder=self._rebuilt(None,identifier,payload['title'].strip(),await self._folder_input_peers(payload['chat_ids']),payload.get('emoticon'))
                await client(f.messages.UpdateDialogFilterRequest(id=identifier,filter=folder))
                return {'changed':True,'operation':operation,'folder':{'id':identifier,'title':payload['title'].strip()},'chat_count':len(payload['chat_ids'])}
            existing=[item for item in filters if item.id==payload['folder_id']]
            if not existing:
                raise BackendError('folder_not_found','The folder no longer exists. Nothing was changed.')
            existing=existing[0]
            if operation=='delete':
                await client(f.messages.UpdateDialogFilterRequest(id=existing.id,filter=None))
                return {'changed':True,'operation':operation,'folder':{'id':existing.id,'title':_folder_title(existing)}}
            current=list(getattr(existing,'include_peers',None) or [])
            ids=self._peer_ids(existing)
            if operation=='rename':
                new_title,include=payload['title'].strip(),current
            elif operation=='add_chats':
                new_title,include=_folder_title(existing),current+[peer for peer,chat_id in zip(await self._folder_input_peers(payload['chat_ids']),payload['chat_ids']) if chat_id not in ids]
            else:
                drop=set(payload['chat_ids'])
                new_title,include=_folder_title(existing),[peer for peer in current if self._one_id(peer) not in drop]
            await client(f.messages.UpdateDialogFilterRequest(id=existing.id,filter=self._rebuilt(existing,existing.id,new_title,include)))
            return {'changed':True,'operation':operation,'folder':{'id':existing.id,'title':new_title},'chat_count':len(include)}
        except BackendError:
            raise
        except Exception as error:
            if 'Filter' in type(error).__name__ or 'DIALOG_FILTER' in str(getattr(error,'message','')) or 'FILTER' in str(getattr(error,'message','')):
                failure=BackendError('folder_limit','Telegram refused the folder change (too many folders or chats in a folder, or an invalid name). Nothing was changed.')
                failure.retry_safe=True
                raise failure from None
            raise

    async def _folders_drop_peers(self,chat_ids):
        """Best effort after leaving chats: remove them from every folder that lists them."""
        f,t,u=_tl()
        dropped=0
        try:
            for item in await self._filters():
                if type(item).__name__!='DialogFilter':
                    continue
                keep,gone=[],0
                for peer in getattr(item,'include_peers',None) or []:
                    if self._one_id(peer) in chat_ids:
                        gone+=1
                    else:
                        keep.append(peer)
                if gone:
                    await self._client(f.messages.UpdateDialogFilterRequest(id=item.id,filter=self._rebuilt(item,item.id,_folder_title(item),keep)))
                    dropped+=gone
        except Exception:
            return {'folder_cleanup':'failed','removed_from_folders':dropped}
        return {'folder_cleanup':'done','removed_from_folders':dropped}
