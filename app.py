#!/usr/bin/env python3
"""
MAL Catalog Picker - portable native app.
Runs a small local server, serves a UI in your default browser, and stores
your progress in a plain JSON file next to this program. Nothing is sent
anywhere except to AniList (to fetch anime info) and, only if you choose to
export, to MyAnimeList when you upload the resulting XML yourself.

No third-party dependencies: standard library only.
"""

import json
import os
import sys
import threading
import time
import webbrowser
import socket
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Where progress lives: right next to this program (or the .exe, when frozen)
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PROGRESS_PATH = os.path.join(BASE_DIR, "mal_picker_progress.json")

ANILIST_URL = "https://graphql.anilist.co"

SEASON_QUERY = """
query ($season: MediaSeason, $seasonYear: Int, $page: Int) {
  Page(page: $page, perPage: 24) {
    pageInfo { hasNextPage currentPage }
    media(season: $season, seasonYear: $seasonYear, type: ANIME, sort: POPULARITY_DESC) {
      id
      idMal
      title { romaji english }
      coverImage { medium large }
      startDate { year month day }
      format
      episodes
    }
  }
}
"""

SEARCH_QUERY = """
query ($search: String, $page: Int) {
  Page(page: $page, perPage: 24) {
    pageInfo { hasNextPage currentPage }
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id
      idMal
      title { romaji english }
      coverImage { medium large }
      startDate { year month day }
      format
      episodes
    }
  }
}
"""


def anilist_query(query, variables):
    """Runs a GraphQL query against AniList. Raises RuntimeError with a
    human-readable message on failure so the caller can surface it cleanly."""
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        ANILIST_URL,
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            body = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            time.sleep(1.5)
            return anilist_query(query, variables)
        raise RuntimeError(f"AniList returned an error (HTTP {e.code}). Try again shortly.")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach AniList — check your internet connection. ({e.reason})")
    except Exception as e:
        raise RuntimeError(f"Unexpected error talking to AniList: {e}")

    if "errors" in body and body["errors"]:
        msg = body["errors"][0].get("message", "unknown error")
        raise RuntimeError(f"AniList rejected the request: {msg}")
    return body["data"]["Page"]


def simplify_media_list(page_data):
    items = []
    for m in page_data.get("media", []):
        title = (m.get("title") or {}).get("english") or (m.get("title") or {}).get("romaji") or "Untitled"
        cover = (m.get("coverImage") or {}).get("large") or (m.get("coverImage") or {}).get("medium") or ""
        sd = m.get("startDate") or {}
        if sd.get("year"):
            date_str = "-".join(str(x) for x in [sd.get("year"), sd.get("month"), sd.get("day")] if x)
        else:
            date_str = "unknown date"
        items.append({
            "id": m.get("id"),
            "idMal": m.get("idMal"),
            "title": title,
            "cover": cover,
            "date": date_str,
            "format": m.get("format") or "Unknown",
            "episodes": m.get("episodes") or 0,
        })
    return {
        "items": items,
        "hasNextPage": (page_data.get("pageInfo") or {}).get("hasNextPage", False),
    }


# ---------------------------------------------------------------------------
# Progress file: read/write with clear error messages
# ---------------------------------------------------------------------------
_progress_lock = threading.Lock()


def read_progress():
    with _progress_lock:
        if not os.path.exists(PROGRESS_PATH):
            return {"selections": {}, "theme": "dark", "banner": "", "lastView": None}
        try:
            with open(PROGRESS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("selections", {})
            data.setdefault("theme", "dark")
            data.setdefault("banner", "")
            data.setdefault("lastView", None)
            return data
        except (json.JSONDecodeError, OSError) as e:
            raise RuntimeError(
                f"Couldn't read the progress file at {PROGRESS_PATH}. "
                f"Please make sure that file exists, isn't open in another program, "
                f"and isn't corrupted. ({e})"
            )


def write_progress(data):
    with _progress_lock:
        try:
            tmp_path = PROGRESS_PATH + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, PROGRESS_PATH)
        except OSError as e:
            raise RuntimeError(
                f"Couldn't write to the progress file at {PROGRESS_PATH}. "
                f"Please make sure this program has permission to write next to itself, "
                f"and that the folder isn't read-only. ({e})"
            )


