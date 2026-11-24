# Cook AI

Pantry and meal-suggestion web app. Each user signs in with Google and gets a private kitchen: pantry, kitchen essentials, favourite dishes, a not-allowed list, a shopping list and cooking history.

**Free stack:** Streamlit Community Cloud (hosting) · Turso (cloud SQLite) · Google Gemini API (AI) · Google sign-in.

**Files:** `app.py` (screens) · `database.py` (Turso, per-user data) · `gemini_client.py` (AI + checks) · `logic.py` (matching rules) · `requirements.txt` · `.streamlit/secrets.toml.example`

---

## 1. Turso database

1. Sign up at https://turso.tech (free plan).
2. Dashboard → **Create Database** → name it `cook-ai` → pick the region nearest your users.
3. Open the database. Copy the **URL** (`libsql://cook-ai-….turso.io`).
4. Click **Generate Token** (read & write, no expiry) and copy it.

The app creates its own tables on first start. Nothing to run by hand.

## 2. Gemini API key

1. Go to https://aistudio.google.com/apikey and sign in.
2. **Create API key** → copy it.
3. The free tier has per-minute and per-day limits shared by all your users. The app counts requests per user (`DAILY_SUGGESTIONS`, default 10) and falls back to plain saved dishes if the limit is hit.

## 3. Google sign-in

1. Open https://console.cloud.google.com → create a project (for example `cook-ai`).
2. **APIs & Services → OAuth consent screen** → **External** → fill the app name and your email → save.
   Scopes: only the default `openid`, `email`, `profile`.
3. **Credentials → Create credentials → OAuth client ID** → type **Web application**.
   - Authorised redirect URIs: add both
     `https://YOUR-APP-NAME.streamlit.app/oauth2callback` and, for local tests, `http://localhost:8501/oauth2callback`.
4. Copy the **Client ID** and **Client secret**.
5. While the consent screen is in **Testing**, only emails you list under *Test users* can sign in (limit 100). When you are ready for everyone, click **Publish app** → *In production*. With only the basic scopes, Google does not require a review.

## 4. Put the code on GitHub

1. Create a **private** GitHub repository and upload all files in this folder (keep `.gitignore`).
2. Never upload `.streamlit/secrets.toml` or `.env`. They are in `.gitignore`.

## 5. Deploy on Streamlit Community Cloud

1. Go to https://share.streamlit.io → sign in with GitHub → **Create app**.
2. Pick your repository, branch `main`, main file `app.py`. Choose a URL name (it must match the redirect URI from step 3).
3. **Advanced settings** → Python version 3.12 → paste the **Secrets** below (use your own values) → **Deploy**.

```toml
GEMINI_API_KEY = "…"
TURSO_DATABASE_URL = "libsql://cook-ai-….turso.io"
TURSO_AUTH_TOKEN = "…"

[auth]
redirect_uri = "https://YOUR-APP-NAME.streamlit.app/oauth2callback"
cookie_secret = "a-long-random-string-of-32-or-more-characters"
client_id = "….apps.googleusercontent.com"
client_secret = "…"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
```

4. Open the app link on your phone and laptop, sign in with Google and add a few pantry items.
5. Share the link. Free Streamlit apps go to sleep after a few days without visits and wake up in about 30 seconds.

## Run locally

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Add your Gemini API key in .env
cp .env.example .env
# Edit .env and set GEMINI_API_KEY="your-gemini-key"

# 3. Launch the Modern Web App (Desktop & Mobile Responsive)
python -m uvicorn server:app --reload --port 8000
```
Open **`http://localhost:8000`** in your browser.

*(Optional legacy Streamlit view: `streamlit run app.py`)*


To test Google sign-in locally, copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and fill it in.

---

## How it works

- **Python decides, AI writes.** Python matches saved dishes to the pantry (plurals handled, "chicken breast" counts as "chicken"), applies the diet and not-allowed list, and finds dishes missing 1-2 ingredients. Gemini writes the steps and two new ideas.
- **Every AI recipe is checked in Python.** Ingredients must exist in the user's inventory, steps may only mention that recipe's own ingredients, and nothing in the not-allowed list may appear. Failed answers are retried twice with the problems listed; after that the app shows plain saved dishes.
- **Nothing is assumed.** Salt, oil, water and spices count only if the user saved them under Kitchen essentials. (Change `ASSUME_WATER` in `gemini_client.py` to relax the water rule.)
- **Privacy.** Only Google's stable account id is stored. Every query filters by it. Users can delete all their data from the Account section.

## Limits you may want to change

| Setting | Where | Default |
|---|---|---|
| Suggestions per user per day | `DAILY_SUGGESTIONS` secret | 10 |
| AI model | `GEMINI_MODEL` secret | `gemini-2.5-flash-lite` |
| Favourites that get AI steps per request | `MAX_STEP_DISHES` in `gemini_client.py` | 8 (the rest are still listed) |
| Items per list / dishes per user | `MAX_ITEMS`, `MAX_DISHES` in `database.py` | 300 / 100 |
