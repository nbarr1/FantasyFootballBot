"""Reconcile the local endpoint registry against MFL's live API documentation.

Why this exists: MFL's own ``api_info`` page is the authoritative description of
the export and import endpoints, and it was not reachable from the machine that
generated this code. Rather than shipping guessed write endpoints, the registry
ships them **inert**, and this module turns them on -- but only for names the
documentation actually contains.

The flow is:

1. Fetch ``{host}/{season}/api_info?STATE=details``.
2. Extract the documented ``export?TYPE=`` and ``import?TYPE=`` names, plus the
   parameter names mentioned near each.
3. For each write capability, find which of its candidate names the
   documentation confirms. A capability whose candidates all miss is reported,
   not resolved.
4. Write ``endpoints.lock.json`` with the confirmed names and a ``field_map``
   template the user completes for anything that could not be matched
   unambiguously.

Nothing here writes to the league. It is a read of a public documentation page.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import TransportError
from .endpoints import (
    LOCK_FILENAME,
    Capability,
    EndpointRegistry,
)

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_EXPORT_TYPE_RE = re.compile(r"export\?TYPE=([A-Za-z_][A-Za-z0-9_]*)")
_IMPORT_TYPE_RE = re.compile(r"import\?TYPE=([A-Za-z_][A-Za-z0-9_]*)")
# Multi-letter parameter names, plus MFL's single-letter ones. Single letters
# are whitelisted rather than matched generally, because a bare "A" or "I" in
# prose is not a parameter.
_PARAM_RE = re.compile(r"\b([A-Z][A-Z0-9_]{1,24})\b")
_SINGLE_LETTER_PARAMS = frozenset({"L", "W", "F", "P"})
_SINGLE_LETTER_RE = re.compile(r"\b([LWFP])\b")

#: Parameters that appear everywhere and say nothing about a specific endpoint.
_GENERIC_PARAMS = frozenset({"TYPE", "JSON", "XML", "APIKEY", "URL", "HTTP", "HTTPS", "GET", "POST", "API", "MFL", "ID"})

#: Payload field -> MFL parameter, for mappings that are unambiguous from the
#: parameter list itself (the field means the same thing under any naming).
_OBVIOUS_MAPPINGS = {
    "league_id": ("L",),
    "week": ("W", "WEEK"),
    "franchise_id": ("FRANCHISE", "FRANCHISE_ID", "F"),
}


@dataclass(slots=True)
class DocumentedEndpoint:
    type_name: str
    params: tuple[str, ...] = ()
    context: str = ""


@dataclass(slots=True)
class VerificationReport:
    exports: dict[str, DocumentedEndpoint] = field(default_factory=dict)
    imports: dict[str, DocumentedEndpoint] = field(default_factory=dict)
    #: capability -> resolved TYPE name
    resolved_writes: dict[Capability, str] = field(default_factory=dict)
    #: capability -> the candidates that were tried and not found
    unresolved_writes: dict[Capability, tuple[str, ...]] = field(default_factory=dict)
    #: Registry read endpoints the documentation does not mention.
    unknown_reads: tuple[str, ...] = ()
    #: capability -> payload fields still needing a manual parameter mapping.
    incomplete_field_maps: dict[Capability, tuple[str, ...]] = field(default_factory=dict)
    source_url: str = ""

    @property
    def all_writes_resolved(self) -> bool:
        return not self.unresolved_writes

    def render(self) -> str:
        lines = [f"Verified against: {self.source_url}", ""]
        lines.append(f"Documented export types found: {len(self.exports)}")
        lines.append(f"Documented import types found: {len(self.imports)}")
        if self.imports:
            lines.append("  " + ", ".join(sorted(self.imports)))
        lines.append("")

        lines.append("Write capabilities:")
        for capability in Capability:
            if capability in self.resolved_writes:
                type_name = self.resolved_writes[capability]
                missing = self.incomplete_field_maps.get(capability, ())
                state = "READY" if not missing else "NEEDS FIELD MAP"
                lines.append(f"  [{state}] {capability} -> import?TYPE={type_name}")
                if missing:
                    lines.append(
                        f"      map these payload fields in {LOCK_FILENAME}: "
                        f"{', '.join(missing)}"
                    )
                    params = self.imports[type_name].params
                    if params:
                        lines.append(
                            f"      documented parameters: {', '.join(params)}"
                        )
            else:
                tried = self.unresolved_writes.get(capability, ())
                lines.append(f"  [BLOCKED] {capability} -- not found in the docs")
                lines.append(f"      tried: {', '.join(tried) or '(no candidates)'}")
        lines.append("")

        if self.unknown_reads:
            lines.append(
                "Read endpoints this bot uses that the docs did not mention "
                "(they may still work; the page format may just differ):"
            )
            lines.extend(f"  - {name}" for name in self.unknown_reads)
            lines.append("")

        if self.all_writes_resolved and not self.incomplete_field_maps:
            lines.append("All write capabilities are verified and mapped.")
        else:
            lines.append(
                "Writes stay disabled for anything not marked READY above. "
                "That is intentional: the bot will not POST an unverified endpoint."
            )
        return "\n".join(lines)


def _strip_html(html: str) -> str:
    text = _TAG_RE.sub(" ", html)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", text)


def parse_api_info(html: str) -> tuple[dict[str, DocumentedEndpoint], dict[str, DocumentedEndpoint]]:
    """Extract documented export and import endpoints from the api_info page."""
    # Search the raw HTML for TYPE= occurrences (they appear inside links and
    # example URLs), then use the stripped text for parameter context.
    text = _strip_html(html)
    combined = html + " " + text

    exports: dict[str, DocumentedEndpoint] = {}
    imports: dict[str, DocumentedEndpoint] = {}

    for regex, bucket in ((_EXPORT_TYPE_RE, exports), (_IMPORT_TYPE_RE, imports)):
        for match in regex.finditer(combined):
            type_name = match.group(1)
            if type_name in bucket:
                continue
            # Parameters are documented near the type name; take a window of the
            # stripped text around the first textual mention.
            context = ""
            position = text.find(type_name)
            if position >= 0:
                context = text[max(0, position - 200) : position + 1200]
            found = {
                p
                for p in _PARAM_RE.findall(context)
                if p not in _GENERIC_PARAMS and not p.isdigit()
            }
            found |= set(_SINGLE_LETTER_RE.findall(context)) & _SINGLE_LETTER_PARAMS
            params = tuple(sorted(found))
            bucket[type_name] = DocumentedEndpoint(type_name, params, context[:400])
    return exports, imports


def _auto_field_map(payload_fields: tuple[str, ...], params: tuple[str, ...]) -> dict[str, str]:
    """Map payload fields to documented parameters where it is unambiguous."""
    mapping: dict[str, str] = {}
    available = set(params)
    for field_name, candidates in _OBVIOUS_MAPPINGS.items():
        if field_name not in payload_fields:
            continue
        for candidate in candidates:
            if candidate in available:
                mapping[field_name] = candidate
                break
    return mapping


#: Payload fields each capability will need mapped, so the report can say what
#: is still outstanding. Derived from the payload dataclasses.
def _payload_fields(capability: Capability) -> tuple[str, ...]:
    from ..recommend.models import (
        AddDropPayload,
        LineupPayload,
        TradeProposalPayload,
        TradeResponsePayload,
    )

    by_capability = {
        Capability.SUBMIT_LINEUP: LineupPayload,
        Capability.ADD_DROP_FCFS: AddDropPayload,
        Capability.WAIVER_CLAIM_ORDER: AddDropPayload,
        Capability.WAIVER_CLAIM_BBID: AddDropPayload,
        Capability.PROPOSE_TRADE: TradeProposalPayload,
        Capability.RESPOND_TO_TRADE: TradeResponsePayload,
    }
    # The payload class is authoritative about which of its fields are actually
    # transmitted, so this cannot drift from what the write client enforces.
    return by_capability[capability].transmitted_fields()


def verify_endpoints(
    client, registry: EndpointRegistry | None = None, *, lock_path: Path | str = LOCK_FILENAME
) -> VerificationReport:
    """Fetch the documentation, reconcile, and write the lock file."""
    registry = registry or EndpointRegistry.load(lock_path)
    url = client.league.api_docs_url

    try:
        response = client._client.get(url)  # documentation page, not an API call
    except Exception as exc:  # noqa: BLE001
        raise TransportError(
            f"Could not fetch MFL's API documentation at {url}: {exc}"
        ) from exc
    if response.status_code != 200:
        raise TransportError(
            f"MFL's API documentation at {url} returned HTTP {response.status_code}"
        )

    exports, imports = parse_api_info(response.text)
    if not exports and not imports:
        raise TransportError(
            f"Fetched {url} but found no documented endpoints in it. The page format "
            f"may have changed; verify manually before enabling writes."
        )

    report = VerificationReport(exports=exports, imports=imports, source_url=url)
    report.unknown_reads = tuple(
        name for name in sorted(registry.reads) if name not in exports
    )

    lock: dict[str, Any] = {"_source": url, "reads": {}, "writes": {}}

    for name, endpoint in registry.reads.items():
        if name in exports:
            lock["reads"][name] = {
                "params": list(endpoint.params),
                "ttl_seconds": endpoint.ttl_seconds,
                "description": endpoint.description,
                "requires_auth": endpoint.requires_auth,
            }

    existing_writes = {
        cap: registry.writes[cap] for cap in registry.writes if registry.writes[cap].field_map
    }

    for capability, write in registry.writes.items():
        resolved = next((c for c in write.candidates if c in imports), None)
        if resolved is None:
            report.unresolved_writes[capability] = write.candidates
            continue
        report.resolved_writes[capability] = resolved

        payload_fields = _payload_fields(capability)
        documented_params = imports[resolved].params
        mapping = dict(_auto_field_map(payload_fields, documented_params))
        # Preserve any mapping the user already completed by hand.
        previous = existing_writes.get(capability)
        if previous:
            mapping.update(previous.field_map)

        missing = tuple(f for f in payload_fields if f not in mapping)
        if missing:
            report.incomplete_field_maps[capability] = missing
            # Leave an explicit placeholder so the file is self-documenting.
            for field_name in missing:
                mapping.setdefault(field_name, "")

        lock["writes"][str(capability)] = {
            "type_name": resolved,
            "params": list(documented_params),
            "field_map": {k: v for k, v in mapping.items() if v},
            "_unmapped_fields": list(missing),
            "_documented_params": list(documented_params),
        }

    registry.save_lock(lock, lock_path)
    log.info("Wrote %s", lock_path)
    return report
