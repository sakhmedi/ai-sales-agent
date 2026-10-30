import logging

from pydantic import BaseModel

from app.models.lead import CompanyInfo, CrmSaveResult, EmailDraft, Qualification
from app.tools.base import Tool, ToolContext

logger = logging.getLogger(__name__)


class SaveToCrmInput(BaseModel):
    lead: CompanyInfo
    qualification: Qualification
    email: EmailDraft | None = None


def save_to_crm(params: SaveToCrmInput, ctx: ToolContext) -> CrmSaveResult:
    """Record the lead in the CRM.

    Placeholder: only logs. HubSpot integration comes later.
    """
    q = params.qualification
    logger.info(
        "CRM (dry_run=%s): %s | score=%d fit=%s | email=%s",
        ctx.settings.dry_run,
        params.lead.name,
        q.score,
        q.fit,
        "yes" if params.email else "no",
    )
    return CrmSaveResult(
        status="logged",
        dry_run=ctx.settings.dry_run,
        message=f"Lead '{params.lead.name}' logged (CRM integration not implemented yet).",
    )


TOOL = Tool(
    name="save_to_crm",
    description=(
        "Save the lead, its qualification and the email draft (if any) to the CRM. "
        "Call it exactly once per company, as the last step."
    ),
    input_model=SaveToCrmInput,
    handler=save_to_crm,
)
