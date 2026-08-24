"""Player news ingestion behind a swappable source interface."""

from .base import NewsSource, Classification
from .mfl_injuries import MFLInjurySource
from .sleeper import SleeperSource
from .registry import build_sources, SOURCE_REGISTRY

__all__ = [
    "NewsSource",
    "Classification",
    "MFLInjurySource",
    "SleeperSource",
    "build_sources",
    "SOURCE_REGISTRY",
]
