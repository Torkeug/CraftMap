# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**CraftMap** is a Windows desktop overlay that tracks in-game resource deposits and crafting recipes. It sits always-on-top over a game window (borderless mode) and can be toggled visible/hidden via a global hotkey (default: F1). It is a Python/tkinter application: almost everything lives in `overlay.py`, with Win32 interop split out into `win32util.py` (see Architecture).

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

**Run tests:**
```
pip install pytest
python -m pytest tests/
```
Tests cover `resolve_recipe_tree` only (the one part of the app with real logic to regress: ceil-based craft counts, cycle detection, alternate-recipe selection). There is no UI test coverage — `win32util.py`'s hwnd/focus behavior is Windows-interop that's verified manually (see the "F1 to focus" note below), not unit tested.

## Architecture

Almost everything lives in [overlay.py](overlay.py) (~4000 lines): config, both databases, recipe resolution, and the entire tkinter UI. [win32util.py](win32util.py) holds all Win32/ctypes interop (hwnd resolution, OS focus detection/grabbing, click-through, resize-redraw nudging, single-instance mutex) — it used to be scattered across overlay.py as ad-hoc `ctypes.windll` calls at each call site, which is how a real bug shipped: two call sites assumed different hwnd semantics (an inner content window vs. the actual top-level ancestor) for what was supposed to be the same window. `tests/` has pytest coverage for recipe resolution only.

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
   - Both windows are `-alpha`-translucent `overrideredirect` popups. They deliberately do **not** set `WS_EX_COMPOSITED` - it was originally added to reduce resize flicker, but on this alpha+overrideredirect combination it left a permanent unpainted black band along one window edge (present since the very first commit, not a regression). `win32util.redraw_window` (called from the resize-drag handlers only) is the flicker mitigation that doesn't have that side effect.
   - **Focus / click-through**: the overlay is click-through (`WS_EX_TRANSPARENT`) whenever it doesn't have real OS focus, so clicks and the cursor pass to the game underneath; `_poll_input_passthrough` re-checks this every 250ms via `win32util.hwnd_is_foreground` (a raw `GetForegroundWindow()` comparison — deliberately not Tk's own `focus_get()`, which is Tcl-internal bookkeeping that can drift from what Windows actually considers focused). F1 regains focus via `_grab_os_focus`, which calls `win32util.force_foreground_window` — a plain `SetForegroundWindow()` is silently ignored by Windows' foreground-lock heuristic when called (as here) from a background hotkey thread marshalled onto the Tk loop, so it uses the `AttachThreadInput` workaround.

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

## Data model

**`deposits`** columns: `id`, `res_type`, `resource`, `sector`, `system_name`, `planet`, `status`, `notes`, `logged_at`.

`status` is one of: `Free`, `Claimed`, `Depleted`, `Unknown`. `planet` is the only required field for insert/update. Duplicate detection prevents inserting or updating to an exact `(res_type, resource, sector, system_name, planet)` combination.

**`recipes`** columns: `id`, `name`, `output_qty`, `output_name`. `output_name` is NULL when the recipe's output has the same name as the recipe itself.

**`recipe_ingredients`** columns: `id`, `recipe_id`, `ingredient_name`, `quantity`.

**`recipe_checked`** columns: `recipe_id`, `path_key` (composite PK). `path_key` is a `|`-joined chain of ingredient names encoding the tree path.

**`recipe_alt_prefs`** columns: `ingredient_name` (PK), `recipe_id`. Stores which alternate recipe the user prefers for each ingredient name.
