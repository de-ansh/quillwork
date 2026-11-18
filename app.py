#!/usr/bin/env python3
"""Quillwork: Gemini drafts posts on your schedule, you approve them at http://localhost:8787, it publishes."""
import html, json, os, re, secrets, sqlite3, ssl, subprocess, threading, time
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pathlib import Path

import certifi

ROOT = Path(__file__).resolve().parent
DB, SETTINGS, TOKEN = ROOT / "posts.db", ROOT / "settings.json", ROOT / "token.json"
PORT = 8787
BASE = f"http://localhost:{PORT}"
REDIRECT = BASE + "/callback"
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
STATE = secrets.token_urlsafe(16)
LOCK = threading.Lock()
LAST_ERROR = ""
GEMINI = None
# python.org macOS builds ship no CA bundle, so urllib can't verify LinkedIn without certifi's.
TLS = ssl.create_default_context(cafile=certifi.where())

if (ROOT / ".env").exists():
    for line in (ROOT / ".env").read_text().splitlines():
        k, _, v = line.partition("=")
        if k.strip() and not line.strip().startswith("#"):
            os.environ.setdefault(k.strip(), v.strip())

esc = html.escape


# ---------- storage ----------
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def check_settings(s):
    """Return an error string, or None if settings are usable."""
    try:
        p, sch = s["profile"], s["schedule"]
        if not isinstance(p.get("topics"), list) or not p["topics"]:
            raise ValueError("profile.topics must be a non-empty list")
        if not sch["days"] or any(d not in DAYS for d in sch["days"]):
            raise ValueError(f"schedule.days must be a non-empty list of {DAYS}")
        if not sch["times"]:
            raise ValueError("schedule.times must be a non-empty list like [\"09:00\"]")
        for t in sch["times"]:
            datetime.strptime(t, "%H:%M")
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        return f"Invalid settings: {e}"


def load_settings():
    s = json.loads(SETTINGS.read_text())
    if err := check_settings(s):
        raise ValueError(err)
    return s


def load_token():
    return json.loads(TOKEN.read_text()) if TOKEN.exists() else None


def save_token(t):
    TOKEN.touch(mode=0o600)
    os.chmod(TOKEN, 0o600)
    TOKEN.write_text(json.dumps(t))


# ---------- schedule ----------
def slot_key(dt):
    return dt.isoformat(timespec="minutes")


def upcoming_slots(s, days=14, now=None):
    now = now or datetime.now()
    out = []
    for d in range(days + 1):
        day = (now + timedelta(days=d)).date()
        if DAYS[day.weekday()] in s["schedule"]["days"]:
            for t in s["schedule"]["times"]:
                slot = datetime.combine(day, datetime.strptime(t, "%H:%M").time())
                if slot > now:
                    out.append(slot)
    return sorted(out)


