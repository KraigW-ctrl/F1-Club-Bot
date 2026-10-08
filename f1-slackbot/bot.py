"""
F1 Slack bot for #formula1-club.

Two jobs:
  python bot.py race-week   -> On the Monday of a race week, posts
                               "Race Week! Round N - Location" (linked to the
                               round's schedule page) with screenshots of the
                               drivers' and teams' standings.
  python bot.py breaking    -> Posts any formula1.com article whose headline
                               starts with "Breaking".

Both jobs are idempotent: before posting, the bot reads recent channel history
and skips anything it has already posted. Slack itself is the state store, so
nothing needs to be saved between runs.

Useful flags:
  --dry-run            Print the message / save screenshots locally, don't post.
  --force              (race-week) Post the next upcoming round even if today
                       isn't its Monday. Handy for testing.
  --date YYYY-MM-DD    (race-week) Pretend today is this date.

Env vars:
  SLACK_BOT_TOKEN      xoxb-... token (required unless --dry-run)
  SLACK_CHANNEL_ID     ID of #formula1-club, e.g. C0123ABCD (required unless --dry-run)
  F1_SEASON            Defaults to the current year.
  BOT_TIMEZONE         Defaults to America/New_York (decides what "Monday" means).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE = "https://www.formula1.com"
TZ = ZoneInfo(os.getenv("BOT_TIMEZONE", "America/New_York"))
SEASON = int(os.getenv("F1_SEASON", datetime.now(TZ).year))

SCHEDULE_URL = f"{BASE}/en/racing/{SEASON}"
DRIVERS_URL = f"{BASE}/en/results/{SEASON}/drivers"
TEAMS_URL = f"{BASE}/en/results/{SEASON}/team"
NEWS_URLS = [f"{BASE}/en", f"{BASE}/en/latest"]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}


# --------------------------------------------------------------------------- #
# Fetching / parsing
# --------------------------------------------------------------------------- #
def get_soup(url: str) -> BeautifulSoup:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


@dataclass
class Round:
    number: int
    location: str
    url: str
    start: date
    end: date


ROUND_RE = re.compile(r"^ROUND\s+(\d+)$", re.I)
# "09 - 11 Oct" or "30 Oct - 01 Nov"
DATE_RE = re.compile(r"(\d{1,2})(?:\s+([A-Za-z]{3}))?\s*-\s*(\d{1,2})\s+([A-Za-z]{3})")
SKIP_TOKENS = re.compile(r"^(flag of|chequered|formula 1|round\s|testing|next$)", re.I)


def _month(abbr: str) -> int:
    return datetime.strptime(abbr.title(), "%b").month


def parse_dates(text: str, year: int) -> tuple[date, date] | None:
    m = DATE_RE.search(text)
    if not m:
        return None
    d1, m1, d2, m2 = m.groups()
    end_month = _month(m2)
    start_month = _month(m1) if m1 else end_month
    start_year = year - 1 if start_month > end_month else year
    return date(start_year, start_month, int(d1)), date(year, end_month, int(d2))


def fetch_schedule() -> list[Round]:
    soup = get_soup(SCHEDULE_URL)
    rounds: dict[int, Round] = {}
    prefix = f"/en/racing/{SEASON}/"

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if prefix not in href:
            continue
        tokens = [t.strip() for t in a.get_text("|").split("|") if t.strip()]
        rnum = next((int(m.group(1)) for t in tokens if (m := ROUND_RE.match(t))), None)
        if rnum is None:
            continue  # testing sessions, nav links, etc.
        dates = parse_dates(" | ".join(tokens), SEASON)
        if not dates:
            continue

        slug = href.rstrip("/").split("/")[-1]
        location = next(
            (t for t in tokens if not SKIP_TOKENS.match(t) and not DATE_RE.search(t)
             and not re.search(r"\d", t)),
            slug.replace("-", " ").title(),
        )
        url = href if href.startswith("http") else BASE + href
        rounds.setdefault(rnum, Round(rnum, location, url, *dates))

    if not rounds:
        raise RuntimeError(
            f"Couldn't find any rounds on {SCHEDULE_URL} - the page layout may have changed."
        )
    return sorted(rounds.values(), key=lambda r: r.number)


def week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def fetch_breaking_articles() -> list[tuple[str, str]]:
    """Return (headline, url) for articles whose headline starts with 'Breaking'."""
    found: dict[str, str] = {}
    for page in NEWS_URLS:
        try:
            soup = get_soup(page)
        except requests.RequestException as e:
            print(f"warn: couldn't load {page}: {e}", file=sys.stderr)
            continue
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/latest/article/" not in href:
                continue
            headline = a.get_text(" ", strip=True)
            headline = re.sub(r"^unlocked\s+", "", headline, flags=re.I)  # paywall badge
            if re.match(r"^breaking\b", headline, re.I):
                url = href if href.startswith("http") else BASE + href
                found.setdefault(url, headline)
    return [(h, u) for u, h in found.items()]


# --------------------------------------------------------------------------- #
# Screenshots
# --------------------------------------------------------------------------- #
CONSENT_BUTTON = re.compile(r"(reject all|essential only|accept all|accept|agree)", re.I)


def _dismiss_consent(page) -> None:
    """Close the cookie banner (it lives in an iframe) so it doesn't cover the table."""
    for frame in page.frames:
        try:
            btn = frame.get_by_role("button", name=CONSENT_BUTTON).first
            if btn.is_visible(timeout=1500):
                btn.click(timeout=3000)
                page.wait_for_timeout(800)
                break
        except Exception:
            continue
    # Belt and braces: strip any leftover consent overlay.
    page.evaluate(
        """() => {
            document.querySelectorAll('[id^="sp_message_container"], #onetrust-consent-sdk')
              .forEach(el => el.remove());
            document.documentElement.style.overflow = 'auto';
            document.body.style.overflow = 'auto';
        }"""
    )


