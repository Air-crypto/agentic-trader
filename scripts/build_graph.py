#!/usr/bin/env python3
"""Validate Markdown knowledge nodes and build a deterministic trading graph."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
NODE_TYPES = (
    "security",
    "issuer",
    "sector",
    "factor",
    "macro",
    "event_type",
    "source",
    "thesis",
    "model",
)
RELATIONS = (
    "supports",
    "contradicts",
    "affected_by",
    "benefits_from",
    "hurt_by",
    "co_moves_with",
    "invalidates",
)
CAUSAL_RELATIONS = {"affected_by", "benefits_from", "hurt_by", "invalidates"}
STATUSES = {"draft", "example", "active", "invalidated", "archived", "reference"}
SIGNS = {"positive", "negative", "neutral", "mixed"}
UNCERTAINTIES = {"low", "medium", "high", "unknown"}
CAUSALITIES = {"hypothesis", "non_causal"}
EDGE_FIELDS = {
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
TOP_LEVEL_FIELDS = {"id", "type", "status", "title", "aliases", "related"}
ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class Node:
    id: str
    type: str
    status: str
    title: str
    path: Path
    aliases: list[str] = field(default_factory=list)
    related: list[dict[str, Any]] = field(default_factory=list)


def parse_scalar(raw: str) -> Any:
    """Parse the deliberately small scalar subset used by node frontmatter."""
    value = raw.strip()
    if value == "[]":
        return []
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    lowered = value.lower()
    if lowered in {"null", "~"}:
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def parse_frontmatter(text: str, path: Path) -> tuple[dict[str, Any], str]:
    """Parse strict YAML-style frontmatter without adding a runtime dependency."""
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError(f"{path}: missing opening frontmatter delimiter")
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError(f"{path}: missing closing frontmatter delimiter") from exc

    frontmatter = lines[1:closing]
    data: dict[str, Any] = {}
    index = 0
    while index < len(frontmatter):
        line = frontmatter[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        if line[:1].isspace() or ":" not in line:
            raise ValueError(f"{path}: invalid frontmatter line {index + 2}: {line!r}")
        key, raw_value = (part.strip() for part in line.split(":", 1))
        if key in data:
            raise ValueError(f"{path}: duplicate frontmatter field {key!r}")
        if key not in TOP_LEVEL_FIELDS:
            raise ValueError(f"{path}: unsupported frontmatter field {key!r}")
        index += 1

        if raw_value:
            data[key] = parse_scalar(raw_value)
            continue

        if key == "aliases":
            aliases: list[str] = []
            while index < len(frontmatter) and frontmatter[index].startswith("  - "):
                alias = parse_scalar(frontmatter[index][4:])
                if not isinstance(alias, str) or not alias:
                    raise ValueError(f"{path}: aliases must be non-empty strings")
                aliases.append(alias)
                index += 1
            data[key] = aliases
            continue

        if key == "related":
            related: list[dict[str, Any]] = []
            current: dict[str, Any] | None = None
            while index < len(frontmatter):
                nested = frontmatter[index]
                if nested.startswith("  - "):
                    if current is not None:
                        related.append(current)
                    current = {}
                    inline = nested[4:]
                    if ":" not in inline:
                        raise ValueError(f"{path}: invalid related item {nested!r}")
                    nested_key, nested_value = (part.strip() for part in inline.split(":", 1))
                    current[nested_key] = parse_scalar(nested_value)
                    index += 1
                    continue
                if nested.startswith("    ") and current is not None:
                    inline = nested.strip()
                    if ":" not in inline:
                        raise ValueError(f"{path}: invalid related field {nested!r}")
                    nested_key, nested_value = (part.strip() for part in inline.split(":", 1))
                    if nested_key in current:
                        raise ValueError(f"{path}: duplicate related field {nested_key!r}")
                    current[nested_key] = parse_scalar(nested_value)
                    index += 1
                    continue
                break
            if current is not None:
                related.append(current)
            data[key] = related
            continue

        raise ValueError(f"{path}: field {key!r} cannot be an empty block")

    body = "\n".join(lines[closing + 1 :]).lstrip("\n")
    return data, body


def load_nodes(root: Path = ROOT) -> tuple[list[Node], list[str]]:
    knowledge = root / "knowledge"
    nodes: list[Node] = []
    errors: list[str] = []
    if not knowledge.is_dir():
        return [], [f"{knowledge}: knowledge directory does not exist"]

    for path in sorted(knowledge.rglob("*.md")):
        if path.name == "README.md":
            continue
        try:
            meta, body = parse_frontmatter(path.read_text(encoding="utf-8"), path)
            if not body.strip():
                errors.append(f"{path}: Markdown body is empty")
            nodes.append(
                Node(
                    id=str(meta.get("id", "")).strip(),
                    type=str(meta.get("type", "")).strip(),
                    status=str(meta.get("status", "")).strip(),
                    title=str(meta.get("title", "")).strip(),
                    path=path,
                    aliases=list(meta.get("aliases", [])),
                    related=list(meta.get("related", [])),
                )
            )
        except (TypeError, ValueError) as exc:
            errors.append(str(exc))
    return nodes, errors


def validate_as_of(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def validate_nodes(nodes: list[Node], errors: list[str]) -> None:
    by_id: dict[str, Node] = {}
    for node in nodes:
        if not node.id:
            errors.append(f"{node.path}: missing id")
        elif not ID_PATTERN.fullmatch(node.id):
            errors.append(f"{node.path}: invalid id {node.id!r}")
        elif node.id in by_id:
            errors.append(f"duplicate id {node.id!r}: {by_id[node.id].path} and {node.path}")
        else:
            by_id[node.id] = node
        if node.type not in NODE_TYPES:
            errors.append(f"{node.path}: invalid type {node.type!r}")
        if node.status not in STATUSES:
            errors.append(f"{node.path}: invalid status {node.status!r}")
        if not node.title:
            errors.append(f"{node.path}: missing title")
        if not isinstance(node.aliases, list) or not all(
            isinstance(alias, str) and alias for alias in node.aliases
        ):
            errors.append(f"{node.path}: aliases must be non-empty strings")

    seen_edges: set[tuple[str, str, str]] = set()
    for node in nodes:
        for position, edge in enumerate(node.related, start=1):
            label = f"{node.path}: related item {position}"
            unknown_fields = set(edge) - EDGE_FIELDS
            missing_fields = EDGE_FIELDS - set(edge)
            if unknown_fields:
                errors.append(f"{label}: unknown fields {sorted(unknown_fields)}")
            if missing_fields:
                errors.append(f"{label}: missing fields {sorted(missing_fields)}")
                continue

            relation = edge["relation"]
            target = edge["target"]
            provenance = edge["provenance"]
            if relation not in RELATIONS:
                errors.append(f"{label}: invalid relation {relation!r}")
            if target not in by_id:
                errors.append(f"{label}: unknown target id {target!r}")
            if provenance not in by_id:
                errors.append(f"{label}: unknown provenance id {provenance!r}")
            elif by_id[provenance].type != "source":
                errors.append(f"{label}: provenance must reference a source node")
            if edge["sign"] not in SIGNS:
                errors.append(f"{label}: invalid sign {edge['sign']!r}")
            if not isinstance(edge["horizon"], str) or not edge["horizon"].strip():
                errors.append(f"{label}: horizon must be a non-empty string")
            if (
                isinstance(edge["observations"], bool)
                or not isinstance(edge["observations"], int)
                or edge["observations"] < 0
            ):
                errors.append(f"{label}: observations must be a non-negative integer")
            if edge["uncertainty"] not in UNCERTAINTIES:
                errors.append(f"{label}: invalid uncertainty {edge['uncertainty']!r}")
            if not validate_as_of(edge["as_of"]):
                errors.append(f"{label}: as_of must be null or an ISO date")
            if edge["causality"] not in CAUSALITIES:
                errors.append(f"{label}: invalid causality {edge['causality']!r}")
            if relation in CAUSAL_RELATIONS and edge["causality"] != "hypothesis":
                errors.append(f"{label}: causal relation {relation!r} must be labelled hypothesis")
            if relation == "co_moves_with" and edge["causality"] != "non_causal":
                errors.append(f"{label}: co_moves_with must be labelled non_causal")

            key = (node.id, str(relation), str(target))
            if key in seen_edges:
                errors.append(f"{label}: duplicate edge {node.id} --{relation}--> {target}")
            seen_edges.add(key)


def build_payload(root: Path = ROOT) -> tuple[dict[str, Any], list[str]]:
    nodes, errors = load_nodes(root)
    validate_nodes(nodes, errors)
    knowledge = root / "knowledge"

    node_payload = [
        {
            "aliases": node.aliases,
            "id": node.id,
            "path": node.path.relative_to(root).as_posix(),
            "status": node.status,
            "title": node.title,
            "type": node.type,
        }
        for node in nodes
    ]
    edge_payload = []
    for node in nodes:
        for edge in node.related:
            if EDGE_FIELDS - set(edge):
                continue
            edge_payload.append(
                {
                    "as_of": edge["as_of"],
                    "causality": edge["causality"],
                    "horizon": edge["horizon"],
                    "id": f"{node.id}--{edge['relation']}--{edge['target']}",
                    "observations": edge["observations"],
                    "provenance": edge["provenance"],
                    "relation": edge["relation"],
                    "sign": edge["sign"],
                    "source": node.id,
                    "target": edge["target"],
                    "uncertainty": edge["uncertainty"],
                }
            )

    node_payload.sort(key=lambda item: item["id"])
    edge_payload.sort(key=lambda item: item["id"])
    payload: dict[str, Any] = {
        "edges": edge_payload,
        "generated_by": "scripts/build_graph.py",
        "meta": {
            "edge_count": len(edge_payload),
            "node_count": len(node_payload),
            "node_types": list(NODE_TYPES),
            "relation_types": list(RELATIONS),
            "source_of_truth": knowledge.relative_to(root).as_posix() + "/**/*.md",
        },
        "nodes": node_payload,
        "schema_version": 1,
    }
    return payload, errors


def render_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate and fail when the generated graph is missing or stale",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="Repository root (defaults to the parent of this script)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output path (defaults to <root>/knowledge/graph.json)",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output.resolve() if args.output else root / "knowledge" / "graph.json"
    payload, errors = build_payload(root)
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    if errors:
        return 1

    rendered = render_payload(payload)
    if args.check:
        if not output.is_file() or output.read_text(encoding="utf-8") != rendered:
            print(f"error: {output} is missing or stale", file=sys.stderr)
            return 1
        print(f"ok: {payload['meta']['node_count']} nodes, {payload['meta']['edge_count']} edges")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    print(
        f"wrote {output}: {payload['meta']['node_count']} nodes, "
        f"{payload['meta']['edge_count']} edges"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
