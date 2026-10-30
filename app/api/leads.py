from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import Engine

from app.api.deps import get_agent, get_engine, get_sender
from app.config import Settings, get_settings
from app.models.api import (
    ApprovalOut,
    ApproveRequest,
    EmailOut,
    LeadDetail,
    LeadOut,
    LeadRunResponse,
    ReviewRequest,
    RunOut,
)
from app.models.outreach import LeadStage
from app.outbox import EmailSender, MissingRecipient, OutboxError
from app.pipeline import (
    BatchSummary,
    ImportSummary,
    LeadNotFound,
    LeadProcessor,
    NoPendingEmail,
    approve_lead,
    get_lead,
    import_leads,
    list_leads,
    parse_leads_csv,
    qualification_reasons,
    reject_lead,
    run_lead,
    run_new_leads,
)

router = APIRouter(prefix="/leads", tags=["leads"])

EngineDep = Annotated[Engine, Depends(get_engine)]
AgentDep = Annotated[LeadProcessor, Depends(get_agent)]


def _not_found(e: Exception) -> HTTPException:
    return HTTPException(status_code=404, detail=str(e))


@router.post("/import", response_model=ImportSummary)
def import_csv(engine: EngineDep, file: Annotated[UploadFile, File()]) -> ImportSummary:
    """Upload a CSV with company_name, website and optional contact_name, contact_email."""
    try:
        text = file.file.read().decode("utf-8-sig")
        rows, warnings = parse_leads_csv(text)
    except (UnicodeDecodeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid CSV: {e}") from e
    return import_leads(engine, rows, warnings)


@router.get("", response_model=list[LeadOut])
def get_leads(
    engine: EngineDep,
    fit: Annotated[str | None, Query(pattern="^(hot|warm|cold)$")] = None,
    status: LeadStage | None = None,
) -> list[LeadOut]:
    """List leads, best score first. Filter by fit (hot/warm/cold) and/or pipeline status."""
    return [LeadOut.model_validate(lead) for lead in list_leads(engine, fit, status)]


@router.post("/run-batch", response_model=BatchSummary)
def run_batch(
    engine: EngineDep, agent: AgentDep, limit: Annotated[int, Query(ge=1, le=50)] = 10
) -> BatchSummary:
    """Run the agent on leads with status `new`, one after another.

    Synchronous: the request waits for the whole batch (~20 s per lead). In production
    this would be a background job queue.
    """
    return run_new_leads(engine, agent, limit)


@router.get("/{lead_id}", response_model=LeadDetail)
def get_lead_detail(engine: EngineDep, lead_id: int) -> LeadDetail:
    """A lead with its latest email draft and why it qualified: what a reviewer needs."""
    try:
        lead = get_lead(engine, lead_id)
    except LeadNotFound as e:
        raise _not_found(e) from e
    email = lead.emails[-1] if lead.emails else None
    return LeadDetail(
        **LeadOut.model_validate(lead).model_dump(),
        reasons=qualification_reasons(engine, lead),
        email=EmailOut.model_validate(email) if email else None,
    )


@router.post("/{lead_id}/run", response_model=LeadRunResponse)
def run_one(engine: EngineDep, agent: AgentDep, lead_id: int) -> LeadRunResponse:
    """Run the agent on one lead (~20 s). An email draft, if any, waits for approval."""
    try:
        result = run_lead(engine, agent, lead_id)
        lead = get_lead(engine, lead_id)
    except LeadNotFound as e:
        raise _not_found(e) from e
    return LeadRunResponse(
        lead=LeadOut.model_validate(lead),
        run=RunOut(
            status=result.status, steps=result.steps, summary=result.summary, error=result.error
        ),
    )


@router.post("/{lead_id}/approve", response_model=ApprovalOut)
def approve(
    engine: EngineDep,
    lead_id: int,
    body: ApproveRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    sender: Annotated[EmailSender | None, Depends(get_sender)],
) -> ApprovalOut:
    """A human approves the lead's pending email: it is sent (in DRY_RUN only logged)."""
    try:
        result = approve_lead(engine, lead_id, settings, sender, body.reviewer, body.to)
    except (LeadNotFound, NoPendingEmail) as e:
        raise _not_found(e) from e
    except MissingRecipient as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except OutboxError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return ApprovalOut(
        email_id=result.email_id,
        status=result.status,
        recipient=result.recipient,
        error=result.error,
    )


@router.post("/{lead_id}/reject", status_code=204)
def reject(engine: EngineDep, lead_id: int, body: ReviewRequest) -> None:
    """A human rejects the lead's pending email. The lead stays qualified."""
    try:
        reject_lead(engine, lead_id, body.reviewer)
    except (LeadNotFound, NoPendingEmail) as e:
        raise _not_found(e) from e
