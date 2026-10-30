from enum import StrEnum


class LeadStage(StrEnum):
    """Where a lead is in the outreach pipeline.

    new -> qualified (hot/warm) | disqualified (cold) -> pending_approval -> sent
    """

    NEW = "new"
    QUALIFIED = "qualified"
    DISQUALIFIED = "disqualified"
    PENDING_APPROVAL = "pending_approval"
    SENT = "sent"


class EmailStatus(StrEnum):
    """Lifecycle of an email draft. Only a human moves it out of pending_approval."""

    PENDING_APPROVAL = "pending_approval"
    SENT = "sent"
    REJECTED = "rejected"
    FAILED = "failed"
    # A newer qualification replaced this draft before anyone reviewed it.
    SUPERSEDED = "superseded"
