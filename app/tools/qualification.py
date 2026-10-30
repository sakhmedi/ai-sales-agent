from pydantic import BaseModel

from app.models.icp import ICP
from app.models.lead import CompanyInfo, Fit, Qualification
from app.tools.base import Tool, ToolContext


class QualifyLeadInput(BaseModel):
    company_info: CompanyInfo


def score_company(info: CompanyInfo, icp: ICP) -> Qualification:
    """Score a company against the ICP with transparent, rule-based points.

    The model extracts the facts; this code does the scoring, so the result is
    reproducible and every point can be explained.
    """
    points = 0
    reasons: list[str] = []
    weights = icp.scoring

    industries = {i.lower() for i in icp.industries}
    if info.industry and info.industry.lower() in industries:
        points += weights.industry
        reasons.append(f"+{weights.industry}: target industry ({info.industry})")
    else:
        reasons.append(f"+0: industry '{info.industry or 'unknown'}' is not in the ICP")

    size = icp.company_size
    if info.employee_count is None:
        reasons.append("+0: company size unknown")
    elif size.min_employees <= info.employee_count <= size.max_employees:
        points += weights.company_size
        reasons.append(f"+{weights.company_size}: {info.employee_count} employees is in range")
    else:
        reasons.append(
            f"+0: {info.employee_count} employees is outside "
            f"{size.min_employees}-{size.max_employees}"
        )

    if info.country and info.country.upper() in icp.countries:
        points += weights.country
        reasons.append(f"+{weights.country}: target country ({info.country.upper()})")
    else:
        reasons.append(f"+0: country '{info.country or 'unknown'}' is not in the ICP")

    evidence = {s.id: s.evidence for s in info.signals}
    signal_points = 0
    for signal in icp.signals.positive:
        if signal.id in evidence:
            signal_points += signal.weight
            reasons.append(f"+{signal.weight}: {signal.description} ({evidence[signal.id]})")
    points += min(signal_points, weights.max_signal_points)

    for signal in icp.signals.negative:
        if signal.id in evidence:
            points -= signal.weight
            reasons.append(f"-{signal.weight}: {signal.description} ({evidence[signal.id]})")

    score = max(0, min(100, points))
    fit = _fit_for(score, icp)
    # "hot" means "contact first". That needs a confirmed size, not just a good guess.
    if fit == "hot" and info.employee_count is None:
        fit = "warm"
        reasons.append("fit capped at warm: company size not confirmed")
    return Qualification(score=score, fit=fit, reasons=reasons)


def _fit_for(score: int, icp: ICP) -> Fit:
    if score >= icp.fit_thresholds.hot:
        return "hot"
    if score >= icp.fit_thresholds.warm:
        return "warm"
    return "cold"


def qualify_lead(params: QualifyLeadInput, ctx: ToolContext) -> Qualification:
    return score_company(params.company_info, ctx.icp)


TOOL = Tool(
    name="qualify_lead",
    description=(
        "Score a company against the Ideal Customer Profile. Pass the facts you found on its "
        "website; use null for anything you could not confirm. Returns a 0-100 score, a fit "
        "(hot/warm/cold) and the reasons behind each point."
    ),
    input_model=QualifyLeadInput,
    handler=qualify_lead,
)
