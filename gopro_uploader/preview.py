"""Local video server for the preview player.

Serves only files that were explicitly registered (scanned clips or their
.LRV proxies), each under a random key, on 127.0.0.1. Supports HTTP Range so
the player can seek without reading the whole file from the HDD.
"""
from __future__ import annotations

import os
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_RANGE = re.compile(r"bytes=(\d*)-(\d*)$")
_BLOCK = 1024 * 1024


def lrv_path(mp4_path: str) -> str | None:
    """GoPro's low-res H.264 proxy for a clip, if it is next to it.

    GX010827.MP4 -> GL010827.LRV (HERO6+); GOPR0123.MP4 / GP010123.MP4 -> same stem .LRV.
    """
    folder, name = os.path.split(mp4_path)
    stem = os.path.splitext(name)[0]
    candidates = [stem + ".LRV"]
    if stem[:2].upper() == "GX":
        candidates.insert(0, "GL" + stem[2:] + ".LRV")
    for c in candidates:
        p = os.path.join(folder, c)
        if os.path.isfile(p):
            return p
    return None


class PreviewServer:
    def __init__(self):
        self._files: dict[str, str] = {}
        self._keys: dict[str, str] = {}
        self._lock = threading.Lock()
        files = self._files

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep the app log clean
                pass

            def do_GET(self):
                self._serve(body=True)

            def do_HEAD(self):
                self._serve(body=False)

            def _serve(self, body: bool):
                path = files.get(self.path.lstrip("/"))
                if not path or not os.path.isfile(path):
                    self.send_error(404)
                    return
                size = os.path.getsize(path)
                start, end = 0, size - 1
                m = _RANGE.match(self.headers.get("Range", ""))
                if m and (m.group(1) or m.group(2)):
                    if m.group(1):
                        start = int(m.group(1))
                        end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                    else:  # suffix range: last N bytes
                        start = max(size - int(m.group(2)), 0)
                    if start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.end_headers()
                        return
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                else:
                    self.send_response(200)
                length = end - start + 1
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                self.end_headers()
                if not body:
                    return
                try:
                    with open(path, "rb") as f:
                        f.seek(start)
                        left = length
                        while left > 0:
                            chunk = f.read(min(_BLOCK, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                except (ConnectionError, OSError):
                    pass  # player seeked or closed; normal

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/"

    def url_for(self, path: str) -> str:
        path = os.path.abspath(path)
        with self._lock:
            key = self._keys.get(path)
            if key is None:
                key = secrets.token_urlsafe(16)
                self._keys[path] = key
                self._files[key] = path
        return self.base_url + key

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()
