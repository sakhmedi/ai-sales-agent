import httpx
import pytest

from app.models.icp import ICP
from app.models.lead import CompanyInfo, Qualification
from app.tools import ToolContext, ToolError
from app.tools.crm import SaveToCrmInput, save_to_crm
from app.tools.email import DraftEmailInput, draft_email
from app.tools.qualification import score_company
from app.tools.website import FetchWebsiteInput, fetch_company_website
from tests.conftest import make_http


def ideal_company(**overrides: object) -> CompanyInfo:
    data: dict[str, object] = {
        "name": "Nordic Freight",
        "website": "https://nordicfreight.dk",
        "industry": "logistics",
        "employee_count": 200,
        "country": "DK",
        "description": "Moves goods across the Nordics.",
        "signals": ["hiring_finance", "growth"],
    }
    data.update(overrides)
    return CompanyInfo.model_validate(data)


# --- fetch_company_website ---


def test_fetch_returns_clean_text(ctx: ToolContext) -> None:
    result = fetch_company_website(FetchWebsiteInput(url="nordicfreight.dk"), ctx)

    assert result.url == "https://nordicfreight.dk"
    assert result.title == "Nordic Freight A/S"
    assert result.meta_description == "Logistics for the Nordics"
    assert "hiring an accountant" in result.text
    assert "tracking" not in result.text
    assert "color: red" not in result.text
    assert "  " not in result.text
    assert result.truncated is False


def test_fetch_truncates_long_text(ctx: ToolContext) -> None:
    ctx.settings.fetch_max_chars = 20

    result = fetch_company_website(FetchWebsiteInput(url="https://x.dk"), ctx)

    assert len(result.text) == 20
    assert result.truncated is True


def test_fetch_http_error_raises_tool_error(ctx: ToolContext) -> None:
    http = make_http(lambda request: httpx.Response(404))
    ctx = ToolContext(settings=ctx.settings, icp=ctx.icp, http=http)

    with pytest.raises(ToolError, match="Could not fetch"):
        fetch_company_website(FetchWebsiteInput(url="https://x.dk"), ctx)


def test_fetch_rejects_non_html(ctx: ToolContext) -> None:
    http = make_http(lambda request: httpx.Response(200, json={"a": 1}))
    ctx = ToolContext(settings=ctx.settings, icp=ctx.icp, http=http)

    with pytest.raises(ToolError, match="non-HTML"):
        fetch_company_website(FetchWebsiteInput(url="https://x.dk"), ctx)


def test_fetch_rejects_non_http_scheme(ctx: ToolContext) -> None:
    with pytest.raises(ToolError, match="Invalid URL"):
        fetch_company_website(FetchWebsiteInput(url="file:///etc/passwd"), ctx)


# --- qualify_lead ---


def test_ideal_company_is_hot(icp: ICP) -> None:
    result = score_company(ideal_company(), icp)

    # 30 industry + 25 size + 20 country + 10 hiring + 8 growth
    assert result.score == 93
    assert result.fit == "hot"


def test_unknown_facts_score_zero_points(icp: ICP) -> None:
    info = ideal_company(industry=None, employee_count=None, country=None, signals=[])

    result = score_company(info, icp)

    assert result.score == 0
    assert result.fit == "cold"
    assert "+0: company size unknown" in result.reasons


def test_signal_points_are_capped(icp: ICP) -> None:
    all_positive = [s.id for s in icp.signals.positive]  # 10 + 8 + 7 + 5 = 30

    result = score_company(ideal_company(signals=all_positive), icp)

    assert result.score == 100  # 75 + capped 25


def test_negative_signal_lowers_score(icp: ICP) -> None:
    result = score_company(ideal_company(signals=["has_competitor"]), icp)

    assert result.score == 60
    assert result.fit == "warm"
    assert any(r.startswith("-15") for r in result.reasons)


# --- draft_email / save_to_crm ---


def test_draft_email_is_personalized(ctx: ToolContext) -> None:
    qualification = Qualification(score=93, fit="hot", reasons=[])

    email = draft_email(DraftEmailInput(lead=ideal_company(), qualification=qualification), ctx)

    assert "Nordic Freight" in email.subject
    assert "Moves goods across the Nordics" in email.body
    assert "hiring finance or accounting roles" in email.body
    assert ctx.icp.seller.sender_name in email.body


def test_save_to_crm_only_logs(ctx: ToolContext, caplog: pytest.LogCaptureFixture) -> None:
    qualification = Qualification(score=10, fit="cold", reasons=[])

    with caplog.at_level("INFO"):
        result = save_to_crm(SaveToCrmInput(lead=ideal_company(), qualification=qualification), ctx)

    assert result.status == "logged"
    assert result.dry_run is True
    assert "Nordic Freight" in caplog.text
