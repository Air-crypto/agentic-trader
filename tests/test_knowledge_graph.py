from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GRAPH_PATH = ROOT / "knowledge" / "graph.json"
NODE_TYPES = {
    "security",
    "issuer",
    "sector",
    "factor",
    "macro",
    "event_type",
    "source",
    "thesis",
    "model",
}
RELATIONS = {
    "supports",
    "contradicts",
    "affected_by",
    "benefits_from",
    "hurt_by",
    "co_moves_with",
    "invalidates",
}
CAUSAL_RELATIONS = {"affected_by", "benefits_from", "hurt_by", "invalidates"}
EDGE_FIELDS = {
    "id",
    "source",
    "relation",
    "target",
    "sign",
    "horizon",
    "observations",
    "uncertainty",
    "as_of",
    "provenance",
    "causality",
}


def load_graph() -> dict:
    return json.loads(GRAPH_PATH.read_text(encoding="utf-8"))


def test_generated_graph_is_current() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/build_graph.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_all_markdown_nodes_are_in_generated_graph() -> None:
    graph = load_graph()
    authored = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "knowledge").rglob("*.md")
        if path.name != "README.md"
    }
    generated = {node["path"] for node in graph["nodes"]}
    assert generated == authored
    assert len(generated) == len(graph["nodes"])
    assert {node["type"] for node in graph["nodes"]} == NODE_TYPES


def test_referential_integrity_and_edge_schema() -> None:
    graph = load_graph()
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert len(nodes) == len(graph["nodes"])
    edge_ids = set()
    for edge in graph["edges"]:
        assert set(edge) == EDGE_FIELDS
        assert edge["id"] not in edge_ids
        edge_ids.add(edge["id"])
        assert edge["source"] in nodes
        assert edge["target"] in nodes
        assert edge["provenance"] in nodes
        assert nodes[edge["provenance"]]["type"] == "source"
        assert edge["relation"] in RELATIONS
        assert edge["sign"] in {"positive", "negative", "neutral", "mixed"}
        assert isinstance(edge["horizon"], str) and edge["horizon"]
        assert isinstance(edge["observations"], int) and edge["observations"] >= 0
        assert edge["uncertainty"] in {"low", "medium", "high", "unknown"}
        assert edge["as_of"] is None or len(edge["as_of"]) == 10
        assert edge["causality"] in {"hypothesis", "non_causal"}
        if edge["relation"] in CAUSAL_RELATIONS:
            assert edge["causality"] == "hypothesis"
        if edge["relation"] == "co_moves_with":
            assert edge["causality"] == "non_causal"


def test_build_is_byte_for_byte_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    for output in (first, second):
        result = subprocess.run(
            [
                sys.executable,
                "scripts/build_graph.py",
                "--output",
                str(output),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    assert first.read_bytes() == second.read_bytes() == GRAPH_PATH.read_bytes()


def test_seed_data_is_explicitly_unobserved_and_undated() -> None:
    graph = load_graph()
    assert graph["edges"]
    assert all(edge["observations"] == 0 for edge in graph["edges"])
    assert all(edge["as_of"] is None for edge in graph["edges"])


def test_viewer_defaults_to_all_nodes() -> None:
    source = (ROOT / "viewer" / "app.js").read_text(encoding="utf-8")
    assert 'const DEFAULT_SCOPE = "all";' in source
    assert "hiddenTypes: new Set()" in source
    assert "hiddenRelations: new Set()" in source
