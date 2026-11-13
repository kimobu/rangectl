from __future__ import annotations

from .models import ActivityConfigError, AgentConfig, RangeActivity

__all__ = [
    "ActivityConfigError",
    "AgentConfig",
    "RangeActivity",
    "compile_activity",
]


def compile_activity(*args, **kwargs):
    from .compiler import compile_activity as _compile_activity

    return _compile_activity(*args, **kwargs)
