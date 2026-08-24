"""Approval-gated writes to MFL.

Every method here takes an :class:`~mflbot.approval.token.ApprovalToken`. There
is no code path that submits without one, and the token is consumed -- verified,
matched against the exact payload, and atomically spent -- *before* the HTTP
request is built. A caller cannot skip that step, because building the request
is not a separate public method.

Analysis code is never handed one of these objects. See
``tests/test_write_isolation.py``, which asserts that no module under
``mflbot/analysis`` imports this one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import httpx

from ..approval.token import ApprovalToken, TokenService
from ..config import LeagueRef
from ..errors import EndpointNotVerifiedError, TransportError
from ..recommend.models import ActionPayload, payload_hash
from .auth import AuthState, redact
from .client import resolve_user_agent
from .endpoints import Capability, EndpointRegistry
from .ratelimit import RateLimiter, RateLimitPolicy

log = logging.getLogger(__name__)


@dataclass(slots=True)
class WriteResult:
    capability: Capability
    endpoint_type: str
    request_summary: str
    http_status: int
    body: str
    succeeded: bool
    #: Set once the write has been confirmed by re-reading league state.
    confirmed: bool = False
    confirmation_note: str = ""


class MFLWriteClient:
    """Submits approved actions to MFL's ``import`` API. Nothing else."""

    def __init__(
        self,
        league: LeagueRef,
        auth: AuthState,
        token_service: TokenService,
        *,
        registry: EndpointRegistry | None = None,
        rate_limiter: RateLimiter | None = None,
        transport: httpx.Client | None = None,
    ) -> None:
        self.league = league
        self.auth = auth
        self.tokens = token_service
        self.registry = registry or EndpointRegistry.load()
        self.rate_limiter = rate_limiter or RateLimiter(RateLimitPolicy())
        self._client = transport or httpx.Client(
            timeout=httpx.Timeout(30.0),
            headers={"User-Agent": resolve_user_agent()},
            follow_redirects=True,
        )
        self._owns_transport = transport is None

    def close(self) -> None:
        if self._owns_transport:
            self._client.close()

    def __enter__(self) -> MFLWriteClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- the single write path --------------------------------------------

    def submit(self, payload: ActionPayload, token: ApprovalToken) -> WriteResult:
        """Submit one approved action.

        Order matters and is not negotiable:

        1. The endpoint must be verified against MFL's documentation.
        2. The session must be write-capable.
        3. The request must be fully buildable -- the payload has everything
           its wire format needs (a bid amount, a waiver round, whatever the
           capability requires).
        4. Only then is the token verified against *this* payload and spent.
        5. Only then is the request actually sent.

        Step 3 comes before step 4 deliberately: a payload that fails to build
        must never consume a real approval for a request that was never sent.
        Any failure before step 5 means nothing was sent.
        """
        capability = payload.capability
        endpoint = self.registry.write(capability)
        endpoint_type = endpoint.require_verified()  # raises if unverified

        self.auth.require_writable(f"submitting {capability}")

        # Build the request now, before touching the token. wire_fields() can
        # raise PayloadIncomplete (a blind-bid claim with no bid amount, a
        # waiver-order claim with no round) -- that must surface here, not
        # after the approval has been spent, or an incomplete payload burns a
        # real approval for a request that was never actually sent.
        wire = payload.wire_fields()
        unmapped = endpoint.missing_field_mappings(tuple(wire))
        if unmapped:
            raise EndpointNotVerifiedError(
                f"Write capability '{capability}' has no verified request-parameter "
                f"mapping for: {', '.join(unmapped)}.\n"
                f"  Complete the 'field_map' for this capability in endpoints.lock.json "
                f"(run `bot verify-endpoints` to regenerate the template).\n"
                f"  Nothing was submitted."
            )
        params = self._params_from_wire(wire, endpoint)

        # Consume the approval. This both authorises and, by spending the token,
        # guarantees the same approval cannot drive a second submission.
        self.tokens.consume(token, payload_hash(payload.to_dict()))

        summary = f"import?TYPE={endpoint_type}&" + "&".join(
            f"{k}={v}" for k, v in sorted(params.items()) if k != "TYPE"
        )

        self.rate_limiter.acquire()
        url = f"{self.league.base_url}/import"
        try:
            # No APIKEY here, deliberately: MFL's docs state the API key
            # alternate-auth path "does not work for import requests, only
            # export" (and does not work for actions requiring commissioner
            # access either way). Every import is therefore authorised solely
            # by the session cookie, which require_writable() above already
            # guarantees is present.
            response = self._client.post(
                url,
                data=params,
                headers=self.auth.request_headers(),
            )
        except httpx.HTTPError as exc:
            raise TransportError(
                f"Submitting {capability} failed at the network layer: {exc}. "
                f"The approval has been spent; re-approve if you want to retry."
            ) from exc

        body = response.text[:2000]
        succeeded = response.status_code == 200 and "error" not in body.lower()
        log.info(
            "submitted %s -> HTTP %s (%s)",
            capability,
            response.status_code,
            "ok" if succeeded else "rejected",
        )
        return WriteResult(
            capability=capability,
            endpoint_type=endpoint_type,
            request_summary=redact(summary),
            http_status=response.status_code,
            body=redact(body),
            succeeded=succeeded,
        )

    def _params_from_wire(self, wire: dict, endpoint) -> dict[str, str]:
        """Translate a payload's wire fields into MFL request parameters.

        Uses only the verified ``field_map``; there is no fallback that guesses
        a parameter name from a field name. ``wire`` is the output of
        :meth:`~mflbot.recommend.models.ActionPayload.wire_fields` -- already
        shaped for this specific capability, not the raw dataclass fields (see
        that method for why the two can differ).

        The DATA/XML question this docstring used to flag as open is resolved:
        MFL's Request Reference Page confirms all six of this bot's write
        capabilities take flat key=value parameters, not an XML DATA blob
        (that format is used elsewhere -- draftResults, auctionResults,
        salaries -- none of which this bot writes to).
        """
        params: dict[str, str] = {"TYPE": endpoint.type_name}
        for field_name, param_name in endpoint.field_map.items():
            value = wire.get(field_name)
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                params[param_name] = ",".join(str(v) for v in value)
            elif isinstance(value, bool):
                params[param_name] = "1" if value else "0"
            elif isinstance(value, datetime):
                params[param_name] = str(int(value.timestamp()))
            else:
                params[param_name] = str(value)
        return params
