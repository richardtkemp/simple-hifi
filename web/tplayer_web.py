#!/usr/bin/env python3
"""simple-hifi player: a small web page for adding YouTube audio to an album.

Paste one or more YouTube links (videos or playlists), choose an album (a folder
under the music directory) or name a new one, and the Pi downloads the audio with
yt-dlp, one job at a time, into <music>/<album>/. The page is also an installable
web app with an Android share target (/share), so "Share" in the YouTube app
opens it with the link filled in.

Files are named "<4-digit number> <title>.<ext>": the number continues from the
album's highest number, so an album plays in the order things were added, and a
playlist keeps its own order. Nothing is ever overwritten.

Standard library only. Settings come from the environment:
  TPLAYER_MUSIC   music directory          (default /srv/music)
  TPLAYER_STATE   job list directory       (default $STATE_DIRECTORY or /var/lib/tplayer-web)
  TPLAYER_PORT    port                     (default 8090)
  TPLAYER_BIND    address                  (default 0.0.0.0)
  TPLAYER_YTDLP   yt-dlp command           (default yt-dlp)

  python3 tplayer_web.py          run the server
  python3 tplayer_web.py --help   this text
"""

import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import uuid
import zlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

MAX_BODY = 64 * 1024
LIST_TIMEOUT = 15 * 60       # seconds to list a playlist
ITEM_TIMEOUT = 60 * 60       # seconds to download one video (a Pi 3 is slow)
KEEP_JOBS = 100              # finished jobs kept in the list
KEEP_SECONDS = 14 * 24 * 3600
TMP_NAME = ".tplayer-web-tmp"  # hidden: the music folder's .mpdignore skips dot names

YOUTUBE_URL = re.compile(
    r"(?:https?://)?(?:[a-z0-9-]+\.)*(?:youtube\.com|youtu\.be|youtube-nocookie\.com)/[^\s<>\"']+",
    re.IGNORECASE,
)
ANY_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
NUMBER_PREFIX = re.compile(r"^(\d{4,})[ ._-]")


# --------------------------------------------------------------------------- text helpers

def extract_links(text):
    """YouTube links in text, in order, without duplicates; and the other http links."""
    links, others = [], []
    for m in YOUTUBE_URL.finditer(text or ""):
        url = m.group(0).rstrip(").,;!?")
        if not url.lower().startswith("http"):
            url = "https://" + url
        if url not in links:
            links.append(url)
    for m in ANY_URL.finditer(text or ""):
        url = m.group(0).rstrip(").,;!?")
        host = (urlsplit(url).hostname or "").lower()
        if not re.search(r"(^|\.)(youtube\.com|youtu\.be|youtube-nocookie\.com)$", host):
            if url not in others:
                others.append(url)
    return links, others


def clean_album(name):
    """A safe folder name for an album, or raise ValueError with a plain message."""
    name = unicodedata.normalize("NFC", (name or "").strip())
    if not name:
        raise ValueError("Please type a name for the new album.")
    if len(name) > 80:
        raise ValueError("That album name is too long. Please use a shorter one.")
    if name in (".", "..") or name.startswith("."):
        raise ValueError("An album name can't start with a dot.")
    for ch in name:
        if ch in "/\\" or unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp"):
            raise ValueError("An album name can't contain / or \\. Please use letters, numbers and spaces.")
    return name


def clean_title(title):
    """A video title as a file name part: no slashes, no control characters, not too long."""
    title = unicodedata.normalize("NFC", title or "")
    out = []
    for ch in title:
        if ch in "/\\":
            out.append("-")
        elif unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp"):
            out.append(" ")
        else:
            out.append(ch)
    title = re.sub(r"\s+", " ", "".join(out)).strip().lstrip(".").strip()
    # Keep the whole name well under the 255-byte file name limit.
    while len(title.encode("utf-8")) > 150:
        title = title[:-1]
    return title.rstrip() or "untitled"


def friendly_error(stderr):
    """A plain-words message for a yt-dlp failure."""
    s = (stderr or "").lower()
    if "no space left" in s:
        return "The player's memory card is full. Tell the person who set up the player."
    if "confirm you" in s and "bot" in s:
        return "YouTube is refusing downloads at the moment. Try again later; if it keeps happening, tell the person who set up the player."
    if "confirm your age" in s or "age-restricted" in s or "inappropriate for some users" in s:
        return "YouTube only shows this video to signed-in adults, so it can't be added."
    if any(k in s for k in ("private video", "video unavailable", "has been removed", "members-only",
                            "not available in your country", "this video is unavailable",
                            "account associated with this video has been terminated",
                            "playlist does not exist", "the playlist does not exist")):
        return "This video or playlist can't be downloaded (it is private, removed or restricted). Try a different one."
    if "unsupported url" in s or "is not a valid url" in s or "incomplete youtube id" in s:
        return "That doesn't look like a YouTube video or playlist link. Copy the link again and paste it."
    if any(k in s for k in ("unable to download", "timed out", "getaddrinfo", "name or service not known",
                            "connection reset", "network is unreachable", "temporary failure in name resolution",
                            "http error 5")):
        return "The player couldn't reach YouTube. Check the internet is working, then press Try again."
    return "Something went wrong with this download. Press Try again; if it fails again, tell the person who set up the player."