# ---------- AI ----------
def gemini(s, system, prompt):
    from google import genai  # imported lazily so the UI still starts without the SDK/key
    from google.genai import types

    global GEMINI
    GEMINI = GEMINI or genai.Client()  # must outlive the call: a throwaway Client closes its HTTP pool mid-request
    r = GEMINI.models.generate_content(model=s.get("model", "gemini-3.8-flash"), contents=prompt,
                                       config=types.GenerateContentConfig(system_instruction=system))
    text = (r.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty post")
    return text


def write_x(s, text):
    """Condense a LinkedIn post into one X post (280 chars max)."""
    p = s["profile"]
    system = ("You adapt LinkedIn posts for X (Twitter) for one person, in their voice.\n"
              f"Voice: {p.get('voice', '')}\nAvoid: {p.get('avoid', '')}\n"
              "Output one X post only: plain text, at most 270 characters including spaces, at most 2 hashtags, "
              "no preamble, no quotes around it.")
    out = gemini(s, system, f"Turn this LinkedIn post into a single X post that keeps the hook and the main takeaway:\n\n{text}")
    if len(out) > 280:
        out = gemini(s, system, f"Shorten this to under 270 characters, same meaning and voice:\n\n{out}")
    return out


def voice_examples(rows, n=3):
    """Pick posts in the user's real voice from (text, ai_text) rows, newest first: ones they edited before approving come first."""
    edited = [t for t, ai in rows if ai and ai.strip() != t.strip()]
    return (edited + [t for t, _ in rows if t not in edited])[:n]


def build_prompt(p, idea=None, recent=(), examples=(), prefs=(), feedback=None, current=None):
    system = (
        "You ghost-write LinkedIn posts for one person, in their voice.\n"
        f"About them: {p.get('about', '')}\nVoice: {p.get('voice', '')}\nAudience: {p.get('audience', '')}\n"
        f"Format: {p.get('formatting', '')}\nHashtags: {p.get('hashtags', '')}\nAvoid: {p.get('avoid', '')}\n"
        "Output only the post text. Plain text, no markdown (LinkedIn does not render it), no preamble, under 2800 characters.\n"
        "Never invent numbers, metrics, timings, names or events that are not given above or in the request. "
        "If a specific figure would help, write a placeholder in square brackets like [X ms] for the person to fill in."
    )
    if examples:
        system += ("\n\nPosts this person actually published. Match their voice, rhythm and length; "
                   "do not reuse their topics or sentences:\n" + "\n---\n".join(examples))
    if prefs:
        system += "\n\nStanding preferences from their past feedback:\n" + "\n".join(f"- {x}" for x in prefs)
    parts = [f"Write a post about: {idea}" if idea else f"Write a post with a fresh angle on one of these topics: {', '.join(p['topics'])}"]
    if recent:
        parts.append("Do not repeat these recent posts:\n" + "\n---\n".join(recent))
    if current:
        parts.append(f"Rewrite this draft:\n{current}")
    if feedback:
        parts.append(f"Feedback to apply: {feedback}")
    return system, "\n\n".join(parts)


def write_post(s, c, idea=None, feedback=None, current=None):
    recent = [r[0][:300] for r in c.execute("SELECT text FROM posts WHERE text IS NOT NULL ORDER BY id DESC LIMIT 8")]
    rows = c.execute("SELECT text, ai_text FROM posts WHERE status IN ('published','approved') AND text IS NOT NULL "
                     "ORDER BY id DESC LIMIT 20").fetchall()
    prefs = [r[0] for r in c.execute("SELECT text FROM feedback ORDER BY id DESC LIMIT 5")]
    system, prompt = build_prompt(s["profile"], idea, recent, voice_examples([tuple(r) for r in rows]), prefs, feedback, current)
    return gemini(s, system, prompt)


# ---------- LinkedIn ----------
def call(url, data=None, headers=None, form=False):
    headers = dict(headers or {})
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
        data = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=30, context=TLS) as r:
            body = r.read()
            return r.headers, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LinkedIn {e.code}: {e.read().decode(errors='replace')[:400]}") from None


LITTLE = re.compile(r"(?<!\w)#(\w+)|([\\|{}@\[\]()<>#*_~])")
esc_little = lambda s: re.sub(r"([\\|{}@\[\]()<>#*_~])", r"\\\1", s)


def to_little_text(text):
    """LinkedIn 'little text': reserved chars must be escaped or the post gets cut off; #word stays a real hashtag."""
    return LITTLE.sub(lambda m: "{hashtag|\\#|" + esc_little(m[1]) + "}" if m[1] else "\\" + m[2], text)


def linkedin_version():
    # shortcut: LinkedIn versions are YYYYMM and retired after ~1 year; 2 months back is safe. Set LINKEDIN_VERSION if it breaks.
    return os.environ.get("LINKEDIN_VERSION") or (datetime.now().replace(day=1) - timedelta(days=45)).strftime("%Y%m")


