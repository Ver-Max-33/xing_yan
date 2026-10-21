"""Rule versions (spec §5.9): built-ins are read-only; an edit with different content is a new version."""
import pytest

from tests.test_l3_rule import EXAMPLE
from wb.engine.l3.rule import RuleError
from wb.workspace.labels import HitLabels
from wb.workspace.rules import Rules, builtin_rules


def test_builtins_are_listed_and_read_only(paths):
    rules = Rules(paths)
    ids = {r["id"] for r in rules.catalog() if r["builtin"]}
    assert {"long13", "long13_jp", "long13_oral", "long13_no_limit", "vol_burst", "limit_streak", "review",
            "box_breakout_after_rally"} <= ids
    rule, text, version = rules.get("long13")
    assert rule.markets == ["CN"] and "1.098" in text and version == 1
    with pytest.raises(RuleError, match="内置规则"):
        rules.save(builtin_rules()["long13"][1])


def test_saving_makes_versions_and_the_same_content_does_not(paths):
    rules = Rules(paths)
    with pytest.raises(RuleError, match="内置规则"):
        rules.save(EXAMPLE)  # the spec example ships as a built-in with this id
    mine = EXAMPLE.replace("id: box_breakout_after_rally", "id: my_box")
    assert rules.save(mine) == ("my_box", 1, True)
    assert rules.save(mine) == ("my_box", 1, False)             # same content: nothing new
    assert rules.save(mine.replace("value: 2.0", "value: 2.5")) == ("my_box", 2, True)
    assert rules.get("my_box")[0].param("vol_spike") == 2.5 and rules.get("my_box", 1)[0].param("vol_spike") == 2.0
    assert [v["version"] for v in rules.versions("my_box")] == [1, 2]
    assert next(r for r in rules.catalog() if r["id"] == "my_box")["version"] == 2
    with pytest.raises(RuleError, match="没有规则"):
        rules.get("nope")


def test_a_stored_rule_that_no_longer_loads_is_listed_with_the_reason(paths):
    rules = Rules(paths)
    rules.conn.execute("INSERT INTO rule_version VALUES ('old', 1, 'h', 'id: old\nname: 旧\nmarkets: [CN]\nstages: []', '2026-01-01')")
    old = next(r for r in rules.catalog() if r["id"] == "old")
    assert old["error"] and old["version"] == 1


def test_a_rule_can_be_enabled_or_archived_and_the_catalog_says_when_it_last_changed(paths):
    rules = Rules(paths)
    rules.save(EXAMPLE.replace("id: box_breakout_after_rally", "id: my_box"))
    row = next(r for r in rules.catalog() if r["id"] == "my_box")
    assert row["enabled"] is False and row["archived"] is False and row["updated_at"]
    rules.set_state("my_box", enabled=True)
    rules.set_state("long13", archived=True)                     # a built-in can be put away too
    cat = {r["id"]: r for r in rules.catalog()}
    assert cat["my_box"]["enabled"] is True and cat["my_box"]["archived"] is False
    assert cat["long13"]["archived"] is True and cat["long13"]["updated_at"] is None
    rules.set_state("long13", archived=False)
    assert next(r for r in rules.catalog() if r["id"] == "long13")["archived"] is False
    with pytest.raises(RuleError, match="没有规则"):
        rules.set_state("nope", enabled=True)


def test_the_catalog_counts_your_verdicts_on_each_rules_hits(paths):
    HitLabels(paths).set("long13", "CN:600519", "2026-09-25", "right")
    HitLabels(paths).set("long13", "CN:000001", "2026-09-24", "wrong")
    cat = {r["id"]: r for r in Rules(paths).catalog()}
    assert cat["long13"]["verdicts"] == {"right": 1, "wrong": 1} and cat["review"]["verdicts"] == {"right": 0, "wrong": 0}


def test_a_new_rule_never_adds_a_version_to_another_one(paths):
    rules = Rules(paths)
    mine = EXAMPLE.replace("id: box_breakout_after_rally", "id: my_box")
    assert rules.save(mine, create=True) == ("my_box", 1, True)
    with pytest.raises(RuleError, match="已经有规则 my_box"):
        rules.save(mine.replace("value: 2.0", "value: 2.5"), create=True)
    assert len(rules.versions("my_box")) == 1
