#!/usr/bin/env python3
"""
DiffLynx
Crawls all sub-pages under each configured docs site, detects content changes,
and sends alerts via Gmail (SMTP) and Slack (Bot Token).

Alert modes:
  per-site -- one Gmail + one Slack message per changed site.
  digest   -- one Gmail + one Slack message listing all changed sites.

Mode priority (highest wins): CLI --mode flag > config.json > .env ALERT_MODE.

Mintlify formatting-error detection:
  Sites listed in MINTLIFY_SITES (or MINTLIFY_URLS in .env) are additionally
  scanned on every crawl for Markdown save-corruption patterns such as
  unrendered **bold**, stray backslashes after section headings, and raw
  escape sequences. Alerts are sent immediately when errors are found,
  independent of content-hash changes.
"""

import argparse
import hashlib
import json
import os
import re
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_FILE = os.path.join(BASE_DIR, "snapshot.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
SCRIPT_PATH = os.path.join(BASE_DIR, "watcher.py")

# ---------------------------------------------------------------------------
# .env loading
# ---------------------------------------------------------------------------
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Default lists are intentionally empty — configure your sites via WATCH_URLS
# in .env, or populate personal_config.py (gitignored) for local defaults.
DEFAULT_WATCH_URLS: List[str] = []

# Sites to additionally scan for Mintlify Markdown save-corruption artifacts.
# Can be extended via the MINTLIFY_URLS env var (comma-separated), or via
# personal_config.py (gitignored).
DEFAULT_MINTLIFY_SITES: List[str] = []

# Load personal overrides if present (file is gitignored).
try:
    import personal_config as _pc  # type: ignore
    DEFAULT_WATCH_URLS = list(getattr(_pc, "WATCH_URLS", DEFAULT_WATCH_URLS))
    DEFAULT_MINTLIFY_SITES = list(getattr(_pc, "MINTLIFY_SITES", DEFAULT_MINTLIFY_SITES))
except ImportError:
    pass

# Each tuple is (label, compiled_regex).  A match anywhere in the visible text
# of a page signals a Mintlify formatting error.
#
# Pattern rationale:
#   UNRENDERED_BOLD   -- Mintlify failed to render **text** → shows raw asterisks.
#   TRAILING_BACKSLASH -- A lone \ or \\ appears immediately after a heading or
#                         at the end of a paragraph (common Mintlify save glitch).
#   RAW_ESCAPE_SEQ    -- Sequences like \n, \t, \r appearing as literal visible
#                         characters in body text (escaped content leaked to HTML).
MINTLIFY_ERROR_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    (
        "Unrendered bold/italic Markdown (e.g. **text** or *text*)",
        re.compile(r"\*{1,3}[^\s*][^*]*\*{1,3}", re.MULTILINE),
    ),
    (
        "Stray backslash after heading or at paragraph end",
        re.compile(r"(?m)(?:^#{1,6}\s.+|[.!?])\s*\\{1,2}\s*$"),
    ),
    (
        "Raw escape sequence in visible text (\\n, \\t, \\r, \\\\)",
        re.compile(r"\\[ntr\\]"),
    ),
]

VALID_MODES = {"per-site", "digest"}
EMAIL_FOOTER = (
    "\n\nTo change alert mode, run: "
    "python3 " + SCRIPT_PATH + " --mode [digest|per-site]"
)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config_json() -> Dict[str, str]:
    """Load persistent config from config.json (created by Slack server)."""
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_config_json(data: Dict[str, str]) -> None:
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)


def resolve_mode(cli_mode: Optional[str]) -> str:
    """
    Determine the effective alert mode.

    Priority (highest first):
      1. CLI --mode flag
      2. config.json
      3. .env ALERT_MODE
      4. Default: 'per-site'
    """
    if cli_mode is not None:
        if cli_mode not in VALID_MODES:
            print(
                "[ERROR] Invalid --mode '{}'. Choose: {}".format(cli_mode, VALID_MODES),
                file=sys.stderr,
            )
            sys.exit(1)
        return cli_mode

    config = load_config_json()
    if config.get("alert_mode") in VALID_MODES:
        return config["alert_mode"]

    env_mode = os.environ.get("ALERT_MODE", "per-site").strip()
    if env_mode in VALID_MODES:
        return env_mode

    return "per-site"


