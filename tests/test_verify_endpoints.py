"""Endpoint verification and the fail-closed write gate."""

from __future__ import annotations

import pytest

from mflbot.errors import EndpointNotVerifiedError
from mflbot.mfl.endpoints import (
    Capability,
    EndpointRegistry,
    Provenance,
    WriteEndpoint,
)
from mflbot.mfl.verify import _auto_field_map, parse_api_info
from mflbot.recommend.models import LineupPayload

SYNTHETIC_DOCS = """
<html><body>
<h2>Export requests</h2>
<a href="/2026/export?TYPE=league&L=53367">league</a>
<a href="/2026/export?TYPE=rules&L=53367">rules</a>
<a href="/2026/export?TYPE=rosters&L=53367">rosters</a>
<h2>Import requests</h2>
<p>The import?TYPE=lineup request takes L, W, FRANCHISE and STARTERS.</p>
<a href="/2026/import?TYPE=tradeProposal">tradeProposal</a>
</body></html>
"""


def test_documented_types_are_extracted_from_both_links_and_prose() -> None:
    exports, imports = parse_api_info(SYNTHETIC_DOCS)
    assert {"league", "rules", "rosters"} <= set(exports)
    assert {"lineup", "tradeProposal"} <= set(imports)


def test_single_letter_parameters_are_detected() -> None:
    _, imports = parse_api_info(SYNTHETIC_DOCS)
    assert {"L", "W"} <= set(imports["lineup"].params)


def test_obvious_parameters_map_automatically_and_the_rest_are_reported() -> None:
    _, imports = parse_api_info(SYNTHETIC_DOCS)
    fields = LineupPayload.transmitted_fields()
    mapping = _auto_field_map(fields, imports["lineup"].params)
    assert mapping == {"league_id": "L", "week": "W", "franchise_id": "FRANCHISE"}
    assert [f for f in fields if f not in mapping] == ["starter_ids"]


def test_every_write_ships_unverified() -> None:
    """The shipped default must be inert for all write capabilities."""
    registry = EndpointRegistry.load("does-not-exist.json")
    assert set(registry.unverified_writes()) == set(Capability)


def test_an_unverified_capability_refuses_with_an_actionable_message() -> None:
    registry = EndpointRegistry.load("does-not-exist.json")
    with pytest.raises(EndpointNotVerifiedError, match="verify-endpoints"):
        registry.write(Capability.SUBMIT_LINEUP).require_verified()


def test_a_verified_capability_reports_its_type_name() -> None:
    endpoint = WriteEndpoint(
        capability=Capability.SUBMIT_LINEUP,
        candidates=("lineup",),
        description="synthetic",
        type_name="lineup",
        provenance=Provenance.DOC_VERIFIED,
    )
    assert endpoint.is_verified
    assert endpoint.require_verified() == "lineup"


def test_a_name_without_documentation_confirmation_stays_blocked() -> None:
    """Having a candidate name is not the same as having confirmed it."""
    endpoint = WriteEndpoint(
        capability=Capability.PROPOSE_TRADE,
        candidates=("tradeProposal",),
        description="synthetic",
        type_name="tradeProposal",
        provenance=Provenance.UNVERIFIED,
    )
    assert not endpoint.is_verified
    with pytest.raises(EndpointNotVerifiedError):
        endpoint.require_verified()


def test_lock_file_round_trips_a_verified_write(tmp_path) -> None:
    registry = EndpointRegistry.load(tmp_path / "endpoints.lock.json")
    registry.save_lock(
        {
            "reads": {},
            "writes": {
                "submit_lineup": {
                    "type_name": "lineup",
                    "params": ["L", "W", "FRANCHISE", "STARTERS"],
                    "field_map": {
                        "league_id": "L", "week": "W",
                        "franchise_id": "FRANCHISE", "starter_ids": "STARTERS",
                    },
                }
            },
        },
        tmp_path / "endpoints.lock.json",
    )
    reloaded = EndpointRegistry.load(tmp_path / "endpoints.lock.json")
    endpoint = reloaded.write(Capability.SUBMIT_LINEUP)
    assert endpoint.is_verified
    assert endpoint.missing_field_mappings(LineupPayload.transmitted_fields()) == ()
    # Other capabilities remain blocked -- verifying one does not unlock the rest.
    assert Capability.PROPOSE_TRADE in reloaded.unverified_writes()


def test_read_endpoints_declare_their_provenance() -> None:
    registry = EndpointRegistry.load("does-not-exist.json")
    for name, endpoint in registry.reads.items():
        assert endpoint.provenance in set(Provenance), name
        assert endpoint.ttl_seconds > 0, name
