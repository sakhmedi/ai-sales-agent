"""Run the agent on a single company from the command line.

Usage: uv run python -m app.agent "Company name" https://company.example
"""

import argparse

import anthropic
import httpx

from app.agent.loop import SalesAgent
from app.config import get_settings
from app.logging_config import setup_logging
from app.models.icp import load_icp
from app.models.lead import CompanyInput


def main() -> None:
    parser = argparse.ArgumentParser(description="Process one company with the AI SDR agent.")
    parser.add_argument("name")
    parser.add_argument("website")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level)

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value())
    with httpx.Client() as http:
        agent = SalesAgent(client, settings, load_icp(settings.icp_path), http)
        result = agent.run(CompanyInput(name=args.name, website=args.website))

    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
