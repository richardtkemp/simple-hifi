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
  TPLAYER_MPD     mpd address, host:port   (default localhost:6600), for "playing now"

A download can be stopped (yt-dlp and everything it started is killed; songs of a
playlist that were already added stay). A failed download can be removed from the
list. Failed downloads are tried again by themselves: at most one an hour, each
song waiting longer after every failure (see retry_delay).

  python3 tplayer_web.py          run the server
  python3 tplayer_web.py --help   this text
"""

import json
import os
import random
import re
import shutil
import signal
import socket
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
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

MAX_BODY = 64 * 1024
LIST_TIMEOUT = 15 * 60       # seconds to list a playlist
ITEM_TIMEOUT = 60 * 60       # seconds to download one video (a Pi 3 is slow)
KEEP_JOBS = 100              # finished jobs kept in the list
KEEP_SECONDS = 14 * 24 * 3600
DELETED_NAME = ".deleted"  # hidden too: deleted songs wait here, recoverable by hand
TMP_NAME = ".tplayer-web-tmp"  # hidden: the music folder's .mpdignore skips dot names
AUTO_EVERY = 3600            # at most one automatic retry this often (seconds)
RETRY_BASE = 3600            # wait after the first failure; doubles after each further failure
RETRY_CAP = 7 * 24 * 3600    # longest wait between automatic retries of one song
RETRY_JITTER = 0.2           # each wait is spread by +-20% so songs drift apart
ACTIVE = ("queued", "listing", "downloading", "cancelling")

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


def song_title(file):
    """ "0003 Wheels on the bus.m4a" -> "Wheels on the bus" (display only)."""
    stem = file.rsplit(".", 1)[0] if "." in file else file
    m = NUMBER_PREFIX.match(stem)
    return (stem[m.end():] if m else stem).strip() or stem


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


def retry_delay(fails, r):
    """Seconds to wait before the automatic retry after the fails-th failure in a row.
    r is a random number in [0, 1): it spreads the wait by +-RETRY_JITTER.
    1 h, 2 h, 4 h, 8 h, 16 h, 32 h, 64 h, 128 h, then 7 days for good."""
    base = min(RETRY_BASE * 2 ** max(0, fails - 1), RETRY_CAP)
    return base * (1 - RETRY_JITTER + 2 * RETRY_JITTER * r)


# --------------------------------------------------------------------------- playing now

def mpd_query(addr, commands, timeout=2.0):
    """Send commands to mpd; return one {key: value} dict per command. Raises OSError."""
    host, _, port = addr.rpartition(":")
    with socket.create_connection((host or "localhost", int(port or 6600)), timeout=timeout) as sock, \
            sock.makefile("rwb") as f:
        if not f.readline().startswith(b"OK MPD"):
            raise OSError("not mpd")
        out = []
        for cmd in commands:
            f.write(cmd.encode() + b"\n")
            f.flush()
            reply = {}
            while True:
                line = f.readline()
                if not line:
                    raise OSError("mpd closed the connection")
                line = line.decode("utf-8", "replace").rstrip("\n")
                if line == "OK":
                    break
                if line.startswith("ACK"):
                    raise OSError(line)
                k, _, v = line.partition(": ")
                reply.setdefault(k, v)
            out.append(reply)
        return out


def now_playing(addr):
    """{"state": "play"|"pause"|"stop"|"off", "album", "song"} as the player sees it."""
    try:
        status, song = mpd_query(addr, ["status", "currentsong"])
    except (OSError, ValueError):
        return {"state": "off", "album": None, "song": None}
    state = status.get("state", "stop")
    file = song.get("file")
    if state not in ("play", "pause") or not file:
        return {"state": "stop", "album": None, "song": None}
    album, _, name = file.rpartition("/")
    return {"state": state, "album": album.split("/")[0] or None, "song": song_title(name)}


# --------------------------------------------------------------------------- icons

def make_png(size):
    """A round night-blue icon with a moon-yellow music note, drawn without any image library."""
    bg = (255, 255, 255, 0)
    orange = (35, 38, 74, 255)      # night #23264a (name kept: the drawing code below uses it)
    white = (244, 201, 93, 255)     # moon #f4c95d
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

    def __init__(self, music, state, ytdlp, clock=time.time, rng=random.random):
        self.music = Path(music)
        self.state = Path(state)
        self.ytdlp = ytdlp
        self.clock = clock      # for automatic retries (tests pass a fake clock)
        self.rng = rng
        self.file = self.state / "jobs.json"
        self.cond = threading.Condition()   # an RLock inside: _place may re-enter it
        self.jobs = []          # oldest first
        self.next_number = {}   # album -> next file number reserved (survives failed items)
        self.last_auto = 0.0    # when the last automatic retry started
        self.current = None     # the job the worker is on
        self.proc = None        # the yt-dlp it is running
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
        self.last_auto = data.get("last_auto", 0.0)
        # A stop that was under way when the service stopped is finished now.
        self.jobs = [j for j in self.jobs if j["status"] != "cancelling"]
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
        tmp.write_text(json.dumps({"jobs": self.jobs, "next_number": self.next_number,
                                   "last_auto": self.last_auto}, ensure_ascii=False))
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

    def album_songs(self, album):
        """The songs in an album, in play order: [{file, title}]."""
        if album not in self.albums():
            raise ValueError("That album isn't there any more.")
        d = self.album_dir(album)
        files = sorted((p.name for p in d.iterdir() if p.is_file() and not p.name.startswith(".")),
                       key=lambda n: n.encode("utf-8"))
        return [{"file": f, "title": song_title(f)} for f in files]

    def delete_song(self, album, file):
        """Move one song to <music>/.deleted/<album>/ (hidden from mpd, recoverable by hand).
        An album left with no songs is removed too. Returns True when the album went."""
        with self.cond:
            if album not in self.albums():
                raise ValueError("That album isn't there any more.")
            d = self.album_dir(album)
            if not file or "/" in file or "\\" in file or file.startswith(".") or not (d / file).is_file():
                raise ValueError("That song isn't there any more.")
            trash = self.music / DELETED_NAME / album
            trash.mkdir(parents=True, exist_ok=True)
            dest = trash / file
            if dest.exists():
                dest = trash / f"{int(time.time())}-{file}"
            os.replace(d / file, dest)
            if any(not p.name.startswith(".") for p in d.iterdir()):
                return False
            for p in d.iterdir():           # only hidden leftovers: keep them with the deleted songs
                os.replace(p, trash / p.name)
            d.rmdir()
            return True

    def song_counts(self, names):
        """Number of songs (non-hidden files) in each album folder."""
        counts = {}
        for n in names:
            try:
                counts[n] = sum(1 for p in (self.music / n).iterdir() if p.is_file() and not p.name.startswith("."))
            except OSError:
                pass
        return counts

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

    def clear(self, job_id=None):
        """Remove a failed download (or, with no id, every failed one) from the list.
        Songs it already added stay. A removed download is never tried again."""
        with self.cond:
            before = len(self.jobs)
            self.jobs = [j for j in self.jobs
                         if not (j["status"] == "failed" and job_id in (None, j["id"]))]
            if len(self.jobs) == before:
                return False
            self._save()
            return True

    def cancel(self, job_id):
        """Stop a waiting or running download and remove it from the list. A running yt-dlp
        and its children are killed; songs of a playlist that were already added stay."""
        with self.cond:
            job = next((j for j in self.jobs if j["id"] == job_id), None)
            if job is None or job["status"] not in ACTIVE:
                return False
            if self.current == job_id:
                job["status"] = "cancelling"     # the worker removes it once yt-dlp is gone
                self._kill()
            else:
                self.jobs.remove(job)
            self._save()
            return True

    def _kill(self):
        """Kill the running yt-dlp and everything it started (its own process group)."""
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _cancelled(self, job):
        return job["status"] == "cancelling"

    # ---- automatic retries

    def _failed_units(self):
        """The things that can be retried on their own: each failed song, and each failed
        link that never got as far as a song list. Pairs of (job, unit)."""
        for job in self.jobs:
            if job["status"] != "failed":
                continue
            if job["items"]:
                for item in job["items"]:
                    if item["status"] == "failed":
                        yield job, item
            else:
                yield job, job

    def _note_failure(self, unit):
        """Count a failure of a song (or link) and set when it is tried again by itself."""
        unit["fails"] = unit.get("fails", 0) + 1
        unit["retry_at"] = self.clock() + retry_delay(unit["fails"], self.rng())

    def auto_retry(self):
        """Called often (every minute). Starts at most one retry per AUTO_EVERY seconds: the
        failed song whose own wait ran out first, and only while nothing else is downloading."""
        with self.cond:
            now = self.clock()
            if now - self.last_auto < AUTO_EVERY:
                return None
            if any(j["status"] in ACTIVE for j in self.jobs):
                return None
            due = [(u.get("retry_at", 0), job, u) for job, u in self._failed_units()
                   if u.get("retry_at", 0) <= now]
            if not due:
                return None
            _, job, unit = min(due, key=lambda d: d[0])
            unit["status"] = "queued"
            unit["error"] = unit["detail"] = None
            job["status"] = "queued"
            job["error"] = job["detail"] = None
            self.last_auto = now
            self._save()
            self.cond.notify_all()
            return job["id"]

    def auto_loop(self):
        while True:
            time.sleep(60)
            try:
                self.auto_retry()
            except Exception:
                traceback.print_exc()

    def snapshot(self):
        with self.cond:
            jobs = json.loads(json.dumps(self.jobs[::-1]))
            now = self.clock()
            soonest = self.last_auto + AUTO_EVERY
            for job in jobs:
                if job["status"] == "failed":
                    units = [i for i in job["items"] if i["status"] == "failed"] or [job]
                    at = max(min(u.get("retry_at", 0) for u in units), soonest)
                    job["retry_in"] = max(0, int(at - now))
            return jobs

    # ---- worker

    def run(self):
        while True:
            with self.cond:
                job = next((j for j in self.jobs if j["status"] == "queued"), None)
                while job is None:
                    self.cond.wait()
                    job = next((j for j in self.jobs if j["status"] == "queued"), None)
                self.current = job["id"]
            try:
                self._do_job(job)
            except Exception as e:  # keep the worker alive whatever happens
                print(f"tplayer-web: job {job['id']} crashed:", file=sys.stderr)
                traceback.print_exc()
                with self.cond:
                    if not self._cancelled(job):
                        job["status"] = "failed"
                        job["error"] = friendly_error(str(e))
                        job["detail"] = repr(e)
                        job["finished"] = time.time()
                        self._note_failure(job)
                    self._save()
            finally:
                with self.cond:
                    self.current = None
                    if self._cancelled(job) and job in self.jobs:
                        self.jobs.remove(job)
                        self._save()

    def _set(self, obj, **kw):
        with self.cond:
            obj.update(kw)
            self._save()

    def _set_status(self, job, status):
        """Set a job's status unless it is being stopped (a stop must not be undone)."""
        with self.cond:
            if not self._cancelled(job):
                job["status"] = status
                self._save()

    def _run(self, args, timeout, on_line=None):
        """Run yt-dlp; return (exit code, stdout lines, stderr text)."""
        cmd = self.ytdlp.split() + ["--js-runtimes", "node", "--no-colors", "--ignore-config"] + args
        # Its own process group, so a stop kills Node and ffmpeg along with it.
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", start_new_session=True)
        with self.cond:
            self.proc = proc
            job = next((j for j in self.jobs if j["id"] == self.current), None)
            if job is not None and self._cancelled(job):   # stopped while starting
                self._kill()
        err = []
        t = threading.Thread(target=lambda: err.extend(proc.stderr), daemon=True)
        t.start()
        killed = []
        def on_timeout():
            killed.append(1)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        timer = threading.Timer(timeout, on_timeout)
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
            with self.cond:
                self.proc = None
        t.join(5)
        proc.stdout.close()
        proc.stderr.close()
        stderr = "".join(err)
        if killed:
            stderr += f"\ntimed out after {timeout} s"
        return code, out, stderr

    def _list(self, job):
        """Fill job["items"] with the videos behind job["url"], in playlist order."""
        self._set_status(job, "listing")
        code, out, err = self._run(["--flat-playlist", "--yes-playlist", "-J", job["url"]], LIST_TIMEOUT)
        if self._cancelled(job):
            return None
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
            if self._cancelled(job):
                return None
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
            if self._cancelled(job):
                return
            if problem:
                with self.cond:
                    job.update(status="failed", error=friendly_error(problem), detail=problem[-2000:],
                               finished=time.time())
                    self._note_failure(job)
                    self._save()
                return
        self._set_status(job, "downloading")
        for item in job["items"]:
            if self._cancelled(job):
                return
            if item["status"] == "queued":
                self._download(job, item)
        with self.cond:
            if self._cancelled(job):
                return
            failed = sum(1 for i in job["items"] if i["status"] == "failed")
            job["status"] = "failed" if failed else "done"
            if failed:
                job["error"] = (job["items"][0]["error"] if len(job["items"]) == 1 else
                                f"{failed} of {len(job['items'])} could not be downloaded. "
                                "Press Try again to retry them.")
            job["finished"] = time.time()
            job.pop("retry_at", None)   # a listing failure's wait; the songs keep their own
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
            with self.cond:     # a stop cannot slip in between this check and the placing
                if self._cancelled(job):
                    item.update(status="queued", percent=None)
                    return
                if code != 0 or len(files) != 1:
                    problem = err or f"yt-dlp exited with {code} and left {len(files)} files"
                    item.update(status="failed", percent=None, error=friendly_error(problem),
                                detail=problem[-2000:])
                    self._note_failure(item)
                    self._save()
                    return
                dest = self._place(job["album"], item, files[0])
                item.update(status="done", percent=100, file=dest.name, fails=0)
                item.pop("retry_at", None)
                self._save()
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
    # Drawn by make_icons.py. A new design gets a new name: Chrome and Cloudflare cache icons.
    "/icon-3-192.png": ("icon-3-192.png", "image/png"),
    "/icon-3-512.png": ("icon-3-512.png", "image/png"),
    "/icon-3-maskable-512.png": ("icon-3-maskable-512.png", "image/png"),
}


