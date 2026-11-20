"""Jasna 下载：发布清单、真实 HTTP 续传/回退和下载器调用。"""

import hashlib
import importlib.util
import io
import json
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import raises_rendered

TOOLS = Path(__file__).resolve().parents[1] / "tools"
DATA = b"jasna test archive\n" * 10000


@pytest.fixture
def module(monkeypatch):
    monkeypatch.syspath_prepend(str(TOOLS))
    spec = importlib.util.spec_from_file_location("download_jasna", TOOLS / "download_jasna.py")
    loaded = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "download_jasna", loaded)
    spec.loader.exec_module(loaded)
    for key in list(loaded.os.environ):
        if key.startswith("VIDEO_TURBO_JASNA_"):
            monkeypatch.delenv(key)
    return loaded


def release():
    assets = []
    for index in range(3):
        name = f"jasna-linux-nvidia-0.12.2.tar.zst.part{index:03}"
        assets.append({
            "name": name, "size": len(DATA),
            "browser_download_url": f"https://github.com/Kruk2/jasna/releases/download/v0.12.2/{name}",
            "digest": "sha256:" + hashlib.sha256(DATA).hexdigest(),
        })
    return {"tag_name": "v0.12.2", "assets": assets}


def asset(module, url="file"):
    return module.Asset("test.part000", url, len(DATA), hashlib.sha256(DATA).hexdigest())


def test_release_selects_only_current_linux_nvidia_parts_in_order(module):
    payload = release()
    payload["assets"].reverse()
    payload["assets"].append({"name": "jasna-linux-amd-0.12.2.tar.zst.part000"})
    payload["assets"].append({"name": "jasna-linux-nvidia-0.11.0.tar.zst.part000"})
    assert [item.name[-3:] for item in module.release_assets(payload)] == ["000", "001", "002"]


@pytest.mark.parametrize("problem", ["gap", "duplicate", "url", "size", "digest", "tag"])
def test_release_rejects_incomplete_or_invalid_metadata(module, problem):
    payload = release()
    if problem == "gap":
        payload["assets"].pop(1)
    elif problem == "duplicate":
        payload["assets"].append(payload["assets"][0])
    elif problem == "tag":
        payload["tag_name"] = "../../bad"
    else:
        key, value = {
            "url": ("browser_download_url", "https://unexpected.example/file"),
            "size": ("size", 0), "digest": ("digest", "md5:bad"),
        }[problem]
        payload["assets"][0][key] = value
    with pytest.raises(ValueError):
        module.release_assets(payload)


def test_actual_asset_is_probed_and_sources_are_ranked(module, monkeypatch):
    item = module.release_assets(release())[0]
    calls = []

    def probe(url, size):
        calls.append((url, size))
        return None if "ghfast" in url else 0.1 if "gh-proxy" in url else 1.0

    monkeypatch.setattr(module, "probe", probe)
    assert module.rank_sources(item) == ["https://gh-proxy.com/", "", "https://ghfast.top/"]
    assert len(calls) == 3
    assert all(url.endswith(item.url) and size == item.size for url, size in calls)
    calls.clear()
    assert module.rank_sources(item, "direct") == [""]
    assert module.rank_sources(item, "https://custom.example/") == ["https://custom.example/", ""]
    assert not calls


