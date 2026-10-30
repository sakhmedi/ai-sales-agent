"""Request and response bodies of the leads API."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.lead import LeadStatus


class EmailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    subject: str
    body: str
    to_address: str | None
    created_at: datetime
    reviewed_by: str | None
    sent_at: datetime | None
    error: str | None


class LeadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    website: str
    domain: str
    status: str
    score: int | None
    fit: str | None
    contact_name: str | None
    contact_email: str | None
    last_error: str | None
    updated_at: datetime


class LeadDetail(LeadOut):
    reasons: list[str]
    email: EmailOut | None = Field(description="The latest email draft for this lead, if any.")


class RunOut(BaseModel):
    """Outcome of one agent run."""

    status: LeadStatus
    steps: int
    summary: str
    error: str | None


class LeadRunResponse(BaseModel):
    lead: LeadOut
    run: RunOut


class ReviewRequest(BaseModel):
    reviewer: str = Field(min_length=1, description="Who reviewed the email (stored for audit).")


class ApproveRequest(ReviewRequest):
    to: str | None = Field(
        default=None, description="Recipient. Defaults to the lead's contact email."
    )


class ApprovalOut(BaseModel):
    email_id: int
    status: Literal["dry_run", "sent", "failed"]
    recipient: str
    error: str | None = None