def screenshot_standings(out_dir: Path) -> list[tuple[Path, str]]:
    from playwright.sync_api import sync_playwright

    targets = [
        (DRIVERS_URL, out_dir / "drivers_standings.png", f"{SEASON} Drivers' Standings"),
        (TEAMS_URL, out_dir / "teams_standings.png", f"{SEASON} Teams' Standings"),
    ]
    shots = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            device_scale_factor=2,
            user_agent=UA,
            locale="en-US",
        )
        page = ctx.new_page()
        for url, path, title in targets:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_selector("table", timeout=30_000)
            page.wait_for_timeout(1500)  # let logos/fonts settle
            _dismiss_consent(page)
            table = page.locator("table").first
            table.scroll_into_view_if_needed()
            try:
                table.screenshot(path=str(path))
            except Exception:
                page.screenshot(path=str(path), full_page=True)
            shots.append((path, title))
        browser.close()
    return shots


# --------------------------------------------------------------------------- #
# Slack
# --------------------------------------------------------------------------- #
def slack_client():
    from slack_sdk import WebClient

    token = os.getenv("SLACK_BOT_TOKEN")
    if not token:
        sys.exit("SLACK_BOT_TOKEN is not set.")
    return WebClient(token=token)


def channel_id() -> str:
    cid = os.getenv("SLACK_CHANNEL_ID")
    if not cid:
        sys.exit("SLACK_CHANNEL_ID is not set (the ID of #formula1-club, e.g. C0123ABCD).")
    return cid


TEST_TAG = "[TEST]"


def recent_channel_text(client, cid: str, limit: int = 200) -> str:
    """Concatenate recent message text so we can check what's already been posted.
    Messages tagged [TEST] are ignored, so test posts never block real ones."""
    resp = client.conversations_history(channel=cid, limit=limit)
    return "\n".join(
        m.get("text", "") for m in resp.get("messages", [])
        if TEST_TAG not in m.get("text", "")
    )


def tag(text: str, test: bool) -> str:
    return f"{TEST_TAG} {text}" if test else text


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
REQUIRED_SCOPES = {"chat:write", "files:write", "channels:history"}


def run_check(post: bool) -> None:
    """Verify token, scopes and channel access. Posts nothing unless --post."""
    from slack_sdk.errors import SlackApiError

    client, cid = slack_client(), channel_id()
    ok = True

    try:
        auth = client.auth_test()
        print(f"[ok] Token valid: bot '{auth['user']}' in workspace '{auth['team']}'")
        scopes = set((auth.headers.get("x-oauth-scopes") or "").replace(" ", "").split(","))
        missing = REQUIRED_SCOPES - scopes
        if missing:
            ok = False
            print(f"[!!] Missing scopes: {', '.join(sorted(missing))} "
                  "-> add them under OAuth & Permissions, then Reinstall to Workspace")
        else:
            print(f"[ok] Scopes present: {', '.join(sorted(REQUIRED_SCOPES))}")
    except SlackApiError as e:
        sys.exit(f"[!!] Token rejected: {e.response['error']}")

    try:
        client.conversations_history(channel=cid, limit=1)
        print(f"[ok] Can read channel {cid}")
    except SlackApiError as e:
        ok = False
        err = e.response["error"]
        hint = {
            "not_in_channel": "invite the bot: /invite @F1 Club Bot in #formula1-club",
            "channel_not_found": "check the channel ID, and invite the bot if the channel is private",
            "missing_scope": "add channels:history (or groups:history if private) and reinstall",
        }.get(err, "")
        print(f"[!!] Can't read channel {cid}: {err}  {('-> ' + hint) if hint else ''}")

    if post and ok:
        client.chat_postMessage(channel=cid, text=f"{TEST_TAG} F1 Club Bot connection test :wave:")
        print("[ok] Test message posted")

    print("\nAll good." if ok else "\nFix the [!!] items above and run again.")
    if not ok:
        sys.exit(1)


