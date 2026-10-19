"""Dynamic, thread-safe tool registry."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from .core import DuplicateToolError, ToolNotFoundError, ToolSpec, parse_version

logger = logging.getLogger(__name__)

EventKind = Literal["registered", "unregistered", "enabled", "disabled"]


@dataclass(frozen=True)
class RegistryEvent:
    kind: EventKind
    name: str
    version: str


Listener = Callable[[RegistryEvent], None]


class ToolRegistry:
    """Holds every known tool, keyed by name then version."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tools: dict[str, dict[str, ToolSpec]] = {}
        # Version-level disablement (``disable(name, version)``) and name-level
        # disablement (``disable(name)``). A name-level flag overrides every
        # version of the name, including versions registered while it is set, so
        # neither a pinned older version nor a newly registered one can slip
        # through. ``enable(name)`` clears both kinds of flag for the name.
        self._disabled: set[tuple[str, str]] = set()
        self._disabled_names: set[str] = set()
        self._listeners: list[Listener] = []

    # -- mutation -----------------------------------------------------------

    def register(self, spec: ToolSpec, *, replace: bool = False) -> ToolSpec:
        with self._lock:
            versions = self._tools.setdefault(spec.name, {})
            if spec.version in versions and not replace:
                raise DuplicateToolError(f"tool {spec.name}@{spec.version} is already registered")
            versions[spec.version] = spec
        self._emit(RegistryEvent("registered", spec.name, spec.version))
        return spec

    def unregister(self, name: str, version: str | None = None) -> list[ToolSpec]:
        """Remove one version, or every version when ``version`` is None."""
        with self._lock:
            versions = self._tools.get(name)
            if not versions or (version is not None and version not in versions):
                raise ToolNotFoundError(_ident(name, version))
            targets = [version] if version is not None else list(versions)
            removed = [versions.pop(v) for v in targets]
            if not versions:
                del self._tools[name]
                self._disabled_names.discard(name)
            for spec in removed:
                self._disabled.discard(spec.key)
        for spec in removed:
            self._emit(RegistryEvent("unregistered", spec.name, spec.version))
        return removed

    def disable(self, name: str, version: str | None = None) -> None:
        """Disable one version, or the whole tool name (every version, now and later).

        Emits one ``disabled`` event naming ``version``, or the latest version when
        the whole name is disabled.
        """
        self._set_enabled(name, version, enabled=False)

    def enable(self, name: str, version: str | None = None) -> None:
        """Enable one version, or the whole tool name.

        Enabling the name clears the name-level flag and every per-version flag of
        that name. Enabling one version clears only that version's own flag, so it
        stays disabled while the name is disabled. Emits one ``enabled`` event.
        """
        self._set_enabled(name, version, enabled=True)

    def _set_enabled(self, name: str, version: str | None, *, enabled: bool) -> None:
        with self._lock:
            spec = self.get(name, version)
            if version is None:
                if enabled:
                    self._disabled_names.discard(name)
                    self._disabled = {key for key in self._disabled if key[0] != name}
                else:
                    self._disabled_names.add(name)
            elif enabled:
                self._disabled.discard(spec.key)
            else:
                self._disabled.add(spec.key)
        self._emit(RegistryEvent("enabled" if enabled else "disabled", spec.name, spec.version))

    # -- queries ------------------------------------------------------------

    def get(self, name: str, version: str | None = None) -> ToolSpec:
        with self._lock:
            versions = self._tools.get(name)
            if not versions:
                raise ToolNotFoundError(_ident(name, version))
            if version is not None:
                try:
                    return versions[version]
                except KeyError:
                    raise ToolNotFoundError(_ident(name, version)) from None
            return max(versions.values(), key=lambda s: s.version_tuple)

    def is_enabled(self, name: str, version: str | None = None) -> bool:
        """Whether that version (default: the latest) and its tool name are both enabled."""
        with self._lock:
            return self._enabled(self.get(name, version))

    def _enabled(self, spec: ToolSpec) -> bool:
        return spec.key not in self._disabled and spec.name not in self._disabled_names

    def list(self, *, include_disabled: bool = True) -> list[ToolSpec]:
        """Latest version of every tool name, sorted by name."""
        with self._lock:
            latest = [self.get(name) for name in sorted(self._tools)]
            if include_disabled:
                return latest
            return [s for s in latest if self._enabled(s)]

    def versions(self, name: str) -> list[str]:
        with self._lock:
            versions = self._tools.get(name)
            if not versions:
                raise ToolNotFoundError(name)
            return sorted(versions, key=parse_version)

    def find_by_capability(self, capability: str) -> list[ToolSpec]:
        """Enabled tools (latest version per name) declaring ``capability``."""
        return [
            spec
            for spec in self.list(include_disabled=False)
            if capability in spec.capability_names
        ]

    def capabilities(self) -> set[str]:
        caps: set[str] = set()
        for spec in self.list(include_disabled=False):
            caps |= spec.capability_names
        return caps

    def __contains__(self, name: object) -> bool:
        with self._lock:
            return name in self._tools

    def __len__(self) -> int:
        with self._lock:
            return len(self._tools)

    # -- events -------------------------------------------------------------

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Register a listener; returns a function that unsubscribes it."""
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def _emit(self, event: RegistryEvent) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                logger.exception("registry listener failed for %s", event)


def _ident(name: str, version: str | None) -> str:
    return f"{name}@{version}" if version else name
