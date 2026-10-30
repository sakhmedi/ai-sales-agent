import json
import logging
from dataclasses import dataclass
from typing import Any

import anthropic
import httpx
from anthropic.types.beta import (
    BetaContentBlock,
    BetaMessage,
    BetaMessageParam,
    BetaToolResultBlockParam,
    BetaToolUseBlock,
    BetaUsage,
)
from pydantic import BaseModel, ValidationError

from app.agent.prompts import build_system_prompt, build_user_prompt
from app.config import Settings
from app.models.icp import ICP
from app.models.lead import (
    CompanyInfo,
    CompanyInput,
    CrmSaveResult,
    EmailDraft,
    LeadResult,
    LeadStatus,
    Qualification,
    TokenUsage,
)
from app.tools import ALL_TOOLS, Tool, ToolContext, ToolError
from app.tools.qualification import QualifyLeadInput

logger = logging.getLogger(__name__)

MAX_TOKENS = 16000
# On a safety decline, the API retries the request on a fallback model by itself.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Returns the model's short progress notes between tool calls, so we can log them.
THINKING_UPDATES_BETA = "thinking-display-updates-2026-08-18"
LOG_PREVIEW_CHARS = 300


@dataclass
class _RunState:
    """What the tools have produced so far for the current lead."""

    company_info: CompanyInfo | None = None
    qualification: Qualification | None = None
    email: EmailDraft | None = None
    crm: CrmSaveResult | None = None

    def record(self, params: BaseModel, output: BaseModel) -> None:
        if isinstance(params, QualifyLeadInput):
            self.company_info = params.company_info
        if isinstance(output, Qualification):
            self.qualification = output
        elif isinstance(output, EmailDraft):
            self.email = output
        elif isinstance(output, CrmSaveResult):
            self.crm = output


