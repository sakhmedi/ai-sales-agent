"""HubSpot integration tests. All HTTP is mocked with respx; no real requests."""

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.config import Settings
from app.domains import company_domain
from app.integrations.hubspot import (
    CONTACT_TO_COMPANY,
    DEAL_TO_COMPANY,
    DEAL_TO_CONTACT,
    NOTE_TO_DEAL,
    HubSpotAuthError,
    HubSpotClient,
    HubSpotError,
)
from app.models.lead import CompanyInfo, ContactInfo, EmailDraft, Qualification
from app.tools import ToolContext, ToolError
from app.tools.crm import SaveToCrmInput, save_to_crm

BASE_URL = "https://api.hubapi.com"
OBJECTS = "/crm/v3/objects"

LEAD = CompanyInfo(
    name="Nordic Freight",
    website="https://www.nordicfreight.dk/about",
    industry="logistics",
    employee_count=200,
    country="DK",
    description="Moves goods across the Nordics.",
)
QUALIFICATION = Qualification(score=85, fit="hot", reasons=["+30: target industry (logistics)"])
EMAIL = EmailDraft(subject="Hello", body="Hi team,\nShort <pitch>.")
CONTACT = ContactInfo(name="Anna Holm", email="anna@nordicfreight.dk", job_title="CFO")

EMPTY_SEARCH = {"total": 0, "results": []}


def found(record_id: str) -> dict[str, Any]:
    return {"total": 1, "results": [{"id": record_id}]}


def body(route: respx.Route, call: int = -1) -> Any:
    return json.loads(route.calls[call].request.content)


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def client(sleeps: list[float]) -> Iterator[HubSpotClient]:
    with HubSpotClient("test-token", base_url=BASE_URL, sleep=sleeps.append) as c:
        yield c


@pytest.fixture
def hubspot() -> Iterator[respx.MockRouter]:
    """Mock HubSpot. Deal properties already exist; any unmocked request fails the test."""
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        router.get(path__regex=r"^/crm/v3/properties/deals/\w+$").respond(200, json={})
        yield router


def test_company_domain() -> None:
    assert company_domain("https://www.Example.dk/about") == "example.dk"
    assert company_domain("example.dk") == "example.dk"
    assert company_domain("http://shop.example.dk:8080") == "shop.example.dk"


def test_new_lead_creates_everything(client: HubSpotClient, hubspot: respx.MockRouter) -> None:
    company_search = hubspot.post(f"{OBJECTS}/companies/search").respond(json=EMPTY_SEARCH)
    company_create = hubspot.post(f"{OBJECTS}/companies").respond(201, json={"id": "c1"})
    hubspot.post(f"{OBJECTS}/contacts/search").respond(json=EMPTY_SEARCH)
    contact_create = hubspot.post(f"{OBJECTS}/contacts").respond(201, json={"id": "p1"})
    hubspot.post(f"{OBJECTS}/deals/search").respond(json=EMPTY_SEARCH)
    deal_create = hubspot.post(f"{OBJECTS}/deals").respond(201, json={"id": "d1"})
    note_create = hubspot.post(f"{OBJECTS}/notes").respond(201, json={"id": "n1"})

    result = client.sync_lead(LEAD, QUALIFICATION, EMAIL, CONTACT)

    assert result.status == "saved"
    assert (result.company_id, result.contact_id, result.deal_id, result.note_id) == (
        "c1",
        "p1",
        "d1",
        "n1",
    )
    assert result.created_company and result.created_deal

    assert company_search.calls.last.request.headers["Authorization"] == "Bearer test-token"
    search_filter = body(company_search)["filterGroups"][0]["filters"][0]
    assert search_filter == {
        "propertyName": "domain",
        "operator": "EQ",
        "value": "nordicfreight.dk",
    }

    company = body(company_create)["properties"]
    assert company["domain"] == "nordicfreight.dk"
    assert company["numberofemployees"] == "200"

    contact = body(contact_create)
    assert contact["properties"] == {
        "email": "anna@nordicfreight.dk",
        "firstname": "Anna",
        "lastname": "Holm",
        "jobtitle": "CFO",
    }
    assert contact["associations"][0]["to"]["id"] == "c1"
    assert contact["associations"][0]["types"][0]["associationTypeId"] == CONTACT_TO_COMPANY

    deal = body(deal_create)
    assert deal["properties"]["ai_lead_score"] == "85"
    assert deal["properties"]["ai_lead_fit"] == "hot"
    assert deal["properties"]["pipeline"] == "default"
    links = {(a["to"]["id"], a["types"][0]["associationTypeId"]) for a in deal["associations"]}
    assert links == {("c1", DEAL_TO_COMPANY), ("p1", DEAL_TO_CONTACT)}

    note = body(note_create)
    assert note["associations"][0]["to"]["id"] == "d1"
    assert note["associations"][0]["types"][0]["associationTypeId"] == NOTE_TO_DEAL
    note_body = note["properties"]["hs_note_body"]
    assert "Hi team,<br>Short &lt;pitch&gt;." in note_body  # escaped HTML, kept line breaks
    assert "85/100" in note_body