def linkedin_post(text):
    t = load_token()
    if not t or t["expires_at"] < time.time():
        raise RuntimeError("LinkedIn not connected or token expired: click Connect LinkedIn")
    h, _ = call(
        "https://api.linkedin.com/rest/posts",
        {
            "author": f"urn:li:person:{t['sub']}",
            "commentary": to_little_text(text),
            "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        },
        {"Authorization": f"Bearer {t['access_token']}", "LinkedIn-Version": linkedin_version(), "X-Restli-Protocol-Version": "2.0.0"},
    )
    return h.get("x-restli-id", "")


def publish(c, p):
    try:
        lid = linkedin_post(p["text"])
        c.execute("UPDATE posts SET status='published', linkedin_id=?, published=?, error=NULL WHERE id=?",
                  (lid, datetime.now().isoformat(timespec="seconds"), p["id"]))
        msg = "Published to LinkedIn"
    except Exception as e:
        c.execute("UPDATE posts SET status='failed', error=? WHERE id=?", (str(e)[:500], p["id"]))
        msg = f"Publish failed: {e}"
    c.commit()
    notify(msg)
    return msg


def notify(msg):
    try:
        subprocess.run(["osascript", "-e", f"display notification {json.dumps(msg)} with title \"Quillwork\""], check=False, timeout=5)
    except Exception:
        pass


# ---------- scheduler ----------
def insert_draft(c, s, slot, idea=None):
    text = write_post(s, c, idea=idea)
    c.execute("INSERT INTO posts(slot, idea, text, ai_text, status, created) VALUES(?,?,?,?,?,?)",
              (slot, idea, text, text, "draft", datetime.now().isoformat(timespec="seconds")))
    c.commit()
    notify(f"New LinkedIn draft for {pretty(slot)}: approve at {BASE}")


def tick():
    s = load_settings()
    now = datetime.now()
    ahead = timedelta(hours=s["schedule"].get("draft_hours_ahead", 24))
    with LOCK, db() as c:
        for slot in upcoming_slots(s):
            if slot - now > ahead:
                break
            if not c.execute("SELECT 1 FROM posts WHERE slot=?", (slot_key(slot),)).fetchone():
                insert_draft(c, s, slot_key(slot))
        nowkey = slot_key(now)
        for p in c.execute("SELECT * FROM posts WHERE status='approved' AND slot<=?", (nowkey,)).fetchall():
            publish(c, p)
        c.execute("UPDATE posts SET status='missed' WHERE status='draft' AND slot<?", (nowkey,))
        c.commit()


def loop():
    global LAST_ERROR
    while True:
        try:
            tick()
            LAST_ERROR = ""
        except Exception as e:
            LAST_ERROR = str(e)
            print("scheduler error:", e, flush=True)
        time.sleep(60)


# ---------- actions ----------
PLACEHOLDER = re.compile(r"\[[^\]\n]{1,40}\]")


def act(f):
    pid, do, text = int(f.get("id", 0)), f.get("do"), f.get("text", "").strip()
    with LOCK, db() as c:
        p = c.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
        if not p:
            return "Post not found"
        if p["status"] == "published" and do not in ("x_generate", "x_save"):
            return "Already published"
        if f.get("x_text") is not None:
            c.execute("UPDATE posts SET x_text=? WHERE id=?", (f["x_text"].strip() or None, pid))
        if do in ("approve", "save", "post_now"):
            if not 0 < len(text) <= 3000:
                return "Post must be 1 to 3000 characters"
            if do != "save" and (m := PLACEHOLDER.search(text)):
                return f"Fill in the placeholder {m[0]} before approving or posting"
            c.execute("UPDATE posts SET text=? WHERE id=?", (text, pid))
        if do == "approve":
            if p["slot"] <= slot_key(datetime.now()):
                return "That slot has passed: use Post now instead"
            c.execute("UPDATE posts SET status='approved' WHERE id=?", (pid,))
            msg = f"Approved for {pretty(p['slot'])}"
        elif do == "save":
            msg = "Saved"
        elif do == "skip":
            c.execute("UPDATE posts SET status='skipped' WHERE id=?", (pid,))
            msg = "Skipped"
        elif do == "regenerate":
            new = write_post(load_settings(), c, idea=p["idea"], feedback=f.get("feedback"), current=text or p["text"])
            c.execute("UPDATE posts SET text=?, ai_text=?, status='draft', error=NULL WHERE id=?", (new, new, pid))
            fb = (f.get("feedback") or "").strip()
            if fb and not c.execute("SELECT 1 FROM feedback WHERE text=?", (fb,)).fetchone():
                c.execute("INSERT INTO feedback(text, created) VALUES(?,?)", (fb, datetime.now().isoformat(timespec="seconds")))
            msg = "Regenerated" + (" (feedback remembered)" if fb else "")
        elif do == "x_generate":
            c.execute("UPDATE posts SET x_text=? WHERE id=?", (write_x(load_settings(), text or p["text"]), pid))
            msg = "X version written"
        elif do == "x_save":
            msg = "X text saved"
        elif do == "post_now":
            c.commit()
            return publish(c, c.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone())
        else:
            return "Unknown action"
        c.commit()
        return msg


def new_from_idea(idea):
    s = load_settings()
    with LOCK, db() as c:
        taken = {r[0] for r in c.execute("SELECT slot FROM posts WHERE slot IS NOT NULL")}
        free = [k for k in map(slot_key, upcoming_slots(s, 30)) if k not in taken]
        if not free:
            return "No free slot in the next 30 days: add days or times in Settings"
        insert_draft(c, s, free[0], idea=idea)
        return f"Draft written for {pretty(free[0])}"


# ---------- pages ----------
CSS = """
:root{--bg:#f6f5f1;--fg:#1b2133;--muted:#626a7e;--card:#fff;--line:#e4e2da;--field:#fbfaf7;--ink:#1d2a4a;--ink-fg:#fff;
--gold:#c98a12;--ok:#1f8a5b;--bad:#c53b3b;--info:#2f5fb3}
@media (prefers-color-scheme:dark){:root{--bg:#0f131d;--fg:#e9ecf3;--muted:#9aa2b4;--card:#171e2e;--line:#283149;--field:#121827;
--ink:#e9ecf5;--ink-fg:#111827;--gold:#f4b740;--ok:#4cc38a;--bad:#f07171;--info:#7aa5ff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:inherit}
.wrap{max-width:820px;margin:0 auto;padding:0 16px 64px}
.top{display:flex;align-items:center;gap:10px;padding:18px 0 14px}
.top svg{width:32px;height:32px;flex:none}
.top b{font-size:18px;letter-spacing:-.01em}
.top nav{margin-left:auto;display:flex;gap:4px}
.top nav a{padding:6px 12px;border-radius:8px;text-decoration:none;color:var(--muted);border:1px solid transparent}
.top nav a.on{background:var(--card);color:var(--fg);border-color:var(--line)}
.flash{padding:10px 14px;border-radius:10px;border:1px solid var(--line);border-left:4px solid var(--ok);background:var(--card);margin:0 0 16px}
.flash.bad{border-left-color:var(--bad)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(220px,100%),1fr));gap:12px;margin-bottom:12px}
.stat,.panel,.post{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px}
.stat small,.eyebrow{display:block;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:var(--bad);vertical-align:middle}
.dot.ok{background:var(--ok)}
.alert{color:var(--bad);border:1px solid var(--bad);border-radius:10px;padding:10px 14px;margin:0 0 12px;font-size:14px}
.post{margin:12px 0}
h1{font-size:22px;margin:8px 0 4px}
h2{font-size:17px;margin:28px 0 4px;display:flex;align-items:center;gap:8px}
.badge{font-size:12px;font-weight:600;background:var(--line);border-radius:99px;padding:1px 9px}
textarea,input[type=text],input[type=number]{width:100%;font:inherit;color:inherit;background:var(--field);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
textarea{resize:vertical;line-height:1.6}
textarea:focus,input:focus{outline:2px solid var(--gold);outline-offset:1px}
.post-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.when{font-weight:650}
.chip{font-size:12px;font-weight:650;padding:1px 10px;border-radius:99px;border:1px solid currentColor;text-transform:capitalize}
.chip.draft{color:var(--info)}.chip.approved,.chip.published{color:var(--ok)}.chip.failed{color:var(--bad)}.chip.skipped,.chip.missed{color:var(--muted)}
.idea,.hint{color:var(--muted);font-size:13px}
.err{color:var(--bad);font-size:14px;margin-bottom:8px}
.row{display:flex;gap:10px;align-items:center;margin-top:8px}
.count{font-size:12px;color:var(--muted);white-space:nowrap;margin-left:auto}.count.over{color:var(--bad);font-weight:650}
.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
button{font:inherit;font-weight:600;border-radius:10px;padding:8px 14px;border:1px solid var(--line);background:transparent;color:var(--fg);cursor:pointer}
button:hover{border-color:var(--muted)}
button.primary{background:var(--ink);color:var(--ink-fg);border-color:var(--ink)}
button.danger{color:var(--bad)}
button:disabled{opacity:.55;cursor:progress}
.body{white-space:pre-wrap;margin:0}
details>summary{cursor:pointer;color:var(--muted);margin:28px 0 8px;font-weight:600}
.empty{color:var(--muted);text-align:center;padding:22px;border:1px dashed var(--line);border-radius:14px;margin:12px 0}
label.f{display:block;font-weight:600;margin:16px 0 6px}
label.f small{font-weight:400;color:var(--muted)}
.days{display:flex;flex-wrap:wrap;gap:6px}
.day input{position:absolute;opacity:0;pointer-events:none}
.day span{display:inline-block;padding:6px 14px;border:1px solid var(--line);border-radius:99px;cursor:pointer;font-weight:600}
.day input:checked+span{background:var(--ink);color:var(--ink-fg);border-color:var(--ink)}
.day input:focus-visible+span{outline:2px solid var(--gold)}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(220px,100%),1fr));gap:0 14px}
.x{border-top:1px dashed var(--line);margin-top:14px;padding-top:12px}
.pref{display:flex;align-items:center;gap:10px;padding:8px 0;border-top:1px solid var(--line)}
.pref span{flex:1}.pref button{padding:4px 10px;font-size:13px}
.save{position:sticky;bottom:0;background:var(--bg);padding:12px 0;margin-top:16px;border-top:1px solid var(--line)}
"""

JS = """
document.querySelectorAll('textarea').forEach(t=>{const c=t.dataset.count!==undefined&&t.parentElement.querySelector('.count');
 const max=+(t.dataset.max||3000);
 const f=()=>{if(c){const n=t.value.length;c.textContent=n+' / '+max;c.classList.toggle('over',n>max)}
  if(t.value){t.style.height='auto';t.style.height=(t.scrollHeight+2)+'px'}};
 t.addEventListener('input',f);f()});
document.addEventListener('submit',e=>{const b=e.submitter;
 if(b&&b.dataset.confirm&&!confirm(b.dataset.confirm)){e.preventDefault();return}
 // disable after the browser has captured the clicked button's value
 setTimeout(()=>{e.target.querySelectorAll('button').forEach(x=>x.disabled=true);if(b&&b.dataset.busy)b.textContent=b.dataset.busy},0)});
document.addEventListener('click',e=>{const b=e.target.closest('[data-share-x]');if(!b)return;
 const t=b.closest('.x').querySelector('textarea');window.open('https://x.com/intent/tweet?text='+encodeURIComponent(t.value),'_blank','noopener')});
if(location.search.includes('msg='))history.replaceState(null,'',location.pathname);
"""

LOGO = ('<svg viewBox="0 0 512 512" aria-hidden="true"><rect width="512" height="512" rx="112" fill="#1d2a4a"/>'
        '<path d="M138 392C166 292 246 172 398 102C386 168 356 212 316 236L344 240C302 302 230 354 138 392Z" fill="#fff"/>'
        '<path d="M138 392L338 170" stroke="#1d2a4a" stroke-width="9" stroke-linecap="round"/>'
        '<path d="M138 418Q250 440 380 410" fill="none" stroke="#f4b740" stroke-width="16" stroke-linecap="round"/></svg>')
FAVICON = "data:image/svg+xml," + urllib.parse.quote(LOGO.replace(' aria-hidden="true"', ' xmlns="http://www.w3.org/2000/svg"'))
BAD_MSG = re.compile(r"fail|error|must|not found|invalid|no free|passed|empty|set linkedin|unknown", re.I)
PROFILE_TEXT = ("about", "voice", "audience", "formatting", "hashtags", "avoid")


def pretty(slot):
    return datetime.fromisoformat(slot).strftime("%a %d %b, %H:%M")


def layout(title, body, msg="", page="posts"):
    flash = f"<div class='flash{' bad' if BAD_MSG.search(msg) else ''}' role=status>{esc(msg)}</div>" if msg else ""
    nav = "".join(f"<a href='{h}'{' class=on aria-current=page' if k == page else ''}>{n}</a>"
                  for k, h, n in (("posts", "/", "Posts"), ("settings", "/settings", "Settings")))
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><link rel=icon href=\"{FAVICON}\"><style>{CSS}</style></head><body><div class=wrap>"
            f"<header class=top>{LOGO}<b>Quillwork</b><nav>{nav}</nav></header>{flash}{body}</div><script>{JS}</script></body></html>")


