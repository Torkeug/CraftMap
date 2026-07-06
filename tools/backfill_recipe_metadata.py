"""
One-off maintenance script: enrich resources.db's existing recipes with
game-authoritative station/craft-time/multi-output data pulled from
game_data_extract/ (itself extracted from the game's data.cdb by the
sibling shipbuilder/tools/extract_craft_data.py). Does not touch anything
outside recipes/recipe_outputs - deposits and other tables are untouched.

Craft-time formula mirrors the game's own compiled logic, decompiled from
hlboot.dat (src/lib/utils/CraftUtils.hx:34-44):
    autoTime   = props.autoTime   if set else station.autoCraftTime   * (props.craftTimeFactor ?? 1)
    manualTime = props.manualTime if set else station.manualCraftTime * (props.manualTimeFactor ?? props.craftTimeFactor ?? 1)

Usage:
    python tools/backfill_recipe_metadata.py                 # enrich existing recipes
    python tools/backfill_recipe_metadata.py --report-missing # write a review doc for
                                                                # game recipes with no DB
                                                                # counterpart yet (read-only)
"""
import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import overlay  # noqa: E402

GAME_DATA_DIR = REPO_ROOT / "game_data_extract"

# Local-machine-only path to the game's raw data.cdb, needed only for
# multi-station enrichment: the per-building time-modifier attributes
# (ProduceTimeFactor, UseManualCraftingTime) aren't part of the
# game_data_extract/ snapshot, since only one category (Workshop_Smelter)
# currently needs them. Acceptable here since this is a one-off dev-machine
# maintenance script, not part of the shipped app.
DATA_CDB_PATH = Path(r"D:\Documents\Spacecraft\shipbuilder\pak_out\data.cdb")

# $Const.ShipOnBoardSmelterTimeFactor, verified via hlbc decompile of
# hlboot.dat's `constant` sheet - the ship's on-board smelter skill runs at
# getManualTime(craft) * this factor, with no auto/passive-queue equivalent.
SHIP_ONBOARD_SMELTER_FACTOR = 3.0


def _norm(s):
    return re.sub(r"\s+", " ", s or "").strip().lower()


def load_game_data():
    items = json.loads((GAME_DATA_DIR / "items.json").read_text(encoding="utf-8"))
    recipes = json.loads(
        (GAME_DATA_DIR / "craft_recipes.json").read_text(encoding="utf-8")
    )
    item_tags = json.loads(
        (GAME_DATA_DIR / "item_tags.json").read_text(encoding="utf-8")
    )
    return items, recipes, item_tags


def item_name(items, item_id):
    return items.get(item_id, {}).get("name") or item_id


def resolve_craft_time(craft, item_tags):
    """Return (auto_seconds, manual_seconds), mirroring the game's own
    CraftUtils.getAutoTime/getManualTime exactly."""
    props = craft.get("props", {})
    station = item_tags.get(craft.get("where"), {})

    auto_s = props.get("autoTime")
    if auto_s is None:
        base = station.get("autoCraftTime")
        base = base if base is not None else 0
        factor = props.get("craftTimeFactor")
        factor = factor if factor is not None else 1
        auto_s = base * factor

    manual_s = props.get("manualTime")
    if manual_s is None:
        base = station.get("manualCraftTime")
        base = base if base is not None else 0
        factor = props.get("manualTimeFactor")
        if factor is None:
            factor = props.get("craftTimeFactor")
        if factor is None:
            factor = 1
        manual_s = base * factor

    return float(auto_s), float(manual_s)


def humanize(id_str):
    """CopperIngot_Azurite -> 'Copper Ingot Azurite'; RecycleSteelHull ->
    'Recycle Steel Hull'. Best-effort - just a suggestion for review."""
    s = id_str.replace("_", " ")
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def propose_name(craft, items):
    primary_item = craft["outputs"][0]["item"]
    primary_name = item_name(items, primary_item)
    craft_id = craft["id"]
    if craft_id == primary_item:
        return primary_name
    prefix = primary_item + "_"
    if craft_id.startswith(prefix):
        return f"{primary_name} {humanize(craft_id[len(prefix):])}"
    return humanize(craft_id)


def build_game_index(items, recipes):
    """item display name (normalized) -> [{"craft": craft_dict, "inputs": {norm_name: qty}}, ...]"""
    index = defaultdict(list)
    for craft in recipes:
        resolved_inputs = {
            _norm(item_name(items, i["item"])): i["qty"] for i in craft["inputs"]
        }
        for o in craft["outputs"]:
            index[_norm(item_name(items, o["item"]))].append(
                {"craft": craft, "inputs": resolved_inputs}
            )
    return index