def run_race_week(today: date, force: bool, dry_run: bool, test: bool = False) -> None:
    schedule = fetch_schedule()

    if force or test:
        rnd = next((r for r in schedule if r.end >= today), None)
    else:
        # Normally fires on the Monday, but if that run was missed (PC off),
        # any later day up to the start of the race weekend still posts.
        # Channel-history dedupe stops it posting twice.
        rnd = next((r for r in schedule if week_monday(r.start) <= today <= r.start), None)

    if rnd is None:
        print(f"{today}: not a race week. Nothing to post.")
        return

    headline = f"Race Week! Round {rnd.number} - {rnd.location}"
    message = tag(
        f":checkered_flag: *Race Week!* <{rnd.url}|Round {rnd.number} - {rnd.location}>", test
    )
    print(f"Posting: {headline}  ({rnd.url})")

    out_dir = Path("screenshots") if dry_run else Path(tempfile.mkdtemp())
    out_dir.mkdir(exist_ok=True)
    shots = screenshot_standings(out_dir)

    if dry_run:
        print("DRY RUN message:", message)
        for path, title in shots:
            print(f"DRY RUN screenshot: {title} -> {path.resolve()}")
        return

    client, cid = slack_client(), channel_id()
    if not (force or test) and f"Round {rnd.number} - " in recent_channel_text(client, cid):
        print("Already posted this round - skipping.")
        return

    client.files_upload_v2(
        channel=cid,
        initial_comment=message,
        file_uploads=[{"file": str(p), "title": t} for p, t in shots],
    )
    print("Posted.")


def fetch_latest_article() -> tuple[str, str] | None:
    """Most recent article on the homepage, used to test the breaking-news post format."""
    soup = get_soup(NEWS_URLS[0])
    for a in soup.find_all("a", href=True):
        if "/latest/article/" in a["href"]:
            headline = re.sub(r"^unlocked\s+", "", a.get_text(" ", strip=True), flags=re.I)
            href = a["href"]
            return headline, href if href.startswith("http") else BASE + href
    return None


def run_breaking(dry_run: bool, test: bool = False) -> None:
    articles = fetch_breaking_articles()
    print(f"Found {len(articles)} 'Breaking' article(s).")

    if test and not articles:
        latest = fetch_latest_article()
        if latest:
            print("No breaking news right now; using the latest article to test the format.")
            articles = [latest]

    if not articles:
        return

    if dry_run:
        for headline, url in articles:
            print(f"DRY RUN: {tag(f':rotating_light: <{url}|{headline}>', test)}")
        return

    client, cid = slack_client(), channel_id()
    history = "" if test else recent_channel_text(client, cid)
    for headline, url in articles[:1] if test else articles:
        if url in history:
            continue
        client.chat_postMessage(
            channel=cid,
            text=tag(f":rotating_light: <{url}|{headline}>", test),
            unfurl_links=True,
        )
        print(f"Posted: {headline}")


def main() -> None:
    ap = argparse.ArgumentParser(description="F1 bot for #formula1-club")
    ap.add_argument("job", choices=["check", "race-week", "breaking"])
    ap.add_argument("--dry-run", action="store_true", help="Don't touch Slack")
    ap.add_argument("--force", action="store_true", help="race-week: post next round now")
    ap.add_argument("--test", action="store_true",
                    help="Post a [TEST]-tagged message that won't block real posts later")
    ap.add_argument("--post", action="store_true", help="check: also post a test message")
    ap.add_argument("--date", help="Pretend today is YYYY-MM-DD (race-week only)")
    args = ap.parse_args()

    if args.job == "check":
        run_check(args.post)
    elif args.job == "race-week":
        today = date.fromisoformat(args.date) if args.date else datetime.now(TZ).date()
        run_race_week(today, args.force, args.dry_run, args.test)
    else:
        run_breaking(args.dry_run, args.test)


if __name__ == "__main__":
    main()