def x_block(p):
    if not p["text"]:
        return ""
    if not p["x_text"]:
        return "<div class=x><div class=actions><button name=do value=x_generate data-busy='Writing…'>Make X version</button></div></div>"
    return ("<div class=x><label class=eyebrow for=x{0}>X version</label>"
            "<textarea id=x{0} name=x_text rows=3 data-count data-max=280>{1}</textarea><div class=row><span class=count></span></div>"
            "<div class=actions><button type=button data-share-x>Share on X</button>"
            "<button name=do value=x_save>Save X text</button>"
            "<button name=do value=x_generate data-busy='Writing…'>Rewrite for X</button></div></div>").format(p["id"], esc(p["x_text"]))


def card(p):
    st = p["status"]
    idea = f"<span class=idea>Idea: {esc(p['idea'])}</span>" if p["idea"] else ""
    head = (f"<div class=post-head><span class=when>{pretty(p['slot']) if p['slot'] else 'Unscheduled'}</span>"
            f"<span class='chip {st}'>{st}</span>{idea}</div>")
    err = f"<div class=err>{esc(p['error'])}</div>" if p["error"] else ""
    if st == "published":
        link = (f" · <a href='https://www.linkedin.com/feed/update/{esc(p['linkedin_id'])}/' target=_blank rel=noopener>View on LinkedIn</a>"
                if p["linkedin_id"] else "")
        when = pretty(p["published"][:16]) if p["published"] else ""
        return (f"<form class=post method=post action=/action><input type=hidden name=id value={p['id']}>{head}"
                f"<p class=body>{esc(p['text'] or '')}</p><p class=hint>Published {when}{link}</p>{x_block(p)}</form>")
    editable = st in ("draft", "approved", "failed")
    post_now = "<button name=do value=post_now class=danger data-confirm='Publish this post on LinkedIn right now?' data-busy='Posting…'>Post now</button>"
    if editable:
        primary = ("<button name=do value=approve class=primary>Approve</button>" if st != "approved"
                   else "<button name=do value=save class=primary>Save</button>")
        actions = (primary + ("<button name=do value=save>Save edit</button>" if st != "approved" else "")
                   + "<button name=do value=regenerate data-busy='Writing…'>Regenerate</button>"
                   + "<button name=do value=skip data-confirm='Skip this slot? It will not be posted.'>Skip</button>" + post_now)
        extra = ("<div class=row><input type=text name=feedback aria-label='Feedback for Regenerate' "
                 "placeholder='Feedback for Regenerate, e.g. shorter, more personal, add a story'><span class=count></span></div>")
    else:
        actions, extra = post_now, "<div class=row><span class=count></span></div>"
    return (f"<form class=post method=post action=/action><input type=hidden name=id value={p['id']}>{head}{err}"
            f"<textarea name=text rows=8 data-count aria-label='Post text'>{esc(p['text'] or '')}</textarea>{extra}"
            f"<div class=actions>{actions}</div>{x_block(p)}</form>")


