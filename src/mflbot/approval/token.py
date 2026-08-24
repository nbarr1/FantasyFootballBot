"""Approval tokens: the mechanism that makes "no write without approval" structural.

The rule this module enforces:

    A write to MFL requires a token. A token can only be minted by an approval
    channel acting on an explicit user decision. A token is bound to one exact
    payload, is valid once, and expires.

Consequences that fall out of that, deliberately:

* **Silence never executes.** A token is only created by an affirmative
  decision. An un-answered recommendation simply expires.
* **Editing invalidates.** The token signs the payload hash, so if the user
  edits an action after approving it, the old token no longer matches and the
  executor refuses. A fresh approval is required for the edited payload.
* **Approving one item approves only that item.** Tokens are per-recommendation;
  there is no batch token.
* **A leaked or replayed token is inert.** Consumption is a single atomic
  UPDATE guarded on ``consumed_at IS NULL``, so a token cannot be spent twice
  even under concurrent execution.
"""

from __future__ import annotations

import hmac
import os
import secrets
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from ..errors import ApprovalError
from ..storage.db import Database, utc_now_iso

ENV_SECRET = "MFLBOT_APPROVAL_SECRET"
DEFAULT_SECRET_PATH = Path(".approval_secret")
DEFAULT_TTL = timedelta(hours=12)


def load_or_create_secret(path: Path | str = DEFAULT_SECRET_PATH) -> bytes:
    """Return the HMAC key, generating one on first use.

    Environment first, so a container can inject it. Otherwise a random key is
    written to a mode-0600 file. The key is never logged and never leaves this
    process except as a signature.
    """
    from_env = os.environ.get(ENV_SECRET)
    if from_env:
        return from_env.encode("utf-8")

    path = Path(path)
    if path.exists():
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ApprovalError(
                f"{path} is group/world readable. Anyone who can read it can mint "
                f"approval tokens. Run: chmod 600 {path}"
            )
        return path.read_bytes().strip()

    secret = secrets.token_hex(32).encode("ascii")
    path.write_bytes(secret)
    path.chmod(0o600)
    return secret


@dataclass(frozen=True, slots=True)
class ApprovalToken:
    token_id: str
    recommendation_id: str
    payload_hash: str
    signature: str
    issued_at: datetime
    expires_at: datetime
    approved_by: str

    @property
    def is_expired(self) -> bool:
        return datetime.now(UTC) >= self.expires_at


def _sign(secret: bytes, token_id: str, recommendation_id: str, payload_hash: str) -> str:
    message = f"{token_id}|{recommendation_id}|{payload_hash}".encode()
    return hmac.new(secret, message, sha256).hexdigest()


@dataclass(slots=True)
class TokenService:
    """Mints and redeems approval tokens."""

    db: Database
    secret: bytes
    ttl: timedelta = DEFAULT_TTL

    @classmethod
    def create(
        cls, db: Database, secret_path: Path | str = DEFAULT_SECRET_PATH, **kwargs
    ) -> TokenService:
        return cls(db=db, secret=load_or_create_secret(secret_path), **kwargs)

    def issue(self, recommendation, approved_by: str) -> ApprovalToken:
        """Mint a token for one recommendation's current payload.

        Called only by an approval channel, only after a user decision. The
        expiry is the sooner of the token TTL and the recommendation's own
        expiry, so a token can never outlive the action's validity window (a
        lineup token dies at lock, not twelve hours later).
        """
        token_id = uuid.uuid4().hex
        payload_hash = recommendation.payload_hash
        signature = _sign(self.secret, token_id, recommendation.id, payload_hash)
        issued_at = datetime.now(UTC)
        expires_at = min(issued_at + self.ttl, recommendation.expires_at)
        if expires_at <= issued_at:
            raise ApprovalError(
                f"Recommendation {recommendation.id} expired at "
                f"{recommendation.expires_at:%Y-%m-%d %H:%M UTC}; it can no longer "
                f"be approved. Re-run the analysis to get a current one."
            )

        self.db.execute(
            "INSERT INTO approval_tokens (token_id, recommendation_id, payload_hash, "
            "signature, issued_at, expires_at, consumed_at, approved_by) "
            "VALUES (?,?,?,?,?,?,NULL,?)",
            (
                token_id,
                recommendation.id,
                payload_hash,
                signature,
                issued_at.isoformat(),
                expires_at.isoformat(),
                approved_by,
            ),
        )
        self.db.commit()
        return ApprovalToken(
            token_id=token_id,
            recommendation_id=recommendation.id,
            payload_hash=payload_hash,
            signature=signature,
            issued_at=issued_at,
            expires_at=expires_at,
            approved_by=approved_by,
        )

    def verify(self, token: ApprovalToken, payload_hash: str) -> None:
        """Check a token without consuming it. Raises on any mismatch."""
        expected = _sign(
            self.secret, token.token_id, token.recommendation_id, token.payload_hash
        )
        if not hmac.compare_digest(expected, token.signature):
            raise ApprovalError(
                f"Approval token {token.token_id} has an invalid signature and will "
                f"not be honoured."
            )
        if token.payload_hash != payload_hash:
            raise ApprovalError(
                "This approval was given for a different action than the one being "
                "submitted. The payload changed after approval, so the token no "
                "longer applies.\n"
                f"  approved: {token.payload_hash[:16]}...\n"
                f"  submitting: {payload_hash[:16]}...\n"
                "Approve the edited action explicitly."
            )
        row = self.db.query_one(
            "SELECT * FROM approval_tokens WHERE token_id=?", (token.token_id,)
        )
        if row is None:
            raise ApprovalError(
                f"Approval token {token.token_id} is not on record. Tokens must be "
                f"issued by the approval interface."
            )
        if row["consumed_at"] is not None:
            raise ApprovalError(
                f"Approval token {token.token_id} was already used at "
                f"{row['consumed_at']}. Each approval authorises exactly one "
                f"submission."
            )
        if token.is_expired:
            raise ApprovalError(
                f"Approval token {token.token_id} expired at "
                f"{token.expires_at:%Y-%m-%d %H:%M UTC}. Approve again if the action "
                f"is still what you want."
            )

    def consume(self, token: ApprovalToken, payload_hash: str) -> None:
        """Verify then atomically spend the token.

        The guarded UPDATE is what makes single-use real: two concurrent
        executors racing on the same token produce exactly one winner.
        """
        self.verify(token, payload_hash)
        cursor = self.db.execute(
            "UPDATE approval_tokens SET consumed_at=? WHERE token_id=? "
            "AND consumed_at IS NULL",
            (utc_now_iso(), token.token_id),
        )
        self.db.commit()
        if cursor.rowcount != 1:
            raise ApprovalError(
                f"Approval token {token.token_id} was consumed concurrently; "
                f"no action was taken."
            )

    def load(self, token_id: str) -> ApprovalToken | None:
        row = self.db.query_one(
            "SELECT * FROM approval_tokens WHERE token_id=?", (token_id,)
        )
        if row is None:
            return None
        return ApprovalToken(
            token_id=row["token_id"],
            recommendation_id=row["recommendation_id"],
            payload_hash=row["payload_hash"],
            signature=row["signature"],
            issued_at=datetime.fromisoformat(row["issued_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
            approved_by=row["approved_by"],
        )
