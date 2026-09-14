"""Tool definition primitives.

A tool is a typed, self-describing capability the model may invoke. Read tools answer questions
and run immediately; write tools touch the outside world and may only ever *propose* an action,
which the merchant then approves (SPEC.md §2.4).

Parameters are declared as Pydantic models, so the JSON schema advertised to the model and the
validation applied to its reply come from the same source — the model cannot hand us a shape the
handler was not written for.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.orm import Session

from munshiji.db.models import ActionRequest, Merchant
from munshiji.errors import ValidationError
from munshiji.providers.factory import ProviderBundle
from munshiji.providers.llm import ToolSpec

__all__ = ["Tool", "ToolContext", "ToolResult", "tool_spec_from_model"]


@dataclass(slots=True)
class ToolContext:
    """Everything a handler is allowed to touch."""

    session: Session
    merchant: Merchant
    providers: ProviderBundle
    as_of: datetime
    language: str = "hi-IN"
    conversation_id: str | None = None
    insight_id: str | None = None

    @property
    def merchant_id(self) -> str:
        return self.merchant.id


@dataclass(slots=True)
class ToolResult:
    """What a tool produced.

    ``data`` is fed back to the model as JSON and must contain every number the reply might quote —
    the composer is forbidden from inventing figures, so anything unsaid here cannot be said at all.
    ``summary_hi`` / ``summary_en`` are for the UI trace and the audit log, not for the model.
    """

    ok: bool = True
    data: dict[str, Any] = field(default_factory=dict)
    summary_en: str = ""
    summary_hi: str = ""
    #: Set by write tools: the proposal now awaiting the merchant's yes.
    action: ActionRequest | None = None
    error: str = ""

    @property
    def summary(self) -> str:
        return self.summary_en or self.summary_hi


ToolHandler = Callable[[ToolContext, Any], Awaitable[ToolResult]]


def tool_spec_from_model(name: str, description: str, params_model: type[BaseModel]) -> ToolSpec:
    """Derive the advertised JSON schema from the handler's own parameter model."""
    schema = params_model.model_json_schema()
    # Models the size of sarvam-105b handle $defs, but inlining keeps the prompt smaller and the
    # local provider's job simpler.
    definitions = schema.pop("$defs", None)
    if definitions:
        schema["definitions"] = definitions
    schema.pop("title", None)
    return ToolSpec(name=name, description=description, parameters=schema)


@dataclass(slots=True)
class Tool:
    """One capability advertised to the model."""

    name: str
    description: str
    params_model: type[BaseModel]
    handler: ToolHandler
    requires_approval: bool = False
    #: Short label shown in the UI trace, e.g. "Aaj ka hisaab".
    label_hi: str = ""
    label_en: str = ""

    def spec(self) -> ToolSpec:
        return tool_spec_from_model(self.name, self.description, self.params_model)

    def parse(self, arguments: dict[str, Any] | None) -> BaseModel:
        """Validate the model's arguments, raising a clean error the loop can recover from."""
        try:
            return self.params_model.model_validate(arguments or {})
        except PydanticValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
                for err in exc.errors()[:4]
            )
            raise ValidationError(
                f"invalid arguments for tool {self.name}: {problems}",
                tool=self.name,
                arguments=arguments,
            ) from exc

    async def run(self, ctx: ToolContext, arguments: dict[str, Any] | None) -> ToolResult:
        """Validate and execute."""
        params = self.parse(arguments)
        return await self.handler(ctx, params)
