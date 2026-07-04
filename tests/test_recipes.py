"""Tests for recipe tree resolution (overlay.resolve_recipe_tree).

This is the most complex pure-logic piece of the app - recursive crafting
breakdown with ceil-based craft counts, cycle detection, and alternate-recipe
handling - and the part most likely to silently regress, since a wrong tree
doesn't crash, it just quietly shows the wrong numbers.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import overlay  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(overlay, "DB_PATH", str(tmp_path / "test.db"))
    overlay.init_db()
    return overlay


def test_raw_ingredient_has_no_children(db):
    tree = overlay.resolve_recipe_tree("Iron Ore", qty_needed=5)
    assert tree["name"] == "Iron Ore"
    assert tree["qty"] == 5
    assert tree["is_recipe"] is False
    assert tree["children"] == []
    assert tree["alts"] == []


def test_single_level_recipe_breaks_down_ingredients(db):
    db.save_recipe(None, "Iron Bar", output_qty=2, ingredients=[("Iron Ore", 3)])

    tree = db.resolve_recipe_tree("Iron Bar", qty_needed=4)

    assert tree["is_recipe"] is True
    assert tree["output_qty"] == 2
    assert len(tree["children"]) == 1
    ore = tree["children"][0]
    assert ore["name"] == "Iron Ore"
    # 4 needed / 2 per craft = 2 crafts -> 2 * 3 ore per craft = 6 ore
    assert ore["qty"] == 6
    assert ore["is_recipe"] is False


def test_craft_count_rounds_up_with_ceil(db):
    # output_qty=3 but only 4 needed -> ceil(4/3) = 2 crafts, not 1 or 1.33
    db.save_recipe(None, "Plate", output_qty=3, ingredients=[("Iron Bar", 1)])

    tree = db.resolve_recipe_tree("Plate", qty_needed=4)

    iron_bar = tree["children"][0]
    assert iron_bar["qty"] == 2  # 2 crafts * 1 iron bar each


def test_multi_level_nesting(db):
    db.save_recipe(None, "Iron Bar", output_qty=1, ingredients=[("Iron Ore", 2)])
    db.save_recipe(None, "Gear", output_qty=1, ingredients=[("Iron Bar", 3)])

    tree = db.resolve_recipe_tree("Gear", qty_needed=1)

    iron_bar = tree["children"][0]
    assert iron_bar["is_recipe"] is True
    assert iron_bar["qty"] == 3
    ore = iron_bar["children"][0]
    assert ore["name"] == "Iron Ore"
    assert ore["qty"] == 6  # 3 iron bars * 2 ore each


def test_cycle_is_broken_not_infinite(db):
    # A needs B, B needs A - resolving A must terminate and treat the
    # second occurrence of A as a raw (non-recipe) leaf.
    db.save_recipe(None, "A", output_qty=1, ingredients=[("B", 1)])
    db.save_recipe(None, "B", output_qty=1, ingredients=[("A", 1)])

    tree = db.resolve_recipe_tree("A", qty_needed=1)

    assert tree["is_recipe"] is True
    b_node = tree["children"][0]
    assert b_node["is_recipe"] is True
    a_again = b_node["children"][0]
    assert a_again["name"] == "A"
    assert a_again["is_recipe"] is False  # cycle broken: treated as raw here


def test_alternate_recipes_are_listed(db):
    db.save_recipe(None, "Fuel", output_qty=1, ingredients=[("Coal", 2)], output_name="Energy")
    db.save_recipe(None, "Battery", output_qty=1, ingredients=[("Lithium", 1)], output_name="Energy")

    tree = db.resolve_recipe_tree("Energy", qty_needed=1)

    # First-created recipe (by id) is the default.
    assert tree["recipe_name"] == "Fuel"
    assert len(tree["alts"]) == 1
    assert tree["alts"][0]["recipe_name"] == "Battery"


def test_alt_pref_overrides_default_recipe(db):
    db.save_recipe(None, "Fuel", output_qty=1, ingredients=[("Coal", 2)], output_name="Energy")
    battery_id = db.save_recipe(
        None, "Battery", output_qty=1, ingredients=[("Lithium", 1)], output_name="Energy"
    )

    tree = db.resolve_recipe_tree("Energy", qty_needed=1, _alt_prefs={"Energy": battery_id})

    assert tree["recipe_name"] == "Battery"
    assert tree["children"][0]["name"] == "Lithium"
