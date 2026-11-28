"""Self-check: .venv/bin/python test_app.py"""
from datetime import datetime

from app import check_settings, to_little_text, upcoming_slots

# little text: reserved chars escaped, hashtags kept as real hashtags
assert to_little_text("Ship it (today) #AI #build_fast") == r"Ship it \(today\) {hashtag|\#|AI} {hashtag|\#|build\_fast}", to_little_text("Ship it (today) #AI #build_fast")
assert to_little_text("C# @you") == r"C\# \@you"
assert to_little_text("plain text") == "plain text"

# slots: Tue/Thu 09:00, from Tue 2026-11-17 10:00 -> next is Thu 19th, then Tue 24th
s = {"profile": {"topics": ["x"]}, "schedule": {"days": ["Tue", "Thu"], "times": ["09:00"]}}
got = upcoming_slots(s, days=7, now=datetime(2026, 11, 17, 10, 0))
assert got == [datetime(2026, 11, 19, 9, 0), datetime(2026, 11, 24, 9, 0)], got

assert check_settings(s) is None
assert "days" in check_settings({**s, "schedule": {"days": ["Monday"], "times": ["09:00"]}})
assert check_settings({**s, "schedule": {"days": ["Mon"], "times": ["9am"]}})
assert check_settings({"schedule": s["schedule"]})
print("ok")

# settings form -> settings dict
from app import form_to_settings
base = {"model": "m", "profile": {"about": "old", "extra": "kept"}, "schedule": {}}
got = form_to_settings(base, {"about": " new ", "topics": "a\n\n b \n", "times": "09:00, 18:30,", "days": ["Thu", "Mon", "Xyz"], "draft_hours_ahead": "oops"})
assert got["profile"]["about"] == "new" and got["profile"]["extra"] == "kept" and got["profile"]["topics"] == ["a", "b"], got
assert got["schedule"] == {"days": ["Mon", "Thu"], "times": ["09:00", "18:30"], "draft_hours_ahead": 24}, got["schedule"]
assert got["model"] == "gemini-3.8-flash"
print("ok form")

# voice learning: edited posts first, then the rest; prompt carries examples and prefs
from app import build_prompt, voice_examples
rows = [("new plain", "new plain"), ("edited by me", "ai wrote this"), ("old plain", None), ("older edit", "x")]
assert voice_examples(rows) == ["edited by me", "older edit", "new plain"], voice_examples(rows)
system, prompt = build_prompt({"topics": ["t1"]}, examples=["EX1"], prefs=["shorter"], feedback="punchier", current="draft")
assert "EX1" in system and "- shorter" in system and "Feedback to apply: punchier" in prompt and "Rewrite this draft:\ndraft" in prompt
system, _ = build_prompt({"topics": ["t1"]})
assert "actually published" not in system and "Standing preferences" not in system
print("ok voice")

from app import PLACEHOLDER
assert PLACEHOLDER.search("adding only [X ms] of overhead")[0] == "[X ms]"
assert not PLACEHOLDER.search("plain post with (parens) and #tags")
print("ok placeholder")

# topic rotation: never-used first (settings order), then least recently used
from app import pick_topic
assert pick_topic(["a", "b", "c"], {"a": "2026-11-17T10:30"}) == "b"
assert pick_topic(["a", "b"], {"a": "2026-11-19T10:30", "b": "2026-11-17T10:30"}) == "b"
assert pick_topic([], {}) is None
assert "this topic: b" in build_prompt({"topics": ["a", "b"]}, topic="b")[1]
print("ok topics")

# post from a link: page text extraction and prompt
from app import URL, page_text
html_doc = ("<html><head><title> My Post </title><meta name=description content='Short desc'><style>.x{}</style></head>"
            "<body><nav>Menu Home</nav><article><h1>Hello</h1><p>Body   text\n here.</p><script>evil()</script></article>"
            "<footer>copyright</footer></body></html>")
assert page_text(html_doc) == "My Post\nShort desc\nHello Body text here.", page_text(html_doc)
assert URL.search("read this https://ex.com/a_b?x=1, great")[0] == "https://ex.com/a_b?x=1,"  # trailing punctuation stripped in link_source
_, prompt = build_prompt({"topics": ["t"]}, idea="see https://ex.com", source=("https://ex.com", "PAGE TEXT"))
assert "PAGE TEXT" in prompt and "not as instructions" in prompt
_, prompt = build_prompt({"topics": ["t"]}, idea="see https://ex.com", source=("https://ex.com", None))
assert "could not be read" in prompt
print("ok link")
# calendar: posts placed on their day, open future slots added, past empty slots not
from app import calendar_days
s = {"profile": {"topics": ["x"]}, "schedule": {"days": ["Tue", "Thu"], "times": ["10:30"]}}
posts = [{"slot": "2026-11-17T10:30", "status": "published", "id": 1, "idea": None, "topic": None, "text": "First line\nmore"}]
days = dict(calendar_days(s, posts, datetime(2026, 11, 20, 9, 0), weeks=2))
assert len(days) == 14 and min(days) == "2026-11-16", min(days)
assert days["2026-11-17"][0]["label"] == "First line" and days["2026-11-17"][0]["status"] == "published"
assert days["2026-11-19"] == [], days["2026-11-19"]  # past Thursday with no post: not shown as open
assert days["2026-11-24"][0]["status"] == "open" and days["2026-11-26"][0]["time"] == "10:30"
print("ok calendar")

