from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LineageNode:
    id: str
    kind: str
    metadata: dict[str, str]


@dataclass(frozen=True, slots=True)
class LineageEdge:
    source: str
    relation: str
    target: str


class LineageGraph:
    def __init__(self) -> None:
        self.nodes: dict[str, LineageNode] = {}
        self.edges: list[LineageEdge] = []

    def add_node(self, node: LineageNode) -> None:
        self.nodes[node.id] = node

    def link(self, edge: LineageEdge) -> None:
        if edge.source not in self.nodes or edge.target not in self.nodes:
            raise ValueError("Both lineage nodes must exist before linking.")
        self.edges.append(edge)

    def upstream(self, node_id: str) -> set[str]:
        frontier = [node_id]
        seen = {node_id}

        while frontier:
            current = frontier.pop()
            for edge in self.edges:
                if edge.target == current and edge.source not in seen:
                    seen.add(edge.source)
                    frontier.append(edge.source)

        seen.remove(node_id)
        return seen
