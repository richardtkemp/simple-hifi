#!/usr/bin/env python3
"""Tests for tplayer_web.py, with a fake yt-dlp. Run: python3 test_tplayer_web.py"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tplayer_web as tw  # noqa: E402

FAKE_YTDLP = r'''#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
url = args[-1]
if "-J" in args:
    if "flaky" in url:
        sys.stderr.write("ERROR: [youtube] abc: Unable to download webpage: timed out\n")
        sys.exit(1)
    if "private" in url:
        sys.stderr.write("ERROR: [youtube] abc: Private video. Sign in if you've been granted access\n")
        sys.exit(1)
    if "list=PLslow" in url:
        print(json.dumps({"_type": "playlist", "title": "Slow list", "entries": [
            {"id": "aaa", "title": "First / song", "url": "https://www.youtube.com/watch?v=aaa"},
            {"id": "slow", "title": "Long one", "url": "https://www.youtube.com/watch?v=slow"}]}))
    elif "list=PLnet" in url:
        print(json.dumps({"_type": "playlist", "title": "Net list", "entries": [
            {"id": "aaa", "title": "First / song", "url": "https://www.youtube.com/watch?v=aaa"},
            {"id": "net", "title": "Flaky", "url": "https://www.youtube.com/watch?v=net"},
            {"id": "ccc", "title": "Third", "url": "https://www.youtube.com/watch?v=ccc"}]}))
    elif "list=" in url:
        print(json.dumps({"_type": "playlist", "title": "My list", "entries": [
            {"id": "aaa", "title": "First / song", "url": "https://www.youtube.com/watch?v=aaa"},
            {"id": "bad", "title": "Gone", "url": "https://www.youtube.com/watch?v=bad"},
            {"id": "ccc", "title": "Third", "url": "https://www.youtube.com/watch?v=ccc"}]}))
    else:
        print(json.dumps({"id": "one", "title": "Ένα τραγούδι", "webpage_url": url}))
    sys.exit(0)
if url.endswith("bad"):
    sys.stderr.write("ERROR: [youtube] bad: Video unavailable\n")
    sys.exit(1)
if url.endswith("net"):
    sys.stderr.write("ERROR: [youtube] net: Unable to download webpage: timed out\n")
    sys.exit(1)
out = args[args.index("-o") + 1].replace("%(ext)s", "m4a")
if url.endswith("slow"):
    # Like a real download: a part file, and a child process (ffmpeg/Node), then a long wait.
    import os, subprocess, time
    open(out + ".part", "w").write("half")
    here = os.path.dirname(os.path.abspath(sys.argv[0]))
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    open(os.path.join(here, "child.pid"), "w").write(f"{child.pid} {os.getpid()}")
    print("PROGRESS 10 100 NA", flush=True)
    time.sleep(120)
print("PROGRESS 50 100 NA", flush=True)
open(out, "w").write(url)
print("PROGRESS 100 100 NA", flush=True)
'''


class Helpers(unittest.TestCase):
    def test_extract_links(self):
        text = ("Watch this https://youtu.be/abc?si=x, and www.youtube.com/watch?v=def&list=PL1\n"
                "https://youtu.be/abc?si=x https://example.com/x")
        links, others = tw.extract_links(text)
        self.assertEqual(links, ["https://youtu.be/abc?si=x", "https://www.youtube.com/watch?v=def&list=PL1"])
        self.assertEqual(others, ["https://example.com/x"])

    def test_clean_album_rejects_escapes(self):
        for bad in ("", "  ", "..", ".hidden", "a/b", "../x", "a\\b", "x\x00y", "x" * 81):
            with self.assertRaises(ValueError, msg=repr(bad)):
                tw.clean_album(bad)
        self.assertEqual(tw.clean_album("  Ελληνικά τραγούδια "), "Ελληνικά τραγούδια")

    def test_clean_title(self):
        self.assertEqual(tw.clean_title("a/b\\c\nd"), "a-b-c d")
        self.assertEqual(tw.clean_title("..."), "untitled")
        self.assertLessEqual(len(tw.clean_title("é" * 400).encode()), 150)

    def test_friendly_error(self):
        self.assertIn("private", tw.friendly_error("ERROR: Private video"))
        self.assertIn("internet", tw.friendly_error("ERROR: Unable to download webpage: timed out"))
        self.assertIn("Try again", tw.friendly_error("weird"))

    def test_png(self):
        self.assertTrue(tw.make_png(48).startswith(b"\x89PNG"))


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def alive(pid):
    """True while pid runs (a zombie counts as gone)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


