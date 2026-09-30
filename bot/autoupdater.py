#!/usr/bin/env python3
"""
Auto-Updating Company Wiki - AI synchronization bot.

Pipeline (one "sync pass"):

    raw company data (JSON)          e.g. Slack posts, emails, release-note drafts
            |
            |  1. load + group entries by their `target_page`
            v
    per-page bundle of raw notes
            |
            |  2. fetch the page's CURRENT wikitext from MediaWiki (if it exists)
            v
    Gemini (google-genai SDK)        3. merge new notes into the existing page and
            |                           return clean wikitext as structured JSON
            v
    MediaWiki Action API             4. create or update the page as the bot user

A small state file remembers a fingerprint of the raw notes behind each page, so
re-running the bot (or running it in --watch mode) only calls Gemini and edits
the wiki when the source data for that page actually changed.

Usage:
    python autoupdater.py --dry-run            # generate wikitext, print it, touch nothing
    python autoupdater.py                      # sync all pages to the wiki
    python autoupdater.py --page "Remote Work Policy" --force
    python autoupdater.py --watch 60           # re-check the data file every 60 seconds

Configuration is read from environment variables (or bot/.env, see .env.example).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field

# -----------------------------------------------------------------------------
# Paths and constants
# -----------------------------------------------------------------------------

BOT_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_FILE = BOT_DIR / "data" / "company_updates.json"
DEFAULT_STATE_FILE = BOT_DIR / ".autoupdater_state.json"

# Every page the bot writes is tagged with this category, so humans can find
# (and audit) all AI-maintained pages at Category:Auto-updated_by_AI.
BOT_CATEGORY = "Auto-updated by AI"

# Everything after this marker is owned by the bot and regenerated on every
# edit. It is stripped before existing content is sent back to Gemini so the
# footer never gets duplicated or "rewritten" by the model.
FOOTER_MARKER = "<!-- autoupdater:footer -->"

USER_AGENT = "CompanyWikiAutoUpdater/1.0 (hackathon demo; python-requests)"

log = logging.getLogger("autoupdater")


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    """Runtime settings, sourced from environment variables."""

    gemini_api_key: str
    gemini_model: str
    # "low" / "medium" / "high" for Gemini 3.x models. Empty string = let the
    # model use its default (needed for older models such as gemini-2.5-*,
    # which use thinking budgets instead of thinking levels).
    gemini_thinking_level: str
    wiki_api_url: str
    wiki_bot_user: str
    wiki_bot_password: str

    @classmethod
    def from_env(cls, require_wiki: bool) -> "Config":
        # bot/.env takes effect only for variables not already set in the shell.
        load_dotenv(BOT_DIR / ".env")

        cfg = cls(
            gemini_api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", ""),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
            gemini_thinking_level=os.getenv("GEMINI_THINKING_LEVEL", "low").strip(),
            wiki_api_url=os.getenv("WIKI_API_URL", "http://localhost:8080/api.php"),
            wiki_bot_user=os.getenv("WIKI_BOT_USER", ""),
            wiki_bot_password=os.getenv("WIKI_BOT_PASSWORD", ""),
        )

        missing = []
        if not cfg.gemini_api_key:
            missing.append("GEMINI_API_KEY")
        if require_wiki:
            if not cfg.wiki_bot_user:
                missing.append("WIKI_BOT_USER")
            if not cfg.wiki_bot_password:
                missing.append("WIKI_BOT_PASSWORD")
        if missing:
            raise SystemExit(
                f"Missing required configuration: {', '.join(missing)}. "
                "Set them in your shell or in bot/.env (see bot/.env.example)."
            )
        return cfg


# -----------------------------------------------------------------------------
# 1. Raw company data
# -----------------------------------------------------------------------------

def load_raw_updates(path: Path) -> tuple[str, dict[str, list[dict[str, Any]]]]:
    """
    Read the raw data export and group entries by the wiki page they belong to.

    Expected shape (see data/company_updates.json):
        {
          "company": "Clankers Inc.",
          "updates": [
            {"id": "...", "source": "...", "author": "...", "date": "YYYY-MM-DD",
             "target_page": "Remote Work Policy", "content": "free-form text"}
          ]
        }

    Returns (company_name, {page_title: [entries sorted oldest -> newest]}).
    Sorting by date matters: later notes (e.g. corrections) must win.
    """
    with path.open(encoding="utf-8") as fh:
        payload = json.load(fh)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in payload.get("updates", []):
        page = (entry.get("target_page") or "").strip()
        if not page or not (entry.get("content") or "").strip():
            log.warning("Skipping entry %s: needs both target_page and content", entry.get("id"))
            continue
        grouped[page].append(entry)

    for entries in grouped.values():
        entries.sort(key=lambda e: e.get("date", ""))

    return payload.get("company", "the company"), dict(grouped)


def fingerprint(entries: list[dict[str, Any]]) -> str:
    """Stable hash of a page's raw notes, used to skip unchanged pages."""
    canonical = json.dumps(entries, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_state(path: Path) -> dict[str, str]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_state(path: Path, state: dict[str, str]) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


# -----------------------------------------------------------------------------
# 2-3. Gemini: raw notes (+ existing page) -> clean MediaWiki wikitext
# -----------------------------------------------------------------------------

class WikiPageDraft(BaseModel):
    """Structured output schema Gemini must return for each page."""

    edit_summary: str = Field(
        description="One-line MediaWiki edit summary (max ~120 chars) describing what changed."
    )
    wikitext: str = Field(
        description="The complete page body in MediaWiki wikitext. No Markdown, no code fences."
    )


SYSTEM_INSTRUCTION = """\
You are the technical writer bot for an internal company wiki running MediaWiki.

You receive (a) the current wikitext of a page, which may be empty for new pages,
and (b) a batch of raw, informal notes from Slack, email and drafts.

Produce the COMPLETE updated page:
- Write valid MediaWiki wikitext only. Never use Markdown (no #, **, ``` or [text](url)).
  Use == Section == headings, '''bold''', ''italic'', * bullets, # numbered lists,
  {| class="wikitable" ... |} tables and <code>...</code> for commands.
- Start with a short lead paragraph summarising the page. Do not repeat the page title as a heading.
- Merge new information into the existing structure. Keep still-valid existing content,
  update anything the notes supersede, and remove statements that are now wrong.
- When notes conflict, the note with the later date wins (corrections override originals).
- Include effective dates and owners/contacts where the notes provide them.
- Add an "== Change log ==" section (newest first) with one bullet per dated change.
- Be neutral and professional: drop chit-chat, emoji and Slack slang.
- Never invent facts, numbers, dates, links or people that are not in the input.
- Do not add categories or a bot footer; those are appended automatically.
"""


def build_prompt(
    company: str,
    title: str,
    entries: list[dict[str, Any]],
    existing_wikitext: str | None,
) -> str:
    """Assemble the per-page user prompt sent to Gemini."""
    notes = "\n\n".join(
        f"[{e.get('date', 'undated')}] {e.get('source', 'unknown source')} - "
        f"{e.get('author', 'unknown author')} (ref {e.get('id', 'n/a')}):\n{e['content'].strip()}"
        for e in entries
    )
    current = existing_wikitext.strip() if existing_wikitext else "(page does not exist yet)"
    return (
        f"Company: {company}\n"
        f"Wiki page title: {title}\n"
        f"Today's date: {datetime.now(timezone.utc):%Y-%m-%d}\n\n"
        f"=== CURRENT PAGE WIKITEXT ===\n{current}\n\n"
        f"=== RAW NOTES (oldest first) ===\n{notes}\n"
    )


def synthesize_page(
    client: genai.Client,
    cfg: Config,
    company: str,
    title: str,
    entries: list[dict[str, Any]],
    existing_wikitext: str | None,
) -> WikiPageDraft:
    """Ask Gemini to turn raw notes into a finished wiki page."""
    config_kwargs: dict[str, Any] = {
        "system_instruction": SYSTEM_INSTRUCTION,
        # Structured output: the SDK sends the Pydantic schema to the API and
        # parses the JSON reply back into a WikiPageDraft (response.parsed).
        "response_mime_type": "application/json",
        "response_schema": WikiPageDraft,
        # No tools are used, so skip the SDK's automatic function calling loop.
        "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
    }
    if cfg.gemini_thinking_level:
        config_kwargs["thinking_config"] = types.ThinkingConfig(
            thinking_level=cfg.gemini_thinking_level.upper()
        )

    response = client.models.generate_content(
        model=cfg.gemini_model,
        contents=build_prompt(company, title, entries, existing_wikitext),
        config=types.GenerateContentConfig(**config_kwargs),
    )

    draft = response.parsed
    if not isinstance(draft, WikiPageDraft):
        # Fallback if the SDK could not auto-parse (e.g. truncated output).
        draft = WikiPageDraft.model_validate_json(response.text or "")
    return draft


def strip_bot_footer(wikitext: str) -> str:
    """Remove the bot-owned footer (and anything after it) from a page."""
    return wikitext.split(FOOTER_MARKER, 1)[0].rstrip()


def finalize_wikitext(wikitext: str, entries: list[dict[str, Any]]) -> str:
    """
    Clean up model output and append the bot footer + category.

    The footer is generated in code, not by the model, so it is always accurate
    and always parseable by strip_bot_footer() on the next run.
    """
    body = wikitext.strip()
    # Defensive: remove Markdown code fences if the model wrapped its answer.
    body = re.sub(r"^```[a-zA-Z]*\s*\n|\n```\s*$", "", body).strip()
    # Categories are managed by the bot; drop any the model added anyway.
    body = re.sub(r"\[\[Category:[^\]]*\]\]\s*", "", body).rstrip()
    body = strip_bot_footer(body)

    sources = ", ".join(sorted({e.get("id", "?") for e in entries}))
    synced = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    footer = (
        f"\n\n{FOOTER_MARKER}\n"
        "----\n"
        f"''This page is maintained automatically by the AutoUpdater bot. "
        f"Last synced: {synced}. Source records: {sources}.''\n\n"
        f"[[Category:{BOT_CATEGORY}]]\n"
    )
    return body + footer


# -----------------------------------------------------------------------------
# 4. MediaWiki Action API client
# -----------------------------------------------------------------------------

class MediaWikiError(RuntimeError):
    """Raised when the MediaWiki API reports an error or is unreachable."""


class MediaWikiClient:
    """
    Minimal client for the MediaWiki Action API (api.php).

    Authentication uses a *bot password* (Special:BotPasswords), which is the
    recommended way for scripts to log in. The username has the form
    "<WikiUser>@<BotName>", e.g. "Admin@autoupdater".

    Flow:
        login()  -> fetch login token, POST action=login, fetch CSRF token
        get_page_wikitext(title)            -> current content or None
        edit_page(title, wikitext, summary) -> creates or updates the page
    """

    def __init__(self, api_url: str, username: str, password: str, timeout: float = 30.0):
        self.api_url = api_url
        self.username = username
        self.password = password
        self.timeout = timeout
        self.csrf_token: str | None = None
        # A Session keeps the login cookies between requests.
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    # -- low-level helpers ----------------------------------------------------

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        params = {**params, "format": "json", "formatversion": "2"}
        try:
            if method == "GET":
                resp = self.session.get(self.api_url, params=params, timeout=self.timeout)
            else:
                resp = self.session.post(self.api_url, data=params, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise MediaWikiError(
                f"Could not reach {self.api_url}: {exc}. Is `docker compose up -d` running?"
            ) from exc

        try:
            data = resp.json()
        except ValueError as exc:
            # Before installation finishes, api.php serves an HTML installer page.
            raise MediaWikiError(
                f"{self.api_url} did not return JSON. Finish the web installer and mount "
                "LocalSettings.php (see README) before running the bot."
            ) from exc

        if "error" in data:
            err = data["error"]
            raise MediaWikiError(f"API error {err.get('code')}: {err.get('info')}")
        return data

    def _get(self, **params: Any) -> dict[str, Any]:
        return self._request("GET", params)

    def _post(self, **params: Any) -> dict[str, Any]:
        return self._request("POST", params)

    # -- public API ------------------------------------------------------------

    def login(self) -> None:
        """Log in with a bot password and cache a CSRF (edit) token."""
        login_token = self._get(action="query", meta="tokens", type="login")[
            "query"]["tokens"]["logintoken"]

        result = self._post(
            action="login",
            lgname=self.username,
            lgpassword=self.password,
            lgtoken=login_token,
        )["login"]
        if result.get("result") != "Success":
            raise MediaWikiError(
                f"Login failed for {self.username!r}: {str(result.get('reason', result)).rstrip('.')}. "
                "Create a bot password at Special:BotPasswords (see README)."
            )

        self.csrf_token = self._get(action="query", meta="tokens")["query"]["tokens"]["csrftoken"]
        log.info("Logged in to %s as %s", self.api_url, result.get("lgusername", self.username))

    def get_page_wikitext(self, title: str) -> str | None:
        """Return the latest wikitext of `title`, or None if the page does not exist."""
        data = self._get(
            action="query",
            prop="revisions",
            titles=title,
            rvprop="content",
            rvslots="main",
        )
        page = data["query"]["pages"][0]
        if page.get("missing") or "revisions" not in page:
            return None
        return page["revisions"][0]["slots"]["main"]["content"]

    def edit_page(self, title: str, wikitext: str, summary: str) -> dict[str, Any]:
        """
        Create or overwrite `title` with `wikitext`.

        Returns the API's `edit` object, which includes `new: true` for newly
        created pages and `nochange: true` if the content was identical.
        """
        if not self.csrf_token:
            raise MediaWikiError("Not logged in; call login() first.")
        result = self._post(
            action="edit",
            title=title,
            text=wikitext,
            summary=summary,
            bot="1",  # Flags the edit as a bot edit if the account has the 'bot' right.
            token=self.csrf_token,
        )["edit"]
        if result.get("result") != "Success":
            raise MediaWikiError(f"Edit of {title!r} failed: {result}")
        return result


# -----------------------------------------------------------------------------
# Orchestration
# -----------------------------------------------------------------------------

def run_once(
    args: argparse.Namespace,
    cfg: Config,
    gemini: genai.Client,
    wiki: MediaWikiClient | None,
) -> int:
    """Run a single sync pass. Returns the number of pages generated/updated."""
    company, pages = load_raw_updates(args.data)
    if args.page:
        pages = {t: e for t, e in pages.items() if t == args.page}
        if not pages:
            log.error("No raw entries target page %r in %s", args.page, args.data)
            return 0

    state = load_state(args.state_file)
    changed = 0

    for title, entries in sorted(pages.items()):
        digest = fingerprint(entries)
        if not args.force and not args.dry_run and state.get(title) == digest:
            log.info("[skip] %s - source notes unchanged since last sync", title)
            continue

        existing = wiki.get_page_wikitext(title) if wiki else None
        log.info(
            "[gemini] %s - %d note(s), %s",
            title, len(entries), "updating existing page" if existing else "new page",
        )

        draft = synthesize_page(
            gemini, cfg, company, title, entries,
            strip_bot_footer(existing) if existing else None,
        )
        wikitext = finalize_wikitext(draft.wikitext, entries)
        summary = f"AutoUpdater: {draft.edit_summary.strip()}"[:250]

        if args.dry_run or wiki is None:
            print(f"\n{'=' * 78}\n{title}\n  edit summary: {summary}\n{'=' * 78}")
            print(wikitext)
            changed += 1
            continue

        result = wiki.edit_page(title, wikitext, summary)
        if result.get("nochange"):
            log.info("[wiki] %s - no content change", title)
        else:
            action = "created" if result.get("new") else "updated"
            log.info("[wiki] %s - %s (rev %s)", title, action, result.get("newrevid"))
            changed += 1

        state[title] = digest
        save_state(args.state_file, state)

    return changed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synthesize raw company updates into MediaWiki pages using Gemini."
    )
    parser.add_argument(
        "--data", type=Path, default=DEFAULT_DATA_FILE,
        help=f"Raw updates JSON file (default: {DEFAULT_DATA_FILE.relative_to(BOT_DIR.parent)})",
    )
    parser.add_argument(
        "--state-file", type=Path, default=DEFAULT_STATE_FILE,
        help="Where to store per-page fingerprints of already-synced notes.",
    )
    parser.add_argument("--page", help="Only process this page title.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Generate and print wikitext without connecting to MediaWiki.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Regenerate pages even if their source notes have not changed.",
    )
    parser.add_argument(
        "--watch", type=int, metavar="SECONDS", default=0,
        help="Keep running and re-check the data file every SECONDS seconds.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # The SDK and urllib3 are chatty at INFO/DEBUG; keep the demo output readable.
    for noisy in ("httpx", "urllib3", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg = Config.from_env(require_wiki=not args.dry_run)
    gemini = genai.Client(api_key=cfg.gemini_api_key)
    log.info("Using Gemini model %s", cfg.gemini_model)

    wiki: MediaWikiClient | None = None
    if not args.dry_run:
        wiki = MediaWikiClient(cfg.wiki_api_url, cfg.wiki_bot_user, cfg.wiki_bot_password)
        try:
            wiki.login()
        except MediaWikiError as exc:
            log.error("%s", exc)
            return 1

    while True:
        try:
            changed = run_once(args, cfg, gemini, wiki)
            log.info("Sync pass complete: %d page(s) %s", changed,
                     "generated" if args.dry_run else "written")
        except MediaWikiError as exc:
            log.error("%s", exc)
            if not args.watch:
                return 1
            # Sessions/CSRF tokens can expire during long watch runs; refresh them.
            try:
                if wiki:
                    wiki.login()
            except MediaWikiError as login_exc:
                log.error("Re-login failed: %s", login_exc)
        except genai_errors.APIError as exc:
            log.error("Gemini API error (%s): %s", exc.code, exc.message)
            if not args.watch:
                return 1

        if not args.watch:
            return 0
        log.info("Watching %s - next check in %ds (Ctrl+C to stop)", args.data, args.watch)
        try:
            time.sleep(args.watch)
        except KeyboardInterrupt:
            log.info("Stopped.")
            return 0


if __name__ == "__main__":
    sys.exit(main())
