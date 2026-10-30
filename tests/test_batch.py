from pathlib import Path

import pytest
from sqlalchemy import Engine

from app.batch import load_companies, run_batch
from app.models.lead import (
    CompanyInput,
    LeadResult,
    LeadStatus,
    Qualification,
    TokenUsage,
)
from app.storage import load_run, make_engine, persist_result


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return make_engine(f"sqlite:///{tmp_path / 'sub' / 'test.db'}")


def write_csv(tmp_path: Path, content: str, encoding: str = "utf-8") -> Path:
    path = tmp_path / "companies.csv"
    path.write_text(content, encoding=encoding)
    return path


def result_for(company: CompanyInput, fit: str = "warm", score: int = 58) -> LeadResult:
    return LeadResult(
        company=company,
        status=LeadStatus.COMPLETED,
        qualification=Qualification(score=score, fit=fit, reasons=["test"]),  # type: ignore[arg-type]
        steps=4,
        usage=TokenUsage(input_tokens=10, output_tokens=5, cache_read_input_tokens=100),
    )


# --- CSV loading ---


def test_load_companies_skips_blank_incomplete_and_duplicate_rows(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = write_csv(
        tmp_path,
        "name,website\n"
        "LEMAN,https://leman.com\n"
        "\n"
        "No website,\n"
        "LEMAN again,https://www.leman.com/about\n"  # same domain as line 2
        " Apple , apple.com \n",
    )

    companies = load_companies(path)

    assert companies == [
        CompanyInput(name="LEMAN", website="https://leman.com"),
        CompanyInput(name="Apple", website="apple.com"),
    ]
    assert "Line 4 skipped" in caplog.text
    assert "duplicate domain leman.com" in caplog.text


def test_load_companies_reads_excel_semicolon_csv_with_bom(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "Name;Website\nLEMAN;https://leman.com\n", encoding="utf-8-sig")

    assert load_companies(path) == [CompanyInput(name="LEMAN", website="https://leman.com")]


def test_load_companies_requires_columns(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "company,url\nLEMAN,https://leman.com\n")

    with pytest.raises(ValueError, match="missing columns: name, website"):
        load_companies(path)


# --- storage ---


def test_save_and_load_result(engine: Engine) -> None:
    company = CompanyInput(name="LEMAN", website="https://leman.com")

    persist_result(engine, "run-1", result_for(company))
    persist_result(engine, "run-2", result_for(company, fit="cold", score=10))

    [record] = load_run(engine, "run-1")
    assert (record.company_name, record.status, record.score, record.fit) == (
        "LEMAN",
        "completed",
        58,
        "warm",
    )
    assert record.total_input_tokens == 110
    assert record.to_result().qualification == Qualification(score=58, fit="warm", reasons=["test"])


# --- batch runner ---


class FakeAgent:
    def __init__(self, fail_on: str) -> None:
        self.fail_on = fail_on
        self.seen: list[str] = []

    def run(self, company: CompanyInput) -> LeadResult:
        self.seen.append(company.name)
        if company.name == self.fail_on:
            raise RuntimeError("boom")
        return result_for(company, fit="hot" if company.name == "A" else "warm")


def test_batch_continues_after_a_failure_and_stores_everything(engine: Engine) -> None:
    companies = [
        CompanyInput(name="A", website="a.dk"),
        CompanyInput(name="B", website="b.dk"),
        CompanyInput(name="C", website="c.dk"),
    ]
    agent = FakeAgent(fail_on="B")

    summary = run_batch(agent, companies, engine, run_id="run-x")

    assert agent.seen == ["A", "B", "C"]
    assert summary.total == 3
    assert summary.by_status == {"completed": 2, "failed": 1}
    assert summary.by_fit == {"hot": 1, "warm": 1}
    assert summary.usage.input_tokens == 20

    records = load_run(engine, "run-x")
    assert [r.company_name for r in records] == ["A", "B", "C"]
    assert records[1].status == "failed"
    assert records[1].error is not None and "boom" in records[1].error