class FakeMpd:
    """Answers status/currentsong like mpd 0.24, from a dict the test changes."""

    def __init__(self):
        self.status = {"state": "stop"}
        self.song = {}
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.addr = "127.0.0.1:%d" % self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn, conn.makefile("rwb") as f:
                f.write(b"OK MPD 0.24.0\n")
                f.flush()
                for line in f:
                    reply = {"status": self.status, "currentsong": self.song}.get(line.strip().decode())
                    if reply is None:
                        break
                    for k, v in reply.items():
                        f.write(f"{k}: {v}\n".encode())
                    f.write(b"OK\n")
                    f.flush()

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)   # wakes the accept() above
        except OSError:
            pass
        self.sock.close()


class Backoff(unittest.TestCase):
    def test_schedule_doubles_to_a_cap_with_jitter(self):
        hour = 3600
        mid = [tw.retry_delay(n, 0.5) / hour for n in range(1, 11)]
        self.assertEqual(mid, [1, 2, 4, 8, 16, 32, 64, 128, 168, 168])
        self.assertAlmostEqual(tw.retry_delay(1, 0.0), 0.8 * hour)
        self.assertAlmostEqual(tw.retry_delay(1, 0.999999), 1.2 * hour, places=0)
        self.assertAlmostEqual(tw.retry_delay(20, 0.0), 0.8 * 168 * hour)


class NowPlaying(unittest.TestCase):
    def setUp(self):
        self.mpd = FakeMpd()

    def tearDown(self):
        self.mpd.close()

    def test_states(self):
        self.assertEqual(tw.now_playing(self.mpd.addr), {"state": "stop", "album": None, "song": None})
        self.mpd.status = {"state": "play", "song": "3"}
        self.mpd.song = {"file": "Pixabay/0017-starostin kids music.mp3", "Title": "x"}
        self.assertEqual(tw.now_playing(self.mpd.addr),
                         {"state": "play", "album": "Pixabay", "song": "starostin kids music"})
        self.mpd.status = {"state": "pause"}
        self.assertEqual(tw.now_playing(self.mpd.addr)["state"], "pause")

    def test_player_off(self):
        addr = self.mpd.addr
        self.mpd.close()
        self.assertEqual(tw.now_playing(addr)["state"], "off")


class Title(unittest.TestCase):
    def test_title_fills_page_and_manifest(self):
        title = 'Zoë\'s <b>"music"</b>'
        page = tw.fill_title("index.html", (tw.STATIC / "index.html").read_bytes(), title).decode()
        self.assertNotIn("PLAYER_TITLE", page)
        self.assertIn("<title>Zoë&#x27;s &lt;b&gt;&quot;music&quot;&lt;/b&gt;</title>", page)
        manifest = tw.fill_title("manifest.webmanifest", (tw.STATIC / "manifest.webmanifest").read_bytes(), title)
        data = json.loads(manifest)
        self.assertEqual(data["name"], title)
        self.assertEqual(data["short_name"], title)


