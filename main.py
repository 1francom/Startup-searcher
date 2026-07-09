#!/usr/bin/env python3
"""
Startup Scout — automated daily discovery of startups for spontaneous
applications (Initiativbewerbung).

The agent:
  1. Reads the list of already-seen startup domains from ``visited_startups.json``.
  2. Fetches public RSS feeds from German startup portals and open job
     aggregators (no paid API keys required).
  3. Extracts each startup's name and primary website, keeping only entries
     that match the target role keywords and target locations.
  4. Filters out any domain that has already been discovered.
  5. Emails a summary of the *newly* discovered startups via ``smtplib``.
  6. Persists the newly discovered domains back into ``visited_startups.json``.

The script is intentionally dependency-light (``requests``, ``feedparser`` and
``beautifulsoup4``) and defensively coded so a single broken feed never aborts
the whole run.
"""

from __future__ import annotations

import json
import logging
import os
import re
import smtplib
import ssl
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# File that stores every domain we have already surfaced. Kept in the repo so
# GitHub Actions can commit it back after each run.
VISITED_FILE = Path(__file__).with_name("visited_startups.json")

# Generic User-Agent so portals don't reject the request as an obvious bot.
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; StartupScout/1.0; "
        "+https://github.com/) "
    )
}

# Network timeout (seconds) applied to every outbound HTTP request.
REQUEST_TIMEOUT = 20

# RSS / Atom feeds from German startup portals and open job aggregators.
# These are all publicly accessible and require no API key. Add or remove
# sources here — the rest of the pipeline adapts automatically.
FEEDS: list[dict[str, str]] = [
    # --- Startup news portals -------------------------------------------- #
    {"name": "Gründerszene", "url": "https://www.gruenderszene.de/feed"},
    {"name": "deutsche-startups.de", "url": "https://www.deutsche-startups.de/feed/"},
    {"name": "Berlin Valley", "url": "https://berlinvalley.com/feed/"},
    {"name": "Startbase", "url": "https://www.startbase.com/feed/"},
    # --- Open job aggregators (RSS) -------------------------------------- #
    # WeWorkRemotely and RemoteOK expose keyword-filterable public RSS feeds.
    {
        "name": "RemoteOK (data)",
        "url": "https://remoteok.com/remote-data-jobs.rss",
    },
    {
        "name": "WeWorkRemotely (data)",
        "url": "https://weworkremotely.com/categories/remote-data-jobs.rss",
    },
]

# Role keywords. An entry is kept when ANY of these appears in its title or
# summary. Lower-cased for case-insensitive matching.
ROLE_KEYWORDS: list[str] = [
    "data analytics",
    "data analyst",
    "data science",
    "data scientist",
    "datenanalyse",
    "datenanalyst",
    "economic research",
    "economist",
    "volkswirt",
    "quantitative",
    "quant ",
    "econometric",
    "ökonom",
    "business intelligence",
    "bi analyst",
    "research analyst",
]

# Location keywords. When an entry mentions a location at all, it must match one
# of these. Entries that mention no location survive (startup news posts rarely
# state a city), so we don't accidentally discard good leads.
LOCATION_KEYWORDS: list[str] = [
    "münchen",
    "munich",
    "frankfurt",
    "berlin",
    "hamburg",
    "köln",
    "cologne",
    "stuttgart",
    "düsseldorf",
    "leipzig",
    "germany",
    "deutschland",
    "buenos aires",
    "argentina",
    "argentinien",
]

