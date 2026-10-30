"""Lead/email lifecycle and human approval. No network: a fake sender records sends."""

from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from app.config import Settings
from app.models.lead import (
    CompanyInput,
    ContactInfo,
    EmailDraft,
    LeadResult,
    LeadStatus,
    Qualification,
)
from app.models.outreach import EmailStatus, LeadStage
from app.outbox import OutboxError, approve, list_pending, reject
from app.storage import Email, Lead, make_engine, open_session, persist_result
from tests.conftest import FakeSender

LEMAN = CompanyInput(name="LEMAN", website="https://leman.com")


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return make_engine(f"sqlite:///{tmp_path / 'test.db'}")


@pytest.fixture
def live_settings(settings: Settings) -> Settings:
    settings.dry_run = False
    return settings


def result(
    fit: str = "warm",
    email: bool = True,
    contact_email: str | None = None,
    status: LeadStatus = LeadStatus.COMPLETED,
) -> LeadResult:
    return LeadResult(
        company=LEMAN,
        status=status,
        qualification=Qualification(score=58 if fit != "cold" else 10, fit=fit, reasons=["+30"]),  # type: ignore[arg-type]
        email=EmailDraft(subject="Growth at LEMAN", body="Hi LEMAN team, ...") if email else None,
        contact=ContactInfo(name="Anna", email=contact_email) if contact_email else None,
    )


def lead(engine: Engine) -> Lead:
    with open_session(engine) as session:
        found = session.scalar(select(Lead).where(Lead.domain == "leman.com"))
        assert found is not None
        return found


def email(engine: Engine, email_id: int) -> Email:
    with open_session(engine) as session:
        found = session.get(Email, email_id)
        assert found is not None
        return found


# --- persist_result: lead and email statuses ---


def test_qualified_lead_with_draft_waits_for_approval(engine: Engine) -> None:
    persisted = persist_result(engine, "run-1", result(contact_email="anna@leman.com"))

    assert persisted.email_id is not None
    assert lead(engine).status == LeadStage.PENDING_APPROVAL
    draft = email(engine, persisted.email_id)
    assert draft.status == EmailStatus.PENDING_APPROVAL
    assert draft.to_address == "anna@leman.com"
    assert [e.id for e in list_pending(engine)] == [persisted.email_id]


def test_cold_lead_is_disqualified_without_draft(engine: Engine) -> None:
    persisted = persist_result(engine, "run-1", result(fit="cold", email=False))

    assert persisted.email_id is None
    assert lead(engine).status == LeadStage.DISQUALIFIED


def test_qualified_lead_without_draft_is_qualified(engine: Engine) -> None:
    persist_result(engine, "run-1", result(email=False))

    assert lead(engine).status == LeadStage.QUALIFIED


def test_new_qualification_supersedes_unreviewed_draft(engine: Engine) -> None:
    first = persist_result(engine, "run-1", result())
    second = persist_result(engine, "run-2", result())

    assert first.email_id is not None
    assert email(engine, first.email_id).status == EmailStatus.SUPERSEDED
    assert [e.id for e in list_pending(engine)] == [second.email_id]


def test_failed_run_keeps_lead_status(engine: Engine) -> None:
    persist_result(engine, "run-1", result())
    failed = LeadResult(company=LEMAN, status=LeadStatus.FAILED, error="API error 500")

    persist_result(engine, "run-2", failed)

    assert lead(engine).status == LeadStage.PENDING_APPROVAL
    assert lead(engine).last_error == "API error 500"
    assert len(list_pending(engine)) == 1


def test_already_emailed_lead_gets_no_second_draft(engine: Engine, live_settings: Settings) -> None:
    first = persist_result(engine, "run-1", result(contact_email="anna@leman.com"))
    assert first.email_id is not None
    approve(engine, first.email_id, live_settings, FakeSender(), reviewer="salima")

    again = persist_result(engine, "run-2", result())

    assert again.email_id is None
    assert lead(engine).status == LeadStage.SENT
    assert list_pending(engine) == []


# --- approve / reject ---


