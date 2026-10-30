"""Agent loop tests with a scripted fake Claude client: no network, no API key."""

from types import SimpleNamespace
from typing import Any

import httpx

from app.agent import SalesAgent
from app.config import Settings
from app.models.icp import ICP
from app.models.lead import CompanyInput, LeadStatus

COMPANY = CompanyInput(name="Nordic Freight", website="https://nordicfreight.dk")
COMPANY_INFO = {
    "name": "Nordic Freight",
    "website": "https://nordicfreight.dk",
    "industry": "logistics",
    "employee_count": 200,
    "country": "DK",
    "description": "Moves goods across the Nordics.",
    "signals": ["hiring_finance"],
}
QUALIFICATION = {"score": 85, "fit": "hot", "reasons": ["test"]}


def tool_use(block_id: str, name: str, tool_input: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=tool_input)


def text(value: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=value)


def response(stop_reason: str, *content: SimpleNamespace) -> SimpleNamespace:
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=20,
        cache_read_input_tokens=300,
        cache_creation_input_tokens=50,
    )
    return SimpleNamespace(
        stop_reason=stop_reason, model="claude-test", content=list(content), usage=usage
    )


class FakeClient:
    """Mimics client.beta.messages.create, returning scripted responses in order."""

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        if len(self._responses) == 1:
            return self._responses[0]  # repeat the last response forever
        return self._responses.pop(0)


def make_agent(client: FakeClient, settings: Settings, icp: ICP, http: httpx.Client) -> SalesAgent:
    return SalesAgent(client, settings, icp, http)  # type: ignore[arg-type]


def test_full_run_completes(settings: Settings, icp: ICP, html_http: httpx.Client) -> None:
    client = FakeClient(
        [
            response("tool_use", tool_use("t1", "fetch_company_website", {"url": COMPANY.website})),
            response("tool_use", tool_use("t2", "qualify_lead", {"company_info": COMPANY_INFO})),
            response(
                "tool_use",
                tool_use(
                    "t3", "draft_email", {"lead": COMPANY_INFO, "qualification": QUALIFICATION}
                ),
            ),
            response(
                "tool_use",
                tool_use(
                    "t4", "save_to_crm", {"lead": COMPANY_INFO, "qualification": QUALIFICATION}
                ),
            ),
            response("end_turn", text("Hot lead: Danish logistics company, hiring finance.")),
        ]
    )

    result = make_agent(client, settings, icp, html_http).run(COMPANY)

    assert result.status == LeadStatus.COMPLETED
    assert result.steps == 5
    assert result.company_info is not None and result.company_info.industry == "logistics"
    assert result.qualification is not None and result.qualification.fit == "hot"
    assert result.email is not None
    assert result.crm is not None and result.crm.dry_run is True
    assert result.summary.startswith("Hot lead")
    assert result.usage.input_tokens == 500
    assert result.usage.output_tokens == 100
    assert result.usage.cache_read_input_tokens == 1500
    assert result.usage.total_input_tokens == 500 + 1500 + 250

    # Every tool result is sent back with the id of the tool call it answers.
    second_call_messages = client.calls[1]["messages"]
    tool_result = second_call_messages[-1]["content"][0]
    assert second_call_messages[-1]["role"] == "user"
    assert tool_result["tool_use_id"] == "t1"
    assert "is_error" not in tool_result


def test_stops_at_step_limit(settings: Settings, icp: ICP, html_http: httpx.Client) -> None:
    settings.agent_max_steps = 3
    client = FakeClient(
        [response("tool_use", tool_use("t", "fetch_company_website", {"url": COMPANY.website}))]
    )

    result = make_agent(client, settings, icp, html_http).run(COMPANY)

    assert result.status == LeadStatus.MAX_STEPS_REACHED
    assert result.steps == 3
    assert len(client.calls) == 3


def test_invalid_tool_input_is_reported_to_model(
    settings: Settings, icp: ICP, html_http: httpx.Client
) -> None:
    client = FakeClient(
        [
            response("tool_use", tool_use("bad", "qualify_lead", {"wrong": "field"})),
            response("end_turn", text("Could not qualify.")),
        ]
    )

    result = make_agent(client, settings, icp, html_http).run(COMPANY)

    tool_result = client.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert "Invalid input" in tool_result["content"]
    assert result.status == LeadStatus.COMPLETED
    assert result.qualification is None


def test_unknown_tool_is_reported_to_model(
    settings: Settings, icp: ICP, html_http: httpx.Client
) -> None:
    client = FakeClient(
        [
            response("tool_use", tool_use("x", "delete_database", {})),
            response("end_turn", text("Done.")),
        ]
    )

    make_agent(client, settings, icp, html_http).run(COMPANY)

    tool_result = client.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert "Unknown tool" in tool_result["content"]


def test_refusal_marks_lead_failed(settings: Settings, icp: ICP, html_http: httpx.Client) -> None:
    client = FakeClient([response("refusal")])

    result = make_agent(client, settings, icp, html_http).run(COMPANY)

    assert result.status == LeadStatus.FAILED
    assert result.error is not None and "refusal" in result.error
