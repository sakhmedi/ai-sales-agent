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
  agent/            Agent loop (loop.py), prompts, CLI entry point (__main__.py)
  tools/            Tools the agent can call: one module per tool, registered in ALL_TOOLS
  integrations/     Clients for external services; hubspot.py = HubSpot CRM (v3, retries)
  api/              FastAPI routers (leads.py) and dependencies (deps.py, overridden in tests)
  models/           Pydantic models (API schemas, domain objects)
  pipeline.py       Shared service layer: CSV import, run lead(s), list, approve (API + CLI use it)
  cli.py            CLI: run / import / leads
  storage.py        SQLAlchemy: lead_results (run log), leads, emails; persist_result()
  outbox.py         Human review CLI: list / show / approve (sends) / reject
config/icp.yaml     Ideal Customer Profile: industries, size, countries, signals, scoring
tests/              pytest tests (the agent loop is tested with a scripted fake client)
data/               Input lists and local SQLite DB (DB files are gitignored)
```

## Agent design
- Manual tool-use loop (no LangChain) in `app/agent/loop.py`, max `AGENT_MAX_STEPS` model calls per lead.
- Claude extracts facts; scoring in `qualify_lead` is deterministic Python driven by `config/icp.yaml`.
- Signals carry evidence (`{id, evidence}`); `draft_email` is a separate Claude call (structured
  output) that writes only from that evidence. The signature is appended by code, never by the model.
- Tool errors are returned to the model as `is_error` tool results, never raised out of the loop.
- `save_to_crm` upserts company (by domain), contact (by email), deal (by name) and adds a note.
  HubSpot calls are mocked with respx in tests; never hit the real API in tests.
- Each tool = Pydantic input model + handler + description (`app/tools/base.py: Tool`).
- Model: `claude-opus-5-5` with adaptive thinking; depth is controlled by `ANTHROPIC_EFFORT`.

## Code rules
- **Type hints everywhere**, including return types. Code must pass `mypy` (strict).
- **Pydantic models** for all data crossing a boundary: API requests/responses, tool inputs/outputs,
  data from external APIs. No raw dicts passed around.
- **Logging, not print.** Use `logger = logging.getLogger(__name__)` per module.
  Never log secrets or full email bodies at INFO level.
- **No secrets in code.** All keys and tokens come from `app.config.Settings` (env / `.env`).
  Secrets are typed as `SecretStr`. `.env` is never committed; update `.env.example` when adding settings.
- **The agent never sends email.** Drafts are stored as `pending_approval`; sending happens only in
  `app/outbox.py` after a human approves. Never give the agent a send tool.
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
uv run python -m app.agent "Name" https://example.com   # run the agent on one company
uv run python -m app.cli run data/sample_leads.csv [--limit N]  # import CSV + run new leads
uv run python -m app.outbox list|show ID|approve ID [--to addr]|reject ID  # review drafts
uv run pytest                             # run tests
uv run ruff check .                       # lint
uv run ruff format .                      # format
uv run mypy app                           # type check
```
