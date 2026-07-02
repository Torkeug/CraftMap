# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**CraftMap** is a Windows desktop overlay that tracks in-game resource deposits and crafting recipes. It sits always-on-top over a game window (borderless mode) and can be toggled visible/hidden via a global hotkey (default: F1). It is a single-file Python/tkinter application.

## Commands

**Run from source:**
```
python overlay.py
```
On Windows, the script auto-relaunches itself via `pythonw.exe` to suppress the console window. Run as administrator if the global hotkey fails to register.

**Install the only non-stdlib dependency:**
```
pip install keyboard --break-system-packages
```

**Build the standalone executable:**
```
build.bat
```
This runs PyInstaller (`--onefile --noconsole`) and copies the output to `CraftMap.exe` in the project root.

## Architecture

Everything lives in [overlay.py](overlay.py) (~2480 lines). There are no modules, packages, or tests.

**Layers (top to bottom in the file):**

1. **Config** (`load_config` / `save_config`) — reads/writes `config.json`, which persists window position, size, hotkey, view mode, and collapsed tree node keys.

2. **Deposits DB** — SQLite file `resources.db`. `init_db()` creates the `deposits` table and runs additive column migrations (ALTER TABLE) for the `res_type` and `sector` columns. All reads go through `fetch_all()`, which builds a dynamic WHERE clause for search text and type filtering and supports two sort orders (`resource` vs `location`). Dropdown values come from `distinct_values(column)` — no hardcoded lists.

3. **Recipe DB** — Four additional tables created by `init_db()`:
   - `recipes` (`id`, `name`, `output_qty`, `output_name`) — `output_name` is NULL when the recipe produces an item with the same name as the recipe.
   - `recipe_ingredients` (`id`, `recipe_id`, `ingredient_name`, `quantity`)
   - `recipe_checked` (`recipe_id`, `path_key`) — persists per-ingredient checkbox state across sessions.
   - `recipe_alt_prefs` (`ingredient_name`, `recipe_id`) — stores the user's preferred alternate recipe for each ingredient name.

   Key recipe DB helpers: `get_all_recipes`, `get_recipe_ingredients`, `get_recipes_using_ingredient`, `save_recipe`, `delete_recipe`, `get_checked_paths`, `toggle_checked`, `get_alt_prefs`, `set_alt_pref`, `get_deposits_for_ingredient`.

4. **Recipe tree resolution** (`resolve_recipe_tree`) — Recursively expands a recipe into a tree of `{name, qty, is_recipe, output_qty, recipe_name, children, alts}` nodes. Uses `math.ceil` for craft counts. `alts` lists every other recipe that produces the same output. `_alt_prefs` overrides the default recipe choice per ingredient. Uses cycle detection via `_visited` frozenset. Shared recipe data is loaded in a single `_load_recipe_data()` call and threaded through recursive calls.

5. **`_LiveDropdown`** — Attaches a no-grab suggestion popup (Toplevel + Listbox) to any `ttk.Combobox`. Updates live as the user types; does not lock input. `pre_fn()` is called first to refresh the box's values (e.g. cascade filter); `on_select_fn(val)` is called after the user picks. Never pass `_refresh_recipe_list` as a `pre_fn` — it clears the typed text.

