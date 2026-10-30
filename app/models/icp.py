from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Seller(BaseModel):
    """The company on whose behalf the agent sells."""

    company: str
    country: str
    product: str
    value_proposition: str
    sender_name: str
    sender_title: str


class CompanySize(BaseModel):
    min_employees: int = Field(ge=0)
    max_employees: int = Field(ge=0)


class Signal(BaseModel):
    """A buying signal (positive) or disqualifier (negative) the agent looks for."""

    id: str
    description: str
    weight: int = Field(ge=0)


class Signals(BaseModel):
    positive: list[Signal] = []
    negative: list[Signal] = []


class Scoring(BaseModel):
    industry: int
    company_size: int
    country: int
    max_signal_points: int


class FitThresholds(BaseModel):
    hot: int
    warm: int


class ICP(BaseModel):
    """Ideal Customer Profile used to qualify leads."""

    seller: Seller
    industries: list[str]
    company_size: CompanySize
    countries: list[str]
    signals: Signals
    scoring: Scoring
    fit_thresholds: FitThresholds


def load_icp(path: Path) -> ICP:
    """Load and validate the ICP from a YAML file."""
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return ICP.model_validate(data)