class Flow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.music = root / "music"
        self.music.mkdir()
        (self.music / "old").mkdir()
        (self.music / "old" / "0007 existing.m4a").write_text("keep")
        (self.music / ".hidden").mkdir()
        fake = root / "yt-dlp"
        fake.write_text(FAKE_YTDLP)
        fake.chmod(0o755)
        self.pidfile = root / "child.pid"
        self.clock = FakeClock()
        self.jobs = tw.Jobs(self.music, root / "state", f"{sys.executable} {fake}",
                            clock=self.clock, rng=lambda: 0.5)
        threading.Thread(target=self.jobs.run, daemon=True).start()
        self.mpd = FakeMpd()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), tw.make_handler(self.jobs, {}, self.mpd.addr))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.mpd.close()
        if self.pidfile.exists():       # never leave a fake child behind
            for pid in map(int, self.pidfile.read_text().split()):
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass
        self.tmp.cleanup()

    def call(self, path, body=None):
        req = urllib.request.Request(self.base + path)
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def wait_idle(self):
        for _ in range(200):
            jobs = self.call("/api/jobs")[1]["jobs"]
            if all(j["status"] in ("done", "failed") for j in jobs):
                return jobs
            time.sleep(0.05)
        self.fail("jobs did not finish")

    def wait_for(self, check, what):
        for _ in range(200):
            if check():
                return
            time.sleep(0.05)
        self.fail(what)

    # ---- clear failed

    def test_clear_failed_download_is_not_retried(self):
        self.call("/api/jobs", {"links": "https://youtu.be/private", "album": "old"})
        self.call("/api/jobs", {"links": "https://youtu.be/one", "album": "old"})
        jobs = self.wait_idle()
        failed = next(j for j in jobs if j["status"] == "failed")
        done = next(j for j in jobs if j["status"] == "done")
        self.assertEqual(self.call(f"/api/jobs/{done['id']}/clear", {})[0], 404)   # only failed ones
        self.assertEqual(self.call(f"/api/jobs/{failed['id']}/clear", {}), (200, {"ok": True}))
        self.assertEqual([j["id"] for j in self.call("/api/jobs")[1]["jobs"]], [done["id"]])
        self.clock.t += 30 * 24 * 3600
        self.assertIsNone(self.jobs.auto_retry())
        self.assertEqual(self.call(f"/api/jobs/{failed['id']}/retry", {})[0], 404)

    def test_clear_all_failed(self):
        self.call("/api/jobs", {"links": "https://youtu.be/private https://youtu.be/private2 https://youtu.be/one",
                                "album": "old"})
        self.wait_idle()
        self.assertEqual(self.call("/api/clear-failed", {}), (200, {"ok": True}))
        self.assertEqual([j["status"] for j in self.call("/api/jobs")[1]["jobs"]], ["done"])

    # ---- cancel

    def test_cancel_waiting_download(self):
        self.call("/api/jobs", {"links": "https://youtu.be/slow", "album": "old"})
        self.wait_for(self.pidfile.exists, "slow download did not start")
        self.call("/api/jobs", {"links": "https://youtu.be/one", "album": "old"})
        waiting = self.call("/api/jobs")[1]["jobs"][0]
        self.assertEqual(waiting["status"], "queued")
        self.assertEqual(self.call(f"/api/jobs/{waiting['id']}/cancel", {}), (200, {"ok": True}))
        running = self.call("/api/jobs")[1]["jobs"]
        self.assertEqual(len(running), 1)
        self.call(f"/api/jobs/{running[0]['id']}/cancel", {})
        self.wait_for(lambda: not self.call("/api/jobs")[1]["jobs"], "jobs not removed")
        self.assertEqual(sorted(os.listdir(self.music / "old")), ["0007 existing.m4a"])

    def test_cancel_mid_download_kills_children_and_keeps_finished_songs(self):
        # aaa downloads, then "slow" hangs mid-download with a child process and a part file.
        self.call("/api/jobs", {"links": "https://www.youtube.com/watch?v=aaa&list=PLslow", "album": "old"})
        self.wait_for(self.pidfile.exists, "slow download did not start")
        child, ytdlp = map(int, self.pidfile.read_text().split())
        self.assertTrue(alive(child) and alive(ytdlp))
        job = self.call("/api/jobs")[1]["jobs"][0]
        self.assertEqual(job["status"], "downloading")
        tmp = self.music / tw.TMP_NAME
        self.assertTrue(any(tmp.rglob("*.part")))
        self.assertEqual(self.call(f"/api/jobs/{job['id']}/cancel", {}), (200, {"ok": True}))
        self.wait_for(lambda: not self.call("/api/jobs")[1]["jobs"], "stopped job not removed")
        self.wait_for(lambda: not alive(child) and not alive(ytdlp), "yt-dlp or its child still running")
        self.assertEqual(sorted(os.listdir(self.music / "old")), ["0007 existing.m4a", "0008 First - song.m4a"])
        self.assertFalse(tmp.exists() and any(tmp.iterdir()))
        # The worker carries on with the next download.
        self.call("/api/jobs", {"links": "https://youtu.be/one", "album": "old"})
        self.assertEqual(self.wait_idle()[0]["status"], "done")

    def test_cancel_refuses_finished(self):
        self.call("/api/jobs", {"links": "https://youtu.be/one", "album": "old"})
        job = self.wait_idle()[0]
        self.assertEqual(self.call(f"/api/jobs/{job['id']}/cancel", {})[0], 404)

    # ---- automatic retry

    def test_auto_retry_backoff_with_fake_clock(self):
        hour = 3600
        self.call("/api/jobs", {"links": "https://www.youtube.com/watch?v=net", "album": "old"})
        job = self.wait_idle()[0]
        self.assertEqual(job["status"], "failed")
        item = job["items"][0]
        self.assertEqual(item["fails"], 1)
        self.assertEqual(item["retry_at"], self.clock.t + hour)
        self.assertEqual(job["retry_in"], hour)
        start = self.clock.t
        # The wait doubles after each failure: retried at +1 h, then +2 h later, then +4 h later.
        tried = []
        for minutes in range(0, 10 * 60, 1):
            self.clock.t = start + minutes * 60
            if self.jobs.auto_retry():
                tried.append(minutes)
                self.wait_idle()
        self.assertEqual(tried, [60, 180, 420])
        item = self.wait_idle()[0]["items"][0]
        self.assertEqual(item["fails"], 4)
        self.assertEqual(item["retry_at"], start + 420 * 60 + 8 * hour)

    def test_auto_retry_at_most_one_an_hour_and_only_failed_songs(self):
        self.call("/api/jobs", {"links": "https://www.youtube.com/watch?v=aaa&list=PLnet", "album": "New", "new": True})
        self.call("/api/jobs", {"links": "https://youtu.be/flaky", "album": "old"})
        self.wait_idle()
        self.clock.t += 5 * 3600          # both are due
        first = self.jobs.auto_retry()
        self.assertIsNotNone(first)
        self.assertIsNone(self.jobs.auto_retry())   # busy, and less than an hour since
        self.wait_idle()
        self.clock.t += 59 * 60
        self.assertIsNone(self.jobs.auto_retry())
        self.clock.t += 60
        second = self.jobs.auto_retry()
        self.assertNotIn(second, (None, first))
        jobs = self.wait_idle()
        pl = next(j for j in jobs if j.get("playlist"))
        self.assertEqual([i["status"] for i in pl["items"]], ["done", "failed", "done"])
        self.assertEqual([i.get("fails", 0) for i in pl["items"]], [0, 2, 0])
        # Songs that were added are not downloaded again.
        self.assertEqual(sorted(os.listdir(self.music / "New")), ["0001 First - song.m4a", "0003 Third.m4a"])

    def test_auto_retry_waits_for_running_download(self):
        self.call("/api/jobs", {"links": "https://youtu.be/flaky", "album": "old"})
        self.wait_idle()
        self.clock.t += 2 * 3600
        self.call("/api/jobs", {"links": "https://youtu.be/slow", "album": "old"})
        self.wait_for(self.pidfile.exists, "slow download did not start")
        self.assertIsNone(self.jobs.auto_retry())
        running = self.call("/api/jobs")[1]["jobs"][0]
        self.call(f"/api/jobs/{running['id']}/cancel", {})
        self.wait_idle()
        self.assertIsNotNone(self.jobs.auto_retry())

    def test_manual_retry_still_works_and_counts(self):
        self.call("/api/jobs", {"links": "https://youtu.be/flaky", "album": "old"})
        job = self.wait_idle()[0]
        self.assertEqual(job["fails"], 1)
        self.assertEqual(self.call(f"/api/jobs/{job['id']}/retry", {})[0], 200)
        job = self.wait_idle()[0]
        self.assertEqual(job["fails"], 2)
        self.assertEqual(job["retry_in"], 2 * 3600)

    def test_no_auto_retry_for_private_or_removed(self):
        # a video that can never work is not retried by itself; a network failure is.
        self.call("/api/jobs", {"links": "https://youtu.be/private", "album": "old"})
        self.call("/api/jobs", {"links": "https://www.youtube.com/watch?v=bad", "album": "old"})
        jobs = self.wait_idle()
        self.assertEqual([j["status"] for j in jobs], ["failed", "failed"])
        for j in jobs:
            self.assertNotIn("retry_in", j)
        self.clock.t += 30 * 24 * 3600
        self.assertIsNone(self.jobs.auto_retry())
        # The manual Try again still works on them.
        self.assertEqual(self.call(f"/api/jobs/{jobs[0]['id']}/retry", {})[0], 200)
        self.wait_idle()
        self.call("/api/jobs", {"links": "https://youtu.be/flaky", "album": "old"})
        self.wait_idle()
        self.clock.t += 2 * 3600
        self.assertIsNotNone(self.jobs.auto_retry())

    # ---- playing now

    def test_now_playing_endpoint(self):
        self.mpd.status = {"state": "pause"}
        self.mpd.song = {"file": "old/0007 existing.m4a"}
        self.assertEqual(self.call("/api/now"), (200, {"state": "pause", "album": "old", "song": "existing"}))

    def test_albums_hide_dot_folders(self):
        self.assertEqual(self.call("/api/albums")[1], {"albums": ["old"], "songs": {"old": 1}})

    def test_list_songs(self):
        (self.music / "old" / "0002 second.mp3").write_text("x")
        (self.music / "old" / ".junk").write_text("x")
        code, r = self.call("/api/songs?album=old")
        self.assertEqual(code, 200, r)
        self.assertEqual(r["songs"], [{"file": "0002 second.mp3", "title": "second"},
                                      {"file": "0007 existing.m4a", "title": "existing"}])
        self.assertEqual(self.call("/api/songs?album=..")[0], 404)
        self.assertEqual(self.call("/api/songs?album=nope")[0], 404)

    def test_delete_song_then_album_goes_with_last_song(self):
        (self.music / "old" / "0002 second.mp3").write_text("x")
        code, r = self.call("/api/delete", {"album": "old", "file": "0002 second.mp3"})
        self.assertEqual((code, r), (200, {"ok": True, "album_deleted": False}))
        self.assertFalse((self.music / "old" / "0002 second.mp3").exists())
        self.assertTrue((self.music / ".deleted" / "old" / "0002 second.mp3").exists())
        code, r = self.call("/api/delete", {"album": "old", "file": "0007 existing.m4a"})
        self.assertEqual((code, r), (200, {"ok": True, "album_deleted": True}))
        self.assertFalse((self.music / "old").exists())
        self.assertEqual(self.call("/api/albums")[1]["albums"], [])

    def test_delete_refuses_escapes(self):
        (self.music / "secret.txt").write_text("x")
        for album, file in (("old", "../secret.txt"), ("old", ".."), ("..", "secret.txt"),
                            ("old", ""), ("old", "missing.m4a"), (".hidden", "x")):
            code, _ = self.call("/api/delete", {"album": album, "file": file})
            self.assertEqual(code, 404, (album, file))
        self.assertTrue((self.music / "secret.txt").exists())
        self.assertTrue((self.music / "old" / "0007 existing.m4a").exists())

    def test_add_to_existing_album_continues_numbering(self):
        code, r = self.call("/api/jobs", {"links": "https://youtu.be/one", "album": "old"})
        self.assertEqual(code, 200, r)
        jobs = self.wait_idle()
        self.assertEqual(jobs[0]["status"], "done", jobs)
        names = sorted(p.name for p in (self.music / "old").iterdir())
        self.assertEqual(names, ["0007 existing.m4a", "0008 Ένα τραγούδι.m4a"])
        self.assertEqual((self.music / "old" / "0007 existing.m4a").read_text(), "keep")
        self.assertFalse((self.music / tw.TMP_NAME).exists() and any((self.music / tw.TMP_NAME).iterdir()))

    def test_playlist_order_failure_and_retry(self):
        code, r = self.call("/api/jobs", {"links": "https://www.youtube.com/watch?v=aaa&list=PL1",
                                          "album": "New one", "new": True})
        self.assertEqual(code, 200, r)
        job = self.wait_idle()[0]
        self.assertEqual(job["status"], "failed")
        self.assertIn("1 of 3", job["error"])
        self.assertEqual([i["status"] for i in job["items"]], ["done", "failed", "done"])
        self.assertIn("private, removed", job["items"][1]["error"])
        names = sorted(p.name for p in (self.music / "New one").iterdir())
        self.assertEqual(names, ["0001 First - song.m4a", "0003 Third.m4a"])

        # A later job takes numbers after the reserved ones, so a retry keeps playlist order.
        self.call("/api/jobs", {"links": "https://youtu.be/one", "album": "New one"})
        self.wait_idle()
        self.assertIn("0004 Ένα τραγούδι.m4a", os.listdir(self.music / "New one"))
        self.assertEqual(self.call(f"/api/jobs/{job['id']}/retry", {})[0], 200)
        job = next(j for j in self.wait_idle() if j["id"] == job["id"])
        self.assertEqual(job["items"][1]["status"], "failed")  # fake always fails it; slot 2 stays free

    def test_never_overwrites(self):
        (self.music / "old" / "0008 Ένα τραγούδι.m4a").write_text("mine")
        # Reservations look at the disk, so force the clash by placing at a taken number.
        src = self.music / "src.m4a"
        src.write_text("new")
        dest = self.jobs._place("old", {"number": 8, "title": "Ένα τραγούδι"}, src)
        self.assertEqual((self.music / "old" / "0008 Ένα τραγούδι.m4a").read_text(), "mine")
        self.assertEqual(dest.read_text(), "new")
        self.assertNotEqual(dest.name, "0008 Ένα τραγούδι.m4a")

    def test_errors_in_plain_words(self):
        code, r = self.call("/api/jobs", {"links": "hello", "album": "old"})
        self.assertEqual(code, 400)
        self.assertIn("no YouTube link", r["error"])
        code, r = self.call("/api/jobs", {"links": "https://youtu.be/x", "album": "nope"})
        self.assertEqual(code, 400)
        code, r = self.call("/api/jobs", {"links": "https://youtu.be/x", "album": "../etc", "new": True})
        self.assertEqual(code, 400)
        self.assertFalse((self.music.parent / "etc").exists())
        self.call("/api/jobs", {"links": "https://youtu.be/private", "album": "old"})
        job = self.wait_idle()[0]
        self.assertEqual(job["status"], "failed")
        self.assertIn("private", job["error"])

    def test_form_post_refused(self):
        req = urllib.request.Request(self.base + "/api/jobs", data=b"links=x",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 415)

    def test_restart_requeues_running_work(self):
        self.jobs.jobs.append({"id": "abc", "url": "u", "album": "old", "status": "downloading",
                               "items": [{"status": "downloading", "percent": 5}]})
        self.jobs._save()
        again = tw.Jobs(self.music, self.jobs.state, "true")
        self.assertEqual(again.jobs[-1]["status"], "queued")
        self.assertEqual(again.jobs[-1]["items"][0]["status"], "queued")


if __name__ == "__main__":
    unittest.main()
