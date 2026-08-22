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
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import markdown

DATA_DIR = os.environ.get("BRIEFING_DATA", "/app/data")
PORT = int(os.environ.get("BRIEFING_PORT", "49010"))
MAX_BODY = int(os.environ.get("BRIEFING_MAX_BODY", str(100 * 1024 * 1024)))  # 100 MB
VERSION = "1.1"

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


def layout(title, body, scripts=""):
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · agent-briefing</title>
<style>
  :root {{ --bg:#0f1115; --panel:#171a21; --line:#262b36; --fg:#e6e9ef;
          --dim:#8b93a7; --accent:#5eead4; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg);
         font:15px/1.6 -apple-system,"Segoe UI",Roboto,"Noto Sans KR",sans-serif; }}
  a {{ color:var(--accent); text-decoration:none; }}
  a:hover {{ text-decoration:underline; }}
  header {{ border-bottom:1px solid var(--line); padding:18px 28px;
            display:flex; align-items:baseline; gap:14px;
            max-width:1080px; margin:0 auto; }}
  header h1 {{ font-size:18px; margin:0; letter-spacing:.3px; }}
  header .sub {{ color:var(--dim); font-size:13px; }}
  main {{ max-width:1080px; margin:0 auto; padding:26px 28px 80px; }}
  .card {{ background:var(--panel); border:1px solid var(--line);
           border-radius:10px; padding:20px 24px; margin-bottom:14px; }}
  .card h2 {{ margin:0 0 6px; font-size:17px; }}
  .meta {{ color:var(--dim); font-size:12.5px; margin-bottom:10px;
           display:flex; flex-wrap:wrap; gap:10px; }}
  .badge {{ padding:1px 9px; border-radius:99px; font-size:11.5px;
            font-weight:600; border:1px solid var(--line); }}
  .badge.done {{ color:#22c55e; border-color:#22c55e55; }}
  .badge.in_progress {{ color:#f59e0b; border-color:#f59e0b55; }}
  .badge.blocked {{ color:#ef4444; border-color:#ef444455; }}
  .preview {{ color:var(--dim); font-size:13.5px; }}
  .content {{ line-height:1.75; word-wrap:break-word; }}
  .content h1,.content h2,.content h3 {{ border-bottom:1px solid var(--line);
      padding-bottom:6px; margin-top:28px; }}
  .content pre {{ background:#0b0d11; border:1px solid var(--line);
      border-radius:8px; padding:14px; overflow-x:auto; }}
  .content code {{ background:#0b0d11; border-radius:4px; padding:1px 5px;
      font-size:13px; }}
  .content pre code {{ padding:0; background:none; }}
  .content table {{ border-collapse:collapse; width:100%; }}
  .content th,.content td {{ border:1px solid var(--line); padding:7px 11px;
      text-align:left; }}
  .content th {{ background:#0b0d11; }}
  .content blockquote {{ border-left:3px solid var(--accent); margin:12px 0;
      padding:4px 16px; color:var(--dim); }}
  .content img {{ max-width:100%; border-radius:8px; }}
  .empty {{ color:var(--dim); text-align:center; padding:70px 0; }}
  .empty code {{ background:var(--panel); border:1px solid var(--line); }}
  .ghead {{ font-size:14px; color:var(--accent); margin:30px 0 10px;
            letter-spacing:.3px; }}
  .ghead:first-child {{ margin-top:0; }}
  .gcount {{ color:var(--dim); font-weight:400; font-size:12px; }}
  footer {{ max-width:1080px; margin:0 auto; padding:0 28px 40px;
            color:var(--dim); font-size:12.5px; }}
</style>
</head>
<body>
<header>
  <h1><a href="/">agent-briefing</a></h1>
</header>
<main>
{body}
</main>
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

    # --- filter chips (by agent)
    chips = ""
    if all_agents:
        chip_links = [f'<a class="badge" href="/?group={group}&sort={sort}">전체 ({len(items)})</a>']
        for a in all_agents:
            n = sum(1 for x in items if x.get("agent", "unknown") == a)
            cls = "badge done" if agent_filter == a else "badge"
            chip_links.append(
                f'<a class="{cls}" '
                f'href="/?agent={html.escape(a, quote=True)}&group={group}&sort={sort}">'
                f'{html.escape(a)} ({n})</a>')
        chips = f'<div class="meta" style="margin-bottom:12px">{"".join(chip_links)}</div>'

    # --- group/sort controls
    def ctl(cur, val, label, key, keep):
        cls = "badge done" if cur == val else "badge"
        keep_q = "&".join(f"{k}={html.escape(v, quote=True)}" for k, v in keep if v)
        href = f"/?{keep_q}&{key}={val}" if keep_q else f"/?{key}={val}"
        return f'<a class="{cls}" href="{href}">{label}</a>'

    controls = f'<div class="meta" style="margin-bottom:22px">그룹: ' + \
        ctl(group, "agent", "에이전트별", "group", [("agent", agent_filter), ("sort", sort)]) + " " + \
        ctl(group, "date", "일자별", "group", [("agent", agent_filter), ("sort", sort)]) + \
        ' <span style="margin-left:14px;">정렬: ' + \
        ctl(sort, "new", "최신순", "sort", [("agent", agent_filter), ("group", group)]) + " " + \
        ctl(sort, "old", "오래된순", "sort", [("agent", agent_filter), ("group", group)]) + \
        '</span></div>'

    if agent_filter:
        items = [x for x in items if x.get("agent", "unknown") == agent_filter]
    items.sort(key=lambda x: x.get("ts", 0), reverse=(sort != "old"))

    if not items:
        if agent_filter:
            body = ('<div class="empty">해당 에이전트의 제출물이 없습니다. '
                    '<a href="/">전체 보기</a></div>')
        else:
            body = """<div class="empty">
          아직 제출된 보고가 없습니다.<br><br>
          에이전트는 <code>AGENTS.md</code> 프로토콜에 따라 제출할 수 있습니다:
          <pre style="text-align:left;display:inline-block;margin-top:18px;">curl -X POST http://localhost:49010/submit \\
  -H 'Content-Type: application/json' \\
  -d '{"title":"첫 보고","body_markdown":"# 안녕하세요"}'</pre>
        </div>"""
        return layout("index", body)

    # --- grouping
    def card(it):
        st = it.get("status", "done")
        st_label = STATUS_STYLE.get(st, st)
        tags = " ".join(f'<span class="badge">#{html.escape(t)}</span>'
                        for t in it.get("tags", []))
        n_att = len(it.get("attachments", []))
        att_info = f'<span>📎 {n_att}</span>' if n_att else ""
        preview = html.escape((it.get("summary") or it.get("body_markdown", ""))[:200])
        return f"""
  <a class="card" style="display:block;color:inherit;" href="/view/{it['id']}">
    <h2>{html.escape(it.get('title', '(untitled)'))}</h2>
    <div class="meta">
      <span class="badge {html.escape(str(st))}">{html.escape(st_label)}</span>
      <span>{html.escape(it.get('agent', 'unknown'))}</span>
      <span>{time.strftime('%Y-%m-%d %H:%M', time.localtime(it.get('ts', 0)))}</span>
      {att_info}
      {tags}
    </div>
    <div class="preview">{preview}…</div>
  </a>"""

    sections = []
    if group == "date":
        by_day = {}
        for it in items:
            day = time.strftime("%Y-%m-%d (%a)", time.localtime(it.get("ts", 0)))
            by_day.setdefault(day, []).append(it)
        for day in sorted(by_day, reverse=(sort != "old")):
            sections.append(f'<h2 class="ghead">{html.escape(day)} '
                            f'<span class="gcount">{len(by_day[day])}건</span></h2>')
            sections.extend(card(it) for it in by_day[day])
    else:  # group by agent
        by_agent = {}
        for it in items:
            by_agent.setdefault(it.get("agent", "unknown"), []).append(it)
        for a in sorted(by_agent):
            sections.append(f'<h2 class="ghead">{html.escape(a)} '
                            f'<span class="gcount">{len(by_agent[a])}건</span></h2>')
            sections.extend(card(it) for it in by_agent[a])
    return layout("index", chips + controls + "".join(sections))


def render_view(sub_id):
    items = load_submissions()
    it = next((x for x in items if x["id"] == sub_id), None)
    if not it:
        return ('<div class="empty">없는 제출물입니다. <a href="/">목록으로</a></div>', 404, "")
    st = it.get("status", "done")
    st_label = STATUS_STYLE.get(st, st)
    content, charts = extract_charts(md_to_html(sub_id, it.get("body_markdown", "")))
    atts = it.get("attachments", [])
    att_html = ""
    if atts:
        rows = "".join(
            f'<li><a href="/attachments/{it["id"]}/{html.escape(a)}" download>{html.escape(a)}</a></li>'
            for a in atts)
        att_html = f"<h3>첨부 파일</h3><ul>{rows}</ul>"
    updated = ""
    if it.get("updated_ts") and it["updated_ts"] != it.get("ts"):
        updated = (f'<span>수정: {time.strftime("%Y-%m-%d %H:%M", time.localtime(it["updated_ts"]))}</span>')
    body = f"""
  <p style="margin:0 0 14px;"><a href="/">&larr; 목록으로</a></p>
  <div class="card">
    <h2 style="font-size:22px;">{html.escape(it.get('title', '(untitled)'))}</h2>
    <div class="meta">
      <span class="badge {html.escape(str(st))}">{html.escape(st_label)}</span>
      <span>{html.escape(it.get('agent', 'unknown'))}</span>
      <span>{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(it.get('ts', 0)))}</span>
      {updated}
      <span>id: {html.escape(it['id'])}</span>
    </div>
    <div class="content">{content}
    {att_html}</div>
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

    # ---- GET
    def do_GET(self):
        u = urlparse(self.path)
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
            return self._send(code, layout(title, body, scripts))
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
            items = load_submissions()
            if q.get("agent"):
                items = [x for x in items if x.get("agent", "unknown") == q["agent"][0]]
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
    print(f"agent-briefing v{VERSION} listening on :{PORT} (data: {DATA_DIR})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
