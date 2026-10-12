"""Short-lived, unguessable download links; filesystem paths stay private."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import secrets
import sqlite3
import time
import warnings

from PIL import Image, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route


def _inline_image_type(path: Path) -> str | None:
    """Recognize bounded raster images from bytes, never from the supplied name."""
    try:
        if path.stat().st_size > 50 * 1024 * 1024:
            return None
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path, formats=("PNG", "JPEG", "WEBP")) as image:
                if image.width * image.height > 20_000_000:
                    return None
                media_type = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[image.format]
                image.verify()
                return media_type
    except (OSError, ValueError, SyntaxError, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        return None


class DownloadStore:
    def __init__(self, public_url: str, output_dir: Path, db_path: Path, clock=time.time, *, delete_expired_files=True, route_prefix='/files'):
        self.public_url = public_url.rstrip("/")
        self.output_dir = Path(output_dir).resolve()
        self.db_path = Path(db_path)
        self.clock = clock
        self.delete_expired_files = delete_expired_files
        self.route_prefix = route_prefix

    def _db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.db_path)
        if os.name != "nt":
            os.chmod(self.db_path, 0o600)
        db.execute("CREATE TABLE IF NOT EXISTS downloads (digest TEXT PRIMARY KEY, path TEXT NOT NULL, filename TEXT NOT NULL, expires REAL NOT NULL)")
        expired = db.execute("SELECT path FROM downloads WHERE expires < ?", (self.clock(),)).fetchall()
        db.execute("DELETE FROM downloads WHERE expires < ?", (self.clock(),))
        for (path,) in expired:
            if self.delete_expired_files and db.execute("SELECT 1 FROM downloads WHERE path=?", (path,)).fetchone() is None:
                try:
                    self._within_output(path).unlink()
                except (ValueError, OSError):
                    pass
        db.commit()
        return db

    def prune(self):
        self._db().close()

    def _within_output(self, path: str) -> Path:
        candidate = Path(path).resolve()
        if not candidate.is_relative_to(self.output_dir) or not candidate.is_file():
            raise ValueError("Invalid download path")
        return candidate

    def issue(self, path: str, filename: str) -> str:
        candidate = self._within_output(path)
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._db() as db:
            db.execute("INSERT INTO downloads VALUES (?, ?, ?, ?)",
                       (digest, str(candidate), filename, self.clock() + 600))
        db.close()
        return f"{self.public_url}{self.route_prefix}/{token}"

    async def serve(self, request: Request):
        token = request.path_params.get("ticket", "")
        if len(token) != 43:
            return JSONResponse({"error": "download_expired_or_invalid"}, status_code=404)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._db() as db:
            row = db.execute("SELECT path, filename, expires FROM downloads WHERE digest=?", (digest,)).fetchone()
        db.close()
        if row is None or row[2] < self.clock():
            return JSONResponse({"error": "download_expired_or_invalid"}, status_code=404)
        try:
            path = self._within_output(row[0])
        except ValueError:
            return JSONResponse({"error": "download_expired_or_invalid"}, status_code=404)
        image_type = await run_in_threadpool(_inline_image_type, path)
        return FileResponse(path, filename=row[1], media_type=image_type or "application/octet-stream",
                            content_disposition_type="inline" if image_type else "attachment",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                     "Referrer-Policy": "no-referrer"})

    @property
    def routes(self):
        return [Route(self.route_prefix+"/{ticket}", self.serve, methods=["GET", "HEAD"])]