# --------------------------------------------------------------------------- icons

def make_png(size):
    """A round orange icon with a white music note, drawn without any image library."""
    bg = (255, 255, 255, 0)
    orange = (242, 140, 40, 255)
    white = (255, 255, 255, 255)
    c = size / 2

    def pixel(x, y):
        px, py = x + 0.5, y + 0.5
        if (px - c) ** 2 + (py - c) ** 2 > (size * 0.48) ** 2:
            return bg
        u, v = px / size, py / size
        # note heads (ellipses) and stems, a beam across the top
        for hx, hy in ((0.36, 0.68), (0.62, 0.62)):
            if ((u - hx) / 0.085) ** 2 + ((v - hy) / 0.065) ** 2 <= 1:
                return white
        if 0.41 <= u <= 0.45 and 0.30 <= v <= 0.68:
            return white
        if 0.67 <= u <= 0.71 and 0.24 <= v <= 0.62:
            return white
        top = 0.30 - (u - 0.41) * (0.06 / 0.30)
        if 0.41 <= u <= 0.71 and top <= v <= top + 0.07:
            return white
        return orange

    rows = b"".join(b"\x00" + b"".join(bytes(pixel(x, y)) for x in range(size)) for y in range(size))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9))
            + chunk(b"IEND", b""))


# --------------------------------------------------------------------------- jobs

