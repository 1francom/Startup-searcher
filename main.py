#!/usr/bin/env python3
"""
Startup Scout — automated daily discovery of startups for spontaneous
applications (Initiativbewerbung).

Two kinds of sources are handled differently, because they carry different
signals:

  * **Portals** (startup news: Gründerszene, deutsche-startups.de, Startbase)
    describe companies, not open roles. For an Initiativbewerbung you apply
    *speculatively*, so we surface EVERY newly-covered startup and let you
    triage — no role/location gate.

  * **Job boards** (Berlin Startup Jobs, RemoteOK, …) advertise concrete roles,
    so we DO apply the role + location filters here to keep only relevant
    postings.

Each entry's own RSS body rarely contains the startup's actual website, so for
feeds flagged ``resolve=True`` the agent fetches the full article/posting page
and extracts the real outbound company link(s) from it — a single "roundup"
article covering several startups yields one lead per startup. This is only
enabled for feeds verified to reliably expose that link in public HTML
(deutsche-startups.de, the job boards); Gründerszene's public pages are
paywalled/truncated and Startbase's articles never link to the company at all,
so both fall back to the article link rather than risk matching the wrong
domain (an ad partner, a nav link) as if it were the startup's site.

The agent:
  1. Reads the set of already-seen identities from ``visited_startups.json``.
  2. Fetches the configured public RSS feeds (no paid API keys required).
  3. Builds one or more leads per entry (name + website), resolving the real
     company link where possible and applying role/location filters only to
     job boards.
  4. Drops anything already seen.
  5. Emails a summary of the *new* leads via ``smtplib``.
  6. Persists the new identities back into ``visited_startups.json``.

The script is dependency-light (``requests``, ``feedparser``,
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
from urllib.parse import urlparse, urlunparse

import feedparser
import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# File that stores every identity we have already surfaced. Kept in the repo so
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

# Feed source types.
PORTAL = "portal"        # startup news → surface every entry (no role gate)
JOBBOARD = "jobboard"    # job listings → apply role + location filters

# RSS / Atom feeds. All publicly accessible, no API key. Each entry declares a
# ``type`` (see above) and a ``resolve`` flag: whether it's worth fetching the
# full article/posting page to find the startup's real website. This is only
# turned on for feeds verified to actually expose that link in public HTML —
# see the module docstring for why Gründerszene and Startbase are excluded.
# Reachability of every URL below was verified before inclusion; a feed that
# later goes offline is simply logged and skipped.
FEEDS: list[dict[str, str | bool]] = [
    # --- Startup news portals (surface ALL newly-covered startups) ------- #
    {"name": "Gründerszene", "url": "https://www.gruenderszene.de/feed", "type": PORTAL, "resolve": False},
    {
        "name": "deutsche-startups.de",
        "url": "https://www.deutsche-startups.de/feed/",
        "type": PORTAL,
        "resolve": True,
    },
    {"name": "Startbase", "url": "https://www.startbase.com/feed/", "type": PORTAL, "resolve": False},
    # --- Job boards (role + location filtered) --------------------------- #
    {
        "name": "Berlin Startup Jobs",
        "url": "https://berlinstartupjobs.com/feed/",
        "type": JOBBOARD,
        "resolve": True,
    },
    {
        "name": "Berlin Startup Jobs (Data Science)",
        "url": "https://berlinstartupjobs.com/skill-areas/data-science/feed/",
        "type": JOBBOARD,
        "resolve": True,
    },
    # NB: RemoteOK's category RSS feeds (e.g. /remote-data-jobs.rss) were
    # retired (HTTP 410) — only their JSON API remains, which would need a
    # separate fetcher. Dropped rather than left permanently broken; add back
    # if/when a suitable RSS/Atom endpoint reappears.
]

# Role keywords. A job-board entry is kept when ANY appears in its title or
# summary. (Portals are NOT gated by these.) Lower-cased for matching.
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
    "analytics engineer",
]

# Location keywords. On a job-board entry that mentions a location at all, one
# of these must match. Entries that mention no location survive (many postings
# omit a city), so we don't discard good leads. (Portals are NOT gated by these.)
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

# Signals that an entry is talking about *a* location at all (used only to
# decide whether the location gate should apply on job boards).
LOCATION_SIGNALS: list[str] = LOCATION_KEYWORDS + [
    "remote",
    "hybrid",
    "office",
    "standort",
    "location",
    "based in",
    "worldwide",
    "europe",
    "usa",
]

# Portals/aggregators/social sites that must never be treated as a discovered
# startup's own website.
IGNORED_DOMAINS: set[str] = {
    "gruenderszene.de",
    "deutsche-startups.de",
    "startbase.com",
    "startbase.de",
    "businessinsider.de",
    "businessinsider.com",
    "berlinstartupjobs.com",
    "londonstartupjobs.co.uk",
    # Site-wide ad widget embedded in every deutsche-startups.de article —
    # not a mentioned startup.
    "digitale-leute.de",
    "remoteok.com",
    "remoteok.io",
    "weworkremotely.com",
    "t3n.de",
    # Recurring sponsor/newsletter widgets embedded in deutsche-startups.de
    # articles — not the covered startup's own site.
    "startupland.de",
    "startupradar.substack.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "linkedin.com",
    "xing.com",
    "instagram.com",
    "youtube.com",
    "google.com",
    "apple.com",
    "amazon.com",
    "wikipedia.org",
    "github.com",
    "medium.com",
}

# Anchor texts too generic to use as a startup's display name (fall back to its
# domain instead). Lower-cased for matching.
GENERIC_LINK_TEXT: set[str] = {
    "", "hier", "here", "mehr", "mehr erfahren", "mehr dazu", "website",
    "webseite", "homepage", "startseite", "link", "read more", "learn more",
    "zur website", "zur webseite", "weiterlesen", "quelle", "source",
}

# Safety cap on distinct company links pulled from a single article/posting —
# generous enough for the largest realistic "N new startups" roundup post.
MAX_LINKS_PER_ENTRY = 10

# deutsche-startups.de tags its entries with content-type hashtags in the
# title. #DealMonitor / #Brandneu reliably name their headline subject via the
# FIRST outbound link in the body (funding-round journalism convention:
# subject first, citations/investors/press after) — enough to trust exactly
# one link. An explicit "N neue Startups: A, B, C" enumeration goes further
# and reliably links each of the N companies in turn, so multi-link
# extraction is safe there too. Everything else — including #StartupTicker,
# which bundles several unrelated topics ("+++ A +++ B +++") behind one
# headline and was verified to pick up citations/government bodies/press as
# often as the real subject, plus guest posts, interviews, and weekly
# recaps — has no reliable link position, so those entries fall back to the
# article link instead of guessing.
DEAL_TAG_PATTERN = re.compile(r"#(dealmonitor|brandneu)", re.IGNORECASE)
ROUNDUP_TITLE_PATTERN = re.compile(r"\d+\s+neue\s+startups", re.IGNORECASE)

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
    """A single discovered lead.

    ``key`` is the deduplication identity: the company's own domain when we can
    extract it, otherwise the article/job-posting URL (which is unique per
    entry). ``website`` is what we show the user — the company site if found,
    else the article/posting link. ``resolved`` is True when ``website`` is a
    confirmed company domain rather than a fallback article/posting link.
    """

    name: str
    website: str
    key: str
    source: str
    resolved: bool = False

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

    # ``domains`` holds the set of seen identities (company domains and/or
    # article URLs). Guarantee the keys we rely on always exist.
    data.setdefault("domains", [])
    data.setdefault("startups", [])
    data.setdefault("last_run", None)
    return data


def save_visited(state: dict, new_startups: list[Startup]) -> None:
    """Merge freshly discovered startups into the state and write it back."""
    known_keys = set(state.get("domains", []))

    for s in new_startups:
        known_keys.add(s.key)
        state["startups"].append(
            {
                **asdict(s),
                "discovered_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    state["domains"] = sorted(known_keys)
    state["last_run"] = datetime.now(timezone.utc).isoformat()

    VISITED_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    log.info("Persisted state: %d total identities known.", len(state["domains"]))


# --------------------------------------------------------------------------- #
# Fetching & extraction
# --------------------------------------------------------------------------- #

def _text_matches_any(text: str, keywords: Iterable[str]) -> bool:
    """Case-insensitive substring match of ``text`` against ``keywords``."""
    lowered = text.lower()
    return any(kw in lowered for kw in keywords)


def _is_ignored_domain(domain: str) -> bool:
    """True if ``domain`` is (or is a subdomain of) an ignored domain."""
    return any(domain == d or domain.endswith("." + d) for d in IGNORED_DOMAINS)


def _extract_domain(url: str) -> str | None:
    """Return the host without ``www.`` (and without port) or ``None``."""
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


def _normalize_url(url: str) -> str:
    """Strip query/fragment so the same posting yields a stable dedup key."""
    try:
        p = urlparse(url)
    except ValueError:
        return url
    return urlunparse((p.scheme, p.netloc, p.path.rstrip("/"), "", "", ""))


def _fetch_html(url: str) -> str | None:
    """Fetch a page's raw HTML, or ``None`` on any network/HTTP failure."""
    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("    ✗ Could not fetch page %s: %s", url, exc)
        return None
    return resp.text


