"""CSV import, running leads and status transitions. A fake agent stands in for Claude."""

from pathlib import Path

import pytest
from sqlalchemy import Engine

from app.config import Settings
from app.models.lead import (
    CompanyInput,
    ContactInfo,
    EmailDraft,
    LeadResult,
    LeadStatus,
    Qualification,
    TokenUsage,
)
from app.models.outreach import EmailStatus, LeadStage
from app.pipeline import (
    ImportRow,
    LeadNotFound,
    NoPendingEmail,
    approve_lead,
    get_lead,
    import_leads,
    list_leads,
    parse_leads_csv,
    run_lead,
    run_new_leads,
)
from app.storage import make_engine
from tests.conftest import FakeSender

SAMPLE_CSV = Path(__file__).resolve().parents[1] / "data" / "sample_leads.csv"


class FakeAgent:
    """warm + email for most companies, cold for 'Coldco', crashes on 'Crashco'."""

    def __init__(self) -> None:
        self.calls: list[tuple[CompanyInput, ContactInfo | None]] = []

    def run(self, company: CompanyInput, contact: ContactInfo | None = None) -> LeadResult:
        self.calls.append((company, contact))
        if company.name == "Crashco":
            raise RuntimeError("boom")
        cold = company.name == "Coldco"
        return LeadResult(
            company=company,
            status=LeadStatus.COMPLETED,
            contact=contact,
            qualification=Qualification(
                score=10 if cold else 58, fit="cold" if cold else "warm", reasons=["+30: test"]
            ),
            email=None if cold else EmailDraft(subject=f"Hi {company.name}", body="Body"),
            steps=5,
            usage=TokenUsage(input_tokens=100, output_tokens=20),
        )


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return make_engine(f"sqlite:///{tmp_path / 'test.db'}")


def import_csv(engine: Engine, text: str) -> None:
    rows, warnings = parse_leads_csv(text)
    import_leads(engine, rows, warnings)


# --- CSV parsing ---


def test_parse_full_columns_with_contacts() -> None:
    rows, warnings = parse_leads_csv(
        "company_name,website,contact_name,contact_email\n"
        "LEMAN,https://leman.com,Test Contact,test+leman@example.com\n"
    )

    assert rows == [
        ImportRow(
            CompanyInput(name="LEMAN", website="https://leman.com"),
            ContactInfo(name="Test Contact", email="test+leman@example.com"),
        )
    ]
    assert warnings == []


def test_parse_name_alias_and_no_contact_columns() -> None:
    rows, _ = parse_leads_csv("name,website\nLEMAN,https://leman.com\n")

    assert rows == [ImportRow(CompanyInput(name="LEMAN", website="https://leman.com"), None)]


def test_parse_excel_semicolon_csv_with_bom() -> None:
    rows, _ = parse_leads_csv("﻿Company_Name;Website\nLEMAN;https://leman.com\n")

    assert [r.company.name for r in rows] == ["LEMAN"]


def test_parse_skips_blank_incomplete_duplicate_rows_and_bad_emails() -> None:
    rows, warnings = parse_leads_csv(
        "company_name,website,contact_name,contact_email\n"
        "LEMAN,https://leman.com,,\n"
        ",,,\n"
        "No website,,,\n"
        "LEMAN again,https://www.leman.com/about,,\n"
        "Pleo,pleo.io,Test,not-an-email\n"
    )

    assert [r.company.name for r in rows] == ["LEMAN", "Pleo"]
    assert rows[1].contact == ContactInfo(name="Test", email=None)
    assert warnings == [
        "Line 4 skipped: company name and website are required.",
        "Line 5 skipped: duplicate domain leman.com.",
        "Line 6: invalid contact email 'not-an-email' ignored.",
    ]


def test_parse_requires_columns() -> None:
    with pytest.raises(ValueError, match="company_name"):
        parse_leads_csv("company,url\nLEMAN,https://leman.com\n")


def test_sample_csv_has_ten_danish_companies_with_fake_contacts() -> None:
    rows, warnings = parse_leads_csv(SAMPLE_CSV.read_text(encoding="utf-8"))

    assert len(rows) == 10 and warnings == []
    assert all(
        r.contact and r.contact.email and r.contact.email.endswith("@example.com") for r in rows
    )


# --- import ---


