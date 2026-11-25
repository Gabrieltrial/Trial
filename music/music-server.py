#!/usr/bin/env python3
"""Music server: search (YouTube, SoundCloud), download audio, play it from a phone.

Files go to  <music dir>/<artist>/<album>/<title>.<ext>  (default: ~/Music/baixat internet).
Open the address it prints on a phone on the same Wi-Fi and type the PIN it shows.
Runs on a Mac or on a Raspberry Pi (Python 3.9+, yt-dlp; ffmpeg optional).
"""
import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE / "music.html"
CONFIG_FILE = Path.home() / ".music-server.json"
AUDIO_EXT = {".m4a", ".mp3", ".aac", ".opus", ".ogg", ".oga", ".webm", ".flac", ".wav"}
STATE = {"music_dir": None, "pin": None, "tokens": set(), "fails": [], "lock": threading.Lock()}
JOBS = {}                     # job id -> {title, artist, album, progress, done, error, file}
DOWNLOAD_LOCK = threading.Lock()


# ---------- config: PIN and the phones that already typed it ----------
def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=1))
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except Exception:
        pass


def lan_address():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.0.1", 9))          # no packet is sent; only picks the outgoing interface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ---------- yt-dlp ----------
def ytdlp():
    try:
        import yt_dlp
        return yt_dlp
    except ImportError:
        return None


def base_opts():
    opts = {"quiet": True, "no_warnings": True, "socket_timeout": 20, "noplaylist": True}
    runtimes = {name: {} for name in ("deno", "node", "bun") if shutil.which(name)}
    if runtimes:
        opts["js_runtimes"] = runtimes
    return opts


def search(query):
    y = ytdlp()
    if y is None:
        raise RuntimeError("yt-dlp is not installed: python3 -m pip install -U yt-dlp")

    def one(prefix, count, source):
        opts = base_opts()
        opts.update({"extract_flat": True, "skip_download": True})
        with y.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"{prefix}{count}:{query}", download=False)
        out = []
        for e in (info or {}).get("entries") or []:
            if not e:
                continue
            url = e.get("webpage_url") or e.get("url")
            if (not url or not url.startswith("http")) and e.get("id") and source == "YouTube":
                url = "https://www.youtube.com/watch?v=" + e["id"]
            if not url or (e.get("duration") or 999) < 30:
                continue
            thumbs = e.get("thumbnails") or []
            out.append({"title": e.get("title") or url, "uploader": e.get("uploader") or e.get("channel") or "",
                        "duration": e.get("duration"), "url": url, "source": source,
                        "thumb": (thumbs[-1].get("url") if thumbs else None) or e.get("thumbnail")})
        return out

    res, errors = [], {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [("YouTube", pool.submit(one, "ytsearch", 15, "YouTube")), ("SoundCloud", pool.submit(one, "scsearch", 8, "SoundCloud"))]
        for name, f in jobs:
            try:
                res.extend(f.result())
            except Exception as exc:
                errors[name] = str(exc).splitlines()[0][:200]
    return {"results": res, "errors": errors}


def safe_name(text, fallback):
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", text or "").strip().strip(".")
    text = re.sub(r"\s+", " ", text)
    return text[:120] or fallback


def download(job_id, url, artist, album, title):
    job = JOBS[job_id]
    y = ytdlp()
    try:
        if y is None:
            raise RuntimeError("yt-dlp is not installed")
        folder = Path(STATE["music_dir"]) / safe_name(artist, "Desconegut") / safe_name(album, "Senzills")
        folder.mkdir(parents=True, exist_ok=True)
        base = safe_name(title, "Cançó")
        name, n = base, 2
        while any((folder / (name + ext)).exists() for ext in AUDIO_EXT):
            name, n = f"{base} ({n})", n + 1

        def hook(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total:
                    job["progress"] = min(0.95, d.get("downloaded_bytes", 0) / total * 0.95)
            elif d.get("status") == "finished":
                job["progress"] = 0.96

        opts = base_opts()
        opts.update({"format": "bestaudio[ext=m4a]/bestaudio/best", "outtmpl": str(folder / (name + ".%(ext)s")), "progress_hooks": [hook]})
        if shutil.which("ffmpeg"):
            # m4a (AAC) plays everywhere, iPhone included; an m4a source is only re-wrapped
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}]
        with DOWNLOAD_LOCK, y.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        files = [f for f in folder.iterdir() if f.stem == name and f.suffix.lower() in AUDIO_EXT]
        if not files:
            raise RuntimeError("the audio could not be saved")
        thumbs = info.get("thumbnails") or []
        meta = {"title": title, "artist": artist, "album": album, "source": url, "duration": info.get("duration"),
                "thumb": (thumbs[-1].get("url") if thumbs else None) or info.get("thumbnail"), "added": int(time.time())}
        (folder / (name + ".json")).write_text(json.dumps(meta, ensure_ascii=False, indent=1))
        job.update(progress=1.0, done=True, file=track_id(files[0]))
    except Exception as exc:
        job.update(done=True, error=(str(exc).splitlines() or ["error"])[0].replace("ERROR: ", "")[:300])


