"""ToolGateway — abstraction only. No actual tool implementations here."""

from typing import Protocol, runtime_checkable

from app.domain import ToolExecution, ToolRequest


@runtime_checkable
class ToolGateway(Protocol):
    async def execute(self, request: ToolRequest) -> ToolExecution: ...