def test_dry_run_approval_sends_nothing_and_keeps_status(
    engine: Engine, settings: Settings
) -> None:
    email_id = persist_result(engine, "run-1", result()).email_id
    assert email_id is not None
    sender = FakeSender()

    outcome = approve(engine, email_id, settings, sender, reviewer="salima", to="anna@leman.com")

    assert outcome.status == "dry_run"
    assert sender.sent == []
    assert email(engine, email_id).status == EmailStatus.PENDING_APPROVAL
    assert lead(engine).status == LeadStage.PENDING_APPROVAL


def test_approval_sends_and_records_who_approved(engine: Engine, live_settings: Settings) -> None:
    email_id = persist_result(engine, "run-1", result()).email_id
    assert email_id is not None
    sender = FakeSender()

    outcome = approve(
        engine, email_id, live_settings, sender, reviewer="salima", to="anna@leman.com"
    )

    assert outcome.status == "sent"
    assert sender.sent == [("anna@leman.com", "Growth at LEMAN", "Hi LEMAN team, ...")]
    sent = email(engine, email_id)
    assert sent.status == EmailStatus.SENT
    assert (sent.reviewed_by, sent.to_address, sent.message_id) == (
        "salima",
        "anna@leman.com",
        "<msg-1@fjordflow.dk>",
    )
    assert sent.sent_at is not None
    assert lead(engine).status == LeadStage.SENT


def test_approval_needs_a_recipient(engine: Engine, live_settings: Settings) -> None:
    email_id = persist_result(engine, "run-1", result()).email_id  # no contact found
    assert email_id is not None

    with pytest.raises(OutboxError, match="--to"):
        approve(engine, email_id, live_settings, FakeSender(), reviewer="salima")


def test_email_cannot_be_sent_twice(engine: Engine, live_settings: Settings) -> None:
    email_id = persist_result(engine, "run-1", result(contact_email="anna@leman.com")).email_id
    assert email_id is not None
    sender = FakeSender()
    approve(engine, email_id, live_settings, sender, reviewer="salima")

    with pytest.raises(OutboxError, match="'sent'"):
        approve(engine, email_id, live_settings, sender, reviewer="salima")
    assert len(sender.sent) == 1


def test_smtp_failure_marks_email_failed(engine: Engine, live_settings: Settings) -> None:
    email_id = persist_result(engine, "run-1", result(contact_email="anna@leman.com")).email_id
    assert email_id is not None

    outcome = approve(engine, email_id, live_settings, FakeSender(fail=True), reviewer="salima")

    assert outcome.status == "failed"
    failed = email(engine, email_id)
    assert failed.status == EmailStatus.FAILED
    assert failed.error == "SMTP login failed."
    assert lead(engine).status == LeadStage.PENDING_APPROVAL


def test_reject_keeps_lead_qualified(engine: Engine) -> None:
    email_id = persist_result(engine, "run-1", result()).email_id
    assert email_id is not None

    reject(engine, email_id, reviewer="salima")

    rejected = email(engine, email_id)
    assert (rejected.status, rejected.reviewed_by) == (EmailStatus.REJECTED, "salima")
    assert lead(engine).status == LeadStage.QUALIFIED
    assert list_pending(engine) == []


def test_failed_email_can_be_approved_again(engine: Engine, live_settings: Settings) -> None:
    email_id = persist_result(engine, "run-1", result(contact_email="anna@leman.com")).email_id
    assert email_id is not None
    approve(engine, email_id, live_settings, FakeSender(fail=True), reviewer="salima")
    assert [e.id for e in list_pending(engine, include_failed=True)] == [email_id]
    assert list_pending(engine) == []  # not a new draft, a failed send

    sender = FakeSender()
    outcome = approve(engine, email_id, live_settings, sender, reviewer="salima")

    assert outcome.status == "sent"
    assert len(sender.sent) == 1
    retried = email(engine, email_id)
    assert (retried.status, retried.error) == (EmailStatus.SENT, None)
    assert lead(engine).status == LeadStage.SENT


def test_failed_email_can_be_rejected(engine: Engine, live_settings: Settings) -> None:
    email_id = persist_result(engine, "run-1", result(contact_email="anna@leman.com")).email_id
    assert email_id is not None
    approve(engine, email_id, live_settings, FakeSender(fail=True), reviewer="salima")

    reject(engine, email_id, reviewer="salima")

    assert email(engine, email_id).status == EmailStatus.REJECTED
    assert lead(engine).status == LeadStage.QUALIFIED
