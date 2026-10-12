import base64
import hashlib
import html
import json
import re
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oauth_owner import OwnerOAuth


@pytest.fixture
def oauth(tmp_path):
    salt = b"test-salt-01234567890123456789"
    password_hash = hashlib.scrypt(b"test-owner-password", salt=salt, n=16384, r=8, p=1, dklen=64)
    config = tmp_path / "owner.json"
    config.write_text(json.dumps({"password_salt": base64.b64encode(salt).decode(),
                                  "password_hash": base64.b64encode(password_hash).decode()}))
    server = OwnerOAuth("https://telegram.example", config, tmp_path / "auth.sqlite")
    with TestClient(Starlette(routes=server.routes), base_url=server.public_url) as client:
        yield server, client, config


def register(client, redirect="https://chatgpt.com/connector/oauth/callback", name="ChatGPT"):
    response = client.post("/oauth/register", json={"redirect_uris": [redirect], "client_name": name})
    assert response.status_code == 201, response.text
    return response.json()["client_id"]


def auth_form(server, client, client_id, scope="telegram.read telegram.send"):
    verifier = "a" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    request = {"client_id": client_id, "redirect_uri": "https://chatgpt.com/connector/oauth/callback",
               "response_type": "code", "state": "return-to-chat", "code_challenge": challenge,
               "code_challenge_method": "S256", "scope": scope, "resource": server.resource}
    response = client.get("/oauth/authorize", params=request)
    assert response.status_code == 200, response.text
    fields = dict((name, html.unescape(value)) for name, value in re.findall(
        r'<input type="hidden" name="([^"]+)" value="([^"]*)">', response.text))
    return fields, verifier, response


def approve(server, client, client_id, *, send=False):
    fields, verifier, _ = auth_form(server, client, client_id)
    fields.update(password="test-owner-password", allow_read="yes", approve="yes")
    if send:
        fields["allow_send"] = "yes"
    response = client.post("/oauth/authorize", data=fields, follow_redirects=False)
    assert response.status_code == 303, response.text
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["state"] == ["return-to-chat"]
    assert query["iss"] == [server.public_url]
    return query["code"][0], verifier


def exchange(server, client, client_id, code, verifier):
    return client.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": client_id,
                       "code": code, "redirect_uri": "https://chatgpt.com/connector/oauth/callback",
                       "resource": server.resource, "code_verifier": verifier})


def test_discovery_metadata(oauth):
    server, client, _ = oauth
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["issuer"] == server.public_url
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["token_endpoint_auth_methods_supported"] == ["none"]
    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
        assert client.get(path).json()["resource"] == server.resource


@pytest.mark.parametrize("redirect", ["http://chatgpt.com/cb", "https://chatgpt.com/cb#fragment",
                                      "https://evil@chatgpt.com/cb", " https://chatgpt.com/cb",
                                      "https://chatgpt.com\\evil/cb", "https://chatgpt.com:8443/cb"])
def test_register_rejects_unsafe_redirect(oauth, redirect):
    _, client, _ = oauth
    assert client.post("/oauth/register", json={"redirect_uris": [redirect]}).status_code == 400


def test_one_use_code_scopes_hashed_storage_and_persistence(oauth):
    server, client, config = oauth
    client_id = register(client)
    code, verifier = approve(server, client, client_id)
    response = exchange(server, client, client_id, code, verifier)
    assert response.status_code == 200
    tokens = response.json()
    assert tokens["scope"] == "telegram.read"
    verified = server.verify_token(tokens["access_token"])
    assert verified["client_id"] == client_id
    assert verified["scopes"] == ["telegram.read"]
    assert exchange(server, client, client_id, code, verifier).json() == {"error": "invalid_grant"}
    persisted = server.db_path.read_bytes()
    assert tokens["access_token"].encode() not in persisted
    assert tokens["refresh_token"].encode() not in persisted
    assert OwnerOAuth(server.public_url, config, server.db_path).verify_token(tokens["access_token"]) == verified


def test_pkce_and_client_and_resource_binding(oauth):
    server, client, _ = oauth
    client_id = register(client)
    other_client = register(client)
    code, verifier = approve(server, client, client_id)
    assert exchange(server, client, client_id, code, "b" * 64).status_code == 400
    assert exchange(server, client, other_client, code, verifier).status_code == 400
    valid = exchange(server, client, client_id, code, verifier)
    assert valid.status_code == 200
    refresh = valid.json()["refresh_token"]
    wrong = client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": client_id,
                       "refresh_token": refresh, "resource": "https://other.example/mcp"})
    assert wrong.status_code == 400


def test_refresh_rotation_send_scope_and_revoke(oauth):
    server, client, _ = oauth
    client_id = register(client)
    code, verifier = approve(server, client, client_id, send=True)
    original = exchange(server, client, client_id, code, verifier).json()
    assert set(original["scope"].split()) == {"telegram.read", "telegram.send"}
    refresh_request = {"grant_type": "refresh_token", "client_id": client_id,
                       "refresh_token": original["refresh_token"], "resource": server.resource}
    refreshed = client.post("/oauth/token", data=refresh_request)
    assert refreshed.status_code == 200
    rotated = refreshed.json()
    assert server.verify_token(original["access_token"]) is None
    assert server.verify_token(rotated["access_token"])
    assert client.post("/oauth/token", data=refresh_request).json()["error"] == "invalid_grant"
    assert client.post("/oauth/revoke", data={"client_id": client_id,
                        "token": rotated["refresh_token"]}).status_code == 200
    assert server.verify_token(rotated["access_token"]) is None


