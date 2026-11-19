#!/usr/bin/env python3
"""Tests for tplayer_web.py, with a fake yt-dlp. Run: python3 test_tplayer_web.py"""

import json
import os
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
    if "private" in url:
        sys.stderr.write("ERROR: [youtube] abc: Private video. Sign in if you've been granted access\n")
        sys.exit(1)
    if "list=" in url:
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
out = args[args.index("-o") + 1].replace("%(ext)s", "m4a")
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
        self.jobs = tw.Jobs(self.music, root / "state", f"{sys.executable} {fake}")
        threading.Thread(target=self.jobs.run, daemon=True).start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), tw.make_handler(self.jobs, {}))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
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

    def test_albums_hide_dot_folders(self):
        self.assertEqual(self.call("/api/albums")[1], {"albums": ["old"]})

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