def enrich(items, recipes, item_tags):
    game_index = build_game_index(items, recipes)

    conn = sqlite3.connect(overlay.DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT r.id, r.name, ro.item_name"
        " FROM recipes r JOIN recipe_outputs ro ON ro.recipe_id = r.id"
        " WHERE ro.id = (SELECT MIN(id) FROM recipe_outputs WHERE recipe_id = r.id)"
        " AND (r.game_craft_id IS NULL OR r.game_craft_id = '')"
    )
    db_recipes = c.fetchall()

    matched = 0
    skipped = []
    for rid, rname, out_name in db_recipes:
        c.execute(
            "SELECT ingredient_name, quantity FROM recipe_ingredients WHERE recipe_id=?",
            (rid,),
        )
        db_ings = {_norm(n) for n, _ in c.fetchall()}
        candidates = game_index.get(_norm(out_name), [])
        best = None
        if len(candidates) == 1:
            best = candidates[0]
        else:
            for cand in candidates:
                if set(cand["inputs"].keys()) == db_ings:
                    best = cand
                    break
        if best is None:
            skipped.append((rname, out_name, len(candidates)))
            continue

        craft = best["craft"]
        station = item_tags.get(craft.get("where"), {}).get("label")
        auto_s, manual_s = resolve_craft_time(craft, item_tags)
        c.execute(
            "UPDATE recipes SET game_craft_id=?, station=?, auto_craft_seconds=?,"
            " manual_craft_seconds=? WHERE id=?",
            (craft["id"], station, auto_s, manual_s, rid),
        )

        c.execute("SELECT item_name FROM recipe_outputs WHERE recipe_id=?", (rid,))
        existing = {_norm(n) for (n,) in c.fetchall()}
        for o in craft["outputs"]:
            oname = item_name(items, o["item"])
            if _norm(oname) in existing:
                continue
            c.execute(
                "INSERT INTO recipe_outputs (recipe_id, item_name, quantity)"
                " VALUES (?, ?, ?)",
                (rid, oname, o.get("qty", 1)),
            )
            existing.add(_norm(oname))
        matched += 1

    conn.commit()
    conn.close()

    print(f"Matched/updated {matched} of {len(db_recipes)} unmatched recipes.")
    if skipped:
        print(
            f"{len(skipped)} recipes could not be matched automatically "
            "(no game candidate's ingredients matched exactly):"
        )
        for rname, out_name, n_candidates in skipped:
            print(
                f"  - {rname!r} (produces {out_name!r}): {n_candidates} game candidates"
            )


def load_buildings_by_tag():
    """Every `item` sheet entry that provides a craft station (props.tag
    set), grouped by tag, with its time-modifier attributes. Usually one
    building per tag; Workshop_Smelter currently has two (Smelter,
    Micro-Furnace)."""
    if not DATA_CDB_PATH.exists():
        raise FileNotFoundError(
            f"Cannot find {DATA_CDB_PATH} - multi-station enrichment needs direct "
            "access to the game's raw data.cdb, since the per-building time-modifier "
            "attributes (ProduceTimeFactor, UseManualCraftingTime) aren't part of "
            "game_data_extract/. Skipping multi-station enrichment."
        )
    data = json.loads(DATA_CDB_PATH.read_text(encoding="utf-8"))
    sheets = {s["name"]: s for s in data["sheets"]}
    buildings_by_tag = defaultdict(list)
    for line in sheets["item"]["lines"]:
        tag = line.get("props", {}).get("tag")
        if not tag:
            continue
        attrs = {a["attr"]: a["value"] for a in line.get("attributes", [])}
        buildings_by_tag[tag].append(
            {
                "id": line["id"],
                "name": line.get("name") or line["id"],
                "produce_time_factor": attrs.get("ProduceTimeFactor", 1),
                "use_manual_crafting_time": bool(
                    attrs.get("UseManualCraftingTime", 0)
                ),
            }
        )
    return buildings_by_tag


