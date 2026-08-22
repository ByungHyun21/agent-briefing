#!/usr/bin/env python3
"""agent-briefing: a submission board for AI agents.

- Agents POST/GET/PATCH/DELETE markdown reports at /submit...
- Markdown extras: attachment refs (att:...), !video(), YouTube/Vimeo
  auto-embed, ```chart fenced blocks (Chart.js), raw HTML passthrough.
- Humans browse at /.  Protocol for agents: AGENTS.md (also at /AGENTS.md).
"""
import base64
import html
import json
import os
import re
import secrets
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import markdown

DATA_DIR = os.environ.get("BRIEFING_DATA", "/app/data")
PORT = int(os.environ.get("BRIEFING_PORT", "49010"))
MAX_BODY = int(os.environ.get("BRIEFING_MAX_BODY", str(100 * 1024 * 1024)))  # 100 MB
VERSION = "1.1"

_ADMIN_KEY = None


def admin_key():
    """Operator key for the human web UI: BRIEFING_ADMIN_KEY if set, else a
    random key generated once and persisted as data/.admin_key."""
    global _ADMIN_KEY
    if _ADMIN_KEY is None:
        _ADMIN_KEY = (os.environ.get("BRIEFING_ADMIN_KEY") or "").strip()
        keyfile = os.path.join(DATA_DIR, ".admin_key")
        if not _ADMIN_KEY:
            try:
                with open(keyfile, encoding="utf-8") as f:
                    _ADMIN_KEY = f.read().strip()
            except FileNotFoundError:
                _ADMIN_KEY = ""
        if not _ADMIN_KEY:
            _ADMIN_KEY = secrets.token_urlsafe(12)
            with open(keyfile, "w", encoding="utf-8") as f:
                f.write(_ADMIN_KEY + "\n")
            print(f"admin UI key (auto-generated, data/.admin_key): {_ADMIN_KEY}",
                  flush=True)
    return _ADMIN_KEY

INLINE_TYPES = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml",
    "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
    "mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg",
    "pdf": "application/pdf", "txt": "text/plain", "csv": "text/csv",
    "json": "application/json", "md": "text/markdown",
}


# ---------------------------------------------------------------- storage
def submissions_path():
    return os.path.join(DATA_DIR, "submissions.json")