def get_watch_urls() -> List[str]:
    raw = os.environ.get("WATCH_URLS", "").strip()
    if raw:
        return [u.strip() for u in raw.split(",") if u.strip()]
    return DEFAULT_WATCH_URLS


def get_mintlify_sites() -> "set[str]":
    """Return the set of base URLs that should receive Mintlify formatting checks."""
    raw = os.environ.get("MINTLIFY_URLS", "").strip()
    extra = {u.strip() for u in raw.split(",") if u.strip()} if raw else set()
    return set(DEFAULT_MINTLIFY_SITES) | extra


# ---------------------------------------------------------------------------
# Credentials (loaded lazily so --dry-run can be used without real creds)
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print("[ERROR] Environment variable {} is not set.".format(name), file=sys.stderr)
        sys.exit(1)
    return val


# ---------------------------------------------------------------------------
# Mintlify formatting-error detection
# ---------------------------------------------------------------------------

def check_mintlify_formatting_errors(
    html: str, url: str
) -> List[Tuple[str, str]]:
    """
    Scan the visible text of *html* for Mintlify Markdown save-corruption
    artifacts.  Returns a list of (error_label, matched_snippet) tuples; an
    empty list means the page looks clean.

    Only the visible text content is inspected (script/style tags are stripped)
    so CSS class names or HTML attributes cannot trigger false positives.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Remove non-visible elements before extracting text.
    for tag in soup(["script", "style", "head", "meta", "noscript"]):
        tag.decompose()

    visible_text = soup.get_text(separator="\n")
    errors: List[Tuple[str, str]] = []

    for label, pattern in MINTLIFY_ERROR_PATTERNS:
        match = pattern.search(visible_text)
        if match:
            # Capture a short snippet (up to 120 chars) around the match.
            start = max(0, match.start() - 30)
            end = min(len(visible_text), match.end() + 30)
            snippet = visible_text[start:end].strip().replace("\n", " ")
            errors.append((label, snippet[:120]))

    return errors


# ---------------------------------------------------------------------------
# Crawling
# ---------------------------------------------------------------------------

def fetch_page(url: str) -> Optional[str]:
    """Fetch a URL and return its text content, or None on failure."""
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        print("[WARN] Could not fetch {}: {}".format(url, e), file=sys.stderr)
        return None


def extract_doc_links(html: str, base_url: str, site_base: str) -> "set":
    """Return all internal links that stay under site_base."""
    soup = BeautifulSoup(html, "html.parser")
    links: "set" = set()
    parsed_base = urlparse(site_base)
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        if (
            parsed.netloc == parsed_base.netloc
            and parsed.path.startswith(parsed_base.path)
        ):
            links.add(parsed._replace(fragment="").geturl())
    return links


def crawl_site(
    site_base: str, mintlify_check: bool = False
) -> Tuple[Dict[str, str], Dict[str, List[Tuple[str, str]]]]:
    """
    Crawl all pages under site_base.

    Returns:
        hashes   -- {url: sha256_hash}
        fmt_errors -- {url: [(error_label, snippet), ...]}
                      Only populated when mintlify_check is True and errors
                      are found; otherwise an empty dict.
    """
    visited: Dict[str, str] = {}
    fmt_errors: Dict[str, List[Tuple[str, str]]] = {}
    queue: List[str] = [site_base]

    while queue:
        url = queue.pop(0)
        if url in visited:
            continue
        html = fetch_page(url)
        if html is None:
            continue
        content_hash = hashlib.sha256(html.encode()).hexdigest()
        visited[url] = content_hash

        if mintlify_check:
            page_errors = check_mintlify_formatting_errors(html, url)
            if page_errors:
                fmt_errors[url] = page_errors

        for link in extract_doc_links(html, url, site_base):
            if link not in visited:
                queue.append(link)

    return visited, fmt_errors


# ---------------------------------------------------------------------------
# Snapshot (keyed by site_base -> {page_url: hash})
# ---------------------------------------------------------------------------

def load_snapshot() -> Dict[str, Dict[str, str]]:
    """
    Load snapshot.json.

    Migrates the legacy flat format {page_url: hash} to the new nested format
    {site_base: {page_url: hash}} on first run after upgrade.
    """
    if not os.path.exists(SNAPSHOT_FILE):
        return {}
    with open(SNAPSHOT_FILE) as f:
        data = json.load(f)

    # Migration: if any top-level value is a string (not a dict) it's the old format.
    if data and isinstance(next(iter(data.values())), str):
        print("[INFO] Migrating legacy snapshot.json to multi-site format.")
        legacy_key = DEFAULT_WATCH_URLS[0] if DEFAULT_WATCH_URLS else "unknown-site"
        migrated: Dict[str, Dict[str, str]] = {legacy_key: data}
        save_snapshot(migrated)
        return migrated

    return data


def save_snapshot(snapshot: Dict[str, Dict[str, str]]) -> None:
    with open(SNAPSHOT_FILE, "w") as f:
        json.dump(snapshot, f, indent=2)


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------

def diff_snapshots(
    old: Dict[str, str], new: Dict[str, str]
) -> Tuple[List[str], List[str], List[str]]:
    """Return (new_pages, changed_pages, removed_pages) for a single site."""
    new_pages = [url for url in new if url not in old]
    changed_pages = [url for url in new if url in old and old[url] != new[url]]
    removed_pages = [url for url in old if url not in new]
    return new_pages, changed_pages, removed_pages


# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------

def build_site_message(
    site: str,
    new_pages: List[str],
    changed_pages: List[str],
    removed_pages: List[str],
    include_footer: bool = True,
) -> Tuple[str, str]:
    """Return (subject, body) for a single site's changes."""
    subject = "Docs update detected \u2014 {}".format(site)
    lines = ["Update detected on {}\n".format(site)]

    if new_pages:
        lines.append("New pages ({}):".format(len(new_pages)))
        lines.extend("  + {}".format(u) for u in sorted(new_pages))

    if changed_pages:
        lines.append("\nChanged pages ({}):".format(len(changed_pages)))
        lines.extend("  ~ {}".format(u) for u in sorted(changed_pages))

    if removed_pages:
        lines.append("\nRemoved pages ({}):".format(len(removed_pages)))
        lines.extend("  - {}".format(u) for u in sorted(removed_pages))

    body = "\n".join(lines)
    if include_footer:
        body += EMAIL_FOOTER
    return subject, body