# ---------- library ----------
def track_id(path):
    rel = str(Path(path).resolve().relative_to(Path(STATE["music_dir"]).resolve()))
    return base64.urlsafe_b64encode(rel.encode()).decode().rstrip("=")


def path_of(tid):
    try:
        rel = base64.urlsafe_b64decode(tid + "=" * (-len(tid) % 4)).decode()
    except Exception:
        return None
    root = Path(STATE["music_dir"]).resolve()
    p = (root / rel).resolve()
    if root not in p.parents or not p.is_file() or p.suffix.lower() not in AUDIO_EXT:
        return None
    return p


def library():
    root = Path(STATE["music_dir"])
    out = []
    if not root.exists():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() not in AUDIO_EXT or fn.startswith("."):
                continue
            rel = p.relative_to(root).parts
            meta = {}
            side = p.with_suffix(".json")
            if side.exists():
                try:
                    meta = json.loads(side.read_text())
                except Exception:
                    meta = {}
            out.append({"id": track_id(p), "title": meta.get("title") or p.stem,
                        "artist": meta.get("artist") or (rel[0] if len(rel) > 2 else "Desconegut"),
                        "album": meta.get("album") or (rel[1] if len(rel) > 2 else (rel[0] if len(rel) > 1 else "")),
                        "duration": meta.get("duration"), "thumb": meta.get("thumb"), "added": meta.get("added") or int(p.stat().st_mtime)})
    out.sort(key=lambda t: (t["artist"].lower(), t["album"].lower(), t["title"].lower()))
    return out


def playlists_file():
    return Path(STATE["music_dir"]) / ".llistes.json"


