"""Persistence for recommendations.

Every recommendation is durable and queryable after the fact, together with the
decision the user made and what happened when it was executed. That is the
audit trail requirement: nothing the bot proposes is ephemeral.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..mfl.endpoints import Capability
from ..storage.db import Database, utc_now_iso
from .models import (
    ActionPayload,
    AddDropPayload,
    Confidence,
    Evidence,
    LineupPayload,
    Recommendation,
    RecommendationKind,
    RecommendationStatus,
    TradeProposalPayload,
    TradeResponsePayload,
    canonical_json,
)

_PAYLOAD_TYPES: dict[str, type[ActionPayload]] = {
    RecommendationKind.LINEUP: LineupPayload,
    RecommendationKind.ADD_DROP: AddDropPayload,
    RecommendationKind.TRADE_PROPOSAL: TradeProposalPayload,
    RecommendationKind.TRADE_RESPONSE: TradeResponsePayload,
}


def _parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def payload_from_dict(kind: str, data: dict[str, Any]) -> ActionPayload:
    cls = _PAYLOAD_TYPES[RecommendationKind(kind)]
    data = dict(data)
    data["capability"] = Capability(data["capability"])
    for key, value in list(data.items()):
        # Tuples round-trip through JSON as lists; restore them so payload
        # hashes computed before and after a save agree.
        annotation = cls.__annotations__.get(key, "")
        if isinstance(value, list) and "tuple" in str(annotation):
            data[key] = tuple(value)
    return cls(**data)


@dataclass(slots=True)
class RecommendationStore:
    db: Database

    def save(self, recommendation: Recommendation) -> Recommendation:
        self.db.execute(
            """
            INSERT INTO recommendations (id, kind, created_at, expires_at, status,
                payload_json, payload_hash, rationale, evidence_json, confidence,
                caveats_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status, payload_json=excluded.payload_json,
                payload_hash=excluded.payload_hash, rationale=excluded.rationale,
                evidence_json=excluded.evidence_json, caveats_json=excluded.caveats_json,
                expires_at=excluded.expires_at
            """,
            (
                recommendation.id,
                str(recommendation.kind),
                recommendation.created_at.isoformat(),
                recommendation.expires_at.isoformat(),
                str(recommendation.status),
                canonical_json(recommendation.payload_dict),
                recommendation.payload_hash,
                recommendation.rationale,
                json.dumps(recommendation.evidence.to_dict(), separators=(",", ":")),
                str(recommendation.confidence),
                json.dumps(list(recommendation.caveats)),
            ),
        )
        self.db.commit()
        return recommendation

    def _row_to_recommendation(self, row) -> Recommendation:
        evidence_raw = json.loads(row["evidence_json"])
        return Recommendation(
            id=row["id"],
            kind=RecommendationKind(row["kind"]),
            payload=payload_from_dict(row["kind"], json.loads(row["payload_json"])),
            rationale=row["rationale"],
            evidence=Evidence(
                projections=evidence_raw.get("projections", {}),
                news_refs=tuple(evidence_raw.get("news_refs", [])),
                sources=tuple(evidence_raw.get("sources", [])),
                notes=evidence_raw.get("notes", {}),
            ),
            confidence=Confidence(row["confidence"]),
            caveats=tuple(json.loads(row["caveats_json"])),
            created_at=_parse_dt(row["created_at"]),
            expires_at=_parse_dt(row["expires_at"]),
            status=RecommendationStatus(row["status"]),
        )

    def get(self, recommendation_id: str) -> Recommendation | None:
        row = self.db.query_one(
            "SELECT * FROM recommendations WHERE id=?", (recommendation_id,)
        )
        return self._row_to_recommendation(row) if row else None

    def pending(self, *, include_expired: bool = False) -> list[Recommendation]:
        rows = self.db.query(
            "SELECT * FROM recommendations WHERE status=? ORDER BY created_at",
            (str(RecommendationStatus.PROPOSED),),
        )
        out = [self._row_to_recommendation(r) for r in rows]
        if include_expired:
            return out
        return [r for r in out if not r.is_expired]

    def all(self, limit: int = 100) -> list[Recommendation]:
        rows = self.db.query(
            "SELECT * FROM recommendations ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [self._row_to_recommendation(r) for r in rows]

    def set_status(
        self, recommendation_id: str, status: RecommendationStatus
    ) -> None:
        column = "executed_at" if status == RecommendationStatus.EXECUTED else "decided_at"
        self.db.execute(
            f"UPDATE recommendations SET status=?, {column}=? WHERE id=?",  # noqa: S608
            (str(status), utc_now_iso(), recommendation_id),
        )
        self.db.commit()

    def replace_payload(
        self, recommendation_id: str, payload: ActionPayload
    ) -> Recommendation | None:
        """Apply a user edit. The payload hash changes, which invalidates any
        token already issued against the previous payload."""
        recommendation = self.get(recommendation_id)
        if recommendation is None:
            return None
        recommendation.payload = payload
        self.save(recommendation)
        return recommendation

    def expire_stale(self) -> int:
        """Mark elapsed recommendations expired.

        Expiry is the *only* terminal state silence can produce. A recommendation
        never ages into execution.
        """
        now = datetime.now(UTC).isoformat()
        cursor = self.db.execute(
            "UPDATE recommendations SET status=? WHERE status=? AND expires_at < ?",
            (str(RecommendationStatus.EXPIRED), str(RecommendationStatus.PROPOSED), now),
        )
        self.db.commit()
        return cursor.rowcount or 0