def _find_company_links(html: str) -> list[tuple[str, str]]:
    """Return ``(url, anchor_text)`` pairs for external, non-ignored links.

    Prefers an isolated content container (``<article>`` or an
    entry/post/article-content class, typical of the WordPress-based portals
    this project reads) so navigation/sidebar/footer chrome doesn't leak in;
    falls back to the whole page when no such container exists. De-duplicates
    by domain, preserving document order, and caps at ``MAX_LINKS_PER_ENTRY``.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    container = (
        soup.find("article")
        or soup.find(class_=re.compile(r"(entry|post|article)-content"))
        or soup
    )

    seen_domains: set[str] = set()
    results: list[tuple[str, str]] = []
    for a in container.find_all("a", href=True):
        if len(results) >= MAX_LINKS_PER_ENTRY:
            break
        href = a["href"].strip()
        if not href.startswith("http"):
            continue
        domain = _extract_domain(href)
        if not domain or _is_ignored_domain(domain) or domain in seen_domains:
            continue
        seen_domains.add(domain)
        results.append((href, a.get_text(strip=True)))
    return results


def _clean_name(raw: str) -> str:
    """Tidy a startup/company name pulled from a feed title or link anchor."""
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
    feed_type = feed.get("type", JOBBOARD)
    log.info("Fetching feed: %s [%s] (%s)", source, feed_type, url)

    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("  ✗ Could not fetch %s: %s", source, exc)
        return []

    parsed = feedparser.parse(resp.content)
    if parsed.bozo:
        log.warning("  ! Feed %s reported a parse warning: %s", source, parsed.bozo_exception)

    resolve = bool(feed.get("resolve", False))
    candidates: list[Startup] = []
    for entry in parsed.entries:
        candidates.extend(_entry_to_startups(entry, source, feed_type, resolve))

    log.info("  → %d candidate(s) from %s", len(candidates), source)
    return candidates


def _passes_job_filters(haystack: str) -> bool:
    """Role + location gate applied to job-board entries only."""
    # 1) Role filter — must mention at least one target role keyword.
    if not _text_matches_any(haystack, ROLE_KEYWORDS):
        return False

    # 2) Location filter — if a location is mentioned at all, it must match.
    mentions_location = _text_matches_any(haystack, LOCATION_SIGNALS)
    if mentions_location and not _text_matches_any(haystack, LOCATION_KEYWORDS):
        return False

    return True


def _entry_to_startups(entry, source: str, feed_type: str, resolve: bool) -> list[Startup]:
    """Convert a single feed entry into one or more ``Startup`` leads.

    When ``resolve`` is True, tries hard to find the startup's own website —
    first in the RSS body (cheap), then by fetching the full article/posting
    page (one extra request). A "roundup" article covering several startups
    yields one lead per distinct company link found. When no real company
    link can be found anywhere (or ``resolve`` is False), falls back to a
    single lead pointing at the article/posting link itself.
    """
    title = getattr(entry, "title", "") or ""
    summary = getattr(entry, "summary", "") or ""
    content_html = ""
    if getattr(entry, "content", None):
        # feedparser stores full content as a list of dicts.
        content_html = " ".join(c.get("value", "") for c in entry.content)
    entry_link = getattr(entry, "link", "") or ""

    # Job boards are role/location gated; portals surface everything.
    if feed_type == JOBBOARD:
        haystack = " ".join([title, summary, content_html])
        if not _passes_job_filters(haystack):
            return []

    company_links: list[tuple[str, str]] = []
    if resolve:
        # 1) Cheap: the RSS body itself sometimes already has the link.
        raw_links = _find_company_links(summary + content_html)
        # 2) Otherwise fetch the real page and look there.
        if not raw_links and entry_link:
            page_html = _fetch_html(entry_link)
            if page_html:
                raw_links = _find_company_links(page_html)

        if feed_type == JOBBOARD:
            # A job posting is about one company; keep only the first link
            # even if the page also links a "similar jobs" sidebar elsewhere.
            company_links = raw_links[:1]
        elif ROUNDUP_TITLE_PATTERN.search(title):
            company_links = raw_links  # each link is a distinct new startup
        elif DEAL_TAG_PATTERN.search(title):
            company_links = raw_links[:1]  # subject is linked first
        # else: guest posts / interviews / recaps — leave empty so we fall
        # back to the article link below rather than guess.

    if company_links:
        leads: list[Startup] = []
        multi = len(company_links) > 1
        for href, anchor_text in company_links:
            domain = _extract_domain(href)
            candidate_name = _clean_name(anchor_text)
            if not candidate_name or candidate_name.lower() in GENERIC_LINK_TEXT:
                # A single-link entry's title is almost always the company
                # name ("Acme raises 5M"); a roundup entry's title lists
                # several companies at once, so the domain reads better.
                candidate_name = (_clean_name(title) if not multi else None) or domain or href
            leads.append(
                Startup(
                    name=candidate_name,
                    website=href,
                    key=domain or _normalize_url(href),
                    source=source,
                    resolved=True,
                )
            )
        return leads

    # Nothing resolvable — fall back to the article/posting link itself so a
    # real lead is never silently dropped.
    if not entry_link:
        return []
    name = _clean_name(title) or entry_link
    return [Startup(name=name, website=entry_link, key=_normalize_url(entry_link), source=source, resolved=False)]


def discover_startups() -> list[Startup]:
    """Run every configured feed and return a de-duplicated candidate list."""
    seen_this_run: set[str] = set()
    results: list[Startup] = []

    for feed in FEEDS:
        for startup in fetch_feed(feed):
            if startup.key in seen_this_run:
                continue
            seen_this_run.add(startup.key)
            results.append(startup)

    log.info("Discovered %d unique candidate(s) this run.", len(results))
    return results


# --------------------------------------------------------------------------- #
# Notification
# --------------------------------------------------------------------------- #

def _build_email_body(new_startups: list[Startup]) -> tuple[str, str]:
    """Return ``(plain_text, html)`` bodies listing the new startups.

    ``new_startups`` is expected to already be filtered down to resolved
    leads only (see ``main()``) — every entry here links straight to the
    startup's own website, never a portal/article page.
    """
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
    # NB: use ``or`` (not a .get default) so an empty RECIPIENT_EMAIL — which is
    # what GitHub injects when the optional secret is undefined — falls back to
    # the sender instead of producing a blank "To" address.
    recipient_email = os.environ.get("RECIPIENT_EMAIL") or sender_email

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
    known_keys = set(state.get("domains", []))
    log.info("Loaded %d previously-known identity(ies).", len(known_keys))

    candidates = discover_startups()

    # Keep only leads whose identity we have never surfaced before.
    new_startups = [s for s in candidates if s.key not in known_keys]
    log.info("%d of %d candidates are new.", len(new_startups), len(candidates))

    if new_startups:
        for s in new_startups:
            log.info("  NEW: %s", s)

    # Only email leads resolved to the startup's own website — fallback leads
    # (portal/article link, company site not found) are still persisted below
    # so they're never reprocessed, just never sent.
    resolved_new = [s for s in new_startups if s.resolved]
    log.info(
        "%d of %d new leads resolved to a real company site — emailing those only.",
        len(resolved_new), len(new_startups),
    )

    send_email(resolved_new)
    save_visited(state, new_startups)

    log.info("=== Startup Scout run finished ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # pragma: no cover - top-level safety net
        log.exception("Unhandled error: %s", exc)
        sys.exit(1)