class Jobs:
    """The job list, saved to disk after each change, and the one download worker."""

    def __init__(self, music, state, ytdlp):
        self.music = Path(music)
        self.state = Path(state)
        self.ytdlp = ytdlp
        self.file = self.state / "jobs.json"
        self.cond = threading.Condition()
        self.jobs = []          # oldest first
        self.next_number = {}   # album -> next file number reserved (survives failed items)
        self._load()

    # ---- persistence

    def _load(self):
        try:
            data = json.loads(self.file.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            print(f"tplayer-web: ignoring unreadable {self.file}: {e}", file=sys.stderr)
            return
        self.jobs = data.get("jobs", [])
        self.next_number = data.get("next_number", {})
        # Work that was running when the service stopped starts again.
        for job in self.jobs:
            if job["status"] in ("listing", "downloading"):
                job["status"] = "queued"
            for item in job.get("items", []):
                if item["status"] == "downloading":
                    item["status"] = "queued"
                    item["percent"] = None

    def _save(self):
        self.state.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"jobs": self.jobs, "next_number": self.next_number}, ensure_ascii=False))
        os.replace(tmp, self.file)

    def _prune(self):
        now = time.time()
        finished = [j for j in self.jobs if j["status"] in ("done", "failed")]
        drop = {j["id"] for j in finished if now - j.get("finished", now) > KEEP_SECONDS}
        extra = len(finished) - len(drop) - KEEP_JOBS
        if extra > 0:
            drop |= {j["id"] for j in [j for j in finished if j["id"] not in drop][:extra]}
        self.jobs = [j for j in self.jobs if j["id"] not in drop]

    # ---- albums

    def albums(self):
        try:
            names = [p.name for p in self.music.iterdir() if p.is_dir() and not p.name.startswith(".")]
        except FileNotFoundError:
            names = []
        return sorted(names, key=lambda n: n.encode("utf-8"))

    def album_dir(self, album):
        path = self.music / album
        if path.resolve().parent != self.music.resolve():
            raise ValueError("That album name can't be used. Please choose another.")
        return path

    def _reserve(self, album, count):
        """First of `count` file numbers for album, after every number on disk and every reservation."""
        highest = 0
        try:
            for p in self.album_dir(album).iterdir():
                m = NUMBER_PREFIX.match(p.name)
                if m:
                    highest = max(highest, int(m.group(1)))
        except FileNotFoundError:
            pass
        first = max(highest + 1, self.next_number.get(album, 1))
        self.next_number[album] = first + count
        return first

    # ---- api

    def add(self, links, album):
        with self.cond:
            new = []
            for url in links:
                job = {"id": uuid.uuid4().hex[:12], "url": url, "album": album, "status": "queued",
                       "title": None, "error": None, "detail": None, "items": [],
                       "created": time.time()}
                self.jobs.append(job)
                new.append(job)
            self._save()
            self.cond.notify_all()
            return new

    def retry(self, job_id):
        with self.cond:
            for job in self.jobs:
                if job["id"] == job_id and job["status"] == "failed":
                    job["status"] = "queued"
                    job["error"] = job["detail"] = None
                    for item in job["items"]:
                        if item["status"] == "failed":
                            item["status"] = "queued"
                            item["error"] = item["detail"] = None
                    self._save()
                    self.cond.notify_all()
                    return True
            return False

    def snapshot(self):
        with self.cond:
            return json.loads(json.dumps(self.jobs[::-1]))

    # ---- worker

    def run(self):
        while True:
            with self.cond:
                job = next((j for j in self.jobs if j["status"] == "queued"), None)
                while job is None:
                    self.cond.wait()
                    job = next((j for j in self.jobs if j["status"] == "queued"), None)
            try:
                self._do_job(job)
            except Exception as e:  # keep the worker alive whatever happens
                print(f"tplayer-web: job {job['id']} crashed:", file=sys.stderr)
                traceback.print_exc()
                with self.cond:
                    job["status"] = "failed"
                    job["error"] = friendly_error(str(e))
                    job["detail"] = repr(e)
                    job["finished"] = time.time()
                    self._save()

    def _set(self, obj, **kw):
        with self.cond:
            obj.update(kw)
            self._save()

    def _run(self, args, timeout, on_line=None):
        """Run yt-dlp; return (exit code, stdout lines, stderr text)."""
        cmd = self.ytdlp.split() + ["--js-runtimes", "node", "--no-colors", "--ignore-config"] + args
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace")
        err = []
        t = threading.Thread(target=lambda: err.extend(proc.stderr), daemon=True)
        t.start()
        killed = []
        timer = threading.Timer(timeout, lambda: (killed.append(1), proc.kill()))
        timer.start()
        out = []
        try:
            for line in proc.stdout:
                out.append(line)
                if on_line:
                    on_line(line)
            code = proc.wait()
        finally:
            timer.cancel()
        t.join(5)
        stderr = "".join(err)
        if killed:
            stderr += f"\ntimed out after {timeout} s"
        return code, out, stderr

    def _list(self, job):
        """Fill job["items"] with the videos behind job["url"], in playlist order."""
        self._set(job, status="listing")
        code, out, err = self._run(["--flat-playlist", "--yes-playlist", "-J", job["url"]], LIST_TIMEOUT)
        if code != 0:
            return err or f"yt-dlp exited with {code}"
        info = json.loads("".join(out))
        if info.get("_type") == "playlist":
            entries = [e for e in info.get("entries") or [] if e]
            title = info.get("title") or job["url"]
        else:
            entries = [info]
            title = info.get("title") or job["url"]
        if not entries:
            return "this playlist does not exist or is empty"
        items = []
        for e in entries:
            url = e.get("webpage_url") or e.get("url") or ""
            if e.get("id") and not url.startswith("http"):
                url = "https://www.youtube.com/watch?v=" + e["id"]
            items.append({"id": e.get("id"), "url": url, "title": e.get("title") or e.get("id") or url,
                          "status": "queued", "error": None, "detail": None, "file": None, "percent": None})
        with self.cond:
            self.album_dir(job["album"]).mkdir(mode=0o775, exist_ok=True)  # setgid comes from the music folder
            first = self._reserve(job["album"], len(items))
            for i, item in enumerate(items):
                item["number"] = first + i
            job["items"] = items
            job["title"] = title
            job["playlist"] = info.get("_type") == "playlist"
            self._save()
        return None

    def _do_job(self, job):
        if not job["items"]:
            problem = self._list(job)
            if problem:
                self._set(job, status="failed", error=friendly_error(problem), detail=problem[-2000:],
                          finished=time.time())
                return
        self._set(job, status="downloading")
        for item in job["items"]:
            if item["status"] == "queued":
                self._download(job, item)
        with self.cond:
            failed = sum(1 for i in job["items"] if i["status"] == "failed")
            job["status"] = "failed" if failed else "done"
            if failed:
                job["error"] = (job["items"][0]["error"] if len(job["items"]) == 1 else
                                f"{failed} of {len(job['items'])} could not be downloaded. "
                                "Press Try again to retry them.")
            job["finished"] = time.time()
            self._prune()
            self._save()

    def _download(self, job, item):
        self._set(item, status="downloading", percent=0)
        tmp = self.music / TMP_NAME / f"{job['id']}-{item['number']}"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)

        last = [0.0]

        def progress(line):
            if not line.startswith("PROGRESS "):
                return
            parts = line.split()
            try:
                done = float(parts[1])
                total = next((float(p) for p in parts[2:] if p not in ("NA", "None")), 0)
            except (ValueError, IndexError):
                return
            if total > 0 and time.time() - last[0] > 2:
                last[0] = time.time()
                self._set(item, percent=min(100, int(done * 100 / total)))

        try:
            code, _, err = self._run([
                "-f", "bestaudio[ext=m4a]/bestaudio/best",
                "-x",                       # audio only; keeps the original codec (no slow re-encode)
                "--no-playlist", "--no-overwrites", "--embed-metadata",
                "--newline", "--progress",
                "--progress-template",
                "download:PROGRESS %(progress.downloaded_bytes)s %(progress.total_bytes)s "
                "%(progress.total_bytes_estimate)s",
                "-o", str(tmp / "audio.%(ext)s"),
                item["url"],
            ], ITEM_TIMEOUT, progress)
            files = [p for p in tmp.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))
                     and p.name.startswith("audio.")]
            if code != 0 or len(files) != 1:
                problem = err or f"yt-dlp exited with {code} and left {len(files)} files"
                self._set(item, status="failed", percent=None, error=friendly_error(problem),
                          detail=problem[-2000:])
                return
            dest = self._place(job["album"], item, files[0])
            self._set(item, status="done", percent=100, file=dest.name)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _place(self, album, item, src):
        """Move src into the album as "<number> <title>.<ext>", never replacing a file."""
        folder = self.album_dir(album)
        folder.mkdir(mode=0o775, exist_ok=True)  # setgid comes from the music folder
        number = item["number"]
        while True:
            dest = folder / f"{number:04d} {clean_title(item['title'])}{src.suffix}"
            try:
                os.link(src, dest)      # fails if dest exists: no overwrite, even in a race
            except FileExistsError:
                with self.cond:
                    number = self._reserve(album, 1)
                continue
            os.unlink(src)
            return dest


