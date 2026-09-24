"""runごとに完全分離した実サービスstackを管理する。"""

from __future__ import annotations

from ui_automation.stack.isolation import (
    IsolationViolation,
    RunIsolation,
    cleanup_failed_isolation,
    create_isolation,
    directory_metadata_signature,
)
from ui_automation.stack.lifecycle import IsolatedStack, StackFailure, recover_stale_run_runtimes

__all__ = [
    "IsolationViolation",
    "IsolatedStack",
    "RunIsolation",
    "StackFailure",
    "cleanup_failed_isolation",
    "create_isolation",
    "directory_metadata_signature",
    "recover_stale_run_runtimes",
]