def find_duplicate_titles(selections):
    """Flags anime that share the same (lowercased) title but different
    idMal keys — usually harmless (sequels/re-releases) but worth a heads up
    before export."""
    seen = {}
    dupes = []
    for mal_id, entry in selections.items():
        key = (entry.get("title") or "").strip().lower()
        if not key:
            continue
        if key in seen and seen[key] != mal_id:
            dupes.append(entry.get("title"))
        else:
            seen[key] = mal_id
    return sorted(set(dupes))


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep the console quiet

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query_params(self):
        from urllib.parse import urlparse, parse_qs
        return parse_qs(urlparse(self.path).query)

    def do_GET(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path

        if path == "/":
            self._send_html(FRONTEND_HTML)
            return

        if path == "/api/season":
            q = self._query_params()
            year = int(q.get("year", [0])[0])
            season = q.get("season", [""])[0].upper()
            page = int(q.get("page", [1])[0])
            try:
                data = anilist_query(SEASON_QUERY, {"season": season, "seasonYear": year, "page": page})
                self._send_json({"ok": True, **simplify_media_list(data)})
            except RuntimeError as e:
                self._send_json({"ok": False, "error": str(e)}, status=502)
            return

        if path == "/api/search":
            q = self._query_params()
            query_str = q.get("q", [""])[0]
            page = int(q.get("page", [1])[0])
            try:
                data = anilist_query(SEARCH_QUERY, {"search": query_str, "page": page})
                self._send_json({"ok": True, **simplify_media_list(data)})
            except RuntimeError as e:
                self._send_json({"ok": False, "error": str(e)}, status=502)
            return

        if path == "/api/progress":
            try:
                data = read_progress()
                self._send_json({"ok": True, **data})
            except RuntimeError as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)
            return

        self._send_json({"ok": False, "error": "not found"}, status=404)

    def do_POST(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path

        if path == "/api/progress":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            try:
                incoming = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                self._send_json({"ok": False, "error": "Malformed data sent to the server."}, status=400)
                return
            try:
                write_progress(incoming)
                dupes = find_duplicate_titles(incoming.get("selections", {}))
                self._send_json({"ok": True, "duplicateTitles": dupes})
            except RuntimeError as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)
            return

        self._send_json({"ok": False, "error": "not found"}, status=404)


