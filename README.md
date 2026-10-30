# AI Sales Agent (AI SDR)

An autonomous AI sales development agent: takes a list of companies, researches each one,
qualifies it against ICP criteria, drafts a personalized email and records the result in HubSpot.

Built with Python 3.12, FastAPI, Claude (Anthropic SDK, tool calling), Pydantic, SQLAlchemy and httpx.

## Quick start
```bash
uv sync
cp .env.example .env
uv run uvicorn app.main:app --reload
```
Then open http://127.0.0.1:8000/health.

Run tests with `uv run pytest`.

> Status: early scaffold. See `CLAUDE.md` for architecture and code rules.
