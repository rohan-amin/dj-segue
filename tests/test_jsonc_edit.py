"""In-place JSONC edits keep every byte outside the changed keys."""

from __future__ import annotations

import pytest

from dj_segue.schema import jsonc
from dj_segue.schema.jsonc_edit import DELETE, dumps_inline, edit_object, locate

SRC = """// a plan
{
  "timeline": [
    { "type": "play", "deck": 1 },  // first

    // the blend
    { "type": "transition", "style": "crossfade",
      "start_at": { "after": "x", "offset": { "beats": -32 } },
      "duration": { "beats": 32 } }   /* trailing */
  ]
}
"""


def test_locate_finds_the_segment() -> None:
    node = locate(SRC, ("timeline", 1))
    assert SRC[node.start : node.end].startswith('{ "type": "transition"')
    assert [m.key for m in node.members] == ["type", "style", "start_at", "duration"]


def test_replace_keeps_everything_else() -> None:
    out = edit_object(SRC, ("timeline", 1), {"duration": {"beats": 64}})
    assert out == SRC.replace('"duration": { "beats": 32 }', '"duration": { "beats": 64 }')


def test_insert_goes_after_the_last_key_with_its_indent() -> None:
    out = edit_object(SRC, ("timeline", 1), {"curve": "linear"})
    assert '"duration": { "beats": 32 },\n      "curve": "linear" }   /* trailing */' in out
    assert jsonc.loads(out)["timeline"][1]["curve"] == "linear"
    assert out.startswith("// a plan") and "// first" in out and "// the blend" in out


def test_delete_middle_and_last_keys() -> None:
    out = edit_object(SRC, ("timeline", 1), {"style": DELETE})
    assert jsonc.loads(out)["timeline"][1] == {
        "type": "transition",
        "start_at": {"after": "x", "offset": {"beats": -32}},
        "duration": {"beats": 32},
    }
    out = edit_object(SRC, ("timeline", 1), {"duration": DELETE})
    assert '"offset": { "beats": -32 } } }   /* trailing */' in out
    assert "duration" not in jsonc.loads(out)["timeline"][1]


def test_delete_last_and_insert_together() -> None:
    out = edit_object(SRC, ("timeline", 1), {"duration": DELETE, "in": {"curve": "linear"}})
    seg = jsonc.loads(out)["timeline"][1]
    assert "duration" not in seg and seg["in"] == {"curve": "linear"}


def test_strings_with_braces_and_comment_markers() -> None:
    src = '{ "a": "x}//y", "b": 1 }'
    assert edit_object(src, (), {"b": 2}) == '{ "a": "x}//y", "b": 2 }'


def test_dumps_inline_style() -> None:
    assert dumps_inline({"beats": 8.0}) == '{ "beats": 8 }'
    assert dumps_inline([[0, 0], [0.5, 0.25]]) == "[[0, 0], [0.5, 0.25]]"


def test_bad_edit_is_rejected() -> None:
    with pytest.raises(KeyError):
        edit_object(SRC, ("timeline", 1, "nope"), {"a": 1})
