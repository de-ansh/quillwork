# Quillwork

Personal, local LinkedIn post automation. Gemini writes a draft before each scheduled slot, you get a Mac notification, you approve, edit, regenerate or skip it at http://localhost:8787, and it publishes to your LinkedIn at the slot time.

## Setup (one time)

1. **Gemini key:** create one at https://aistudio.google.com/apikey
2. **LinkedIn app:** go to https://www.linkedin.com/developers/apps and click **Create app**.
   - LinkedIn requires the app to be linked to a Company Page. Any page you admin works; you can create a blank one.
   - **Products** tab: add **Sign In with LinkedIn using OpenID Connect** and **Share on LinkedIn**.
   - **Auth** tab: add the redirect URL `http://localhost:8787/callback`, then copy the Client ID and Client Secret.
3. Install and configure:
   ```sh
   cd ~/Developer/Projects/quillwork
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   cp .env.example .env   # fill in the three values
   ```

## Run

```sh
.venv/bin/python app.py
```

Open http://localhost:8787, click **Connect LinkedIn**, then go to **Settings** and fill in your profile (about, voice, audience, formatting, topics) and schedule.

## How it works

- Every minute it checks for upcoming slots. Each slot within `draft_hours_ahead` hours gets a draft (status: draft).
- **Approve** posts it at the slot time. **Post now** posts immediately. **Regenerate** rewrites the draft, using your feedback if you give any. **Skip** leaves that slot empty.
- A draft that is not approved before its slot becomes `missed` and is never posted on its own.
- The "Write draft" box turns your own idea into a draft for the next free slot.
- **X (Twitter):** "Make X version" condenses a post to 280 characters or fewer. "Share on X" opens X's composer with it prefilled, and you click Post. This needs no X API or developer account.
- Data lives in this folder: `settings.json`, `posts.db` (SQLite) and `token.json` (your LinkedIn token, chmod 600, not encrypted).

## Limits

- It only runs while the app is running and your Mac is awake. An approved post whose slot passed while the Mac was asleep goes out as soon as the app runs again.
- The LinkedIn token lasts about 60 days. The home page warns you, and you click reconnect.
- Text posts only (no images yet). No engagement analytics, because LinkedIn's API doesn't provide them for personal profiles.

Tests: `.venv/bin/python test_app.py`
