"""SQLite storage (SQLAlchemy 2.0): run log, leads and email drafts."""

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from sqlalchemy import Engine, ForeignKey, String, Text, create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from app.domains import company_domain
from app.models.lead import LeadResult
from app.models.outreach import EmailStatus, LeadStage

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class LeadRecord(Base):
    """One agent run for one company (audit log). Key fields as columns, full result as JSON."""

    __tablename__ = "lead_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    company_name: Mapped[str] = mapped_column(String(255))
    website: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(32))
    score: Mapped[int | None]
    fit: Mapped[str | None] = mapped_column(String(8))
    crm_status: Mapped[str | None] = mapped_column(String(16))
    deal_id: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)
    steps: Mapped[int]
    total_input_tokens: Mapped[int]
    output_tokens: Mapped[int]
    result_json: Mapped[str] = mapped_column(Text)

    def to_result(self) -> LeadResult:
        return LeadResult.model_validate_json(self.result_json)


class Lead(Base):
    """Current state of a company in the pipeline. One row per domain."""

    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    website: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(32), default=LeadStage.NEW)
    score: Mapped[int | None]
    fit: Mapped[str | None] = mapped_column(String(8))
    contact_name: Mapped[str | None] = mapped_column(String(255))
    contact_email: Mapped[str | None] = mapped_column(String(320))
    last_run_id: Mapped[str | None] = mapped_column(String(36))
    last_result_id: Mapped[int | None] = mapped_column(ForeignKey("lead_results.id"))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)

    emails: Mapped[list["Email"]] = relationship(back_populates="lead", order_by="Email.id")


class Email(Base):
    """An outreach email draft and what happened to it."""

    __tablename__ = "emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    # The agent run that wrote this draft, so a reviewer can see why the lead qualified.
    lead_result_id: Mapped[int | None] = mapped_column(ForeignKey("lead_results.id"))
    subject: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text)
    to_address: Mapped[str | None] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(32), default=EmailStatus.PENDING_APPROVAL)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    reviewed_by: Mapped[str | None] = mapped_column(String(100))
    reviewed_at: Mapped[datetime | None]
    sent_at: Mapped[datetime | None]
    message_id: Mapped[str | None] = mapped_column(String(255))
    error: Mapped[str | None] = mapped_column(Text)

    lead: Mapped[Lead] = relationship(back_populates="emails")


class Persisted(NamedTuple):
    lead_id: int
    email_id: int | None


def make_engine(database_url: str) -> Engine:
    """Create the engine and any missing tables. For SQLite files, create the folder too."""
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    # FastAPI runs sync endpoints in a thread pool, so SQLite connections cross threads.
    connect_args = {"check_same_thread": False} if url.get_backend_name() == "sqlite" else {}
    engine = create_engine(url, connect_args=connect_args)
    Base.metadata.create_all(engine)
    return engine


def open_session(engine: Engine) -> Session:
    # expire_on_commit=False: returned objects stay readable after the session closes.
    return Session(engine, expire_on_commit=False)


def _run_record(run_id: str, result: LeadResult) -> LeadRecord:
    return LeadRecord(
        run_id=run_id,
        company_name=result.company.name,
        website=result.company.website,
        status=result.status,
        score=result.qualification.score if result.qualification else None,
        fit=result.qualification.fit if result.qualification else None,
        crm_status=result.crm.status if result.crm else None,
        deal_id=result.crm.deal_id if result.crm else None,
        error=result.error,
        steps=result.steps,
        total_input_tokens=result.usage.total_input_tokens,
        output_tokens=result.usage.output_tokens,
        result_json=result.model_dump_json(),
    )


def persist_result(engine: Engine, run_id: str, result: LeadResult) -> Persisted:
    """Log the run, update the lead and queue its email draft for human approval.

    Called right after each company, so an interrupted batch keeps earlier results.
    """
    with open_session(engine) as session, session.begin():
        run_record = _run_record(run_id, result)
        session.add(run_record)

        domain = company_domain(result.company.website)
        lead = session.scalar(select(Lead).where(Lead.domain == domain))
        if lead is None:
            lead = Lead(
                domain=domain,
                name=result.company.name,
                website=result.company.website,
                status=LeadStage.NEW,
            )
            session.add(lead)
        session.flush()  # assigns run_record.id
        lead.last_run_id = run_id
        lead.last_result_id = run_record.id
        lead.last_error = result.error
        # Keep an imported contact; only fill in what this run actually found.
        if result.contact and result.contact.email:
            lead.contact_email = result.contact.email
            lead.contact_name = result.contact.name or lead.contact_name

        if lead.status == LeadStage.SENT:
            # Never queue a second cold email for a company we already contacted.
            if result.email:
                logger.warning("Lead %s was already emailed; new draft not queued", domain)
            session.flush()
            return Persisted(lead.id, None)

        if result.qualification:
            lead.score = result.qualification.score
            lead.fit = result.qualification.fit
            lead.status = (
                LeadStage.DISQUALIFIED
                if result.qualification.fit == "cold"
                else LeadStage.QUALIFIED
            )
            # A fresh qualification replaces drafts nobody has reviewed yet.
            session.flush()
            for old in session.scalars(
                select(Email).where(
                    Email.lead_id == lead.id, Email.status == EmailStatus.PENDING_APPROVAL
                )
            ):
                old.status = EmailStatus.SUPERSEDED

        email = None
        if result.email and lead.status == LeadStage.QUALIFIED:
            session.flush()  # assigns run_record.id
            email = Email(
                lead=lead,
                lead_result_id=run_record.id,
                subject=result.email.subject,
                body=result.email.body,
                to_address=lead.contact_email,
                status=EmailStatus.PENDING_APPROVAL,
            )
            session.add(email)
            lead.status = LeadStage.PENDING_APPROVAL

        session.flush()
        return Persisted(lead.id, email.id if email else None)


def load_run(engine: Engine, run_id: str) -> list[LeadRecord]:
    with open_session(engine) as session:
        query = select(LeadRecord).where(LeadRecord.run_id == run_id).order_by(LeadRecord.id)
        return list(session.scalars(query))
