from dataclasses import replace

import httpx
import pytest

from app.models.icp import ICP
from app.models.lead import CompanyInfo, ContactInfo, Qualification
from app.tools import ToolContext, ToolError
from app.tools.crm import SaveToCrmInput, save_to_crm
from app.tools.email import DraftEmailInput, draft_email
from app.tools.qualification import score_company
from app.tools.website import FetchWebsiteInput, fetch_company_website
from tests.conftest import FakeEmailWriter, make_http


def ideal_company(**overrides: object) -> CompanyInfo:
    data: dict[str, object] = {
        "name": "Nordic Freight",
        "website": "https://nordicfreight.dk",
        "industry": "logistics",
        "employee_count": 200,
        "country": "DK",
        "description": "Moves goods across the Nordics.",
        "signals": [
            {"id": "hiring_finance", "evidence": "Hiring a financial controller in Aarhus"},
            {"id": "growth", "evidence": "Added 161 new trailers to its fleet"},
        ],
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


def test_fetch_returns_internal_links_priority_first(ctx: ToolContext) -> None:
    result = fetch_company_website(FetchWebsiteInput(url="https://nordicfreight.dk"), ctx)

    assert [(link.text, link.url) for link in result.links] == [
        # about/careers-style pages first...
        ("About us", "https://nordicfreight.dk/en/about-us/"),
        ("Karriere", "https://www.nordicfreight.dk/karriere"),
        # ...then the rest in page order. Skipped: the page itself, duplicates, anchors,
        # mailto, PDFs and other domains.
        ("Services", "https://nordicfreight.dk/services"),
    ]


def test_fetch_caps_links_but_keeps_priority_pages(ctx: ToolContext) -> None:
    filler = "".join(f'<a href="/product-{i}">Product {i}</a>' for i in range(40))
    html = f'<html><body>{filler}<a href="/contact">Contact</a></body></html>'
    ctx = replace(ctx, http=make_http(lambda request: httpx.Response(200, html=html)))

    result = fetch_company_website(FetchWebsiteInput(url="https://x.dk"), ctx)

    assert len(result.links) == 30
    assert result.links[0].url == "https://x.dk/contact"


def test_fetch_truncates_long_text(ctx: ToolContext) -> None:
    ctx.settings.fetch_max_chars = 20

    result = fetch_company_website(FetchWebsiteInput(url="https://x.dk"), ctx)

    assert len(result.text) == 20
    assert result.truncated is True


def test_fetch_http_error_raises_tool_error(ctx: ToolContext) -> None:
    http = make_http(lambda request: httpx.Response(404))
    ctx = replace(ctx, http=http)

    with pytest.raises(ToolError, match="Could not fetch"):
        fetch_company_website(FetchWebsiteInput(url="https://x.dk"), ctx)


def test_fetch_rejects_non_html(ctx: ToolContext) -> None:
    http = make_http(lambda request: httpx.Response(200, json={"a": 1}))
    ctx = replace(ctx, http=http)

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
    # Reasons include the evidence, so every point can be traced back to the website.
    growth = next(r for r in result.reasons if r.startswith("+8:"))
    assert growth.endswith("(Added 161 new trailers to its fleet)")


def test_unknown_facts_score_zero_points(icp: ICP) -> None:
    info = ideal_company(industry=None, employee_count=None, country=None, signals=[])

    result = score_company(info, icp)

    assert result.score == 0
    assert result.fit == "cold"
    assert "+0: company size unknown" in result.reasons


def test_signal_points_are_capped(icp: ICP) -> None:
    all_positive = [{"id": s.id, "evidence": "seen"} for s in icp.signals.positive]  # 10+8+7+5

    result = score_company(ideal_company(signals=all_positive), icp)

    assert result.score == 100  # 75 + capped 25


def test_negative_signal_lowers_score(icp: ICP) -> None:
    result = score_company(
        ideal_company(signals=[{"id": "has_competitor", "evidence": "Uses Acme AP"}]), icp
    )

    assert result.score == 60
    assert result.fit == "warm"
    assert any(r.startswith("-15") for r in result.reasons)


def test_global_enterprise_with_unknown_size_is_cold(icp: ICP) -> None:
    """The DSV case: right industry and country, no headcount on the site, clearly huge."""
    dsv = ideal_company(
        employee_count=None,
        signals=[
            {"id": "growth", "evidence": "Acquired Schenker"},
            {"id": "enterprise_scale", "evidence": "Operates in over 90 countries"},
        ],
    )

    result = score_company(dsv, icp)

    assert result.score == 28  # 30 industry + 20 country + 8 growth - 30 enterprise scale
    assert result.fit == "cold"


def test_unknown_size_keeps_a_mid_sized_lead_warm(icp: ICP) -> None:
    """The LEMAN case: no headcount and no sign of enterprise scale stays warm."""
    leman = ideal_company(
        employee_count=None, signals=[{"id": "growth", "evidence": "Added 161 new trailers"}]
    )

    result = score_company(leman, icp)

    assert (result.score, result.fit) == (58, "warm")


def test_hot_needs_confirmed_size(icp: ICP) -> None:
    all_positive = [{"id": s.id, "evidence": "seen"} for s in icp.signals.positive]
    unknown_size = ideal_company(employee_count=None, signals=all_positive)

    result = score_company(unknown_size, icp)

    assert result.score == 75  # 30 + 20 + 25 capped signals, hot by points
    assert result.fit == "warm"
    assert result.reasons[-1] == "fit capped at warm: company size not confirmed"


# --- draft_email / save_to_crm ---


def test_draft_email_sends_evidence_and_adds_signature(
    ctx: ToolContext, email_writer: FakeEmailWriter
) -> None:
    recorded = []
    ctx = replace(ctx, record_usage=recorded.append)
    params = DraftEmailInput(
        lead=ideal_company(),
        qualification=Qualification(score=93, fit="hot", reasons=[]),
        contact=ContactInfo(name="Anna Holm", job_title="CFO"),
    )

    email = draft_email(params, ctx)

    call = email_writer.calls[0]
    prompt = call["messages"][0]["content"]
    assert "Added 161 new trailers to its fleet" in prompt  # the concrete fact, not the label
    assert "Anna Holm" in prompt
    assert ctx.icp.seller.value_proposition in call["system"]
    assert call["model"] == ctx.settings.anthropic_model

    assert email.subject == "161 new trailers at Nordic Freight"
    assert email.body.startswith("Hi Nordic Freight team,")
    # The signature is added by code, so it is always the configured sender.
    seller = ctx.icp.seller
    assert email.body.endswith(f"{seller.sender_name}\n{seller.sender_title}, {seller.company}")
    assert len(recorded) == 1 and recorded[0].output_tokens == 80


def test_draft_email_refusal_raises_tool_error(ctx: ToolContext) -> None:
    ctx = replace(ctx, llm=FakeEmailWriter(stop_reason="refusal"))
    params = DraftEmailInput(
        lead=ideal_company(), qualification=Qualification(score=93, fit="hot", reasons=[])
    )

    with pytest.raises(ToolError, match="refusal"):
        draft_email(params, ctx)


def test_save_to_crm_dry_run_only_logs(ctx: ToolContext, caplog: pytest.LogCaptureFixture) -> None:
    qualification = Qualification(score=10, fit="cold", reasons=[])

    with caplog.at_level("INFO"):
        result = save_to_crm(SaveToCrmInput(lead=ideal_company(), qualification=qualification), ctx)

    assert result.status == "dry_run"
    assert "Nordic Freight" in caplog.text
