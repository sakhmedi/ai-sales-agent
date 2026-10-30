"""Lead pipeline: import a CSV, run the agent on leads, approve emails.

Shared by the HTTP API (app/api/leads.py) and the CLI (app/cli.py), so both behave the same.
"""

import csv
import io
import logging
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from typing import NamedTuple, Protocol

import anthropic
import httpx
from pydantic import BaseModel
from sqlalchemy import Engine, select
from sqlalchemy.orm import selectinload

from app.agent.loop import SalesAgent
from app.config import Settings
from app.integrations.hubspot import company_domain
from app.models.icp import load_icp
from app.models.lead import CompanyInput, ContactInfo, LeadResult, LeadStatus, TokenUsage
from app.models.outreach import EmailStatus, LeadStage
from app.outbox import ApprovalResult, EmailSender, approve, reject
from app.storage import Email, Lead, LeadRecord, open_session, persist_result

logger = logging.getLogger(__name__)

COMPANY_COLUMNS = ("company_name", "name")


class LeadNotFound(Exception):
    pass


class NoPendingEmail(Exception):
    pass


class LeadProcessor(Protocol):
    def run(self, company: CompanyInput, contact: ContactInfo | None = None) -> LeadResult: ...


class ImportRow(NamedTuple):
    company: CompanyInput
    contact: ContactInfo | None


class ImportSummary(BaseModel):
    created: int
    already_known: int
    warnings: list[str]


class BatchSummary(BaseModel):
    run_id: str
    total: int
    by_status: dict[str, int]
    by_fit: dict[str, int]
    usage: TokenUsage


# --- CSV ---


def parse_leads_csv(text: str) -> tuple[list[ImportRow], list[str]]:
    """Parse a lead list. Returns rows plus human-readable warnings for skipped lines.

    Columns: company_name (or name), website, and optional contact_name, contact_email.
    Comma and semicolon separators both work (Excel in many European locales uses semicolons).
    """
    text = text.removeprefix("﻿")  # BOM that Excel adds to UTF-8 files
    try:
        dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    columns = {(c or "").strip().lower() for c in reader.fieldnames or []}
    if "website" not in columns or not columns & set(COMPANY_COLUMNS):
        raise ValueError("CSV needs the columns company_name (or name) and website.")

    rows: list[ImportRow] = []
    warnings: list[str] = []
    seen_domains: set[str] = set()
    for line, raw in enumerate(reader, start=2):
        values = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        name = values.get("company_name") or values.get("name", "")
        website = values.get("website", "")
        if not any(values.values()):
            continue
        if not name or not website:
            warnings.append(f"Line {line} skipped: company name and website are required.")
            continue
        domain = company_domain(website)
        if domain in seen_domains:
            warnings.append(f"Line {line} skipped: duplicate domain {domain}.")
            continue
        seen_domains.add(domain)

        contact_name = values.get("contact_name") or None
        contact_email = values.get("contact_email") or None
        if contact_email and "@" not in contact_email:
            warnings.append(f"Line {line}: invalid contact email '{contact_email}' ignored.")
            contact_email = None
        contact = (
            ContactInfo(name=contact_name, email=contact_email)
            if contact_name or contact_email
            else None
        )
        rows.append(ImportRow(CompanyInput(name=name, website=website), contact))

    for warning in warnings:
        logger.warning(warning)
    return rows, warnings


def import_leads(engine: Engine, rows: list[ImportRow], warnings: list[str]) -> ImportSummary:
    """Insert new leads with status `new`. Companies already in the database stay as they are."""
    created = already_known = 0
    with open_session(engine) as session, session.begin():
        for row in rows:
            domain = company_domain(row.company.website)
            if session.scalar(select(Lead.id).where(Lead.domain == domain)) is not None:
                already_known += 1
                continue
            session.add(
                Lead(
                    domain=domain,
                    name=row.company.name,
                    website=row.company.website,
                    status=LeadStage.NEW,
                    contact_name=row.contact.name if row.contact else None,
                    contact_email=row.contact.email if row.contact else None,
                )
            )
            created += 1
    logger.info("Imported %d new leads (%d already known)", created, already_known)
    return ImportSummary(created=created, already_known=already_known, warnings=warnings)


# --- Running the agent ---


