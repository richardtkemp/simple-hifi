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

// ---- album picker

async function loadAlbums(select) {
  const { albums } = await api("/api/albums");
  const sel = $("album");
  sel.textContent = "";
  for (const a of albums) sel.append(new Option(a, a));
  sel.append(new Option("➕ New album…", NEW));
  const want = select || localStorage.getItem("album");
  sel.value = albums.includes(want) ? want : (albums.length ? albums[albums.length - 1] : NEW);
  albumChanged();
}

function albumChanged() {
  const isNew = $("album").value === NEW;
  $("newname").hidden = !isNew;
  if (isNew) $("newname").focus();
}
$("album").addEventListener("change", albumChanged);

// ---- shared from another app (Android share sheet opens /share?text=...)

function takeShared() {
  const q = new URLSearchParams(location.search);
  const parts = ["url", "text", "title"].map((k) => q.get(k)).filter(Boolean);
  if (!parts.length) return;
  $("links").value = parts.join("\n");
  $("shared").hidden = false;
  history.replaceState(null, "", "/");   // a reload must not share it again
}

// ---- add

$("add").addEventListener("click", async () => {
  const isNew = $("album").value === NEW;
  const album = isNew ? $("newname").value.trim() : $("album").value;
  if (!$("links").value.trim()) return showMsg("Paste a YouTube link first.");
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
    showMsg("Added " + n + " to “" + r.album + "”. You can close this page; the player keeps going."
      + (r.note ? "\n" + r.note : ""), true);
    await loadAlbums(r.album);
    refresh();
  } catch (e) {
    showMsg(e.message);
  } finally {
    $("add").disabled = false;
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
    case "done": return "✓ Added " + (done === 1 ? "1 song" : done + " songs") + " to “" + job.album + "”";
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

function renderJob(job) {
  const box = el("div", "job " + job.status);
  box.append(el("div", "t", job.title || job.url));
  box.append(el("div", "s", statusText(job)));
  if (job.status === "downloading" || job.status === "listing") {
    const bar = el("div", "bar");
    const fill = el("div");
    fill.style.width = percent(job) + "%";
    bar.append(fill);
    box.append(bar);
  }
  if (job.status === "failed") {
    const b = el("button", "small", "Try again");
    b.addEventListener("click", async () => {
      b.disabled = true;
      try { await api("/api/jobs/" + job.id + "/retry", {}); } catch (e) { showMsg(e.message); }
      refresh();
    });
    box.append(b);
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
    box.append(d);
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
    list.textContent = "";
    if (!jobs.length) list.append(el("div", "hint", "Nothing yet."));
    for (const j of jobs) {
      const node = renderJob(j);
      const d = node.querySelector("details");
      if (d) { d.dataset.id = j.id; d.open = open.has(j.id); }
      list.append(node);
      if (j.status === "queued" || j.status === "listing" || j.status === "downloading") busy = true;
    }
  } catch (e) {
    busy = true;   // keep trying
  }
  pollTimer = setTimeout(refresh, busy ? 3000 : 20000);
}

document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

takeShared();
loadAlbums().catch((e) => showMsg(e.message));
refresh();
