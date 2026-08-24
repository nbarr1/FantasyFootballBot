"""Player news ingestion behind a swappable source interface."""

from .base import Classification, NewsSource
from .mfl_injuries import MFLInjurySource
from .registry import SOURCE_REGISTRY, build_sources
from .sleeper import SleeperSource

__all__ = [
    "NewsSource",
    "Classification",
    "MFLInjurySource",
    "SleeperSource",
    "build_sources",
    "SOURCE_REGISTRY",
]
