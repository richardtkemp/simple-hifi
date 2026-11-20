// Music: paste or share YouTube links, pick an album, watch the downloads.
"use strict";

const $ = (id) => document.getElementById(id);
const NEW = "\u0000new";
let pollTimer = null;

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function showMsg(text, good) {
  const m = $("msg");
  m.hidden = !text;
  m.textContent = text || "";
  m.className = "msg " + (good ? "good" : "bad");
}

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  let r;
  try {
    r = await fetch(path, opts);
  } catch (e) {
    throw new Error("Can't reach the music player. Check the phone is online and try again.");
  }
  let data = {};
  try { data = await r.json(); } catch (e) { /* not JSON */ }
  if (!r.ok) throw new Error(data.error || "The music player had a problem (" + r.status + "). Try again.");
  return data;
}

// ---- album picker (tiles)

let albums = [], songs = {}, chosen = null;   // chosen: an album name, or NEW

// "1-nursery-rhymes" -> "Nursery rhymes" (display only; the folder keeps its name)
function pretty(name) {
  const s = name.replace(/^\d+[-_. ]+/, "").replace(/[-_]+/g, " ").trim() || name;
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// A soft colour per album, the same every time.
function tint(name) {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.codePointAt(0)) % 360;
  return `hsl(${(h * 7) % 360} 62% 92%)`;
}

function renderAlbums() {
  const box = $("albums");
  box.textContent = "";
  for (const a of albums) {
    const b = el("button", "album");
    b.type = "button";
    b.style.setProperty("--tint", tint(a));
    b.append(el("span", "n", pretty(a)));
    const n = songs[a];
    if (n != null) b.append(el("span", "c", n === 1 ? "1 song" : n + " songs"));
    b.setAttribute("aria-pressed", String(chosen === a));
    b.addEventListener("click", () => choose(a));
    box.append(b);
  }
  const nb = el("button", "album new");
  nb.type = "button";
  nb.append(el("span", "n", "+ New album"));
  nb.setAttribute("aria-pressed", String(chosen === NEW));
  nb.addEventListener("click", () => choose(NEW));
  box.append(nb);
}

function choose(a) {
  chosen = a;
  renderAlbums();
  const isNew = a === NEW;
  $("newname").hidden = !isNew;
  if (isNew) $("newname").focus();
  updateAdd();
  loadSongs();
}

// ---- songs already in the chosen album, with delete

let songsFor = null;

async function loadSongs() {
  const box = $("songs");
  if (!chosen || chosen === NEW) { box.hidden = true; songsFor = null; return; }
  const album = chosen;
  let list;
  try {
    list = (await api("/api/songs?album=" + encodeURIComponent(album))).songs;
  } catch (e) {
    return;   // the album list refresh will catch up
  }
  if (album !== chosen) return;           // she picked another album meanwhile
  // Leave the list alone while a delete is waiting for its second tap.
  if (songsFor === album && box.querySelector(".del.sure")) return;
  songsFor = album;
  if (songs[album] !== list.length) { songs[album] = list.length; renderAlbums(); }
  box.textContent = "";
  box.hidden = false;
  const head = el("div", "head");
  head.append(el("b", null, "In this album"), el("span", null, list.length === 1 ? "1 song" : list.length + " songs"));
  box.append(head);
  const ol = el("ol");
  for (const s of list) {
    const li = el("li");
    li.append(el("span", "nm", s.title));
    const del = el("button", "del", "✕");
    del.type = "button";
    del.title = "Delete " + s.title;
    del.setAttribute("aria-label", "Delete " + s.title);
    let timer = null;
    del.addEventListener("click", async () => {
      if (!del.classList.contains("sure")) {      // first tap asks, second tap deletes
        del.classList.add("sure");
        del.textContent = "Delete";
        timer = setTimeout(() => { del.classList.remove("sure"); del.textContent = "✕"; }, 3000);
        return;
      }
      clearTimeout(timer);
      del.disabled = true;
      try {
        const r = await api("/api/delete", { album, file: s.file });
        if (r.album_deleted) {
          showMsg("Deleted “" + s.title + "”. That was the last song, so the album “" + pretty(album) + "” is gone too.", true);
          chosen = null;
          await loadAlbums();
        } else {
          li.remove();
          songsFor = null;
          await loadSongs();
        }
      } catch (e) {
        showMsg(e.message);
        del.disabled = false;
      }
    });
    li.append(del);
    ol.append(li);
  }
  box.append(ol);
  if (!list.length) box.append(el("p", "note", "No songs yet."));
  else box.append(el("p", "note", "New songs play after these."));
}

