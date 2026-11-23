#!/usr/bin/env python3
import argparse
import atexit
import glob
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE / "practice-player.html"
HUB = HERE / "index.html"
CACHE = Path(tempfile.gettempdir()) / "practice-player-cache"
DOWNLOAD_LOCK = threading.Lock()

# Pages opened from this helper send a heartbeat every 20 s and say "bye" when they close.
# When the last one is gone, the helper stops (unless --keep-running).
PAGES = {}                 # page id -> time of its last heartbeat
PAGES_LOCK = threading.Lock()
PAGE_STATE = {"armed": False, "empty_since": None}
BYE_GRACE = 8              # seconds to wait after the last page closes (a reload comes back sooner)
HEARTBEAT_TIMEOUT = 180    # a page silent this long is forgotten (closed without saying bye; background tabs still beat once a minute)

CONFIG = {"port": 8765, "max_minutes": 45, "cookies_from_browser": None, "origins": set()}

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

CONTENT_TYPES = {
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".webm": "audio/webm",
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
}


def load_ytdlp():
    try:
        import yt_dlp
        return yt_dlp
    except ImportError:
        return None


def has_ffmpeg():
    return shutil.which("ffmpeg") is not None


def base_options():
    opts = {"quiet": True, "no_warnings": True, "socket_timeout": 20, "noplaylist": True}
    runtimes = {name: {} for name in ("deno", "node", "bun") if shutil.which(name)}
    if runtimes:
        opts["js_runtimes"] = runtimes
    if CONFIG["cookies_from_browser"]:
        opts["cookiesfrombrowser"] = (CONFIG["cookies_from_browser"],)
    return opts


def search_source(yt_dlp, prefix, count, query, source):
    opts = base_options()
    opts.update({"extract_flat": True, "skip_download": True})
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"{prefix}{count}:{query}", download=False)
    results = []
    for entry in (info or {}).get("entries") or []:
        if not entry:
            continue
        url = entry.get("webpage_url") or entry.get("url")
        if not url:
            continue
        if not url.startswith("http") and entry.get("id"):
            url = "https://www.youtube.com/watch?v=" + entry["id"]
        duration = entry.get("duration")
        if duration is not None and duration < 20:
            continue
        results.append({
            "title": entry.get("title") or url,
            "uploader": entry.get("uploader") or entry.get("channel") or "",
            "duration": duration,
            "url": url,
            "source": source,
        })
    return results


def search_all(query):
    yt_dlp = load_ytdlp()
    if yt_dlp is None:
        raise RuntimeError("yt-dlp is not installed. Run: python3 -m pip install -U yt-dlp")
    jobs = [("YouTube", "ytsearch", 15), ("SoundCloud", "scsearch", 10)]
    out = {"results": [], "errors": {}}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [(name, pool.submit(search_source, yt_dlp, prefix, count, query, name)) for name, prefix, count in jobs]
        for name, future in futures:
            try:
                out["results"].extend(future.result())
            except Exception as exc:
                out["errors"][name] = str(exc).splitlines()[0][:300]
    return out


def page_seen(page_id, alive):
    with PAGES_LOCK:
        if alive:
            PAGES[page_id] = time.monotonic()
            PAGE_STATE["armed"] = True
        else:
            PAGES.pop(page_id, None)


def watch_pages(server):
    while True:
        time.sleep(2)
        now = time.monotonic()
        with PAGES_LOCK:
            for page_id, seen in list(PAGES.items()):
                if now - seen > HEARTBEAT_TIMEOUT:
                    del PAGES[page_id]
            if not PAGE_STATE["armed"] or PAGES:
                PAGE_STATE["empty_since"] = None
                continue
            if PAGE_STATE["empty_since"] is None:
                PAGE_STATE["empty_since"] = now
                continue
            if now - PAGE_STATE["empty_since"] < BYE_GRACE:
                continue
        print("The page was closed: stopping the helper.")
        server.shutdown()
        return


