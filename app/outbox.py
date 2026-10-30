"""Human review of email drafts. The only place where emails are sent.

Usage:
  uv run python -m app.outbox list
  uv run python -m app.outbox show ID
  uv run python -m app.outbox approve ID [--to address] [--by name]
  uv run python -m app.outbox reject ID [--by name]
"""

import argparse
import getpass
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, joinedload

from app.config import Settings, get_settings
from app.integrations.email import EmailSendError, SmtpSender
from app.logging_config import setup_logging
from app.models.outreach import EmailStatus, LeadStage
from app.storage import Email, LeadRecord, make_engine, open_session

logger = logging.getLogger(__name__)


class OutboxError(Exception):
    """The requested review action is not allowed."""


class MissingRecipient(OutboxError):
    """There is no address to send the email to."""


class EmailSender(Protocol):
    def send(self, to: str, subject: str, body: str) -> str: ...


@dataclass(frozen=True)
class ApprovalResult:
    email_id: int
    status: Literal["dry_run", "sent", "failed"]
    recipient: str
    error: str | None = None


# A failed send can be approved again (e.g. after fixing SMTP settings) or rejected.
REVIEWABLE = (EmailStatus.PENDING_APPROVAL, EmailStatus.FAILED)


def list_pending(engine: Engine, include_failed: bool = False) -> list[Email]:
    statuses = REVIEWABLE if include_failed else (EmailStatus.PENDING_APPROVAL,)
    with open_session(engine) as session:
        query = (
            select(Email)
            .options(joinedload(Email.lead))
            .where(Email.status.in_(statuses))
            .order_by(Email.id)
        )
        return list(session.scalars(query))


def get_email(engine: Engine, email_id: int) -> Email:
    with open_session(engine) as session:
        email = session.get(Email, email_id, options=[joinedload(Email.lead)])
        if email is None:
            raise OutboxError(f"Email {email_id} does not exist.")
        return email


def _reviewable(session: Session, email_id: int) -> Email:
    email = session.get(Email, email_id, options=[joinedload(Email.lead)])
    if email is None:
        raise OutboxError(f"Email {email_id} does not exist.")
    if email.status not in REVIEWABLE:
        raise OutboxError(
            f"Email {email_id} is '{email.status}'; only pending_approval or failed emails "
            "can be reviewed."
        )
    return email


def approve(
    engine: Engine,
    email_id: int,
    settings: Settings,
    sender: EmailSender | None,
    reviewer: str,
    to: str | None = None,
) -> ApprovalResult:
    """A human approved this draft: send it (or, in DRY_RUN, only log what would be sent).

    Also retries an email whose previous send failed.
    """
    with open_session(engine) as session, session.begin():
        email = _reviewable(session, email_id)
        recipient = (to or email.to_address or "").strip()
        if "@" not in recipient:
            raise MissingRecipient(
                f'Email {email_id} has no valid recipient. Pass one with --to (CLI) or "to" (API).'
            )

        if settings.dry_run:
            logger.info(
                "DRY_RUN: would send email %d to %s\nSubject: %s\n\n%s",
                email_id,
                recipient,
                email.subject,
                email.body,
            )
            return ApprovalResult(email_id, "dry_run", recipient)

        if sender is None:
            raise OutboxError("No email sender configured.")

        now = datetime.now(UTC)
        email.to_address = recipient
        email.reviewed_by = reviewer
        email.reviewed_at = now
        try:
            email.message_id = sender.send(recipient, email.subject, email.body)
        except EmailSendError as e:
            email.status = EmailStatus.FAILED
            email.error = str(e)
            logger.error("Email %d failed: %s", email_id, e)
            return ApprovalResult(email_id, "failed", recipient, str(e))

        email.status = EmailStatus.SENT
        email.sent_at = now
        email.error = None
        email.lead.status = LeadStage.SENT
        logger.info("Email %d approved by %s and sent to %s", email_id, reviewer, recipient)
        return ApprovalResult(email_id, "sent", recipient)


def reject(engine: Engine, email_id: int, reviewer: str) -> Email:
    """A human rejected this draft. The lead stays qualified for a new attempt."""
    with open_session(engine) as session, session.begin():
        email = _reviewable(session, email_id)
        email.status = EmailStatus.REJECTED
        email.reviewed_by = reviewer
        email.reviewed_at = datetime.now(UTC)
        email.lead.status = LeadStage.QUALIFIED
        logger.info("Email %d rejected by %s", email_id, reviewer)
        return email


def _reasons(engine: Engine, lead_result_id: int | None) -> list[str]:
    """Qualification reasons from the agent run that wrote the draft."""
    if lead_result_id is None:
        return []
    with open_session(engine) as session:
        record = session.get(LeadRecord, lead_result_id)
    result = record.to_result() if record else None
    return result.qualification.reasons if result and result.qualification else []


def main() -> None:
    parser = argparse.ArgumentParser(description="Review and send email drafts.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Show emails waiting for approval.")
    show = commands.add_parser("show", help="Show one email with the lead's qualification.")
    show.add_argument("email_id", type=int)
    approve_cmd = commands.add_parser("approve", help="Approve and send an email.")
    approve_cmd.add_argument("email_id", type=int)
    approve_cmd.add_argument("--to", help="Recipient address (required if none was found).")
    approve_cmd.add_argument("--by", default=getpass.getuser(), help="Reviewer name.")
    reject_cmd = commands.add_parser("reject", help="Reject an email draft.")
    reject_cmd.add_argument("email_id", type=int)
    reject_cmd.add_argument("--by", default=getpass.getuser(), help="Reviewer name.")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level)
    engine = make_engine(settings.database_url)

    try:
        if args.command == "list":
            pending = list_pending(engine, include_failed=True)
            if not pending:
                print("No emails waiting for approval.")
            for e in pending:
                failed = (
                    "  [FAILED: approve again to retry]" if e.status == EmailStatus.FAILED else ""
                )
                print(
                    f"#{e.id}  {e.lead.name} ({e.lead.fit}, {e.lead.score})  "
                    f"to={e.to_address or '-'}  {e.subject}{failed}"
                )
        elif args.command == "show":
            e = get_email(engine, args.email_id)
            print(f"Email #{e.id} [{e.status}] for {e.lead.name} ({e.lead.website})")
            print(f"Lead: {e.lead.fit}, score {e.lead.score}")
            for reason in _reasons(engine, e.lead_result_id):
                print(f"  {reason}")
            print(f"To: {e.to_address or '- (pass --to when approving)'}")
            print(f"Subject: {e.subject}\n\n{e.body}")
        elif args.command == "approve":
            sender = None if settings.dry_run else SmtpSender.from_settings(settings)
            result = approve(engine, args.email_id, settings, sender, args.by, args.to)
            print(f"Email #{result.email_id}: {result.status} (to {result.recipient})")
            if result.status == "failed":
                print(f"Error: {result.error}")
                sys.exit(1)
        elif args.command == "reject":
            reject(engine, args.email_id, args.by)
            print(f"Email #{args.email_id}: rejected")
    except (OutboxError, EmailSendError) as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