def build_digest_message(
    site_diffs: Dict[str, Tuple[List[str], List[str], List[str]]]
) -> Tuple[str, str]:
    """Return (subject, body) summarising changes across all sites."""
    subject = "Docs update digest \u2014 {} site(s) changed".format(len(site_diffs))
    lines = ["Changes detected across {} site(s):\n".format(len(site_diffs))]

    for site, (new_pages, changed_pages, removed_pages) in sorted(site_diffs.items()):
        lines.append("=== {} ===".format(site))
        if new_pages:
            lines.append("  New ({}):".format(len(new_pages)))
            lines.extend("    + {}".format(u) for u in sorted(new_pages))
        if changed_pages:
            lines.append("  Changed ({}):".format(len(changed_pages)))
            lines.extend("    ~ {}".format(u) for u in sorted(changed_pages))
        if removed_pages:
            lines.append("  Removed ({}):".format(len(removed_pages)))
            lines.extend("    - {}".format(u) for u in sorted(removed_pages))
        lines.append("")

    body = "\n".join(lines) + EMAIL_FOOTER
    return subject, body


def build_formatting_error_message(
    site: str,
    fmt_errors: Dict[str, List[Tuple[str, str]]],
) -> Tuple[str, str]:
    """
    Return (subject, body) for Mintlify formatting errors detected on *site*.

    fmt_errors maps page_url -> [(error_label, snippet), ...].
    """
    total = sum(len(v) for v in fmt_errors.values())
    subject = "Mintlify formatting error detected \u2014 {} ({} issue(s))".format(
        site, total
    )
    lines = [
        "Mintlify save-corruption artifacts detected on {}".format(site),
        "Pages affected: {}".format(len(fmt_errors)),
        "",
    ]
    for page_url in sorted(fmt_errors):
        lines.append("  Page: {}".format(page_url))
        for label, snippet in fmt_errors[page_url]:
            lines.append("    Issue : {}".format(label))
            lines.append("    Sample: ...{}...".format(snippet))
        lines.append("")

    body = "\n".join(lines) + EMAIL_FOOTER
    return subject, body


