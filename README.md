# the-clankers-v2: Auto-Updating Company Wiki

A hackathon demo where a company wiki stays current by itself. Raw, messy updates from Slack posts, emails and drafts go into a Python bot. The bot uses **Gemini** to merge them into the existing wiki pages and publishes clean MediaWiki pages to a local **MediaWiki** instance.

```
bot/data/company_updates.json      Slack posts, emails, release-note drafts
          │
          ▼
bot/autoupdater.py ──► Gemini (google-genai)  merges new notes into the current page
          │
          ▼
MediaWiki API (localhost:8080)                creates or updates the wiki page
```

## Repository layout

| Path | Purpose |
| --- | --- |
| `docker-compose.yml` | MediaWiki 1.43 LTS and MariaDB 11.4, with persistent volumes |
| `.env.example` | Optional port and database credential overrides for Docker Compose |
| `bot/autoupdater.py` | The AI sync bot |
| `bot/requirements.txt` | Python dependencies |
| `bot/.env.example` | Bot configuration template (Gemini key, wiki bot login) |
| `bot/data/company_updates.json` | Mock raw company data (HR, engineering, release notes) |

## Prerequisites

- Docker with the Compose plugin (`docker compose version`)
- Python 3.10 or newer
- A Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey)

---

## 1. Start MediaWiki

```bash
# Optional: change the port or database passwords
cp .env.example .env

docker compose up -d
docker compose ps          # wait until "database" shows (healthy)
```

This starts two containers:

- `company-wiki-mediawiki` serves the wiki at <http://localhost:8080>.
- `company-wiki-db` runs MariaDB. It is only reachable from the wiki container and has no host port.

Data lives in the named volumes `db_data` (database) and `wiki_images` (uploads), so it survives `docker compose down`.

## 2. Run the MediaWiki web installer

Open <http://localhost:8080> and click **set up the wiki**. Go through the installer with these values:

1. **Language**: keep the defaults and click **Continue**.
2. **Welcome**: the environment checks should pass. Click **Continue**.
3. **Connect to database**:
   - Database type: **MariaDB, MySQL, or compatible**
   - Database host: `database` (this is the Compose service name, not `localhost`)
   - Database name: `wikidb`
   - Table prefix: leave empty
   - Database username: `wikiuser`
   - Database password: `wikipass`

   If you changed these in `.env`, use your values instead.
4. **Database settings**: keep "Use the same account as for installation" checked and click **Continue**.
5. **Name**: pick a wiki name (for example `Clankers Wiki`) and create the admin account (for example user `Admin`). Remember the password.
6. Select **"I'm bored already, just install the wiki."** and click **Continue** through the install.
7. On the last page, click **Download LocalSettings.php**.

## 3. Activate the wiki configuration

1. Move the downloaded `LocalSettings.php` into the repository root, next to `docker-compose.yml`. It is git-ignored because it contains secrets.
2. In `docker-compose.yml`, uncomment this line under `mediawiki.volumes`:

   ```yaml
   - ./LocalSettings.php:/var/www/html/LocalSettings.php:ro
   ```
3. Recreate the container:

   ```bash
   docker compose up -d
   ```

<http://localhost:8080> now shows your wiki's Main Page.

## 4. Create a bot password for the updater

The bot logs in through the MediaWiki API with a [bot password](https://www.mediawiki.org/wiki/Manual:Bot_passwords). It does not use your admin password.

1. Log in to the wiki as your admin user.
2. Go to <http://localhost:8080/index.php/Special:BotPasswords>.
3. Enter the bot name `autoupdater` and click **Create**.
4. Tick these grants:
   - **High-volume (bot) access**
   - **Edit existing pages**
   - **Create, edit, and move pages**
5. Click **Create**. MediaWiki shows a login name such as `Admin@autoupdater` and a generated password. Copy both now, because the password is only shown once.

## 5. Run the AI updater bot

```bash
cd bot
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env and set GEMINI_API_KEY, WIKI_BOT_USER=Admin@autoupdater and WIKI_BOT_PASSWORD
```

**Preview without touching the wiki.** This needs only the Gemini key:

```bash
python autoupdater.py --dry-run
```

**Publish to the wiki:**

```bash
python autoupdater.py
```

The bot then:

1. Reads `data/company_updates.json` and groups the entries by `target_page`.
2. Downloads each page's current wikitext from MediaWiki, if the page exists.
3. Sends the existing page and the raw notes to Gemini. Gemini uses structured JSON output and returns the full updated wikitext plus an edit summary. Later notes override earlier ones, so a correction email wins over the original Slack post.
4. Adds a bot footer and `[[Category:Auto-updated by AI]]`, then saves the page with an edit summary.
5. Records a fingerprint of each page's source notes in `.autoupdater_state.json`. Pages whose notes have not changed are skipped on the next run.

With the mock data, this creates **Remote Work Policy**, **Engineering Onboarding** and **Platform Release Notes Q3 2026**. You can see all bot-maintained pages at <http://localhost:8080/index.php/Category:Auto-updated_by_AI>. Open any page's **View history** tab to see the AI's edit summaries.

### Live demo: watch mode

```bash
python autoupdater.py --watch 30
```

Leave it running, then add or edit an entry in `data/company_updates.json`, for example a new HR note that targets `Remote Work Policy`. Within 30 seconds only that page is regenerated. The bot merges the new note into the existing page and adds a change-log entry. Refresh the page in the browser to show the update.

### CLI options

| Flag | Description |
| --- | --- |
| `--dry-run` | Print the generated wikitext and don't connect to MediaWiki |
| `--page "Title"` | Process only one page |
| `--force` | Regenerate pages even if their source notes are unchanged |
| `--watch SECONDS` | Keep running and re-check the data file every N seconds |
| `--data PATH` | Use a different raw-data JSON file |
| `-v` | Debug logging |

### Bot configuration (`bot/.env`)

| Variable | Default | Notes |
| --- | --- | --- |
| `GEMINI_API_KEY` | none | Required. `GOOGLE_API_KEY` also works |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Any Gemini model with structured output |
| `GEMINI_THINKING_LEVEL` | `low` | `low`, `medium` or `high` for Gemini 3.x. Leave empty for `gemini-2.5-*` models |
| `WIKI_API_URL` | `http://localhost:8080/api.php` | |
| `WIKI_BOT_USER` | none | For example `Admin@autoupdater` |
| `WIKI_BOT_PASSWORD` | none | The generated bot password |

## Raw data format

Each entry in `bot/data/company_updates.json` looks like this:

```json
{
  "id": "hr-2026-041",
  "source": "slack#people-ops",
  "author": "Dana Whitfield (People Ops)",
  "date": "2026-09-22",
  "target_page": "Remote Work Policy",
  "content": "free-form text, as messy as real life"
}
```

`target_page` sets which wiki page the note goes to. A new title creates a new page. `date` sets the order in which conflicting notes are applied.

## Troubleshooting

- **`api.php did not return JSON`**: the installer hasn't finished, or `LocalSettings.php` isn't mounted yet. Repeat step 3.
- **`Login failed ... WrongPassword`**: use the full `User@botname` login name and the generated bot password, not your normal admin password.
- **`API error permissiondenied`**: the bot password is missing the edit or create grants. Edit it at Special:BotPasswords.
- **Installer can't connect to the database**: use `database` as the host, and wait until `docker compose ps` shows MariaDB as healthy.
- **Reset everything**: `docker compose down -v` deletes the database and uploads. Also delete `LocalSettings.php` and `bot/.autoupdater_state.json`.
