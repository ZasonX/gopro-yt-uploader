import urllib.error
import urllib.request

import pytest

from gopro_uploader.preview import PreviewServer, lrv_path


@pytest.fixture
def server():
    s = PreviewServer()
    yield s
    s.close()


def _get(url, rng=None):
    req = urllib.request.Request(url, headers={"Range": rng} if rng else {})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, dict(r.headers), r.read()


def test_lrv_path_prefers_gopro_proxy(tmp_path):
    (tmp_path / "GX010827.MP4").write_bytes(b"x")
    assert lrv_path(str(tmp_path / "GX010827.MP4")) is None
    (tmp_path / "GL010827.LRV").write_bytes(b"x")
    assert lrv_path(str(tmp_path / "GX010827.MP4")) == str(tmp_path / "GL010827.LRV")
    (tmp_path / "GOPR0123.MP4").write_bytes(b"x")
    (tmp_path / "GOPR0123.LRV").write_bytes(b"x")
    assert lrv_path(str(tmp_path / "GOPR0123.MP4")) == str(tmp_path / "GOPR0123.LRV")


def test_serves_registered_file_with_ranges(tmp_path, server):
    f = tmp_path / "GX010001.MP4"
    data = bytes(range(256)) * 40
    f.write_bytes(data)
    url = server.url_for(str(f))
    assert url == server.url_for(str(f))  # stable key per file

    status, headers, body = _get(url)
    assert status == 200 and body == data and headers["Accept-Ranges"] == "bytes"

    status, headers, body = _get(url, "bytes=100-199")
    assert status == 206 and body == data[100:200]
    assert headers["Content-Range"] == f"bytes 100-199/{len(data)}"

    status, _, body = _get(url, "bytes=10000-")
    assert status == 206 and body == data[10000:]

    status, _, body = _get(url, "bytes=-16")
    assert status == 206 and body == data[-16:]


def test_unregistered_paths_are_not_served(tmp_path, server):
    secret = tmp_path / "token.json"
    secret.write_text("{}")
    server.url_for(str(tmp_path / "other.MP4"))
    for path in ("token.json", "../token.json", str(secret).replace("\\", "/"), ""):
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(server.base_url + path)
        assert e.value.code == 404