# ---------------------------------------------------------------------------
# Alert sending
# ---------------------------------------------------------------------------

def send_gmail(subject: str, body: str) -> None:
    sender = _require_env("GMAIL_SENDER")
    password = _require_env("GMAIL_APP_PASSWORD")
    recipient = _require_env("GMAIL_RECIPIENT")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())

    print("[INFO] Gmail alert sent.")


def send_slack(text: str) -> None:
    token = _require_env("SLACK_BOT_TOKEN")
    channel = _require_env("SLACK_CHANNEL")

    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": "Bearer {}".format(token),
            "Content-Type": "application/json",
        },
        json={"channel": channel, "text": text},
        timeout=15,
    )
    data = resp.json()
    if not data.get("ok"):
        print("[WARN] Slack error: {}".format(data.get("error")), file=sys.stderr)
    else:
        print("[INFO] Slack alert sent.")


# ---------------------------------------------------------------------------
# Slack slash-command server
# ---------------------------------------------------------------------------

def run_slack_server() -> None:
    """
    Start a lightweight HTTP server that accepts Slack slash-command POSTs.

    Only imported/started when --slack-server is passed.
    Listens on port 3000 for POST /slack/command.

    Expected payload fields: command, text
    Supported command text: 'mode digest' or 'mode per-site'
    """
    try:
        from flask import Flask, Response, request  # type: ignore
    except ImportError:
        print(
            "[ERROR] Flask is required for --slack-server. "
            "Install it with: pip install flask>=3.0.0",
            file=sys.stderr,
        )
        sys.exit(1)

    app = Flask(__name__)

    @app.route("/slack/command", methods=["POST"])
    def slack_command() -> "Response":
        text = (request.form.get("text") or "").strip().lower()
        parts = text.split()

        if parts[:1] == ["mode"] and len(parts) == 2:
            new_mode = parts[1]
            if new_mode not in VALID_MODES:
                return Response(
                    "Invalid mode '{}'. Choose: {}".format(
                        new_mode, ", ".join(sorted(VALID_MODES))
                    ),
                    status=200,
                    mimetype="text/plain",
                )
            config = load_config_json()
            config["alert_mode"] = new_mode
            save_config_json(config)
            return Response(
                "Alert mode updated to '{}'.".format(new_mode),
                status=200,
                mimetype="text/plain",
            )

        return Response(
            "Usage: /docswatcher mode [digest|per-site]",
            status=200,
            mimetype="text/plain",
        )

    print("[INFO] Slack command server listening on port 3000 (POST /slack/command).")
    app.run(host="0.0.0.0", port=3000)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="DiffLynx -- multi-site docs change detector."
    )
    parser.add_argument(
        "--mode",
        choices=list(VALID_MODES),
        default=None,
        help="Alert mode: 'per-site' (default) or 'digest'. Overrides .env and config.json.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Crawl all sites and print detected changes without sending alerts.",
    )
    parser.add_argument(
        "--slack-server",
        action="store_true",
        help="Start the Slack slash-command HTTP server on port 3000 (blocking).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.slack_server:
        run_slack_server()
        return

    mode = resolve_mode(args.mode)
    watch_urls = get_watch_urls()
    mintlify_sites = get_mintlify_sites()

    print("[INFO] Alert mode: {}".format(mode))
    print("[INFO] Watching {} site(s).".format(len(watch_urls)))
    print("[INFO] Mintlify formatting checks enabled for: {}".format(
        ", ".join(sorted(mintlify_sites)) or "(none)"
    ))

    # Load existing snapshot (migrates legacy format if needed)
    snapshot = load_snapshot()

    # Determine if this is the first-ever run (no snapshot at all)
    first_run = not snapshot

    # Crawl every site and collect diffs + formatting errors
    new_snapshot: Dict[str, Dict[str, str]] = {}
    site_diffs: Dict[str, Tuple[List[str], List[str], List[str]]] = {}
    all_fmt_errors: Dict[str, Dict[str, List[Tuple[str, str]]]] = {}

    for site in watch_urls:
        is_mintlify = site in mintlify_sites
        print("[INFO] Crawling {} {}...".format(
            site, "(+ Mintlify check)" if is_mintlify else ""
        ))
        current, fmt_errors = crawl_site(site, mintlify_check=is_mintlify)
        print("[INFO]   Found {} page(s) under {}.".format(len(current), site))
        new_snapshot[site] = current

        if fmt_errors:
            all_fmt_errors[site] = fmt_errors
            print("[WARN]   Mintlify formatting errors found on {} page(s) of {}.".format(
                len(fmt_errors), site
            ))

        old_site = snapshot.get(site, {})

        if not old_site:
            print("[INFO]   No previous snapshot for {} -- baseline will be saved.".format(site))
            continue

        new_pages, changed_pages, removed_pages = diff_snapshots(old_site, current)
        if new_pages or changed_pages or removed_pages:
            site_diffs[site] = (new_pages, changed_pages, removed_pages)
        else:
            print("[INFO]   No changes on {}.".format(site))

    # Persist updated snapshot (regardless of dry-run so baseline is always written)
    save_snapshot(new_snapshot)

    # ------------------------------------------------------------------
    # Mintlify formatting-error alerts (fired every run errors are found,
    # independent of content-hash changes)
    # ------------------------------------------------------------------
    if all_fmt_errors:
        for site, fmt_errors in all_fmt_errors.items():
            subject, body = build_formatting_error_message(site, fmt_errors)
            print("\n[FORMATTING ERROR] {}\n{}".format(subject, body))
            if not args.dry_run:
                send_gmail(subject, body)
                send_slack("*{}*\n```\n{}\n```".format(subject, body))

    if args.dry_run and all_fmt_errors:
        print("[DRY-RUN] Formatting-error alerts not sent.")

    # ------------------------------------------------------------------
    # Content-change alerts
    # ------------------------------------------------------------------
    if first_run or not any(snapshot.values()):
        print("[INFO] No previous snapshot existed. Baseline saved. No change alert sent.")
        return

    if not site_diffs:
        print("[INFO] No content changes detected across all sites.")
        return

    # Print diffs
    for site, (new_pages, changed_pages, removed_pages) in site_diffs.items():
        _, body = build_site_message(
            site, new_pages, changed_pages, removed_pages, include_footer=False
        )
        print(body)

    if args.dry_run:
        print("[DRY-RUN] No change alerts sent.")
        return

    # Send alerts
    if mode == "digest":
        subject, body = build_digest_message(site_diffs)
        send_gmail(subject, body)
        send_slack("*{}*\n```\n{}\n```".format(subject, body))
    else:
        # per-site
        for site, (new_pages, changed_pages, removed_pages) in site_diffs.items():
            subject, body = build_site_message(site, new_pages, changed_pages, removed_pages)
            send_gmail(subject, body)
            send_slack("*{}*\n```\n{}\n```".format(subject, body))

    print("[INFO] Snapshot updated.")


if __name__ == "__main__":
    main()
