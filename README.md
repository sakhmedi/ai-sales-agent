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

The API runs with `uv run uvicorn app.main:app --reload` (health check at http://127.0.0.1:8000/health).

By default `DRY_RUN=true`: the agent logs what it would send to HubSpot and writes nothing.

## Process a list of companies
Put companies in a CSV with `name` and `website` columns (comma or semicolon separated,
so Excel exports work). See `data/companies.example.csv`.

```bash
uv run python -m app.batch data/companies.example.csv
uv run python -m app.batch data/my_companies.csv --limit 5   # first 5 only
```
Each result is saved to SQLite (`data/app.db`, table `lead_results`) as soon as the company is
done, so an interrupted batch keeps what it already processed. One failing company never stops the batch.
Your own CSV files in `data/` are gitignored; only the example is committed.

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
