"""Approval interface (Component 4)."""

from .channel import ApprovalChannel, ApprovalDecision
from .token import ApprovalToken, TokenService

__all__ = ["ApprovalChannel", "ApprovalDecision", "ApprovalToken", "TokenService"]
