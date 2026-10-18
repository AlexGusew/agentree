"""Agent providers (opencode, Claude Code, ...): see base.py for the contract and registry.py
for descriptors / lookup. This package init stays lightweight: it never imports a concrete
provider module."""
from __future__ import annotations

from agentree.providers.base import Provider, ProviderSpec

__all__ = ["Provider", "ProviderSpec"]