@contextmanager
def open_agent(settings: Settings) -> Iterator[SalesAgent]:
    """A ready-to-use agent with its own HTTP client, closed when done."""
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value())
    with httpx.Client() as http:
        yield SalesAgent(client, settings, load_icp(settings.icp_path), http)


def run_lead(
    engine: Engine, agent: LeadProcessor, lead_id: int, run_id: str | None = None
) -> LeadResult:
    """Run the agent on one stored lead and save the outcome. Never raises for agent errors."""
    with open_session(engine) as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            raise LeadNotFound(f"Lead {lead_id} does not exist.")
        company = CompanyInput(name=lead.name, website=lead.website)
        contact = (
            ContactInfo(name=lead.contact_name, email=lead.contact_email)
            if lead.contact_name or lead.contact_email
            else None
        )

    try:
        result = agent.run(company, contact)
    except Exception as e:
        logger.exception("Unexpected error while processing %s", company.name)
        result = LeadResult(company=company, status=LeadStatus.FAILED, error=repr(e))

    persist_result(engine, run_id or str(uuid.uuid4()), result)
    return result


def run_new_leads(engine: Engine, agent: LeadProcessor, limit: int | None = None) -> BatchSummary:
    """Process every lead with status `new`, one by one. One failing lead never stops the batch."""
    with open_session(engine) as session:
        query = select(Lead.id).where(Lead.status == LeadStage.NEW).order_by(Lead.id)
        lead_ids = list(session.scalars(query.limit(limit) if limit else query))

    run_id = str(uuid.uuid4())
    statuses: Counter[str] = Counter()
    fits: Counter[str] = Counter()
    usage = TokenUsage()
    for i, lead_id in enumerate(lead_ids, start=1):
        logger.info("### Batch %s: lead %d/%d (id=%d)", run_id[:8], i, len(lead_ids), lead_id)
        result = run_lead(engine, agent, lead_id, run_id)
        statuses[result.status] += 1
        if result.qualification:
            fits[result.qualification.fit] += 1
        usage.merge(result.usage)

    summary = BatchSummary(
        run_id=run_id,
        total=len(lead_ids),
        by_status=dict(statuses),
        by_fit=dict(fits),
        usage=usage,
    )
    logger.info("### Batch done: %s", summary.model_dump_json())
    return summary


# --- Reading and reviewing ---


def list_leads(engine: Engine, fit: str | None = None, status: str | None = None) -> list[Lead]:
    query = select(Lead).order_by(Lead.score.desc().nulls_last(), Lead.id)
    if fit:
        query = query.where(Lead.fit == fit)
    if status:
        query = query.where(Lead.status == status)
    with open_session(engine) as session:
        return list(session.scalars(query))


def get_lead(engine: Engine, lead_id: int) -> Lead:
    with open_session(engine) as session:
        lead = session.get(Lead, lead_id, options=[selectinload(Lead.emails)])
        if lead is None:
            raise LeadNotFound(f"Lead {lead_id} does not exist.")
        return lead


def qualification_reasons(engine: Engine, lead: Lead) -> list[str]:
    """Reasons from the lead's latest agent run."""
    if lead.last_result_id is None:
        return []
    with open_session(engine) as session:
        record = session.get(LeadRecord, lead.last_result_id)
    result = record.to_result() if record else None
    return result.qualification.reasons if result and result.qualification else []


def _pending_email_id(engine: Engine, lead_id: int) -> int:
    get_lead(engine, lead_id)  # raises LeadNotFound
    with open_session(engine) as session:
        email_id = session.scalar(
            select(Email.id)
            .where(Email.lead_id == lead_id, Email.status == EmailStatus.PENDING_APPROVAL)
            .order_by(Email.id.desc())
        )
    if email_id is None:
        raise NoPendingEmail(f"Lead {lead_id} has no email waiting for approval.")
    return email_id


def approve_lead(
    engine: Engine,
    lead_id: int,
    settings: Settings,
    sender: EmailSender | None,
    reviewer: str,
    to: str | None = None,
) -> ApprovalResult:
    """Approve the lead's pending email. Sending itself stays in app/outbox.py."""
    return approve(engine, _pending_email_id(engine, lead_id), settings, sender, reviewer, to)


def reject_lead(engine: Engine, lead_id: int, reviewer: str) -> None:
    reject(engine, _pending_email_id(engine, lead_id), reviewer)
