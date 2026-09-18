"""Load untrusted YAML as plain data, keeping the source line of every key.

`yaml.safe_load` is safe from code execution but not from alias expansion
("billion laughs"), and it throws away line numbers. This composes the node
graph (which shares aliased nodes instead of expanding them), refuses any node
reached twice (an alias) and any explicit non-standard tag, and builds plain
dicts/lists whose line numbers live in a side table.
"""
from __future__ import annotations

import yaml

_STANDARD_TAGS = {
    "tag:yaml.org,2002:map",
    "tag:yaml.org,2002:seq",
    "tag:yaml.org,2002:str",
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:float",
    "tag:yaml.org,2002:bool",
    "tag:yaml.org,2002:null",
}


class UnsafeYAML(ValueError):
    def __init__(self, message: str, line: int | None = None):
        super().__init__(message)
        self.line = line


class LineMap(dict):
    """A dict that also records the 1-based line of each key."""

    def __init__(self):
        super().__init__()
        self.lines: dict = {}

    def line_of(self, key, default=None):
        return self.lines.get(key, default)


class LineList(list):
    def __init__(self):
        super().__init__()
        self.lines: list = []

    def line_of(self, index, default=None):
        return self.lines[index] if 0 <= index < len(self.lines) else default


def load(text: str):
    """Return the document as LineMap/LineList/scalars. Raises UnsafeYAML."""
    try:
        nodes = list(yaml.compose_all(text, Loader=yaml.SafeLoader))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        raise UnsafeYAML(f"not valid YAML: {exc}", mark.line + 1 if mark else None) from None
    if len(nodes) != 1 or nodes[0] is None:
        raise UnsafeYAML("expected exactly one YAML document")
    constructor = yaml.SafeLoader("")
    seen: set[int] = set()

    def build(node):
        line = node.start_mark.line + 1
        if id(node) in seen:
            raise UnsafeYAML("YAML anchors and aliases are not allowed", line)
        seen.add(id(node))
        if node.tag not in _STANDARD_TAGS:
            raise UnsafeYAML(f"YAML tag {node.tag!r} is not allowed", line)
        if isinstance(node, yaml.MappingNode):
            out = LineMap()
            for key_node, value_node in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    raise UnsafeYAML("YAML merge keys (<<) are not allowed", key_node.start_mark.line + 1)
                key = build(key_node)
                if isinstance(key, (dict, list)):
                    raise UnsafeYAML("complex mapping keys are not allowed", key_node.start_mark.line + 1)
                if key in out:
                    raise UnsafeYAML(f"duplicate key {key!r}", key_node.start_mark.line + 1)
                out[key] = build(value_node)
                out.lines[key] = key_node.start_mark.line + 1
            return out
        if isinstance(node, yaml.SequenceNode):
            out = LineList()
            for item in node.value:
                out.append(build(item))
                out.lines.append(item.start_mark.line + 1)
            return out
        try:
            return constructor.construct_object(node, deep=True)
        except yaml.YAMLError as exc:
            raise UnsafeYAML(f"unreadable value: {exc}", line) from None

    return build(nodes[0])
