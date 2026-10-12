import base64
import hashlib
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from oauth_owner import OwnerOAuth
from server import create_app
from telegram_backend import BackendError
from file_downloads import DownloadStore


class FakeBackend:
    def __init__(self, output_dir):
        self.output_dir = Path(output_dir)
        self.calls = []

    async def status(self):
        self.calls.append("status")
        return {"connected": True, "profile": {"telegram_id": 1}}

    async def send_prepared_message(self, draft_id, chat_id, text):
        self.calls.append((draft_id, chat_id, text))
        return {"sent": True}

    async def get_participants(self, chat_id, limit):
        raise BackendError("permission_denied", "Admin required")

    async def close(self):
        pass


@pytest.fixture
def app_fixture(tmp_path, monkeypatch):
    salt = b"isolated-test-salt-1234567890"
    config = tmp_path / "auth.json"
    config.write_text(json.dumps({"password_salt": base64.b64encode(salt).decode(),
        "password_hash": base64.b64encode(hashlib.scrypt(b"test", salt=salt, n=16384, r=8, p=1, dklen=64)).decode()}))
    oauth = OwnerOAuth("https://telegram.example", config, tmp_path / "auth.sqlite")
    monkeypatch.setenv("TG_DATA_DIR", str(tmp_path))
    backend = FakeBackend(tmp_path / "output")
    with TestClient(create_app(backend, oauth), base_url="https://telegram.example") as client:
        yield client, oauth, backend


def bearer(oauth, scopes):
    with oauth._db() as db:
        tokens = oauth._issue_tokens(db, "fixture-client", oauth.resource,
                                    " ".join(scopes), "test-family", 9999999999)
    return {"Authorization": "Bearer " + tokens["access_token"],
            "Accept": "application/json, text/event-stream"}


def rpc(client, headers, method, params=None):
    return client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1,
                       "method": method, "params": params or {}})


def test_private_mcp_never_connects_unauthenticated(app_fixture):
    client, oauth, backend = app_fixture
    assert client.get("/healthz").status_code == 200
    response = rpc(client, {}, "tools/list")
    assert response.status_code == 401
    assert oauth.public_url in response.headers["www-authenticate"]
    assert backend.calls == []
    response = rpc(client, {"Authorization": "Bearer wrong"}, "tools/list")
    assert response.status_code == 401
    assert backend.calls == []


def test_tool_schema_write_annotation_and_status(app_fixture):
    client, oauth, backend = app_fixture
    headers = bearer(oauth, ["telegram.read"])
    response = rpc(client, headers, "tools/list")
    assert response.status_code == 200, response.text
    tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
    assert len(tools) == 9
    assert tools["telegram_status"]["annotations"]["readOnlyHint"] is True
    send = tools["telegram_send_prepared_message"]
    assert send["annotations"]["readOnlyHint"] is False
    assert set(send["inputSchema"]["required"]) == {"draft_id", "chat_id", "text"}
    response = rpc(client, headers, "tools/call", {"name": "telegram_status", "arguments": {}})
    assert response.status_code == 200, response.text
    assert response.json()["result"]["structuredContent"]["connected"] is True
    assert backend.calls == ["status"]


def test_send_scope_is_enforced_before_backend(app_fixture):
    client, oauth, backend = app_fixture
    headers = bearer(oauth, ["telegram.read"])
    arguments = {"draft_id": "x" * 43, "chat_id": 42, "text": "reviewed"}
    response = rpc(client, headers, "tools/call", {"name": "telegram_send_prepared_message", "arguments": arguments})
    assert response.json()["result"]["structuredContent"]["error"] == "insufficient_scope"
    assert backend.calls == []
    headers = bearer(oauth, ["telegram.read", "telegram.send"])
    response = rpc(client, headers, "tools/call", {"name": "telegram_send_prepared_message", "arguments": arguments})
    assert response.json()["result"]["structuredContent"]["sent"] is True
    assert backend.calls == [("x" * 43, 42, "reviewed")]


def test_permission_error_is_reported_without_bypassing(app_fixture):
    client, oauth, backend = app_fixture
    response = rpc(client, bearer(oauth, ["telegram.read"]), "tools/call",
                   {"name": "telegram_get_participants", "arguments": {"chat_id": 42}})
    assert response.json()["result"]["structuredContent"]["error"] == "permission_denied"


def test_download_link_is_bounded_and_expires(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    file = output / "document.txt"
    file.write_bytes(b"private file")
    now = [1000]
    store = DownloadStore("https://telegram.example", output, tmp_path / "files.sqlite", lambda: now[0])
    link = store.issue(str(file), "document.txt")
    assert str(output) not in link
    from starlette.applications import Starlette
    with TestClient(Starlette(routes=store.routes), base_url="https://telegram.example") as client:
        response = client.get(link)
        assert response.status_code == 200
        assert response.content == b"private file"
        assert response.headers["cache-control"] == "no-store"
        now[0] += 601
        assert client.get(link).status_code == 404
        assert client.get("/files/" + "z" * 43).status_code == 404
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    with pytest.raises(ValueError):
        store.issue(str(outside), "outside.txt")
