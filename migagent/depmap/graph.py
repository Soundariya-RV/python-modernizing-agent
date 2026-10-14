"""Import graph → migration units (cycles grouped) → dependency-first order.

Deterministic on purpose: the migration order is a fact about the code, not a
model decision. Edges point from importer to imported ("A depends on B"), and
B is migrated before A.
"""
from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Set


def strongly_connected(nodes: Iterable[str], edges: Dict[str, Set[str]]) -> List[List[str]]:
    """Tarjan's algorithm, iterative (deep import chains must not hit recursion limits)."""
    index: Dict[str, int] = {}
    low: Dict[str, int] = {}
    on_stack: Set[str] = set()
    stack: List[str] = []
    out: List[List[str]] = []
    counter = 0
    for root in sorted(nodes):
        if root in index:
            continue
        work = [(root, iter(sorted(edges.get(root, ()))))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(sorted(edges.get(w, ())))))
                    advanced = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                out.append(sorted(comp))
    return out


def migration_units(nodes: Iterable[str], edges: Dict[str, Set[str]],
                    tie_break: Callable[[List[str]], tuple]) -> List[List[str]]:
    """Units in migration order: every unit comes after all units it imports.

    Kahn's algorithm on the condensation; among units that are ready at the
    same time, `tie_break` decides (e.g. libraries before tests, then by path)
    so the order is stable across runs.
    """
    nodes = list(nodes)
    comps = strongly_connected(nodes, edges)
    comp_of = {n: i for i, c in enumerate(comps) for n in c}
    deps: Dict[int, Set[int]] = {i: set() for i in range(len(comps))}
    for a in nodes:
        for b in edges.get(a, ()):
            if b in comp_of and comp_of[a] != comp_of[b]:
                deps[comp_of[a]].add(comp_of[b])
    remaining = {i: set(d) for i, d in deps.items()}
    done: Set[int] = set()
    order: List[List[str]] = []
    while remaining:
        ready = [i for i, d in remaining.items() if d <= done]
        if not ready:  # cannot happen on a condensation; guard anyway
            raise RuntimeError('cycle left after SCC condensation')
        pick = min(ready, key=lambda i: tie_break(comps[i]))
        order.append(comps[pick])
        done.add(pick)
        del remaining[pick]
    return order