# ---------- AI instrument separation (optional) ----------
# Demucs (Meta, open source) is a neural network trained to split a mix into stems,
# the same family of technique that services like Suno use. htdemucs_6s gives
# drums, bass, guitar, piano, vocals and "other"; for a sax recording the sax ends up
# in vocals + other.
# It runs in a separate Python (by default ~/.practice-player-ai-venv, made by
# mac/install-ai.sh), because PyTorch needs an older Python than the helper may use.
SEP_JOBS = {}            # id -> {"progress", "done", "error", "stems", "keys", "proc"}
SEP_LOCK = threading.Lock()
AI_STATE = {"python": None, "checked": False}

SEP_WORKER = r"""
import os, sys
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
try:
    import certifi   # the python.org Python for macOS has no SSL certificates of its own
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except Exception:
    pass
import numpy as np
import torch
from demucs.audio import convert_audio

inp, sr, ch, outdir = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
pcm = np.fromfile(inp, dtype="<i2").astype(np.float32) / 32768.0
wav = pcm.reshape(-1, ch).T.copy()
if ch == 1:
    wav = np.concatenate([wav, wav], axis=0)
length = wav.shape[1]

def callback(info):
    if info.get("state") != "end":
        return
    models = max(1, info.get("models", 1))
    frac = (info.get("model_idx_in_bag", 0) + min(1.0, (info.get("segment_offset", 0) + 1) / max(1, info.get("audio_length", 1)))) / models
    print("progress %.4f" % frac, flush=True)

def separate(device):
    try:
        from demucs.api import Separator          # Demucs 4.1
    except ImportError:
        Separator = None
    if Separator is not None:
        sep = Separator(model="htdemucs_6s", device=device, shifts=1, overlap=0.25, progress=False, callback=callback)
        _, stems = sep.separate_tensor(torch.from_numpy(wav), sr)
        return sep.samplerate, stems
    # Demucs 4.0.1 (the version that installs on Intel Macs)
    import types
    import demucs.apply as dapply
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    def tq(iterable, **kw):
        items = list(iterable)
        for i, item in enumerate(items):
            yield item
            print("progress %.4f" % ((i + 1) / max(1, len(items))), flush=True)

    dapply.tqdm = types.SimpleNamespace(tqdm=tq)
    model = get_model("htdemucs_6s")
    model.eval()
    w = convert_audio(torch.from_numpy(wav), sr, model.samplerate, model.audio_channels)
    ref = w.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8
    with torch.no_grad():
        out = apply_model(model, ((w - mean) / std)[None], device=device, shifts=1, split=True, overlap=0.25, progress=True)[0]
    out = out * std + mean
    return model.samplerate, dict(zip(model.sources, out))

device = "mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available() else "cpu"
try:
    model_sr, stems = separate(device)
except Exception:
    if device == "cpu":
        raise
    print("progress 0", flush=True)
    model_sr, stems = separate("cpu")

def back(x):
    y = convert_audio(x, model_sr, sr, 2).numpy().astype(np.float32)
    if y.shape[1] < length:
        y = np.pad(y, ((0, 0), (0, length - y.shape[1])))
    y = y[:, :length]
    return np.ascontiguousarray(y[:1] if ch == 1 else y)

out = {"sax": stems["vocals"] + stems["other"], "bass": stems["bass"], "drums": stems["drums"]}
for name, x in out.items():
    back(x).astype("<f4").tofile(os.path.join(outdir, name + ".f32"))
piano = float((stems["piano"] ** 2).mean()) if "piano" in stems else 0.0
guitar = float((stems["guitar"] ** 2).mean()) if "guitar" in stems else 0.0
print("keys " + ("Piano" if piano >= guitar else "Guitar"), flush=True)
print("done", flush=True)
"""


