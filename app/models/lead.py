from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, computed_field


class CompanyInput(BaseModel):
    """A company from the input list, before any research."""

    name: str
    website: str


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
    signals: list[str] = Field(
        default=[], description="IDs of ICP signals (positive or negative) found as evidence."
    )


Fit = Literal["hot", "warm", "cold"]


class Qualification(BaseModel):
    """Result of scoring a company against the ICP."""

    score: int = Field(ge=0, le=100)
    fit: Fit
    reasons: list[str]


class EmailDraft(BaseModel):
    subject: str
    body: str


class CrmSaveResult(BaseModel):
    status: Literal["logged"]
    dry_run: bool
    message: str


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
