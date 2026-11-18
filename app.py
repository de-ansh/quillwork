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
def write_post(s, c, idea=None, feedback=None, current=None):
    from google import genai  # imported lazily so the UI still starts without the SDK/key
    from google.genai import types

    p = s["profile"]
    recent = [r[0][:300] for r in c.execute("SELECT text FROM posts WHERE text IS NOT NULL ORDER BY id DESC LIMIT 8")]
    system = (
        "You ghost-write LinkedIn posts for one person, in their voice.\n"
        f"About them: {p.get('about', '')}\nVoice: {p.get('voice', '')}\nAudience: {p.get('audience', '')}\n"
        f"Format: {p.get('formatting', '')}\nHashtags: {p.get('hashtags', '')}\nAvoid: {p.get('avoid', '')}\n"
        "Output only the post text. Plain text, no markdown (LinkedIn does not render it), no preamble, under 2800 characters."
    )
    parts = [f"Write a post about: {idea}" if idea else f"Write a post with a fresh angle on one of these topics: {', '.join(p['topics'])}"]
    if recent:
        parts.append("Do not repeat these recent posts:\n" + "\n---\n".join(recent))
    if current:
        parts.append(f"Rewrite this draft:\n{current}")
    if feedback:
        parts.append(f"Feedback to apply: {feedback}")
    global GEMINI
    GEMINI = GEMINI or genai.Client()  # must outlive the call: a throwaway Client closes its HTTP pool mid-request
    r = GEMINI.models.generate_content(
        model=s.get("model", "gemini-3.8-flash"), contents="\n\n".join(parts),
        config=types.GenerateContentConfig(system_instruction=system),
    )
    text = (r.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty post")
    return text


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
    c.execute("INSERT INTO posts(slot, idea, text, status, created) VALUES(?,?,?,?,?)",
              (slot, idea, write_post(s, c, idea=idea), "draft", datetime.now().isoformat(timespec="seconds")))
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
def act(f):
    pid, do, text = int(f.get("id", 0)), f.get("do"), f.get("text", "").strip()
    with LOCK, db() as c:
        p = c.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
        if not p or p["status"] == "published":
            return "Post not found or already published"
        if do in ("approve", "save", "post_now"):
            if not 0 < len(text) <= 3000:
                return "Post must be 1 to 3000 characters"
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
            c.execute("UPDATE posts SET text=?, status='draft', error=NULL WHERE id=?", (new, pid))
            msg = "Regenerated"
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
CSS = """body{font:15px/1.5 system-ui,sans-serif;max-width:760px;margin:24px auto;padding:0 16px;color-scheme:light dark}
textarea,input[type=text]{width:100%;box-sizing:border-box;font:inherit;padding:6px}
.card{border:1px solid #8885;border-radius:8px;padding:12px;margin:12px 0}.meta{font-size:13px;opacity:.75;margin-bottom:6px}
.err{color:#d33}.msg{padding:8px 12px;border-radius:6px;background:#8882}button{margin:6px 6px 0 0;padding:6px 12px}
nav a{margin-right:14px}h2{margin-top:28px}"""


def pretty(slot):
    return datetime.fromisoformat(slot).strftime("%a %d %b, %H:%M")


def layout(title, body, msg=""):
    m = f"<p class=msg>{esc(msg)}</p>" if msg else ""
    return (f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><style>{CSS}</style><nav><a href=/>Posts</a><a href=/settings>Settings</a></nav>{m}{body}</html>")


def card(p):
    st = p["status"]
    btns = {"draft": "approve save regenerate skip post_now", "approved": "approve save regenerate skip post_now",
            "failed": "post_now save regenerate skip", "skipped": "post_now", "missed": "post_now"}.get(st, "").split()
    labels = {"approve": "Approve", "save": "Save edit", "regenerate": "Regenerate", "skip": "Skip", "post_now": "Post now"}
    err = f"<div class=err>{esc(p['error'])}</div>" if p["error"] else ""
    fb = "<input type=text name=feedback placeholder='Feedback for Regenerate (optional), e.g. shorter, more personal'>" if "regenerate" in btns else ""
    buttons = "".join(f"<button name=do value={b}>{labels[b]}</button>" for b in btns)
    ro = "" if btns else " readonly"
    return (f"<form class=card method=post action=/action><input type=hidden name=id value={p['id']}>"
            f"<div class=meta>{pretty(p['slot']) if p['slot'] else 'Unscheduled'} · <b>{st}</b>{' · idea: ' + esc(p['idea']) if p['idea'] else ''}</div>{err}"
            f"<textarea name=text rows=10 aria-label='Post text'{ro}>{esc(p['text'] or '')}</textarea>{fb}{buttons}</form>")


def page_home(msg):
    t = load_token()
    if t and t["expires_at"] > time.time():
        days = int((t["expires_at"] - time.time()) / 86400)
        li = f"LinkedIn: connected as <b>{esc(t.get('name', ''))}</b>, token expires in {days} days" + (" (<a href=/login>reconnect</a>)" if days < 7 else "")
    else:
        li = "LinkedIn: not connected. <a href=/login>Connect LinkedIn</a>"
    err = f"<p class=err>Scheduler error: {esc(LAST_ERROR)}</p>" if LAST_ERROR else ""
    try:
        nxt = ", ".join(pretty(slot_key(x)) for x in upcoming_slots(load_settings())[:3])
    except Exception as e:
        nxt = esc(str(e))
    with db() as c:
        todo = c.execute("SELECT * FROM posts WHERE status IN ('draft','approved','failed') ORDER BY slot").fetchall()
        done = c.execute("SELECT * FROM posts WHERE status NOT IN ('draft','approved','failed') ORDER BY slot DESC LIMIT 30").fetchall()
    body = (f"<h1>Quillwork</h1><p>{li}</p>{err}<p class=meta>Next slots: {nxt or 'none'}</p>"
            "<form method=post action=/idea><textarea name=idea rows=2 placeholder='Got an idea? Describe it and a draft goes into the next free slot' aria-label='Post idea' required></textarea>"
            "<button>Write draft</button></form>"
            f"<h2>Needs you ({len(todo)})</h2>{''.join(map(card, todo)) or '<p class=meta>Nothing to review.</p>'}"
            f"<h2>History</h2>{''.join(map(card, done)) or '<p class=meta>No posts yet.</p>'}")
    return layout("Quillwork", body, msg)


def page_settings(msg, text=None):
    text = SETTINGS.read_text() if text is None else text
    body = ("<h1>Settings</h1><p class=meta>profile = your voice for the AI. schedule.days use Mon..Sun, times use 24h HH:MM. "
            "Drafts are written draft_hours_ahead before each slot.</p>"
            f"<form method=post action=/settings><textarea name=settings rows=28 aria-label='Settings JSON' style='font-family:monospace'>{esc(text)}</textarea>"
            "<button>Save</button></form>")
    return layout("Settings", body, msg)


def init():
    if not SETTINGS.exists():
        SETTINGS.write_text((ROOT / "settings.example.json").read_text())
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS posts(id INTEGER PRIMARY KEY, slot TEXT UNIQUE, idea TEXT, text TEXT, "
                  "status TEXT, error TEXT, linkedin_id TEXT, created TEXT, published TEXT)")


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
def action(id: int = Form(0), do: str = Form(""), text: str = Form(""), feedback: str = Form("")):
    try:
        return go("/", act({"id": id, "do": do, "text": text, "feedback": feedback}))
    except Exception as e:
        return go("/", f"Error: {e}")


@app.post("/idea", dependencies=[Depends(same_origin)])
def idea(idea: str = Form("")):
    try:
        return go("/", new_from_idea(idea.strip()) if idea.strip() else "Idea is empty")
    except Exception as e:
        return go("/", f"Error: {e}")


@app.post("/settings", response_class=HTMLResponse, dependencies=[Depends(same_origin)])
def save_settings(settings: str = Form("")):
    try:
        s = json.loads(settings)
        err = check_settings(s) if isinstance(s, dict) else "Settings must be a JSON object"
    except json.JSONDecodeError as e:
        err = f"Invalid JSON: {e}"
    if err:
        return page_settings(err, settings)
    SETTINGS.write_text(json.dumps(s, indent=2, ensure_ascii=False))
    return go("/settings", "Saved")


if __name__ == "__main__":
    import uvicorn

    print(f"Quillwork running at {BASE}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
