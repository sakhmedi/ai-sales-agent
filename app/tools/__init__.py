"""Tools the agent can call (research, qualification, email drafting, CRM sync)."""

from app.tools import crm, email, qualification, website
from app.tools.base import Tool, ToolContext, ToolError

ALL_TOOLS: list[Tool] = [website.TOOL, qualification.TOOL, email.TOOL, crm.TOOL]

__all__ = ["ALL_TOOLS", "Tool", "ToolContext", "ToolError"]
