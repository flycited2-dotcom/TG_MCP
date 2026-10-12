"""Bounded Telegram operations for a private MCP server.

This module never logs credentials or upstream exception text. It uses one
serialized client, scopes message reads to a selected numeric dialog, and makes
each outbound draft a single-use persistent operation.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


class BackendError(Exception):
    """An intentionally sanitized error safe to return through MCP."""

    def __init__(self, code: str, message: str, retry_after_seconds: int | None = None, *, diagnostics: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        self.diagnostics = diagnostics or {}

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"error": self.code, "message": str(self)}
        if self.retry_after_seconds is not None:
            result["retry_after_seconds"] = self.retry_after_seconds
        result.update(self.diagnostics)
        return result


def _safe_error(error: Exception) -> BackendError:
    # Never stringify the original exception: errors can contain request data.
    name = type(error).__name__
    if name in {"FloodWaitError", "FloodTestPhoneWaitError", "SlowModeWaitError"}:
        seconds = getattr(error, "seconds", 0)
        seconds = max(0, int(seconds)) if isinstance(seconds, (int, float)) else 0
        return BackendError("rate_limited", "Telegram requires a wait before another request.", seconds)
    if name in {"ChatAdminRequiredError", "ChatWriteForbiddenError", "ChannelPrivateError", "UserBannedInChannelError"}:
        return BackendError("permission_denied", "Your Telegram account does not have permission for this operation.")
    if name in {"AuthKeyUnregisteredError", "SessionRevokedError", "SessionExpiredError", "UnauthorizedError", "AuthKeyDuplicatedError"}:
        return BackendError("authorization_required", "The saved Telegram session must be authorized again.")
    if name in {"UserPrivacyRestrictedError", "UserNotMutualContactError", "UserChannelsTooMuchError", "PeerFloodError"}:
        return BackendError("privacy_restricted", "Telegram privacy or anti-spam restrictions prohibit this action; no bypass or retry was attempted.")
    if name in {"PremiumAccountRequiredError", "StoriesTooMuchError", "StoriesSendUnavailableError", "StoryPeriodInvalidError"}:
        return BackendError("feature_unavailable", "This account cannot use the requested feature or Story settings.")
    if name in {"ValidationError", "MessageIdInvalidError", "MessageEditTimeExpiredError", "MessageNotModifiedError", "MessageTooLongError", "MediaInvalidError", "PhotoInvalidError", "ReactionInvalidError"}:
        return BackendError("invalid_input", "The requested message/media/settings are invalid or cannot be changed by this account.")
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return BackendError("timeout", "The Telegram request timed out. No automatic retry was made.")
    if isinstance(error, (ConnectionError, OSError)):
        return BackendError("connection_error", "The Telegram operation could not access the network or local storage.")
    diagnostics = {}
    # Expose only known protocol codes, never stringify an exception/request.
    # Telethon may return new rich-message failures as the generic RPCError.
    from telethon.errors import RPCError
    if isinstance(error, RPCError):
        diagnostics['telegram_error_type'] = 'RPCError'
        rpc_code = getattr(error, 'code', None)
        if type(rpc_code) is int and rpc_code in (400, 401, 403, 404, 406, 420, 500, 503):
            diagnostics['telegram_rpc_code'] = rpc_code
        protocol_code = getattr(error, 'message', None)
        if protocol_code in {
            'RICH_MESSAGE_INVALID', 'RICH_MESSAGE_NOT_ALLOWED', 'RICH_MESSAGE_TOO_LONG',
            'RICH_MESSAGE_EMPTY', 'RICH_MESSAGE_UNSUPPORTED', 'RICH_MESSAGE_BLOCK_INVALID',
            'MESSAGE_EMPTY', 'BOT_METHOD_INVALID', 'METHOD_INVALID', 'INPUT_CONSTRUCTOR_INVALID',
            'PHOTO_INVALID', 'PEER_ID_INVALID', 'USER_NOT_ALLOWED', 'PREMIUM_ACCOUNT_REQUIRED',
        }:
            diagnostics['telegram_rpc_error'] = protocol_code
    return BackendError("telegram_error", "The Telegram operation failed. No retry was made; request data and credentials are withheld.", diagnostics=diagnostics)


def _operation(function):
    @functools.wraps(function)
    async def wrapped(self, *args, **kwargs):
        async with self._lock:
            try:
                timeout = 300 if function.__name__ in {"download_attachment", "execute_action"} else 45
                return await asyncio.wait_for(function(self, *args, **kwargs), timeout)
            except BackendError:
                raise
            except Exception as error:
                raise _safe_error(error) from None
    return wrapped


def _json_safe_numbers(value):
    """Preserve Telegram int64 identifiers through JavaScript JSON clients."""
    if type(value) is int and abs(value) > 9007199254740991:
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe_numbers(item) for item in value]
    return value


def _limit(value: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise BackendError("invalid_input", f"limit must be an integer between 1 and {maximum}.")
    return value


def _chat_id(value: int | str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise BackendError("invalid_input", "Select a numeric chat_id returned by find_chats.")
    if isinstance(value, str) and not re.fullmatch(r"-?[0-9]{1,20}", value):
        raise BackendError("invalid_input", "Select a numeric chat_id returned by find_chats.")
    result = int(value)
    if not result or not -(2**63) <= result < 2**63:
        raise BackendError("invalid_input", "The chat_id is invalid.")
    return result


def _text(value: str, maximum: int, field: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value.strip()):
        raise BackendError("invalid_input", f"{field} must contain between 1 and {maximum} characters.")
    return value


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _user(user: Any, *, include_phone: bool = False) -> dict[str, Any]:
    result = {
        "telegram_id": getattr(user, "id", None),
        "first_name": getattr(user, "first_name", None) or "",
        "last_name": getattr(user, "last_name", None) or "",
        "username": getattr(user, "username", None),
        "bot": bool(getattr(user, "bot", False)),
        "deleted": bool(getattr(user, "deleted", False)),
    }
    if include_phone and getattr(user, "phone", None):
        result["phone"] = user.phone
    return result


class TelegramBackend:
    """Keep one instance per process; all account operations share its lock."""

    def __init__(
        self,
        *,
        client: Any = None,
        client_factory: Callable[[], Any] | None = None,
        output_dir: str | Path = "/data/output",
        drafts_db: str | Path = "/data/drafts.db",
        allow_send: bool = False,
        max_download_bytes: int = 50 * 1024 * 1024,
        max_total_download_bytes: int = 200 * 1024 * 1024,
        clock: Callable[[], float] = time.time,
        document_filter_factory: Callable[[], Any] | None = None,
    ):
        self._client = client
        self._client_factory = client_factory
        self._lock = asyncio.Lock()
        self.output_dir = Path(output_dir)
        self.drafts_db = Path(drafts_db)
        self.allow_send = allow_send
        self.max_download_bytes = max_download_bytes
        self.max_total_download_bytes = max_total_download_bytes
        self._clock = clock
        self._document_filter_factory = document_filter_factory

    @classmethod
    def from_env(cls) -> "TelegramBackend":
        """Read credentials without opening a Telegram connection or logging them."""
        credentials: dict[str, Any] = {}
        credentials_path = os.environ.get("TG_CREDENTIALS_FILE")
        try:
            if credentials_path:
                path = Path(credentials_path)
                if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
                    raise BackendError("configuration_error", "The Telegram credentials file must be private (mode 600).")
                credentials = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(credentials, dict):
                    raise ValueError()
            api_id = int(os.environ.get("TG_API_ID") or credentials.get("api_id") or credentials.get("TG_API_ID") or 0)
            api_hash = os.environ.get("TG_API_HASH") or credentials.get("api_hash") or credentials.get("TG_API_HASH")
            session = os.environ.get("TG_SESSION") or credentials.get("session") or credentials.get("TG_SESSION")
            if api_id <= 0 or not isinstance(api_hash, str) or not api_hash or not isinstance(session, str) or not session:
                raise ValueError()
            max_bytes = int(os.environ.get("TG_DOWNLOAD_MAX_BYTES", 50 * 1024 * 1024))
            if not 1 <= max_bytes <= 500 * 1024 * 1024:
                raise ValueError()
            total_bytes = int(os.environ.get("TG_DOWNLOAD_TOTAL_BYTES", 200 * 1024 * 1024))
            if not 1 <= total_bytes <= 2 * 1024 * 1024 * 1024:
                raise ValueError()
        except BackendError:
            raise
        except Exception:
            raise BackendError("configuration_error", "Valid private Telegram credentials and download settings are required.") from None

        def make_client():
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            quiet_logger = logging.getLogger("private_telegram_mcp.telethon")
            quiet_logger.disabled = True
            # Telethon uses child loggers. A disabled parent alone does not
            # prevent their records from propagating to the root logger.
            quiet_logger.addHandler(logging.NullHandler())
            quiet_logger.propagate = False
            stored_session = StringSession(session)
            return TelegramClient(
                stored_session, api_id, api_hash, use_ipv6=":" in stored_session.server_address,
                receive_updates=False, connection_retries=1, request_retries=0,
                flood_sleep_threshold=0, timeout=15, base_logger=quiet_logger,
            )

        return cls(
            client_factory=make_client,
            output_dir=os.environ.get("TG_OUTPUT_DIR", "/data/output"),
            drafts_db=os.environ.get("TG_DRAFTS_DB", "/data/drafts.db"),
            allow_send=os.environ.get("TG_ALLOW_SEND", "false").strip().lower() in {"1", "true", "yes"},
            max_download_bytes=max_bytes,
            max_total_download_bytes=total_bytes,
        )

    async def _ready(self):
        if self._client is None:
            if self._client_factory is None:
                raise BackendError("configuration_error", "Telegram credentials are not configured.")
            self._client = self._client_factory()
        if not self._client.is_connected():
            await self._client.connect()
        if not await self._client.is_user_authorized():
            raise BackendError("authorization_required", "The saved Telegram session must be authorized again.")
        return self._client

    @staticmethod
    def _dialog(dialog: Any) -> dict[str, Any]:
        entity = dialog.entity
        return {
            "chat_id": dialog.id,
            "title": dialog.name or "",
            "username": getattr(entity, "username", None),
            "type": "group" if bool(getattr(entity, "megagroup", False)) or bool(getattr(dialog, "is_group", False)) else "channel" if bool(getattr(dialog, "is_channel", False)) else "private",
        }

    async def _resolve(self, chat_id: int | str) -> tuple[Any, dict[str, Any]]:
        selected_id = _chat_id(chat_id)
        client = await self._ready()
        # StringSession does not persist entity access hashes. Fetch dialog
        # metadata to resolve IDs after restarts, never any message previews.
        async for dialog in client.iter_dialogs():
            if dialog.id == selected_id:
                return dialog.entity, self._dialog(dialog)
        raise BackendError("chat_not_found", "The selected chat is not in this account's accessible dialogs.")

    @staticmethod
    def _message(message: Any, chat_id: int) -> dict[str, Any]:
        file = getattr(message, "file", None)
        attachment = None
        if file is not None:
            attachment = {
                "filename": getattr(file, "name", None),
                "mime_type": getattr(file, "mime_type", None),
                "size_bytes": getattr(file, "size", None),
            }
        result = {
            "message_id": message.id,
            "chat_id": chat_id,
            "date": _iso(getattr(message, "date", None)),
            "text": getattr(message, "message", None) or "",
            "sender_id": getattr(message, "sender_id", None),
            "outgoing": bool(getattr(message, "out", False)),
            "attachment": attachment,
        }
        result.update({"album_id": getattr(message, "grouped_id", None),
                       "reply_to_message_id": getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None),
                       "saved_peer_id": getattr(getattr(message, "saved_peer_id", None), "user_id", None),
                       "links": re.findall(r"(?:https?://|tg://)[^\s<>]+", result['text'])})
        saved_peer=getattr(message,'saved_peer_id',None)
        if saved_peer is not None:
            if getattr(saved_peer,'channel_id',None):
                result['saved_peer_id']=-(1000000000000+saved_peer.channel_id)
            elif getattr(saved_peer,'chat_id',None):
                result['saved_peer_id']=-saved_peer.chat_id
        if attachment:
            attachment.update({"duration_seconds": getattr(file, "duration", None),
                               "width": getattr(file, "width", None), "height": getattr(file, "height", None),
                               "media_kind": "voice" if getattr(message, "voice", None) else "video_note" if getattr(message,"video_note",None) else "video" if getattr(message,"video",None) else "photo" if getattr(message,"photo",None) else "animation" if getattr(message,"gif",None) else "sticker" if getattr(message,"sticker",None) else "document"})
        for item in getattr(message, 'entities', None) or []:
            if getattr(item, 'url', None):
                result['links'].append(item.url)
        preview=getattr(getattr(message,'media',None),'webpage',None)
        if getattr(preview,'url',None):
            result['link_preview']={key:getattr(preview,key,None) for key in ('url','display_url','title','description','site_name','type')}
        reply=getattr(message,'reply_to',None)
        result['topic_id']=getattr(reply,'reply_to_top_id',None) or (getattr(reply,'reply_to_msg_id',None) if getattr(reply,'forum_topic',False) else None)
        if getattr(message, 'rich_message', None):
            rich=message.rich_message
            result['rich_message'] = {'blocks':[b.to_dict() for b in getattr(rich,'blocks',[])],
                                      'photo_ids':[p.id for p in getattr(rich,'photos',[])],
                                      'document_ids':[d.id for d in getattr(rich,'documents',[])]}
        return _json_safe_numbers(result)

    @staticmethod
    def _verify_message_chat(message: Any, chat_id: int):
        # Telegram's ordinary message IDs are not channel-local in every API.
        # Verify the returned peer before exposing or downloading its contents.
        if getattr(message, "chat_id", None) != chat_id:
            raise BackendError("message_not_found", "The message was not found in the selected chat.")

    @_operation
    async def status(self) -> dict[str, Any]:
        client = await self._ready()
        me = await client.get_me()
        return {"connected": True, "authorized": True, "profile": _user(me), "sending_enabled": self.allow_send}

    @_operation
    async def find_chats(self, query: str, limit: int = 20) -> dict[str, Any]:
        query = _text(query, 200, "query").strip().casefold()
        limit = _limit(limit, 20)
        client = await self._ready()
        result = []
        async for dialog in client.iter_dialogs():
            metadata = self._dialog(dialog)
            if query in metadata["title"].casefold() or query in (metadata["username"] or "").casefold() or query == str(metadata["chat_id"]):
                result.append(metadata)
                if len(result) >= limit:
                    break
        return {"chats": result, "limit": limit}

    async def _messages(self, chat_id, limit, **kwargs):
        limit = _limit(limit, 100)
        entity, metadata = await self._resolve(chat_id)
        result = []
        async for message in self._client.iter_messages(entity, limit=limit, **kwargs):
            self._verify_message_chat(message, metadata["chat_id"])
            result.append(self._message(message, metadata["chat_id"]))
        return {"chat": metadata, "messages": result, "limit": limit}

    @_operation
    async def read_messages(self, chat_id: int | str, limit: int = 50) -> dict[str, Any]:
        return await self._messages(chat_id, limit)

    @_operation
    async def search_messages(self, chat_id: int | str, query: str, limit: int = 50) -> dict[str, Any]:
        query = _text(query, 1000, "query")
        return await self._messages(chat_id, limit, search=query)

    @_operation
    async def list_documents(self, chat_id: int | str, limit: int = 50) -> dict[str, Any]:
        if self._document_filter_factory:
            document_filter = self._document_filter_factory()
        else:
            from telethon.tl.types import InputMessagesFilterDocument
            document_filter = InputMessagesFilterDocument()
        return await self._messages(chat_id, limit, filter=document_filter)

    def _existing_download_bytes(self) -> int:
        """Count only our regular output files, with bounded directory work."""
        if not self.output_dir.exists():
            return 0
        output_root = self.output_dir.resolve()
        total = 0
        with os.scandir(self.output_dir) as entries:
            for count, entry in enumerate(entries, start=1):
                if count > 10000:
                    raise BackendError("output_quota_exceeded", "The download directory has too many entries. Remove expired downloads before retrying.")
                if not re.fullmatch(r"[a-f0-9]{32}_.+", entry.name) or not entry.is_file(follow_symlinks=False):
                    continue
                path = Path(entry.path).resolve()
                if not path.is_relative_to(output_root):
                    continue
                total += entry.stat(follow_symlinks=False).st_size
                if total >= self.max_total_download_bytes:
                    raise BackendError("output_quota_exceeded", "The total download storage limit has been reached. Remove expired downloads before retrying.")
        return total

    @_operation
    async def download_attachment(self, chat_id: int | str, message_id: int) -> dict[str, Any]:
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            raise BackendError("invalid_input", "message_id must be a positive integer.")
        entity, metadata = await self._resolve(chat_id)
        message = await self._client.get_messages(entity, ids=message_id)
        if message is None:
            raise BackendError("message_not_found", "The message was not found in the selected chat.")
        self._verify_message_chat(message, metadata["chat_id"])
        file = getattr(message, "file", None)
        if not file or not getattr(message, "media", None):
            raise BackendError("attachment_not_found", "The selected message has no downloadable attachment.")
        declared_size = getattr(file, "size", None)
        if isinstance(declared_size, (int, float)) and declared_size > self.max_download_bytes:
            raise BackendError("attachment_too_large", "The attachment exceeds the configured download limit.")
        existing_bytes = self._existing_download_bytes()
        if isinstance(declared_size, (int, float)) and existing_bytes + declared_size > self.max_total_download_bytes:
            raise BackendError("output_quota_exceeded", "The attachment would exceed the total download storage limit. Remove expired downloads before retrying.")
        self.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        original_name = getattr(file, "name", None) or "attachment.bin"
        # A Telegram-provided filename is untrusted, including Windows paths.
        basename = str(original_name).replace("\\", "/").rsplit("/", 1)[-1]
        safe_name = re.sub(r"[^\w.() -]", "_", basename, flags=re.UNICODE).strip(" .")[:160] or "attachment.bin"
        path = self.output_dir / f"{secrets.token_hex(16)}_{safe_name}"
        size = 0
        try:
            with path.open("xb") as stream:
                if os.name != "nt":
                    os.chmod(path, 0o600)
                async for chunk in self._client.iter_download(message.media, request_size=512 * 1024):
                    size += len(chunk)
                    if size > self.max_download_bytes:
                        raise BackendError("attachment_too_large", "The attachment exceeds the configured download limit.")
                    if existing_bytes + size > self.max_total_download_bytes:
                        raise BackendError("output_quota_exceeded", "The attachment would exceed the total download storage limit. Remove expired downloads before retrying.")
                    stream.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return {"chat": metadata, "message_id": message_id, "filename": safe_name,
                "mime_type": getattr(file, "mime_type", None), "size_bytes": size, "path": str(path.resolve())}

    @_operation
    async def get_participants(self, chat_id: int | str, limit: int = 100) -> dict[str, Any]:
        limit = _limit(limit, 500)
        entity, metadata = await self._resolve(chat_id)
        users = await self._client.get_participants(entity, limit=limit, aggressive=False)
        return {"chat": metadata, "participants": [_user(user, include_phone=True) for user in users],
                "retrieved_count": len(users), "total_reported": getattr(users, "total", None),
                "limit": limit, "scope": "Only participants and contact fields visible to this account; this may be incomplete."}

    def _db(self):
        self.drafts_db.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.drafts_db, timeout=5)
        try:
            if os.name != "nt":
                os.chmod(self.drafts_db, 0o600)
            db.execute("""CREATE TABLE IF NOT EXISTS drafts (
                draft_id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL,
                recipient TEXT NOT NULL, body TEXT NOT NULL, expires REAL NOT NULL,
                state TEXT NOT NULL, result TEXT, text_hash TEXT
            )""")
            if "text_hash" not in {row[1] for row in db.execute("PRAGMA table_info(drafts)")}:
                db.execute("ALTER TABLE drafts ADD COLUMN text_hash TEXT")
            for draft_id, body in db.execute("SELECT draft_id, body FROM drafts WHERE text_hash IS NULL AND body != ''").fetchall():
                db.execute("UPDATE drafts SET text_hash=? WHERE draft_id=?",
                           (hashlib.sha256(body.encode("utf-8")).hexdigest(), draft_id))
            db.commit()
            return db
        except BaseException:
            db.close()
            raise

    @_operation
    async def prepare_message(self, chat_id: int | str, text: str) -> dict[str, Any]:
        text = _text(text, 4096, "text")
        _, recipient = await self._resolve(chat_id)
        draft_id = secrets.token_urlsafe(32)
        expires = self._clock() + 600
        with self._db() as db:
            # Keep consumed IDs as tombstones so repeats can never send again,
            # but stop retaining message text after its useful lifetime.
            db.execute("UPDATE drafts SET body='' WHERE expires < ? AND state != 'sending'", (self._clock(),))
            db.execute("INSERT INTO drafts (draft_id,chat_id,recipient,body,expires,state,result,text_hash) VALUES (?, ?, ?, ?, ?, 'prepared', NULL, ?)",
                       (draft_id, recipient["chat_id"], json.dumps(recipient, ensure_ascii=False), text, expires,
                        hashlib.sha256(text.encode("utf-8")).hexdigest()))
        db.close()
        return {"draft_id": draft_id, "recipient": recipient, "text": text,
                "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat(),
                "sending_enabled": self.allow_send, "requires_explicit_confirmation": True}

    @_operation
    async def send_prepared_message(self, draft_id: str, expected_chat_id: int | str, expected_text: str) -> dict[str, Any]:
        if not self.allow_send:
            raise BackendError("send_disabled", "Sending is disabled. Set TG_ALLOW_SEND=true to enable it.")
        if not isinstance(draft_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", draft_id):
            raise BackendError("invalid_input", "A valid draft_id returned by prepare_message is required.")
        expected_chat_id = _chat_id(expected_chat_id)
        expected_text = _text(expected_text, 4096, "expected_text")
        db = self._db()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT chat_id, recipient, body, expires, state, result, text_hash FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
            if row is None:
                raise BackendError("draft_not_found", "The message draft does not exist.")
            chat_id, recipient_json, body, expires, state, result, text_hash = row
            if chat_id != expected_chat_id or not hmac.compare_digest(
                hashlib.sha256(expected_text.encode("utf-8")).hexdigest(), text_hash or ""
            ):
                raise BackendError("draft_mismatch", "The confirmed recipient or text does not match the prepared draft.")
            if state == "sent":
                response = json.loads(result)
                response["already_sent"] = True
                return response
            if state in {"sending", "uncertain"}:
                raise BackendError("delivery_uncertain", "This draft was already submitted. Check the selected chat before preparing another message.")
            if expires <= self._clock():
                raise BackendError("draft_expired", "The message draft expired. Prepare and review a new draft.")
            # Resolve and authorize before the persistent claim. Network reads
            # do not send and cannot create ambiguous delivery states.
            entity, _ = await self._resolve(chat_id)
            db.execute("UPDATE drafts SET state='sending' WHERE draft_id=?", (draft_id,))
            db.commit()
            try:
                message = await self._client.send_message(entity, body, parse_mode=None, link_preview=False)
                response = {"draft_id": draft_id, "recipient": json.loads(recipient_json),
                            "message_id": message.id, "sent": True, "already_sent": False}
                db.execute("UPDATE drafts SET state='sent', body='', result=? WHERE draft_id=?",
                           (json.dumps(response, ensure_ascii=False), draft_id))
                db.commit()
                return response
            except BaseException:
                # Even a timeout or process interruption can happen after
                # delivery. Retrying the same draft is intentionally forbidden.
                db.execute("UPDATE drafts SET state='uncertain', body='' WHERE draft_id=?", (draft_id,))
                db.commit()
                raise
        finally:
            db.close()

    async def close(self):
        async with self._lock:
            if self._client is not None and self._client.is_connected():
                try:
                    await self._client.disconnect()
                except Exception:
                    raise BackendError("connection_error", "The Telegram connection could not be closed cleanly.") from None