def test_import_creates_new_leads_and_skips_known_ones(engine: Engine) -> None:
    rows, warnings = parse_leads_csv(
        "company_name,website,contact_name,contact_email\n"
        "LEMAN,https://leman.com,Test Contact,test+leman@example.com\n"
    )

    first = import_leads(engine, rows, warnings)
    second = import_leads(engine, rows, warnings)

    assert (first.created, first.already_known) == (1, 0)
    assert (second.created, second.already_known) == (0, 1)
    [lead] = list_leads(engine)
    assert (lead.status, lead.contact_email) == (LeadStage.NEW, "test+leman@example.com")


# --- running leads and status transitions ---


def test_run_lead_passes_imported_contact_and_queues_email_to_it(engine: Engine) -> None:
    import_csv(
        engine,
        "company_name,website,contact_name,contact_email\n"
        "LEMAN,https://leman.com,Test Contact,test+leman@example.com\n",
    )
    lead_id = list_leads(engine)[0].id
    agent = FakeAgent()

    run_lead(engine, agent, lead_id)

    assert agent.calls[0][1] == ContactInfo(name="Test Contact", email="test+leman@example.com")
    lead = get_lead(engine, lead_id)
    assert (lead.status, lead.fit, lead.score) == (LeadStage.PENDING_APPROVAL, "warm", 58)
    assert lead.emails[0].to_address == "test+leman@example.com"
    assert lead.emails[0].status == EmailStatus.PENDING_APPROVAL


def test_run_unknown_lead_raises(engine: Engine) -> None:
    with pytest.raises(LeadNotFound):
        run_lead(engine, FakeAgent(), 999)


def test_batch_runs_only_new_leads_and_survives_a_crash(engine: Engine) -> None:
    import_csv(
        engine,
        "company_name,website\nWarmco,warm.dk\nCrashco,crash.dk\nColdco,cold.dk\n",
    )
    agent = FakeAgent()

    summary = run_new_leads(engine, agent)

    assert [c.name for c, _ in agent.calls] == ["Warmco", "Crashco", "Coldco"]
    assert summary.total == 3
    assert summary.by_status == {"completed": 2, "failed": 1}
    assert summary.by_fit == {"warm": 1, "cold": 1}
    assert summary.usage.input_tokens == 200

    by_name = {lead.name: lead for lead in list_leads(engine)}
    assert by_name["Warmco"].status == LeadStage.PENDING_APPROVAL
    assert by_name["Coldco"].status == LeadStage.DISQUALIFIED
    # A crashed lead stays `new`, so the next batch retries it; the error is kept.
    assert by_name["Crashco"].status == LeadStage.NEW
    assert by_name["Crashco"].last_error is not None and "boom" in by_name["Crashco"].last_error

    second = run_new_leads(engine, agent)
    assert second.total == 1  # only the crashed lead is retried


def test_batch_respects_limit(engine: Engine) -> None:
    import_csv(engine, "company_name,website\nA,a.dk\nB,b.dk\nC,c.dk\n")

    assert run_new_leads(engine, FakeAgent(), limit=2).total == 2


def test_list_leads_filters_by_fit(engine: Engine) -> None:
    import_csv(engine, "company_name,website\nWarmco,warm.dk\nColdco,cold.dk\n")
    run_new_leads(engine, FakeAgent())

    assert [lead.name for lead in list_leads(engine, fit="cold")] == ["Coldco"]
    assert [lead.name for lead in list_leads(engine, status="pending_approval")] == ["Warmco"]


# --- approval ---


def test_approve_lead_sends_to_imported_contact(engine: Engine, settings: Settings) -> None:
    settings.dry_run = False
    import_csv(
        engine,
        "company_name,website,contact_name,contact_email\n"
        "LEMAN,https://leman.com,Test Contact,test+leman@example.com\n",
    )
    lead_id = list_leads(engine)[0].id
    run_lead(engine, FakeAgent(), lead_id)
    sender = FakeSender()

    result = approve_lead(engine, lead_id, settings, sender, reviewer="salima")

    assert result.status == "sent"
    assert sender.sent[0][0] == "test+leman@example.com"
    assert get_lead(engine, lead_id).status == LeadStage.SENT
    with pytest.raises(NoPendingEmail):
        approve_lead(engine, lead_id, settings, sender, reviewer="salima")
