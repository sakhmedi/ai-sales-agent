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

## HubSpot setup

The agent writes to HubSpot through a **private app** token. A free HubSpot account is enough.

### 1. Create a free HubSpot account
1. Go to https://www.hubspot.com and click **Get started free** (Free CRM).
2. Sign up and finish onboarding. You land in your HubSpot account.

### 2. Create a private app
HubSpot now lists private apps under "Legacy apps".

1. In HubSpot, open **Development** in the main menu, then **Legacy apps** in the left sidebar.
2. Click **Create legacy app** and choose **Private**.
3. **Basic Info** tab: name it e.g. `AI Sales Agent`.
4. **Scopes** tab: click **Add new scope** and select:

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

5. Click **Create app**, then **Continue creating**.

### 3. Copy the token
1. Open the app and go to the **Auth** tab.
2. Click **Show token** and copy it.
3. In `.env`:
   ```
   HUBSPOT_TOKEN=pat-...
   DRY_RUN=false
   ```
   Never commit `.env`; it is in `.gitignore`.

### 4. First run
```bash
uv run python -m app.agent "Company name" https://company.example
```
On the first real run the agent creates two custom deal properties, **AI lead score** (`ai_lead_score`)
and **AI lead fit** (`ai_lead_fit`). In HubSpot you'll then see:

- **Company**, found by domain or created
- **Contact**, only if the agent found a real person or email on the website
- **Deal** `"<Company> - AI SDR"` with score and fit, linked to the company and contact
- **Note** on the deal with the email draft and the qualification reasons

Running the agent again for the same company updates the existing deal instead of creating a duplicate.

### Troubleshooting
- `401`: the token is wrong or was rotated. Copy it again from the **Auth** tab.
- `403`: a scope is missing. Add it on the **Scopes** tab, save, and run again.
- `429`: HubSpot rate limit. The client retries automatically (`HUBSPOT_MAX_RETRIES`).
- Deal stage error: the default pipeline in your account may use other stage IDs.
  Set `HUBSPOT_DEAL_STAGE` (and `HUBSPOT_PIPELINE`) in `.env`.

> The client uses HubSpot's legacy `/crm/v3` endpoints (supported until September 2027).
> Moving to the date-based API (`/crm/objects/2026-09/...`) is a planned follow-up.

See `CLAUDE.md` for architecture and code rules.
