"""Pure-Python chain integrity verifier.

Used both by:
- ``tests/property/test_amendment_chain.py`` (Hypothesis property test;
  takes random in-memory chains and asserts integrity invariants), and
- ``jobs/chain_reconciler.py`` (daily DB sweep; loads each report's
  version rows and runs the same checks).

The verifier is data-only — no DB types, no asyncpg dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal
from uuid import UUID

AnomalyKind = Literal[
    "gap_in_version_numbers",
    "cycle_detected",
    "unreachable_from_head",
    "amendment_off_unsigned_parent",
    "multiple_genesis_versions",
    "parent_missing",
]


@dataclass(frozen=True, slots=True)
class ChainNode:
    id: UUID
    version_number: int
    parent_id: UUID | None
    is_amendment: bool
    parent_signed: bool  # whether the parent_version was signed (for amendments)


@dataclass(frozen=True, slots=True)
class Anomaly:
    kind: AnomalyKind
    detail: dict[str, object]


def verify_chain(
    nodes: Iterable[ChainNode],
    *,
    current_version_id: UUID | None = None,
) -> list[Anomaly]:
    """Check append-only chain invariants. Returns the list of anomalies."""
    out: list[Anomaly] = []
    by_id: dict[UUID, ChainNode] = {}
    by_number: dict[int, ChainNode] = {}
    for n in nodes:
        by_id[n.id] = n
        if n.version_number in by_number and n.version_number == 1:
            out.append(
                Anomaly(
                    kind="multiple_genesis_versions",
                    detail={"version_number": n.version_number, "id": str(n.id)},
                )
            )
        by_number[n.version_number] = n

    if not by_number:
        return out

    # Contiguous version_number 1..N (gaps are not allowed because
    # append_version increments expected_version+1).
    numbers = sorted(by_number.keys())
    if numbers[0] != 1:
        out.append(Anomaly(kind="gap_in_version_numbers", detail={"first": numbers[0]}))
    for prev, cur in zip(numbers, numbers[1:]):
        if cur != prev + 1:
            out.append(
                Anomaly(
                    kind="gap_in_version_numbers",
                    detail={"after": prev, "next": cur},
                )
            )

    # Genesis must have parent_id = None.
    genesis = by_number.get(1)
    if genesis and genesis.parent_id is not None:
        out.append(
            Anomaly(
                kind="parent_missing",
                detail={"version_number": 1, "parent_id_present": str(genesis.parent_id)},
            )
        )

    # Parent links resolve + amendments only off signed parents.
    for n in by_id.values():
        if n.parent_id is None:
            continue
        parent = by_id.get(n.parent_id)
        if parent is None:
            out.append(
                Anomaly(
                    kind="parent_missing",
                    detail={"id": str(n.id), "parent_id": str(n.parent_id)},
                )
            )
            continue
        if n.is_amendment and not n.parent_signed:
            out.append(
                Anomaly(
                    kind="amendment_off_unsigned_parent",
                    detail={"id": str(n.id), "parent_id": str(n.parent_id)},
                )
            )

    # Cycle detection via DFS.
    WHITE, GRAY, BLACK = 0, 1, 2
    colour: dict[UUID, int] = {nid: WHITE for nid in by_id}
    for start in by_id:
        if colour[start] != WHITE:
            continue
        stack: list[UUID] = [start]
        path: set[UUID] = set()
        while stack:
            cur = stack[-1]
            if colour[cur] == WHITE:
                colour[cur] = GRAY
                path.add(cur)
                parent = by_id[cur].parent_id
                if parent is not None and parent in by_id:
                    if colour.get(parent, BLACK) == GRAY:
                        out.append(
                            Anomaly(
                                kind="cycle_detected",
                                detail={"id": str(cur), "parent_id": str(parent)},
                            )
                        )
                        colour[cur] = BLACK
                        path.discard(cur)
                        stack.pop()
                        continue
                    if colour[parent] == WHITE:
                        stack.append(parent)
                        continue
                colour[cur] = BLACK
                path.discard(cur)
                stack.pop()
            elif colour[cur] == GRAY:
                colour[cur] = BLACK
                path.discard(cur)
                stack.pop()
            else:
                stack.pop()

    # Reachability from head: every node should be reachable by walking
    # parent_id backwards from the current_version_id (or from the
    # highest version_number if current is None).
    head_id = current_version_id or by_number[max(by_number)].id
    reachable: set[UUID] = set()
    visited: set[UUID] = set()
    cur: UUID | None = head_id
    while cur is not None and cur not in visited:
        visited.add(cur)
        reachable.add(cur)
        node = by_id.get(cur)
        if node is None:
            break
        cur = node.parent_id
    for nid in by_id:
        if nid not in reachable:
            out.append(
                Anomaly(
                    kind="unreachable_from_head",
                    detail={"id": str(nid)},
                )
            )
    return out
