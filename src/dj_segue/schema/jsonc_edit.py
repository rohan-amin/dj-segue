"""Edit one object inside a JSONC file in place, keeping every other byte.

Used by `dj-segue tune` to save a segment: only the keys that changed are
rewritten, so comments, formatting and the rest of the plan stay exactly as
the user wrote them.

`locate(src, path)` finds the object at a path like `("timeline", 3)`;
`edit_object(src, path, changes)` applies `{key: new_value}` changes, where a
value of `DELETE` removes the key. New values are written inline in the
plan's style: `{ "beats": 8 }`, `[1, 2]`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from dj_segue.schema import jsonc

DELETE = object()


@dataclass
class Member:
    key: str
    key_start: int  # the opening quote of the key
    value_start: int
    value_end: int  # exclusive


@dataclass
class Node:
    start: int
    end: int  # exclusive
    members: list[Member] = field(default_factory=list)  # objects
    items: list["Node"] = field(default_factory=list)  # arrays
    children: dict[str, "Node"] = field(default_factory=dict)  # objects


class _Scanner:
    def __init__(self, src: str) -> None:
        self.src = src
        self.i = 0

    def skip(self) -> None:
        """Skip whitespace and comments."""
        s, n = self.src, len(self.src)
        while self.i < n:
            c = s[self.i]
            if c in " \t\r\n":
                self.i += 1
            elif s.startswith("//", self.i):
                j = s.find("\n", self.i)
                self.i = n if j < 0 else j + 1
            elif s.startswith("/*", self.i):
                j = s.find("*/", self.i + 2)
                if j < 0:
                    raise ValueError("unterminated /* comment")
                self.i = j + 2
            else:
                return

    def expect(self, ch: str) -> None:
        self.skip()
        if not self.src.startswith(ch, self.i):
            raise ValueError(f"expected {ch!r} at offset {self.i}")
        self.i += 1

    def string(self) -> str:
        start = self.i
        self.i += 1
        while True:
            c = self.src[self.i]
            if c == "\\":
                self.i += 2
            elif c == '"':
                self.i += 1
                return json.loads(self.src[start : self.i])
            else:
                self.i += 1

    def value(self) -> Node:
        self.skip()
        start = self.i
        c = self.src[self.i]
        if c == "{":
            node = Node(start, start)
            self.i += 1
            self.skip()
            if self.src[self.i] == "}":
                self.i += 1
            else:
                while True:
                    self.skip()
                    key_start = self.i
                    key = self.string()
                    self.expect(":")
                    child = self.value()
                    node.members.append(Member(key, key_start, child.start, child.end))
                    node.children[key] = child
                    self.skip()
                    if self.src[self.i] == ",":
                        self.i += 1
                        continue
                    self.expect("}")
                    break
            node.end = self.i
            return node
        if c == "[":
            node = Node(start, start)
            self.i += 1
            self.skip()
            if self.src[self.i] == "]":
                self.i += 1
            else:
                while True:
                    node.items.append(self.value())
                    self.skip()
                    if self.src[self.i] == ",":
                        self.i += 1
                        continue
                    self.expect("]")
                    break
            node.end = self.i
            return node
        if c == '"':
            self.string()
            return Node(start, self.i)
        while self.i < len(self.src) and self.src[self.i] not in ",}] \t\r\n/":
            self.i += 1
        return Node(start, self.i)


def locate(src: str, path: tuple) -> Node:
    """The node at `path` (object keys and array indices) in a JSONC text."""
    node = _Scanner(src).value()
    for step in path:
        node = node.items[step] if isinstance(step, int) else node.children[step]
    return node


def dumps_inline(value: Any) -> str:
    """JSON on one line, in the plan files' style: `{ "beats": 8 }`, `[0, 1]`."""
    if isinstance(value, dict):
        if not value:
            return "{}"
        inner = ", ".join(f"{json.dumps(k)}: {dumps_inline(v)}" for k, v in value.items())
        return "{ " + inner + " }"
    if isinstance(value, list):
        return "[" + ", ".join(dumps_inline(v) for v in value) + "]"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return json.dumps(value)


def edit_object(src: str, path: tuple, changes: dict[str, Any]) -> str:
    """Apply `changes` to the object at `path`; return the new text.

    Existing keys keep their place; new keys go after the last one, on a new
    line indented like it. The result is re-parsed to check it's valid.
    """
    obj = locate(src, path)
    by_key = {m.key: m for m in obj.members}
    edits: list[tuple[int, int, str]] = []  # (start, end, replacement)

    for key, value in changes.items():
        m = by_key.get(key)
        if m is None:
            continue
        if value is DELETE:
            edits.append(_delete_span(src, obj, m))
        else:
            edits.append((m.value_start, m.value_end, dumps_inline(value)))

    new_keys = [(k, v) for k, v in changes.items() if k not in by_key and v is not DELETE]
    if new_keys:
        if obj.members:
            last = obj.members[-1]
            line_start = src.rfind("\n", 0, last.key_start) + 1
            first_on_line = line_start + len(src[line_start:]) - len(src[line_start:].lstrip(" \t"))
            indent = src[line_start:first_on_line]
            text = "".join(f",\n{indent}{json.dumps(k)}: {dumps_inline(v)}" for k, v in new_keys)
            edits.append((last.value_end, last.value_end, text))
        else:
            text = ", ".join(f"{json.dumps(k)}: {dumps_inline(v)}" for k, v in new_keys)
            edits.append((obj.start + 1, obj.end - 1, " " + text + " "))

    out = src
    for start, end, text in sorted(edits, key=lambda e: e[0], reverse=True):
        out = out[:start] + text + out[end:]
    jsonc.loads(out)  # raises if we broke it
    return out


def _delete_span(src: str, obj: Node, m: Member) -> tuple[int, int, str]:
    idx = obj.members.index(m)
    if idx + 1 < len(obj.members):
        # Up to the next key: takes the comma and the gap after it.
        return (m.key_start, obj.members[idx + 1].key_start, "")
    if idx > 0:
        # Last key: from the end of the previous value (takes its comma).
        return (obj.members[idx - 1].value_end, m.value_end, "")
    return (m.key_start, m.value_end, "")
