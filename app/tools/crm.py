import json
import logging
from datetime import UTC, datetime

from pydantic import BaseModel

from app.integrations.hubspot import HubSpotClient, HubSpotError, build_plan
from app.models.lead import CompanyInfo, ContactInfo, CrmSaveResult, EmailDraft, Qualification
from app.tools.base import Tool, ToolContext, ToolError

logger = logging.getLogger(__name__)


class SaveToCrmInput(BaseModel):
    lead: CompanyInfo
    qualification: Qualification
    email: EmailDraft | None = None
    contact: ContactInfo | None = None


def save_to_crm(params: SaveToCrmInput, ctx: ToolContext) -> CrmSaveResult:
    """Save the lead to HubSpot, or only log what would be sent when DRY_RUN is on."""
    settings = ctx.settings
    if settings.dry_run:
        plan = build_plan(
            params.lead,
            params.qualification,
            params.email,
            params.contact,
            pipeline=settings.hubspot_pipeline,
            deal_stage=settings.hubspot_deal_stage,
            now=datetime.now(UTC),
        )
        if plan["note"]:
            # Don't log full email bodies at INFO level.
            body_length = len(plan["note"]["hs_note_body"])
            plan["note"]["hs_note_body"] = f"<{body_length} chars>"
        logger.info(
            "DRY_RUN: would send to HubSpot:\n%s", json.dumps(plan, indent=2, ensure_ascii=False)
        )
        return CrmSaveResult(
            status="dry_run",
            message="DRY_RUN is on: nothing was written to HubSpot.",
        )

    if not settings.hubspot_token.get_secret_value():
        raise ToolError("HUBSPOT_TOKEN is not set. Add it to .env or enable DRY_RUN.")

    try:
        with HubSpotClient.from_settings(settings) as client:
            return client.sync_lead(params.lead, params.qualification, params.email, params.contact)
    except HubSpotError as e:
        raise ToolError(str(e)) from e


TOOL = Tool(
    name="save_to_crm",
    description=(
        "Save the lead, its qualification, the email draft (if any) and a contact person "
        "(only if a real person or email is shown on the website) to the CRM. "
        "Call it exactly once per company, as the last step."
    ),
    input_model=SaveToCrmInput,
    handler=save_to_crm,
)
