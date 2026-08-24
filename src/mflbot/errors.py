"""Error and missing-data types.

Two rules shape this module:

1. **Fail closed.** When the bot cannot determine something it needs, it raises
   or returns an explicit "I do not know" value. No function in this codebase
   substitutes a plausible default for missing league data.
2. **Blocked, not broken.** A feature that cannot run because of a data gap is
   recorded as blocked with the specific gap, so the user can be told exactly
   what is missing instead of silently receiving degraded recommendations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypeVar

T = TypeVar("T")


class MFLBotError(Exception):
    """Base for every error raised by this package."""


class ConfigError(MFLBotError):
    """The on-disk configuration is missing or invalid."""


class AuthError(MFLBotError):
    """Authentication is missing, malformed, or was rejected by MFL.

    Raised rather than degrading to public/unauthenticated data, because
    pending trades, lineups and franchise-scoped rosters silently differ (or
    vanish) when unauthenticated, which would produce confidently wrong advice.
    """


class RateLimitError(MFLBotError):
    """MFL signalled a rate limit and the client exhausted its backoff budget."""


class TransportError(MFLBotError):
    """Network or HTTP-level failure talking to an upstream API."""


class ParseError(MFLBotError):
    """An upstream response did not match the shape this client expects."""


class EndpointNotVerifiedError(MFLBotError):
    """A write endpoint was invoked before its definition was verified.

    MFL's API documentation is the only authoritative source for import
    endpoint names and parameters. Until ``bot verify-endpoints`` has
    reconciled the local registry against that documentation, write endpoints
    refuse to fire. See :mod:`mflbot.mfl.endpoints`.
    """


class ApprovalError(MFLBotError):
    """An approval token was absent, forged, expired, reused, or bound to a
    different payload than the one being submitted."""


class PreconditionFailed(MFLBotError):
    """League state changed between approval and submission.

    The executor re-reads live state immediately before every write. If the
    world moved (player claimed, lineup locked, trade window closed) the write
    is abandoned and a *fresh* approval is required -- never an automatic retry
    with an adjusted payload.
    """


class PayloadIncomplete(MFLBotError):
    """A payload cannot be translated into a valid MFL request as it stands --
    e.g. a blind-bid waiver claim with no bid amount, or a waiver-order claim
    with no round number. Raised while building the request, strictly before
    the approval token is consumed: an incomplete payload must never burn a
    real approval for a request that was never actually sent.
    """


@dataclass(frozen=True, slots=True)
class Missing:
    """An explicit absent-value marker carrying the reason it is absent.

    Returned by any calculation that would otherwise have to invent a value.
    Truthiness is ``False`` so ``if not result:`` reads naturally, but callers
    are expected to branch on ``isinstance(x, Missing)``.
    """

    reason: str
    detail: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return False

    def __str__(self) -> str:
        if not self.detail:
            return self.reason
        bits = ", ".join(f"{k}={v!r}" for k, v in sorted(self.detail.items()))
        return f"{self.reason} ({bits})"


#: A value that is either present or explicitly, describably missing.
Maybe = T | Missing


def is_missing(value: object) -> bool:
    """True if ``value`` is a :class:`Missing` marker."""
    return isinstance(value, Missing)


@dataclass(frozen=True, slots=True)
class BlockedFeature:
    """A feature disabled at runtime because required data is unavailable."""

    feature: str
    reason: str
    gaps: tuple[str, ...] = ()
    remedy: str | None = None

    def describe(self) -> str:
        lines = [f"{self.feature}: BLOCKED -- {self.reason}"]
        lines.extend(f"    gap: {g}" for g in self.gaps)
        if self.remedy:
            lines.append(f"    remedy: {self.remedy}")
        return "\n".join(lines)