# ---------------------------------------------------------------------------
# Frontend (served as a single embedded page)
# ---------------------------------------------------------------------------
FRONTEND_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Catalog / MAL Import Builder</title>
<style>
  :root{
    --bg:#12151a; --panel:#1a1e25; --card:#1e232b; --line:#2b323d; --text:#e7e4dc;
    --muted:#8b93a1; --amber:#ffb454; --amber-dim:#7a5a2e; --teal:#4fd1c5; --red:#e0645a;
    --mono:'JetBrains Mono','SFMono-Regular',Consolas,monospace;
    --sans:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  }
  body.light{
    --bg:#f2efe8; --panel:#ffffff; --card:#fbfaf6; --line:#ddd7c8; --text:#24211c;
    --muted:#726c5e; --amber:#b3690f; --amber-dim:#f1dcb8; --teal:#0f7a70; --red:#b53a30;
  }
  *{box-sizing:border-box;}
  body{ margin:0; background:var(--bg); color:var(--text); font-family:var(--sans); min-height:100vh; }
  #errBanner{ display:none; background:#7a2e2e; color:#ffdcdc; font-family:var(--mono); font-size:12px; padding:8px 16px; text-align:center; }
  header{ position:sticky; top:0; z-index:20; background:var(--panel) center/cover no-repeat; border-bottom:1px solid var(--line); padding:14px 20px; }
  header.has-banner .brand, header.has-banner .controls, header.has-banner .status-bar{ background:rgba(0,0,0,0.55); border-radius:8px; padding:8px 12px; }
  body.light header.has-banner .brand, body.light header.has-banner .controls, body.light header.has-banner .status-bar{ background:rgba(255,255,255,0.75); }
  header.has-banner .brand{ margin-bottom:8px; }
  .brand{ display:flex; align-items:baseline; gap:10px; margin-bottom:12px; flex-wrap:wrap; }
  .brand .dot{ width:10px; height:10px; border-radius:50%; background:var(--amber); box-shadow:0 0 8px var(--amber);}
  .brand h1{ font-family:var(--mono); font-size:15px; letter-spacing:1px; margin:0; color:var(--amber); text-transform:uppercase; }
  .brand span{ font-size:11px; color:var(--muted); font-family:var(--mono);}
  .brand .rightlinks{ margin-left:auto; display:flex; gap:8px; }
  .controls{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
  .tabs{ display:flex; border:1px solid var(--line); border-radius:6px; overflow:hidden; }
  .tabs button{ background:var(--card); color:var(--muted); border:none; padding:8px 14px; font-family:var(--mono); font-size:12px; cursor:pointer; letter-spacing:0.5px; }
  .tabs button.active{ background:var(--amber-dim); color:var(--amber); }
  select, input[type=text], input[type=number]{ background:var(--card); border:1px solid var(--line); color:var(--text); padding:8px 10px; border-radius:6px; font-family:var(--mono); font-size:13px; }
  select:focus, input:focus{ outline:1px solid var(--amber); }
  button.action{ background:var(--card); border:1px solid var(--line); color:var(--text); padding:8px 14px; border-radius:6px; font-family:var(--mono); font-size:12px; cursor:pointer; letter-spacing:0.5px; }
  button.action:hover{ border-color:var(--amber); color:var(--amber); }
  button.action:disabled{ opacity:0.4; cursor:default; }
  button.primary{ background:var(--amber-dim); border-color:var(--amber); color:var(--amber); }
  button.iconbtn{ padding:8px 10px; }
  label.chk{ font-family:var(--mono); font-size:11px; color:var(--muted); display:flex; align-items:center; gap:6px; cursor:pointer; }
  .status-bar{ margin-top:10px; display:flex; gap:16px; flex-wrap:wrap; font-family:var(--mono); font-size:11px; color:var(--muted); }
  .status-bar b{ color:var(--text); }
  .status-bar .seg{ padding:2px 8px; border-radius:4px; border:1px solid var(--line); }
  details#instructions{ margin:16px 20px 0 20px; background:var(--panel); border:1px solid var(--line); border-radius:8px; font-size:13px; line-height:1.5; }
  details#instructions summary{ cursor:pointer; padding:12px 16px; font-family:var(--mono); font-size:12px; color:var(--amber); letter-spacing:0.5px; list-style:none; }
  details#instructions summary::-webkit-details-marker{ display:none; }
  details#instructions .body{ padding:0 16px 16px 16px; color:var(--muted); }
  details#instructions .body h4{ color:var(--text); font-size:12px; margin:14px 0 6px 0; font-family:var(--mono); }
  details#instructions .body p{ margin:4px 0; }
  details#instructions .body .privacy{ border-left:3px solid var(--teal); padding-left:10px; color:var(--text); }
  details#instructions .body .bannerform{ display:flex; gap:8px; align-items:center; margin-top:8px; flex-wrap:wrap; }
  main{ padding:20px; max-width:1400px; margin:0 auto; }
  .loading{ font-family:var(--mono); color:var(--amber); font-size:13px; padding:20px; text-align:center; }
  .empty{ font-family:var(--mono); color:var(--muted); font-size:13px; padding:40px; text-align:center; }
  .posterProgress{ font-family:var(--mono); font-size:11px; color:var(--teal); text-align:center; padding:6px 0 14px 0; }
  .grid{ display:grid; grid-template-columns:repeat(auto-fill, minmax(220px, 1fr)); gap:16px; }
  .card{ background:var(--card); border:1px solid var(--line); border-radius:8px; overflow:hidden; display:flex; flex-direction:column; transition:border-color 0.15s, box-shadow 0.15s; }
  .card.tagged{ border-color:var(--amber); }
  .card.hovered{ box-shadow:0 0 0 2px var(--amber) inset; }
  .card.hidden-tagged{ display:none; }
  .card.nomal{ opacity:0.55; }
  .card-top{ display:flex; gap:10px; padding:10px; }
  .card-top .thumbwrap{ width:64px; height:90px; flex-shrink:0; border-radius:4px; overflow:hidden; background:linear-gradient(100deg, var(--line) 30%, var(--card) 50%, var(--line) 70%); background-size:200% 100%; animation:shimmer 1.2s infinite; }
  .card-top .thumbwrap.loaded{ animation:none; background:none; }
  .card-top img{ width:64px; height:90px; object-fit:cover; display:block; opacity:0; transition:opacity 0.25s; }
  .card-top img.loaded{ opacity:1; }
  @keyframes shimmer{ 0%{background-position:200% 0;} 100%{background-position:-200% 0;} }
  .card-info{ min-width:0; }
  .card-info .title{ font-size:13px; line-height:1.3; margin:0 0 6px 0; font-weight:600; }
  .card-info .meta{ font-family:var(--mono); font-size:10px; color:var(--teal); }
  .card-info .type{ font-family:var(--mono); font-size:10px; color:var(--muted); margin-top:2px; }
  .card-info .kbd{ font-family:var(--mono); font-size:9px; color:var(--muted); margin-top:4px; }
  .card-info .nomaltag{ font-family:var(--mono); font-size:9px; color:var(--red); margin-top:4px; }
  .seg-row{ display:grid; grid-template-columns:repeat(3, 1fr); border-top:1px solid var(--line); }
  .seg-row button{ background:none; border:none; border-right:1px solid var(--line); border-top:1px solid var(--line); color:var(--muted); font-family:var(--mono); font-size:9.5px; padding:6px 2px; cursor:pointer; letter-spacing:0.3px; }
  .seg-row button:nth-child(3n){ border-right:none; }
  .seg-row button:nth-child(-n+3){ border-top:none; }
  .seg-row button.sel-none.active{ background:#2b323d; color:var(--text); }
  .seg-row button.sel-watching.active{ background:#2e4a7a; color:#8fc1ff; }
  .seg-row button.sel-completed.active{ background:#2e6b4f; color:#7de3ab; }
  .seg-row button.sel-onhold.active{ background:#7a662e; color:var(--amber); }
  .seg-row button.sel-dropped.active{ background:#7a2e2e; color:var(--red); }
  .seg-row button.sel-ptw.active{ background:#4a2e7a; color:#c39bff; }
  .seg-row button:hover:not(.active){ color:var(--text); }
  .seg-row button:disabled{ opacity:0.3; cursor:default; }
  .score-row{ display:flex; align-items:center; gap:6px; padding:6px 10px; border-top:1px solid var(--line); font-family:var(--mono); font-size:10px; color:var(--muted); }
  .score-row input{ width:48px; padding:4px 6px; font-size:11px; }
  .pager{ display:flex; justify-content:center; gap:10px; margin:20px 0; }
  footer{ text-align:center; padding:20px; font-family:var(--mono); font-size:11px; color:var(--muted); }
  #toast{ position:fixed; bottom:20px; left:50%; transform:translateX(-50%) translateY(20px); background:var(--panel); border:1px solid var(--amber); color:var(--text); padding:10px 16px; border-radius:8px; font-family:var(--mono); font-size:12px; display:flex; gap:12px; align-items:center; opacity:0; pointer-events:none; transition:opacity 0.2s, transform 0.2s; z-index:100; }
  #toast.show{ opacity:1; transform:translateX(-50%) translateY(0); pointer-events:auto; }
  #toast button{ background:none; border:none; color:var(--amber); font-family:var(--mono); cursor:pointer; font-size:12px; text-decoration:underline; }
</style>
</head>
<body>
<div id="errBanner"></div>
<header id="mainHeader">
  <div class="brand">
    <div class="dot"></div><h1>Catalog // Import Builder</h1><span>tag → export → upload to myanimelist.net/import.php</span>
    <div class="rightlinks"><button class="action iconbtn" id="themeBtn" title="Toggle theme">☾/☀</button></div>
  </div>
  <div class="controls">
    <div class="tabs">
      <button id="tabSeason" class="active">Browse by season</button>
      <button id="tabSearch">Search by title</button>
    </div>
    <div id="seasonControls">
      <button class="action iconbtn" id="prevSeasonBtn" title="Previous season">◀</button>
      <select id="yearSel"></select>
      <select id="seasonSel">
        <option value="WINTER">Winter</option>
        <option value="SPRING">Spring</option>
        <option value="SUMMER">Summer</option>
        <option value="FALL">Fall</option>
      </select>
      <button class="action iconbtn" id="nextSeasonBtn" title="Next season">▶</button>
      <button class="action primary" id="loadSeasonBtn">Load</button>
    </div>
    <div id="searchControls" style="display:none;">
      <input type="text" id="searchInput" placeholder="anime title..." style="width:240px;">
      <button class="action primary" id="searchBtn">Search</button>
    </div>
    <label class="chk"><input type="checkbox" id="hideTaggedChk"> hide tagged</label>
    <button class="action" id="exportBackupBtn" style="margin-left:auto;">💾 Backup file</button>
    <button class="action" id="importBackupBtn">📂 Import backup</button>
    <input type="file" id="importBackupInput" accept="application/json" style="display:none;">
    <button class="action" id="exportBtn">⤓ Export MAL XML</button>
    <button class="action" id="clearBtn">Clear all tags</button>
  </div>
  <div class="status-bar" id="statusBar"></div>
</header>

<details id="instructions">
  <summary>▸ How this works / read before sharing with friends</summary>
  <div class="body">
    <h4>Basics</h4>
    <p>Browse by season/year, or search a title. Click a status button on a card to tag it (or hover a card and press <b>1–6</b>). Hit <b>Export MAL XML</b> — this downloads a file. Go to <b>myanimelist.net/import.php</b>, choose import type "MyAnimeList Import", and upload it.</p>
    <p>Scoring is optional — leave it at 0 if you don't care, or set 1–10 and it carries into the import.</p>
    <p>Entries with no MAL match (rare — mostly obscure/regional titles AniList knows but MAL doesn't) are shown dimmed and can't be tagged, since there'd be nothing to export them as.</p>

    <h4 class="privacy">Privacy — read this</h4>
    <p class="privacy">This is a program that runs entirely on your own computer. Your tagged statuses are saved to a plain file (<code>mal_picker_progress.json</code>) sitting right next to this program — nothing is sent to us, to each other, or anywhere else. The only network calls this makes are to AniList (to fetch anime info to show you) and, only when <i>you</i> choose to export and upload it, to MyAnimeList. You can open that JSON file in a text editor any time to see exactly what's stored — there's nothing hidden in it.</p>

    <h4>Customize the top banner</h4>
    <p>Yes, you can put your waifu up there. Upload an image below. Recommended resolution: roughly <b>1600×220px</b> (wide, short) since it stretches across the header.</p>
    <div class="bannerform">
      <input type="file" id="bannerInput" accept="image/*">
      <button class="action" id="removeBannerBtn">Remove banner</button>
    </div>
  </div>
</details>

<main>
  <div id="content"><div class="empty">Pick a year + season, or search a title, to start tagging.</div></div>
  <div class="pager" id="pager" style="display:none;">
    <button class="action" id="prevBtn">← Prev page</button>
    <span id="pageLabel" style="font-family:var(--mono); font-size:12px; color:var(--muted); align-self:center;"></span>
    <button class="action" id="nextBtn">Next page →</button>
  </div>
</main>
<footer>Data via AniList. Progress saved locally next to this program.</footer>
<div id="toast"><span id="toastMsg"></span><button id="toastUndo">Undo</button></div>

<script>
const STATUSES = [
  {key:'none', label:'None', cls:'sel-none'},
  {key:'watching', label:'Watch', cls:'sel-watching'},
  {key:'completed', label:'Done', cls:'sel-completed'},
  {key:'onhold', label:'Hold', cls:'sel-onhold'},
  {key:'dropped', label:'Drop', cls:'sel-dropped'},
  {key:'ptw', label:'Plan', cls:'sel-ptw'},
];
const MAL_STATUS_TEXT = { watching:'Watching', completed:'Completed', onhold:'On-Hold', dropped:'Dropped', ptw:'Plan to Watch' };
const SEASON_ORDER = ['WINTER','SPRING','SUMMER','FALL'];

let selections = {};
let currentMode = 'season';
let currentPage = 1;
let hasNextPage = false;
let lastResults = [];
let hoveredCardId = null;
let lastToastUndo = null;
let hideTagged = false;

const contentEl = document.getElementById('content');
const statusBarEl = document.getElementById('statusBar');
const pagerEl = document.getElementById('pager');
const pageLabelEl = document.getElementById('pageLabel');
const errBanner = document.getElementById('errBanner');
const headerEl = document.getElementById('mainHeader');
const yearSel = document.getElementById('yearSel');

function showErr(msg){
  errBanner.textContent = '⚠ ' + msg;
  errBanner.style.display = 'block';
}
function clearErr(){ errBanner.style.display = 'none'; }

// ---- backend progress persistence ----
let saveTimer = null;
function saveProgress(){
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async ()=>{
    try{
      const res = await fetch('/api/progress', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({selections, theme: document.body.classList.contains('light')?'light':'dark', banner: currentBanner, lastView: currentLastView()})
      });
      const data = await res.json();
      if(!data.ok){ showErr(data.error); }
      else{
        clearErr();
        if(data.duplicateTitles && data.duplicateTitles.length){
          showErr('Possible duplicate titles tagged under different entries: ' + data.duplicateTitles.join(', '));
        }
      }
    }catch(e){ showErr('Could not reach the local server to save progress. Is the app still running?'); }
  }, 300);
}
function currentLastView(){
  return { mode: currentMode, year: yearSel.value, season: document.getElementById('seasonSel').value, query: document.getElementById('searchInput').value };
}
let currentBanner = '';

async function loadAllProgress(){
  try{
    const res = await fetch('/api/progress');
    const data = await res.json();
    if(!data.ok){ showErr(data.error); return null; }
    selections = data.selections || {};
    if(data.theme === 'light') document.body.classList.add('light');
    if(data.banner){ currentBanner = data.banner; applyBanner(data.banner); }
    renderStatusBar();
    return data.lastView;
  }catch(e){
    showErr('Could not reach the local server. Make sure the app is still running, then reload this page.');
    return null;
  }
}

// ---- theme ----
document.getElementById('themeBtn').onclick = ()=>{
  document.body.classList.toggle('light');
  saveProgress();
};

// ---- banner ----
function applyBanner(dataUrl){
  headerEl.style.backgroundImage = `url(${dataUrl})`;
  headerEl.classList.add('has-banner');
}
document.getElementById('bannerInput').addEventListener('change', (e)=>{
  const file = e.target.files[0];
  if(!file) return;
  const reader = new FileReader();
  reader.onload = ()=>{
    currentBanner = reader.result;
    applyBanner(currentBanner);
    saveProgress();
  };
  reader.readAsDataURL(file);
});
document.getElementById('removeBannerBtn').onclick = ()=>{
  currentBanner = '';
  headerEl.style.backgroundImage = '';
  headerEl.classList.remove('has-banner');
  saveProgress();
};

// ---- year select ----
const nowYear = new Date().getFullYear();
for(let y = nowYear; y >= 1960; y--){
  const opt = document.createElement('option');
  opt.value = y; opt.textContent = y;
  if(y === nowYear) opt.selected = true;
  yearSel.appendChild(opt);
}

// ---- tabs ----
const tabSeason = document.getElementById('tabSeason');
const tabSearch = document.getElementById('tabSearch');
const seasonControls = document.getElementById('seasonControls');
const searchControls = document.getElementById('searchControls');
tabSeason.onclick = ()=>{ currentMode='season'; tabSeason.classList.add('active'); tabSearch.classList.remove('active'); seasonControls.style.display=''; searchControls.style.display='none'; };
tabSearch.onclick = ()=>{ currentMode='search'; tabSearch.classList.add('active'); tabSeason.classList.remove('active'); seasonControls.style.display='none'; searchControls.style.display=''; };

document.getElementById('prevSeasonBtn').onclick = ()=> shiftSeason(-1);
document.getElementById('nextSeasonBtn').onclick = ()=> shiftSeason(1);
function shiftSeason(dir){
  const seasonSelEl = document.getElementById('seasonSel');
  let idx = SEASON_ORDER.indexOf(seasonSelEl.value);
  let year = parseInt(yearSel.value, 10);
  idx += dir;
  if(idx < 0){ idx = 3; year -= 1; }
  if(idx > 3){ idx = 0; year += 1; }
  seasonSelEl.value = SEASON_ORDER[idx];
  if([...yearSel.options].some(o => parseInt(o.value,10) === year)) yearSel.value = year;
  loadSeason(1);
}

function escapeHtml(str){
  return String(str).replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
}

function cardHTML(anime){
  const id = anime.idMal;
  const hasMal = !!id;
  const key = hasMal ? id : ('nomal-' + anime.id);
  const sel = hasMal ? selections[id] : null;
  const curStatus = sel ? sel.status : 'none';
  const curScore = sel && sel.score ? sel.score : 0;
  const tagged = curStatus !== 'none';

  let segButtons = STATUSES.map(s => {
    const active = s.key === curStatus ? 'active' : '';
    const disabled = hasMal ? '' : 'disabled';
    return `<button class="${s.cls} ${active}" data-id="${key}" data-status="${s.key}" ${disabled}>${s.label}</button>`;
  }).join('');

  return `<div class="card ${tagged?'tagged':''} ${hideTagged && tagged ? 'hidden-tagged':''} ${hasMal?'':'nomal'}" id="card-${key}" data-id="${key}" data-malid="${hasMal?id:''}">
    <div class="card-top">
      <div class="thumbwrap"><img src="${anime.cover}" alt="" loading="lazy"></div>
      <div class="card-info">
        <p class="title">${escapeHtml(anime.title)}</p>
        <div class="meta">${escapeHtml(anime.date)}</div>
        <div class="type">${escapeHtml(anime.format)}${anime.episodes ? ' · ' + anime.episodes + ' ep' : ''}</div>
        ${hasMal ? '<div class="kbd">hover + press 1-6 to tag</div>' : '<div class="nomaltag">not linked to MAL — can\'t export</div>'}
      </div>
    </div>
    <div class="seg-row">${segButtons}</div>
    <div class="score-row">score: <input type="number" min="0" max="10" step="1" value="${curScore}" data-id="${key}" class="scoreInput" ${hasMal?'':'disabled'}></div>
  </div>`;
}

function renderResults(results){
  lastResults = results;
  if(!results.length){ contentEl.innerHTML = '<div class="empty">No results.</div>'; return; }
  contentEl.innerHTML = `<div class="posterProgress" id="posterProgress">Loading posters: 0 / ${results.length}</div><div class="grid">${results.map(cardHTML).join('')}</div>`;

  let loadedCount = 0;
  const progressEl = document.getElementById('posterProgress');
  contentEl.querySelectorAll('.card-top img').forEach(img=>{
    const finish = ()=>{
      loadedCount++;
      img.classList.add('loaded');
      img.closest('.thumbwrap').classList.add('loaded');
      if(progressEl){
        progressEl.textContent = `Loading posters: ${loadedCount} / ${results.length}`;
        if(loadedCount >= results.length) setTimeout(()=>{ if(progressEl) progressEl.style.display='none'; }, 400);
      }
    };
    if(img.complete) finish();
    else{ img.addEventListener('load', finish); img.addEventListener('error', finish); }
  });

  contentEl.querySelectorAll('.seg-row button:not(:disabled)').forEach(btn=> btn.addEventListener('click', onStatusClick));
  contentEl.querySelectorAll('.scoreInput:not(:disabled)').forEach(inp=> inp.addEventListener('change', onScoreChange));
  contentEl.querySelectorAll('.card').forEach(card=>{
    card.addEventListener('mouseenter', ()=>{ hoveredCardId = card.dataset.malid || null; card.classList.add('hovered'); });
    card.addEventListener('mouseleave', ()=>{ if(hoveredCardId === card.dataset.malid) hoveredCardId = null; card.classList.remove('hovered'); });
  });
}

function findAnimeByKey(key){
  return lastResults.find(a => a.idMal && String(a.idMal) === String(key));
}

function applyStatus(id, status, showToastMsg){
  const anime = findAnimeByKey(id);
  if(!anime) return;
  const prevSel = selections[id] ? {...selections[id]} : null;

  if(status === 'none') delete selections[id];
  else selections[id] = { status, title: anime.title, type: anime.format, episodes: anime.episodes || 0, score: prevSel ? (prevSel.score||0) : 0 };

  saveProgress();

  const card = document.getElementById('card-' + id);
  if(card){
    const tagged = status !== 'none';
    card.classList.toggle('tagged', tagged);
    card.classList.toggle('hidden-tagged', hideTagged && tagged);
    card.querySelectorAll('.seg-row button').forEach(b=> b.classList.toggle('active', b.dataset.status === status));
  }
  renderStatusBar();

  if(showToastMsg){
    const label = status === 'none' ? 'cleared' : ('tagged as ' + (MAL_STATUS_TEXT[status] || status));
    showToast(`"${anime.title}" ${label}`, ()=> applyStatus(id, prevSel ? prevSel.status : 'none', false));
  }
}

function onStatusClick(e){ applyStatus(e.currentTarget.dataset.id, e.currentTarget.dataset.status, true); }

function onScoreChange(e){
  const id = e.currentTarget.dataset.id;
  let val = parseInt(e.currentTarget.value, 10);
  if(isNaN(val) || val < 0) val = 0;
  if(val > 10) val = 10;
  e.currentTarget.value = val;
  if(selections[id]){ selections[id].score = val; saveProgress(); }
  else if(val > 0){ applyStatus(id, 'ptw', false); selections[id].score = val; saveProgress(); }
}

document.addEventListener('keydown', (e)=>{
  if(!hoveredCardId) return;
  const num = parseInt(e.key, 10);
  if(num >= 1 && num <= 6) applyStatus(hoveredCardId, STATUSES[num-1].key, true);
});

let toastTimer = null;
function showToast(msg, undoFn){
  const toast = document.getElementById('toast');
  document.getElementById('toastMsg').textContent = msg;
  lastToastUndo = undoFn;
  toast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(()=> toast.classList.remove('show'), 4000);
}
document.getElementById('toastUndo').onclick = ()=>{
  if(lastToastUndo) lastToastUndo();
  document.getElementById('toast').classList.remove('show');
};

document.getElementById('hideTaggedChk').addEventListener('change', (e)=>{
  hideTagged = e.target.checked;
  document.querySelectorAll('.card.tagged').forEach(c=> c.classList.toggle('hidden-tagged', hideTagged));
});

function renderStatusBar(){
  const counts = {none:0, watching:0, completed:0, onhold:0, dropped:0, ptw:0};
  Object.values(selections).forEach(s => { counts[s.status] = (counts[s.status]||0) + 1; });
  const total = Object.keys(selections).length;
  statusBarEl.innerHTML = `<span class="seg"><b>${total}</b> tagged total</span>` +
    STATUSES.filter(s=>s.key!=='none').map(s=>`<span class="seg">${s.label}: <b>${counts[s.key]}</b></span>`).join('');
}

async function loadSeason(page){
  const year = yearSel.value;
  const season = document.getElementById('seasonSel').value;
  contentEl.innerHTML = '<div class="loading">Loading ' + season + ' ' + year + ' — page ' + page + '...</div>';
  pagerEl.style.display = 'none';
  try{
    const res = await fetch(`/api/season?year=${year}&season=${season}&page=${page}`);
    const data = await res.json();
    if(!data.ok){ contentEl.innerHTML = '<div class="empty">' + escapeHtml(data.error) + '</div>'; return; }
    hasNextPage = data.hasNextPage; currentPage = page;
    renderResults(data.items || []);
    updatePager();
    saveProgress();
  }catch(e){ contentEl.innerHTML = '<div class="empty">Could not reach the local server. Is the app still running?</div>'; }
}

async function doSearch(page){
  const q = document.getElementById('searchInput').value.trim();
  if(!q) return;
  contentEl.innerHTML = '<div class="loading">Searching "' + escapeHtml(q) + '" — page ' + page + '...</div>';
  pagerEl.style.display = 'none';
  try{
    const res = await fetch(`/api/search?q=${encodeURIComponent(q)}&page=${page}`);
    const data = await res.json();
    if(!data.ok){ contentEl.innerHTML = '<div class="empty">' + escapeHtml(data.error) + '</div>'; return; }
    hasNextPage = data.hasNextPage; currentPage = page;
    renderResults(data.items || []);
    updatePager();
    saveProgress();
  }catch(e){ contentEl.innerHTML = '<div class="empty">Could not reach the local server. Is the app still running?</div>'; }
}

function updatePager(){
  pagerEl.style.display = 'flex';
  pageLabelEl.textContent = 'page ' + currentPage;
  document.getElementById('prevBtn').disabled = currentPage <= 1;
  document.getElementById('nextBtn').disabled = !hasNextPage;
}

document.getElementById('loadSeasonBtn').onclick = ()=> loadSeason(1);
document.getElementById('searchBtn').onclick = ()=> doSearch(1);
document.getElementById('searchInput').addEventListener('keydown', e=>{ if(e.key==='Enter') doSearch(1); });
document.getElementById('prevBtn').onclick = ()=>{ if(currentPage<=1) return; currentMode==='season' ? loadSeason(currentPage-1) : doSearch(currentPage-1); };
document.getElementById('nextBtn').onclick = ()=>{ if(!hasNextPage) return; currentMode==='season' ? loadSeason(currentPage+1) : doSearch(currentPage+1); };

document.getElementById('clearBtn').onclick = ()=>{
  if(!confirm('Clear all tagged statuses? This cannot be undone.')) return;
  selections = {};
  saveProgress();
  renderStatusBar();
  if(lastResults.length) renderResults(lastResults);
};

// backup export/import (on top of the automatic local file)
document.getElementById('exportBackupBtn').onclick = ()=>{
  const total = Object.keys(selections).length;
  if(!total){ alert('Nothing tagged yet.'); return; }
  const payload = JSON.stringify({savedAt: new Date().toISOString(), selections}, null, 2);
  const blob = new Blob([payload], {type:'application/json'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href = url; a.download = 'mal_picker_backup.json';
  document.body.appendChild(a); a.click(); document.body.removeChild(a); URL.revokeObjectURL(url);
};
document.getElementById('importBackupBtn').onclick = ()=> document.getElementById('importBackupInput').click();
document.getElementById('importBackupInput').addEventListener('change', (e)=>{
  const file = e.target.files[0];
  if(!file) return;
  const reader = new FileReader();
  reader.onload = ()=>{
    try{
      const parsed = JSON.parse(reader.result);
      const incoming = parsed.selections || parsed;
      const incomingCount = Object.keys(incoming).length;
      if(!incomingCount){ alert('That file has no tagged anime in it.'); return; }
      const merge = confirm(`Loaded ${incomingCount} tagged anime.\n\nOK = merge into current progress\nCancel = replace current progress entirely`);
      selections = merge ? {...selections, ...incoming} : incoming;
      saveProgress();
      renderStatusBar();
      if(lastResults.length) renderResults(lastResults);
      alert('Progress loaded.');
    }catch(err){ alert('Could not read that file.'); }
  };
  reader.readAsText(file);
  e.target.value = '';
});

function buildXML(){
  const entries = Object.entries(selections);
  let body = entries.map(([id, s])=>{
    const statusText = MAL_STATUS_TEXT[s.status];
    const watchedEps = s.status === 'completed' ? (s.episodes || 0) : 0;
    const score = s.score || 0;
    return `<anime>
<series_animedb_id>${id}</series_animedb_id>
<series_title><![CDATA[${s.title}]]></series_title>
<series_type>${escapeHtml(s.type)}</series_type>
<series_episodes>${s.episodes || 0}</series_episodes>
<my_id>0</my_id>
<my_watched_episodes>${watchedEps}</my_watched_episodes>
<my_start_date>0000-00-00</my_start_date>
<my_finish_date>0000-00-00</my_finish_date>
<my_score>${score}</my_score>
<my_status>${statusText}</my_status>
<my_times_watched>0</my_times_watched>
<my_rewatching>0</my_rewatching>
<update_on_import>1</update_on_import>
</anime>`;
  }).join('\n');
  return `<?xml version="1.0" encoding="UTF-8" ?>\n<myanimelist>\n<myinfo>\n<user_export_type>1</user_export_type>\n</myinfo>\n${body}\n</myanimelist>`;
}

document.getElementById('exportBtn').onclick = ()=>{
  const total = Object.keys(selections).length;
  if(!total){ alert('No anime tagged yet — pick some statuses first.'); return; }
  const dupes = findDuplicatesClient();
  if(dupes.length && !confirm(`Heads up: these titles look tagged under more than one entry:\n${dupes.join(', ')}\n\nExport anyway?`)) return;
  const xml = buildXML();
  const blob = new Blob([xml], {type:'application/xml'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href = url; a.download = 'mal_import.xml';
  document.body.appendChild(a); a.click(); document.body.removeChild(a); URL.revokeObjectURL(url);
};
function findDuplicatesClient(){
  const seen = {}; const dupes = [];
  Object.entries(selections).forEach(([id, s])=>{
    const key = (s.title||'').trim().toLowerCase();
    if(!key) return;
    if(seen[key] && seen[key] !== id) dupes.push(s.title);
    else seen[key] = id;
  });
  return [...new Set(dupes)];
}

(async function init(){
  const lastView = await loadAllProgress();
  if(lastView){
    if(lastView.mode === 'search' && lastView.query){
      tabSearch.click();
      document.getElementById('searchInput').value = lastView.query;
      doSearch(1);
    } else if(lastView.year && lastView.season){
      if([...yearSel.options].some(o=>o.value === String(lastView.year))) yearSel.value = lastView.year;
      document.getElementById('seasonSel').value = lastView.season;
      loadSeason(1);
    }
  }
})();
</script>
</body>
</html>
"""


def find_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    port = find_free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"

    print("=" * 60, flush=True)
    print(" MAL Catalog Picker", flush=True)
    print(f" Progress file: {PROGRESS_PATH}", flush=True)
    print(f" Running at:    {url}", flush=True)
    print(" Opening your browser now. Close this window to stop the app.", flush=True)
    print("=" * 60, flush=True)

    threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