function updateAdd() {
  const btn = $("add");
  btn.textContent = "";
  if (chosen === NEW) {
    const n = $("newname").value.trim();
    btn.append(n ? "Add to " : "Add to new album");
    if (n) btn.append(el("span", "to", n));
  } else if (chosen) {
    btn.append("Add to ", el("span", "to", pretty(chosen)));
  } else {
    btn.append("Add");
  }
}
$("newname").addEventListener("input", updateAdd);

async function loadAlbums(select) {
  const data = await api("/api/albums");
  albums = data.albums || [];
  songs = data.songs || {};
  const want = select || chosen || localStorage.getItem("album");
  chosen = albums.includes(want) ? want : (albums.length ? albums[albums.length - 1] : NEW);
  choose(chosen);
}

// ---- paste button (Chrome asks once for clipboard access)

if (navigator.clipboard && navigator.clipboard.readText) {
  $("paste").hidden = false;
  $("paste").addEventListener("click", async () => {
    try {
      const t = (await navigator.clipboard.readText()).trim();
      if (!t) return showMsg("The clipboard is empty. Copy a YouTube link first.");
      const cur = $("links").value.trim();
      $("links").value = cur ? cur + "\n" + t : t;
      showMsg("");
    } catch (e) {
      showMsg("Couldn't read the clipboard. Press and hold in the box and choose Paste.");
    }
  });
}

// ---- shared from another app (Android share sheet opens /share?text=...)