def page_home(msg):
    t = load_token()
    if t and t["expires_at"] > time.time():
        days = int((t["expires_at"] - time.time()) / 86400)
        li = (f"<span class='dot ok'></span>Connected as <b>{esc(t.get('name', ''))}</b>"
              f"<div class=hint>Token expires in {days} days{' · <a href=/login>Reconnect</a>' if days < 7 else ''}</div>")
    else:
        li = "<span class=dot></span>Not connected<div class=hint><a href=/login>Connect LinkedIn</a></div>"
    try:
        slots = upcoming_slots(load_settings())[:3]
        nxt = "<br>".join(pretty(slot_key(x)) for x in slots) or "None scheduled"
    except Exception as e:
        nxt = f"<span class=err>{esc(str(e))}</span>"
    with db() as c:
        todo = c.execute("SELECT * FROM posts WHERE status IN ('draft','approved','failed') ORDER BY slot").fetchall()
        done = c.execute("SELECT * FROM posts WHERE status NOT IN ('draft','approved','failed') ORDER BY slot DESC LIMIT 30").fetchall()
    approved = sum(p["status"] == "approved" for p in todo)
    alert = f"<div class=alert role=alert><b>Scheduler error:</b> {esc(LAST_ERROR)}</div>" if LAST_ERROR else ""
    body = (f"{alert}<div class=stats><div class=stat><small>LinkedIn</small>{li}</div>"
            f"<div class=stat><small>Next slots</small>{nxt}</div>"
            f"<div class=stat><small>Queue</small><b>{approved}</b> approved · <b>{len(todo) - approved}</b> to review</div></div>"
            "<form class=panel method=post action=/idea><label class=eyebrow for=idea>New idea</label>"
            "<textarea id=idea name=idea rows=2 required placeholder='A moment, lesson or opinion. Quillwork writes it up for your next free slot.'></textarea>"
            "<div class=actions><button class=primary data-busy='Writing…'>Write draft</button></div></form>"
            f"<h2>Needs you <span class=badge>{len(todo)}</span></h2>"
            f"{''.join(map(card, todo)) or '<div class=empty>Nothing to review. New drafts appear here before each slot.</div>'}"
            f"<details{' open' if done and not todo else ''}><summary>History ({len(done)})</summary>"
            f"{''.join(map(card, done)) or '<div class=empty>No posts yet.</div>'}</details>")
    return layout("Quillwork", body, msg)