def test_existing_company_and_deal_are_reused(
    client: HubSpotClient, hubspot: respx.MockRouter
) -> None:
    hubspot.post(f"{OBJECTS}/companies/search").respond(json=found("c9"))
    company_create = hubspot.post(f"{OBJECTS}/companies")
    hubspot.post(f"{OBJECTS}/deals/search").respond(json=found("d9"))
    deal_create = hubspot.post(f"{OBJECTS}/deals")
    deal_update = hubspot.patch(f"{OBJECTS}/deals/d9").respond(200, json={"id": "d9"})

    result = client.sync_lead(LEAD, QUALIFICATION, email=None, contact=None)

    assert result.company_id == "c9" and not result.created_company
    assert result.deal_id == "d9" and not result.created_deal
    assert result.contact_id is None and result.note_id is None
    assert not company_create.called
    assert not deal_create.called
    assert body(deal_update)["properties"]["ai_lead_score"] == "85"


def test_duplicate_contact_on_create_is_reused(
    client: HubSpotClient, hubspot: respx.MockRouter
) -> None:
    hubspot.post(f"{OBJECTS}/companies/search").respond(json=found("c1"))
    hubspot.post(f"{OBJECTS}/contacts/search").mock(
        side_effect=[httpx.Response(200, json=EMPTY_SEARCH), httpx.Response(200, json=found("p7"))]
    )
    hubspot.post(f"{OBJECTS}/contacts").respond(
        409, json={"message": "Contact already exists. Existing ID: p7"}
    )
    link = hubspot.put(f"{OBJECTS}/contacts/p7/associations/companies/c1/{CONTACT_TO_COMPANY}")
    link.respond(200, json={})
    hubspot.post(f"{OBJECTS}/deals/search").respond(json=EMPTY_SEARCH)
    hubspot.post(f"{OBJECTS}/deals").respond(201, json={"id": "d1"})

    result = client.sync_lead(LEAD, QUALIFICATION, email=None, contact=CONTACT)

    assert result.contact_id == "p7"
    assert link.called


def test_rate_limit_is_retried_with_retry_after(
    client: HubSpotClient, hubspot: respx.MockRouter, sleeps: list[float]
) -> None:
    route = hubspot.post(f"{OBJECTS}/companies/search").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, json=found("c1")),
        ]
    )

    assert client.search("companies", "domain", "x.dk") == "c1"
    assert route.call_count == 2
    assert sleeps == [2.0]


def test_rate_limit_gives_up_after_max_retries(
    client: HubSpotClient, hubspot: respx.MockRouter, sleeps: list[float]
) -> None:
    route = hubspot.post(f"{OBJECTS}/companies/search").respond(429, json={"message": "slow down"})

    with pytest.raises(HubSpotError) as error:
        client.search("companies", "domain", "x.dk")

    assert error.value.status == 429
    assert route.call_count == 4  # first try + 3 retries
    assert sleeps == [1.0, 2.0, 4.0]  # exponential backoff


def test_unauthorized_is_not_retried(client: HubSpotClient, hubspot: respx.MockRouter) -> None:
    route = hubspot.post(f"{OBJECTS}/companies/search").respond(401, json={"message": "nope"})

    with pytest.raises(HubSpotAuthError, match="HUBSPOT_TOKEN"):
        client.search("companies", "domain", "x.dk")

    assert route.call_count == 1


def test_missing_deal_properties_are_created_once(sleeps: list[float]) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        get = router.get(path__regex=r"^/crm/v3/properties/deals/\w+$").respond(404, json={})
        create = router.post("/crm/v3/properties/deals").respond(201, json={})

        with HubSpotClient("t", base_url=BASE_URL, sleep=sleeps.append) as client:
            client.ensure_deal_properties()
            client.ensure_deal_properties()

    assert get.call_count == 2
    assert create.call_count == 2
    assert {json.loads(c.request.content)["name"] for c in create.calls} == {
        "ai_lead_score",
        "ai_lead_fit",
    }


# --- save_to_crm tool ---


def crm_input() -> SaveToCrmInput:
    return SaveToCrmInput(lead=LEAD, qualification=QUALIFICATION, email=EMAIL, contact=CONTACT)


def test_dry_run_sends_nothing(ctx: ToolContext, caplog: pytest.LogCaptureFixture) -> None:
    with respx.mock(assert_all_called=False) as router, caplog.at_level("INFO"):
        result = save_to_crm(crm_input(), ctx)

    assert result.status == "dry_run"
    assert router.calls.call_count == 0
    assert "would send to HubSpot" in caplog.text
    assert "nordicfreight.dk" in caplog.text
    assert "Short &lt;pitch&gt;" not in caplog.text  # email body is not logged


def test_real_mode_without_token_fails(ctx: ToolContext) -> None:
    ctx.settings.dry_run = False

    with pytest.raises(ToolError, match="HUBSPOT_TOKEN is not set"):
        save_to_crm(crm_input(), ctx)


def test_real_mode_hubspot_error_becomes_tool_error(
    settings: Settings, ctx: ToolContext, hubspot: respx.MockRouter
) -> None:
    settings.dry_run = False
    settings.hubspot_token = SecretStr("bad-token")
    hubspot.post(f"{OBJECTS}/companies/search").respond(401, json={})

    with pytest.raises(ToolError, match="401"):
        save_to_crm(crm_input(), ctx)
