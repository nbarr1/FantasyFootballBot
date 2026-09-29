"""Turns an approved recommendation into a confirmed MFL action.

The executor consumes ``(recommendation, approval_token)`` pairs and nothing
else. Around the submission itself it does four things that matter as much as
the write:

0. **Confirms the approval still stands.** The recommendation is re-read from
   the store, and it must be ``approved`` *now* -- not when the caller loaded
   it -- and the token must have been issued for this recommendation. A
   rejected, edited or already-executed recommendation is refused, whatever
   token accompanies it.
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
from ..errors import MFLBotError, PreconditionFailed, TransportError
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

    def _pending_offers(self, franchise_id: str):
        """The pending trades involving ``franchise_id``, parsed and fresh."""
        from ..ingest.league_state import parse_pending_trades

        offers, _ = parse_pending_trades(
            self._read.pending_trades(franchise=franchise_id, force_refresh=True)
        )
        return offers

    def _check_offer_still_open(self, payload: TradeResponsePayload) -> None:
        try:
            offers = self._pending_offers(payload.franchise_id)
        except Exception as exc:  # noqa: BLE001
            raise PreconditionFailed(
                f"Could not confirm offer {payload.offer_id} is still open: {exc}"
            ) from exc
        # An exact id match on a parsed offer. A text search of the response
        # would find "12" inside "123", or inside a timestamp.
        if not any(o.trade_id == payload.offer_id for o in offers):
            raise PreconditionFailed(
                f"Trade offer {payload.offer_id} is no longer pending -- it was "
                f"withdrawn or already resolved. Nothing was submitted."
            )

    def _season(self) -> int:
        return self._read.league.season

    # -- execution ----------------------------------------------------------

    def authorisation_refusal(
        self, recommendation: Recommendation | None, token: ApprovalToken
    ) -> str | None:
        """Why this token may not drive this recommendation, or None if it may."""
        if recommendation is None:
            return "That recommendation is not on record; nothing was submitted."
        if getattr(token, "recommendation_id", None) != recommendation.id:
            return (
                f"Approval token {token.token_id} was issued for recommendation "
                f"{getattr(token, 'recommendation_id', '?')}, not {recommendation.id}. "
                f"An approval authorises only the recommendation it was given for. "
                f"Nothing was submitted."
            )
        if recommendation.status != RecommendationStatus.APPROVED:
            return (
                f"Recommendation {recommendation.id} is {recommendation.status}, not "
                f"approved, so it cannot be submitted. Nothing was submitted."
            )
        return None

    def _refuse(self, recommendation_id: str, token, capability, summary: str,
                reason: str) -> ExecutionOutcome:
        """Record a refusal that sent nothing and leaves the recommendation as it was."""
        self._repos.audit(
            recommendation_id=recommendation_id,
            token_id=getattr(token, "token_id", None),
            capability=str(capability) if capability else None,
            request_summary=summary,
            outcome="refused",
            response_summary=reason,
        )
        return ExecutionOutcome(recommendation_id, submitted=False, confirmed=False,
                                message=reason)

    def execute(
        self, recommendation: Recommendation, token: ApprovalToken
    ) -> ExecutionOutcome:
        # Decide on the stored recommendation, never the caller's copy: it may
        # have been loaded before an approval, a rejection or an edit.
        current = self._store.get(recommendation.id)
        refusal = self.authorisation_refusal(current, token)
        if refusal is not None:
            source = current or recommendation
            return self._refuse(recommendation.id, token, source.payload.capability,
                                source.payload.describe(), refusal)
        recommendation = current
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
        except TransportError as exc:
            # Raised after the token was spent: the request may or may not have
            # reached MFL, so the action is over and a retry needs a new approval.
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
        except MFLBotError as exc:
            # Everything else the write client raises -- a refused token, an
            # unverified endpoint, an incomplete payload, no write session --
            # happens before anything is sent. Nothing about the action itself
            # failed, so its status is left as it was.
            return self._refuse(recommendation.id, token, capability,
                                payload.describe(), f"Not submitted: {exc}")

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
        """Read the submitted lineup back and compare it with what was sent.

        Checking that the starters are on the roster proves nothing -- they
        were before the write too. What counts is that MFL now lists exactly
        these players as this franchise's starters for this week.
        """
        from ..ingest.league_state import submitted_starters

        response = self._read.export(
            "weeklyResults", L=payload.league_id, W=payload.week, force_refresh=True,
        ).payload
        now_starting = set(submitted_starters(response, payload.franchise_id))
        if not now_starting:
            return False, "the submitted lineup could not be read back"
        intended = set(payload.starter_ids)
        if now_starting == intended:
            return True, "MFL lists exactly the intended starters"
        missing, extra = intended - now_starting, now_starting - intended
        return False, (
            f"MFL's starters differ from what was sent: missing "
            f"{sorted(missing) or 'none'}, unexpected {sorted(extra) or 'none'}"
        )

    def _confirm_trade(self, payload) -> tuple[bool, str]:
        offers = self._pending_offers(payload.franchise_id)
        if isinstance(payload, TradeProposalPayload):
            match = any(
                o.offering_franchise == payload.franchise_id
                and o.offered_to == payload.to_franchise_id
                and set(o.gives) == set(payload.gives_player_ids)
                and set(o.receives) == set(payload.receives_player_ids)
                for o in offers
            )
            if match:
                return True, "the proposal appears in pending trades"
            return False, "no pending trade matches the proposal that was sent"
        if any(o.trade_id == payload.offer_id for o in offers):
            return False, f"offer {payload.offer_id} is still pending"
        return True, "the offer is no longer pending, consistent with the response"
