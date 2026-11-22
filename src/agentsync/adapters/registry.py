"""Plugin-style adapter registry enabling new tools without modifying core sync logic."""

from __future__ import annotations

from collections.abc import Callable

from agentsync.adapters.base import ToolAdapter
from agentsync.config import AgentSyncConfig, ToolConfig


class AdapterRegistry:
    """Registry mapping canonical tool names to ToolAdapter classes."""

    def __init__(self) -> None:
        self._adapters: dict[str, type[ToolAdapter]] = {}

    def register(self, adapter_cls: type[ToolAdapter]) -> type[ToolAdapter]:
        if not adapter_cls.tool_name:
            raise ValueError(f"Adapter class {adapter_cls.__name__} must define tool_name")
        self._adapters[adapter_cls.tool_name] = adapter_cls
        return adapter_cls

    def get(self, tool_name: str) -> type[ToolAdapter]:
        if tool_name not in self._adapters:
            available = ", ".join(sorted(self._adapters))
            raise KeyError(f"Unknown tool adapter '{tool_name}'. Available: {available}")
        return self._adapters[tool_name]

    def list_names(self) -> list[str]:
        return sorted(self._adapters.keys())

    def create(self, tool_name: str, tool_config: ToolConfig) -> ToolAdapter:
        cls = self.get(tool_name)
        return cls(tool_config)

    def create_enabled(self, config: AgentSyncConfig) -> list[ToolAdapter]:
        adapters: list[ToolAdapter] = []
        for name in self.list_names():
            tcfg = config.tools.get(name)
            if tcfg and tcfg.enabled:
                adapters.append(self.create(name, tcfg))
        return adapters


_GLOBAL_REGISTRY = AdapterRegistry()
_BUILTINS_LOADED = False


def register_adapter(adapter_cls: type[ToolAdapter]) -> type[ToolAdapter]:
    """Class decorator registering a ToolAdapter in the global registry."""
    return _GLOBAL_REGISTRY.register(adapter_cls)


def _load_builtin_adapters() -> None:
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    # Importing modules triggers @register_adapter decorators
    from agentsync.adapters import (  # noqa: F401
        antigravity,
        claude_code,
        codex,
        deepseek_harness,
        opencode,
    )

    _BUILTINS_LOADED = True


def get_registry() -> AdapterRegistry:
    """Return the global AdapterRegistry with all built-in adapters loaded."""
    _load_builtin_adapters()
    return _GLOBAL_REGISTRY


def register_custom_adapter(
    factory: Callable[[], type[ToolAdapter]] | type[ToolAdapter],
) -> type[ToolAdapter]:
    """Register a third-party adapter class dynamically."""
    cls = factory if isinstance(factory, type) else factory()
    return _GLOBAL_REGISTRY.register(cls)
