"""Pluggable CLI execution backends for ANCOR (Hermes, Codex, AGY, DSH, etc.)."""
from .agy import AgyBackend
from .base import BackendCapabilities, ExecutionBackend, LaunchSpec, OutputEvent
from .codex import CodexBackend
from .dsh import DshBackend
from .hermes import HermesBackend
from .registry import BackendRegistry, BackendRouter, ResolvedRoute

__all__ = [
    'AgyBackend',
    'BackendCapabilities',
    'BackendRegistry',
    'BackendRouter',
    'CodexBackend',
    'DshBackend',
    'ExecutionBackend',
    'HermesBackend',
    'LaunchSpec',
    'OutputEvent',
    'ResolvedRoute',
]
