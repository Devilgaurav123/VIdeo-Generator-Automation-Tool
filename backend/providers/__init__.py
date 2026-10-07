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


class ProviderCreditsExhausted(ProviderBlocked):
    """Provider account is out of budget/credits. Parks the job in
    WAITING_FOR_CREDITS so it can resume once the balance is topped up."""


def is_credit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in ("budget", "ratelimit", "rate limit", "insufficient_quota", "insufficient quota", "402", "credit"))
