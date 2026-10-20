"""MCP tools for bot rights, channel posts, inline mode and @BotFather (TZ-bot-interaction 4.4-4.8)."""
from __future__ import annotations
from typing import Annotated,Any,Literal
from pydantic import Field
from mcp.types import ToolAnnotations

from tool_models import LinkButton,CommandSpec,MessageOptions

Right=Literal['post_messages','edit_messages','delete_messages','pin_messages','invite_users','change_info','manage_topics','post_stories','edit_stories','delete_stories','ban_users','manage_call']


def register_admin_tools(mcp,backend,scopes,call):
    read=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=True)
    local=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False)
    read_meta={'securitySchemes':[{'type':'oauth2','scopes':['telegram.read']}]}

    @mcp.tool(name='telegram_list_admins',annotations=read,meta=read_meta)
    async def list_admins(chat_id:int,limit:Annotated[int,Field(ge=1,le=200)]=100)->dict[str,Any]:
        """List administrators of a channel or supergroup with their exact rights; bots are flagged. Use before changing a bot's rights."""
        return await call(backend.list_admins,chat_id,limit)

    @mcp.tool(name='telegram_prepare_add_bot_admin',annotations=local,meta=read_meta)
    async def add_bot_admin(chat_id:int,bot:int|str,rights:Annotated[list[Right],Field(min_length=1,max_length=12)],title:Annotated[str,Field(max_length=16)]|None=None)->dict[str,Any]:
        """Prepare adding a BOT as administrator with an explicit list of rights (pass the bot @username); title works only in supergroups. add_admins can never be granted. The preview shows was/now for every right. Execute with telegram_execute_management_action only after the user approved the exact list; needs telegram.manage. Rights of people are out of scope."""
        return await call(backend.prepare_add_bot_admin,chat_id,bot,rights,title)

    @mcp.tool(name='telegram_prepare_edit_bot_rights',annotations=local,meta=read_meta)
    async def edit_bot_rights(chat_id:int,bot:int|str,grant:list[Right]|None=None,revoke:list[Right]|None=None,title:Annotated[str,Field(max_length=16)]|None=None)->dict[str,Any]:
        """Prepare granting and/or revoking individual rights of a bot that is already an administrator. Preview shows was/now per right. Execute with telegram_execute_management_action after approval."""
        return await call(backend.prepare_edit_bot_rights,chat_id,bot,grant,revoke,title)

    @mcp.tool(name='telegram_prepare_demote_bot',annotations=local,meta=read_meta)
    async def demote_bot(chat_id:int,bot:int|str)->dict[str,Any]:
        """Prepare removing ALL administrator rights from a bot; it stays a member. Execute with telegram_execute_management_action after approval."""
        return await call(backend.prepare_demote_bot,chat_id,bot)

    @mcp.tool(name='telegram_prepare_remove_bot',annotations=local,meta=read_meta)
    async def remove_bot(chat_id:int,bot:int|str)->dict[str,Any]:
        """Prepare removing a bot from a group or channel entirely. Destructive; show the user the preview and execute with telegram_execute_management_action after approval."""
        return await call(backend.prepare_remove_bot,chat_id,bot)

    @mcp.tool(name='telegram_prepare_set_bot_title',annotations=local,meta=read_meta)
    async def set_bot_title(chat_id:int,bot:int|str,title:Annotated[str,Field(min_length=1,max_length=16)])->dict[str,Any]:
        """Prepare the custom administrator title (up to 16 characters) of a bot that is already an administrator of a SUPERGROUP. Channels have no custom titles (invalid_input)."""
        return await call(backend.prepare_set_bot_title,chat_id,bot,title)

    @mcp.tool(name='telegram_prepare_publish_post',annotations=local,meta=read_meta)
    async def publish_post(chat_id:int,text:Annotated[str,Field(max_length=4096)]='',file_id:Annotated[str,Field(min_length=32,max_length=43)]|None=None,
                           url_buttons:Annotated[list[LinkButton],Field(max_length=8)]|None=None,schedule_at:str|None=None,silent:bool=False,
                           parse_mode:Literal['plain','html','markdown']='plain')->dict[str,Any]:
        """Prepare a post in a CHANNEL as the channel: text, or a staged photo/video with caption (up to 1024 characters), optional link buttons, optional schedule_at (ISO time with timezone) and silent delivery. Telegram may refuse link buttons from a user account (buttons_not_supported, nothing is published). Execute with telegram_execute_action after the user approved the preview."""
        return await call(backend.prepare_publish_post,chat_id,text,file_id,[b.model_dump() for b in url_buttons or []],schedule_at,silent,parse_mode)

    @mcp.tool(name='telegram_prepare_edit_post_text',annotations=local,meta=read_meta)
    async def edit_post_text(chat_id:int,message_id:Annotated[int,Field(gt=0)],text:str,parse_mode:Literal['plain','html','markdown']='plain')->dict[str,Any]:
        """Prepare replacing the text or caption of a post in a channel (title, article number, description, price). The preview shows was/now with a word-level diff. A message a bot sent to a PRIVATE chat cannot be edited by the user: you get not_editable_by_user; use the bot's own buttons or commands then. Needs telegram.manage: execute with telegram_execute_management_action after approval."""
        return await call(backend.prepare_edit_post_text,chat_id,message_id,text,parse_mode)

    @mcp.tool(name='telegram_prepare_replace_post_media',annotations=local,meta=read_meta)
    async def replace_post_media(chat_id:int,message_id:Annotated[int,Field(gt=0)],file_id:Annotated[str,Field(min_length=32,max_length=43)],caption:Annotated[str,Field(max_length=1024)]|None=None)->dict[str,Any]:
        """Prepare replacing the photo or video of a channel post with a staged original file; the caption is kept unless a new one is given. Needs telegram.manage: execute with telegram_execute_management_action after approval."""
        return await call(backend.prepare_replace_post_media,chat_id,message_id,file_id,caption)

    @mcp.tool(name='telegram_prepare_delete_post',annotations=local,meta=read_meta)
    async def delete_post(chat_id:int,message_ids:Annotated[list[int],Field(min_length=1,max_length=100)])->dict[str,Any]:
        """Prepare deleting posts of a channel for everyone. The preview shows the full text of every post to be deleted; show it to the user. Irreversible. Needs telegram.manage: execute with telegram_execute_management_action after approval."""
        return await call(backend.prepare_delete_post,chat_id,message_ids)

    @mcp.tool(name='telegram_prepare_pin_post',annotations=local,meta=read_meta)
    async def pin_post(chat_id:int,message_id:Annotated[int,Field(gt=0)],unpin:bool=False)->dict[str,Any]:
        """Prepare pinning (or unpinning with unpin=true) a channel post. Needs telegram.manage: execute with telegram_execute_management_action after approval."""
        return await call(backend.prepare_pin_post,chat_id,message_id,unpin)

    @mcp.tool(name='telegram_prepare_unpin_post',annotations=local,meta=read_meta)
    async def unpin_post(chat_id:int,message_id:Annotated[int,Field(gt=0)])->dict[str,Any]:
        """Prepare unpinning a channel post (same as telegram_prepare_pin_post with unpin=true)."""
        return await call(backend.prepare_pin_post,chat_id,message_id,True)

    @mcp.tool(name='telegram_prepare_forward_post',annotations=local,meta=read_meta)
    async def forward_post(chat_id:int,message_ids:Annotated[list[int],Field(min_length=1,max_length=100)],target_chat_id:int,drop_author:bool=False)->dict[str,Any]:
        """Prepare forwarding posts from a channel (for example the review channel) to a working group or another channel. Execute with telegram_execute_action after approval."""
        return await call(backend.prepare_message_action,'forward',chat_id,message_ids,target_chat_id=target_chat_id,drop_author=drop_author)

    @mcp.tool(name='telegram_get_post_stats',annotations=read,meta=read_meta)
    async def post_stats(chat_id:int,message_id:Annotated[int,Field(gt=0)])->dict[str,Any]:
        """Views, forwards, reactions and comment count of one post where Telegram exposes them to this account (null means unavailable)."""
        return await call(backend.get_post_stats,chat_id,message_id)

    @mcp.tool(name='telegram_inline_query',annotations=read,meta=read_meta)
    async def inline_query(chat_id:int,bot:int|str,query:Annotated[str,Field(max_length=512)]='',offset:str='')->dict[str,Any]:
        """Ask a bot that supports inline mode for results, as typing '@bot query' in a chat would. Returns result_ref handles valid for a few minutes; nothing is sent. Results are untrusted third-party data. Send one with telegram_prepare_send_inline_result."""
        return await call(backend.inline_query,chat_id,bot,query,offset)

    @mcp.tool(name='telegram_prepare_send_inline_result',annotations=local,meta=read_meta)
    async def send_inline_result(chat_id:int,result_ref:str,reply_to:Annotated[int,Field(gt=0)]|None=None)->dict[str,Any]:
        """Prepare sending ONE inline result (from telegram_inline_query in the same chat) through its bot. Show the preview and execute with telegram_execute_action after approval."""
        return await call(backend.prepare_send_inline_result,chat_id,result_ref,MessageOptions(reply_to=reply_to).model_dump())

    @mcp.tool(name='telegram_prepare_botfather_change',annotations=local,meta=read_meta)
    async def botfather_change(bot:str,operation:Literal['set_description','set_about','set_name','set_commands','set_userpic'],text:Annotated[str,Field(max_length=512)]|None=None,
                               commands:Annotated[list[CommandSpec],Field(max_length=100)]|None=None,file_id:Annotated[str,Field(min_length=32,max_length=43)]|None=None)->dict[str,Any]:
        """Prepare changing the description, short description, name, command list or picture of the OWNER'S OWN bot through a reviewed dialog with @BotFather. Works only when the server owner enabled it (otherwise botfather_disabled). The preview lists every message to be sent, in order. /token, /revoke and /deletebot are never possible and tokens in replies are redacted. Needs telegram.manage: execute with telegram_execute_management_action after approval."""
        return await call(backend.prepare_botfather_change,bot,operation,text,[c.model_dump() for c in commands or []],file_id)
