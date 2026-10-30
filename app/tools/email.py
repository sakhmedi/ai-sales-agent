from pydantic import BaseModel

from app.models.lead import CompanyInfo, EmailDraft, Qualification
from app.tools.base import Tool, ToolContext


class DraftEmailInput(BaseModel):
    lead: CompanyInfo
    qualification: Qualification


def draft_email(params: DraftEmailInput, ctx: ToolContext) -> EmailDraft:
    """Build a personalized cold email from a template.

    Placeholder: will be replaced by a model-written draft.
    """
    lead = params.lead
    seller = ctx.icp.seller
    signals = {s.id: s.description for s in ctx.icp.signals.positive}
    hooks = [signals[s] for s in lead.signals if s in signals]

    opener = f"I came across {lead.name}"
    if lead.description:
        opener += f" - {lead.description.rstrip('.')}"
    opener += "."

    hook_line = f"\n\nOne thing that stood out: {hooks[0].lower()}." if hooks else ""

    body = (
        f"Hi {lead.name} team,\n\n"
        f"{opener}{hook_line}\n\n"
        f"{seller.value_proposition}\n\n"
        "Would a 20-minute call next week be useful to see if this fits your finance team?\n\n"
        f"Best regards,\n{seller.sender_name}\n{seller.sender_title}, {seller.company}"
    )
    subject = f"{lead.name} + {seller.company}: less manual finance work"
    return EmailDraft(subject=subject, body=body)


TOOL = Tool(
    name="draft_email",
    description=(
        "Write a personalized cold outreach email for a qualified lead (hot or warm). "
        "Do not call it for cold leads."
    ),
    input_model=DraftEmailInput,
    handler=draft_email,
)