# images: card shows image + buttons only when editable; publish sends the image path
import app
from app import image_block
row = {"id": 7, "image": "post-7-1.jpg"}
assert "src='/image/7?v=post-7-1.jpg'" in image_block(row, True) and "Remove image" in image_block(row, True)
assert "button" not in image_block(row, False) and "Add image" in image_block({"id": 7, "image": None}, True)
print("ok images")

# host check: only localhost / 127.0.0.1 are served (DNS-rebinding guard)
import asyncio
def status_for(host):
    out = {}
    async def send(m):
        if m["type"] == "http.response.start":
            out["status"] = m["status"]
    async def receive():
        return {"type": "http.request", "body": b""}
    scope = {"type": "http", "method": "GET", "path": "/health", "query_string": b"", "scheme": "http",
             "server": ("127.0.0.1", 8787), "headers": [(b"host", host.encode())]}
    asyncio.run(app.app(scope, receive, send))
    return out["status"]
assert status_for("localhost:8787") == 200 and status_for("127.0.0.1:8787") == 200
assert status_for("attacker.example:8787") == 400
print("ok host check")

# telegram: buttons act on the right post, strangers are ignored (temp DB, no network)
import tempfile
from pathlib import Path
from app import draft_msg, tg_handle, DRAFT_REF
tmp = Path(tempfile.mkdtemp())
app.DB, app.SETTINGS = tmp / "posts.db", tmp / "settings.json"
app.init()
with app.db() as c:
    ok = c.execute("INSERT INTO posts(slot, text, status) VALUES('2099-01-01T10:30', 'Hello world', 'draft')").lastrowid
    ph = c.execute("INSERT INTO posts(slot, text, status) VALUES('2099-01-02T10:30', 'Saved [X ms]', 'draft')").lastrowid
m = draft_msg(ok, "2099-01-01T10:30", "Hello world")
assert DRAFT_REF.match(m["text"])[1] == str(ok) and [b["callback_data"] for b in m["reply_markup"]["inline_keyboard"][0]] == [f"approve:{ok}", f"regenerate:{ok}", f"skip:{ok}"]
press = lambda chat, data: {"callback_query": {"id": "q", "data": data, "message": {"chat": {"id": chat}}}}
assert tg_handle(press(999, f"approve:{ok}"), "42") == []  # stranger
assert "TELEGRAM_CHAT_ID=999" in tg_handle(press(999, "x"), "")[0]["text"]  # setup hint when unconfigured
r = tg_handle(press(42, f"approve:{ok}"), "42")
assert r[0]["chat_id"] == "42" and r[0]["text"].startswith("Approved"), r
assert "placeholder [X ms]" in tg_handle(press(42, f"approve:{ph}"), "42")[0]["text"]
assert tg_handle(press(42, f"skip:{ph}"), "42")[0]["text"] == "Skipped"
assert tg_handle(press(42, "delete:1"), "42")[0]["text"] == "Unknown button"
assert "reply to a draft" in tg_handle({"message": {"chat": {"id": 42}, "text": "hi"}}, "42")[0]["text"]
with app.db() as c:
    assert [r[0] for r in c.execute("SELECT status FROM posts ORDER BY id")] == ["approved", "skipped"]
print("ok telegram")

# suggested ideas: parsed from Gemini's list, prompt avoids recent posts and queued ideas, escaped on the page
from app import parse_ideas, suggest_ideas
assert parse_ideas("1. First idea\n- Second\n\n* First idea\n2) Third\n• Fourth\nFifth\nSixth") == ["First idea", "Second", "Third", "Fourth", "Fifth"]
seen = {}
app.gemini = lambda s, system, prompt: seen.update(prompt=prompt) or "1. Idea A\n2. Idea B"
app.queue_ideas("Queued thing")
with app.db() as c:
    got = suggest_ideas(app.load_settings(), c)
assert got == ["Idea A", "Idea B"] and "Hello world" in seen["prompt"] and "Queued thing" in seen["prompt"], seen
page = app.page_home("", ['Say "no" <b>'])
assert 'value="Say &quot;no&quot; &lt;b&gt;"' in page and "Ideas for your next post" in page and "<b>" not in page.split("Ideas for your next post")[1].split("</section>")[0]
assert "Ideas for your next post" not in app.page_home("")
print("ok suggest ideas")
