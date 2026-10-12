"""Private, bounded file staging with verified bytes and safe signed-URL fetches."""
from __future__ import annotations
import asyncio
import base64
import hashlib
import http.client
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import ssl
import time
from urllib.parse import urlsplit, urljoin

from telegram_backend import BackendError

ID = re.compile(r"^[a-f0-9]{32}$")


class ClosingConnection(sqlite3.Connection):
    def __exit__(self,*args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def clean_name(value):
    name = str(value).replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:160] or "attachment.bin"


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, hostname, address):
        super().__init__(hostname, timeout=30, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class FileStore:
    def __init__(self, root, *, max_bytes=200 * 1024 * 1024, quota=500 * 1024 * 1024, clock=time.time):
        self.root = Path(root)
        self.max_bytes, self.quota, self.clock = max_bytes, quota, clock
        self._lock = asyncio.Lock()
        self.allowed_hosts = tuple(os.environ.get("TG_FILE_DOWNLOAD_HOSTS", "files.oaiusercontent.com,*.oaiusercontent.com,*.blob.core.windows.net").split(","))

    def _db(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.root / "files.sqlite", timeout=10, factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY, filename TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT, expires REAL NOT NULL, state TEXT NOT NULL, received INTEGER NOT NULL DEFAULT 0)")
        db.commit()
        if os.name != "nt":
            os.chmod(self.root / "files.sqlite", 0o600)
        return db

    def path(self, file_id):
        if not isinstance(file_id, str) or not ID.fullmatch(file_id):
            raise BackendError("invalid_file_id", "Use file_id returned by upload_file or get_attachment.")
        path = self.root / (file_id + ".bin")
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            raise BackendError("invalid_file_id", "The file is unavailable.")
        return path

    def prune(self):
        with self._db() as db:
            expired = db.execute("SELECT id FROM files WHERE expires < ?", (self.clock(),)).fetchall()
            for row in expired:
                self.path(row['id']).unlink(missing_ok=True)
            db.execute("DELETE FROM files WHERE expires < ?", (self.clock(),))
        db.close()

    def metadata(self, file_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        db.close()
        if not row or row['state'] != 'ready' or row['expires'] <= self.clock() or not self.path(file_id).is_file():
            raise BackendError("file_not_found", "The staged file expired or is incomplete. Upload or fetch it again.")
        return {"file_id": row['id'], "filename": row['filename'], "mime_type": row['mime'],
                "size_bytes": row['size'], "sha256": row['sha256'], "expires_at": row['expires'],
                "resource_uri": "telegram-file://" + row['id']}

    def verified_path(self, file_id):
        info = self.metadata(file_id)
        path = self.path(file_id)
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if path.stat().st_size != info['size_bytes'] or digest != info['sha256']:
            raise BackendError("file_integrity_error", "The staged bytes changed; upload the original file again.")
        return path

    def _reserve(self, filename, mime, size, digest=None):
        self.prune()
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= self.max_bytes:
            raise BackendError("file_too_large", f"File must contain 1..{self.max_bytes} bytes.")
        if digest is not None and not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise BackendError("invalid_input", "sha256 must be a lowercase hexadecimal SHA-256 digest.")
        file_id = secrets.token_hex(16)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            total = db.execute("SELECT coalesce(sum(size),0) FROM files").fetchone()[0]
            if total + size > self.quota:
                raise BackendError("file_quota_exceeded", "Private staging storage is full; remove staged files or wait for expiry.")
            selected_mime=(mimetypes.guess_type(filename)[0] if not mime or mime=='application/octet-stream' else mime) or 'application/octet-stream'
            selected_mime=selected_mime.split(';')[0].strip().lower()
            if not re.fullmatch(r'[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*',selected_mime):
                raise BackendError('invalid_input','A valid MIME type is required.')
            db.execute("INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, 'uploading', 0)",
                       (file_id, clean_name(filename), selected_mime[:128], size, digest, self.clock()+3600))
        db.close()
        with self.path(file_id).open('xb'):
            pass
        if os.name != 'nt':
            os.chmod(self.path(file_id), 0o600)
        return file_id

    async def begin(self, filename, mime, size, sha256=None):
        async with self._lock:
            return {'upload_id': self._reserve(filename, mime, size, sha256), 'chunk_max_bytes': 768 * 1024,
                    'expires_in_seconds': 3600, 'requires_complete_bytes': True}

    async def append(self, upload_id, offset, base64_data, final=False):
        if not isinstance(base64_data, str) or len(base64_data) > 1024 * 1024:
            raise BackendError('invalid_input', 'Each base64 chunk must contain at most 768 KiB of bytes.')
        try:
            chunk = base64.b64decode(base64_data, validate=True)
        except (ValueError, TypeError):
            raise BackendError('invalid_input', 'Invalid base64 bytes.') from None
        async with self._lock:
            with self._db() as db:
                row = db.execute('SELECT * FROM files WHERE id=?', (upload_id,)).fetchone()
                if not row or row['state'] != 'uploading' or row['expires'] <= self.clock():
                    raise BackendError('upload_expired', 'Begin a new upload.')
                if isinstance(offset, bool) or offset != row['received'] or offset + len(chunk) > row['size']:
                    raise BackendError('upload_offset_mismatch', 'Use the exact received_bytes offset; chunks are never appended twice.')
                with self.path(upload_id).open('ab') as stream:
                    stream.write(chunk)
                received = offset + len(chunk)
                db.execute('UPDATE files SET received=? WHERE id=?', (received, upload_id))
                if final:
                    if received != row['size']:
                        db.commit()
                        raise BackendError('upload_incomplete', 'All declared bytes are required before finishing.')
                    with self.path(upload_id).open('rb') as stream:
                        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                    if row['sha256'] and row['sha256'] != digest:
                        self.path(upload_id).unlink(missing_ok=True)
                        db.execute('DELETE FROM files WHERE id=?', (upload_id,))
                        db.commit()
                        raise BackendError('file_integrity_error', 'Uploaded bytes do not match the declared SHA-256.')
                    db.execute("UPDATE files SET state='ready',sha256=?,expires=? WHERE id=?", (digest, self.clock()+86400, upload_id))
            db.close()
            return self.metadata(upload_id) if final else {'upload_id': upload_id, 'received_bytes': received}

    def _remote(self, url):
        parsed = urlsplit(url)
        host = parsed.hostname or ''
        allowed = any(host == item or (item.startswith('*.') and host.endswith(item[1:]) and host != item[2:]) for item in self.allowed_hosts)
        if (parsed.scheme != 'https' or parsed.port not in (None, 443) or parsed.username or parsed.password
                or not allowed or parsed.fragment or any(c in url for c in ('\\', '\r', '\n'))):
            raise BackendError('file_reference_unavailable', 'A ChatGPT file reference with an authorized HTTPS download_url is required; internal IDs or sandbox paths cannot be downloaded.')
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
            raise BackendError('unsafe_download_address', 'Private or local network download addresses are forbidden.')
        return _PinnedHTTPS(host, sorted(addresses)[0]), (parsed.path or '/') + ('?' + parsed.query if parsed.query else '')

    def _fetch(self, ref):
        url = ref['download_url']
        for hop in range(4):
            conn, target = self._remote(url)
            file_id = None
            try:
                conn.request('GET', target, headers={'Accept': '*/*'})
                response = conn.getresponse()
                if response.status in (301,302,303,307,308):
                    location = response.getheader('Location')
                    if not location:
                        break
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise BackendError('file_reference_expired', 'ChatGPT did not provide downloadable bytes. Refresh the file reference or use the private upload fallback.')
                declared = response.getheader('Content-Length')
                reserve = int(declared) if declared else self.max_bytes
                file_id = self._reserve(ref.get('file_name','attachment.bin'), (response.getheader('Content-Type') if not ref.get('mime_type') or ref.get('mime_type')=='application/octet-stream' else ref['mime_type']), reserve)
                size = 0
                digest = hashlib.sha256()
                with self.path(file_id).open('wb') as stream:
                    while chunk := response.read(512 * 1024):
                        size += len(chunk)
                        if size > reserve or size > self.max_bytes:
                            raise BackendError('file_too_large', 'Downloaded bytes exceed the declared or configured limit.')
                        digest.update(chunk)
                        stream.write(chunk)
                if not size or (declared and size != int(declared)):
                    raise BackendError('file_integrity_error', 'The downloaded file was empty or truncated.')
                with self._db() as db:
                    db.execute("UPDATE files SET size=?,received=?,sha256=?,state='ready',expires=? WHERE id=?", (size,size,digest.hexdigest(),self.clock()+86400,file_id))
                db.close()
                return self.metadata(file_id)
            except BaseException:
                if file_id:
                    self.path(file_id).unlink(missing_ok=True)
                    with self._db() as db:
                        db.execute('DELETE FROM files WHERE id=?',(file_id,))
                    db.close()
                raise
            finally:
                conn.close()
        raise BackendError('file_reference_unavailable', 'Too many redirects or an invalid download URL.')

    async def upload_reference(self, ref):
        async with self._lock:
            try:
                return await asyncio.wait_for(asyncio.to_thread(self._fetch, ref), 180)
            except BackendError:
                raise
            except Exception:
                raise BackendError('file_download_failed', 'The file could not be fetched; obtain a fresh ChatGPT file reference or use bounded byte upload.') from None

    async def import_path(self, source, filename, mime):
        async with self._lock:
            source = Path(source)
            file_id = self._reserve(filename,mime,source.stat().st_size)
            import shutil
            shutil.copyfile(source,self.path(file_id))
            with self.path(file_id).open('rb') as stream:
                digest = hashlib.file_digest(stream,'sha256').hexdigest()
            with self._db() as db:
                db.execute("UPDATE files SET received=size,state='ready',sha256=?,expires=? WHERE id=?",(digest,self.clock()+86400,file_id))
            db.close()
            return self.metadata(file_id)

    async def remove(self, file_id):
        async with self._lock:
            self.metadata(file_id)
            self.path(file_id).unlink(missing_ok=True)
            with self._db() as db:
                db.execute('DELETE FROM files WHERE id=?',(file_id,))
            db.close()
            return {'file_id':file_id,'deleted':True}
