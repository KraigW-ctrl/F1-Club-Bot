# F1 Club Bot

Posts to **#formula1-club**:

1. **Race week (Mondays):** `Race Week! Round 17 - Singapore`, with the round linked to its formula1.com schedule page, plus screenshots of the drivers' and teams' standings.
2. **Breaking news (every 15 min):** any formula1.com article whose headline starts with "Breaking".

The bot checks recent channel history before posting, so it never double-posts and needs no database or state file.

## 1. Create the Slack app

1. Go to https://api.slack.com/apps → **Create New App** → **From a manifest**, pick your workspace, and paste `slack-app-manifest.yml`.
2. **Install to Workspace**, then copy the **Bot User OAuth Token** (`xoxb-...`).
3. In Slack, open #formula1-club and type `/invite @F1 Club Bot`.
4. Get the channel ID: click the channel name → the ID (`C0...`) is at the bottom of the About tab.

## 2. Test locally (Windows / PowerShell)

```powershell
py -m pip install -r requirements.txt
py -m playwright install chromium

# Dry runs: print the message and save screenshots to .\screenshots, no posting
py bot.py race-week --dry-run --force
py bot.py breaking --dry-run

# Real post to Slack
$env:SLACK_BOT_TOKEN = "xoxb-..."
$env:SLACK_CHANNEL_ID = "C0123ABCD"
py bot.py race-week --force
```

`--date 2026-10-19` lets you simulate a specific Monday.

## 3. Run it on a schedule (GitHub Actions)

1. Push this folder to a GitHub repo.
2. **Settings → Secrets and variables → Actions**: add `SLACK_BOT_TOKEN` and `SLACK_CHANNEL_ID`.
3. Done. `race-week.yml` runs every Monday at 13:00 UTC; `breaking-news.yml` runs every 15 minutes. Both can be triggered by hand from the Actions tab.

**Minutes budget:** in a *public* repo Actions minutes are free. In a *private* repo on the free plan (2,000 min/month), a 15-minute breaking-news check uses roughly 2,900 min/month, so change its cron to `*/30 * * * *` or host the bot elsewhere (e.g. Windows Task Scheduler on an always-on machine running `py bot.py breaking`).

## Notes

- "Monday" is evaluated in `America/New_York` (override with `BOT_TIMEZONE`). The bot posts on the Monday of the week the race weekend starts, so a Thursday–Saturday weekend like Las Vegas still gets its Monday post.
- The season defaults to the current year, so it rolls over to 2027 automatically on Jan 1.
- All the data is scraped from formula1.com. If F1 redesigns those pages, the bot will fail loudly (red X in Actions) rather than post something wrong; the selectors live at the top of `fetch_schedule()` and `fetch_breaking_articles()`.
