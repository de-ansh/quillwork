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