def test_csrf_and_exact_redirect(oauth):
    server, client, _ = oauth
    client_id = register(client)
    fields, _, _ = auth_form(server, client, client_id)
    fields.update(password="test-owner-password", allow_read="yes", approve="yes")
    missing = dict(fields)
    missing.pop("csrf")
    assert client.post("/oauth/authorize", data=missing).status_code == 403
    altered = dict(fields, state="changed")
    assert client.post("/oauth/authorize", data=altered).status_code == 403
    assert client.post("/oauth/authorize", data=fields, headers={"Origin": "https://evil.example"}).status_code == 403
    bad_redirect = dict(fields, redirect_uri="https://chatgpt.com/connector/oauth/callback/other")
    assert client.post("/oauth/authorize", data=bad_redirect).status_code == 400
    client.cookies.clear()
    assert client.post("/oauth/authorize", data=fields).status_code == 403


def test_password_bruteforce_limit(oauth):
    server, client, _ = oauth
    client_id = register(client)
    for attempt in range(6):
        fields, _, _ = auth_form(server, client, client_id)
        fields.update(password="wrong", allow_read="yes", approve="yes")
        response = client.post("/oauth/authorize", data=fields)
        assert response.status_code == (401 if attempt < 5 else 429)
        assert "wrong" not in response.text


def test_body_limit_and_form_escaping(oauth):
    server, client, _ = oauth
    response = client.post("/oauth/register", content=b"x" * 9000)
    assert response.status_code == 413
    client_id = register(client, name='<script>alert("x")</script>')
    _, _, response = auth_form(server, client, client_id)
    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_expired_tokens_and_code(oauth):
    server, client, _ = oauth
    client_id = register(client)
    code, verifier = approve(server, client, client_id)
    with sqlite3.connect(server.db_path) as db:
        db.execute("UPDATE codes SET expires_at=?", (time.time() - 1,))
    assert exchange(server, client, client_id, code, verifier).status_code == 400
    code, verifier = approve(server, client, client_id)
    tokens = exchange(server, client, client_id, code, verifier).json()
    with sqlite3.connect(server.db_path) as db:
        db.execute("UPDATE access_tokens SET expires_at=?", (time.time() - 1,))
    assert server.verify_token(tokens["access_token"]) is None


def test_base_path_issuer_and_form(oauth):
    original, _, config = oauth
    server = OwnerOAuth("https://example.org/telegram-personal-mcp", config, original.db_path)
    assert server.resource == "https://example.org/telegram-personal-mcp/mcp"
    assert server.cookie_path == "/telegram-personal-mcp/oauth/authorize"
    assert server.origin == "https://example.org"
    with TestClient(Starlette(routes=server.routes), base_url="https://example.org") as client:
        meta = client.get("/.well-known/oauth-authorization-server").json()
        assert meta["authorization_endpoint"] == server.public_url + "/oauth/authorize"
        client_id = register(client)
        _, _, response = auth_form(server, client, client_id)
        assert 'action="https://example.org/telegram-personal-mcp/oauth/authorize"' in response.text
        assert "Path=/telegram-personal-mcp/oauth/authorize" in response.headers["set-cookie"]


@pytest.mark.parametrize("redirect", ["http://localhost:53682/callback", "http://127.0.0.1:1455/auth/callback",
                                      "http://[::1]:8080/cb", "https://claude.ai/api/mcp/auth_callback"])
def test_native_and_claude_clients_complete_flow_without_resource(oauth, redirect):
    server, client, _ = oauth
    client_id = register(client, redirect, "Claude Code")
    verifier = "c" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    request = {"client_id": client_id, "redirect_uri": redirect, "response_type": "code", "state": "s",
               "code_challenge": challenge, "code_challenge_method": "S256", "scope": "telegram.read"}
    form = client.get("/oauth/authorize", params=request)
    assert form.status_code == 200, form.text
    origin = redirect.split("/")[0] + "//" + redirect.split("/")[2]
    assert "form-action 'self' " + origin + ";" in form.headers["content-security-policy"]
    fields = dict((name, html.unescape(value)) for name, value in re.findall(
        r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form.text))
    fields.update(password="test-owner-password", allow_read="yes")
    response = client.post("/oauth/authorize", data=fields, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith(redirect + "?")
    code = parse_qs(urlsplit(response.headers["location"]).query)["code"][0]
    tokens = client.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": client_id,
                         "code": code, "redirect_uri": redirect, "code_verifier": verifier})
    assert tokens.status_code == 200, tokens.text
    assert server.verify_token(tokens.json()["access_token"])["scopes"] == ["telegram.read"]


def test_loopback_port_may_vary_but_path_and_host_may_not(oauth):
    server, client, _ = oauth
    client_id = register(client, "http://127.0.0.1:1455/callback", "Codex")
    base = {"client_id": client_id, "response_type": "code", "state": "s", "code_challenge": "a" * 43,
            "code_challenge_method": "S256", "resource": server.resource}
    assert client.get("/oauth/authorize", params=dict(base, redirect_uri="http://127.0.0.1:2000/callback")).status_code == 200
    for wrong in ("http://127.0.0.1:2000/other", "http://localhost:1455/callback", "http://evil.example:1455/callback"):
        assert client.get("/oauth/authorize", params=dict(base, redirect_uri=wrong)).status_code == 400


@pytest.mark.parametrize("redirect", ["http://evil.example/cb", "http://localhost.evil.example/cb",
                                      "http://user@localhost/cb", "http://localhost/cb#x", "http://192.168.1.2/cb"])
def test_register_rejects_non_loopback_http(oauth, redirect):
    _, client, _ = oauth
    assert client.post("/oauth/register", json={"redirect_uris": [redirect]}).status_code == 400
