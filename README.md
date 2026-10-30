# AI Sales Agent (AI SDR)

An autonomous AI sales development agent: takes a list of companies, researches each one,
qualifies it against ICP criteria, drafts a personalized email and records the result in HubSpot.

Built with Python 3.12, FastAPI, Claude (Anthropic SDK, tool calling), Pydantic, SQLAlchemy and httpx.

## Quick start
```bash
uv sync
cp .env.example .env          # then set ANTHROPIC_API_KEY
uv run pytest
uv run python -m app.agent "Company name" https://company.example
```

By default `DRY_RUN=true`: the agent logs what it would send to HubSpot and writes nothing.

## Process a list of leads
Put leads in a CSV with `company_name` (or `name`) and `website`, plus optional `contact_name`
and `contact_email`. Comma or semicolon separators both work, so Excel exports are fine.
`data/sample_leads.csv` has 10 public Danish B2B companies with fake `@example.com` contacts.

```bash
uv run python -m app.cli run data/sample_leads.csv --limit 2   # import + run 2 new leads
uv run python -m app.cli import data/my_leads.csv              # import only
uv run python -m app.cli leads --fit warm                      # list leads
```
Leads are stored in SQLite (`data/app.db`) with status `new`; each run moves them on and is saved
right away, so an interrupted batch keeps what it already processed. One failing lead never stops
the batch (it stays `new` and is retried next time). Your own CSV files in `data/` are gitignored.

> **Upgrading from an older version?** The database schema changed. Delete `data/app.db` once;
> it is recreated on the next run. (Schema migrations with Alembic are a planned follow-up.)

## HTTP API
```bash
uv run uvicorn app.main:app --reload
```
Open http://127.0.0.1:8000/docs to try every endpoint in the browser.

| Method | Path | What it does |
|---|---|---|
| `POST` | `/leads/import` | upload a CSV (multipart field `file`) |
| `GET` | `/leads?fit=hot&status=pending_approval` | list leads, best score first |
| `GET` | `/leads/{id}` | lead + latest email draft + qualification reasons |
| `POST` | `/leads/{id}/run` | run the agent on one lead (~20 s) |
| `POST` | `/leads/run-batch?limit=10` | run the agent on all `new` leads, one by one |
| `POST` | `/leads/{id}/approve` | `{"reviewer": "...", "to": "optional"}`: send the pending email |
| `POST` | `/leads/{id}/reject` | `{"reviewer": "..."}`: reject the pending email |

Runs are synchronous to keep the project simple; in production they would go to a job queue.

## Sending emails (human approval)

**The agent never sends email.** Every draft is stored in SQLite with status `pending_approval`.
A person reviews it and only an explicit `approve` sends it.

```bash
uv run python -m app.outbox list                         # drafts waiting for approval
uv run python -m app.outbox show 1                       # email + why the lead qualified
uv run python -m app.outbox approve 1 --to anna@company.com
uv run python -m app.outbox reject 1
```
`--to` is needed when the agent found no contact email on the website. `--by` records the reviewer
(defaults to your OS user name).

Statuses in SQLite:

| Lead (`leads`) | Email (`emails`) |
|---|---|
| `new` → `qualified` / `disqualified` → `pending_approval` → `sent` | `pending_approval` → `sent` / `rejected` / `failed`, or `superseded` by a newer draft |

Safety rules built in: an email can be sent only once; a lead that was already emailed never gets a
second draft; with `DRY_RUN=true`, `approve` only logs what it would send and changes nothing.

### SMTP setup for testing

**Mailtrap (recommended).** A fake inbox: emails are caught and never reach real people.
1. Sign up free at https://mailtrap.io, open **Email Testing → Inboxes → My Inbox → SMTP**.
2. Copy host, port, username and password into `.env`:
   ```
   SMTP_HOST=sandbox.smtp.mailtrap.io
   SMTP_PORT=2525
   SMTP_USER=<mailtrap username>
   SMTP_PASSWORD=<mailtrap password>
   SMTP_FROM=sdr@example.com
   ```

**Gmail with an App Password.** Real delivery, so use it only with your own addresses.
1. Turn on 2-Step Verification for your Google account.
2. Create an App Password at https://myaccount.google.com/apppasswords.
3. In `.env`: `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USER` and `SMTP_FROM` = your Gmail
   address, `SMTP_PASSWORD` = the 16-character App Password.