class SalesAgent:
    """Runs the tool-calling loop for one company at a time."""

    def __init__(
        self,
        client: anthropic.Anthropic,
        settings: Settings,
        icp: ICP,
        http: httpx.Client,
        tools: list[Tool] | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._tools = {t.name: t for t in (tools or ALL_TOOLS)}
        self._tool_schemas = [t.to_api_schema() for t in self._tools.values()]
        self._system_prompt = build_system_prompt(icp)
        self._ctx = ToolContext(settings=settings, icp=icp, http=http)

    def run(self, company: CompanyInput) -> LeadResult:
        logger.info(
            "=== Lead: %s (%s) model=%s effort=%s",
            company.name,
            company.website,
            self._settings.anthropic_model,
            self._settings.anthropic_effort,
        )
        messages: list[BetaMessageParam] = [{"role": "user", "content": build_user_prompt(company)}]
        state = _RunState()
        usage = TokenUsage()
        max_steps = self._settings.agent_max_steps

        def finish(status: LeadStatus, steps: int, **extra: Any) -> LeadResult:
            result = LeadResult(
                company=company,
                status=status,
                company_info=state.company_info,
                qualification=state.qualification,
                email=state.email,
                crm=state.crm,
                steps=steps,
                usage=usage,
                **extra,
            )
            logger.info(
                "=== Done: %s status=%s steps=%d tokens %s",
                company.name,
                status,
                steps,
                _format_usage(usage),
            )
            return result

        for step in range(1, max_steps + 1):
            try:
                response = self._call_model(messages)
            except anthropic.APIStatusError as e:
                logger.error("API error %s: %s", e.status_code, e.message)
                return finish(LeadStatus.FAILED, step, error=f"API error {e.status_code}")
            except anthropic.APIConnectionError as e:
                logger.error("API connection error: %s", e)
                return finish(LeadStatus.FAILED, step, error="API connection error")

            step_usage = TokenUsage()
            _add_usage(step_usage, response.usage)
            _add_usage(usage, response.usage)
            # response.model can differ from the configured model if a fallback served the turn.
            logger.info(
                "Step %d/%d: stop_reason=%s model=%s tokens %s",
                step,
                max_steps,
                response.stop_reason,
                response.model,
                _format_usage(step_usage),
            )
            _log_model_output(response.content)

            # The full assistant turn (including thinking blocks) goes back unchanged.
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "tool_use":
                tool_results = [
                    self._run_tool(block, state)
                    for block in response.content
                    if block.type == "tool_use"
                ]
                # All results for one turn go back in a single user message.
                messages.append({"role": "user", "content": tool_results})
            elif response.stop_reason == "pause_turn":
                continue
            elif response.stop_reason == "end_turn":
                return finish(LeadStatus.COMPLETED, step, summary=_final_text(response))
            else:
                return finish(
                    LeadStatus.FAILED, step, error=f"Model stopped: {response.stop_reason}"
                )

        logger.warning("Reached the step limit (%d) for %s", max_steps, company.name)
        return finish(LeadStatus.MAX_STEPS_REACHED, max_steps)

    def _call_model(self, messages: list[BetaMessageParam]) -> BetaMessage:
        return self._client.beta.messages.create(
            model=self._settings.anthropic_model,
            max_tokens=MAX_TOKENS,
            system=self._system_prompt,
            tools=self._tool_schemas,
            messages=messages,
            thinking={"type": "adaptive", "display": "updates"},
            output_config={"effort": self._settings.anthropic_effort},
            cache_control={"type": "ephemeral"},
            fallbacks="default",
            betas=[FALLBACK_BETA, THINKING_UPDATES_BETA],
        )

    def _run_tool(self, block: BetaToolUseBlock, state: _RunState) -> BetaToolResultBlockParam:
        logger.info("  -> %s(%s)", block.name, _preview(block.input))
        tool = self._tools.get(block.name)
        if tool is None:
            return _tool_error(block, f"Unknown tool: {block.name}")

        try:
            params = tool.input_model.model_validate(block.input)
            output = tool.handler(params, self._ctx)
        except ValidationError as e:
            return _tool_error(block, f"Invalid input: {e}")
        except ToolError as e:
            return _tool_error(block, str(e))
        except Exception:
            logger.exception("Tool %s crashed", block.name)
            return _tool_error(block, "Internal error while running the tool.")

        state.record(params, output)
        content = output.model_dump_json()
        logger.info("  <- %s: %s", block.name, _preview(content))
        return {"type": "tool_result", "tool_use_id": block.id, "content": content}


def _tool_error(block: BetaToolUseBlock, message: str) -> BetaToolResultBlockParam:
    logger.warning("  <- %s failed: %s", block.name, message)
    return {
        "type": "tool_result",
        "tool_use_id": block.id,
        "content": message,
        "is_error": True,
    }


def _add_usage(total: TokenUsage, usage: BetaUsage) -> None:
    total.input_tokens += usage.input_tokens
    total.output_tokens += usage.output_tokens
    total.cache_read_input_tokens += usage.cache_read_input_tokens or 0
    total.cache_creation_input_tokens += usage.cache_creation_input_tokens or 0


def _format_usage(usage: TokenUsage) -> str:
    """E.g. 'in=21277 (cached 15028, cache_write 6239) out=568'."""
    return (
        f"in={usage.total_input_tokens} (cached {usage.cache_read_input_tokens}, "
        f"cache_write {usage.cache_creation_input_tokens}) out={usage.output_tokens}"
    )


def _log_model_output(content: list[BetaContentBlock]) -> None:
    for block in content:
        if block.type == "thinking" and block.thinking:
            logger.info("  [agent note] %s", _preview(block.thinking))
        elif block.type == "text" and block.text:
            logger.info("  [agent says] %s", _preview(block.text))


def _final_text(response: BetaMessage) -> str:
    return "\n".join(b.text for b in response.content if b.type == "text").strip()


def _preview(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) > LOG_PREVIEW_CHARS:
        return text[:LOG_PREVIEW_CHARS] + "..."
    return text