def form_to_settings(base, f):
    """Build settings from the Settings form; f maps field -> str, except 'days' -> list."""
    try:
        hours = int(f.get("draft_hours_ahead") or 24)
    except ValueError:
        hours = 24
    return {**base, "model": (f.get("model") or "").strip() or "gemini-3.8-flash",
            "profile": {**base.get("profile", {}), **{k: (f.get(k) or "").strip() for k in PROFILE_TEXT},
                        "topics": [t.strip() for t in (f.get("topics") or "").splitlines() if t.strip()]},
            "schedule": {"days": [d for d in DAYS if d in f.get("days", [])],
                         "times": [t.strip() for t in (f.get("times") or "").split(",") if t.strip()],
                         "draft_hours_ahead": hours}}


def page_settings(msg, s=None):
    s = s if s is not None else json.loads(SETTINGS.read_text())
    p, sch = s.get("profile", {}), s.get("schedule", {})

    def area(k, label, hint, rows=3, val=None):
        v = p.get(k, "") if val is None else val
        return (f"<div><label class=f for={k}>{label} <small>{hint}</small></label>"
                f"<textarea id={k} name={k} rows={rows}>{esc(v)}</textarea></div>")

    days = "".join(f"<label class=day><input type=checkbox name=days value={d}{' checked' if d in sch.get('days', []) else ''}><span>{d}</span></label>"
                   for d in DAYS)
    with db() as c:
        prefs = c.execute("SELECT id, text FROM feedback ORDER BY id DESC").fetchall()
        n_pub = c.execute("SELECT count(*) FROM posts WHERE status='published'").fetchone()[0]
    items = "".join(f"<form method=post action=/feedback/delete class=pref><input type=hidden name=id value={r['id']}>"
                    f"<span>{esc(r['text'])}</span><button aria-label='Forget this preference'>Forget</button></form>" for r in prefs)
    learned = ("<section class=panel style='margin:12px 0'><span class=eyebrow>Learned from you</span>"
               f"<p class=hint>Drafts imitate your {min(n_pub, 3)} most recent published posts (the ones you edited come first). "
               "Feedback you give on Regenerate is remembered here; the newest 5 are used.</p>"
               f"{items or '<p class=hint>No feedback yet.</p>'}</section>")
    body = ("<h1>Settings</h1><p class=hint>This is what the AI knows about you. The more specific, the better the drafts.</p>"
            + learned + "<form method=post action=/settings>"
            "<section class=panel><span class=eyebrow>About you</span>"
            + area("about", "Background", "role, experience, what you've built", 6)
            + "<div class=grid2>" + area("voice", "Voice", "how you sound") + area("audience", "Audience", "who you write for") + "</div>"
            + "</section><section class=panel style='margin-top:12px'><span class=eyebrow>Content</span>"
            + area("topics", "Topics", "one per line", 6, "\n".join(p.get("topics", [])))
            + area("formatting", "Format", "structure and length")
            + "<div class=grid2>" + area("hashtags", "Hashtags", "how many and which kind", 2) + area("avoid", "Avoid", "things never to write", 2) + "</div>"
            + "</section><section class=panel style='margin-top:12px'><span class=eyebrow>Schedule</span>"
            f"<label class=f>Posting days</label><div class=days role=group aria-label='Posting days'>{days}</div>"
            "<div class=grid2>"
            f"<div><label class=f for=times>Times <small>24h, comma separated</small></label><input type=text id=times name=times value='{esc(', '.join(sch.get('times', [])))}' placeholder='09:00, 18:00'></div>"
            f"<div><label class=f for=dh>Draft ahead <small>hours before each slot</small></label><input type=number id=dh name=draft_hours_ahead min=1 max=168 value='{esc(str(sch.get('draft_hours_ahead', 24)))}'></div>"
            "</div></section><section class=panel style='margin-top:12px'><span class=eyebrow>Advanced</span>"
            f"<label class=f for=model>Gemini model</label><input type=text id=model name=model value='{esc(s.get('model', 'gemini-3.8-flash'))}'>"
            "</section><div class=save><button class=primary>Save settings</button></div></form>")
    return layout("Settings · Quillwork", body, msg, page="settings")


