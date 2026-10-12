"""Browser authorization regressions using only artificial credentials."""

import base64
import hashlib
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest

from test_oauth_owner import auth_form, exchange, oauth, register


class _FormInputs(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.hidden = {}
        self.password = None
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("type") == "hidden":
            self.hidden[attrs["name"]] = attrs.get("value", "")
        if tag == "input" and attrs.get("type") == "password":
            self.password = attrs


def _callback_form(server, client, client_id, callback):
    verifier = "z" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    params = {"client_id": client_id, "redirect_uri": callback, "response_type": "code",
              "state": "callback-policy-check", "code_challenge": challenge,
              "code_challenge_method": "S256", "scope": "telegram.read", "resource": server.resource}
    return client.get("/oauth/authorize", params=params), verifier, params


def _submit(client, fields):
    fields = dict(fields, password="test-owner-password", allow_read="yes")
    return client.post("/oauth/authorize", data=fields, follow_redirects=False)


def test_authorization_windows_do_not_invalidate_each_other(oauth):
    server, client, _ = oauth
    client_id = register(client)
    first, _, _ = auth_form(server, client, client_id)
    second, _, _ = auth_form(server, client, client_id)
    first["approve"] = "yes"
    second["approve"] = "yes"
    assert _submit(client, first).status_code == 303
    # Consuming one browser authorization must not clear another window's CSRF cookie.
    assert _submit(client, second).status_code == 303


def test_hidden_consent_survives_submit_without_button_submitter(oauth):
    server, client, _ = oauth
    client_id = register(client)
    fields, _, response = auth_form(server, client, client_id)
    # Password managers and requestSubmit() can submit a form without a named button.
    assert fields.get("approve") == "yes"
    assert 'name="approve" value="yes">Подключить' not in response.text
    assert _submit(client, fields).status_code == 303


def test_missing_cookie_is_still_rejected(oauth):
    server, client, _ = oauth
    client_id = register(client)
    fields, _, _ = auth_form(server, client, client_id)
    fields["approve"] = "yes"
    client.cookies.clear()
    assert _submit(client, fields).status_code == 403


def test_foreign_and_null_origins_are_still_rejected(oauth):
    server, client, _ = oauth
    client_id = register(client)
    for origin in ("https://evil.example", "null"):
        fields, _, _ = auth_form(server, client, client_id)
        fields.update(password="test-owner-password", allow_read="yes", approve="yes")
        response = client.post("/oauth/authorize", data=fields,
                               headers={"Origin": origin}, follow_redirects=False)
        assert response.status_code == 403


def test_missing_cookie_returns_password_empty_form_then_retry_completes(oauth):
    server, client, _ = oauth
    client_id = register(client)
    original, verifier, _ = auth_form(server, client, client_id)
    client.cookies.clear()
    denied = _submit(client, original)
    assert denied.status_code == 403
    assert "text/html" in denied.headers["content-type"]
    assert "location" not in denied.headers
    assert "Введите пароль ещё раз" in denied.text
    assert "test-owner-password" not in denied.text
    retry = _FormInputs(denied.text)
    assert retry.password is not None
    assert retry.password.get("value", "") == ""
    for name in ("client_id", "redirect_uri", "response_type", "state", "code_challenge",
                 "code_challenge_method", "resource", "scope"):
        assert retry.hidden[name] == original[name]
    assert retry.hidden["csrf"] != original["csrf"]

    # A rejected submission cannot succeed merely by repeating the stale body.
    assert _submit(client, original).status_code == 403
    completed = _submit(client, retry.hidden)
    assert completed.status_code == 303
    query = parse_qs(urlsplit(completed.headers["location"]).query)
    assert query["state"] == [original["state"]]
    tokens = exchange(server, client, client_id, query["code"][0], verifier)
    assert tokens.status_code == 200
    assert server.verify_token(tokens.json()["access_token"])["client_id"] == client_id


def test_no_selected_permissions_returns_russian_form_without_grant(oauth):
    server, client, _ = oauth
    client_id = register(client)
    fields, _, _ = auth_form(server, client, client_id)
    denied = client.post("/oauth/authorize", data=dict(fields, password="test-owner-password"),
                         follow_redirects=False)
    assert denied.status_code == 403
    assert "location" not in denied.headers
    assert "text/html" in denied.headers["content-type"]
    assert "Выберите" in denied.text and "разрешение" in denied.text
    assert "test-owner-password" not in denied.text
    retry = _FormInputs(denied.text)
    assert retry.password is not None and retry.password.get("value", "") == ""
    assert _submit(client, retry.hidden).status_code == 303


def test_form_preserves_same_origin_post_but_callback_suppresses_referrer(oauth):
    server, client, _ = oauth
    client_id = register(client)
    fields, verifier, form = auth_form(server, client, client_id)
    # A no-referrer policy on a navigational form POST makes Chromium send
    # Origin:null. Preserve the real same origin while the password form submits.
    assert form.headers["referrer-policy"] == "same-origin"
    completed = client.post(
        "/oauth/authorize", data=dict(fields, password="test-owner-password", allow_read="yes"),
        headers={"Origin": server.origin}, follow_redirects=False,
    )
    assert completed.status_code == 303
    # The cross-site OAuth callback must not receive this private form URL.
    assert completed.headers["referrer-policy"] == "no-referrer"
    callback = parse_qs(urlsplit(completed.headers["location"]).query)
    assert callback["state"] == [fields["state"]]
    tokens = exchange(server, client, client_id, callback["code"][0], verifier)
    assert tokens.status_code == 200
    assert server.verify_token(tokens.json()["access_token"])["scopes"] == ["telegram.read"]


@pytest.mark.parametrize(("callback", "expected_origin"), [
    ("https://chatgpt.com/aip/oauth/callback", "https://chatgpt.com"),
    ("https://CHATGPT.COM:443/aip/oauth/callback", "https://chatgpt.com"),
    ("https://пример.рф/aip/oauth/callback", "https://xn--e1afmkfd.xn--p1ai"),
    ("https://[2001:0db8:0000:0000:0000:0000:0000:0001]/callback", "https://[2001:db8::1]"),
    ("https://chatgpt.com/aip/oauth/callback;frame-ancestors=*?next=https://evil.example/;script-src=*",
     "https://chatgpt.com"),
])
def test_csp_allows_validated_callback_origin_without_uri_punctuation(oauth, callback, expected_origin):
    server, client, _ = oauth
    client_id = register(client, redirect=callback)
    response, verifier, _ = _callback_form(server, client, client_id, callback)
    assert response.status_code == 200
    policy = response.headers["content-security-policy"]
    directives = {}
    for serialized in policy.split(";"):
        name, _, value = serialized.strip().partition(" ")
        assert name not in directives
        directives[name] = value
    assert set(directives) == {"default-src", "form-action", "frame-ancestors", "base-uri"}
    assert directives["form-action"].split() == ["'self'", expected_origin]
    assert directives["default-src"] == "'none'"
    assert directives["frame-ancestors"] == "'none'"
    assert "evil.example" not in policy
    assert "?" not in policy

    approved = _submit(client, _FormInputs(response.text).hidden)
    assert approved.status_code == 303
    parsed = urlsplit(approved.headers["location"])
    code = parse_qs(parsed.query)["code"][0]
    token = client.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": client_id,
                       "code": code, "redirect_uri": callback, "resource": server.resource,
                       "code_verifier": verifier})
    assert token.status_code == 200


@pytest.mark.parametrize("callback", [
    "https://chatgpt.com;script-src/callback",
    "https://*.chatgpt.com/callback",
    "https://chatgpt.com,evil.example/callback",
    "https://chatgpt.com'unsafe-inline/callback",
    "https://chatgpt.com%0d%0a/callback",
    "https://[fe80::1%25x;script-src*]/callback",
    "https://[fe80::1%25eth0]/callback",
])
def test_client_registration_rejects_csp_hostname_injection(oauth, callback):
    _, client, _ = oauth
    response = client.post("/oauth/register", json={"redirect_uris": [callback]})
    assert response.status_code == 400
    assert response.json() == {"error": "invalid_client_metadata"}


def test_unregistered_redirect_cannot_add_a_destination_to_form_policy(oauth):
    server, client, _ = oauth
    client_id = register(client, redirect="https://chatgpt.com/aip/oauth/callback")
    response, _, _ = _callback_form(server, client, client_id, "https://evil.example/callback")
    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}
    assert "content-security-policy" not in response.headers
