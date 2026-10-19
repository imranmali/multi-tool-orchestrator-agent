"""Core data model and error hierarchy shared by every orchestrator module."""

from __future__ import annotations

import inspect
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

Handler = Callable[..., Awaitable[Any]]

# Patterns are applied with ``fullmatch`` (``$`` would also accept a trailing
# newline) and use ASCII classes only (``\d`` matches non-ASCII digits).
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_.-]*")
_VERSION_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


class OrchestratorError(Exception):
    """Base class for all orchestrator errors."""


class ValidationError(OrchestratorError, ValueError):
    """A descriptor, input or plan is malformed."""


class ToolNotFoundError(OrchestratorError, LookupError):
    """No tool matches the requested name/version."""


class DuplicateToolError(OrchestratorError):
    """A tool with the same name and version is already registered."""


class PermissionDeniedError(OrchestratorError):
    """The caller lacks the scopes required to use a tool."""


class NoRouteError(OrchestratorError):
    """No eligible tool provides the requested capability."""


class ExecutionError(OrchestratorError):
    """A tool invocation or plan step failed."""


class ToolTimeoutError(ExecutionError, TimeoutError):
    """A tool invocation exceeded its time budget."""


class ConflictError(OrchestratorError):
    """Results or resource claims could not be reconciled."""


class CycleError(ValidationError):
    """A plan's dependency graph contains a cycle."""


def parse_version(version: str) -> tuple[int, int, int]:
    match = _VERSION_RE.fullmatch(version) if isinstance(version, str) else None
    if not match:
        raise ValidationError(f"invalid semantic version {version!r}; expected MAJOR.MINOR.PATCH")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def _valid_name(name: object) -> bool:
    return isinstance(name, str) and _NAME_RE.fullmatch(name) is not None


@dataclass(frozen=True)
class Capability:
    """A capability a tool offers, with hints used for routing."""

    name: str
    cost: float = 1.0
    latency: float = 1.0
    priority: int = 0

    def __post_init__(self) -> None:
        if not _valid_name(self.name):
            raise ValidationError(f"invalid capability name {self.name!r}")
        for hint in ("cost", "latency"):
            value = getattr(self, hint)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or math.isnan(value)
                or value < 0
            ):
                raise ValidationError(
                    f"capability {self.name!r}: {hint} must be a non-negative number, "
                    f"got {value!r}"
                )
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise ValidationError(f"capability {self.name!r}: priority must be an int")


@dataclass(frozen=True)
class ToolSpec:
    """Immutable description of a tool and the coroutine that runs it."""

    name: str
    version: str
    capabilities: tuple[Capability, ...]
    handler: Handler
    description: str = ""
    required_scopes: frozenset[str] = frozenset()
    input_schema: Mapping[str, type] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _valid_name(self.name):
            raise ValidationError(f"invalid tool name {self.name!r}")
        parse_version(self.version)
        caps = tuple(
            cap if isinstance(cap, Capability) else Capability(cap) for cap in self.capabilities
        )
        if not caps:
            raise ValidationError(f"tool {self.name!r} must declare at least one capability")
        object.__setattr__(self, "capabilities", caps)
        object.__setattr__(self, "required_scopes", frozenset(self.required_scopes))
        if not inspect.iscoroutinefunction(self.handler):
            raise ValidationError(f"tool {self.name!r}: handler must be an async function")

    @property
    def key(self) -> tuple[str, str]:
        return self.name, self.version

    @property
    def version_tuple(self) -> tuple[int, int, int]:
        return parse_version(self.version)

    @property
    def capability_names(self) -> frozenset[str]:
        return frozenset(cap.name for cap in self.capabilities)

    def capability(self, name: str) -> Capability | None:
        for cap in self.capabilities:
            if cap.name == name:
                return cap
        return None

    def validate_input(self, payload: Mapping[str, Any]) -> None:
        """Check that every required key is present with the declared type."""
        for key, expected in self.input_schema.items():
            if key not in payload:
                raise ValidationError(f"tool {self.name!r}: missing input {key!r}")
            if not isinstance(payload[key], expected):
                raise ValidationError(
                    f"tool {self.name!r}: input {key!r} must be {expected.__name__}"
                )


@dataclass(frozen=True)
class ToolResult:
    """Uniform envelope for the outcome of a tool invocation."""

    tool: str
    version: str
    ok: bool
    value: Any = None
    error: str | None = None
    duration: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict)