def init():
    if not SETTINGS.exists():
        SETTINGS.write_text((ROOT / "settings.example.json").read_text())
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS posts(id INTEGER PRIMARY KEY, slot TEXT UNIQUE, idea TEXT, text TEXT, "
                  "status TEXT, error TEXT, linkedin_id TEXT, created TEXT, published TEXT, x_text TEXT)")
        cols = [r[1] for r in c.execute("PRAGMA table_info(posts)")]
        for col in ("x_text", "ai_text"):
            if col not in cols:
                c.execute(f"ALTER TABLE posts ADD COLUMN {col} TEXT")
        c.execute("CREATE TABLE IF NOT EXISTS feedback(id INTEGER PRIMARY KEY, text TEXT UNIQUE, created TEXT)")


@asynccontextmanager
async def lifespan(_):
    init()
    threading.Thread(target=loop, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def same_origin(request: Request):
    # Block other websites from posting to this local server (CSRF).
    if request.headers.get("origin") not in (BASE, f"http://127.0.0.1:{PORT}"):
        raise HTTPException(403, "Bad origin")


def go(to, msg=""):
    return RedirectResponse(to + ("?msg=" + urllib.parse.quote(msg) if msg else ""), status_code=303)


# Routes are plain `def` so FastAPI runs them in a thread pool; Gemini/LinkedIn calls block.
@app.get("/", response_class=HTMLResponse)
def home(msg: str = ""):
    return page_home(msg)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(msg: str = ""):
    return page_settings(msg)


@app.get("/login")
def login():
    if not os.environ.get("LINKEDIN_CLIENT_ID"):
        return go("/", "Set LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET in .env first")
    return RedirectResponse("https://www.linkedin.com/oauth/v2/authorization?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": os.environ["LINKEDIN_CLIENT_ID"], "redirect_uri": REDIRECT,
        "state": STATE, "scope": "openid profile w_member_social"}), status_code=303)


