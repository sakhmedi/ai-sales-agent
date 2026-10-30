"""Process a CSV list of companies and store every result in SQLite.

Usage: uv run python -m app.batch data/companies.csv [--limit N]

The CSV needs `name` and `website` columns. Comma and semicolon separators both work
(Excel in many European locales saves with semicolons).
"""

import argparse
import csv
import logging
import uuid
from collections import Counter
from pathlib import Path
from typing import Protocol

import anthropic
import httpx
from pydantic import BaseModel
from sqlalchemy import Engine

from app.agent.loop import SalesAgent
from app.config import get_settings
from app.integrations.hubspot import company_domain
from app.logging_config import setup_logging
from app.models.icp import load_icp
from app.models.lead import CompanyInput, LeadResult, LeadStatus, TokenUsage
from app.storage import make_engine, persist_result

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = {"name", "website"}


class LeadProcessor(Protocol):
    def run(self, company: CompanyInput) -> LeadResult: ...


class BatchSummary(BaseModel):
    run_id: str
    total: int
    by_status: dict[str, int]
    by_fit: dict[str, int]
    usage: TokenUsage


def load_companies(path: Path) -> list[CompanyInput]:
    """Read companies from CSV. Skips blank and incomplete rows and duplicate domains."""
    # utf-8-sig drops the BOM that Excel adds to UTF-8 files.
    with path.open(newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(sample, delimiters=",;")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(f, dialect=dialect)
        columns = {c.strip().lower() for c in reader.fieldnames or []}
        missing = REQUIRED_COLUMNS - columns
        if missing:
            raise ValueError(f"{path} is missing columns: {', '.join(sorted(missing))}")

        companies: list[CompanyInput] = []
        seen_domains: set[str] = set()
        for line, row in enumerate(reader, start=2):
            values = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            name, website = values.get("name", ""), values.get("website", "")
            if not name and not website:
                continue
            if not name or not website:
                logger.warning("Line %d skipped: both name and website are required", line)
                continue
            domain = company_domain(website)
            if domain in seen_domains:
                logger.warning("Line %d skipped: duplicate domain %s", line, domain)
                continue
            seen_domains.add(domain)
            companies.append(CompanyInput(name=name, website=website))
    return companies


def run_batch(
    agent: LeadProcessor,
    companies: list[CompanyInput],
    engine: Engine,
    run_id: str | None = None,
) -> BatchSummary:
    """Process companies one by one. One failing company never stops the batch."""
    run_id = run_id or str(uuid.uuid4())
    statuses: Counter[str] = Counter()
    fits: Counter[str] = Counter()
    usage = TokenUsage()

    for i, company in enumerate(companies, start=1):
        logger.info("### Batch %s: company %d/%d", run_id[:8], i, len(companies))
        try:
            result = agent.run(company)
        except Exception as e:
            logger.exception("Unexpected error while processing %s", company.name)
            result = LeadResult(company=company, status=LeadStatus.FAILED, error=repr(e))

        persist_result(engine, run_id, result)
        statuses[result.status] += 1
        if result.qualification:
            fits[result.qualification.fit] += 1
        usage.merge(result.usage)

    summary = BatchSummary(
        run_id=run_id,
        total=len(companies),
        by_status=dict(statuses),
        by_fit=dict(fits),
        usage=usage,
    )
    logger.info("### Batch done: %s", summary.model_dump_json())
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Process a CSV of companies with the AI SDR agent."
    )
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--limit", type=int, help="Process only the first N companies.")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level)

    companies = load_companies(args.csv_path)[: args.limit]
    logger.info("Loaded %d companies from %s", len(companies), args.csv_path)
    if not settings.dry_run:
        logger.warning("DRY_RUN is off: results for %d companies go to HubSpot", len(companies))

    engine = make_engine(settings.database_url)
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value())
    with httpx.Client() as http:
        agent = SalesAgent(client, settings, load_icp(settings.icp_path), http)
        summary = run_batch(agent, companies, engine)

    print(summary.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
