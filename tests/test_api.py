"""HTTP API tests: the real FastAPI app with a test database, fake agent and fake sender."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_agent, get_engine, get_sender
from app.config import Settings, get_settings
from app.main import app
from app.storage import make_engine
from tests.conftest import FakeSender
from tests.test_pipeline import FakeAgent

CSV = (
    "company_name,website,contact_name,contact_email\n"
    "Warmco,https://warm.dk,Test Contact,test+warm@example.com\n"
    "Coldco,https://cold.dk,,\n"
    "Nocontact,https://nocontact.dk,,\n"
)


@pytest.fixture
def sender() -> FakeSender:
    return FakeSender()


@pytest.fixture
def client(tmp_path: Path, settings: Settings, sender: FakeSender) -> Iterator[TestClient]:
    engine = make_engine(f"sqlite:///{tmp_path / 'api.db'}")
    app.dependency_overrides = {
        get_engine: lambda: engine,
        get_agent: FakeAgent,
        get_settings: lambda: settings,
        get_sender: lambda: None if settings.dry_run else sender,
    }
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides = {}


def upload(client: TestClient, text: str = CSV) -> dict[str, object]:
    response = client.post("/leads/import", files={"file": ("leads.csv", text, "text/csv")})
    assert response.status_code == 200, response.text
    data: dict[str, object] = response.json()
    return data


def lead_id(client: TestClient, name: str) -> int:
    leads = client.get("/leads").json()
    return int(next(lead["id"] for lead in leads if lead["name"] == name))


def test_import_csv(client: TestClient) -> None:
    assert upload(client) == {"created": 3, "already_known": 0, "warnings": []}
    assert upload(client)["already_known"] == 3


def test_import_rejects_bad_csv(client: TestClient) -> None:
    response = client.post("/leads/import", files={"file": ("x.csv", "foo,bar\n1,2\n", "text/csv")})

    assert response.status_code == 400
    assert "company_name" in response.json()["detail"]


def test_run_one_lead(client: TestClient) -> None:
    upload(client)

    response = client.post(f"/leads/{lead_id(client, 'Warmco')}/run")

    assert response.status_code == 200
    body = response.json()
    assert body["run"]["status"] == "completed"
    assert body["lead"]["status"] == "pending_approval"
    assert body["lead"]["fit"] == "warm"


def test_run_unknown_lead_is_404(client: TestClient) -> None:
    assert client.post("/leads/999/run").status_code == 404


def test_run_batch_and_filter_by_fit(client: TestClient) -> None:
    upload(client)

    summary = client.post("/leads/run-batch").json()

    assert summary["total"] == 3
    assert summary["by_fit"] == {"warm": 2, "cold": 1}
    assert [lead["name"] for lead in client.get("/leads?fit=cold").json()] == ["Coldco"]
    assert len(client.get("/leads?status=pending_approval").json()) == 2
    assert client.get("/leads?fit=lukewarm").status_code == 422
    assert client.post("/leads/run-batch?limit=0").status_code == 422


def test_lead_detail_shows_email_and_reasons(client: TestClient) -> None:
    upload(client)
    warm = lead_id(client, "Warmco")
    client.post(f"/leads/{warm}/run")

    detail = client.get(f"/leads/{warm}").json()

    assert detail["reasons"] == ["+30: test"]
    assert detail["email"]["status"] == "pending_approval"
    assert detail["email"]["to_address"] == "test+warm@example.com"


def test_approve_in_dry_run_sends_nothing(client: TestClient, sender: FakeSender) -> None:
    upload(client)
    warm = lead_id(client, "Warmco")
    client.post(f"/leads/{warm}/run")

    response = client.post(f"/leads/{warm}/approve", json={"reviewer": "salima"})

    assert response.json()["status"] == "dry_run"
    assert sender.sent == []
    assert client.get(f"/leads/{warm}").json()["status"] == "pending_approval"


def test_approve_sends_once(client: TestClient, settings: Settings, sender: FakeSender) -> None:
    settings.dry_run = False
    upload(client)
    warm = lead_id(client, "Warmco")
    client.post(f"/leads/{warm}/run")

    first = client.post(f"/leads/{warm}/approve", json={"reviewer": "salima"})
    second = client.post(f"/leads/{warm}/approve", json={"reviewer": "salima"})

    assert first.json() == {
        "email_id": 1,
        "status": "sent",
        "recipient": "test+warm@example.com",
        "error": None,
    }
    assert second.status_code == 404  # nothing left to approve
    assert len(sender.sent) == 1
    assert client.get(f"/leads/{warm}").json()["status"] == "sent"


def test_approve_without_recipient_is_400(client: TestClient, settings: Settings) -> None:
    settings.dry_run = False
    upload(client)
    lead = lead_id(client, "Nocontact")
    client.post(f"/leads/{lead}/run")

    response = client.post(f"/leads/{lead}/approve", json={"reviewer": "salima"})
    assert response.status_code == 400
    assert "no valid recipient" in response.json()["detail"]

    fixed = client.post(
        f"/leads/{lead}/approve", json={"reviewer": "salima", "to": "test@example.com"}
    )
    assert fixed.json()["status"] == "sent"


def test_approve_requires_reviewer(client: TestClient) -> None:
    assert client.post("/leads/1/approve", json={}).status_code == 422


def test_reject(client: TestClient) -> None:
    upload(client)
    warm = lead_id(client, "Warmco")
    client.post(f"/leads/{warm}/run")

    assert client.post(f"/leads/{warm}/reject", json={"reviewer": "salima"}).status_code == 204

    detail = client.get(f"/leads/{warm}").json()
    assert detail["status"] == "qualified"
    assert detail["email"]["status"] == "rejected"
