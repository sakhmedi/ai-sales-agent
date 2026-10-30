"""FastAPI dependencies. Tests replace them via app.dependency_overrides."""

from collections.abc import Iterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException
from sqlalchemy import Engine

from app.config import Settings, get_settings
from app.integrations.email import EmailSendError, SmtpSender
from app.outbox import EmailSender
from app.pipeline import LeadProcessor, open_agent
from app.storage import make_engine


@lru_cache
def get_engine() -> Engine:
    return make_engine(get_settings().database_url)


def get_agent(settings: Annotated[Settings, Depends(get_settings)]) -> Iterator[LeadProcessor]:
    with open_agent(settings) as agent:
        yield agent


def get_sender(settings: Annotated[Settings, Depends(get_settings)]) -> EmailSender | None:
    """No sender in DRY_RUN: approving then only logs what would be sent."""
    if settings.dry_run:
        return None
    try:
        return SmtpSender.from_settings(settings)
    except EmailSendError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