# --------------------------------------------------------------------------- http

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/share": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
}


def make_handler(jobs, icons):
    class Handler(BaseHTTPRequestHandler):
        server_version = "tplayer-web"

        def log_message(self, fmt, *args):
            sys.stderr.write("tplayer-web: %s %s\n" % (self.address_string(), fmt % args))

        def _send(self, code, body, ctype, cache="no-store"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in STATIC_FILES:
                name, ctype = STATIC_FILES[path]
                self._send(200, (STATIC / name).read_bytes(), ctype)
            elif path in icons:
                self._send(200, icons[path], "image/png", "max-age=86400")
            elif path == "/api/albums":
                self._json(200, {"albums": jobs.albums()})
            elif path == "/api/jobs":
                self._json(200, {"jobs": jobs.snapshot()})
            else:
                self._json(404, {"error": "Not found."})

        def do_POST(self):
            path = urlsplit(self.path).path
            # JSON only: a plain cross-site form cannot send this content type.
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._json(415, {"error": "Send JSON."})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._json(413, {"error": "That is too much text. Paste fewer links at a time."})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json(400, {"error": "Bad request."})

            if path == "/api/jobs":
                return self._add(body)
            m = re.fullmatch(r"/api/jobs/([0-9a-f]+)/retry", path)
            if m:
                ok = jobs.retry(m.group(1))
                return self._json(200 if ok else 404, {"ok": ok})
            self._json(404, {"error": "Not found."})

        def _add(self, body):
            links, others = extract_links(str(body.get("links") or ""))
            if not links:
                msg = "There's no YouTube link in that. In YouTube, press Share, then Copy link, and paste it here."
                return self._json(400, {"error": msg})
            try:
                if body.get("new"):
                    album = clean_album(str(body.get("album") or ""))
                else:
                    album = str(body.get("album") or "")
                    if album not in jobs.albums():
                        return self._json(400, {"error": "Please choose an album."})
                jobs.album_dir(album)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            new = jobs.add(links, album)
            note = None
            if others:
                note = "Skipped (not YouTube): " + ", ".join(others)
            self._json(200, {"added": len(new), "album": album, "note": note})

    return Handler


def main():
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(__doc__)
        return
    music = os.environ.get("TPLAYER_MUSIC", "/srv/music")
    state = os.environ.get("TPLAYER_STATE") or os.environ.get("STATE_DIRECTORY") or "/var/lib/tplayer-web"
    port = int(os.environ.get("TPLAYER_PORT", "8090"))
    bind = os.environ.get("TPLAYER_BIND", "0.0.0.0")
    ytdlp = os.environ.get("TPLAYER_YTDLP", "yt-dlp")

    shutil.rmtree(Path(music) / TMP_NAME, ignore_errors=True)  # leftovers from a crash
    jobs = Jobs(music, state, ytdlp)
    threading.Thread(target=jobs.run, daemon=True).start()
    icons = {"/icon-192.png": make_png(192), "/icon-512.png": make_png(512)}
    server = ThreadingHTTPServer((bind, port), make_handler(jobs, icons))
    server.daemon_threads = True
    print(f"tplayer-web: serving {music} on http://{bind}:{port}", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