6. **`Overlay` class** (subclasses `tk.Tk`) — the entire UI. Key design decisions:
   - `overrideredirect(True)` removes the native title bar; a custom drag bar at the top handles move, close, settings, and view-mode switching.
   - Window position and size are saved to `config.json` on drag-release and resize-release.
   - The tree widget uses `iid=str(row_id)` for leaf (planet) nodes so `on_select` can detect real DB rows by checking `item_id.isdigit()`. Group header nodes use string keys (`"type|..."`, `"res|..."`, `"loc_sec|..."`, etc.) stored in `_iid_to_key` for collapse-state persistence.
   - **Three view modes** (toggled by drag-bar buttons, persisted to config):
     - **Resource view** groups as: Type → Resource → Sector → System → Planet.
     - **Location view** groups as: Sector → System → Planet (with type/resource summaries under each Sector and System header).
     - **Recipe view** — hides the deposit tree/form entirely (`pack_forget`) and shows the recipe panel. After any `messagebox` call in recipe mode, `_recipe_repaint` does a `pack_forget`/`pack` cycle on `_recipe_frame` to force a tkinter repaint (calling `update()` or `UpdateWindow` does not work for `overrideredirect` windows).
   - Dropdown autocomplete uses two independent cascading chains: `Type → Resource` and `Sector → System → Planet`.
   - `HOTKEY_AVAILABLE` flag gates all `keyboard` library usage; the app degrades gracefully if the library is absent.
   - The global hotkey fires on a daemon thread and posts back to the main thread via `self.after(0, self.toggle)`.
   - `quit_app` calls `os._exit(0)` after `destroy()` to forcibly terminate the daemon hotkey thread.
   - `_enable_composited` sets the WS_EX_COMPOSITED style flag on Windows to reduce resize flicker.

   **Recipe panel** (`_build_recipe_panel`):
   - Selector row: recipe combobox + New button + quantity multiplier + Breakdown/Totals/Used In mode toggle.
   - Breakdown tree (`_recipe_breakdown_tree`): renders the resolved recipe tree. Ingredient nodes show a custom checkbox image; clicking the image calls `toggle_checked`. Alternate recipes appear as collapsed `alt_header` nodes — clicking one calls `set_alt_pref` and re-renders. Raw-resource leaves show deposit locations from `get_deposits_for_ingredient`.
   - Totals mode: flattens the tree into "── Crafted ──" (sub-recipes) and "── Raw materials ──" sections.
   - Used In mode: shows an item search box and lists all recipes that use that ingredient, each clickable to load the recipe into the edit form.
   - Edit form: name, output quantity, optional output item name (for recipes that produce a differently-named item), scrollable ingredient rows, Save/Clear/Delete buttons.

7. **`main()`** — calls `init_db()`, creates the `Overlay`, starts the hotkey daemon thread if available, and enters `mainloop()`.

## Runtime paths

`_APP_DIR` is resolved differently depending on execution context:
- **Frozen exe** (`getattr(sys, "frozen", False)` is True): `os.path.dirname(sys.executable)`
- **Script**: `os.path.dirname(os.path.abspath(__file__))`

Both `resources.db` and `config.json` are always co-located with whichever of these is the app root.

## Ship Builder

A Three.js browser-based ship designer in `shipbuilder/`. Launch with `start.bat` (double-click) or `python -m http.server 8765` then open `http://localhost:8765`.

### Files

| File | Purpose |
|------|---------|
| `shipbuilder/index.html` | App shell — palette, viewport, inspector panels |
| `shipbuilder/style.css` | All styling (Orbitron/Rajdhani fonts, dark theme, stats panel, slot grid) |
| `shipbuilder/js/main.js` | All Three.js logic (~950 lines) |
| `shipbuilder/js/data.js` | Part data loading, fan-stat lookup (`statsFor`), ship stat calc functions |
| `shipbuilder/js/meshLoader.js` | Manifest fetch, geometry loading, cache-busted `.bin` fetches |
| `shipbuilder/ship_editor_data.json` | Complete part catalogue: 77 hull + 59 module parts, all shapes and material variants |
| `shipbuilder/ship_stats_data.json` | Fan-sourced stat values (weight, frame, thrust, shields, etc.) keyed by part name |
| `shipbuilder/ship_meshes/` | `.bin` mesh files + `_manifest.json` |
| `shipbuilder/ship_icons/` | Part icon `.webp` files (one per part ID) |
| `shipbuilder/ship_shapes/` | Shape thumbnail `.webp` files |
| `shipbuilder/start.bat` | Launcher: tries Python then Node.js, opens browser automatically |

### Part model (`ship_editor_data.json`)

Each part has `id`, `name`, `group`, `kind`, `mount`, `dims`, `stats`, `shapes`, `color`/`grad`.

- `kind: 'build'` — hull frames, cockpits, wings, engines (77 parts). Each provides **1 internal module slot**.
- `kind: 'module'` — cargo, FTL, shields, batteries, etc. (59 parts).
  - `mount: 'inside'` — placed into a hull slot via the slot sprite system.
  - `mount: 'outside'` — placed on the grid surface like hull pieces.

