#!/usr/bin/env python3
import argparse
import atexit
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE / "practice-player.html"
CACHE = Path(tempfile.gettempdir()) / "practice-player-cache"
DOWNLOAD_LOCK = threading.Lock()

CONFIG = {"port": 8765, "max_minutes": 45, "cookies_from_browser": None, "origins": set()}

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
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
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
            if route in ("/", "/practice-player.html"):
                if PAGE.exists():
                    data = PAGE.read_bytes()
                    content_type = "text/html; charset=utf-8"
                else:
                    data = b"Practice Player helper is running. Open your Practice Player page in the browser."
                    content_type = "text/plain; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            elif route == "/api/ping":
                self.send_json(200, {"ok": True, "ytdlp": load_ytdlp() is not None, "ffmpeg": has_ffmpeg()})
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
    if not any(shutil.which(x) for x in ("deno", "node", "bun")):
        print("Note: YouTube extraction may need a JavaScript runtime (Deno or Node.js) to be installed.")
    atexit.register(lambda: shutil.rmtree(CACHE, ignore_errors=True))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Practice Player helper is running on port {args.port}  (Ctrl+C to stop)")
    if origins:
        print("Pages allowed to use it: " + ", ".join(sorted(origins)))
    if PAGE.exists():
        print(f"Local copy of the page: {url}")
        if not args.no_browser and not origins:
            webbrowser.open(url)
    else:
        print("practice-player.html was not found next to this script, so only your hosted page can use the helper.")
        if not origins:
            print("Start it with: --allow-origin https://YOUR-USER.github.io")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
