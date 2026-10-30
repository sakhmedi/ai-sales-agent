"""SQLite storage for lead results (SQLAlchemy 2.0)."""

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, String, Text, create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.models.lead import LeadResult


class Base(DeclarativeBase):
    pass


class LeadRecord(Base):
    """One processed company. Key fields are columns for querying; the full result is JSON."""

    __tablename__ = "lead_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
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


def make_engine(database_url: str) -> Engine:
    """Create the engine and the tables. For SQLite files, create the parent folder too."""
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    return engine


def save_result(engine: Engine, run_id: str, result: LeadResult) -> int:
    """Store one lead result right away, so a crash mid-batch keeps earlier results."""
    record = LeadRecord(
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
    with Session(engine) as session:
        session.add(record)
        session.commit()
        return record.id


def load_run(engine: Engine, run_id: str) -> list[LeadRecord]:
    with Session(engine) as session:
        query = select(LeadRecord).where(LeadRecord.run_id == run_id).order_by(LeadRecord.id)
        return list(session.scalars(query))
