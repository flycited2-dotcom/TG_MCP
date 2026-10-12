"""Image viewer receives original bytes with renderable, verified MIME headers."""
import io

import pytest
from PIL import Image
from starlette.applications import Starlette
from starlette.testclient import TestClient

from file_downloads import DownloadStore


@pytest.mark.parametrize("format,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_original_raster_can_render_without_reencoding(tmp_path, format, mime):
    original = io.BytesIO()
    Image.new("RGB", (3, 2), "blue").save(original, format=format)
    path = tmp_path / "misleading.dat"
    path.write_bytes(original.getvalue())
    store = DownloadStore("https://telegram.example", tmp_path, tmp_path / "links.sqlite")
    url = store.issue(str(path), "Оригинал.dat")
    with TestClient(Starlette(routes=store.routes)) as client:
        response = client.get(url)
        assert response.status_code == 200
        assert response.content == original.getvalue()
        assert response.headers["content-type"] == mime
        assert response.headers["content-disposition"].startswith("inline;")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        head = client.head(url)
        assert head.headers["content-type"] == mime and not head.content


@pytest.mark.parametrize("payload", [b"<html><script>alert(1)</script></html>",
                                   b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
                                   bytes.fromhex("89504e470d0a1a0a0000")])
def test_active_or_invalid_content_named_png_remains_download_only(tmp_path, payload):
    path = tmp_path / "pretend.png"
    path.write_bytes(payload)
    store = DownloadStore("https://telegram.example", tmp_path, tmp_path / "links.sqlite")
    with TestClient(Starlette(routes=store.routes)) as client:
        response = client.get(store.issue(str(path), "pretend.png"))
        assert response.status_code == 200 and response.content == payload
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["x-content-type-options"] == "nosniff"