@pytest.fixture
def server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Range")))
            if self.path.startswith("/fail/"):
                self.send_error(503)
                return
            if self.path == "/html":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(DATA)))
                self.end_headers()
                return
            first, last = 0, len(DATA) - 1
            requested = self.headers.get("Range")
            if requested:
                first_text, last_text = requested.removeprefix("bytes=").split("-")
                first = int(first_text)
                last = int(last_text) if last_text else last
            body = DATA[first:last + 1]
            self.send_response(206 if requested else 200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            if requested:
                self.send_header("Content-Range", f"bytes {first}-{last}/{len(DATA)}")
            self.end_headers()
            try:
                if self.path.startswith(("/throttle/", "/slow/")):
                    slow = self.path.startswith("/slow/")
                    chunk_size = 512 if slow else 64 * 1024
                    for offset in range(0, len(body), chunk_size):
                        self.wfile.write(body[offset:offset + chunk_size])
                        time.sleep(0.1 if slow else 0.005)
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", requests
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def test_probe_checks_actual_http_range_and_rejects_html(module, server):
    url, requests = server
    assert module.probe(url + "/ok/file", len(DATA)) is not None
    assert requests[-1][1] == f"bytes=0-{len(DATA) - 1}"
    assert module.probe(url + "/html", len(DATA)) is None


@pytest.mark.skipif(not shutil.which("wget"), reason="需要 wget 做真实 HTTP 续传测试")
@pytest.mark.parametrize("initial", [b"", DATA[:12345], b"x" * len(DATA)],
                         ids=["new", "partial", "corrupted"])
def test_wget_download_resumes_and_repairs_corruption(module, tmp_path, server, initial):
    base, requests = server
    item = asset(module)
    path = tmp_path / item.name
    path.write_bytes(initial)
    result = module.download_asset(item, tmp_path, [base + "/ok/"], connections=4,
                                   min_speed=0, slow_seconds=5, aria2=None)
    assert result.read_bytes() == DATA
    if initial == DATA[:12345]:
        assert any(header == "bytes=12345-" for _, header in requests)


@pytest.mark.skipif(not shutil.which("wget"), reason="需要 wget 做真实 HTTP 回退测试")
def test_wget_http_failure_falls_back_to_another_source(module, tmp_path, server):
    base, requests = server
    path = module.download_asset(asset(module), tmp_path, [base + "/fail/", base + "/ok/"],
                                 connections=4, min_speed=0, slow_seconds=5, aria2=None)
    assert path.read_bytes() == DATA
    assert any(path.startswith("/fail/") for path, _ in requests)
    assert any(path.startswith("/ok/") for path, _ in requests)


def test_aria2_keeps_segment_control_when_switching_sources(module, tmp_path, monkeypatch):
    item = asset(module)
    path = tmp_path / item.name
    control = path.with_name(path.name + ".aria2")
    calls = []

    def run(command, **_):
        calls.append(command)
        if len(calls) == 1:
            path.write_bytes(b"partial")
            control.write_bytes(b"segment bitmap")
            return SimpleNamespace(returncode=1)
        assert control.read_bytes() == b"segment bitmap"
        path.write_bytes(DATA)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", run)
    result = module.download_asset(item, tmp_path, ["https://first/", "https://second/"],
                                   connections=4, min_speed=131072, slow_seconds=30, aria2="aria2c")
    assert result.read_bytes() == DATA and not control.exists()
    assert calls[0][-1] == "https://first/file" and calls[1][-1] == "https://second/file"
    assert "--max-connection-per-server=4" in calls[0]
    assert "--split=4" in calls[0] and "--lowest-speed-limit=32768" in calls[0]
    assert f"--checksum=sha-256={item.sha256}" in calls[0]


def test_checksum_failure_never_returns_a_downloaded_path(module, tmp_path, monkeypatch):
    item = asset(module)
    calls = []

    def run(command, **_):
        calls.append(command)
        (tmp_path / item.name).write_bytes(b"x" * len(DATA))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", run)
    with raises_rendered(RuntimeError, "所有下载地址都失败了"):
        module.download_asset(item, tmp_path, ["one/", "two/"], connections=4,
                              min_speed=0, slow_seconds=5, aria2="aria2c")
    assert len(calls) == 2
    assert not (tmp_path / item.name).exists()


def test_verified_cache_skips_probes_and_downloads(module, tmp_path, monkeypatch, capsys):
    payload = release()
    for item in payload["assets"]:
        (tmp_path / item["name"]).write_bytes(DATA)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, "argv", ["download_jasna.py", "--output-dir", str(tmp_path)])
    monkeypatch.setattr(module, "rank_sources", lambda *_: pytest.fail("cache must skip probes"))
    monkeypatch.setattr(module.shutil, "which", lambda *_: None)
    module.main()
    assert capsys.readouterr().out.splitlines() == [str(tmp_path / a["name"]) for a in payload["assets"]]


def test_cli_limits_parallelism_and_downloads_two_parts_at_once(module, tmp_path, monkeypatch, capsys):
    payload = release()
    barrier = threading.Barrier(2)
    active = 0
    peak = 0
    lock = threading.Lock()

    def download(item, directory, sources, **options):
        nonlocal active, peak
        assert options["connections"] == 4
        with lock:
            active += 1
            peak = max(peak, active)
        if not item.name.endswith("002"):
            barrier.wait(timeout=3)
        with lock:
            active -= 1
        return directory / item.name

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, "argv", ["download_jasna.py", "--output-dir", str(tmp_path)])
    monkeypatch.setattr(module, "rank_sources", lambda *_: [""])
    monkeypatch.setattr(module.shutil, "which", lambda *_: "/bin/aria2c")
    monkeypatch.setattr(module, "download_asset", download)
    module.main()
    assert peak == 2
    assert len(capsys.readouterr().out.splitlines()) == 3


def test_rejects_32_connections_before_downloading(module, monkeypatch):
    monkeypatch.setenv("VIDEO_TURBO_JASNA_CONNECTIONS", "32")
    with raises_rendered(ValueError, "1–16"):
        module.env_int("VIDEO_TURBO_JASNA_CONNECTIONS", 4, 16)


