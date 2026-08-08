"""Role registry: role name -> serving backend.

v0 serves `embed.dense` only. The registry is data, not code paths, so
generator roles (Gemma / Kimi candidates, see docs/models/PINS.md) plug in
as new entries without touching callers — callers only ever know role names
(rule E12).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RoleKind(StrEnum):
    embedding = "embedding"
    generation = "generation"
    rerank = "rerank"


@dataclass(frozen=True)
class RoleDescriptor:
    name: str
    kind: RoleKind
    model_id: str
    dim: int | None = None


class RoleRegistry:
    def __init__(self) -> None:
        self._roles: dict[str, RoleDescriptor] = {}

    def register(self, descriptor: RoleDescriptor) -> None:
        self._roles[descriptor.name] = descriptor

    def get(self, name: str) -> RoleDescriptor | None:
        return self._roles.get(name)

    def list(self) -> list[RoleDescriptor]:
        return sorted(self._roles.values(), key=lambda d: d.name)
