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
from typing import Any

import httpx

from ..approval.token import ApprovalToken, TokenService
from ..config import LeagueRef
from ..errors import ApprovalError, EndpointNotVerifiedError, TransportError
from ..recommend.models import ActionPayload, payload_hash
from .auth import AuthState, redact
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
            headers={"User-Agent": "mflbot/0.1"},
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
        3. The token must verify against *this* payload and be spent.
        4. Only then is a request built and sent.

        Any failure before step 4 means nothing was sent.
        """
        capability = payload.capability
        endpoint = self.registry.write(capability)
        endpoint_type = endpoint.require_verified()  # raises if unverified

        self.auth.require_writable(f"submitting {capability}")

        unmapped = endpoint.missing_field_mappings(payload.transmitted_fields())
        if unmapped:
            raise EndpointNotVerifiedError(
                f"Write capability '{capability}' has no verified request-parameter "
                f"mapping for: {', '.join(unmapped)}.\n"
                f"  Complete the 'field_map' for this capability in endpoints.lock.json "
                f"(run `bot verify-endpoints` to regenerate the template).\n"
                f"  Nothing was submitted."
            )

        # Consume the approval. This both authorises and, by spending the token,
        # guarantees the same approval cannot drive a second submission.
        self.tokens.consume(token, payload_hash(payload.to_dict()))

        params = self._build_params(payload, endpoint)
        summary = f"import?TYPE={endpoint_type}&" + "&".join(
            f"{k}={v}" for k, v in sorted(params.items()) if k != "TYPE"
        )

        self.rate_limiter.acquire()
        url = f"{self.league.base_url}/import"
        try:
            response = self._client.post(
                url,
                data=params | self.auth.request_params(),
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

    def _build_params(self, payload: ActionPayload, endpoint) -> dict[str, str]:
        """Translate payload fields into MFL request parameters.

        Uses only the verified ``field_map``; there is no fallback that guesses
        a parameter name from a field name.
        """
        data = payload.to_dict()
        params: dict[str, str] = {"TYPE": endpoint.type_name}
        for field_name, param_name in endpoint.field_map.items():
            value = data.get(field_name)
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                params[param_name] = ",".join(str(v) for v in value)
            elif isinstance(value, bool):
                params[param_name] = "1" if value else "0"
            else:
                params[param_name] = str(value)
        return params
