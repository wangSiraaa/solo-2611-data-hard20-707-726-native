"""Pure functions for the classification catalog.

Edges always point from a child class to a parent class (``child -> parent``).
A class may have several parents.  All identity is by node id; names are
display-only and may repeat.
"""

from __future__ import annotations

from collections import deque
from typing import Iterable, Mapping, Sequence

Edge = tuple[str, str]


def build_parents(edges: Iterable[Edge]) -> dict[str, set[str]]:
    parents: dict[str, set[str]] = {}
    for child, parent in edges:
        parents.setdefault(child, set()).add(parent)
    return parents


def build_children(edges: Iterable[Edge]) -> dict[str, set[str]]:
    children: dict[str, set[str]] = {}
    for child, parent in edges:
        children.setdefault(parent, set()).add(child)
    return children


def find_cycle(nodes: Iterable[str], edges: Iterable[Edge]) -> list[str] | None:
    """Return one concrete cycle as a closed node walk, or ``None``.

    The graph is checked as a whole.  It does not matter in which order a
    batch removed and added edges; only the final edge set is inspected.
    """

    node_set = set(nodes)
    adjacency = build_parents(edges)
    for child, parent in edges:
        node_set.add(child)
        node_set.add(parent)

    color = {node: 0 for node in node_set}  # 0 unvisited, 1 on stack, 2 done
    for start in sorted(node_set):
        if color[start] != 0:
            continue
        color[start] = 1
        path: list[str] = [start]
        position = {start: 0}
        stack: list[tuple[str, Iterable[str]]] = [
            (start, iter(sorted(adjacency.get(start, ()))))
        ]
        while stack:
            node, neighbours = stack[-1]
            advanced = False
            for nxt in neighbours:
                if color.get(nxt, 0) == 0:
                    color[nxt] = 1
                    position[nxt] = len(path)
                    path.append(nxt)
                    stack.append((nxt, iter(sorted(adjacency.get(nxt, ())))))
                    advanced = True
                    break
                if color.get(nxt, 0) == 1:  # back edge to the active path
                    return path[position[nxt]:] + [nxt]
            if not advanced:
                color[node] = 2
                stack.pop()
                path.pop()
                position.pop(node, None)
    return None


def is_acyclic(nodes: Iterable[str], edges: Iterable[Edge]) -> bool:
    return find_cycle(nodes, edges) is None


def ancestors(
    start: str, edges: Iterable[Edge], *, include_self: bool = False
) -> set[str]:
    """Nodes reachable upward from ``start`` (parents, grandparents, ...)."""

    parents = build_parents(edges)
    seen = {start}
    queue: deque[str] = deque([start])
    while queue:
        node = queue.popleft()
        for parent in sorted(parents.get(node, ())):
            if parent not in seen:
                seen.add(parent)
                queue.append(parent)
    if not include_self:
        seen.discard(start)
    return seen


def descendants(
    target: str, edges: Iterable[Edge], *, include_self: bool = True
) -> set[str]:
    """Nodes that can reach ``target`` by following child -> parent edges."""

    children = build_children(edges)
    seen = {target}
    queue: deque[str] = deque([target])
    while queue:
        node = queue.popleft()
        for child in sorted(children.get(node, ())):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    if not include_self:
        seen.discard(target)
    return seen


def distances_to_target(
    target: str, edges: Iterable[Edge]
) -> tuple[dict[str, int], dict[str, str]]:
    """Reverse BFS over parents, giving a shortest path to ``target``.

    ``dist[node]`` is the number of edges from ``node`` up to ``target`` and
    ``next_hop[node]`` is the first parent on a shortest route.  Nodes that
    cannot reach ``target`` are absent.
    """

    children = build_children(edges)
    dist: dict[str, int] = {target: 0}
    next_hop: dict[str, str] = {}
    queue: deque[str] = deque([target])
    while queue:
        node = queue.popleft()
        for child in sorted(children.get(node, ())):
            if child not in dist:
                dist[child] = dist[node] + 1
                next_hop[child] = node
                queue.append(child)
    return dist, next_hop


def path_from(
    start: str, target: str, dist: Mapping[str, int], next_hop: Mapping[str, str]
) -> list[str] | None:
    if start not in dist:
        return None
    path = [start]
    while path[-1] != target:
        path.append(next_hop[path[-1]])
    return path


def choose_path(
    classes: Sequence[str],
    target: str,
    dist: Mapping[str, int],
    next_hop: Mapping[str, str],
) -> list[str] | None:
    """Pick one deterministic shortest path from any direct class.

    Multiple direct classes, and multiple routes from one class, never cause
    extra counting; this helper only exposes one witness path.
    """

    best: tuple[int, tuple[str, ...]] | None = None
    for cls in sorted(set(classes)):
        if cls not in dist:
            continue
        path = path_from(cls, target, dist, next_hop)
        assert path is not None
        candidate = (dist[cls], tuple(path))
        if best is None or candidate < best:
            best = candidate
    return list(best[1]) if best is not None else None


def explain_coverage(
    target: str, direct_classes: Sequence[str], edges: Iterable[Edge]
) -> dict:
    """Explain whether each direct class is covered by ``target``.

    The target node is always inside its own coverage: a ticket directly
    classified in the target has the one-element path ``[target]``.
    """

    edge_set = frozenset(edges)
    dist, next_hop = distances_to_target(target, edge_set)
    entries = []
    for cls in sorted(set(direct_classes)):
        if cls in dist:
            entries.append(
                {
                    "class_id": cls,
                    "covered": True,
                    "distance": dist[cls],
                    "path": path_from(cls, target, dist, next_hop),
                }
            )
        else:
            entries.append(
                {
                    "class_id": cls,
                    "covered": False,
                    "distance": None,
                    "path": None,
                }
            )
    covered = [e["class_id"] for e in entries if e["covered"]]
    uncovered = [e["class_id"] for e in entries if not e["covered"]]
    return {
        "target_id": target,
        "direct_classes": sorted(set(direct_classes)),
        "covered_classes": covered,
        "uncovered_classes": uncovered,
        "entries": entries,
        "explanation": (
            "A direct class is covered when it equals the target or can reach "
            "it through child->parent edges. Multiple parents/paths are not "
            "multiplied. The target itself has path [target]."
        ),
    }
