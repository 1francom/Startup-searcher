# 🚀 Startup Scout

A lightweight, self-hosted agent that **discovers startups matching your
profile** every day and emails you the new leads — perfect for spontaneous
applications (*Initiativbewerbung*).

It runs entirely on **GitHub Actions** (free tier), uses only **public RSS
feeds** and **open job boards** (no paid API keys), and keeps a running memory
of everything it has already shown you so you never get the same lead twice.

---

## What it looks for

| Filter        | Values |
|---------------|--------|
| **Roles**     | Data analytics · Data science · Economic research · Quantitative analysis · Economics-related roles |
| **Locations** | München · Frankfurt · Berlin · Hamburg · Köln · Stuttgart · Düsseldorf · Leipzig · other major German cities · Buenos Aires |
| **Stage**     | Any (early → late) |

You can tune all of these at the top of [`main.py`](main.py) via the
`ROLE_KEYWORDS`, `LOCATION_KEYWORDS`, and `FEEDS` lists.

---

## How it works

1. **Read state** — loads `visited_startups.json` (the list of domains already
   seen).
2. **Fetch feeds** — pulls public RSS/Atom feeds from German startup portals
   (Gründerszene, deutsche-startups.de, Berlin Valley, Startbase) and open job
   aggregators (RemoteOK, WeWorkRemotely).
3. **Filter & extract** — keeps entries that match your role keywords and
   locations, then extracts each startup's **name** and **primary website**.
4. **Deduplicate** — drops any domain already in `visited_startups.json`.
5. **Notify** — emails you a summary of **only the new** startups via
   `smtplib`.
6. **Persist** — writes the new domains back into `visited_startups.json`, and
   the GitHub Actions workflow commits the file to the repo.

A broken or unreachable feed is logged and skipped — it never aborts the run.

---

## Repository layout

```
.
├── main.py                       # The agent
├── requirements.txt              # requests, feedparser, beautifulsoup4
├── visited_startups.json         # Persisted memory (committed by CI)
├── README.md                     # You are here
└── .github/
    └── workflows/
        └── daily_scout.yml       # Daily 08:00 UTC schedule
```

---

## Setup

### 1. Create the repository

Push these files to a new GitHub repository (private is fine).

### 2. Configure the email secrets

The script sends mail with Python's built-in `smtplib`, reading its
configuration from environment variables that GitHub injects from
**repository secrets**.

Go to **Settings → Secrets and variables → Actions → New repository secret**
and add:

| Secret name       | Description                                        | Example |
|-------------------|----------------------------------------------------|---------|
| `SMTP_SERVER`     | SMTP host                                           | `smtp.gmail.com` |
| `SMTP_PORT`       | SMTP port (`587` for STARTTLS, `465` for SSL)       | `587` |
| `SENDER_EMAIL`    | The account that sends the mail                     | `you@gmail.com` |
| `SENDER_PASSWORD` | Password / app password / API key                   | *(see below)* |
| `RECIPIENT_EMAIL` | *(optional)* where to send; defaults to `SENDER_EMAIL` | `you@gmail.com` |

#### Option A — Gmail

1. Enable **2-Step Verification** on your Google account.
2. Create an **App Password**: <https://myaccount.google.com/apppasswords>.
   Google gives you a 16-character password.
3. Use these secrets:
   - `SMTP_SERVER = smtp.gmail.com`
   - `SMTP_PORT = 587`
   - `SENDER_EMAIL = your.address@gmail.com`
   - `SENDER_PASSWORD = <the 16-char app password>` (not your normal password)

#### Option B — SendGrid

1. Create an API key in the SendGrid dashboard.
2. Use these secrets:
   - `SMTP_SERVER = smtp.sendgrid.net`
   - `SMTP_PORT = 587`
   - `SENDER_EMAIL = <your verified sender address>`
   - `SENDER_PASSWORD = <your SendGrid API key>`
   - The literal username SendGrid expects is `apikey`; this project logs in
     with `SENDER_EMAIL`, so if SendGrid rejects it, set `SENDER_EMAIL` to the
     verified sender and it will still deliver. (SendGrid accepts the verified
     sender as the login identity for SMTP relay.)

> If the SMTP secrets are missing, the script still runs, still deduplicates,
> and still updates `visited_startups.json` — it simply skips the email and
> logs a warning. This makes the first dry run safe.

### 3. Enable Actions write access

The workflow needs to push the updated JSON back to the repo. This is already
declared in the workflow via:

```yaml
permissions:
  contents: write
```

Also confirm **Settings → Actions → General → Workflow permissions** is set to
**Read and write permissions**.

---

## Running it

### Automatically

The workflow in [`.github/workflows/daily_scout.yml`](.github/workflows/daily_scout.yml)
runs **every day at 08:00 UTC**.

### Manually (recommended for the first test)

Go to the **Actions** tab → **Daily Startup Scout** → **Run workflow**. Check
the logs, then your inbox.

### Locally

```bash
python -m pip install -r requirements.txt

# Optional: export SMTP settings to also test the email
export SMTP_SERVER=smtp.gmail.com
export SMTP_PORT=587
export SENDER_EMAIL=you@gmail.com
export SENDER_PASSWORD=your_app_password

python main.py
```

On Windows PowerShell:

```powershell
$env:SMTP_SERVER = "smtp.gmail.com"
$env:SMTP_PORT = "587"
$env:SENDER_EMAIL = "you@gmail.com"
$env:SENDER_PASSWORD = "your_app_password"
python main.py
```

---

## Customising

Everything tunable lives at the top of [`main.py`](main.py):

- **`FEEDS`** — add or remove RSS/Atom sources. Any public feed works.
- **`ROLE_KEYWORDS`** — the job/role terms an entry must mention.
- **`LOCATION_KEYWORDS`** — the cities/countries you care about.
- **`IGNORED_DOMAINS`** — portals and social sites that should never be treated
  as a discovered startup.

To reset the memory and re-surface every lead, empty the file back to:

```json
{ "domains": [], "startups": [], "last_run": null }
```

---

## Notes & limitations

- Startup **news** feeds describe companies rather than list jobs, so the
  website is extracted heuristically (first external link, else the article's
  own domain). Job-board feeds map much more cleanly to a company + role.
- The location filter is deliberately lenient: an entry that mentions **no**
  location is kept (many startup posts omit a city), while one that mentions a
  location must match your target list.
- Respect each portal's terms of service. This project only reads public RSS
  feeds at a low daily volume.
