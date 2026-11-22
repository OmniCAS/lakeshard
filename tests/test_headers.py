from lakeshard.headers import parse_imports


def test_plain_imports():
    assert parse_imports("import A\nimport B.C D\n\ndef x := 1\nimport E\n") == ["A", "B.C", "D"]


def test_comments_and_module_keywords():
    text = """/- a header comment
   import NotThis -/
-- line comment
module
public import A
import all B
public meta import C  -- trailing
prelude
theorem t : True := trivial
"""
    assert parse_imports(text) == ["A", "B", "C"]


def test_nested_block_comment():
    assert parse_imports("/- outer /- inner -/ still -/\nimport X\n") == ["X"]


def test_stops_at_first_command():
    assert parse_imports("import A\nopen B\nimport C\n") == ["A"]
