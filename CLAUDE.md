# AI Sales Agent (AI SDR)

## Project
An autonomous sales development agent. For each company in an input list it:

1. **Researches** the company (website, public info).
2. **Qualifies** the lead against ICP (Ideal Customer Profile) criteria.
3. **Writes** a personalized outreach email.
4. **Records** the result in the CRM (HubSpot).

The agent is built on Claude with tool calling: Claude decides which tool to call next,
our code executes the tool and returns the result, until the lead is fully processed.

## Stack
- Python 3.12, managed with **uv**
- FastAPI + Uvicorn (HTTP API)
- Anthropic Python SDK (Claude, tool calling)
- Pydantic v2 + pydantic-settings (data models, configuration)
- SQLAlchemy 2.0 + SQLite (local storage of leads and runs)
- httpx (HTTP client for web research and HubSpot API)
- HubSpot API (free account, private app token)
- pytest, ruff, mypy
- Docker (planned)

## Layout
```
app/
  main.py           FastAPI app factory (create_app) and app instance
  config.py         Settings loaded from env / .env (get_settings)
  logging_config.py Logging setup
  agent/            Agent loop: talks to Claude, dispatches tool calls
  tools/            Tools the agent can call (research, qualify, draft email, CRM sync)
  integrations/     Clients for external services (HubSpot, SMTP, web fetching)
  api/              FastAPI routers
  models/           Pydantic models (API schemas, domain objects)
tests/              pytest tests
data/               Input lists and local SQLite DB (DB files are gitignored)
```

## Code rules
- **Type hints everywhere**, including return types. Code must pass `mypy` (strict).
- **Pydantic models** for all data crossing a boundary: API requests/responses, tool inputs/outputs,
  data from external APIs. No raw dicts passed around.
- **Logging, not print.** Use `logger = logging.getLogger(__name__)` per module.
  Never log secrets or full email bodies at INFO level.
- **No secrets in code.** All keys and tokens come from `app.config.Settings` (env / `.env`).
  Secrets are typed as `SecretStr`. `.env` is never committed; update `.env.example` when adding settings.
- **DRY_RUN is the default.** With `DRY_RUN=true`, nothing is sent and nothing is written to HubSpot;
  side-effecting code must check `settings.dry_run` and log what it *would* do.
- External HTTP calls use `httpx` with explicit timeouts.
- Every tool and integration gets tests; external APIs are mocked in tests.
- Keep functions small; one responsibility per module.
- Code, comments and docs are in English.

## Commands
```bash
uv sync                                   # install dependencies (incl. dev group)
cp .env.example .env                      # then fill in values
uv run uvicorn app.main:app --reload      # run API at http://127.0.0.1:8000 (docs at /docs)
uv run pytest                             # run tests
uv run ruff check .                       # lint
uv run ruff format .                      # format
uv run mypy app                           # type check
```
