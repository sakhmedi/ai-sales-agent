import json
import logging

from pydantic import BaseModel

from app.llm import FALLBACK_BETA
from app.models.icp import ICP
from app.models.lead import CompanyInfo, ContactInfo, EmailDraft, Qualification
from app.tools.base import Tool, ToolContext, ToolError

logger = logging.getLogger(__name__)

# Room for adaptive thinking plus a ~120-word email.
MAX_TOKENS = 8000


class DraftEmailInput(BaseModel):
    lead: CompanyInfo
    qualification: Qualification
    contact: ContactInfo | None = None


class EmailContent(BaseModel):
    """What the model writes. The signature is added by code, so it is always correct."""

    subject: str
    body: str


def build_email_system_prompt(icp: ICP) -> str:
    seller = icp.seller
    return f"""You write cold outreach emails for {seller.company} ({seller.product}).
Value proposition: {seller.value_proposition}

Rules:
- Body: 70-120 words, plain text, short paragraphs, no markdown, no emojis.
- Open with one specific fact about the company taken from the evidence you are given.
  Put it in your own words; never copy signal names or category labels.
- Use only the facts you are given. Never invent numbers, names, customers or events.
- Connect that fact to one concrete finance problem the product solves, then state the value
  briefly. End with one low-pressure call to action: a 20-minute call.
- Greeting: "Hi <first name>," if a contact name is given, otherwise "Hi <company> team,".
- Do not write a sign-off or signature; it is added automatically.
- Subject: at most 8 words, specific to the company, no clickbait, no exclamation marks.
- Write in English."""


def build_email_prompt(params: DraftEmailInput, icp: ICP) -> str:
    """The facts the email may use, as JSON."""
    lead = params.lead
    descriptions = {s.id: s.description for s in icp.signals.positive}
    facts = {
        "company": lead.name,
        "industry": lead.industry,
        "country": lead.country,
        "description": lead.description,
        "evidence": [
            {"signal": descriptions[s.id], "fact": s.evidence}
            for s in lead.signals
            if s.id in descriptions
        ],
        "contact": params.contact.model_dump(exclude_none=True) if params.contact else None,
    }
    return "Write the email using only these facts:\n" + json.dumps(facts, indent=2)


def signature(icp: ICP) -> str:
    seller = icp.seller
    return f"Best regards,\n{seller.sender_name}\n{seller.sender_title}, {seller.company}"


def draft_email(params: DraftEmailInput, ctx: ToolContext) -> EmailDraft:
    """Ask Claude for a personalized cold email grounded in the evidence found on the website."""
    response = ctx.llm.beta.messages.parse(
        model=ctx.settings.anthropic_model,
        max_tokens=MAX_TOKENS,
        system=build_email_system_prompt(ctx.icp),
        messages=[{"role": "user", "content": build_email_prompt(params, ctx.icp)}],
        output_format=EmailContent,
        output_config={"effort": ctx.settings.anthropic_effort},
        fallbacks="default",
        betas=[FALLBACK_BETA],
    )
    ctx.record_usage(response.usage)
    logger.info(
        "Email draft: stop_reason=%s tokens in=%d out=%d",
        response.stop_reason,
        response.usage.input_tokens,
        response.usage.output_tokens,
    )

    content = response.parsed_output
    if response.stop_reason == "refusal" or content is None:
        raise ToolError(f"The email writer returned no draft (stop_reason={response.stop_reason}).")

    body = f"{content.body.strip()}\n\n{signature(ctx.icp)}"
    return EmailDraft(subject=content.subject.strip(), body=body)


TOOL = Tool(
    name="draft_email",
    description=(
        "Write a personalized cold outreach email for a qualified lead (hot or warm), based on "
        "the facts and signal evidence in the lead. Pass the contact if you found one. "
        "Do not call it for cold leads."
    ),
    input_model=DraftEmailInput,
    handler=draft_email,
)