def load_submissions():
    try:
        with open(submissions_path(), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_submissions(items):
    tmp = submissions_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    os.replace(tmp, submissions_path())


def attachments_dir(sub_id):
    d = os.path.join(DATA_DIR, "attachments", sub_id)
    os.makedirs(d, exist_ok=True)
    return d


def valid_id(s):
    return bool(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", s or ""))


def safe_rel_path(s):
    if not s or s.startswith("/") or ".." in s.split("/") or "\\" in s:
        return None
    return s


# ---------------------------------------------------------------- markdown
MD_EXTENSIONS = ["tables", "fenced_code", "nl2br", "sane_lists"]

YT_RE = re.compile(
    r"^(?:<p>)?\s*(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|m\.youtube\.com/watch\?v=)([\w-]{6,})(?:[&<].*)?(?:</p>)?\s*$",
    re.M)
VIMEO_RE = re.compile(r"^(?:<p>)?\s*(?:https?://)?(?:www\.)?vimeo\.com/(\d+)\s*(?:</p>)?\s*$", re.M)


IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp")


def md_to_html(sub_id, text):
    # attachment refs before markdown: ](att:name) -> absolute URL
    text = re.sub(r"\]\(att:([^)\s]+)\)", f"](/attachments/{sub_id}/\\1)", text)
    # !video(att:name) -> raw <video> player
    def vid(m):
        src = m.group(1)
        if src.startswith("att:"):
            src = f"/attachments/{sub_id}/{src[4:]}"
        return (f'<video controls preload="metadata" style="max-width:100%;'
                f'border-radius:8px;" src="{html.escape(src)}"></video>')
    text = re.sub(r"!video\(([^)\s]+)\)", vid, text)

    out = markdown.markdown(text, extensions=MD_EXTENSIONS)

    # image attachments referenced as links: <a href="...x.png">…</a> → <img>
    def swap(m):
        url, label = m.group(1), m.group(2)
        if url.lower().rsplit("?", 1)[0].endswith(IMG_EXTS):
            return f'<img src="{url}" alt="{html.unescape(label)}" loading="lazy">'
        return m.group(0)
    out = re.sub(r'<a href="(/attachments/[^"]+)">([^<]*)</a>', swap, out)

    # YouTube / Vimeo auto-embed (paragraph = bare URL)
    out = YT_RE.sub(
        lambda m: (f'<iframe width="720" height="405" style="max-width:100%;'
                   f'aspect-ratio:16/9;border:0;border-radius:8px;" '
                   f'src="https://www.youtube.com/embed/{m.group(1)}" '
                   f'allowfullscreen></iframe>'), out)
    out = VIMEO_RE.sub(
        lambda m: (f'<iframe width="720" height="405" style="max-width:100%;'
                   f'aspect-ratio:16/9;border:0;border-radius:8px;" '
                   f'src="https://player.vimeo.com/video/{m.group(1)}" '
                   f'allowfullscreen></iframe>'), out)
    return out


def extract_charts(html_text):
    """Turn <pre><code class="language-chart">JSON</code></pre> into a canvas."""
    charts = []

    def repl(m):
        raw = m.group(1)
        try:
            cfg = json.loads(html.unescape(raw))
            json.dumps(cfg)  # sanity
        except ValueError:
            return m.group(0)
        idx = len(charts)
        charts.append(cfg)
        return (f'<div style="background:#0b0d11;border:1px solid #262b36;'
                f'border-radius:8px;padding:14px;margin:14px 0;">'
                f'<canvas id="chart-{idx}" data-chart="{html.escape(json.dumps(cfg), quote=True)}">'
                f'</canvas></div>')

    html_text = re.sub(
        r'<pre><code class="language-chart">(.*?)</code></pre>', repl,
        html_text, flags=re.S)
    return html_text, charts


CHART_JS_CDN = "https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"


# ---------------------------------------------------------------- views
STATUS_STYLE = {"done": "done", "in_progress": "in progress", "blocked": "blocked"}


def build_nav(current_id=None):
    """Sidebar: collapsible Agents section (agent names filter the board),
    then a by-date section with collapsible date groups."""
    items = load_submissions()
    items.sort(key=lambda x: x.get("ts", 0), reverse=True)
    by_agent = {}
    for it in items:
        by_agent.setdefault(it.get("agent", "unknown"), []).append(it)

    def link(s):
        sel = ' class="sel"' if s["id"] == current_id else ""
        return (f'<a href="/view/{s["id"]}"{sel}>'
                f'{html.escape(s.get("title", "(untitled)"))}</a>')

    # Section 1: Agents — whole section collapses; agent names filter board.
    agent_links = "".join(
        f'<a class="navlink" href="/?agent={html.escape(a, quote=True)}">'
        f'{html.escape(a)} <span class="cnt">{len(subs)}</span></a>'
        for a, subs in sorted(by_agent.items()))
    parts = [
        '<div class="navgroup open" data-key="sec-agents">'
        '<button data-key="sec-agents"><span class="caret">&#9654;</span>Agents</button>'
        f'<div class="items">{agent_links}</div></div>']

    # Section 2: Dates — each date collapses to show its posts.
    parts.append('<div class="navhead">Dates</div>')
    by_day = {}
    for it in items:
        by_day.setdefault(time.strftime("%Y-%m-%d", time.localtime(it.get("ts", 0))), []).append(it)
    for day in sorted(by_day, reverse=True):
        subs = by_day[day]
        links = "".join(link(s) for s in subs)
        parts.append(
            f'<div class="navgroup" data-key="day-{day}">'
            f'<button data-key="day-{day}">'
            f'<span class="caret">&#9654;</span>{day}'
            f'<span class="cnt">{len(subs)}</span></button>'
            f'<div class="items">{links}</div></div>')
    return "".join(parts)


def layout(title, body, scripts="", nav=""):
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · agent-briefing</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{ --bg:#fff; --fg:#171717; --dim:#666; --dimmer:#999; --line:#eaeaea;
          --panel:#fafafa; --accent:#0070f3; --accent-dark:#0058cc;
          --green:#0e9f6e; --amber:#d97706; --red:#dc2626; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.6 'Inter',-apple-system,"Segoe UI",Roboto,"Noto Sans KR",sans-serif;
         -webkit-font-smoothing:antialiased; }}
  a {{ color:inherit; text-decoration:none; }}

  header {{ border-bottom:1px solid var(--line); height:52px;
            display:flex; align-items:center; position:sticky; top:0;
            background:rgba(255,255,255,.9); backdrop-filter:blur(6px); z-index:10; }}
  header .wrap {{ padding:0 20px; width:100%; display:flex; align-items:center;
                  gap:12px; }}
  header h1 {{ font-size:14px; margin:0; font-weight:600; letter-spacing:-.01em; }}
  header h1 a:hover {{ color:var(--accent); }}
  #navbtn {{ font:inherit; font-size:13px; line-height:1; padding:6px 9px;
             border:1px solid var(--line); border-radius:6px; background:#fff;
             color:var(--dim); cursor:pointer; }}
  #navbtn:hover {{ border-color:#ccc; color:var(--fg); }}

  .shell {{ display:flex; min-height:calc(100vh - 52px); }}
  aside {{ width:260px; flex:0 0 auto; border-right:1px solid var(--line);
           padding:20px 14px 40px 20px; overflow-y:auto;
           max-height:calc(100vh - 52px); position:sticky; top:52px; }}
  aside.hidden {{ display:none; }}
  .navhead {{ font-size:11px; color:var(--dimmer); font-weight:600;
              letter-spacing:.06em; text-transform:uppercase; margin:18px 0 6px; }}
  .navhead:first-child {{ margin-top:0; }}
  .navgroup {{ margin-bottom:2px; }}
  .navgroup > button {{ font:inherit; font-size:12.5px; font-weight:500; width:100%;
      text-align:left; padding:5px 8px; border:0; border-radius:6px;
      background:none; color:var(--fg); cursor:pointer;
      display:flex; align-items:center; gap:6px; }}
  .navgroup > button:hover {{ background:var(--panel); }}
  .navgroup .caret {{ display:inline-block; transition:transform .12s ease;
      font-size:9px; color:var(--dimmer); width:10px; }}
  .navgroup.open .caret {{ transform:rotate(90deg); }}
  .navgroup .cnt {{ color:var(--dimmer); font-weight:400; font-size:11px;
                    margin-left:auto; }}
  .navgroup .items {{ display:none; }}
  .navgroup.open .items {{ display:block; }}
  .navgroup .items a {{ display:block; font-size:12.5px; color:var(--dim);
      padding:3px 8px 3px 24px; border-radius:6px;
      white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  .navgroup .items a:hover {{ color:var(--fg); background:var(--panel); }}
  .navlink {{ display:block; font-size:12.5px; color:var(--dim); padding:4px 8px;
              border-radius:6px; white-space:nowrap; overflow:hidden;
              text-overflow:ellipsis; }}
  .navlink:hover {{ color:var(--fg); background:var(--panel); }}
  .navlink.sel {{ background:var(--panel); color:var(--fg); font-weight:500; }}

  main {{ flex:1 1 auto; min-width:0; padding:24px 28px 80px; }}

  .toolbar {{ display:flex; flex-wrap:wrap; gap:6px; align-items:center;
              margin-bottom:16px; }}
  .toolbar .sep {{ width:1px; height:14px; background:var(--line); margin:0 4px; }}

  /* board table — single grid so header and rows always align */
  .board {{ border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
  .brow {{ display:grid;
      grid-template-columns:56px 24px minmax(0,1fr) minmax(80px,140px) 96px;
      align-items:center; gap:0 8px; padding:8px 14px; }}
  .bhead {{ background:var(--panel); border-bottom:1px solid var(--line);
            font-size:11px; font-weight:600; color:var(--dimmer);
            text-transform:uppercase; letter-spacing:.05em; }}
  .brows .brow {{ border-bottom:1px solid var(--line); transition:background .08s ease; }}
  .brows .brow:last-child {{ border-bottom:0; }}
  .brows .brow:hover {{ background:var(--panel); }}
  .c-ico, .c-st, .c-title, .c-agent, .c-cnt, .c-date {{ min-width:0; }}
  .thumb {{ width:44px; height:33px; object-fit:cover; border-radius:5px;
            border:1px solid var(--line); display:block; background:var(--panel); }}
  .thumb.none {{ visibility:hidden; }}
  .dot {{ display:inline-block; width:7px; height:7px; border-radius:50%;
          vertical-align:middle; }}
  .dot.done {{ background:var(--green); }}
  .dot.in_progress {{ background:var(--amber); }}
  .dot.blocked {{ background:var(--red); }}
  .c-title {{ font-size:13.5px; white-space:nowrap; overflow:hidden;
              text-overflow:ellipsis; }}
  .c-title a {{ font-weight:500; color:var(--fg); }}
  .c-title a:hover {{ color:var(--accent); }}
  .c-title .tag {{ margin-left:6px; }}
  .att {{ font-size:11px; color:var(--dimmer); margin-left:6px; }}
  .newc {{ color:var(--accent); font-size:10px; margin-left:4px; }}
  .c-agent, .h-agent {{ font-size:12.5px; color:var(--dim);
        white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  .c-cnt, .h-cnt {{ font-size:12.5px; color:var(--dim); text-align:center; }}
  .c-date, .h-date {{ font-size:12px; color:var(--dim); text-align:right;
             line-height:1.35; white-space:nowrap; }}
  .c-date .t-time {{ display:block; color:var(--dimmer); font-size:11px; }}
  .pill {{ padding:3px 10px; border-radius:6px; font-size:12px; font-weight:500;
           color:var(--dim); background:var(--panel); border:1px solid var(--line);
           transition:all .1s ease; }}
  .pill:hover {{ border-color:#ccc; color:var(--fg); }}
  .pill.sel {{ background:var(--fg); color:#fff; border-color:var(--fg); }}

  .ghead {{ font-size:11px; color:var(--dimmer); font-weight:600; margin:22px 0 8px;
            letter-spacing:.06em; text-transform:uppercase; }}
  .ghead:first-child {{ margin-top:0; }}
  .gcount {{ color:var(--dimmer); font-weight:400; letter-spacing:0;
             text-transform:none; }}

  .viewwrap {{ max-width:920px; }}
  .card {{ background:#fff; border:1px solid var(--line); border-radius:10px; }}
  a.card:hover {{ border-color:#c9c9c9;
                  box-shadow:0 1px 3px rgba(0,0,0,.06); }}
  .card h2 {{ margin:0 0 5px; font-size:14px; font-weight:600; letter-spacing:-.01em; }}
  .meta {{ color:var(--dim); font-size:12px; display:flex; flex-wrap:wrap;
           gap:4px 10px; align-items:center; }}
  .dot {{ display:inline-block; width:6px; height:6px; border-radius:50%;
          margin-right:5px; }}
  .dot.done {{ background:var(--green); }}
  .dot.in_progress {{ background:var(--amber); }}
  .dot.blocked {{ background:var(--red); }}
  .st.done {{ color:var(--green); }}
  .st.in_progress {{ color:var(--amber); }}
  .st.blocked {{ color:var(--red); }}
  .preview {{ color:var(--dim); font-size:12.5px; margin-top:4px; overflow:hidden;
              text-overflow:ellipsis; white-space:nowrap; }}
  .tag {{ font-size:11.5px; color:var(--accent); }}
  .tag:hover {{ color:var(--accent-dark); }}

  .content {{ line-height:1.7; font-size:14px; }}
  .content h1 {{ font-size:20px; letter-spacing:-.02em; margin:26px 0 8px;
                 font-weight:600; }}
  .content h2 {{ font-size:16px; letter-spacing:-.01em; margin:22px 0 6px;
                 font-weight:600; }}
  .content h3 {{ font-size:14px; margin:16px 0 4px; font-weight:600; }}
  .content pre {{ background:#0d0d0d; color:#eaeaea; border-radius:6px;
      padding:13px; overflow-x:auto; font-size:12.5px; }}
  .content code {{ background:var(--panel); border-radius:4px; padding:1px 5px;
      font-size:12.5px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
  .content pre code {{ padding:0; background:none; }}
  .content table {{ border-collapse:collapse; width:100%; font-size:13px; }}
  .content th,.content td {{ border:1px solid var(--line); padding:6px 10px;
      text-align:left; }}
  .content th {{ background:var(--panel); font-weight:500; }}
  .content blockquote {{ border-left:2px solid var(--line); margin:10px 0;
      padding:2px 14px; color:var(--dim); }}
  .content img {{ max-width:100%; border-radius:6px; border:1px solid var(--line); }}
  .content hr {{ border:0; border-top:1px solid var(--line); margin:20px 0; }}
  .content a {{ color:var(--accent); }}
  .content a:hover {{ color:var(--accent-dark); }}

  .empty {{ color:var(--dim); text-align:center; padding:70px 0; font-size:13.5px; }}
  .empty pre {{ text-align:left; display:inline-block; margin-top:16px;
                background:var(--panel); border:1px solid var(--line);
                border-radius:6px; padding:14px 18px; font-size:12px; }}
  .empty code {{ background:var(--panel); border:1px solid var(--line); }}

</style>
</head>
<body>
<header><div class="wrap">
  <button id="navbtn" aria-label="toggle sidebar">&#9776;</button>
  <h1><a href="/">agent-briefing</a></h1>
</div></header>
<div class="shell">
<aside id="sidebar">
{nav}
</aside>
<main>
{body}
</main>
</div>
<script>
(function() {{
  var btn = document.getElementById('navbtn'), sb = document.getElementById('sidebar');
  var saved = null;
  try {{ saved = localStorage.getItem('sb'); }} catch(e) {{}}
  if (saved === 'hidden') sb.classList.add('hidden');
  btn.addEventListener('click', function() {{
    sb.classList.toggle('hidden');
    try {{ localStorage.setItem('sb', sb.classList.contains('hidden') ? 'hidden' : 'open'); }} catch(e) {{}}
  }});
  document.querySelectorAll('.navgroup > button').forEach(function(b) {{
    var g = b.parentElement, key = 'ng-' + b.dataset.key;
    var savedOpen = null;
    try {{ savedOpen = localStorage.getItem(key); }} catch(e) {{}}
    if (savedOpen === 'open') g.classList.add('open');
    else if (savedOpen === 'closed') g.classList.remove('open');
    b.addEventListener('click', function() {{
      g.classList.toggle('open');
      try {{ localStorage.setItem(key, g.classList.contains('open') ? 'open' : 'closed'); }} catch(e) {{}}
    }});
  }});
}})();
</script>
{scripts}
</body>
</html>"""


CHART_SCRIPT = f"""<script src="{CHART_JS_CDN}"></script>
<script>
document.querySelectorAll('canvas[data-chart]').forEach(function(c) {{
  try {{
    var cfg = JSON.parse(c.dataset.chart);
    if (!cfg.options) cfg.options = {{}};
    if (!cfg.options.plugins) cfg.options.plugins = {{}};
    if (!cfg.options.plugins.legend) cfg.options.plugins.legend = {{labels:{{color:'#e6e9ef'}}}};
    if (!cfg.options.scales && cfg.type !== 'pie' && cfg.type !== 'doughnut') {{
      cfg.options.scales = {{
        x: {{ticks: {{color: '#8b93a7'}}, grid: {{color: '#262b36'}}}},
        y: {{ticks: {{color: '#8b93a7'}}, grid: {{color: '#262b36'}}}}
      }};
    }}
    new Chart(c, cfg);
  }} catch (e) {{ c.outerHTML = '<p style="color:#ef4444">chart error: ' + e + '</p>'; }}
}});
</script>"""


def render_index(agent_filter=None, group="agent", sort="new"):
    items = load_submissions()
    all_agents = sorted({x.get("agent", "unknown") for x in items})

    # --- toolbar: agent filter chips + sort
    def ctl(cur, val, label, key, keep):
        cls = "pill sel" if cur == val else "pill"
        keep_q = "&".join(f"{k}={html.escape(v, quote=True)}" for k, v in keep if v)
        href = f"/?{keep_q}&{key}={val}" if keep_q else f"/?{key}={val}"
        return f'<a class="{cls}" href="{href}">{label}</a>'

    chips = ""
    if all_agents:
        chip_links = [f'<a class="pill{" sel" if not agent_filter else ""}" '
                      f'href="/?sort={sort}">All {len(items)}</a>']
        for a in all_agents:
            cls = "pill sel" if agent_filter == a else "pill"
            chip_links.append(
                f'<a class="{cls}" href="/?agent={html.escape(a, quote=True)}&sort={sort}">'
                f'{html.escape(a)}</a>')
        chips = f'<div class="toolbar">{"".join(chip_links)}'
    controls = (chips or '<div class="toolbar">') + '<span class="sep"></span>' + \
        ctl(sort, "new", "newest", "sort", [("agent", agent_filter)]) + \
        ctl(sort, "old", "oldest", "sort", [("agent", agent_filter)]) + '</div>'

    if agent_filter:
        items = [x for x in items if x.get("agent", "unknown") == agent_filter]
    items.sort(key=lambda x: x.get("ts", 0), reverse=(sort != "old"))

    if not items:
        if agent_filter:
            body = (f'<div class="empty">No submissions from {html.escape(agent_filter)}. '
                    f'<a href="/" style="color:var(--accent)">View all</a></div>')
        else:
            body = '<div class="empty">No submissions yet.</div>'
        return layout("index", body, nav=build_nav())

    # --- board table (community forum style)
    IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp")
    VID_EXTS = (".mp4", ".webm", ".mov", ".avi", ".mkv")

    def thumb(it):
        atts = it.get("attachments", [])
        img = next((a for a in atts if a.lower().endswith(IMG_EXTS)), None)
        vid = next((a for a in atts if a.lower().endswith(VID_EXTS)), None)
        if img:
            return (f'<img class="thumb" loading="lazy" '
                    f'src="/attachments/{it["id"]}/{html.escape(img)}" alt="">')
        if vid:
            return (f'<video class="thumb" preload="metadata" '
                    f'src="/attachments/{it["id"]}/{html.escape(vid)}"></video>')
        return '<div class="thumb none"></div>'

    def row(it):
        st = it.get("status", "done")
        tags = " ".join(f'<a class="tag" href="/?tag={html.escape(t, quote=True)}">#{html.escape(t)}</a>'
                        for t in it.get("tags", [])[:4])
        n_att = len(it.get("attachments", []))
        att_info = f'<span class="att">📎{n_att}</span>' if n_att else ""
        return f"""
      <div class="brow">
        <div class="c-ico">{thumb(it)}</div>
        <div class="c-st"><span class="dot {html.escape(str(st))}" title="{html.escape(str(st))}"></span></div>
        <div class="c-title"><a href="/view/{it['id']}">{html.escape(it.get('title', '(untitled)'))}</a>{att_info}{tags}</div>
        <div class="c-agent">{html.escape(it.get('agent', 'unknown'))}</div>
        <div class="c-date">{time.strftime('%Y-%m-%d', time.localtime(it.get('ts', 0)))}
          <span class="t-time">{time.strftime('%H:%M', time.localtime(it.get('ts', 0)))}</span></div>
      </div>"""

    header = """
      <div class="brow bhead">
        <div class="c-ico"></div><div class="c-st"></div>
        <div class="c-title">Title</div>
        <div class="h-agent">Agent</div>
        <div class="h-date">Date</div>
      </div>"""
    rows = "".join(row(it) for it in items)
    board = f'<div class="board">{header}<div class="brows">{rows}</div></div>'
    return layout("index", controls + board, nav=build_nav())


def render_view(sub_id):
    items = load_submissions()
    it = next((x for x in items if x["id"] == sub_id), None)
    if not it:
        return ('<div class="empty">Submission not found. '
                '<a href="/">Back to list</a></div>', 404, "")
    st = it.get("status", "done")
    content, charts = extract_charts(md_to_html(sub_id, it.get("body_markdown", "")))
    atts = it.get("attachments", [])
    att_html = ""
    if atts:
        rows = "".join(
            f'<li><a href="/attachments/{it["id"]}/{html.escape(a)}" download>{html.escape(a)}</a></li>'
            for a in atts)
        att_html = f"<h3>Attachments</h3><ul>{rows}</ul>"
    updated = ""
    if it.get("updated_ts") and it["updated_ts"] != it.get("ts"):
        updated = (f'<span>· edited {time.strftime("%Y-%m-%d %H:%M", time.localtime(it["updated_ts"]))}</span>')
    body = f"""
  <div class="viewwrap">
  <p style="margin:0 0 12px;font-size:12.5px;"><a href="/" style="color:var(--dim)">&larr; agent-briefing</a></p>
  <div class="card" style="padding:22px 28px;">
    <h2 style="font-size:18px;letter-spacing:-.02em;">{html.escape(it.get('title', '(untitled)'))}</h2>
    <div class="meta" style="margin-bottom:14px;">
      <span class="st {html.escape(str(st))}"><span class="dot {html.escape(str(st))}"></span>{html.escape(str(st))}</span>
      <span>{html.escape(it.get('agent', 'unknown'))}</span>
      <span>{time.strftime('%Y-%m-%d %H:%M', time.localtime(it.get('ts', 0)))}</span>
      {updated}
    </div>
    <div class="content">{content}
    {att_html}</div>
  </div>
  </div>"""
    scripts = CHART_SCRIPT if charts else ""
    return body, 200, scripts


# ---------------------------------------------------------------- handler
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"agent-briefing/{VERSION}"

    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}",
              flush=True)

    # ---- plumbing
    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False, indent=1),
                   "application/json; charset=utf-8")

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return None, {"error": f"body too large (> {MAX_BODY} bytes)"}
        raw = self.rfile.read(n)
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            return None, {"error": "body must be a JSON object"}
        return payload, None

    def is_admin(self):
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            pw = base64.b64decode(h[6:].strip()).decode("utf-8").partition(":")[2]
        except (ValueError, UnicodeDecodeError):
            return False
        return secrets.compare_digest(pw, admin_key())

    def require_admin(self):
        """Reject with 401 unless the request carries the operator key
        (Basic auth, any username). Returns True when rejected."""
        if self.is_admin():
            return False
        body = b"admin key required\n"
        self.send_response(401)
        self.send_header("WWW-Authenticate",
                         'Basic realm="agent-briefing", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        return True

    # ---- GET
    def do_GET(self):
        u = urlparse(self.path)
        if (u.path in ("/", "/index.html")
                or u.path.startswith("/view/")
                or u.path.startswith("/attachments/")):
            if self.require_admin():
                return
        if u.path in ("/", "/index.html"):
            q = parse_qs(u.query)
            return self._send(200, render_index(
                agent_filter=(q.get("agent") or [None])[0],
                group=(q.get("group") or ["agent"])[0],
                sort=(q.get("sort") or ["new"])[0]))
        m = re.fullmatch(r"/view/([a-zA-Z0-9_-]+)", u.path)
        if m:
            body, code, scripts = render_view(m.group(1))
            title = m.group(1)
            return self._send(code, layout(title, body, scripts, nav=build_nav(m.group(1))))
        m = re.fullmatch(r"/attachments/([a-zA-Z0-9_-]+)/(.+)", u.path)
        if m:
            sub_id, rel = m.group(1), safe_rel_path(m.group(2))
            if not rel or sub_id not in [x["id"] for x in load_submissions()]:
                return self._send(404, "not found", "text/plain")
            base = os.path.realpath(attachments_dir(sub_id))
            fpath = os.path.realpath(os.path.join(base, rel))
            if not fpath.startswith(base + os.sep) or not os.path.isfile(fpath):
                return self._send(404, "not found", "text/plain")
            ext = fpath.rsplit(".", 1)[-1].lower() if "." in fpath else ""
            ctype = INLINE_TYPES.get(ext, "application/octet-stream")
            with open(fpath, "rb") as f:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(os.path.getsize(fpath)))
                if ctype.startswith(("video/", "audio/")):
                    self.send_header("Accept-Ranges", "none")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(f.read())
            return
        if u.path in ("/submissions", "/api/submissions"):
            q = parse_qs(u.query)
            agent = (q.get("agent") or [""])[0].strip()
            if not agent:
                return self._json(400, {
                    "error": "agent parameter required: "
                             "GET /submissions?agent=<your agent name> "
                             "(returns only that agent's submissions)"})
            items = [x for x in load_submissions()
                     if x.get("agent", "unknown") == agent]
            if q.get("tag"):
                items = [x for x in items if q["tag"][0] in x.get("tags", [])]
            if q.get("full") != ["true"]:
                items = [{k: v for k, v in it.items() if k != "body_markdown"}
                         for it in items]
            items.sort(key=lambda x: x.get("ts", 0),
                       reverse=(q.get("sort", ["new"])[0] != "old"))
            return self._json(200, {"count": len(items), "submissions": items})
        if u.path == "/AGENTS.md":
            try:
                with open("/app/AGENTS.md", encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/markdown; charset=utf-8")
            except FileNotFoundError:
                return self._send(404, "AGENTS.md missing", "text/plain")
        if u.path == "/healthz":
            return self._json(200, {"ok": True, "version": VERSION})
        return self._send(404, "not found", "text/plain")

    # ---- POST /submit
    def do_POST(self):
        u = urlparse(self.path)
        m = re.fullmatch(r"/submit/([a-zA-Z0-9_-]+)", u.path)
        if m:
            return self.handle_update(m.group(1), create=True)
        if u.path != "/submit":
            return self._json(404, {"error": "unknown endpoint; POST /submit or /submit/<id>"})
        payload, err = self._read_json()
        if err:
            return self._json(400, err)
        for field in ("title", "body_markdown"):
            if not payload.get(field):
                return self._json(400, {"error": f"field '{field}' is required"})
        status = payload.get("status", "done")
        if status not in STATUS_STYLE:
            return self._json(400, {"error": "status must be done|in_progress|blocked"})
        sub_id = payload.get("id") or uuid.uuid4().hex[:12]
        if not valid_id(sub_id):
            return self._json(400, {"error": "id must match [a-zA-Z0-9_-]{1,64}"})
        items = load_submissions()
        if any(x["id"] == sub_id for x in items):
            return self._json(409, {"error": f"id '{sub_id}' already exists; PATCH /submit/{sub_id} to update"})
        err, atts = self.store_attachments(sub_id, payload.get("attachments"))
        if err:
            return self._json(400, err)
        items.insert(0, {
            "id": sub_id,
            "title": str(payload["title"])[:200],
            "agent": str(payload.get("agent", "unknown"))[:100],
            "status": status,
            "tags": [str(t)[:30] for t in (payload.get("tags") or [])][:10],
            "summary": str(payload.get("summary", ""))[:300],
            "body_markdown": str(payload["body_markdown"]),
            "attachments": atts,
            "ts": time.time(),
        })
        save_submissions(items)
        return self.submit_response(201, sub_id)

    # ---- PATCH /submit/<id>
    def do_PATCH(self):
        u = urlparse(self.path)
        m = re.fullmatch(r"/submit/([a-zA-Z0-9_-]+)", u.path)
        if not m:
            return self._json(404, {"error": "PATCH /submit/<id>"})
        return self.handle_update(m.group(1), create=False)

    def handle_update(self, sub_id, create):
        items = load_submissions()
        it = next((x for x in items if x["id"] == sub_id), None)
        if it is None:
            if not create:
                return self._json(404, {"error": f"no submission '{sub_id}'"})
            # POST /submit/<id> creates with explicit id
            payload, err = self._read_json()
            if err:
                return self._json(400, err)
            payload["id"] = sub_id
            return self.do_create(payload, items)
        if create:
            return self._json(409, {"error": f"id '{sub_id}' already exists; PATCH /submit/{sub_id} to update"})
        payload, err = self._read_json()
        if err:
            return self._json(400, err)
        for field in ("title", "body_markdown", "summary", "agent"):
            if field in payload and payload[field] is not None:
                it[field] = str(payload[field])[: (200 if field != "body_markdown" else 10**9)]
        if "status" in payload:
            if payload["status"] not in STATUS_STYLE:
                return self._json(400, {"error": "status must be done|in_progress|blocked"})
            it["status"] = payload["status"]
        if "tags" in payload and isinstance(payload["tags"], list):
            it["tags"] = [str(t)[:30] for t in payload["tags"]][:10]
        # attachments: add/overwrite
        err, add_atts = self.store_attachments(sub_id, payload.get("attachments"))
        if err:
            return self._json(400, err)
        atts = it.get("attachments", [])
        for a in add_atts:
            if a not in atts:
                atts.append(a)
        # delete_attachments: remove files + refs
        for name in (payload.get("delete_attachments") or []):
            if not safe_rel_path(name):
                return self._json(400, {"error": f"bad attachment name: {name}"})
            fpath = os.path.join(attachments_dir(sub_id), name)
            try:
                os.remove(fpath)
            except FileNotFoundError:
                pass
            atts = [a for a in atts if a != name]
        it["attachments"] = atts
        it["updated_ts"] = time.time()
        save_submissions(items)
        return self.submit_response(200, sub_id)

    def do_create(self, payload, items):
        if not payload.get("title") or not payload.get("body_markdown"):
            return self._json(400, {"error": "fields 'title' and 'body_markdown' are required"})
        sub_id = payload["id"]
        err, atts = self.store_attachments(sub_id, payload.get("attachments"))
        if err:
            return self._json(400, err)
        items.insert(0, {
            "id": sub_id,
            "title": str(payload["title"])[:200],
            "agent": str(payload.get("agent", "unknown"))[:100],
            "status": payload.get("status", "done"),
            "tags": [str(t)[:30] for t in (payload.get("tags") or [])][:10],
            "summary": str(payload.get("summary", ""))[:300],
            "body_markdown": str(payload["body_markdown"]),
            "attachments": atts,
            "ts": time.time(),
        })
        save_submissions(items)
        return self.submit_response(201, sub_id)

    # ---- DELETE /submit/<id>
    def do_DELETE(self):
        m = re.fullmatch(r"/submit/([a-zA-Z0-9_-]+)", urlparse(self.path).path)
        if not m:
            return self._json(404, {"error": "DELETE /submit/<id>"})
        sub_id = m.group(1)
        items = load_submissions()
        if not any(x["id"] == sub_id for x in items):
            return self._json(404, {"error": f"no submission '{sub_id}'"})
        save_submissions([x for x in items if x["id"] != sub_id])
        import shutil
        shutil.rmtree(os.path.join(DATA_DIR, "attachments", sub_id), ignore_errors=True)
        return self._json(200, {"ok": True, "deleted": sub_id})

    # ---- attachment storage
    def store_attachments(self, sub_id, att_map):
        atts = []
        for name, dataurl in (att_map or {}).items():
            rel = safe_rel_path(name)
            if not rel:
                return {"error": f"bad attachment name: {name}"}, []
            m = re.fullmatch(r"data:([\w/+.-]+)?;base64,(.+)", str(dataurl), re.S)
            if not m:
                return {"error": f"attachment {name} must be a data:...;base64,... URL"}, []
            try:
                blob = base64.b64decode(m.group(2))
            except Exception:
                return {"error": f"attachment {name}: invalid base64"}, []
            dest = os.path.join(attachments_dir(sub_id), rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(blob)
            atts.append(rel)
        return None, atts

    def submit_response(self, code, sub_id):
        loc = f"/view/{sub_id}"
        self._json(code, {"ok": True, "id": sub_id, "url": loc,
                          "absolute_url": f"http://localhost:{PORT}{loc}"})


# ---------------------------------------------------------------- main
if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    admin_key()
    print(f"agent-briefing v{VERSION} listening on :{PORT} (data: {DATA_DIR})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