def ai_python():
    """The Python that has Demucs + PyTorch, or None (checked once, it is fast)."""
    if AI_STATE["checked"]:
        return AI_STATE["python"]
    candidates = [os.environ.get("PRACTICE_PLAYER_AI_PYTHON"),
                  str(Path.home() / ".practice-player-ai-venv" / "bin" / "python"),
                  sys.executable]
    probe = "import importlib.util as u, sys; sys.exit(0 if u.find_spec('demucs') and u.find_spec('torch') else 1)"
    for py in candidates:
        if not py or not os.path.exists(py):
            continue
        try:
            if subprocess.run([py, "-c", probe], timeout=20, capture_output=True).returncode == 0:
                AI_STATE["python"] = py
                break
        except Exception:
            pass
    AI_STATE["checked"] = True
    return AI_STATE["python"]


def has_demucs():
    return ai_python() is not None


def run_separation(job_id, pcm, sr, channels):
    job = SEP_JOBS[job_id]
    work = Path(tempfile.mkdtemp(prefix="pp-sep-"))
    try:
        with SEP_LOCK:
            if job.get("cancel"):
                raise RuntimeError("cancelled")
            (work / "in.raw").write_bytes(pcm)
            job["progress"] = 0.03
            proc = subprocess.Popen([ai_python(), "-c", SEP_WORKER, str(work / "in.raw"), str(sr), str(channels), str(work)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            job["proc"] = proc
            err_lines = []
            threading.Thread(target=lambda: err_lines.extend(proc.stderr), daemon=True).start()
            for line in proc.stdout:
                parts = line.split()
                if parts[:1] == ["progress"]:
                    job["progress"] = max(job["progress"] if float(parts[1]) > 0 else 0.03, min(0.97, 0.05 + 0.92 * float(parts[1])))
                elif parts[:1] == ["keys"]:
                    job["keys"] = parts[1]
            proc.wait()
            if job.get("cancel"):
                raise RuntimeError("cancelled")
            if proc.returncode != 0:
                last = [l.strip() for l in err_lines if l.strip()]
                raise RuntimeError(last[-1] if last else f"Demucs stopped (code {proc.returncode})")
        stems = {}
        for name in ("sax", "bass", "drums"):
            stems[name] = ((work / (name + ".f32")).read_bytes(), channels)
        job.update(stems=stems, progress=1.0, done=True)
    except Exception as exc:
        job.update(error=(str(exc).splitlines() or [exc.__class__.__name__])[0][:300], done=True)
    finally:
        job.pop("proc", None)
        shutil.rmtree(work, ignore_errors=True)
    finished = [k for k, v in SEP_JOBS.items() if v.get("done")]
    for k in finished[:-3]:       # keep the 3 most recent results in memory
        SEP_JOBS.pop(k, None)


def cancel_separation(job_id):
    job = SEP_JOBS.get(job_id)
    if job and not job["done"]:
        job["cancel"] = True
        proc = job.get("proc")
        if proc:
            try:
                proc.kill()
            except Exception:
                pass


def start_separation(body, sr, channels):
    if not has_demucs():
        raise RuntimeError("AI separation is not installed (see mac/install-ai.sh).")
    job_id = hashlib.sha1(body + f"{sr}:{channels}".encode()).hexdigest()[:20]
    job = SEP_JOBS.get(job_id)
    if job and not job.get("error"):
        return job_id
    SEP_JOBS[job_id] = {"progress": 0.0, "done": False, "error": None}
    threading.Thread(target=run_separation, args=(job_id, body, sr, channels), daemon=True).start()
    return job_id


# ---------- saved patterns (Pattern Practice) ----------
# Kept on disk next to this script, so they survive the browser: patterns/index.json
# (the list) and patterns/<id>.<part>.f32 (audio: channel L then R, float32).
PATTERN_DIR = HERE / "patterns"
PATTERN_LOCK = threading.Lock()
PATTERN_PARTS = ("harm", "drums", "sax")


def pattern_id_ok(value):
    return bool(value) and len(value) <= 40 and all(c.isalnum() or c == "-" for c in value)


def read_pattern_index():
    try:
        data = json.loads((PATTERN_DIR / "index.json").read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def write_pattern_index(items):
    PATTERN_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PATTERN_DIR / "index.json.tmp"
    tmp.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, PATTERN_DIR / "index.json")


def pattern_files():
    files = {}
    if PATTERN_DIR.exists():
        for f in PATTERN_DIR.glob("*.f32"):
            pid, _, part = f.stem.rpartition(".")
            files.setdefault(pid, []).append(part)
    return files


# ---------- saving files in a folder the user chooses (for browsers without a folder picker) ----------
CHOSEN_DIRS = set()     # only folders picked in the dialog below can be written to


def choose_folder(prompt):
    if sys.platform != "darwin":
        raise RuntimeError("The folder dialog is only available on a Mac.")
    script = ['-e', 'activate', '-e', 'POSIX path of (choose folder with prompt "%s")' % prompt.replace('"', "'")]
    r = subprocess.run(["osascript"] + script, capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        return None          # cancelled
    path = os.path.realpath(r.stdout.strip())
    if not os.path.isdir(path):
        return None
    CHOSEN_DIRS.add(path)
    return path


def safe_file_name(name):
    name = "".join(c for c in name if c not in '/\\:*?"<>|\0').strip().strip(".")
    return name[:180] or "file"


# ---------- iReal Pro public playlists (downloaded by this computer, for Pattern Practice) ----------
IREAL_HOSTS = {"www.irealpro.com", "irealpro.com", "forums.irealpro.com", "www.irealb.com", "irealb.com"}


def fetch_ireal_page(url):
    import ssl
    import urllib.request
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if urllib.parse.urlparse(url).scheme != "https" or host not in IREAL_HOSTS:
        raise RuntimeError("Only pages of the iReal Pro website can be downloaded.")
    try:
        import certifi                     # the python.org Python for macOS has no certificates of its own
        context = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        context = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh) TallerMusical"})
    with urllib.request.urlopen(req, timeout=40, context=context) as r:
        data = r.read(30 * 1024 * 1024)
    return data.decode("utf-8", "replace")


def find_cached(key):
    for path in glob.glob(str(CACHE / (key + ".*"))):
        if not path.endswith((".part", ".ytdl", ".json")):
            return Path(path)
    return None


def fetch_audio(url, max_minutes):
    yt_dlp = load_ytdlp()
    if yt_dlp is None:
        raise RuntimeError("yt-dlp is not installed. Run: python3 -m pip install -U yt-dlp")
    CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(url.encode()).hexdigest()[:20]
    with DOWNLOAD_LOCK:
        cached = find_cached(key)
        meta_file = CACHE / (key + ".json")
        if cached and meta_file.exists():
            return cached, json.loads(meta_file.read_text())["title"]
        opts = base_options()
        opts.update({
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": str(CACHE / (key + ".%(ext)s")),
        })
        if has_ffmpeg():
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "flac"}]
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info and info.get("_type") == "playlist":
                entries = [e for e in info.get("entries") or [] if e]
                if not entries:
                    raise RuntimeError("No playable item found at that link.")
                info = entries[0]
            duration = info.get("duration")
            if duration and duration > max_minutes * 60:
                raise RuntimeError(f"This item is {int(duration // 60)} min long; the limit is {max_minutes} min (use --max-minutes).")
            ydl.process_ie_result(info, download=True)
        path = find_cached(key)
        if path is None:
            raise RuntimeError("The audio could not be extracted.")
        title = info.get("title") or "audio"
        meta_file.write_text(json.dumps({"title": title}))
        return path, title


