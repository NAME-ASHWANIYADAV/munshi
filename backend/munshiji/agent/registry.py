"""The tool registry — what MunshiJi is allowed to do.

Read tools run immediately. Write tools are registered here with ``requires_approval=True`` and,
by construction, can only ever create a pending :class:`ActionRequest`; the registry refuses to
execute one outside the approval flow, so a persuasive prompt cannot talk its way past the gate.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from munshiji.agent.approval import WRITE_TOOLS
from munshiji.agent.tools.base import Tool, ToolContext, ToolResult
from munshiji.errors import ToolNotFoundError
from munshiji.logging import get_logger
from munshiji.providers.llm import ToolSpec

__all__ = ["ToolRegistry", "build_registry", "get_registry"]

logger = get_logger(__name__)


class ToolRegistry:
    """A validated collection of tools, with JSON-schema export for the model."""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        # Cross-check against the approval module's list so the two can never drift apart.
        should_gate = tool.name in WRITE_TOOLS
        if should_gate != tool.requires_approval:
            raise ValueError(
                f"tool {tool.name!r} declares requires_approval={tool.requires_approval} but "
                f"approval.WRITE_TOOLS says {should_gate} — these must agree"
            )
        self._tools[tool.name] = tool

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    @property
    def names(self) -> list[str]:
        return sorted(self._tools)

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(
                f"unknown tool {name!r}", tool=name, available=sorted(self._tools)
            )
        return tool

    def specs(self, *, include_write: bool = True) -> list[ToolSpec]:
        """JSON schemas advertised to the model."""
        return [
            tool.spec()
            for tool in self._tools.values()
            if include_write or not tool.requires_approval
        ]

    async def execute(self, name: str, arguments: dict | None, ctx: ToolContext) -> ToolResult:
        """Run a tool, converting failures into a ``ToolResult`` the loop can narrate."""
        tool = self.get(name)
        try:
            result = await tool.run(ctx, arguments)
        except Exception as exc:
            logger.exception("tool %s failed", name)
            return ToolResult(
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                summary_en=f"{name} failed",
                summary_hi=f"{name} nahi chal paaya",
            )
        return result


_registry: ToolRegistry | None = None


def build_registry() -> ToolRegistry:
    """Construct the full tool set. Imported lazily to keep module import cheap."""
    from munshiji.agent.tools import actions as action_tools
    from munshiji.agent.tools import credit as credit_tools
    from munshiji.agent.tools import insights as insight_tools
    from munshiji.agent.tools import inventory as inventory_tools
    from munshiji.agent.tools import memory as memory_tools
    from munshiji.agent.tools import sales as sales_tools

    registry = ToolRegistry()
    for module in (
        sales_tools,
        inventory_tools,
        credit_tools,
        insight_tools,
        memory_tools,
        action_tools,
    ):
        for tool in module.TOOLS:
            registry.register(tool)
    logger.debug("registry built with %d tools: %s", len(registry), ", ".join(registry.names))
    return registry


def get_registry() -> ToolRegistry:
    """Process-wide registry."""
    global _registry
    if _registry is None:
        _registry = build_registry()
    return _registry


def reset_registry() -> None:
    """Drop the cached registry (tests)."""
    global _registry
    _registry = None