# Domains that are portals/aggregators themselves — never treat these as a
# "discovered startup" even if they appear as a link inside an entry.
IGNORED_DOMAINS: set[str] = {
    "gruenderszene.de",
    "deutsche-startups.de",
    "berlinvalley.com",
    "startbase.com",
    "remoteok.com",
    "weworkremotely.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "linkedin.com",
    "instagram.com",
    "youtube.com",
    "google.com",
    "apple.com",
    "amazon.com",
    "wikipedia.org",
    "github.com",
    "medium.com",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("startup-scout")


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Startup:
    """A single discovered startup lead."""

    name: str
    website: str
    domain: str
    source: str

    def __str__(self) -> str:  # pragma: no cover - cosmetic only
        return f"{self.name} — {self.website} (via {self.source})"


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

def load_visited() -> dict:
    """Load the persisted state, tolerating a missing or corrupt file."""
    if not VISITED_FILE.exists():
        log.info("No %s found — starting fresh.", VISITED_FILE.name)
        return {"domains": [], "startups": [], "last_run": None}

    try:
        data = json.loads(VISITED_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read %s (%s) — starting fresh.", VISITED_FILE.name, exc)
        return {"domains": [], "startups": [], "last_run": None}

    # Normalise: guarantee the keys we rely on always exist.
    data.setdefault("domains", [])
    data.setdefault("startups", [])
    data.setdefault("last_run", None)
    return data


def save_visited(state: dict, new_startups: list[Startup]) -> None:
    """Merge freshly discovered startups into the state and write it back."""
    known_domains = set(state.get("domains", []))

    for s in new_startups:
        known_domains.add(s.domain)
        state["startups"].append(
            {
                **asdict(s),
                "discovered_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    state["domains"] = sorted(known_domains)
    state["last_run"] = datetime.now(timezone.utc).isoformat()

    VISITED_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    log.info("Persisted state: %d total domains known.", len(state["domains"]))


# --------------------------------------------------------------------------- #
# Fetching & extraction
# --------------------------------------------------------------------------- #

def _text_matches_any(text: str, keywords: Iterable[str]) -> bool:
    """Case-insensitive substring match of ``text`` against ``keywords``."""
    lowered = text.lower()
    return any(kw in lowered for kw in keywords)


def _extract_domain(url: str) -> str | None:
    """Return the registrable-ish domain (host without ``www.``) or ``None``."""
    try:
        netloc = urlparse(url).netloc.lower()
    except ValueError:
        return None
    if not netloc:
        return None
    netloc = netloc.split(":")[0]  # strip any port
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc or None


def _first_external_link(html: str) -> str | None:
    """Find the first plausible external company link inside an entry's HTML."""
    soup = BeautifulSoup(html or "", "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith("http"):
            continue
        domain = _extract_domain(href)
        if domain and domain not in IGNORED_DOMAINS:
            return href
    return None


def _clean_name(raw: str) -> str:
    """Tidy a startup/company name pulled from a feed title."""
    # Job titles often look like "Data Scientist (m/w/d) at Acme GmbH".
    # Prefer the part after "at"/"bei"/"@" when present.
    for sep in (" at ", " bei ", " @ ", " – ", " - ", " | "):
        if sep in raw:
            raw = raw.split(sep)[-1]
    raw = re.sub(r"\(m/w/d\)|\(f/m/d\)|\(w/m/d\)", "", raw, flags=re.IGNORECASE)
    return raw.strip(" -–|·•").strip()


def fetch_feed(feed: dict[str, str]) -> list[Startup]:
    """Fetch and parse a single feed into a list of candidate ``Startup``s.

    Any network or parsing error is logged and swallowed so one bad feed can
    never abort the entire run.
    """
    source = feed["name"]
    url = feed["url"]
    log.info("Fetching feed: %s (%s)", source, url)

    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("  ✗ Could not fetch %s: %s", source, exc)
        return []

    parsed = feedparser.parse(resp.content)
    if parsed.bozo:
        log.warning("  ! Feed %s reported a parse warning: %s", source, parsed.bozo_exception)

    candidates: list[Startup] = []
    for entry in parsed.entries:
        startup = _entry_to_startup(entry, source)
        if startup is not None:
            candidates.append(startup)

    log.info("  → %d candidate(s) matched from %s", len(candidates), source)
    return candidates


def _entry_to_startup(entry, source: str) -> Startup | None:
    """Convert a single feed entry into a ``Startup`` if it passes the filters."""
    title = getattr(entry, "title", "") or ""
    summary = getattr(entry, "summary", "") or ""
    content_html = ""
    if getattr(entry, "content", None):
        # feedparser stores full content as a list of dicts.
        content_html = " ".join(c.get("value", "") for c in entry.content)

    haystack = " ".join([title, summary, content_html])

    # 1) Role filter — must mention at least one target role keyword.
    if not _text_matches_any(haystack, ROLE_KEYWORDS):
        return None

    # 2) Location filter — if a location is mentioned at all, it must match.
    mentions_any_location = _text_matches_any(
        haystack,
        # A broad set of "this text talks about a place" signals.
        LOCATION_KEYWORDS + ["remote", "hybrid", "office", "standort", "location"],
    )
    if mentions_any_location and not _text_matches_any(haystack, LOCATION_KEYWORDS):
        return None

    # 3) Resolve the startup website. Prefer an external link found in the body;
    #    otherwise fall back to the entry link's own domain.
    website = _first_external_link(summary + content_html)
    if website is None:
        website = getattr(entry, "link", "") or ""

    domain = _extract_domain(website)
    if not domain or domain in IGNORED_DOMAINS:
        return None

    name = _clean_name(title) or domain
    return Startup(name=name, website=website, domain=domain, source=source)


def discover_startups() -> list[Startup]:
    """Run every configured feed and return a de-duplicated candidate list."""
    seen_this_run: set[str] = set()
    results: list[Startup] = []

    for feed in FEEDS:
        for startup in fetch_feed(feed):
            if startup.domain in seen_this_run:
                continue
            seen_this_run.add(startup.domain)
            results.append(startup)

    log.info("Discovered %d unique candidate startups this run.", len(results))
    return results


# --------------------------------------------------------------------------- #
# Notification
# --------------------------------------------------------------------------- #

def _build_email_body(new_startups: list[Startup]) -> tuple[str, str]:
    """Return ``(plain_text, html)`` bodies listing the new startups."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Plain-text version.
    lines = [f"Startup Scout — {len(new_startups)} new lead(s) on {today}", ""]
    for i, s in enumerate(new_startups, 1):
        lines.append(f"{i}. {s.name}")
        lines.append(f"   {s.website}")
        lines.append(f"   (source: {s.source})")
        lines.append("")
    plain = "\n".join(lines)

    # HTML version.
    items = "\n".join(
        f'<li><a href="{s.website}">{s.name}</a> '
        f'<span style="color:#888">— {s.website} (via {s.source})</span></li>'
        for s in new_startups
    )
    html = f"""\
<html><body>
  <h2>Startup Scout — {len(new_startups)} new lead(s) on {today}</h2>
  <p>Newly discovered startups matching your profile
     (data / economics roles in DE + Buenos Aires):</p>
  <ol>{items}</ol>
  <p style="color:#aaa;font-size:12px">Sent automatically by your GitHub Actions scout.</p>
</body></html>"""

    return plain, html


def send_email(new_startups: list[Startup]) -> None:
    """Email the summary of new startups using credentials from the environment.

    Required environment variables (wire these up as GitHub Secrets):
        SMTP_SERVER, SMTP_PORT, SENDER_EMAIL, SENDER_PASSWORD
    Optional:
        RECIPIENT_EMAIL  (defaults to SENDER_EMAIL)
    """
    if not new_startups:
        log.info("No new startups — skipping email.")
        return

    smtp_server = os.environ.get("SMTP_SERVER")
    smtp_port_raw = os.environ.get("SMTP_PORT", "587")
    sender_email = os.environ.get("SENDER_EMAIL")
    sender_password = os.environ.get("SENDER_PASSWORD")
    recipient_email = os.environ.get("RECIPIENT_EMAIL", sender_email or "")

    missing = [
        var
        for var, val in {
            "SMTP_SERVER": smtp_server,
            "SENDER_EMAIL": sender_email,
            "SENDER_PASSWORD": sender_password,
        }.items()
        if not val
    ]
    if missing:
        log.warning(
            "Email not sent — missing environment variable(s): %s. "
            "New startups were still persisted.",
            ", ".join(missing),
        )
        return

    try:
        smtp_port = int(smtp_port_raw)
    except ValueError:
        log.warning("Invalid SMTP_PORT=%r — defaulting to 587.", smtp_port_raw)
        smtp_port = 587

    plain, html = _build_email_body(new_startups)

    message = MIMEMultipart("alternative")
    message["Subject"] = f"🚀 Startup Scout: {len(new_startups)} new lead(s)"
    message["From"] = sender_email
    message["To"] = recipient_email
    message.attach(MIMEText(plain, "plain", "utf-8"))
    message.attach(MIMEText(html, "html", "utf-8"))

    context = ssl.create_default_context()
    try:
        if smtp_port == 465:
            # Implicit TLS (e.g. SendGrid/Gmail SSL port).
            with smtplib.SMTP_SSL(smtp_server, smtp_port, context=context) as server:
                server.login(sender_email, sender_password)
                server.send_message(message)
        else:
            # STARTTLS (e.g. Gmail/SendGrid port 587).
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(sender_email, sender_password)
                server.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        log.error("Failed to send email: %s", exc)
        return

    log.info("Email sent to %s with %d new startup(s).", recipient_email, len(new_startups))


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def main() -> int:
    log.info("=== Startup Scout run started ===")

    state = load_visited()
    known_domains = set(state.get("domains", []))
    log.info("Loaded %d previously-known domain(s).", len(known_domains))

    candidates = discover_startups()

    # Keep only startups whose domain we have never surfaced before.
    new_startups = [s for s in candidates if s.domain not in known_domains]
    log.info("%d of %d candidates are new.", len(new_startups), len(candidates))

    if new_startups:
        for s in new_startups:
            log.info("  NEW: %s", s)

    send_email(new_startups)
    save_visited(state, new_startups)

    log.info("=== Startup Scout run finished ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # pragma: no cover - top-level safety net
        log.exception("Unhandled error: %s", exc)
        sys.exit(1)
