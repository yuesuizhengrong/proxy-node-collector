from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Source:
    name: str
    url: str
    format: str = "auto"
    enabled: bool = True


@dataclass(slots=True)
class SourceResult:
    source: Source
    ok: bool
    parsed: int = 0
    error: str | None = None


@dataclass(slots=True)
class Node:
    identity: str
    protocol: str
    label: str
    proxy: dict[str, Any]
    sources: set[str] = field(default_factory=set)
    uri: str | None = None


@dataclass(slots=True)
class TestedNode:
    node: Node
    latency_ms: int
    checked_at: str