def test_wget_slow_window_terminates_process_and_preserves_partial(module, tmp_path, monkeypatch):
    path = tmp_path / "partial"
    path.write_bytes(b"keep me")
    process = SimpleNamespace(returncode=None, poll=lambda: None)
    terminated = []
    process.terminate = lambda: terminated.append(True)
    process.wait = lambda **_: None
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_, **__: process)
    times = iter([0, 30])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(module.time, "sleep", lambda *_: None)
    assert module.wget_download(["wget"], path, 131072, 30) == 1
    assert terminated and path.read_bytes() == b"keep me"


def test_legacy_release_without_digest_and_aria2_control_is_not_a_complete_file(module, tmp_path):
    payload = release()
    payload["assets"][0]["digest"] = None
    item = module.release_assets(payload)[0]
    path = tmp_path / item.name
    path.write_bytes(DATA)
    assert module.valid_file(path, item)
    path.with_name(path.name + ".aria2").touch()
    assert not module.valid_file(path, item)


@pytest.mark.skipif(not shutil.which("aria2c"), reason="需要 aria2c 做真实 HTTP 下载测试")
@pytest.mark.parametrize("initial", [b"", DATA[:12345], b"x" * len(DATA)],
                         ids=["new", "partial", "corrupted"])
def test_native_aria2_download_and_resume(module, tmp_path, server, initial):
    base, _ = server
    item = asset(module)
    path = tmp_path / item.name
    path.write_bytes(initial)
    result = module.download_asset(item, tmp_path, [base + "/ok/"], connections=4,
                                   min_speed=131072, slow_seconds=30, aria2=shutil.which("aria2c"))
    assert result.read_bytes() == DATA


@pytest.mark.skipif(not shutil.which("aria2c"), reason="需要 aria2c 做真实分段续传测试")
def test_native_aria2_segment_resume_across_sources(module, tmp_path, server, monkeypatch):
    # 足够大才会触发 --min-split-size=8M；小文件测试不能验证分段断点。
    monkeypatch.setattr(sys.modules[__name__], "DATA", DATA * 200)
    base, requests = server
    item = asset(module)
    path = tmp_path / item.name
    control = path.with_name(path.name + ".aria2")
    command = [
        shutil.which("aria2c"), "--no-conf=true", "--continue=true", "--file-allocation=none",
        "--max-connection-per-server=4", "--split=4", "--min-split-size=8M",
        "--auto-save-interval=1", f"--dir={tmp_path}", f"--out={item.name}",
        base + "/throttle/file",
    ]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if control.exists() and any(header for _, header in requests):
                break
            time.sleep(0.02)
        assert process.poll() is None, "应在文件完成前中断"
        process.send_signal(signal.SIGINT)
        process.wait(timeout=5)
        assert control.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    requests.clear()
    result = module.download_asset(item, tmp_path, [base + "/fail/", base + "/ok/"],
                                   connections=4, min_speed=0, slow_seconds=5,
                                   aria2=shutil.which("aria2c"))
    assert result.read_bytes() == DATA and not control.exists()
    assert any(path.startswith("/fail/") for path, _ in requests)
    assert any(path.startswith("/ok/") and header for path, header in requests)


def test_wget_does_not_resume_a_sparse_aria2_file(module, tmp_path, monkeypatch):
    item = asset(module)
    path = tmp_path / item.name
    control = path.with_name(path.name + ".aria2")
    path.write_bytes(b"sparse partial")
    control.write_bytes(b"bitmap")

    def download(*_):
        assert not path.exists() and not control.exists()
        path.write_bytes(DATA)
        return 0

    monkeypatch.setattr(module, "wget_download", download)
    assert module.download_asset(item, tmp_path, ["direct/"], connections=4,
                                 min_speed=0, slow_seconds=5, aria2=None).read_bytes() == DATA


def test_failed_cli_does_not_output_an_extraction_list(module, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(release())))
    monkeypatch.setattr(sys, "argv", ["download_jasna.py", "--output-dir", str(tmp_path)])
    monkeypatch.setattr(module, "rank_sources", lambda *_: [""])
    monkeypatch.setattr(module.shutil, "which", lambda *_: "/bin/aria2c")

    def fail(*_, **__):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(module, "download_asset", fail)
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 1
    assert capsys.readouterr().out == ""


@pytest.mark.skipif(not shutil.which("aria2c"), reason="需要 aria2c 做真实低速切源测试")
def test_native_aria2_slow_source_falls_back(module, tmp_path, server):
    base, requests = server
    path = module.download_asset(asset(module), tmp_path, [base + "/slow/", base + "/ok/"],
                                 connections=4, min_speed=131072, slow_seconds=5,
                                 aria2=shutil.which("aria2c"))
    assert path.read_bytes() == DATA
    assert any(path.startswith("/slow/") for path, _ in requests)
    assert any(path.startswith("/ok/") for path, _ in requests)
