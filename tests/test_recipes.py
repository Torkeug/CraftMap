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
    db.save_recipe(None, "Iron Bar", outputs=[("Iron Bar", 2)], ingredients=[("Iron Ore", 3)])

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
    # output qty=3 but only 4 needed -> ceil(4/3) = 2 crafts, not 1 or 1.33
    db.save_recipe(None, "Plate", outputs=[("Plate", 3)], ingredients=[("Iron Bar", 1)])

    tree = db.resolve_recipe_tree("Plate", qty_needed=4)

    iron_bar = tree["children"][0]
    assert iron_bar["qty"] == 2  # 2 crafts * 1 iron bar each


def test_multi_level_nesting(db):
    db.save_recipe(None, "Iron Bar", outputs=[("Iron Bar", 1)], ingredients=[("Iron Ore", 2)])
    db.save_recipe(None, "Gear", outputs=[("Gear", 1)], ingredients=[("Iron Bar", 3)])

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
    db.save_recipe(None, "A", outputs=[("A", 1)], ingredients=[("B", 1)])
    db.save_recipe(None, "B", outputs=[("B", 1)], ingredients=[("A", 1)])

    tree = db.resolve_recipe_tree("A", qty_needed=1)

    assert tree["is_recipe"] is True
    b_node = tree["children"][0]
    assert b_node["is_recipe"] is True
    a_again = b_node["children"][0]
    assert a_again["name"] == "A"
    assert a_again["is_recipe"] is False  # cycle broken: treated as raw here


def test_alternate_recipes_are_listed(db):
    db.save_recipe(None, "Fuel", outputs=[("Energy", 1)], ingredients=[("Coal", 2)])
    db.save_recipe(None, "Battery", outputs=[("Energy", 1)], ingredients=[("Lithium", 1)])

    tree = db.resolve_recipe_tree("Energy", qty_needed=1)

    # First-created recipe (by id) is the default.
    assert tree["recipe_name"] == "Fuel"
    assert len(tree["alts"]) == 1
    assert tree["alts"][0]["recipe_name"] == "Battery"


def test_alt_pref_overrides_default_recipe(db):
    db.save_recipe(None, "Fuel", outputs=[("Energy", 1)], ingredients=[("Coal", 2)])
    battery_id = db.save_recipe(
        None, "Battery", outputs=[("Energy", 1)], ingredients=[("Lithium", 1)]
    )

    tree = db.resolve_recipe_tree("Energy", qty_needed=1, _alt_prefs={"Energy": battery_id})

    assert tree["recipe_name"] == "Battery"
    assert tree["children"][0]["name"] == "Lithium"


def test_recipe_station_and_time_returned_in_tree(db):
    db.save_recipe(
        None,
        "Steel Ingot",
        outputs=[("Steel Ingot", 3)],
        ingredients=[("Iron Ingot", 4)],
        station="Smelter",
        auto_craft_seconds=180.0,
        manual_craft_seconds=5.0,
    )

    tree = db.resolve_recipe_tree("Steel Ingot", qty_needed=3)

    assert tree["station"] == "Smelter"
    assert tree["auto_craft_seconds"] == 180.0
    assert tree["manual_craft_seconds"] == 5.0


def test_multi_output_recipe_returns_scaled_byproducts(db):
    # Smelting Aquamarine yields both Silicium Ingot and Aluminium Ingot.
    db.save_recipe(
        None,
        "Aluminium Ingot Aquamarine",
        outputs=[("Silicium Ingot", 2), ("Aluminium Ingot", 1)],
        ingredients=[("Aquamarine", 3)],
    )

    tree = db.resolve_recipe_tree("Aluminium Ingot", qty_needed=2)

    # ceil(2 / 1) = 2 crafts -> 2 * 2 = 4 Silicium Ingot as a byproduct.
    assert tree["output_qty"] == 1
    assert tree["byproducts"] == [{"name": "Silicium Ingot", "qty": 4.0}]


def test_alts_grouped_by_output_item_not_recipe_id(db):
    # A single multi-output recipe must show up as an alt under BOTH of its
    # outputs' buckets, each scaled to that output's own qty - not just its
    # "primary" output. The two single-output recipes are saved first so they
    # win as the default (first-by-id) recipe for each item.
    db.save_recipe(None, "Steel", outputs=[("Steel Ingot", 3)], ingredients=[("Iron Ingot", 4)])
    db.save_recipe(None, "Copper", outputs=[("Copper Ingot", 1)], ingredients=[("Copper Ore", 2)])
    db.save_recipe(
        None,
        "Recycle Steel Hull",
        outputs=[("Steel Ingot", 2), ("Copper Ingot", 1)],
        ingredients=[("Wrecked Hull", 4)],
    )

    steel_tree = db.resolve_recipe_tree("Steel Ingot", qty_needed=3)
    copper_tree = db.resolve_recipe_tree("Copper Ingot", qty_needed=1)

    steel_alt_names = {alt["recipe_name"] for alt in steel_tree["alts"]}
    copper_alt_names = {alt["recipe_name"] for alt in copper_tree["alts"]}
    assert "Recycle Steel Hull" in steel_alt_names
    assert "Recycle Steel Hull" in copper_alt_names

    steel_alt = next(
        alt for alt in steel_tree["alts"] if alt["recipe_name"] == "Recycle Steel Hull"
    )
    assert steel_alt["output_qty"] == 2
    assert steel_alt["byproducts"] == [{"name": "Copper Ingot", "qty": 2.0}]


def test_get_all_output_names_includes_secondary_outputs(db):
    db.save_recipe(
        None,
        "Aluminium Ingot Aquamarine",
        outputs=[("Silicium Ingot", 2), ("Aluminium Ingot", 1)],
        ingredients=[("Aquamarine", 3)],
    )

    names = db.get_all_output_names()

    assert "Silicium Ingot" in names
    assert "Aluminium Ingot" in names
