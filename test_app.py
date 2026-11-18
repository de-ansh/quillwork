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
