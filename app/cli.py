"""Command line for the lead pipeline.

Usage:
  uv run python -m app.cli run data/sample_leads.csv [--limit N]   # import + run new leads
  uv run python -m app.cli import data/sample_leads.csv           # import only
  uv run python -m app.cli leads [--fit hot] [--status pending_approval]

Review and send drafts with: uv run python -m app.outbox list
"""

import argparse
import sys
from pathlib import Path

from app.config import get_settings
from app.logging_config import setup_logging
from app.models.outreach import LeadStage
from app.pipeline import import_leads, list_leads, open_agent, parse_leads_csv, run_new_leads
from app.storage import make_engine


def main() -> None:
    parser = argparse.ArgumentParser(description="AI SDR lead pipeline.")
    commands = parser.add_subparsers(dest="command", required=True)
    run_cmd = commands.add_parser("run", help="Import a CSV, then run the agent on new leads.")
    run_cmd.add_argument("csv_path", type=Path)
    run_cmd.add_argument("--limit", type=int, help="Process at most N new leads.")
    import_cmd = commands.add_parser("import", help="Import a CSV without running the agent.")
    import_cmd.add_argument("csv_path", type=Path)
    leads_cmd = commands.add_parser("leads", help="List leads.")
    leads_cmd.add_argument("--fit", choices=["hot", "warm", "cold"])
    leads_cmd.add_argument("--status", choices=[s.value for s in LeadStage])
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level)
    engine = make_engine(settings.database_url)

    if args.command in ("run", "import"):
        try:
            rows, warnings = parse_leads_csv(args.csv_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            print(f"Error: {e}")
            sys.exit(1)
        summary = import_leads(engine, rows, warnings)
        print(f"Imported {summary.created} new leads ({summary.already_known} already known).")

    if args.command == "run":
        with open_agent(settings) as agent:
            batch = run_new_leads(engine, agent, args.limit)
        print(batch.model_dump_json(indent=2))
        print("Review drafts: uv run python -m app.outbox list")

    if args.command == "leads":
        for lead in list_leads(engine, args.fit, args.status):
            score = "-" if lead.score is None else lead.score
            print(f"#{lead.id}  {lead.name:<28} {lead.status:<17} {lead.fit or '-':<5} {score}")


if __name__ == "__main__":
    main()