def enrich_stations(recipes, item_tags):
    """Populate recipe_stations with every usable station for already-matched
    recipes (game_craft_id set), for categories where a recipe can be
    crafted more than one way - mirrors ent.b.Factory.getProduceTime
    (src/ent/b/Factory.hx:177-181) for each building's auto/passive-queue
    value, plus the plain manual-click value (unaffected by per-building
    modifiers), plus the ship's on-board smelter skill for Workshop_Smelter
    specifically. Categories with exactly one plain building are left as
    already backfilled by `enrich()` - re-running is safe."""
    try:
        buildings_by_tag = load_buildings_by_tag()
    except FileNotFoundError as e:
        print(e)
        return

    crafts_by_id = {c["id"]: c for c in recipes}

    conn = sqlite3.connect(overlay.DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, game_craft_id FROM recipes"
        " WHERE game_craft_id IS NOT NULL AND game_craft_id != ''"
    )
    matched = c.fetchall()

    updated = 0
    for rid, craft_id in matched:
        craft = crafts_by_id.get(craft_id)
        if craft is None:
            continue
        tag = craft.get("where")
        buildings = buildings_by_tag.get(tag, [])
        has_modifiers = any(
            b["produce_time_factor"] != 1 or b["use_manual_crafting_time"]
            for b in buildings
        )
        is_smelter = tag == "Workshop_Smelter"
        if len(buildings) <= 1 and not has_modifiers and not is_smelter:
            continue  # single plain station - already correctly backfilled

        base_auto_s, manual_s = resolve_craft_time(craft, item_tags)
        category_label = item_tags.get(tag, {}).get("label") or tag
        stations = []
        for b in buildings:
            produce_time = (
                manual_s if b["use_manual_crafting_time"] else base_auto_s
            ) * b["produce_time_factor"]
            station_label = b["name"] if len(buildings) > 1 else category_label
            stations.append((station_label, float(produce_time), manual_s))
        if is_smelter:
            stations.append(
                ("Ship (on-board)", None, manual_s * SHIP_ONBOARD_SMELTER_FACTOR)
            )
        if not stations:
            continue

        c.execute("DELETE FROM recipe_stations WHERE recipe_id=?", (rid,))
        for st_name, auto_s, m_s in stations:
            c.execute(
                "INSERT INTO recipe_stations"
                " (recipe_id, station, auto_craft_seconds, manual_craft_seconds)"
                " VALUES (?, ?, ?, ?)",
                (rid, st_name, auto_s, m_s),
            )
        primary = stations[0]
        c.execute(
            "UPDATE recipes SET station=?, auto_craft_seconds=?, manual_craft_seconds=?"
            " WHERE id=?",
            (primary[0], primary[1], primary[2], rid),
        )
        updated += 1

    conn.commit()
    conn.close()
    print(
        f"Multi-station enrichment: updated {updated} of {len(matched)} matched recipes."
    )


def get_matched_craft_ids():
    conn = sqlite3.connect(overlay.DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT DISTINCT game_craft_id FROM recipes"
        " WHERE game_craft_id IS NOT NULL AND game_craft_id != ''"
    )
    ids = {row[0] for row in c.fetchall()}
    conn.close()
    return ids


def report_missing(items, recipes, item_tags):
    matched_ids = get_matched_craft_ids()
    missing = [
        r for r in recipes if r["id"] not in matched_ids and r.get("outputs")
    ]
    by_cat = defaultdict(list)
    for r in missing:
        by_cat[r.get("category") or "(none)"].append(r)

    lines = [
        "# Missing recipes review",
        "",
        f"{len(missing)} of {len(recipes)} game recipes have no matching"
        " resources.db recipe yet. Proposed names are suggestions only -"
        " edit before hand-entering.",
        "",
    ]
    for cat in sorted(by_cat):
        entries = sorted(by_cat[cat], key=lambda r: r["id"])
        lines.append(f"## {cat} ({len(entries)})")
        lines.append("")
        for craft in entries:
            name = propose_name(craft, items)
            station = (
                item_tags.get(craft.get("where"), {}).get("label")
                or craft.get("where")
                or "(none)"
            )
            auto_s, manual_s = resolve_craft_time(craft, item_tags)
            inputs = ", ".join(
                f"{i['qty']:g}x {item_name(items, i['item'])}" for i in craft["inputs"]
            )
            outputs = ", ".join(
                f"{o.get('qty', 1):g}x {item_name(items, o['item'])}"
                for o in craft["outputs"]
            )
            lines.append(
                f"- **{name}** (`{craft['id']}`) - station: {station},"
                f" auto: {auto_s:g}s, manual: {manual_s:g}s"
            )
            lines.append(f"  - inputs: {inputs}")
            lines.append(f"  - outputs: {outputs}")
            if craft.get("note"):
                lines.append(f"  - note: {craft['note']}")
            lines.append("")

    out_path = GAME_DATA_DIR / "missing_recipes_review.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {len(missing)} missing recipes to {out_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-missing",
        action="store_true",
        help="Write game_data_extract/missing_recipes_review.md instead of enriching.",
    )
    args = parser.parse_args()

    overlay.init_db()
    items, recipes, item_tags = load_game_data()

    if args.report_missing:
        report_missing(items, recipes, item_tags)
    else:
        enrich(items, recipes, item_tags)
        enrich_stations(recipes, item_tags)


if __name__ == "__main__":
    main()