def read_playlists():
    try:
        data = json.loads(playlists_file().read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "MusicServer/1.0"

    def log_message(self, fmt, *args):
        return

    def token_ok(self):
        cookie = self.headers.get("Cookie") or ""
        m = re.search(r"(?:^|;\s*)msk=([A-Za-z0-9_-]+)", cookie)
        return bool(m) and m.group(1) in STATE["tokens"]

    def send_json(self, status, payload, extra=None):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def body_json(self, limit=2 * 1024 * 1024):
        n = int(self.headers.get("Content-Length") or 0)
        if not (0 < n <= limit):
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/music.html"):
                data = PAGE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            if u.path == "/api/me":
                self.send_json(200, {"ok": self.token_ok(), "ytdlp": ytdlp() is not None, "ffmpeg": bool(shutil.which("ffmpeg"))})
                return
            if not self.token_ok():
                self.send_json(401, {"error": "PIN"})
                return
            if u.path == "/api/search":
                text = (q.get("q") or [""])[0].strip()
                self.send_json(200, search(text) if text else {"results": [], "errors": {}})
            elif u.path == "/api/library":
                self.send_json(200, {"tracks": library(), "folder": STATE["music_dir"]})
            elif u.path == "/api/jobs":
                self.send_json(200, {"jobs": [dict(id=k, **{x: v[x] for x in v if x != "thread"}) for k, v in JOBS.items()]})
            elif u.path == "/api/playlists":
                self.send_json(200, {"playlists": read_playlists()})
            elif u.path == "/api/file":
                self.send_file(path_of((q.get("id") or [""])[0]))
            else:
                self.send_json(404, {"error": "Not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            try:
                self.send_json(500, {"error": (str(exc).splitlines() or ["error"])[0][:300]})
            except Exception:
                pass

    # audio with HTTP ranges: Safari on the iPhone needs them to play and to seek
    def send_file(self, p):
        if p is None:
            self.send_json(404, {"error": "Not found"})
            return
        size = p.stat().st_size
        ctype = {".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".aac": "audio/aac", ".opus": "audio/ogg", ".ogg": "audio/ogg",
                 ".oga": "audio/ogg", ".webm": "audio/webm", ".flac": "audio/flac", ".wav": "audio/wav"}.get(p.suffix.lower(), "application/octet-stream")
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        with open(p, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(1 << 16, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path == "/api/login":
                with STATE["lock"]:
                    now = time.time()
                    STATE["fails"] = [t for t in STATE["fails"] if now - t < 60]
                    if len(STATE["fails"]) >= 8:
                        self.send_json(429, {"error": "Massa intents. Espera un minut."})
                        return
                    pin = str(self.body_json().get("pin", "")).strip()
                    if not secrets.compare_digest(pin, STATE["pin"]):
                        STATE["fails"].append(now)
                        self.send_json(403, {"error": "PIN incorrecte"})
                        return
                    token = secrets.token_urlsafe(24)
                    STATE["tokens"].add(token)
                    cfg = load_config()
                    cfg["tokens"] = list(STATE["tokens"])[-50:]
                    save_config(cfg)
                self.send_json(200, {"ok": True}, {"Set-Cookie": f"msk={token}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Strict"})
                return
            if not self.token_ok():
                self.send_json(401, {"error": "PIN"})
                return
            if u.path == "/api/download":
                b = self.body_json()
                url = str(b.get("url", ""))
                if urllib.parse.urlparse(url).scheme not in ("http", "https"):
                    self.send_json(400, {"error": "Bad link"})
                    return
                job_id = secrets.token_hex(6)
                JOBS[job_id] = {"title": b.get("title") or "", "artist": b.get("artist") or "", "album": b.get("album") or "",
                                "progress": 0.0, "done": False, "error": None, "file": None, "started": time.time()}
                threading.Thread(target=download, args=(job_id, url, b.get("artist"), b.get("album"), b.get("title")), daemon=True).start()
                for k in [k for k, v in JOBS.items() if v["done"] and time.time() - v["started"] > 3600]:
                    JOBS.pop(k, None)
                self.send_json(200, {"id": job_id})
            elif u.path == "/api/playlists":
                items = self.body_json().get("playlists")
                if not isinstance(items, list):
                    self.send_json(400, {"error": "Bad list"})
                    return
                Path(STATE["music_dir"]).mkdir(parents=True, exist_ok=True)
                tmp = playlists_file().with_suffix(".tmp")
                tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1))
                os.replace(tmp, playlists_file())
                self.send_json(200, {"ok": True})
            elif u.path == "/api/delete":
                p = path_of(str(self.body_json().get("id", "")))
                if p is None:
                    self.send_json(404, {"error": "Not found"})
                    return
                p.unlink()
                p.with_suffix(".json").unlink(missing_ok=True)
                for d in (p.parent, p.parent.parent):          # empty album / artist folders go too
                    try:
                        if d != Path(STATE["music_dir"]) and not any(d.iterdir()):
                            d.rmdir()
                    except Exception:
                        pass
                self.send_json(200, {"ok": True})
            else:
                self.send_json(404, {"error": "Not found"})
        except Exception as exc:
            self.send_json(500, {"error": (str(exc).splitlines() or ["error"])[0][:300]})


def main():
    ap = argparse.ArgumentParser(description="Music server for the phone")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--music-dir", default=None, help="default: ~/Music/baixat internet")
    ap.add_argument("--new-pin", action="store_true", help="make a new PIN (phones must type it again)")
    a = ap.parse_args()
    cfg = load_config()
    music_dir = a.music_dir or cfg.get("music_dir") or str(Path.home() / "Music" / "baixat internet")
    music_dir = os.path.expanduser(music_dir)
    if a.new_pin or not cfg.get("pin"):
        cfg["pin"] = f"{secrets.randbelow(10**6):06d}"
        cfg["tokens"] = []
    if a.music_dir:
        cfg["music_dir"] = music_dir
    save_config(cfg)
    STATE.update(music_dir=music_dir, pin=cfg["pin"], tokens=set(cfg.get("tokens") or []))
    Path(music_dir).mkdir(parents=True, exist_ok=True)
    if ytdlp() is None:
        print("yt-dlp is not installed. Install it with:  python3 -m pip install -U yt-dlp")
    server = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    print("Music server running.")
    print(f"  On the phone (same Wi-Fi):  http://{lan_address()}:{a.port}")
    print(f"  PIN: {cfg['pin']}")
    print(f"  Music folder: {music_dir}")
    print("  Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