4. Set `EMAIL_REDIRECT_TO=<your own address>` so every approved email comes to you instead of
   the real recipient (the intended recipient is shown in the subject).

Then set `DRY_RUN=false` and approve one draft. Set `DRY_RUN=true` again afterwards.

## HubSpot setup

The agent writes to HubSpot with a **service key**, an account-level API token. A free HubSpot
account is enough. This setup was tested end to end on a free account.

### 1. Create a free HubSpot account
1. Go to https://www.hubspot.com and click **Get started free** (Free CRM).
2. Sign up. When asked for a company website and you have none, `example.com` works.

### 2. Create a service key
New HubSpot accounts no longer offer legacy private apps; service keys replace them for
calling the API from your own code.

1. In HubSpot, open **Development** in the main menu, then **Keys → Service keys**
   (on some accounts: **Settings → Integrations → Service keys**).
2. Click **Create service key** and name it e.g. `AI Sales Agent`.
3. Add these scopes:

   | Scope | Used for |
   |---|---|
   | `crm.objects.companies.read` | find a company by domain |
   | `crm.objects.companies.write` | create companies |
   | `crm.objects.contacts.read` | find a contact by email |
   | `crm.objects.contacts.write` | create contacts and notes |
   | `crm.objects.deals.read` | find an existing deal |
   | `crm.objects.deals.write` | create and update deals |
   | `crm.schemas.deals.read` | check that the custom deal properties exist |
   | `crm.schemas.deals.write` | create the custom deal properties |

4. Create the key and copy it.

> Older accounts that still have **Development → Legacy apps → Private app** can use that app's
> access token instead. Both are sent the same way (`Authorization: Bearer <token>`).

### 3. Configure `.env`
```
HUBSPOT_TOKEN=<your service key>
DRY_RUN=true
```
Never commit `.env` (it is in `.gitignore`) and never paste the key into code or chat.

Check that the key works (read-only, writes nothing):
```bash
uv run python -c "from app.config import get_settings; from app.integrations.hubspot import HubSpotClient; c = HubSpotClient.from_settings(get_settings()); print('OK, company id:', c.search('companies', 'domain', 'example.com'))"
```
`OK, company id: None` means the key works and there is simply no such company.

### 4. First run
1. Run once with `DRY_RUN=true` and check the logged `would send to HubSpot` plan.
2. Set `DRY_RUN=false` and run the agent on one company:
   ```bash
   uv run python -m app.agent "Company name" https://company.example
   ```
3. Set `DRY_RUN=true` again when you are done.

On the first real run the agent creates two custom deal properties, **AI lead score** (`ai_lead_score`)
and **AI lead fit** (`ai_lead_fit`). In HubSpot you'll then see:

- **Company**, found by domain or created
- **Contact**, only if the agent found a real person or email on the website
- **Deal** `"<Company> - AI SDR"` with score and fit, linked to the company and contact
- **Note** on the deal with the email draft and the qualification reasons

Running the agent again for the same company updates the existing deal instead of creating a
duplicate; each run adds a new note with the latest email draft.

### 5. See the AI fields in HubSpot
Custom properties are hidden by default.

- **On a deal:** open the deal, scroll to the end of **About this deal** and click
  **View all properties**, then search `AI lead`. To pin them, customize the **About this deal**
  card and add both properties.
- **In the deals list:** switch to the table view, click **Edit columns** and add
  **AI lead score** and **AI lead fit**. Sorting by score shows the hottest leads first.

### Troubleshooting
- `401`: the key is wrong or was rotated. Create or copy it again.
- `403`: a scope is missing. Add it to the service key and run again.
- `429`: HubSpot rate limit. The client retries automatically (`HUBSPOT_MAX_RETRIES`).
- Deal stage error (`400`): your pipeline may use other stage IDs. Find them under
  **Settings → Objects → Deals → Pipelines** and set `HUBSPOT_DEAL_STAGE` (and `HUBSPOT_PIPELINE`) in `.env`.

> The client uses HubSpot's legacy `/crm/v3` endpoints (supported until September 2027).
> Moving to the date-based API (`/crm/objects/2026-09/...`) is a planned follow-up.

See `CLAUDE.md` for architecture and code rules.
