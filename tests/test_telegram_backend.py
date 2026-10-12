"""Exercise scope, secret redaction, download bounds, and send idempotency offline."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from telegram_backend import BackendError, TelegramBackend


def message(message_id, chat_id=100, text="Selected chat text", file=None):
    return NS(id=message_id, chat_id=chat_id, message=text, file=file,
              media=NS(document="fake") if file else None, sender_id=50, out=False, date=None)


class FakeClient:
    def __init__(self):
        self.connected = False
        self.authorized = True
        self.entities = {100: NS(id=100, username="selected"), 200: NS(id=200, username="other")}
        self.dialogs = [NS(id=100, entity=self.entities[100], name="Selected Chat", is_channel=False, is_group=False),
                        NS(id=200, entity=self.entities[200], name="Other Chat", is_channel=True, is_group=False)]
        self.messages = {100: [message(7)], 200: [message(8, 200, "PRIVATE OTHER CHAT")]}
        self.iter_message_calls = []
        self.sent = []
        self.participant_error = None
        self.send_error = None
        self.send_delay = 0
        self.chunks = [b"hello", b" world"]
        self.get_message_override = None
        self.me = NS(id=999, first_name="Owner", last_name="", username="owner", phone="SHOULD_NOT_APPEAR")

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return self.me

    async def iter_dialogs(self):
        for dialog in self.dialogs:
            yield dialog

    async def iter_messages(self, entity, **kwargs):
        self.iter_message_calls.append((entity.id, kwargs))
        for item in self.messages[entity.id][:kwargs["limit"]]:
            if "search" not in kwargs or kwargs["search"] in item.message:
                yield item

    async def get_messages(self, entity, ids):
        if self.get_message_override is not None:
            return self.get_message_override
        return next((item for item in self.messages[entity.id] if item.id == ids), None)

    async def iter_download(self, media, **kwargs):
        for chunk in self.chunks:
            yield chunk

    async def get_participants(self, entity, **kwargs):
        if self.participant_error:
            raise self.participant_error
        return [NS(id=501, first_name="Visible", last_name="", username="visible", phone="123")]

    async def send_message(self, entity, body, **kwargs):
        self.sent.append((entity.id, body, kwargs))
        if self.send_delay:
            await asyncio.sleep(self.send_delay)
        if self.send_error:
            raise self.send_error
        return message(123, entity.id, body)


class BackendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.client = FakeClient()
        self.now = 1_800_000_000.0
        self.backend = self.make_backend()

    def tearDown(self):
        self.temp.cleanup()

    def make_backend(self, **kwargs):
        return TelegramBackend(client=self.client, output_dir=self.root / "output",
                               drafts_db=self.root / "drafts.db", clock=lambda: self.now,
                               document_filter_factory=lambda: "document-only", **kwargs)

    async def assert_error(self, code, operation):
        with self.assertRaises(BackendError) as captured:
            await operation
        self.assertEqual(captured.exception.code, code)
        return captured.exception

    async def test_status_does_not_expose_owner_phone(self):
        response = await self.backend.status()
        self.assertTrue(response["authorized"])
        self.assertNotIn("phone", response["profile"])
        self.assertNotIn("SHOULD_NOT_APPEAR", json.dumps(response))
        self.assertFalse(response["sending_enabled"])

    async def test_dialog_search_does_not_read_message_content(self):
        response = await self.backend.find_chats("selected")
        self.assertEqual([100], [chat["chat_id"] for chat in response["chats"]])
        self.assertEqual([], self.client.iter_message_calls)
        self.assertNotIn("PRIVATE OTHER CHAT", json.dumps(response))

    async def test_megagroup_is_described_as_group(self):
        self.client.entities[200].megagroup = True
        response = await self.backend.find_chats("other")
        self.assertEqual(response["chats"][0]["type"], "group")

    async def test_selected_chat_reads_and_search_are_scoped(self):
        response = await self.backend.read_messages("100", limit=1)
        searched = await self.backend.search_messages(100, "Selected", limit=2)
        self.assertEqual(response["messages"][0]["chat_id"], 100)
        self.assertEqual(searched["messages"][0]["message_id"], 7)
        self.assertEqual([100, 100], [call[0] for call in self.client.iter_message_calls])
        self.assertEqual(self.client.iter_message_calls[1][1]["search"], "Selected")

    async def test_documents_use_server_document_filter(self):
        await self.backend.list_documents(100, 10)
        self.assertEqual(self.client.iter_message_calls, [(100, {"limit": 10, "filter": "document-only"})])

    async def test_limits_and_unknown_chat_do_not_read_messages(self):
        await self.assert_error("invalid_input", self.backend.read_messages(100, 101))
        await self.assert_error("invalid_input", self.backend.read_messages(100, True))
        await self.assert_error("invalid_input", self.backend.read_messages("@other"))
        await self.assert_error("invalid_input", self.backend.get_participants(100, 501))
        await self.assert_error("chat_not_found", self.backend.read_messages(999))
        self.assertEqual([], self.client.iter_message_calls)

    async def test_returned_peer_mismatch_does_not_leak_other_chat(self):
        self.client.messages[100] = [message(8, 200, "PRIVATE OTHER CHAT")]
        error = await self.assert_error("message_not_found", self.backend.read_messages(100))
        self.assertNotIn("PRIVATE OTHER CHAT", str(error))

    async def test_participant_permission_error_is_sanitized(self):
        ChatAdminRequiredError = type("ChatAdminRequiredError", (Exception,), {})
        self.client.participant_error = ChatAdminRequiredError("API_HASH SECRET SESSION_STRING")
        error = await self.assert_error("permission_denied", self.backend.get_participants(200))
        self.assertNotIn("SECRET", json.dumps(error.as_dict()))
        self.assertNotIn("API_HASH", str(error))

    async def test_flood_wait_returns_retry_delay_without_sleep(self):
        FloodWaitError = type("FloodWaitError", (Exception,), {})
        error = FloodWaitError("API_HASH SECRET")
        error.seconds = 3600
        self.client.participant_error = error
        response = await self.assert_error("rate_limited", asyncio.wait_for(self.backend.get_participants(100), 1))
        self.assertEqual(response.retry_after_seconds, 3600)
        self.assertNotIn("SECRET", str(response))

    async def test_revoked_session_does_not_read_messages(self):
        self.client.authorized = False
        await self.assert_error("authorization_required", self.backend.read_messages(100))
        self.assertEqual([], self.client.iter_message_calls)

    async def test_download_sanitizes_untrusted_paths(self):
        self.client.messages[100] = [message(7, file=NS(name="C:\\..\\..\\credentials.json", size=11, mime_type="application/json"))]
        response = await self.backend.download_attachment(100, 7)
        path = Path(response["path"])
        self.assertEqual(path.parent, (self.root / "output").resolve())
        self.assertEqual(path.read_bytes(), b"hello world")
        self.assertEqual(response["filename"], "credentials.json")
        self.assertEqual(response["size_bytes"], 11)

    async def test_download_checks_message_peer_before_creating_files(self):
        self.client.get_message_override = message(7, 200, file=NS(name="secret.txt", size=11))
        await self.assert_error("message_not_found", self.backend.download_attachment(100, 7))
        self.assertFalse((self.root / "output").exists())

    async def test_download_unknown_size_is_bounded_and_partial_deleted(self):
        backend = self.make_backend(max_download_bytes=6)
        self.client.messages[100] = [message(7, file=NS(name="large.bin", size=None))]
        await self.assert_error("attachment_too_large", backend.download_attachment(100, 7))
        self.assertEqual([], list((self.root / "output").iterdir()))

    async def test_download_total_quota_rejects_known_size_before_creating_file(self):
        backend = self.make_backend(max_total_download_bytes=20)
        output = self.root / "output"
        output.mkdir()
        existing = output / ("a" * 32 + "_existing.bin")
        existing.write_bytes(b"1234567890")
        self.client.messages[100] = [message(7, file=NS(name="new.bin", size=11))]
        await self.assert_error("output_quota_exceeded", backend.download_attachment(100, 7))
        self.assertEqual([existing], list(output.iterdir()))

    async def test_download_total_quota_bounds_unknown_size_and_removes_partial(self):
        backend = self.make_backend(max_total_download_bytes=20)
        output = self.root / "output"
        output.mkdir()
        existing = output / ("a" * 32 + "_existing.bin")
        existing.write_bytes(b"1234567890")
        self.client.messages[100] = [message(7, file=NS(name="new.bin", size=None))]
        await self.assert_error("output_quota_exceeded", backend.download_attachment(100, 7))
        self.assertEqual([existing], list(output.iterdir()))
        self.assertEqual(existing.read_bytes(), b"1234567890")

    async def test_total_quota_ignores_unrelated_files_and_subdirectories(self):
        backend = self.make_backend(max_total_download_bytes=11)
        output = self.root / "output"
        output.mkdir()
        (output / "unrelated.txt").write_bytes(b"unrelated" * 30)
        (output / ("b" * 32 + "_directory.bin")).mkdir()
        self.client.messages[100] = [message(7, file=NS(name="new.bin", size=11))]
        response = await backend.download_attachment(100, 7)
        self.assertEqual(response["size_bytes"], 11)

    async def test_disabled_sends_do_not_call_client(self):
        draft = await self.backend.prepare_message(100, "Review this message")
        await self.assert_error("send_disabled", self.backend.send_prepared_message(draft["draft_id"], draft["recipient"]["chat_id"], draft["text"]))
        self.assertEqual([], self.client.sent)

    async def test_prepared_send_is_once_with_concurrent_calls_and_restart(self):
        backend = self.make_backend(allow_send=True)
        self.client.send_delay = 0.02
        draft = await backend.prepare_message(100, "Plain <b>text</b>")
        responses = await asyncio.gather(*(backend.send_prepared_message(draft["draft_id"], draft["recipient"]["chat_id"], draft["text"]) for _ in range(3)))
        self.assertEqual(1, len(self.client.sent))
        self.assertEqual([False, True, True], [r["already_sent"] for r in responses])
        self.assertEqual(self.client.sent[0][2], {"parse_mode": None, "link_preview": False})
        restarted = self.make_backend(allow_send=True)
        self.assertTrue((await restarted.send_prepared_message(draft["draft_id"], draft["recipient"]["chat_id"], draft["text"]))["already_sent"])
        self.assertEqual(1, len(self.client.sent))

    async def test_expired_draft_cannot_send(self):
        backend = self.make_backend(allow_send=True)
        draft = await backend.prepare_message(100, "Expires")
        self.now += 600
        await self.assert_error("draft_expired", backend.send_prepared_message(draft["draft_id"], draft["recipient"]["chat_id"], draft["text"]))
        self.assertEqual([], self.client.sent)

    async def test_confirmation_must_match_draft_recipient_and_exact_text(self):
        backend = self.make_backend(allow_send=True)
        draft = await backend.prepare_message(100, "Exact text")
        await self.assert_error("draft_mismatch", backend.send_prepared_message(draft["draft_id"], 200, "Exact text"))
        await self.assert_error("draft_mismatch", backend.send_prepared_message(draft["draft_id"], 100, "Changed text"))
        self.assertEqual([], self.client.sent)
        self.assertTrue((await backend.send_prepared_message(draft["draft_id"], 100, "Exact text"))["sent"])

    async def test_uncertain_send_is_never_retried_after_network_error(self):
        backend = self.make_backend(allow_send=True)
        draft = await backend.prepare_message(100, "Do not duplicate")
        self.client.send_error = ConnectionError("SECRET SESSION")
        error = await self.assert_error("connection_error", backend.send_prepared_message(draft["draft_id"], draft["recipient"]["chat_id"], draft["text"]))
        self.assertNotIn("SECRET", str(error))
        restarted = self.make_backend(allow_send=True)
        await self.assert_error("delivery_uncertain", restarted.send_prepared_message(draft["draft_id"], draft["recipient"]["chat_id"], draft["text"]))
        self.assertEqual(1, len(self.client.sent))

    async def test_cancelled_send_remains_uncertain(self):
        backend = self.make_backend(allow_send=True)
        draft = await backend.prepare_message(100, "Cancellation")
        self.client.send_delay = 5
        task = asyncio.create_task(backend.send_prepared_message(draft["draft_id"], draft["recipient"]["chat_id"], draft["text"]))
        while not self.client.sent:
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.assert_error("delivery_uncertain", backend.send_prepared_message(draft["draft_id"], draft["recipient"]["chat_id"], draft["text"]))
        self.assertEqual(1, len(self.client.sent))


class EnvironmentTests(unittest.TestCase):
    def test_environment_configuration_error_never_contains_secret(self):
        with patch.dict(os.environ, {"TG_API_ID": "SECRET_INVALID", "TG_API_HASH": "SECRET_HASH", "TG_SESSION": "SECRET_SESSION"}, clear=True):
            with self.assertRaises(BackendError) as captured:
                TelegramBackend.from_env()
            self.assertEqual(captured.exception.code, "configuration_error")
            self.assertNotIn("SECRET", str(captured.exception))

    def test_environment_configuration_does_not_connect_or_import_telethon(self):
        with patch.dict(os.environ, {"TG_API_ID": "123", "TG_API_HASH": "fake-only", "TG_SESSION": "fake-only"}, clear=True):
            backend = TelegramBackend.from_env()
            self.assertIsNone(backend._client)
            self.assertFalse(backend.allow_send)
            self.assertEqual(backend.max_total_download_bytes, 200 * 1024 * 1024)

    def test_environment_total_quota_maximum_is_validated(self):
        with patch.dict(os.environ, {"TG_API_ID": "123", "TG_API_HASH": "fake-only", "TG_SESSION": "fake-only",
                                     "TG_DOWNLOAD_TOTAL_BYTES": str(2 * 1024 * 1024 * 1024 + 1)}, clear=True):
            with self.assertRaises(BackendError) as captured:
                TelegramBackend.from_env()
            self.assertEqual(captured.exception.code, "configuration_error")

    def test_credentials_file_uses_private_permissions_and_does_not_connect(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "credentials.json"
            path.write_text(json.dumps({"api_id": 123, "api_hash": "fake-only", "session": "fake-only"}))
            os.chmod(path, 0o600)
            with patch.dict(os.environ, {"TG_CREDENTIALS_FILE": str(path)}, clear=True):
                backend = TelegramBackend.from_env()
                self.assertIsNone(backend._client)


if __name__ == "__main__":
    unittest.main()