def make_handler(jobs, icons, mpd="localhost:6600"):
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
                body = (STATIC / name).read_bytes()
                if name == "index.html":
                    body = body.replace(b"APPJS_VERSION", str((STATIC / "app.js").stat().st_mtime_ns).encode())
                self._send(200, body, ctype)
            elif path in icons:
                self._send(200, icons[path], "image/png", "max-age=86400")
            elif path == "/api/albums":
                names = jobs.albums()
                self._json(200, {"albums": names, "songs": jobs.song_counts(names)})
            elif path == "/api/songs":
                album = (parse_qs(urlsplit(self.path).query).get("album") or [""])[0]
                try:
                    self._json(200, {"album": album, "songs": jobs.album_songs(album)})
                except ValueError as e:
                    self._json(404, {"error": str(e)})
            elif path == "/api/jobs":
                self._json(200, {"jobs": jobs.snapshot()})
            elif path == "/api/now":
                self._json(200, now_playing(mpd))
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
            if path == "/api/delete":
                try:
                    gone = jobs.delete_song(str(body.get("album") or ""), str(body.get("file") or ""))
                except ValueError as e:
                    return self._json(404, {"error": str(e)})
                return self._json(200, {"ok": True, "album_deleted": gone})
            if path == "/api/clear-failed":
                return self._json(200, {"ok": jobs.clear()})
            m = re.fullmatch(r"/api/jobs/([0-9a-f]+)/(retry|clear|cancel)", path)
            if m:
                ok = {"retry": jobs.retry, "clear": jobs.clear, "cancel": jobs.cancel}[m.group(2)](m.group(1))
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
    mpd = os.environ.get("TPLAYER_MPD", "localhost:6600")

    shutil.rmtree(Path(music) / TMP_NAME, ignore_errors=True)  # leftovers from a crash
    jobs = Jobs(music, state, ytdlp)
    threading.Thread(target=jobs.run, daemon=True).start()
    threading.Thread(target=jobs.auto_loop, daemon=True).start()
    icons = {"/icon-192.png": make_png(192), "/icon-512.png": make_png(512)}
    server = ThreadingHTTPServer((bind, port), make_handler(jobs, icons, mpd))
    server.daemon_threads = True
    print(f"tplayer-web: serving {music} on http://{bind}:{port}", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
