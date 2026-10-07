"""Provider adapters. Each provider fails honestly: missing credentials raise
ProviderBlocked and the pipeline parks the job in BLOCKED_PROVIDER instead of
fabricating output."""

from __future__ import annotations


class ProviderBlocked(Exception):
    def __init__(self, provider: str, reason: str):
        super().__init__(f"{provider}: {reason}")
        self.provider = provider
        self.reason = reason


class ProviderError(Exception):
    def __init__(self, provider: str, reason: str):
        super().__init__(f"{provider}: {reason}")
        self.provider = provider
        self.reason = reason
