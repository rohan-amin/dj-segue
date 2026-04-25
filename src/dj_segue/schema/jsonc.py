"""JSONC (JSON with comments) parsing.

Strips `//` line comments and `/* */` block comments, then parses as JSON.
Newlines inside comments are preserved so JSON parse errors keep accurate
line numbers. Trailing commas are not supported in v0.1.
"""

from __future__ import annotations

import json
from typing import Any


def strip_comments(src: str) -> str:
    out: list[str] = []
    i = 0
    n = len(src)
    in_string = False
    in_line_comment = False
    in_block_comment = False
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if in_line_comment:
            if c == "\n":
                in_line_comment = False
                out.append(c)
            i += 1
            continue
        if in_block_comment:
            if c == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            if c == "\n":
                out.append(c)
            i += 1
            continue
        if in_string:
            out.append(c)
            if c == "\\" and nxt:
                out.append(nxt)
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        if c == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if c == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def loads(src: str) -> Any:
    return json.loads(strip_comments(src))
