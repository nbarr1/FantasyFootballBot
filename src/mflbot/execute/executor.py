"""Turns an approved recommendation into a confirmed MFL action.

The executor consumes ``(recommendation, approval_token)`` pairs and nothing
else. Around the submission itself it does three things that matter as much as
the write:

1. **Re-validates preconditions immediately before submitting.** Approval
   happened at some earlier moment; the world may have moved. If it has, the
   action is abandoned rather than adapted.
2. **Audits unconditionally.** Every attempt writes an audit row -- refused,
   failed or succeeded -- before and after the request.
3. **Confirms by re-reading.** MFL's response to an import is not taken as
   proof. The relevant export is re-read and compared against the intent.

On failure it never retries with a modified action. A changed action is a new
action, and a new action needs a new approval.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..approval.token import ApprovalToken
from ..errors import ApprovalError, PreconditionFailed, TransportError
from ..mfl.endpoints import Capability
from ..recommend.models import (
    AddDropPayload,
    LineupPayload,
    Recommendation,
    RecommendationStatus,
    TradeProposalPayload,
    TradeResponsePayload,
)

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ExecutionOutcome:
    recommendation_id: str
    submitted: bool
    confirmed: bool
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.submitted and self.confirmed


class Executor:
    def __init__(self, write_client, read_client, repos, store) -> None:
        self._write = write_client
        self._read = read_client
        self._repos = repos
        self._store = store

    # -- preconditions ------------------------------------------------------

    def check_preconditions(self, recommendation: Recommendation) -> None:
        """Raise :class:`PreconditionFailed` if the world moved since approval."""
        payload = recommendation.payload

        if recommendation.is_expired:
            raise PreconditionFailed(
                f"Recommendation {recommendation.id} expired at "
                f"{recommendation.expires_at:%Y-%m-%d %H:%M UTC}."
            )

        if isinstance(payload, AddDropPayload):
            self._check_add_drop(payload)
        elif isinstance(payload, LineupPayload):
            self._check_lineup(payload)
        elif isinstance(payload, TradeProposalPayload):
            self._check_trade_window()
        elif isinstance(payload, TradeResponsePayload):
            self._check_offer_still_open(payload)

    def _check_add_drop(self, payload: AddDropPayload) -> None:
        if payload.add_player_id:
            free_agents = set(
                self._repos.current_free_agents(payload.league_id, self._season())
            )
            # Re-read rather than trusting the snapshot the analysis used.
            try:
                from ..ingest.league_state import parse_free_agents

                live = set(
                    parse_free_agents(
                        self._read.export(
                            "freeAgents", L=payload.league_id, force_refresh=True
                        ).payload
                    )
                )
            except Exception as exc:  # noqa: BLE001
                raise PreconditionFailed(
                    f"Could not confirm {payload.add_player_id} is still available: {exc}"
                ) from exc
            if payload.add_player_id not in live:
                raise PreconditionFailed(
                    f"Player {payload.add_player_id} is no longer a free agent -- "
                    f"someone claimed them after you approved this. Nothing was "
                    f"submitted; re-run the waiver analysis for current options."
                )
            del free_agents  # snapshot kept only for the comparison above

        if payload.drop_player_id:
            rosters = self._repos.current_rosters(payload.league_id, self._season())
            ours = rosters.get(payload.franchise_id, [])
            if ours and payload.drop_player_id not in {e.player_id for e in ours}:
                raise PreconditionFailed(
                    f"Player {payload.drop_player_id} is no longer on your roster, so "
                    f"there is nothing to drop."
                )

    def _check_lineup(self, payload: LineupPayload) -> None:
        settings = self._repos.load_league_settings(payload.league_id, self._season())
        if settings is None:
            raise PreconditionFailed(
                "League settings are not available, so the lineup deadline cannot be "
                "checked. Run `bot sync-config` first."
            )
        if settings.lineup_deadline is not None:
            if datetime.now(UTC) >= settings.lineup_deadline:
                raise PreconditionFailed(
                    f"The week {payload.week} lineup locked at "
                    f"{settings.lineup_deadline:%Y-%m-%d %H:%M UTC}. Nothing was "
                    f"submitted."
                )

    def _check_trade_window(self) -> None:
        settings = self._repos.load_league_settings(
            self._read.league.id, self._season()
        )
        if settings is None:
            raise PreconditionFailed("League settings unavailable; cannot check the "
                                     "trade deadline.")
        window = settings.trade_window_open()
        if window is None:
            raise PreconditionFailed(
                "The trade deadline is unknown, so this proposal will not be sent."
            )
        if not window:
            raise PreconditionFailed(
                f"The trade deadline passed at "
                f"{settings.trade_deadline:%Y-%m-%d %H:%M UTC}."
            )

    def _check_offer_still_open(self, payload: TradeResponsePayload) -> None:
        try:
            pending = self._read.pending_trades(franchise=payload.franchise_id)
        except Exception as exc:  # noqa: BLE001
            raise PreconditionFailed(
                f"Could not confirm offer {payload.offer_id} is still open: {exc}"
            ) from exc
        blob = str(pending)
        if payload.offer_id not in blob:
            raise PreconditionFailed(
                f"Trade offer {payload.offer_id} is no longer pending -- it was "
                f"withdrawn or already resolved. Nothing was submitted."
            )

    def _season(self) -> int:
        return self._read.league.season

    # -- execution ----------------------------------------------------------

    def execute(
        self, recommendation: Recommendation, token: ApprovalToken
    ) -> ExecutionOutcome:
        payload = recommendation.payload
        capability = payload.capability

        try:
            self.check_preconditions(recommendation)
        except PreconditionFailed as exc:
            self._repos.audit(
                recommendation_id=recommendation.id,
                token_id=token.token_id,
                capability=str(capability),
                request_summary=payload.describe(),
                outcome="refused",
                response_summary=str(exc),
            )
            self._store.set_status(recommendation.id, RecommendationStatus.FAILED)
            return ExecutionOutcome(
                recommendation.id, submitted=False, confirmed=False, message=str(exc)
            )

        self._repos.audit(
            recommendation_id=recommendation.id,
            token_id=token.token_id,
            capability=str(capability),
            request_summary=payload.describe(),
            outcome="submitting",
        )

        try:
            result = self._write.submit(payload, token)
        except (ApprovalError, TransportError) as exc:
            self._repos.audit(
                recommendation_id=recommendation.id,
                token_id=token.token_id,
                capability=str(capability),
                request_summary=payload.describe(),
                outcome="failed",
                response_summary=str(exc),
            )
            self._store.set_status(recommendation.id, RecommendationStatus.FAILED)
            return ExecutionOutcome(
                recommendation.id, submitted=False, confirmed=False, message=str(exc)
            )

        confirmed, note = (False, "not checked")
        if result.succeeded:
            confirmed, note = self.confirm(payload)

        self._repos.audit(
            recommendation_id=recommendation.id,
            token_id=token.token_id,
            capability=str(capability),
            endpoint_type=result.endpoint_type,
            request_summary=result.request_summary,
            outcome="confirmed" if confirmed else ("submitted" if result.succeeded else "failed"),
            response_summary=result.body[:500],
            confirmed=confirmed,
            detail={"http_status": result.http_status, "confirmation": note},
        )
        self._store.set_status(
            recommendation.id,
            RecommendationStatus.EXECUTED if result.succeeded else RecommendationStatus.FAILED,
        )

        if not result.succeeded:
            message = (
                f"MFL rejected the submission (HTTP {result.http_status}). "
                f"No retry was attempted -- re-run the analysis and approve a fresh "
                f"action if this is still what you want.\n{result.body[:300]}"
            )
        elif confirmed:
            message = f"Submitted and confirmed: {payload.describe()}"
        else:
            message = (
                f"MFL accepted the submission, but re-reading league state did not "
                f"confirm it took effect ({note}). Check MFL directly before assuming "
                f"it worked."
            )

        return ExecutionOutcome(
            recommendation.id,
            submitted=result.succeeded,
            confirmed=confirmed,
            message=message,
            detail={"http_status": result.http_status},
        )

    # -- confirmation -------------------------------------------------------

    def confirm(self, payload) -> tuple[bool, str]:
        """Re-read the relevant export and check the intended change is real."""
        try:
            if isinstance(payload, AddDropPayload):
                return self._confirm_add_drop(payload)
            if isinstance(payload, LineupPayload):
                return self._confirm_lineup(payload)
            if isinstance(payload, (TradeProposalPayload, TradeResponsePayload)):
                return self._confirm_trade(payload)
        except Exception as exc:  # noqa: BLE001 - confirmation must never mask the write
            return False, f"confirmation read failed: {exc}"
        return False, "no confirmation strategy for this action type"

    def _confirm_add_drop(self, payload: AddDropPayload) -> tuple[bool, str]:
        from ..ingest.league_state import parse_rosters

        entries = parse_rosters(
            self._read.export("rosters", L=payload.league_id, force_refresh=True).payload
        )
        ours = {e.player_id for e in entries if e.franchise_id == payload.franchise_id}
        if payload.add_player_id and payload.add_player_id not in ours:
            # In a waiver league a claim is queued, not immediate, so this is
            # expected rather than a failure -- but it is reported as unconfirmed,
            # not as success.
            return False, (
                f"{payload.add_player_id} is not on the roster yet; if this league "
                f"processes waivers on a schedule, the claim is pending"
            )
        if payload.drop_player_id and payload.drop_player_id in ours:
            return False, f"{payload.drop_player_id} is still on the roster"
        return True, "roster reflects the add/drop"

    def _confirm_lineup(self, payload: LineupPayload) -> tuple[bool, str]:
        response = self._read.export(
            "rosters", L=payload.league_id, FRANCHISE=payload.franchise_id,
            force_refresh=True,
        ).payload
        blob = str(response)
        missing = [pid for pid in payload.starter_ids if pid not in blob]
        if missing:
            return False, f"{len(missing)} intended starter(s) not found in the re-read"
        return True, "all intended starters are present on the franchise roster read"

    def _confirm_trade(self, payload) -> tuple[bool, str]:
        pending = self._read.pending_trades(franchise=payload.franchise_id)
        blob = str(pending)
        if isinstance(payload, TradeProposalPayload):
            if payload.to_franchise_id in blob:
                return True, "the proposal appears in pending trades"
            return False, "the proposal was not found in pending trades"
        if payload.offer_id in blob:
            return False, f"offer {payload.offer_id} is still pending"
        return True, "the offer is no longer pending, consistent with the response"
