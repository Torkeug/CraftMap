# Game-extracted crafting data

Extracted directly from the game's own `data.cdb` (`shipbuilder/pak_out/data.cdb`,
Heaps/Haxe CDB sheet format) by
[`shipbuilder/tools/extract_craft_data.py`](../../shipbuilder/tools/extract_craft_data.py).
This is the game's authoritative recipe data — not the hand-entered data currently
in `resources.db`. Nothing here touches `resources.db`; these are plain JSON files
for review before you decide how to merge.

## Files

- **`craft_recipes.json`** — 479 recipes, raw `craft` sheet rows. Fields:
  - `id` — recipe id (not always the same as the output item id)
  - `guid` — stable game-internal id
  - `inputs`: `[{item, qty}, ...]` — ingredient item ids + quantities
  - `outputs`: `[{item, qty}, ...]` — output item ids + quantities. **`qty` is
    omitted (defaults to 1) on 309 of 479 recipes.** 47 recipes have more than
    one output (e.g. smelting a crystal can yield two different ingots).
  - `where` — crafting station id (`Workshop_Smelter`, `Workshop_Atelier`,
    `Workshop_Chemical`, `Workshop_Crystalizer`, `Workshop_Seed`,
    `Workshop_Bottle`, `Workshop_Factory`/`Factory2`, `Workshop_Building`,
    `Workshop_Recycle`, `Workshop_Science`, `Workshop_Uncraftable`, or absent)
  - `category` — one of 26 values (`Craft_RawResource`, `Craft_Modules`,
    `Craft_Parts`, `Craft_Dismantle`, `BaseBuilding`, etc.)
  - `unlockType`, `lootLevel` — progression gating
  - `props` — free-form dict, e.g. `autoPowerCost`, `craftTimeFactor`,
    `manualTime`, `autoTime`
  - `note` — occasional dev comments (e.g. `"replace silicium by chromium at
    some point"`) — these are **known TODOs/inconsistencies from the
    developers themselves**, worth keeping visible rather than "fixing" silently.

- **`items.json`** — `{item_id: {name, type, guid, price, desc}}` for all 585
  items, to resolve the ids used in `inputs`/`outputs` to display names.

- **`item_types.json`** — `{type_id: {name, parent}}`, the item category tree.

- **`item_tags.json`** — `{tag_id: props}`, includes craft-station display
  metadata (`craftAction`, `craftIndex`, `manualCraftTime`, `autoCraftTime`,
  `label`, `color`, `flags`) for tags like `Station`.

- **`craft_values.json`** — per-station economy constants (power cost, price
  decay, etc), keyed by `craftKind` (matches `where` in recipes).

## How this differs from `resources.db`'s recipe tables

| | `resources.db` (`recipes`/`recipe_ingredients`) | game data (`craft_recipes.json`) |
|---|---|---|
| Recipe count | 254 | 479 |
| Source | hand-entered while playing | authoritative game files |
| Output quantity | `recipes.output_qty` (single) | `outputs[].qty`, and **can have multiple outputs per recipe** |
| Output name override | `recipes.output_name` (nullable) | `outputs[].item` vs `craft.id` can already differ; no 1:1 assumption needed |
| Ingredient id | free-text `ingredient_name` | canonical `item_id`, resolvable via `items.json` for a display name |
| Station / category / power cost / dev notes | not tracked | `where`, `category`, `props`, `note` |
| Alternate recipes for one output | inferred at runtime by matching `output_name` across rows | same idea works, but grouping should be done by `outputs[].item`, not by `craft.id` |

Two things worth deciding before merging into `overlay.py`'s schema:

1. **Multiple outputs per recipe** isn't representable in the current
   `recipes` table (`output_qty` + one `output_name`). Recipes like
   `AluminiumIngot_Aquamarine` (outputs `SiliciumIngot` qty 2 *and*
   `AluminiumIngot` qty 1) would need either a new `recipe_outputs` table
   mirroring `recipe_ingredients`, or splitting into synthetic per-output rows.
2. **Ids vs. display names** — `craft_recipes.json` uses internal ids
   (`IronOre`, `AluminiumIngot_Aquamarine`), while `resources.db` currently
   stores human-readable names typed in by hand. `items.json` gives you the
   `name` to resolve ids, but existing manually-entered names may not match
   the game's naming exactly (worth a diff pass before assuming a 1:1 rename).
