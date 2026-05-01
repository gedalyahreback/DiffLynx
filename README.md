<p align="center">
  <img src="logo.png" alt="DiffLynx logo" width="200"/>
</p>

# DiffLynx

Monitors all sub-pages under multiple docs sites for content changes and sends alerts via Gmail and Slack.

## Setup

**1. Install dependencies**

```bash
cd /path/to/DiffLynx
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**2. Configure credentials**

```bash
cp .env.example .env
```

Fill in `.env` with your values:

- `GMAIL_APP_PASSWORD` — a 16-character App Password from https://myaccount.google.com/apppasswords (requires 2-Step Verification on the sender account).
- `SLACK_BOT_TOKEN` — a Bot Token (`xoxb-...`) from your Slack app with `chat:write` scope, with the bot invited to your channel.
- `WATCH_URLS` — comma-separated list of docs base URLs to crawl. All linked sub-pages under each URL are discovered automatically.
- `ALERT_MODE` — `per-site` (default) or `digest`. See Alert Modes below.

## Configuring sites to monitor

Set `WATCH_URLS` in `.env` as a comma-separated list of base URLs:

```
WATCH_URLS=https://docs.example.com/,https://another-site.com/docs/
```

All linked sub-pages under each base URL are discovered and monitored automatically.

Alternatively, create a `personal_config.py` file in the project root (it is gitignored) to set your defaults in Python:

```python
WATCH_URLS = [
    "https://docs.example.com/",
    "https://another-site.com/docs/",
]
```

## Alert modes

`per-site` sends a separate Gmail email and Slack message for each site that has changes. `digest` sends one Gmail email and one Slack message listing all changed sites together.

Mode is resolved in this priority order (highest wins):

1. CLI `--mode` flag
2. `config.json` (written by the Slack slash command server)
3. `ALERT_MODE` in `.env`
4. Default: `per-site`

## Running

**First run — build the baseline snapshot (no alert sent):**

```bash
source .venv/bin/activate
python3 watcher.py
```

**Subsequent runs — detect changes and send alerts:**

```bash
python3 watcher.py
```

**Override alert mode for a single run:**

```bash
python3 watcher.py --mode digest
python3 watcher.py --mode per-site
```

**Dry run — crawl all sites and print changes without sending alerts:**

```bash
python3 watcher.py --dry-run
```

You can combine `--dry-run` with `--mode`:

```bash
python3 watcher.py --dry-run --mode digest
```

## Snapshot migration

If you are upgrading from the single-site version, the old `snapshot.json` (flat `{url: hash}` format) is automatically migrated to the new nested format (`{site_base: {url: hash}}`) on the first run.

## Scheduling with cron (daily at 8 AM)

Run `crontab -e` and add (adjusting the path to your installation):

```
0 8 * * * cd /path/to/DiffLynx && /path/to/DiffLynx/.venv/bin/python3 watcher.py >> /path/to/DiffLynx/watcher.log 2>&1
```

## Slack slash command server

The watcher includes an optional lightweight HTTP server (powered by Flask) that lets you change the alert mode at runtime via a Slack slash command without editing `.env`.

**Start the server:**

```bash
python3 watcher.py --slack-server
```

The server listens on port 3000 for `POST /slack/command`. Configure your Slack app's slash command (e.g., `/docswatcher`) to point to `https://your-host:3000/slack/command`.

**Supported commands:**

```
/docswatcher mode digest
/docswatcher mode per-site
```

The selected mode is written to `config.json` in the project directory and takes effect on the next watcher run (unless overridden by the `--mode` CLI flag).

## Mintlify formatting-error detection

Sites listed in `MINTLIFY_URLS` (or in `personal_config.py` as `MINTLIFY_SITES`) are scanned on every crawl for Markdown save-corruption artifacts that Mintlify sometimes introduces — without waiting for a content-hash change to occur.

Three patterns are detected:

`Unrendered bold/italic` — raw `**text**` or `*text*` appearing in visible page content instead of rendered HTML, indicating Mintlify failed to process the Markdown.

`Stray backslash` — a lone `` or `` immediately after a heading or at the end of a paragraph, a common artifact of Mintlify's autosave glitch.

`Raw escape sequence` — literal `
`, `	`, ``, or `` characters visible in body text (escaped source content leaking into the rendered page).

When any of these are found, a separate alert is fired immediately — independent of whether the page hash changed — listing the affected URLs and a short snippet showing the matched text.

To enable Mintlify checks for a site, add it to `MINTLIFY_URLS` in `.env`:

```
MINTLIFY_URLS=https://docs.example.com/,https://docs.anothersite.com/
```

Or add it to `MINTLIFY_SITES` in `personal_config.py`:

```python
MINTLIFY_SITES = [
    "https://docs.example.com/",
]
```

## How it works

On each run the script crawls every page linked under each configured base URL, computes a SHA-256 hash of each page's HTML, and compares against `snapshot.json`. When pages are added, changed, or removed, it sends an alert listing the affected URLs to Gmail and Slack, then updates the snapshot.
