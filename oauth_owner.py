"""Single-owner OAuth authorization for a remotely hosted Telegram MCP server.

Public clients register without a secret. Each authorization requires the owner
password and PKCE. Passwords, authorization codes and tokens must never be logged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

SCOPES = frozenset({"telegram.read", "telegram.send", "telegram.manage"})
MAX_BODY = 8192
CODE_TTL = 300
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
FORM_TTL = 600
_PKCE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_VERIFIER = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{32}$")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _https_origin(value: str) -> str:
    """Build a CSP host source from a URL without interpolating URL punctuation."""
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if ":" in host:
        if "%" in host:
            raise ValueError("Scoped IPv6 is not a public HTTPS origin")
        host = "[" + str(ipaddress.IPv6Address(host)) + "]"
    else:
        host = host.encode("idna").decode("ascii").lower()
        labels = host.rstrip(".").split(".")
        if len(host) > 253 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in labels
        ):
            raise ValueError("Invalid HTTPS hostname")
    if parsed.scheme != "https" or parsed.port not in (None, 443):
        raise ValueError("Invalid HTTPS origin")
    return "https://" + host


def _safe_https(value: str) -> bool:
    if not isinstance(value, str) or len(value) > 2048:
        return False
    if value != value.strip() or any(ord(c) < 33 or ord(c) == 127 for c in value):
        return False
    try:
        parsed = urlsplit(value)
        _https_origin(value)
        return bool(
            parsed.scheme == "https" and parsed.hostname and parsed.netloc
            and parsed.username is None and parsed.password is None
            and not parsed.fragment and parsed.port in (None, 443)
            and "\\" not in value
        )
    except (ValueError, UnicodeError):
        return False


_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


def _loopback(value: str) -> bool:
    """RFC 8252 native-app callback (Claude Code, Codex): http on the local machine only."""
    if not isinstance(value, str) or len(value) > 2048:
        return False
    if value != value.strip() or any(ord(c) < 33 or ord(c) == 127 for c in value):
        return False
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        return False
    return bool(parsed.scheme == "http" and parsed.hostname in _LOOPBACK
                and parsed.username is None and parsed.password is None
                and not parsed.fragment and "\\" not in value)


def _safe_redirect(value: str) -> bool:
    return _safe_https(value) or _loopback(value)


def _redirect_origin(value: str) -> str:
    """CSP host source for a validated callback."""
    if not _loopback(value):
        return _https_origin(value)
    parsed = urlsplit(value)
    host = "[::1]" if parsed.hostname == "::1" else parsed.hostname
    return "http://" + host + (f":{parsed.port}" if parsed.port else "")


def _redirect_registered(value: str, registered: list) -> bool:
    """Exact match; loopback callbacks may differ only in port (RFC 8252 section 7.3)."""
    if value in registered:
        return True
    if not _loopback(value):
        return False
    wanted = urlsplit(value)._replace(netloc=urlsplit(value).hostname or "")
    for uri in registered:
        if isinstance(uri, str) and _loopback(uri):
            candidate = urlsplit(uri)
            if candidate._replace(netloc=candidate.hostname or "") == wanted:
                return True
    return False


class OAuthError(Exception):
    def __init__(self, code: str = "invalid_request", status: int = 400, reason: str | None = None):
        self.code, self.status = code, status
        self.reason = reason


class OwnerOAuth:
    def __init__(self, public_url: str, config_path: str | Path, db_path: str | Path):
        self.public_url = public_url.rstrip("/")
        if not _safe_https(self.public_url):
            raise ValueError("TG_PUBLIC_URL must be a HTTPS origin")
        parts = urlsplit(self.public_url)
        if parts.query or any(segment in (".", "..") for segment in parts.path.split("/")):
            raise ValueError("TG_PUBLIC_URL must be a HTTPS base URL without query or traversal")
        self.origin = _https_origin(self.public_url)
        self.cookie_path = parts.path + "/oauth/authorize"
        self.resource = self.public_url + "/mcp"
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self._salt = base64.b64decode(config["password_salt"], validate=True)
        self._password_hash = base64.b64decode(config["password_hash"], validate=True)
        if len(self._salt) < 16 or len(self._password_hash) != 64:
            raise ValueError("Invalid owner password configuration")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._attempts: dict[str, list[float]] = {}
        self._global_attempts: list[float] = []
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS clients (
                    client_id TEXT PRIMARY KEY, redirect_uris TEXT NOT NULL,
                    client_name TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS codes (
                    digest TEXT PRIMARY KEY, client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL, challenge TEXT NOT NULL,
                    resource TEXT NOT NULL, scopes TEXT NOT NULL, expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS access_tokens (
                    digest TEXT PRIMARY KEY, client_id TEXT NOT NULL,
                    resource TEXT NOT NULL, scopes TEXT NOT NULL,
                    expires_at REAL NOT NULL, family TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS refresh_tokens (
                    digest TEXT PRIMARY KEY, client_id TEXT NOT NULL,
                    resource TEXT NOT NULL, scopes TEXT NOT NULL,
                    expires_at REAL NOT NULL, family TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)",
                       ("csrf_secret", _b64(secrets.token_bytes(32))))
            self._csrf_secret = db.execute(
                "SELECT value FROM settings WHERE name='csrf_secret'"
            ).fetchone()[0].encode("ascii")
        os.chmod(self.db_path, 0o600)
        self.routes = [
            Route("/.well-known/oauth-protected-resource", self.protected_resource),
            Route("/.well-known/oauth-protected-resource/mcp", self.protected_resource),
            Route("/.well-known/oauth-authorization-server", self.authorization_server),
            Route("/oauth/register", self.register, methods=["POST"]),
            Route("/oauth/authorize", self.authorize_get, methods=["GET"]),
            Route("/oauth/authorize", self.authorize_post, methods=["POST"]),
            Route("/oauth/token", self.token, methods=["POST"]),
            Route("/oauth/revoke", self.revoke, methods=["POST"]),
        ]

    @classmethod
    def from_env(cls) -> "OwnerOAuth":
        return cls(os.environ["TG_PUBLIC_URL"], os.environ["TG_OWNER_CONFIG"],
                   os.environ.get("TG_AUTH_DB", "/data/auth.sqlite"))

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _json(data: dict, status: int = 200) -> JSONResponse:
        return JSONResponse(data, status_code=status, headers={
            "Cache-Control": "no-store", "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        })

    def _error(self, error: OAuthError) -> JSONResponse:
        return self._json({"error": error.code}, error.status)

    async def _body(self, request: Request, as_json: bool = False) -> dict:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                raise OAuthError("invalid_request", 413)
        try:
            if as_json:
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise ValueError()
                return data
            if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
                raise ValueError()
            parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=25)
            if any(len(values) != 1 for values in parsed.values()):
                raise ValueError()
            return {key: values[0] for key, values in parsed.items()}
        except (ValueError, UnicodeError):
            raise OAuthError() from None

    async def protected_resource(self, request: Request):
        return self._json({"resource": self.resource,
                           "authorization_servers": [self.public_url],
                           "scopes_supported": sorted(SCOPES),
                           "bearer_methods_supported": ["header"]})

    async def authorization_server(self, request: Request):
        return self._json({
            "issuer": self.public_url,
            "authorization_endpoint": self.public_url + "/oauth/authorize",
            "token_endpoint": self.public_url + "/oauth/token",
            "registration_endpoint": self.public_url + "/oauth/register",
            "revocation_endpoint": self.public_url + "/oauth/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": sorted(SCOPES),
            "authorization_response_iss_parameter_supported": True,
        })

    async def register(self, request: Request):
        try:
            data = await self._body(request, True)
            redirects = data.get("redirect_uris")
            if (not isinstance(redirects, list) or not 1 <= len(redirects) <= 10
                    or not all(_safe_redirect(uri) for uri in redirects)
                    or data.get("token_endpoint_auth_method", "none") != "none"
                    or data.get("grant_types", ["authorization_code", "refresh_token"])
                       not in (["authorization_code"], ["authorization_code", "refresh_token"],
                               ["refresh_token", "authorization_code"])
                    or data.get("response_types", ["code"]) != ["code"]):
                raise OAuthError("invalid_client_metadata")
            name = data.get("client_name", "MCP client")
            if not isinstance(name, str) or not 1 <= len(name) <= 200:
                raise OAuthError("invalid_client_metadata")
            client_id = secrets.token_urlsafe(24)
            with self._lock, self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT count(*) FROM clients").fetchone()[0] >= 1000:
                    raise OAuthError("temporarily_unavailable", 503)
                db.execute("INSERT INTO clients VALUES (?, ?, ?, ?)",
                           (client_id, json.dumps(redirects), name, time.time()))
            return self._json({"client_id": client_id, "client_id_issued_at": int(time.time()),
                               "client_name": name, "redirect_uris": redirects,
                               "token_endpoint_auth_method": "none",
                               "grant_types": ["authorization_code", "refresh_token"],
                               "response_types": ["code"]}, 201)
        except OAuthError as error:
            return self._error(error)

    def _auth_request(self, data: dict) -> tuple[dict, str]:
        # RFC 8707 resource is optional for clients; this server has exactly one resource.
        data = {**data, "resource": data.get("resource") or self.resource}
        required = ("client_id", "redirect_uri", "response_type", "state", "code_challenge",
                    "code_challenge_method", "resource")
        if any(not isinstance(data.get(key), str) or not data[key] for key in required):
            raise OAuthError()
        if (data["response_type"] != "code" or data["code_challenge_method"] != "S256"
                or not _PKCE.fullmatch(data["code_challenge"])
                or data["resource"] != self.resource or len(data["state"]) > 2048):
            raise OAuthError()
        scopes = data.get("scope", "telegram.read").split()
        if not scopes or len(scopes) != len(set(scopes)) or not set(scopes) <= SCOPES:
            raise OAuthError("invalid_scope")
        with self._db() as db:
            client = db.execute("SELECT * FROM clients WHERE client_id=?", (data["client_id"],)).fetchone()
        if (client is None or not _safe_redirect(data["redirect_uri"])
                or not _redirect_registered(data["redirect_uri"], json.loads(client["redirect_uris"]))):
            raise OAuthError()
        validated = {key: data[key] for key in required}
        validated["scope"] = " ".join(sorted(scopes))
        return validated, client["client_name"]

    def _csrf(self, data: dict, nonce: str) -> str:
        payload = _b64(json.dumps({"exp": int(time.time()) + FORM_TTL,
                                  "request": _digest(json.dumps(data, sort_keys=True)),
                                  "nonce": nonce}, separators=(",", ":")).encode())
        return payload + "." + _b64(hmac.new(self._csrf_secret, payload.encode(), hashlib.sha256).digest())

    def _check_csrf(self, request: Request, data: dict, token: str) -> str:
        reason = "csrf_format"
        try:
            payload, signature = token.split(".")
            expected = _b64(hmac.new(self._csrf_secret, payload.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(signature, expected):
                reason = "csrf_signature"
                raise ValueError()
            obj = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if not isinstance(obj["nonce"], str) or not _NONCE.fullmatch(obj["nonce"]):
                raise ValueError()
            if obj["exp"] < time.time() or obj["exp"] > time.time() + FORM_TTL + 5:
                reason = "csrf_expired"
                raise ValueError()
            cookie_name = "tg_oauth_csrf_" + obj["nonce"]
            cookie = request.cookies.get(cookie_name, "")
            if not cookie:
                reason = "csrf_cookie_missing"
                raise ValueError()
            if not hmac.compare_digest(obj["nonce"], cookie):
                reason = "csrf_cookie_mismatch"
                raise ValueError()
            if obj["request"] != _digest(json.dumps(data, sort_keys=True)):
                reason = "csrf_request_mismatch"
                raise ValueError()
            origin = request.headers.get("origin")
            if origin and origin != self.origin:
                reason = "csrf_origin_null" if origin == "null" else "csrf_origin_mismatch"
                raise ValueError()
            return cookie_name
        except (ValueError, KeyError, TypeError):
            raise OAuthError("access_denied", 403, reason) from None

    def _form(self, data: dict, client_name: str, failed: bool = False,
              *, error_message: str | None = None, status_code: int | None = None):
        escape = lambda value: html.escape(value, quote=True)
        # Chromium applies form-action to 303 redirect destinations too.
        # Only the validated, registered callback origin may receive that redirect.
        callback_origin = _redirect_origin(data["redirect_uri"])
        nonce = secrets.token_urlsafe(24)
        hidden = "".join(f'<input type="hidden" name="{escape(key)}" value="{escape(value)}">'
                         for key, value in data.items())
        hidden += f'<input type="hidden" name="csrf" value="{escape(self._csrf(data, nonce))}">'
        hidden += '<input type="hidden" name="approve" value="yes">'
        permissions = ('<label><input type="checkbox" name="allow_read" value="yes" checked>'
                       ' Читать доступные чаты, контакты и файлы Telegram</label><br>') if "telegram.read" in data["scope"].split() else ""
        if "telegram.send" in data["scope"].split():
            permissions += ('<label><input type="checkbox" name="allow_send" value="yes">'
                            ' Отправлять сообщения от моего имени</label><br>')
        if "telegram.manage" in data["scope"].split():
            permissions += ('<label><input type="checkbox" name="allow_manage" value="yes">'
                            ' Изменять контакты, группы, каналы и права участников</label><br>')
        if failed:
            error_message = "Неверный пароль. Попробуйте ещё раз."
        message = f'<p role="alert">{escape(error_message)}</p>' if error_message else ""
        body = ('<!doctype html><html lang="ru"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>Подключение личного Telegram</title></head><body>'
                '<h1>Подключение личного Telegram</h1>'
                f'<p>Приложение: {escape(client_name)}</p>'
                f'<p>Адрес возврата: {escape(data["redirect_uri"])}</p>'
                '<p>Подтвердите подключение, которое вы начали в ChatGPT, Claude или Codex.</p>'
                + message + f'<form method="post" action="{escape(self.public_url + "/oauth/authorize")}">' + hidden + permissions
                + '<p><label>Личный пароль подключения <input type="password" name="password" '
                  'autocomplete="current-password" required maxlength="1024"></label></p>'
                  '<button type="submit">Подключить</button>'
                  '</form></body></html>')
        response = HTMLResponse(body, status_code=status_code if status_code is not None else 401 if failed else 200, headers={
            # no-referrer makes browsers send Origin: null on HTML form POSTs.
            # same-origin retains our own origin and hides it from other sites.
            "Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "same-origin",
            "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                "default-src 'none'; form-action 'self' " + callback_origin
                + "; frame-ancestors 'none'; base-uri 'none'"
            ),
        })
        response.set_cookie("tg_oauth_csrf_" + nonce, nonce, max_age=FORM_TTL,
                            secure=True, httponly=True, samesite="lax", path=self.cookie_path)
        return response

    async def authorize_get(self, request: Request):
        try:
            if len(request.url.query.encode()) > MAX_BODY:
                raise OAuthError("invalid_request", 413)
            values = parse_qs(request.url.query, keep_blank_values=True, max_num_fields=25)
            if any(len(value) != 1 for value in values.values()):
                raise OAuthError()
            data, name = self._auth_request({key: value[0] for key, value in values.items()})
            return self._form(data, name)
        except (OAuthError, ValueError) as error:
            return self._error(error if isinstance(error, OAuthError) else OAuthError())

    def _rate_limit(self, request: Request) -> None:
        now = time.time()
        ip = request.client.host if request.client else "unknown"
        with self._lock:
            self._global_attempts = [stamp for stamp in self._global_attempts if stamp > now - 60]
            # Bound memory even if a proxy exposes many client IP addresses.
            self._attempts = {key: [stamp for stamp in stamps if stamp > now - 60]
                              for key, stamps in self._attempts.items() if stamps and stamps[-1] > now - 60}
            recent = self._attempts.setdefault(ip, [])
            if len(recent) >= 5 or len(self._global_attempts) >= 20:
                raise OAuthError("temporarily_unavailable", 429)
            recent.append(now)
            self._global_attempts.append(now)

    async def authorize_post(self, request: Request):
        data = None
        try:
            supplied = await self._body(request)
            data, name = self._auth_request(supplied)
            csrf_cookie = self._check_csrf(request, data, supplied.get("csrf", ""))
            self._rate_limit(request)
            password = supplied.get("password", "")
            if not isinstance(password, str) or len(password) > 1024:
                raise OAuthError("access_denied", 403, "password_format")
            calculated = hashlib.scrypt(password.encode(), salt=self._salt, n=16384, r=8, p=1, dklen=64)
            if not hmac.compare_digest(calculated, self._password_hash):
                return self._form(data, name, failed=True)
            if supplied.get("approve") != "yes":
                raise OAuthError("access_denied", 403, "approve_missing")
            scopes = []
            if "telegram.read" in data["scope"].split() and supplied.get("allow_read") == "yes":
                scopes.append("telegram.read")
            if "telegram.send" in data["scope"].split() and supplied.get("allow_send") == "yes":
                scopes.append("telegram.send")
            if "telegram.manage" in data["scope"].split() and supplied.get("allow_manage") == "yes":
                scopes.append("telegram.manage")
            if not scopes:
                raise OAuthError("access_denied", 403, "scopes_empty")
            if 'telegram.read' not in scopes:
                raise OAuthError('access_denied', 403, 'read_scope_required')
            code = secrets.token_urlsafe(32)
            with self._lock, self._db() as db:
                db.execute("DELETE FROM codes WHERE expires_at < ?", (time.time(),))
                db.execute("INSERT INTO codes VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (_digest(code), data["client_id"], data["redirect_uri"],
                            data["code_challenge"], self.resource, " ".join(scopes), time.time() + CODE_TTL))
            target = urlsplit(data["redirect_uri"])
            query = target.query + ("&" if target.query else "") + urlencode(
                {"code": code, "state": data["state"], "iss": self.public_url})
            response = RedirectResponse(urlunsplit(target._replace(query=query)), status_code=303,
                                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
            response.delete_cookie(csrf_cookie, path=self.cookie_path, secure=True,
                                   httponly=True, samesite="lax")
            logging.getLogger("personal_telegram.oauth").info("OAuth callback issued")
            return response
        except OAuthError as error:
            if error.reason:
                # Only fixed diagnostic categories: never request values or exception text.
                logging.getLogger("personal_telegram.oauth").warning("OAuth authorization rejected: %s", error.reason)
            if data is not None and error.code == "access_denied":
                message = "Окно подключения устарело. Введите пароль ещё раз и нажмите «Подключить»."
                if error.reason in {"csrf_origin_null", "csrf_origin_mismatch"}:
                    message = "Окно входа отправило запрос с другого адреса. Повторите подключение из того же приложения (ChatGPT, Claude или Codex)."
                elif error.reason in {"scopes_empty", "read_scope_required"}:
                    message = "Выберите разрешение читать Telegram, затем повторите подключение."
                return self._form(data, name, error_message=message, status_code=403)
            return self._error(error)

    def _issue_tokens(self, db: sqlite3.Connection, client_id: str, resource: str,
                      scopes: str, family: str, refresh_deadline: float):
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        now = time.time()
        db.execute("DELETE FROM access_tokens WHERE expires_at < ?", (now,))
        db.execute("DELETE FROM refresh_tokens WHERE expires_at < ?", (now,))
        db.execute("INSERT INTO access_tokens VALUES (?, ?, ?, ?, ?, ?)",
                   (_digest(access), client_id, resource, scopes, now + ACCESS_TTL, family))
        db.execute("INSERT INTO refresh_tokens VALUES (?, ?, ?, ?, ?, ?)",
                   (_digest(refresh), client_id, resource, scopes, refresh_deadline, family))
        return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL,
                "refresh_token": refresh, "scope": scopes}

    async def token(self, request: Request):
        try:
            data = await self._body(request)
            data.setdefault("resource", self.resource)
            if data.get("resource") != self.resource or not data.get("client_id"):
                raise OAuthError("invalid_grant")
            now = time.time()
            with self._lock, self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                if data.get("grant_type") == "authorization_code":
                    verifier = data.get("code_verifier", "")
                    row = db.execute("SELECT * FROM codes WHERE digest=?",
                                     (_digest(data.get("code", "")),)).fetchone()
                    if (row is None or row["expires_at"] <= now
                            or row["client_id"] != data["client_id"]
                            or row["redirect_uri"] != data.get("redirect_uri")
                            or row["resource"] != data["resource"]
                            or not _VERIFIER.fullmatch(verifier)
                            or not hmac.compare_digest(row["challenge"], _b64(hashlib.sha256(verifier.encode()).digest()))):
                        raise OAuthError("invalid_grant")
                    db.execute("DELETE FROM codes WHERE digest=?", (row["digest"],))
                    tokens = self._issue_tokens(db, row["client_id"], row["resource"],
                                                row["scopes"], secrets.token_urlsafe(24), now + REFRESH_TTL)
                elif data.get("grant_type") == "refresh_token":
                    row = db.execute("SELECT * FROM refresh_tokens WHERE digest=?",
                                     (_digest(data.get("refresh_token", "")),)).fetchone()
                    if (row is None or row["expires_at"] <= now
                            or row["client_id"] != data["client_id"] or row["resource"] != data["resource"]):
                        raise OAuthError("invalid_grant")
                    requested = data.get("scope", row["scopes"]).split()
                    if not requested or not set(requested) <= set(row["scopes"].split()):
                        raise OAuthError("invalid_scope")
                    db.execute("DELETE FROM refresh_tokens WHERE digest=?", (row["digest"],))
                    db.execute("DELETE FROM access_tokens WHERE family=?", (row["family"],))
                    tokens = self._issue_tokens(db, row["client_id"], row["resource"],
                                                " ".join(sorted(set(requested))), row["family"], row["expires_at"])
                else:
                    raise OAuthError("unsupported_grant_type")
            logging.getLogger("personal_telegram.oauth").info("OAuth token issued")
            return self._json(tokens)
        except OAuthError as error:
            logging.getLogger("personal_telegram.oauth").warning("OAuth token rejected: %s", error.code)
            return self._error(error)

    def verify_token(self, token: str) -> dict | None:
        if not isinstance(token, str) or not token or len(token) > 512:
            return None
        with self._db() as db:
            row = db.execute("SELECT * FROM access_tokens WHERE digest=?", (_digest(token),)).fetchone()
        if row is None or row["expires_at"] <= time.time() or row["resource"] != self.resource:
            return None
        return {"client_id": row["client_id"], "scopes": row["scopes"].split(),
                "resource": row["resource"], "expires_at": row["expires_at"]}

    async def revoke(self, request: Request):
        try:
            data = await self._body(request)
            if not data.get("client_id") or not data.get("token"):
                raise OAuthError()
            digest = _digest(data["token"])
            with self._lock, self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                for table in ("access_tokens", "refresh_tokens"):
                    row = db.execute(f"SELECT family FROM {table} WHERE digest=? AND client_id=?",
                                     (digest, data["client_id"])).fetchone()
                    if row is not None:
                        db.execute("DELETE FROM access_tokens WHERE family=?", (row["family"],))
                        db.execute("DELETE FROM refresh_tokens WHERE family=?", (row["family"],))
            return self._json({})
        except OAuthError as error:
            return self._error(error)
