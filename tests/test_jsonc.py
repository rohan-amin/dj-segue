"""JSONC loader tests."""

from __future__ import annotations

import json

import pytest

from dj_segue.schema import jsonc


def test_loads_plain_json() -> None:
    assert jsonc.loads('{"a": 1, "b": [2, 3]}') == {"a": 1, "b": [2, 3]}


def test_strips_line_comments() -> None:
    src = """
    // top-of-file comment
    {
        "a": 1, // inline tail comment
        "b": 2
    }
    """
    assert jsonc.loads(src) == {"a": 1, "b": 2}


def test_strips_block_comments() -> None:
    src = """
    /* a block comment
       spanning multiple lines */
    { "a": /* inline */ 1, "b": 2 }
    """
    assert jsonc.loads(src) == {"a": 1, "b": 2}


def test_does_not_strip_inside_strings() -> None:
    # `//` and `/*` inside a string literal must be preserved verbatim.
    src = '{"u": "https://example.com/path", "c": "/* not a comment */"}'
    parsed = jsonc.loads(src)
    assert parsed["u"] == "https://example.com/path"
    assert parsed["c"] == "/* not a comment */"


def test_preserves_escaped_quotes_in_strings() -> None:
    src = r'{"q": "she said \"hi\""}'
    assert jsonc.loads(src) == {"q": 'she said "hi"'}


def test_preserves_newlines_for_error_line_numbers() -> None:
    src = "{\n  // line 2 comment\n  \"bad\": ,\n}"
    with pytest.raises(json.JSONDecodeError) as exc:
        jsonc.loads(src)
    # Error should be reported on line 3, not line 1, because newlines were
    # preserved when the comment got stripped.
    assert exc.value.lineno == 3


def test_handles_empty_input() -> None:
    with pytest.raises(json.JSONDecodeError):
        jsonc.loads("")
    with pytest.raises(json.JSONDecodeError):
        jsonc.loads("// nothing but a comment")
