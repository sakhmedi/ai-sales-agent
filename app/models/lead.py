from enum import StrEnum
from typing import Literal

from anthropic.types.beta import BetaUsage
from pydantic import BaseModel, Field, computed_field


class CompanyInput(BaseModel):
    """A company from the input list, before any research."""

    name: str
    website: str


class SignalEvidence(BaseModel):
    """An ICP signal and the concrete fact on the website that shows it."""

    id: str = Field(description="ICP signal ID.")
    evidence: str = Field(
        min_length=1,
        description="Short fact or quote from the website, e.g. 'Added 161 new trailers in 2026'.",
    )


class CompanyInfo(BaseModel):
    """Structured facts about a company, extracted by the agent from its website."""

    name: str
    website: str
    industry: str | None = Field(
        default=None, description="One of the ICP industry labels, or 'other'."
    )
    employee_count: int | None = Field(default=None, description="Best estimate, if known.")
    country: str | None = Field(default=None, description="ISO 3166-1 alpha-2 code, e.g. 'DK'.")
    description: str = Field(default="", description="One or two sentences on what they do.")
    signals: list[SignalEvidence] = Field(
        default=[], description="ICP signals (positive or negative) found, each with evidence."
    )


Fit = Literal["hot", "warm", "cold"]


class Qualification(BaseModel):
    """Result of scoring a company against the ICP."""

    score: int = Field(ge=0, le=100)
    fit: Fit
    reasons: list[str]


class ContactInfo(BaseModel):
    """A real person shown on the company website. Never invented."""

    name: str | None = None
    email: str | None = Field(default=None, description="Only if shown on the website.")
    job_title: str | None = None


class EmailDraft(BaseModel):
    subject: str
    body: str


class CrmSaveResult(BaseModel):
    """What was (or, in dry-run mode, would have been) written to the CRM."""

    status: Literal["dry_run", "saved"]
    message: str
    company_id: str | None = None
    contact_id: str | None = None
    deal_id: str | None = None
    note_id: str | None = None
    created_company: bool = False
    created_deal: bool = False


class TokenUsage(BaseModel):
    """Token counts summed over all model calls for one lead.

    With prompt caching the API splits input into three parts: input_tokens (new,
    uncached), cache_read_input_tokens and cache_creation_input_tokens.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_input_tokens(self) -> int:
        """Everything the model read, cached or not."""
        return self.input_tokens + self.cache_read_input_tokens + self.cache_creation_input_tokens

    def add(self, usage: BetaUsage) -> None:
        """Add the usage of one API response."""
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_input_tokens += usage.cache_read_input_tokens or 0
        self.cache_creation_input_tokens += usage.cache_creation_input_tokens or 0

    def merge(self, other: "TokenUsage") -> None:
        """Add another total, e.g. one lead's usage to a batch total."""
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens


class LeadStatus(StrEnum):
    COMPLETED = "completed"
    MAX_STEPS_REACHED = "max_steps_reached"
    FAILED = "failed"


class LeadResult(BaseModel):
    """Final outcome of processing one company."""

    company: CompanyInput
    status: LeadStatus
    company_info: CompanyInfo | None = None
    qualification: Qualification | None = None
    email: EmailDraft | None = None
    crm: CrmSaveResult | None = None
    summary: str = ""
    steps: int = 0
    usage: TokenUsage = TokenUsage()
    error: str | None = None