function takeShared() {
  const q = new URLSearchParams(location.search);
  const parts = ["url", "text", "title"].map((k) => q.get(k)).filter(Boolean);
  if (!parts.length) return;
  // Apps share "link + video title"; keep only the links. No link at all: show what came.
  const links = [...new Set(parts.join("\n").match(/https?:\/\/[^\s<>"']+/gi) || [])];
  $("links").value = (links.length ? links : parts).join("\n");
  $("shared").hidden = false;
  history.replaceState(null, "", "/");   // a reload must not share it again
}

// ---- add

$("add").addEventListener("click", async () => {
  const isNew = chosen === NEW;
  const album = isNew ? $("newname").value.trim() : chosen;
  if (!$("links").value.trim()) return showMsg("Paste a YouTube link first.");
  if (!chosen) return showMsg("Pick an album first.");
  if (isNew && !album) return showMsg("Type a name for the new album.");
  $("add").disabled = true;
  showMsg("");
  try {
    const r = await api("/api/jobs", { links: $("links").value, album, new: isNew });
    localStorage.setItem("album", r.album);
    $("links").value = "";
    $("newname").value = "";
    $("shared").hidden = true;
    const n = r.added === 1 ? "1 link" : r.added + " links";
    showMsg("Added " + n + " to “" + pretty(r.album) + "”. You can close this page; the player keeps going."
      + (r.note ? "\n" + r.note : ""), true);
    await loadAlbums(r.album);
    refresh();
  } catch (e) {
    showMsg(e.message);
  } finally {
    $("add").disabled = false;
    updateAdd();
  }
});

// ---- download list

function statusText(job) {
  const items = job.items || [];
  const done = items.filter((i) => i.status === "done").length;
  switch (job.status) {
    case "queued": return items.length ? "Waiting to continue…" : "Waiting its turn…";
    case "listing": return "Looking up the link…";
    case "downloading": {
      const cur = items.find((i) => i.status === "downloading");
      const pos = cur ? items.indexOf(cur) + 1 : done;
      const pct = cur && cur.percent != null ? " — " + cur.percent + "%" : "";
      return items.length > 1 ? "Downloading " + pos + " of " + items.length + pct : "Downloading" + pct;
    }
    case "done": return "Added " + (done === 1 ? "1 song" : done + " songs") + " to “" + pretty(job.album) + "”";
    case "cancelling": return "Stopping…";
    case "failed": return (done ? "Added " + done + ". " : "") + (job.error || "It didn't work.");
  }
  return job.status;
}

function percent(job) {
  const items = job.items || [];
  if (!items.length) return 0;
  let sum = 0;
  for (const i of items) sum += i.status === "done" || i.status === "failed" ? 100 : (i.percent || 0);
  return Math.round(sum / items.length);
}

function videoId(job) {
  const items = job.items || [];
  if (items.length && items[0].id) return items[0].id;
  const m = /(?:v=|youtu\.be\/|shorts\/)([\w-]{11})/.exec(job.url || "");
  return m ? m[1] : null;
}

// "in about 3 hours" — rough on purpose: the player only retries when nothing else is downloading.
function whenText(sec) {
  if (sec < 120) return "in a minute or two";
  if (sec < 3600) return "in about " + Math.round(sec / 600) * 10 + " minutes";
  const h = Math.round(sec / 3600);
  if (h < 36) return h === 1 ? "in about an hour" : "in about " + h + " hours";
  return "in about " + Math.round(sec / 86400) + " days";
}

// First tap asks, second tap stops (like delete), so a stray tap can't lose a long download.
function stopButton(job) {
  const b = el("button", "soft", "Stop");
  b.type = "button";
  b.dataset.id = job.id;
  b.addEventListener("click", async () => {
    if (!b.classList.contains("sure")) {
      b.classList.add("sure");
      b.textContent = "Stop this download";
      stopAsking = job.id;
      setTimeout(() => { if (stopAsking === job.id) stopAsking = null; b.classList.remove("sure"); b.textContent = "Stop"; }, 4000);
      return;
    }
    stopAsking = null;
    b.disabled = true;
    try {
      await api("/api/jobs/" + job.id + "/cancel", {});
      const items = job.items || [];
      const done = items.filter((i) => i.status === "done").length;
      showMsg("Stopped." + (done ? " The " + (done === 1 ? "song" : done + " songs") + " already added stay in the album." : ""), true);
    } catch (e) { showMsg(e.message); }
    refresh();
  });
  return b;
}
let stopAsking = null;   // job id whose Stop is waiting for its second tap

function renderJob(job) {
  const box = el("div", "job " + job.status);
  const th = el("div", "thumb");
  const vid = videoId(job);
  if (vid) th.style.backgroundImage = `url("https://i.ytimg.com/vi/${vid}/mqdefault.jpg")`;
  box.append(th);
  const body = el("div");
  box.append(body);
  body.append(el("div", "t", job.title || job.url));
  body.append(el("div", "s", statusText(job)));
  if (job.status === "downloading" || job.status === "listing") {
    const bar = el("div", "prog");
    const fill = el("div");
    fill.style.width = percent(job) + "%";
    bar.append(fill);
    body.append(bar);
  }
  if (job.status === "failed") {
    if (job.retry_in != null) body.append(el("div", "auto", "It will try again by itself " + whenText(job.retry_in) + "."));
    const acts = el("div", "acts");
    const b = el("button", "retry", "Try again");
    b.type = "button";
    b.addEventListener("click", async () => {
      b.disabled = true;
      try { await api("/api/jobs/" + job.id + "/retry", {}); } catch (e) { showMsg(e.message); }
      refresh();
    });
    const rm = el("button", "soft", "Remove");
    rm.type = "button";
    rm.title = "Take it off the list. It won't be tried again.";
    rm.addEventListener("click", async () => {
      rm.disabled = true;
      try { await api("/api/jobs/" + job.id + "/clear", {}); } catch (e) { showMsg(e.message); }
      refresh();
    });
    acts.append(b, rm);
    body.append(acts);
  }
  if (job.status === "queued" || job.status === "listing" || job.status === "downloading") {
    const acts = el("div", "acts");
    acts.append(stopButton(job));
    body.append(acts);
  }
  const items = job.items || [];
  if (items.length > 1 || job.detail || items.some((i) => i.detail)) {
    const d = el("details");
    d.append(el("summary", null, items.length > 1 ? "Songs (" + items.length + ")" : "Details"));
    const ol = el("ol");
    for (const i of items) {
      const li = el("li", i.status, i.title + (i.status === "failed" ? " — " + i.error : ""));
      if (i.detail) li.append(el("div", "tech", i.detail));
      ol.append(li);
    }
    if (items.length) d.append(ol);
    if (job.detail) d.append(el("div", "tech", job.detail));
    body.append(d);
  }
  return box;
}

async function refresh() {
  clearTimeout(pollTimer);
  let busy = false;
  try {
    const { jobs } = await api("/api/jobs");
    const list = $("jobs");
    // Keep any open "Songs" lists open across refreshes.
    const open = new Set([...list.querySelectorAll("details[open]")].map((d) => d.dataset.id));
    if (stopAsking && jobs.some((j) => j.id === stopAsking)) {   // don't redraw under a pending Stop
      pollTimer = setTimeout(refresh, 1000);
      return;
    }
    list.textContent = "";
    const failed = jobs.filter((j) => j.status === "failed").length;
    $("clearfailed").hidden = failed < 2;
    if (!jobs.length) list.append(el("p", "empty", "Nothing yet. Songs you add show up here."));
    for (const j of jobs) {
      const node = renderJob(j);
      const d = node.querySelector("details");
      if (d) { d.dataset.id = j.id; d.open = open.has(j.id); }
      list.append(node);
      if (j.status === "queued" || j.status === "listing" || j.status === "downloading" || j.status === "cancelling") busy = true;
    }
  } catch (e) {
    busy = true;   // keep trying
  }
  loadSongs();
  pollTimer = setTimeout(refresh, busy ? 3000 : 20000);
}

$("clearfailed").addEventListener("click", async () => {
  $("clearfailed").disabled = true;
  try { await api("/api/clear-failed", {}); } catch (e) { showMsg(e.message); }
  $("clearfailed").disabled = false;
  refresh();
});

// ---- playing now (what the player is playing, from mpd)

let nowTimer = null;

async function refreshNow() {
  clearTimeout(nowTimer);
  try {
    const n = await api("/api/now");
    const box = $("now");
    box.hidden = false;
    box.classList.toggle("play", n.state === "play");
    if (n.state === "play" || n.state === "pause") {
      $("now-state").textContent = n.state === "play" ? "Playing now" : "Paused";
      $("now-song").textContent = n.song || "";
      $("now-album").textContent = n.album ? "from “" + pretty(n.album) + "”" : "";
    } else {
      $("now-state").textContent = "Playing now";
      $("now-song").textContent = n.state === "off" ? "The player isn't answering" : "Nothing is playing";
      $("now-album").textContent = "";
    }
  } catch (e) { /* keep the last one shown */ }
  if (!document.hidden) nowTimer = setTimeout(refreshNow, 5000);
}

document.addEventListener("visibilitychange", () => { if (!document.hidden) { refresh(); refreshNow(); } });

takeShared();
loadAlbums().catch((e) => showMsg(e.message));
refresh();
refreshNow();