class Handler(BaseHTTPRequestHandler):
    server_version = "PracticePlayer/1.0"

    def log_message(self, fmt, *args):
        return

    def host_ok(self):
        host = (self.headers.get("Host") or "").lower()
        port = CONFIG["port"]
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    def own_origins(self):
        port = CONFIG["port"]
        return {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    def origin_state(self):
        origin = self.headers.get("Origin")
        if origin:
            if origin in self.own_origins() or origin in CONFIG["origins"]:
                return True, origin
            return False, origin
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False, None
        return True, None

    def end_headers(self):
        origin = getattr(self, "cors_origin", None)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Expose-Headers", "X-Track-Title")
            self.send_header("Access-Control-Allow-Private-Network", "true")
        super().end_headers()

    def do_OPTIONS(self):
        allowed, origin = self.origin_state()
        if not self.host_ok() or not allowed or not origin:
            self.send_json(403, {"error": "Forbidden"})
            return
        self.cors_origin = origin
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def static_file(self, route):
        """The hub (index.html) and the other pages that sit next to this script."""
        name = urllib.parse.unquote(route).lstrip("/") or ("index.html" if HUB.exists() else PAGE.name)
        if "/" in name or "\\" in name or name.startswith("."):
            return None
        path = HERE / name
        if path.suffix.lower() in STATIC_TYPES and path.is_file():
            return path
        return None

    def do_POST(self):
        allowed, origin = self.origin_state()
        if not self.host_ok() or not allowed:
            self.send_json(403, {"error": "Forbidden"})
            return
        self.cors_origin = origin
        route = urllib.parse.urlparse(self.path).path
        try:
            if route in ("/api/heartbeat", "/api/bye"):
                # navigator.sendBeacon sends a POST when the page closes
                length = int(self.headers.get("Content-Length") or 0)
                if 0 < length <= 4096:
                    self.rfile.read(length)
                page_id = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("id") or [""])[0][:64]
                if page_id:
                    page_seen(page_id, route == "/api/heartbeat")
                self.send_json(200, {"ok": True})
                return
            if route == "/api/choose-folder":
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                path = choose_folder((q.get("prompt") or ["Choose a folder"])[0][:120])
                self.send_json(200, {"path": path} if path else {"cancelled": True})
                return
            if route == "/api/save-file":
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                folder = os.path.realpath((q.get("dir") or [""])[0])
                name = safe_file_name((q.get("name") or [""])[0])
                size = int(self.headers.get("Content-Length") or 0)
                if folder not in CHOSEN_DIRS or not (0 < size <= 500 * 1024 * 1024):
                    self.send_json(403, {"error": "Choose the folder first."})
                    return
                data = self.rfile.read(size)
                target = os.path.join(folder, name)
                tmp = target + ".part"
                with open(tmp, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, target)
                self.send_json(200, {"ok": True, "path": target})
                return
            if route == "/api/patterns/meta":
                size = int(self.headers.get("Content-Length") or 0)
                if not (0 < size <= 20 * 1024 * 1024):
                    self.send_json(400, {"error": "Bad list"})
                    return
                items = json.loads(self.rfile.read(size).decode("utf-8")).get("patterns")
                if not isinstance(items, list) or not all(isinstance(x, dict) and pattern_id_ok(str(x.get("id", ""))) for x in items):
                    self.send_json(400, {"error": "Bad list"})
                    return
                with PATTERN_LOCK:
                    write_pattern_index(items)
                self.send_json(200, {"ok": True})
                return
            if route == "/api/patterns/audio":
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                pid, part = (q.get("id") or [""])[0], (q.get("part") or [""])[0]
                size = int(self.headers.get("Content-Length") or 0)
                if not pattern_id_ok(pid) or part not in PATTERN_PARTS or not (0 < size <= 400 * 1024 * 1024):
                    self.send_json(400, {"error": "Bad audio"})
                    return
                data = self.rfile.read(size)
                with PATTERN_LOCK:
                    PATTERN_DIR.mkdir(parents=True, exist_ok=True)
                    tmp = PATTERN_DIR / f"{pid}.{part}.tmp"
                    tmp.write_bytes(data)
                    os.replace(tmp, PATTERN_DIR / f"{pid}.{part}.f32")
                self.send_json(200, {"ok": True})
                return
            if route == "/api/patterns/delete":
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                pid = (q.get("id") or [""])[0]
                if not pattern_id_ok(pid):
                    self.send_json(400, {"error": "Bad id"})
                    return
                with PATTERN_LOCK:
                    write_pattern_index([x for x in read_pattern_index() if x.get("id") != pid])
                    for part in PATTERN_PARTS:
                        (PATTERN_DIR / f"{pid}.{part}.f32").unlink(missing_ok=True)
                self.send_json(200, {"ok": True})
                return
            if route != "/api/separate":
                self.send_json(404, {"error": "Not found"})
                return
            size = int(self.headers.get("Content-Length") or 0)
            sr = int(self.headers.get("X-Sample-Rate") or 0)
            channels = int(self.headers.get("X-Channels") or 0)
            if not (0 < size <= 600 * 1024 * 1024) or not (8000 <= sr <= 192000) or channels not in (1, 2):
                self.send_json(400, {"error": "Bad audio"})
                return
            body = self.rfile.read(size)
            self.send_json(200, {"id": start_separation(body, sr, channels)})
        except Exception as exc:
            self.send_json(500, {"error": str(exc).splitlines()[0][:400] if str(exc) else exc.__class__.__name__})

    def do_GET(self):
        allowed, origin = self.origin_state()
        if not self.host_ok() or not allowed:
            self.send_json(403, {"error": "Forbidden"})
            return
        self.cors_origin = origin
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        route = parsed.path
        try:
            static = self.static_file(route)
            if static is not None or route == "/":
                if static is not None:
                    data = static.read_bytes()
                    content_type = STATIC_TYPES[static.suffix.lower()]
                else:
                    data = b"Practice Player helper is running. Open your Practice Player page in the browser."
                    content_type = "text/plain; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            elif route in ("/api/heartbeat", "/api/bye"):
                page_id = (query.get("id") or [""])[0][:64]
                if page_id:
                    page_seen(page_id, route == "/api/heartbeat")
                self.send_json(200, {"ok": True})
            elif route == "/api/patterns":
                with PATTERN_LOCK:
                    self.send_json(200, {"patterns": read_pattern_index(), "files": pattern_files()})
            elif route == "/api/patterns/audio":
                pid, part = (query.get("id") or [""])[0], (query.get("part") or [""])[0]
                path = PATTERN_DIR / f"{pid}.{part}.f32"
                if not pattern_id_ok(pid) or part not in PATTERN_PARTS or not path.exists():
                    self.send_json(404, {"error": "Not found"})
                    return
                data = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            elif route == "/api/ireal-fetch":
                text = fetch_ireal_page((query.get("url") or [""])[0])
                data = text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            elif route == "/api/ping":
                self.send_json(200, {"ok": True, "ytdlp": load_ytdlp() is not None, "ffmpeg": has_ffmpeg(), "demucs": has_demucs()})
            elif route == "/api/sepstatus":
                job = SEP_JOBS.get((query.get("id") or [""])[0])
                if not job:
                    self.send_json(404, {"error": "Unknown separation"})
                else:
                    self.send_json(200, {"progress": job["progress"], "done": job["done"], "error": job["error"], "keys": job.get("keys")})
            elif route == "/api/sepcancel":
                cancel_separation((query.get("id") or [""])[0])
                self.send_json(200, {"ok": True})
            elif route == "/api/stem":
                job = SEP_JOBS.get((query.get("id") or [""])[0])
                name = (query.get("name") or [""])[0]
                if not job or not job.get("stems") or name not in job["stems"]:
                    self.send_json(404, {"error": "Stem not ready"})
                    return
                data, stem_channels = job["stems"][name]   # planar float32: channel 0, then channel 1
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-Channels", str(stem_channels))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            elif route == "/api/search":
                q = (query.get("q") or [""])[0].strip()
                if not q:
                    self.send_json(400, {"error": "Empty search"})
                    return
                self.send_json(200, search_all(q))
            elif route == "/api/audio":
                url = (query.get("url") or [""])[0].strip()
                if urllib.parse.urlparse(url).scheme not in ("http", "https"):
                    self.send_json(400, {"error": "Only http and https links are supported."})
                    return
                path, title = fetch_audio(url, CONFIG["max_minutes"])
                size = path.stat().st_size
                self.send_response(200)
                self.send_header("Content-Type", CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream"))
                self.send_header("Content-Length", str(size))
                self.send_header("X-Track-Title", urllib.parse.quote(title))
                self.end_headers()
                with open(path, "rb") as fh:
                    while True:
                        chunk = fh.read(1 << 20)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            else:
                self.send_json(404, {"error": "Not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            message = str(exc).splitlines()[0].replace("ERROR: ", "", 1) if str(exc) else exc.__class__.__name__
            try:
                self.send_json(500, {"error": message[:400]})
            except Exception:
                pass


def ensure_ytdlp():
    if load_ytdlp() is not None:
        return
    print("yt-dlp is not installed. It is needed to search YouTube/SoundCloud and extract audio.")
    answer = input("Install it now with pip? [Y/n] ").strip().lower()
    if answer in ("", "y", "yes"):
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--user", "-U", "yt-dlp[default]"])
        except subprocess.CalledProcessError:
            print('Automatic install failed. Try: python3 -m pip install -U "yt-dlp[default]"')
        else:
            import site
            import importlib
            importlib.invalidate_caches()
            if site.getusersitepackages() not in sys.path:
                sys.path.append(site.getusersitepackages())


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local helper for Practice Player")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--max-minutes", type=int, default=45)
    parser.add_argument("--cookies-from-browser", default=None, metavar="BROWSER")
    parser.add_argument("--allow-origin", action="append", default=[], metavar="URL")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--keep-running", action="store_true",
                        help="do not stop when the page is closed")
    parser.add_argument("--open", default=None, metavar="PAGE",
                        help="page or programs to open, e.g. practice-player.html or afinador+chord-practice")
    parser.add_argument("--no-install-prompt", action="store_true")
    args = parser.parse_args(argv)
    raw_origins = args.allow_origin + [o for o in os.environ.get("PRACTICE_PLAYER_ORIGINS", "").split(",") if o.strip()]
    origins = set()
    for raw in raw_origins:
        parsed = urllib.parse.urlparse(raw.strip())
        if parsed.scheme in ("http", "https") and parsed.netloc:
            origins.add(f"{parsed.scheme}://{parsed.netloc}")
    CONFIG.update(port=args.port, max_minutes=args.max_minutes, cookies_from_browser=args.cookies_from_browser, origins=origins)
    if not args.no_install_prompt:
        ensure_ytdlp()
    if not has_ffmpeg():
        print("Note: ffmpeg was not found. Audio will be used as downloaded (usually m4a or webm) instead of FLAC.")
    if has_demucs():
        print(f"AI separation available (Demucs, {ai_python()}).")
    else:
        print("Optional: AI instrument separation is not installed. On a Mac run:")
        print("    curl -fsSL https://raw.githubusercontent.com/Gabrieltrial/Trial/main/mac/install-ai.sh | bash")
    if not any(shutil.which(x) for x in ("deno", "node", "bun")):
        print("Note: YouTube extraction may need a JavaScript runtime (Deno or Node.js) to be installed.")
    atexit.register(lambda: shutil.rmtree(CACHE, ignore_errors=True))
    # Taller musical.app stops the helper with SIGTERM: exit cleanly so the cache is removed.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Practice Player helper is running on port {args.port}  (Ctrl+C to stop)")
    if origins:
        print("Pages allowed to use it: " + ", ".join(sorted(origins)))
    if HUB.exists() or PAGE.exists():
        target = url
        if args.open:
            target = url + args.open if args.open.endswith(".html") else url + "#" + args.open
        print(f"Open in the browser: {target}")
        if not args.no_browser and not origins:
            webbrowser.open(target)
    else:
        print("index.html / practice-player.html were not found next to this script, so only your hosted page can use the helper.")
        if not origins:
            print("Start it with: --allow-origin https://YOUR-USER.github.io")
    if not args.keep_running:
        threading.Thread(target=watch_pages, args=(server,), daemon=True).start()
        print("It stops by itself when you close the page.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for job_id in list(SEP_JOBS):
            cancel_separation(job_id)
        server.server_close()


if __name__ == "__main__":
    main()
