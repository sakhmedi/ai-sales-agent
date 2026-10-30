from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from anthropic.types.beta import BetaToolParam
from pydantic import BaseModel

from app.config import Settings
from app.models.icp import ICP


class ToolError(Exception):
    """Expected tool failure. The message is sent back to the model as an error result."""


@dataclass(frozen=True)
class ToolContext:
    """Dependencies shared by all tools."""

    settings: Settings
    icp: ICP
    http: httpx.Client


@dataclass(frozen=True)
class Tool:
    """A function the agent can call, plus the metadata Claude needs to call it."""

    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[Any, ToolContext], BaseModel]

    def to_api_schema(self) -> BetaToolParam:
        """Tool definition in the format the Messages API expects."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
        }
