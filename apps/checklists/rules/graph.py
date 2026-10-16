"""The dependency graph of a rule set: cycle detection and a deterministic evaluation order.

Nodes are questions and sections.  An edge ``A -> B`` (labelled with a rule code) means *B's effective state depends on
A's answer*: a rule reads A in its condition and one of its actions changes whether B is shown, which options B offers or
which limits B enforces (the "graph edge" actions).  Every section also points to each of its questions (hiding a section
hides them).  Such a graph must be acyclic: with a cycle, "is A visible?" would depend on A itself.  Cycles are rejected
when a rule is saved, imported and published; the evaluator also refuses to run on one.

Annotation-only actions (requiredness, messages, comment/evidence requirements, instructions) add no edges, so
"Q1 is required if Q2 is answered" together with "Q2 is required if Q1 is answered" is fine.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from .actions import ACTIONS
from .compiled import CompiledRule
from .snapshot import VersionSnapshot

Node = tuple[str, str]  # ("item" | "section", code)


@dataclass
class DependencyGraph:
    nodes: list[Node]
    #: node -> list of (successor, rule code or "" for containment)
    edges: dict[Node, list[tuple[Node, str]]] = field(default_factory=dict)

    def rank(self) -> dict[Node, int]:
        return {node: i for i, node in enumerate(self.nodes)}


def build_graph(snapshot: VersionSnapshot, rules: list[CompiledRule]) -> DependencyGraph:
    nodes: list[Node] = []
    edges: dict[Node, list[tuple[Node, str]]] = {}
    for section in sorted(snapshot.sections.values(), key=lambda s: (s.sort_order, s.code)):
        nodes.append(("section", section.code))
        for item in sorted(snapshot.items_in_section(section.code), key=lambda i: (i.sort_order, i.code)):
            nodes.append(("item", item.code))
    for node in nodes:
        edges[node] = []
    for item in snapshot.items.values():
        edges[("section", item.section_code)].append((("item", item.code), ""))
    for rule in rules:
        if not rule.is_active:
            continue
        sources = sorted(rule.sources())
        for action in rule.actions:
            if not ACTIONS[action.type].edge:
                continue
            target = (action.target_kind, action.target_code)
            for source in sources:
                edges[("item", source)].append((target, rule.code))
    rank = {node: i for i, node in enumerate(nodes)}
    for node in edges:
        edges[node].sort(key=lambda e: (rank[e[0]], e[1]))
    return DependencyGraph(nodes, edges)


def find_cycle(graph: DependencyGraph) -> list[tuple[Node, str]] | None:
    """A cycle as ``[(node, rule_to_next), ...]`` (the last rule leads back to the first node), or ``None``."""
    WHITE, GREY, BLACK = 0, 1, 2
    color = {node: WHITE for node in graph.nodes}
    for root in graph.nodes:
        if color[root] != WHITE:
            continue
        stack: list[tuple[Node, int]] = [(root, 0)]
        path: list[tuple[Node, str]] = []  # (node, rule used to reach the next node)
        color[root] = GREY
        while stack:
            node, index = stack[-1]
            successors = graph.edges[node]
            if index < len(successors):
                stack[-1] = (node, index + 1)
                succ, rule = successors[index]
                if color[succ] == GREY:
                    trail = [n for n, _ in path] + [node]
                    start = trail.index(succ)
                    rules = [r for _, r in path] + [rule]
                    return [(trail[i], rules[i]) for i in range(start, len(trail))]
                if color[succ] == WHITE:
                    color[succ] = GREY
                    path.append((node, rule))
                    stack.append((succ, 0))
            else:
                color[node] = BLACK
                stack.pop()
                if path:
                    path.pop()
    return None


def topological_order(graph: DependencyGraph) -> list[Node]:
    """A deterministic order in which every node comes after everything it depends on (graph must be acyclic)."""
    rank = graph.rank()
    indegree = {node: 0 for node in graph.nodes}
    for node in graph.nodes:
        for succ, _ in graph.edges[node]:
            indegree[succ] += 1
    ready = [(rank[n], n) for n in graph.nodes if indegree[n] == 0]
    heapq.heapify(ready)
    order: list[Node] = []
    while ready:
        _, node = heapq.heappop(ready)
        order.append(node)
        for succ, _ in graph.edges[node]:
            indegree[succ] -= 1
            if indegree[succ] == 0:
                heapq.heappush(ready, (rank[succ], succ))
    return order


def describe_cycle(snapshot: VersionSnapshot, cycle: list[tuple[Node, str]]) -> str:
    def label(node: Node) -> str:
        kind, code = node
        if kind == "item":
            return f"{code} “{snapshot.items[code].question[:40]}”"
        return f"section {code} “{snapshot.sections[code].name[:40]}”"

    parts = [f"{label(node)} --({rule or 'inside section'})--> " for node, rule in cycle]
    return "These rules depend on each other in a circle: " + "".join(parts) + label(cycle[0][0]) + ". Remove or change one of the rules."