@app.get("/callback")
def callback(state: str = "", code: str = "", error: str = "", error_description: str = ""):
    if state != STATE:
        return HTMLResponse("Bad OAuth state. Start again from the home page.", 400)
    if not code:
        return go("/", f"LinkedIn login failed: {error_description or error or 'unknown'}")
    try:
        _, tok = call("https://www.linkedin.com/oauth/v2/accessToken", {
            "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
            "client_id": os.environ["LINKEDIN_CLIENT_ID"], "client_secret": os.environ["LINKEDIN_CLIENT_SECRET"]}, form=True)
        _, me = call("https://api.linkedin.com/v2/userinfo", headers={"Authorization": f"Bearer {tok['access_token']}"})
        save_token({"access_token": tok["access_token"], "expires_at": time.time() + tok["expires_in"],
                    "sub": me["sub"], "name": me.get("name", "")})
        return go("/", "LinkedIn connected")
    except Exception as e:
        return go("/", f"LinkedIn login failed: {e}")


@app.post("/action", dependencies=[Depends(same_origin)])
def action(id: int = Form(0), do: str = Form(""), text: str = Form(""), feedback: str = Form(""), x_text: str | None = Form(None)):
    try:
        return go("/", act({"id": id, "do": do, "text": text, "feedback": feedback, "x_text": x_text}))
    except Exception as e:
        return go("/", f"Error: {e}")


@app.post("/idea", dependencies=[Depends(same_origin)])
def idea(idea: str = Form("")):
    try:
        return go("/", new_from_idea(idea.strip()) if idea.strip() else "Idea is empty")
    except Exception as e:
        return go("/", f"Error: {e}")


@app.post("/feedback/delete", dependencies=[Depends(same_origin)])
def forget_feedback(id: int = Form(0)):
    with LOCK, db() as c:
        c.execute("DELETE FROM feedback WHERE id=?", (id,))
    return go("/settings", "Preference forgotten")


@app.post("/settings", response_class=HTMLResponse, dependencies=[Depends(same_origin)])
async def save_settings(request: Request):
    form = await request.form()
    f = {k: form.get(k, "") for k in (*PROFILE_TEXT, "topics", "times", "draft_hours_ahead", "model")}
    f["days"] = form.getlist("days")
    s = form_to_settings(json.loads(SETTINGS.read_text()), f)
    if err := check_settings(s):
        return page_settings(err, s)
    SETTINGS.write_text(json.dumps(s, indent=2, ensure_ascii=False))
    return go("/settings", "Settings saved")


if __name__ == "__main__":
    import uvicorn

    print(f"Quillwork running at {BASE}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
