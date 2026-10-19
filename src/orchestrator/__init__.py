"""Multi-tool orchestrator agent.

Dynamic tool registry, capability-based routing, permission scoping, parallel
execution and conflict resolution, composed by :class:`Orchestrator`.
"""

__version__ = "0.1.0"

from .agent import (
    FallbackExhaustedError,
    FanOut,
    Orchestrator,
    OrchestratorEvent,
    PlanIssue,
    PreflightError,
    RunReport,
    ToolAttempt,
)
from .conflicts import (
    LOCK_MODES,
    ConflictResolver,
    Resolution,
    ResourceLockManager,
    detect_write_conflicts,
)
from .core import (
    Capability,
    ConflictError,
    CycleError,
    DuplicateToolError,
    ExecutionError,
    NoRouteError,
    OrchestratorError,
    PermissionDeniedError,
    ToolNotFoundError,
    ToolResult,
    ToolSpec,
    ToolTimeoutError,
    ValidationError,
    parse_version,
)
from .executor import (
    EventKind,
    ExecutionReport,
    Invoker,
    LockManager,
    ParallelExecutor,
    Plan,
    Ref,
    Step,
    StepEvent,
    StepOutcome,
    StepStatus,
    default_invoker,
)
from .permissions import (
    AccessDeniedError,
    AuditRecord,
    Decision,
    PermissionPolicy,
    Principal,
    scope_matches,
    scopes_cover,
    validate_scope,
)
from .registry import RegistryEvent, ToolRegistry
from .router import (
    BALANCED_WEIGHTS,
    CLOSED,
    HALF_OPEN,
    OPEN,
    STRATEGIES,
    CapabilityRouter,
    RouteExplanation,
    RoutingStrategy,
    ToolStats,
)

__all__ = [
    "__version__",
    # core
    "Capability",
    "ConflictError",
    "CycleError",
    "DuplicateToolError",
    "ExecutionError",
    "NoRouteError",
    "OrchestratorError",
    "PermissionDeniedError",
    "ToolNotFoundError",
    "ToolResult",
    "ToolSpec",
    "ToolTimeoutError",
    "ValidationError",
    "parse_version",
    # registry
    "RegistryEvent",
    "ToolRegistry",
    # router
    "BALANCED_WEIGHTS",
    "CLOSED",
    "HALF_OPEN",
    "OPEN",
    "STRATEGIES",
    "CapabilityRouter",
    "RouteExplanation",
    "RoutingStrategy",
    "ToolStats",
    # permissions
    "AccessDeniedError",
    "AuditRecord",
    "Decision",
    "PermissionPolicy",
    "Principal",
    "scope_matches",
    "scopes_cover",
    "validate_scope",
    # executor
    "EventKind",
    "ExecutionReport",
    "Invoker",
    "LockManager",
    "ParallelExecutor",
    "Plan",
    "Ref",
    "Step",
    "StepEvent",
    "StepOutcome",
    "StepStatus",
    "default_invoker",
    # conflicts
    "LOCK_MODES",
    "ConflictResolver",
    "Resolution",
    "ResourceLockManager",
    "detect_write_conflicts",
    # agent
    "FallbackExhaustedError",
    "FanOut",
    "Orchestrator",
    "OrchestratorEvent",
    "PlanIssue",
    "PreflightError",
    "RunReport",
    "ToolAttempt",
]