**`dims` axis convention is NOT uniform, and NOT even consistent within a single
part kind — this has caused real bugs.** `partDims()` in `main.js` converts each
part's raw `dims` array into Three.js [X, Y, Z] extents:
- Hull frames (the general fallback case): raw `dims` is `[L, W, H]` in a "game"
  convention where W and H are NOT already in Three.js order — `partDims` swaps
  them (`[l, w, h] → [l, h, w]`) to get Three.js X/Y/Z. Confirmed correct for
  hull frames.
- Cockpits: raw `dims` is destructured as `[l, h, w]` (already Three.js-ish
  order) and reordered for the 90°Y rotation `fitGeom` applies to cockpit meshes.
- Outside-mount modules: `partDims` uses raw `dims` **directly, no swap** — but
  the *stored data itself* was authored inconsistently. Most outside modules'
  `dims` are literal `[X, Y, Z]` matching the inspector's "WxHxD" text (applying
  the hull swap to these was a real, confirmed bug — it silently reinterpreted
  Large Solar Panel's `[5, 1, 4]` as a tall 5×4×1 box instead of the intended
  flat 5×1×4 one). But at least two items (Simple Hose Pump, Hi-Pi Laser) were
  entered using the hull `[L, W, H]` convention instead, and needed their stored
  `dims` corrected in place (`[3,14,4]`→`[3,4,14]`, `[4,2,3]`→`[4,3,2]`) rather
  than a code-level swap, since a uniform swap rule broke the majority that were
  already correct. **Determined per-item which convention was used by comparing
  the part's actual extracted mesh bounding-box aspect ratio (post `rotateX(
  -90°)`, i.e. threeX=hmdX, threeY=hmdZ, threeZ=hmdY) against both possible
  readings of `dims` and picking the closer match** — this is the reliable way
  to check, not assumption. If a new outside module looks wrong-shaped in its
  bounding box, check this before suspecting the mesh extraction itself.

### Module slot system (`main.js`)

Inside modules (`isInsideMod(part)`) bypass the grid occupation system entirely and are instead **assigned to a hull piece** via `slotOwner: hullEntry`.

**Key functions:**
- `placeInSlot(part, hullEntry)` — places module at hull center; swaps if slot already occupied.
- `syncSlotModule(hullEntry)` — repositions the slot module after hull drag or rebuild.
- `refreshSlotSprites()` — rebuilds Three.js Sprite billboards over all hull pieces; visible only when Modules tab is active.
- `setSlotHighlight(hullEntry, on)` — highlights the hovered slot sprite.

**Slot sprite textures** (canvas-based, created once at module load):
- `TEX_SLOT_EMPTY` — dashed white border (no module).
- `TEX_SLOT_OCCUPIED` — not used directly; replaced by `getSlotOccupiedTex(part)`.
- `TEX_SLOT_HOVER` / `TEX_SLOT_HOVER_SWAP` — fallback hover states.
- `getSlotOccupiedTex(part)` — canvas texture with part icon + cyan border; async image load updates texture.
- `getSlotHoverTex(part, isSwap)` — hover preview showing selected module's icon; white border = place, amber = swap.

**Interaction:**
- Switch to **Modules tab** → slot sprites appear over all hull pieces.
- Select an inside module → hover over sprites shows the module icon as preview (white = empty, amber = will swap).
- Left-click sprite → place or swap.
- Right-click sprite → remove installed module.
- Dragging a hull piece moves its slot module with it (`syncSlotModule`).
- Removing a hull piece cascades to remove its slot module.

**Save/load:** `slotOwnerIdx` (index into the placed array) persists slot assignments across clipboard save/load.

### Ship stats panel (`updateShipStats` in `main.js`)

Shown in the inspector when parts are placed. Sections:
- **Verdict banner** — flight-ready / not ready.
- **Viability checks** — Cockpit, Engine, Thrust/Mass, Integrity, Sys. support, Power, FTL cap. (if FTL present), Module slots.
- **System Support bar** — SP used vs capacity.
- **Structure** — Weight, Frames, Integrity (fan formula: `200 − 7w²/25f`), Maneuverability (`280×steering/w^1.5`).
- **Propulsion** — Thrust, Force.
- **Power** — Gen, Usage (`PowerUsage + EngineConsumption`), Net, Battery, Recharge, Heat cap.
- **Module Slots** — dot grid (one dot per hull piece, cyan = occupied), foot label.
- **Cargo** — Solid, Liquid, Mag fuel, FTL cap.
- **Combat & Heat** — Shields, Heat gen. (fan data).

Stats sourced from `part.stats` (game data in `ship_editor_data.json`) plus `statsFor(name)` (fan data in `ship_stats_data.json`).

### HMD Mesh Pipeline

The source game (SpaceCraft) runs on **Heaps.io** (a Haxe game engine), or a modified/customized build of it — confirmed by `res.pak`'s directory format, the `HMD` mesh magic, and a `.prefab` object-tree format that matches Heaps' `hxbit` binary serializer conventions (tag bytes 0/1/2/3/4/5/6/7 for null/false/true/int/float/object/string/array — see `tools/prefab_parse.py`). Assume Heaps/Haxe conventions when reverse-engineering any new binary format from `res.pak`.

See [`tools/hmd_format_notes.md`](tools/hmd_format_notes.md) for full format documentation, coordinate transforms, vertex/index buffer layouts, and the .bin output format. **Keep this file up to date** with any new findings discovered during conversion work.

**All tools must be saved to `tools/`** — never write a tool only in memory or in a code block. Save every script immediately after writing it, even if incomplete.

**Do not use .har files as reference — use only the in-game extracted files from pak_out.**

#### Current state

- Production HMD format (magic `HMD\x06`, disc=0x02): fully decoded. Three ring-buffer variants documented in `hmd_format_notes.md`.
- **129 of 130 shapes from pak_out.** All 11 hull sizes complete. Only 8x3x1_N remains HAR-sourced (anomalous format — raw index data at byte 0, no parseable HMD header).
- `shipbuilder/ship_editor_data.json` — complete with all hull sizes and all material variants. No edits needed there.
- `shipbuilder/ship_shapes/` — missing H.webp, I.webp, L.webp, M.webp shape thumbnails.

#### Conversion tools

| Tool                          | Purpose                                                            |
|-------------------------------|--------------------------------------------------------------------|
| `tools/hmd_parse_prod.py`     | Parser for production HMD v0x06: `parse_prod_hmd()`, `parse_material_groups()`, `_parse_attr_blocks()`, `read_verts_f16()`, `read_indices_le_u16()` |
| `tools/hmd_to_bin.py`         | CLI converter: auto-detects format and writes .bin; entry point for all conversions |
| `tools/batch_convert_hulls.py`| Batch converter: converts all Main_Structures sizes from pak_out, updates `_manifest.json` |
| `tools/hmd_parse.py`          | Legacy parser for TestPE (disc=0x00) files — no longer primary focus |
| `tools/pak_extract.py`        | Extracts both disc=0x00 and disc=0x02 files from res.pak using cumulative offset calculation for disc=0x02 |

**Running the converter:**
```
python tools/hmd_to_bin.py <input.hmd> <output.bin>
python tools/batch_convert_hulls.py   # converts all sizes, updates _manifest.json
```

#### Remaining work

**pak_extract.py** handles disc=0x02 extraction (D02_DATA_START = 2,156,315,392, 16-byte alignment). Re-extract any hull size: `python tools/pak_extract.py --extract "Main_Structures" --out pak_out`.

## Data model

**`deposits`** columns: `id`, `res_type`, `resource`, `sector`, `system_name`, `planet`, `status`, `notes`, `logged_at`.

`status` is one of: `Free`, `Claimed`, `Depleted`, `Unknown`. `planet` is the only required field for insert/update. Duplicate detection prevents inserting or updating to an exact `(res_type, resource, sector, system_name, planet)` combination.

**`recipes`** columns: `id`, `name`, `output_qty`, `output_name`. `output_name` is NULL when the recipe's output has the same name as the recipe itself.

**`recipe_ingredients`** columns: `id`, `recipe_id`, `ingredient_name`, `quantity`.

**`recipe_checked`** columns: `recipe_id`, `path_key` (composite PK). `path_key` is a `|`-joined chain of ingredient names encoding the tree path.

**`recipe_alt_prefs`** columns: `ingredient_name` (PK), `recipe_id`. Stores which alternate recipe the user prefers for each ingredient name.
