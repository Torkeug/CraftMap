# pylint: disable=missing-function-docstring,missing-class-docstring,too-many-lines
"""
CraftMap Resource Overlay
A lightweight, always-on-top, hotkey-toggleable resource tracker
designed to sit over the game window in borderless mode.

Dependencies:
    pip install keyboard --break-system-packages
    (Windows: run as admin if hotkey doesn't register)

Run:
    python overlay.py
"""

import json
import tkinter as tk
from tkinter import ttk, messagebox
import sqlite3
import ctypes
import os
import sys
import math
import threading
import datetime

if (
    sys.platform == "win32"
    and not getattr(sys, "frozen", False)
    and not sys.executable.lower().endswith("pythonw.exe")
):
    import shutil
    import subprocess

    _pythonw = shutil.which("pythonw.exe") or shutil.which("pythonw")
    if _pythonw:
        subprocess.Popen([_pythonw, os.path.abspath(__file__)] + sys.argv[1:])
        sys.exit(0)

try:
    import keyboard  # global hotkey support

    HOTKEY_AVAILABLE = True
except ImportError:
    HOTKEY_AVAILABLE = False

try:
    import pystray
    from PIL import Image, ImageDraw

    PYSTRAY_AVAILABLE = True
except ImportError:
    PYSTRAY_AVAILABLE = False

_APP_DIR = (
    os.path.dirname(sys.executable)
    if getattr(sys, "frozen", False)
    else os.path.dirname(os.path.abspath(__file__))
)
DB_PATH = os.path.join(_APP_DIR, "resources.db")
CONFIG_PATH = os.path.join(_APP_DIR, "config.json")

STATUS_OPTIONS = ["Free", "Claimed", "Depleted", "Unknown"]

_AUTOCOMPLETE_SKIP = frozenset(
    {
        "Return",
        "Tab",
        "Escape",
        "Up",
        "Down",
        "Left",
        "Right",
        "Control_L",
        "Control_R",
        "Alt_L",
        "Alt_R",
        "Shift_L",
        "Shift_R",
        "caps_lock",
    }
)


# ---------- Config ----------


def load_config():
    defaults = {"toggle_key": "F1"}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                return {**defaults, **json.load(f)}
        except (OSError, ValueError):
            pass
    return defaults


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


# ---------- Database ----------


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            res_type TEXT,
            resource TEXT NOT NULL,
            system_name TEXT NOT NULL,
            planet TEXT NOT NULL,
            status TEXT,
            notes TEXT,
            logged_at TEXT
        )
    """)
    # migrations: add columns to older DBs that don't have them yet
    c.execute("PRAGMA table_info(deposits)")
    cols = [row[1] for row in c.fetchall()]
    if "res_type" not in cols:
        c.execute("ALTER TABLE deposits ADD COLUMN res_type TEXT")
    if "sector" not in cols:
        c.execute("ALTER TABLE deposits ADD COLUMN sector TEXT")
    c.execute("""
        CREATE TABLE IF NOT EXISTS recipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            output_qty REAL NOT NULL DEFAULT 1,
            output_name TEXT
        )
    """)
    c.execute("PRAGMA table_info(recipes)")
    recipe_cols = [row[1] for row in c.fetchall()]
    if "output_qty" not in recipe_cols:
        c.execute("ALTER TABLE recipes ADD COLUMN output_qty REAL NOT NULL DEFAULT 1")
    if "output_name" not in recipe_cols:
        c.execute("ALTER TABLE recipes ADD COLUMN output_name TEXT")
    c.execute("""
        CREATE TABLE IF NOT EXISTS recipe_ingredients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER NOT NULL,
            ingredient_name TEXT NOT NULL,
            quantity REAL NOT NULL DEFAULT 1,
            FOREIGN KEY (recipe_id) REFERENCES recipes(id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS recipe_checked (
            recipe_id INTEGER NOT NULL,
            path_key TEXT NOT NULL,
            PRIMARY KEY (recipe_id, path_key),
            FOREIGN KEY (recipe_id) REFERENCES recipes(id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS recipe_alt_prefs (
            ingredient_name TEXT PRIMARY KEY,
            recipe_id INTEGER NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS craft_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER NOT NULL,
            quantity REAL NOT NULL DEFAULT 1,
            FOREIGN KEY (recipe_id) REFERENCES recipes(id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS queue_checked (
            queue_id INTEGER NOT NULL,
            path_key TEXT NOT NULL,
            PRIMARY KEY (queue_id, path_key)
        )
    """)
    conn.commit()
    conn.close()


def fetch_all(filter_text="", allowed_types=None, order_by="resource"):
    """allowed_types: None = no type filtering, [] = nothing matches, list = only those types
    (rows with empty/NULL res_type are always included so untyped entries don't vanish).
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    base = """
        SELECT id, res_type, resource, sector, system_name, planet, status, notes, logged_at
        FROM deposits
    """
    where = []
    params = []
    if filter_text:
        like = f"%{filter_text.lower()}%"
        where.append(
            """(lower(resource) LIKE ? OR lower(system_name) LIKE ?
               OR lower(planet) LIKE ? OR lower(notes) LIKE ?
               OR lower(COALESCE(res_type,'')) LIKE ? OR lower(COALESCE(sector,'')) LIKE ?)"""
        )
        params += [like, like, like, like, like, like]
    if allowed_types is not None:
        if len(allowed_types) == 0:
            conn.close()
            return []
        placeholders = ",".join("?" for _ in allowed_types)
        where.append(f"(COALESCE(res_type,'') = '' OR res_type IN ({placeholders}))")
        params += list(allowed_types)
    if where:
        base += " WHERE " + " AND ".join(where)
    if order_by == "location":
        base += (
            " ORDER BY sector COLLATE NOCASE, system_name COLLATE NOCASE,"
            " planet COLLATE NOCASE, res_type COLLATE NOCASE, resource COLLATE NOCASE"
        )
    else:
        base += (
            " ORDER BY res_type COLLATE NOCASE, resource COLLATE NOCASE,"
            " sector COLLATE NOCASE, system_name COLLATE NOCASE, planet COLLATE NOCASE"
        )
    c.execute(base, params)
    rows = c.fetchall()
    conn.close()
    return rows


def distinct_values(column):
    """Pull distinct values already in the DB to power autocomplete dropdowns.
    No hardcoded lists - this grows automatically as you log new entries."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        f"SELECT DISTINCT {column} FROM deposits"
        f" WHERE {column} IS NOT NULL AND {column} != ''"
        f" ORDER BY {column} COLLATE NOCASE"
    )
    vals = [r[0] for r in c.fetchall()]
    conn.close()
    return vals


def insert_row(
    res_type, resource, sector, system_name, planet, status, notes, logged_at
):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO deposits"
        " (res_type, resource, sector, system_name, planet, status, notes, logged_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (res_type, resource, sector, system_name, planet, status, notes, logged_at),
    )
    conn.commit()
    conn.close()


def update_row(
    row_id, res_type, resource, sector, system_name, planet, status, notes, logged_at
):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE deposits"
        " SET res_type=?, resource=?, sector=?, system_name=?, planet=?,"
        " status=?, notes=?, logged_at=? WHERE id=?",
        (
            res_type,
            resource,
            sector,
            system_name,
            planet,
            status,
            notes,
            logged_at,
            row_id,
        ),
    )
    conn.commit()
    conn.close()


def delete_row(row_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM deposits WHERE id=?", (row_id,))
    conn.commit()
    conn.close()


# ---------- Recipe DB ----------


def get_all_recipes():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, name FROM recipes ORDER BY name COLLATE NOCASE")
    rows = c.fetchall()
    conn.close()
    return rows  # [(id, name), ...]


def get_recipe_by_name(name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM recipes WHERE name = ?", (name,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def get_recipe_ingredients(recipe_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT ingredient_name, quantity FROM recipe_ingredients"
        " WHERE recipe_id=? ORDER BY id",
        (recipe_id,),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def distinct_ingredient_names():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT DISTINCT ingredient_name FROM recipe_ingredients"
        " ORDER BY ingredient_name COLLATE NOCASE"
    )
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_recipes_using_ingredient(ingredient_name):
    """Return (recipe_id, recipe_name, qty, output_name, output_qty) for every
    recipe that uses ingredient_name."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT r.id, r.name, ri.quantity,"
        " COALESCE(r.output_name, r.name), COALESCE(r.output_qty, 1)"
        " FROM recipe_ingredients ri"
        " JOIN recipes r ON r.id = ri.recipe_id"
        " WHERE ri.ingredient_name = ?"
        " ORDER BY r.name COLLATE NOCASE",
        (ingredient_name,),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def save_recipe(recipe_id, name, output_qty, ingredients, output_name=None):
    """Insert (recipe_id=None) or update a recipe, replacing its ingredients. Returns id."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    oname = output_name if output_name and output_name != name else None
    if recipe_id is None:
        c.execute(
            "INSERT INTO recipes (name, output_qty, output_name) VALUES (?, ?, ?)",
            (name, output_qty, oname),
        )
        recipe_id = c.lastrowid
    else:
        c.execute(
            "UPDATE recipes SET name=?, output_qty=?, output_name=? WHERE id=?",
            (name, output_qty, oname, recipe_id),
        )
        c.execute("DELETE FROM recipe_ingredients WHERE recipe_id=?", (recipe_id,))
    for ing_name, qty in ingredients:
        c.execute(
            "INSERT INTO recipe_ingredients (recipe_id, ingredient_name, quantity)"
            " VALUES (?, ?, ?)",
            (recipe_id, ing_name, qty),
        )
    conn.commit()
    conn.close()
    return recipe_id


def get_recipe_name(recipe_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT name FROM recipes WHERE id=?", (recipe_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else ""


def get_recipe_output_name(recipe_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT COALESCE(output_name, name) FROM recipes WHERE id=?", (recipe_id,)
    )
    row = c.fetchone()
    conn.close()
    return row[0] if row else ""


def get_all_output_names():
    """Distinct item names that recipes produce, for autocomplete."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT DISTINCT COALESCE(output_name, name) FROM recipes"
        " ORDER BY 1 COLLATE NOCASE"
    )
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_recipe_output_qty(recipe_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COALESCE(output_qty, 1) FROM recipes WHERE id=?", (recipe_id,))
    row = c.fetchone()
    conn.close()
    return float(row[0]) if row else 1.0


def delete_recipe(recipe_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM recipe_ingredients WHERE recipe_id=?", (recipe_id,))
    c.execute("DELETE FROM recipe_checked WHERE recipe_id=?", (recipe_id,))
    c.execute("DELETE FROM recipes WHERE id=?", (recipe_id,))
    conn.commit()
    conn.close()


def get_checked_paths(recipe_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT path_key FROM recipe_checked WHERE recipe_id=?", (recipe_id,))
    paths = {row[0] for row in c.fetchall()}
    conn.close()
    return paths


def toggle_checked(recipe_id, path_key, currently_checked):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if not currently_checked:
        c.execute(
            "INSERT OR REPLACE INTO recipe_checked (recipe_id, path_key) VALUES (?, ?)",
            (recipe_id, path_key),
        )
    else:
        c.execute(
            "DELETE FROM recipe_checked WHERE recipe_id=? AND path_key=?",
            (recipe_id, path_key),
        )
    conn.commit()
    conn.close()


def get_alt_prefs():
    """Return {ingredient_name: recipe_id} of user-chosen alternate recipes."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT ingredient_name, recipe_id FROM recipe_alt_prefs")
    prefs = dict(c.fetchall())
    conn.close()
    return prefs


def set_alt_pref(ingredient_name, recipe_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO recipe_alt_prefs (ingredient_name, recipe_id) VALUES (?, ?)",
        (ingredient_name, recipe_id),
    )
    conn.commit()
    conn.close()


def clear_alt_pref(ingredient_name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "DELETE FROM recipe_alt_prefs WHERE ingredient_name=?", (ingredient_name,)
    )
    conn.commit()
    conn.close()


def get_deposits_for_ingredient(resource_name):
    """Deposit locations for a resource, excluding Claimed."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT COALESCE(sector,''), system_name, planet, COALESCE(status,'')"
        " FROM deposits"
        " WHERE resource = ? AND COALESCE(status,'') != 'Claimed'"
        " ORDER BY sector COLLATE NOCASE, system_name COLLATE NOCASE, planet COLLATE NOCASE",
        (resource_name,),
    )
    rows = c.fetchall()
    conn.close()
    return rows


# ---------- Craft Queue DB ----------


def get_craft_queue():
    """Return [(queue_id, recipe_id, recipe_name, output_name, quantity), ...]."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT cq.id, cq.recipe_id, r.name,"
        " COALESCE(r.output_name, r.name), cq.quantity"
        " FROM craft_queue cq JOIN recipes r ON r.id = cq.recipe_id"
        " ORDER BY cq.id"
    )
    rows = c.fetchall()
    conn.close()
    return rows


def add_to_queue(recipe_id, quantity=1.0):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO craft_queue (recipe_id, quantity) VALUES (?, ?)",
        (recipe_id, quantity),
    )
    queue_id = c.lastrowid
    conn.commit()
    conn.close()
    return queue_id


def update_queue_qty(queue_id, quantity):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE craft_queue SET quantity=? WHERE id=?", (quantity, queue_id))
    conn.commit()
    conn.close()


def remove_from_queue(queue_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM queue_checked WHERE queue_id=?", (queue_id,))
    c.execute("DELETE FROM craft_queue WHERE id=?", (queue_id,))
    conn.commit()
    conn.close()


def get_queue_checked(queue_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT path_key FROM queue_checked WHERE queue_id=?", (queue_id,))
    paths = {row[0] for row in c.fetchall()}
    conn.close()
    return paths


def toggle_queue_checked(queue_id, path_key, currently_checked):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if not currently_checked:
        c.execute(
            "INSERT OR REPLACE INTO queue_checked (queue_id, path_key) VALUES (?, ?)",
            (queue_id, path_key),
        )
    else:
        c.execute(
            "DELETE FROM queue_checked WHERE queue_id=? AND path_key=?",
            (queue_id, path_key),
        )
    conn.commit()
    conn.close()


def clear_queue_checked(queue_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM queue_checked WHERE queue_id=?", (queue_id,))
    conn.commit()
    conn.close()


# ---------- Recipe tree resolution ----------


def _load_recipe_data():
    """Load all recipes and ingredients in two queries."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Order by id ASC so the first (oldest) recipe for each output_name is the default.
    c.execute(
        "SELECT id, name, COALESCE(output_name, name), COALESCE(output_qty, 1)"
        " FROM recipes ORDER BY id ASC"
    )
    recipe_map = {}  # output_name / recipe_name → first recipe_id
    output_map = {}  # recipe_id → output_qty
    output_name_by_id = {}  # recipe_id → what it produces (COALESCE(output_name, name))
    recipe_name_by_id = {}  # recipe_id → recipe's own name
    alts_by_output = {}  # output_name → [(rid, recipe_name, oqty), ...]
    for rid, rname, oname, oqty in c.fetchall():
        output_map[rid] = float(oqty)
        output_name_by_id[rid] = oname
        recipe_name_by_id[rid] = rname
        alts_by_output.setdefault(oname, []).append((rid, rname, float(oqty)))
        if oname not in recipe_map:  # first by id wins as default
            recipe_map[oname] = rid
        # Also index by recipe name so ingredients can reference alternates by name
        if rname not in recipe_map:
            recipe_map[rname] = rid
    c.execute(
        "SELECT recipe_id, ingredient_name, quantity FROM recipe_ingredients ORDER BY id"
    )
    ing_map: dict = {}
    for rid, ing_name, qty in c.fetchall():
        ing_map.setdefault(rid, []).append((ing_name, qty))
    conn.close()
    return (
        recipe_map,
        ing_map,
        output_map,
        output_name_by_id,
        alts_by_output,
        recipe_name_by_id,
    )


def resolve_recipe_tree(
    name,
    qty_needed=1.0,
    _visited=None,
    _recipe_map=None,
    _ing_map=None,
    _output_map=None,
    _root_recipe_id=None,
    _output_name_by_id=None,
    _alts_by_output=None,
    _recipe_name_by_id=None,
    _alt_prefs=None,
):
    """
    Recursively build a breakdown tree for `name`.
    Returns: {'name', 'qty', 'is_recipe', 'output_qty', 'recipe_name', 'children', 'alts'}
    'alts' lists every other recipe producing the same output — shown as collapsible branches.
    _root_recipe_id: forces a specific recipe at the top level (for alternate recipe views).
    _alt_prefs: {ingredient_name: recipe_id} of user-selected alternate recipes.
    """
    if _recipe_map is None or _ing_map is None or _output_map is None:
        (
            _recipe_map,
            _ing_map,
            _output_map,
            _output_name_by_id,
            _alts_by_output,
            _recipe_name_by_id,
        ) = _load_recipe_data()
    if _visited is None:
        _visited = frozenset()

    if _root_recipe_id is not None:
        recipe_id = _root_recipe_id
    elif _alt_prefs and name in _alt_prefs:
        recipe_id = _alt_prefs[name]
    else:
        recipe_id = _recipe_map.get(name)
    is_recipe = recipe_id is not None and name not in _visited

    children = []
    alts = []
    output_qty = 1.0
    used_recipe_name = name
    if is_recipe:
        output_qty = _output_map.get(recipe_id, 1.0)
        used_recipe_name = (_recipe_name_by_id or {}).get(recipe_id, name)
        crafts = math.ceil(qty_needed / output_qty)
        sub_visited = _visited | {name}
        for ing_name, ing_qty in _ing_map.get(recipe_id, []):
            child = resolve_recipe_tree(
                ing_name,
                crafts * ing_qty,
                sub_visited,
                _recipe_map,
                _ing_map,
                _output_map,
                _output_name_by_id=_output_name_by_id,
                _alts_by_output=_alts_by_output,
                _recipe_name_by_id=_recipe_name_by_id,
                _alt_prefs=_alt_prefs,
            )
            children.append(child)
        # Find every other recipe that produces the same output
        actual_output = (_output_name_by_id or {}).get(recipe_id, name)
        for alt_rid, alt_rname, alt_oqty in (_alts_by_output or {}).get(
            actual_output, []
        ):
            if alt_rid == recipe_id:
                continue
            alt_crafts = math.ceil(qty_needed / alt_oqty)
            alt_children = []
            for ing_name, ing_qty in _ing_map.get(alt_rid, []):
                alt_child = resolve_recipe_tree(
                    ing_name,
                    alt_crafts * ing_qty,
                    sub_visited,
                    _recipe_map,
                    _ing_map,
                    _output_map,
                    _output_name_by_id=_output_name_by_id,
                    _alts_by_output=_alts_by_output,
                    _recipe_name_by_id=_recipe_name_by_id,
                    _alt_prefs=_alt_prefs,
                )
                alt_children.append(alt_child)
            alts.append(
                {
                    "recipe_id": alt_rid,
                    "recipe_name": alt_rname,
                    "output_qty": alt_oqty,
                    "children": alt_children,
                }
            )

    return {
        "name": name,
        "qty": qty_needed,
        "is_recipe": is_recipe,
        "output_qty": output_qty,
        "recipe_name": used_recipe_name,
        "children": children,
        "alts": alts,
    }


# ---------- UI ----------

_NO_ARROW_STYLES: set[str] = set()


def _strip_arrow(layout):
    result = []
    for name, opts in layout:
        if "arrow" in name.lower():
            continue
        new_opts = dict(opts)
        if "children" in new_opts:
            new_opts["children"] = _strip_arrow(new_opts["children"])
        result.append((name, new_opts))
    return result


def _remove_combobox_arrow(box: ttk.Combobox) -> None:
    base = box.cget("style") or "TCombobox"
    patched = "_LiveDD." + base
    if patched not in _NO_ARROW_STYLES:
        style = ttk.Style(box)
        try:
            style.layout(patched, _strip_arrow(style.layout(base)))
            _NO_ARROW_STYLES.add(patched)
        except Exception:
            return
    box.configure(style=patched)


class _LiveDropdown:
    """
    Attaches a no-grab suggestion popup to any ttk.Combobox.
    Updates live while the user types; no input lock.
    pre_fn()           — called first to refresh box["values"] (e.g. cascade filter)
    on_select_fn(val)  — called after the user picks an item
    """

    def __init__(self, box: ttk.Combobox, pre_fn=None, on_select_fn=None):
        self._box = box
        self._pre_fn = pre_fn
        self._on_select = on_select_fn
        self._win: tk.Toplevel | None = None
        self._lb: tk.Listbox | None = None
        self._padding_applied = False

        box.bind("<KeyRelease>", self._on_key, add=True)
        box.bind("<FocusOut>", lambda _e: box.after(150, self._maybe_hide), add=True)
        box.bind("<Escape>", lambda _e: self.hide(), add=True)
        box.bind("<Down>", self._on_down, add=True)
        box.bind("<Return>", self._on_return, add=True)
        box.bind("<Configure>", self._reposition_arrow, add=True)
        box.bind("<Destroy>", lambda _: self._arrow_btn.destroy(), add=True)
        _remove_combobox_arrow(box)
        self._arrow_btn = tk.Button(
            box.master,
            text="▾",
            command=self._on_arrow_click,
            bg="#21262d",
            fg="#8b949e",
            activebackground="#30363d",
            activeforeground="#c9d1d9",
            relief="flat",
            bd=0,
            font=("Segoe UI", 7),
            cursor="arrow",
            takefocus=False,
        )
        box.after(1, self._reposition_arrow)

    def _reposition_arrow(self, _=None):
        b = self._box
        b.update_idletasks()
        btn_w = max(b.winfo_height(), 18)
        self._arrow_btn.place(
            in_=b.master,
            x=b.winfo_x() + b.winfo_width() - btn_w,
            y=b.winfo_y(),
            width=btn_w,
            height=b.winfo_height(),
        )
        self._arrow_btn.lift()
        if not self._padding_applied:
            self._padding_applied = True
            ttk.Style(b).configure(b.cget("style"), padding=[0, 0, btn_w, 0])

    def _on_arrow_click(self):
        if self._win and self._win.winfo_exists() and self._win.winfo_ismapped():
            self.hide()
        else:
            if self._pre_fn:
                self._pre_fn()
            self._box.after(0, self._refresh)
        self._box.focus_set()

    def _on_key(self, event):
        if event.keysym in _AUTOCOMPLETE_SKIP:
            return
        if self._pre_fn:
            self._pre_fn()
        self._box.after(60, self._refresh)

    def _refresh(self):
        typed = self._box.get()
        vals = list(self._box["values"])
        shown = [v for v in vals if typed.lower() in v.lower()] if typed else vals
        if shown:
            self._show(shown)
        else:
            self.hide()

    def _show(self, values):
        if self._win is None or not self._win.winfo_exists():
            self._build()
        lb = self._lb
        lb.delete(0, "end")  # type: ignore[union-attr]
        for v in values:
            lb.insert("end", v)  # type: ignore[union-attr]
        self._reposition()
        self._win.deiconify()  # type: ignore[union-attr]
        self._win.lift()  # type: ignore[union-attr]

    def _build(self):
        self._win = tk.Toplevel(self._box)
        self._win.withdraw()  # hide until repositioned to avoid flash at (0,0)
        self._win.overrideredirect(True)
        self._win.attributes("-topmost", True)
        frm = tk.Frame(self._win, bg="#21262d", bd=1, relief="solid")
        frm.pack(fill="both", expand=True)
        vsb = ttk.Scrollbar(frm, orient="vertical", style="Thin.Vertical.TScrollbar")
        self._lb = tk.Listbox(
            frm,
            bg="#161b22",
            fg="#c9d1d9",
            selectbackground="#1f6feb",
            selectforeground="white",
            activestyle="none",
            relief="flat",
            bd=0,
            font=("Segoe UI", 9),
            height=8,
        )
        self._lb.pack(side="left", fill="both", expand=True)
        vsb.config(command=self._lb.yview)
        lb = self._lb
        def _yscroll(first, last):
            if float(first) <= 0.0 and float(last) >= 1.0:
                vsb.pack_forget()
            else:
                vsb.pack(side="right", fill="y", before=lb)
            vsb.set(first, last)
        lb.configure(yscrollcommand=_yscroll)
        self._lb.bind("<ButtonRelease-1>", self._on_lb_click)
        self._lb.bind("<Return>", lambda _e: self._lb_pick())
        self._lb.bind("<Escape>", lambda _e: self.hide())
        self._lb.bind("<KeyPress>", self._lb_keypress)

    def _reposition(self):
        b = self._box
        b.update_idletasks()
        x, y = b.winfo_rootx(), b.winfo_rooty() + b.winfo_height()
        w = max(b.winfo_width(), 120)
        h = min(8, self._lb.size()) * 20 + 4  # type: ignore[union-attr]
        self._win.geometry(f"{w}x{h}+{x}+{y}")  # type: ignore[union-attr]

    def _on_lb_click(self, event):
        idx = self._lb.nearest(event.y)  # type: ignore[union-attr]
        self._select(self._lb.get(idx))  # type: ignore[union-attr]

    def _lb_pick(self):
        sel = self._lb.curselection()  # type: ignore[union-attr]
        if sel:
            self._select(self._lb.get(sel[0]))  # type: ignore[union-attr]

    def _lb_keypress(self, event):
        if event.keysym in ("Return", "KP_Enter"):
            self._lb_pick()
        elif event.keysym == "Escape":
            self.hide()
        elif len(event.char) == 1 and event.char.isprintable():
            self._box.focus_set()
            self._box.insert("end", event.char)
            if self._pre_fn:
                self._pre_fn()
            self._box.after(60, self._refresh)

    def _select(self, value):
        self._box.set(value)
        self.hide()
        self._box.focus_set()
        self._box.icursor("end")
        self._box.selection_range(0, "end")
        if self._on_select:
            self._on_select(value)

    def _on_down(self, _event):
        if self._win and self._win.winfo_viewable() and self._lb:
            self._lb.focus_set()
            if not self._lb.curselection():
                self._lb.selection_set(0)
                self._lb.activate(0)
            return "break"

    def _on_return(self, _event):
        if self._win and self._win.winfo_viewable() and self._lb:
            self._lb_pick()

    def _maybe_hide(self):
        f = self._box.focus_get()
        if f is not self._box and f is not self._lb:
            self.hide()

    def hide(self):
        if self._win and self._win.winfo_exists():
            self._win.withdraw()


def _autohide_yscroll(sb):
    """yscrollcommand callback that hides the scrollbar when all content is visible."""
    def _cmd(first, last):
        if float(first) <= 0.0 and float(last) >= 1.0:
            sb.grid_remove()
        else:
            sb.grid()
        sb.set(first, last)
    return _cmd


class CraftQueuePanel:
    """
    Pinnable always-on-top floating window for the persistent crafting queue.
    Queue mode: scrollable job list + breakdown of the selected job.
    Totals mode: job list + aggregated raw materials across all queued jobs.
    Pin button keeps the panel visible when the main overlay is toggled.

    queue_id=0 is a sentinel for aggregate checked state in the Totals view
    (SQLite FK constraints are not enforced here, so this is safe).
    """

    _TOTALS_QID = 0  # sentinel queue_id for totals-view checkbox persistence

    def __init__(self, master, overlay):
        self._overlay = overlay
        self._pinned = False
        self._mode = "queue"
        self._selected_job = None  # (queue_id, recipe_id, output_name, qty)
        self._job_frames: dict = {}
        self._bd_iid_info: dict = {}
        self._bd_toggled = False
        self._drag_x = self._drag_y = 0
        self._resize_x = self._resize_y = 0
        self._resize_w = self._resize_h = 0
        self.on_hide_cb = lambda: None

        cfg = load_config()
        self._pinned = bool(cfg.get("queue_pinned", False))

        self._win = tk.Toplevel(master)
        self._win.title("Craft Queue")
        self._win.configure(bg="#0d1117")
        self._win.attributes("-topmost", True)
        self._win.overrideredirect(True)
        self._win.attributes("-alpha", 0.94)

        x = cfg.get("queue_x", 400)
        y = cfg.get("queue_y", 60)
        w = cfg.get("queue_w", 320)
        h = cfg.get("queue_h", 500)
        self._split_h = cfg.get("queue_split", 120)
        self._win.geometry(f"{w}x{h}+{x}+{y}")

        self._build_ui()
        self._refresh_job_list()
        self._enable_composited()
        self._win.bind("<Escape>", lambda _e: self.hide())

    def _build_ui(self):
        # --- drag bar ---
        drag = tk.Frame(self._win, bg="#161b22", height=28)
        drag.pack(fill="x")

        tk.Label(
            drag,
            text="⠿  Craft Queue",
            bg="#161b22",
            fg="#c9d1d9",
            font=("Segoe UI", 9),
        ).pack(side="left", padx=8)

        tk.Button(
            drag,
            text="✕",
            bg="#161b22",
            fg="#c9d1d9",
            bd=0,
            command=self.hide,
            font=("Segoe UI", 9),
        ).pack(side="right", padx=4)

        self._pin_btn = tk.Button(
            drag,
            text="📌",
            bg="#161b22",
            fg="#f0883e" if self._pinned else "#6e7681",
            bd=0,
            command=self._toggle_pin,
            font=("Segoe UI", 9),
        )
        self._pin_btn.pack(side="right", padx=2)

        self._btn_totals = tk.Button(
            drag,
            text="Totals",
            bg="#1f6feb" if self._mode == "totals" else "#21262d",
            fg="white" if self._mode == "totals" else "#8b949e",
            relief="flat",
            bd=0,
            padx=6,
            font=("Segoe UI", 8),
            command=lambda: self._set_mode("totals"),
        )
        self._btn_totals.pack(side="right", padx=(0, 2))

        self._btn_queue = tk.Button(
            drag,
            text="Queue",
            bg="#1f6feb" if self._mode == "queue" else "#21262d",
            fg="white" if self._mode == "queue" else "#8b949e",
            relief="flat",
            bd=0,
            padx=6,
            font=("Segoe UI", 8),
            command=lambda: self._set_mode("queue"),
        )
        self._btn_queue.pack(side="right", padx=(0, 2))

        drag.bind("<ButtonPress-1>", self._start_move)
        drag.bind("<B1-Motion>", self._do_move)
        drag.bind("<ButtonRelease-1>", lambda _e: self._save_pos())

        # Bottom items must be packed before the expanding PanedWindow so the
        # pack manager reserves their space before distributing the remainder.
        self._build_resize_grip()

        # --- add-job row ---
        add_row = tk.Frame(self._win, bg="#0d1117")
        add_row.pack(side="bottom", fill="x", padx=6, pady=(4, 6))

        self._add_recipe_var = tk.StringVar()
        self._add_recipe_cb = ttk.Combobox(
            add_row, textvariable=self._add_recipe_var, width=20
        )
        self._add_recipe_cb.pack(side="left", padx=(0, 4))
        self._add_recipe_cb.configure(values=[n for _, n in get_all_recipes()])
        self._add_recipe_cb.bind(
            "<FocusIn>",
            lambda _e: self._add_recipe_cb.configure(
                values=[n for _, n in get_all_recipes()]
            ),
        )
        _LiveDropdown(
            self._add_recipe_cb,
            pre_fn=lambda: self._add_recipe_cb.configure(
                values=[n for _, n in get_all_recipes()]
            ),
        )

        self._add_qty_var = tk.StringVar(value="1")
        tk.Entry(
            add_row,
            textvariable=self._add_qty_var,
            width=4,
            bg="#161b22",
            fg="#c9d1d9",
            insertbackground="#c9d1d9",
            relief="flat",
            justify="center",
        ).pack(side="left", padx=(0, 4), ipady=2)

        tk.Button(
            add_row,
            text="+ Add",
            command=self._add_job,
            bg="#238636",
            fg="white",
            relief="flat",
            padx=6,
            font=("Segoe UI", 8),
        ).pack(side="left")

        tk.Button(
            add_row,
            text="Clear done",
            command=self._clear_all_done,
            bg="#21262d",
            fg="#c9d1d9",
            relief="flat",
            padx=6,
            font=("Segoe UI", 8),
        ).pack(side="right")

        # --- PanedWindow: job list (top pane) + breakdown tree (bottom pane) ---
        self._pw = tk.PanedWindow(
            self._win, orient=tk.VERTICAL,
            bg="#21262d", sashwidth=5, sashrelief="flat",
            sashpad=0, handlesize=0, bd=0,
        )
        self._pw.pack(fill="both", expand=True, padx=6, pady=(6, 0))
        self._pw.bind("<ButtonRelease-1>", self._save_split)

        # Top pane: scrollable job list
        job_frame = tk.Frame(self._pw, bg="#0d1117")
        self._pw.add(job_frame, height=self._split_h, minsize=40, stretch="never")

        job_frame.grid_rowconfigure(0, weight=1)
        job_frame.grid_columnconfigure(0, weight=1)
        self._job_canvas = tk.Canvas(job_frame, bg="#0d1117", highlightthickness=0)
        self._job_canvas.grid(row=0, column=0, sticky="nsew")
        jvsb = ttk.Scrollbar(job_frame, orient="vertical", style="Thin.Vertical.TScrollbar",
                              command=self._job_canvas.yview)
        jvsb.grid(row=0, column=1, sticky="ns")
        self._job_canvas.configure(yscrollcommand=_autohide_yscroll(jvsb))
        self._job_inner = tk.Frame(self._job_canvas, bg="#0d1117")
        _jwin = self._job_canvas.create_window((0, 0), window=self._job_inner, anchor="nw")
        self._job_inner.bind(
            "<Configure>",
            lambda _e: self._job_canvas.configure(
                scrollregion=self._job_canvas.bbox("all") or (0, 0, 0, 0)
            ),
        )
        self._job_canvas.bind(
            "<Configure>", lambda e: self._job_canvas.itemconfig(_jwin, width=e.width)
        )
        self._job_canvas.bind("<MouseWheel>", self._job_scroll)
        self._job_inner.bind("<MouseWheel>", self._job_scroll)

        # Bottom pane: breakdown / totals tree
        bd_frame = tk.Frame(self._pw, bg="#0d1117")
        self._pw.add(bd_frame, minsize=60, stretch="always")

        bd_frame.grid_rowconfigure(0, weight=1)
        bd_frame.grid_columnconfigure(0, weight=1)
        self._bd_tree = ttk.Treeview(bd_frame, show="tree")
        self._bd_tree.grid(row=0, column=0, sticky="nsew")
        bd_vsb = ttk.Scrollbar(bd_frame, orient="vertical", style="Thin.Vertical.TScrollbar",
                                command=self._bd_tree.yview)
        bd_vsb.grid(row=0, column=1, sticky="ns")
        self._bd_tree.configure(yscrollcommand=_autohide_yscroll(bd_vsb))
        self._bd_tree.bind("<ButtonRelease-1>", self._on_bd_click)
        self._bd_tree.bind(
            "<<TreeviewOpen>>", lambda _e: setattr(self, "_bd_toggled", True)
        )
        self._bd_tree.bind(
            "<<TreeviewClose>>", lambda _e: setattr(self, "_bd_toggled", True)
        )

    def _job_scroll(self, event):
        if self._job_inner.winfo_reqheight() > self._job_canvas.winfo_height():
            self._job_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    # --- drag / position ---

    def _start_move(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _do_move(self, _event):
        x = self._win.winfo_pointerx() - self._drag_x
        y = self._win.winfo_pointery() - self._drag_y
        self._win.geometry(f"+{x}+{y}")

    def _save_pos(self):
        cfg: dict = load_config()
        cfg["queue_x"] = self._win.winfo_x()
        cfg["queue_y"] = self._win.winfo_y()
        cfg["queue_w"] = self._win.winfo_width()
        cfg["queue_h"] = self._win.winfo_height()
        try:
            cfg["queue_split"] = self._pw.sash_coord(0)[1]
        except Exception:
            pass
        save_config(cfg)

    def _save_split(self, _):
        self._save_pos()

    def _build_resize_grip(self):
        btm = tk.Frame(self._win, bg="#0d1117")
        btm.pack(side="bottom", fill="x")
        grip = tk.Label(btm, text="◢", bg="#0d1117", fg="#3b434d",
                        font=("Segoe UI", 7), cursor="size_nw_se")
        grip.pack(side="right", padx=2, pady=1)
        grip.bind("<ButtonPress-1>", self._start_resize)
        grip.bind("<B1-Motion>", self._do_resize)
        grip.bind("<ButtonRelease-1>", self._end_resize)

    def _enable_composited(self):
        if sys.platform != "win32":
            return
        self._win.update_idletasks()
        hwnd = self._win.winfo_id()
        style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
        ctypes.windll.user32.SetWindowLongW(hwnd, -20, style | 0x02000000)

    def _start_resize(self, event):
        self._resize_x = event.x_root
        self._resize_y = event.y_root
        self._resize_w = self._win.winfo_width()
        self._resize_h = self._win.winfo_height()

    def _do_resize(self, event):
        dw = event.x_root - self._resize_x
        dh = event.y_root - self._resize_y
        new_w = max(320, self._resize_w + dw)
        new_h = max(380, self._resize_h + dh)
        self._win.geometry(f"{new_w}x{new_h}+{self._win.winfo_x()}+{self._win.winfo_y()}")
        self._win.update_idletasks()
        if sys.platform == "win32":
            ctypes.windll.user32.RedrawWindow(self._win.winfo_id(), None, None, 0x0185)

    def _end_resize(self, _):
        self._save_pos()

    # --- pin / mode ---

    def _toggle_pin(self):
        self._pinned = not self._pinned
        self._pin_btn.config(fg="#f0883e" if self._pinned else "#6e7681")
        cfg: dict = load_config()
        cfg["queue_pinned"] = self._pinned
        save_config(cfg)
        if not self._pinned and self._overlay.state() == "withdrawn":
            self.hide()

    def _set_mode(self, mode):
        self._mode = mode
        self._btn_queue.config(
            bg="#1f6feb" if mode == "queue" else "#21262d",
            fg="white" if mode == "queue" else "#8b949e",
        )
        self._btn_totals.config(
            bg="#1f6feb" if mode == "totals" else "#21262d",
            fg="white" if mode == "totals" else "#8b949e",
        )
        self._refresh_breakdown()

    # --- job list ---

    def _refresh_job_list(self):
        for w in self._job_inner.winfo_children():
            w.destroy()
        self._job_frames = {}
        jobs = get_craft_queue()
        if not jobs:
            tk.Label(
                self._job_inner,
                text="No jobs — add one below.",
                bg="#0d1117",
                fg="#6e7681",
                font=("Segoe UI", 8),
            ).pack(anchor="w", padx=4, pady=4)
        for queue_id, recipe_id, _, output_name, qty in jobs:
            self._build_job_row(queue_id, recipe_id, output_name, qty)
        self._job_canvas.configure(
            scrollregion=self._job_canvas.bbox("all") or (0, 0, 0, 0)
        )

    def _build_job_row(self, queue_id, recipe_id, output_name, qty):
        is_sel = self._selected_job is not None and self._selected_job[0] == queue_id
        bg = "#1f6feb" if is_sel else "#161b22"
        row = tk.Frame(self._job_inner, bg=bg, cursor="hand2")
        row.pack(fill="x", pady=1)
        self._job_frames[queue_id] = row

        lbl = tk.Label(
            row,
            text=output_name,
            bg=bg,
            fg="white" if is_sel else "#c9d1d9",
            font=("Segoe UI", 8),
            anchor="w",
        )
        lbl.pack(side="left", padx=(6, 2), pady=3, fill="x", expand=True)

        rm_btn = tk.Button(
            row,
            text="×",
            bg=bg,
            fg="#ffaaaa" if is_sel else "#da3633",
            relief="flat",
            bd=0,
            font=("Segoe UI", 9),
            command=lambda qid=queue_id: self._remove_job(qid),
        )
        rm_btn.pack(side="right", padx=4)
        rm_btn.bind("<MouseWheel>", self._job_scroll, add=True)

        qty_var = tk.StringVar(value=f"{qty:g}")
        qty_e = tk.Entry(
            row,
            textvariable=qty_var,
            width=4,
            bg="#21262d",
            fg="#c9d1d9",
            insertbackground="#c9d1d9",
            relief="flat",
            justify="center",
            font=("Segoe UI", 8),
        )
        qty_e.pack(side="right", padx=2, ipady=1)
        qty_e.bind(
            "<Return>", lambda _e, qid=queue_id, v=qty_var: self._update_qty(qid, v)
        )
        qty_e.bind(
            "<FocusOut>", lambda _e, qid=queue_id, v=qty_var: self._update_qty(qid, v)
        )

        def _on_click(_ev, qid=queue_id, rid=recipe_id, oname=output_name, qv=qty_var):
            self._select_job(qid, rid, oname, qv)

        for w in (row, lbl):
            w.bind("<ButtonPress-1>", _on_click)
            w.bind("<MouseWheel>", self._job_scroll, add=True)
        qty_e.bind("<MouseWheel>", self._job_scroll, add=True)

    def _select_job(self, queue_id, recipe_id, output_name, qty_var):
        try:
            qty = max(float(qty_var.get()), 0.001)
        except ValueError:
            qty = 1.0
        self._selected_job = (queue_id, recipe_id, output_name, qty)
        self._refresh_job_list()
        if self._mode == "queue":
            self._refresh_breakdown()

    def _update_qty(self, queue_id, qty_var):
        try:
            qty = float(qty_var.get())
            if qty <= 0:
                raise ValueError
        except ValueError:
            return
        update_queue_qty(queue_id, qty)
        if self._selected_job and self._selected_job[0] == queue_id:
            old = self._selected_job
            self._selected_job = (old[0], old[1], old[2], qty)
            self._refresh_breakdown()

    def _remove_job(self, queue_id):
        remove_from_queue(queue_id)
        if self._selected_job and self._selected_job[0] == queue_id:
            self._selected_job = None
        self._refresh_job_list()
        self._refresh_breakdown()

    def _add_job(self):
        name = self._add_recipe_var.get().strip()
        if not name:
            return
        recipe_id = get_recipe_by_name(name)
        if recipe_id is None:
            return
        try:
            qty = max(float(self._add_qty_var.get()), 0.001)
        except ValueError:
            qty = 1.0
        add_to_queue(recipe_id, qty)
        self._add_recipe_var.set("")
        self._add_qty_var.set("1")
        self._refresh_job_list()
        if self._mode == "totals":
            self._refresh_breakdown()

    def _clear_all_done(self):
        for queue_id, *_ in get_craft_queue():
            clear_queue_checked(queue_id)
        clear_queue_checked(self._TOTALS_QID)
        self._refresh_breakdown()

    # --- breakdown / totals tree ---

    def _refresh_breakdown(self):
        tree = self._bd_tree
        for item in tree.get_children():
            tree.delete(item)
        self._bd_iid_info = {}
        tree.tag_configure("root", foreground="#f0883e", font=("Segoe UI", 9, "bold"))
        tree.tag_configure(
            "section", foreground="#8b949e", font=("Segoe UI", 8, "italic")
        )
        tree.tag_configure("ingredient", foreground="#c9d1d9")
        tree.tag_configure("done", foreground="#6e7681")
        tree.tag_configure("location", foreground="#3fb950", font=("Segoe UI", 8))
        tree.tag_configure(
            "alt_header", foreground="#8b949e", font=("Segoe UI", 8, "italic")
        )
        if self._mode == "totals":
            self._render_totals(tree)
        else:
            self._render_breakdown(tree)

    def _render_breakdown(self, tree):
        if self._selected_job is None:
            tree.insert(
                "",
                "end",
                text="← Select a job above to see its breakdown.",
                tags=("section",),
            )
            return
        queue_id, recipe_id, output_name, qty = self._selected_job
        alt_prefs = get_alt_prefs()
        node = resolve_recipe_tree(
            output_name, qty_needed=qty, _root_recipe_id=recipe_id, _alt_prefs=alt_prefs
        )
        checked = get_queue_checked(queue_id)
        oqty = node.get("output_qty", 1.0)
        crafts = math.ceil(qty / oqty)
        root_label = f"◆  {output_name}  ×{qty:g}"
        if crafts > 1 or oqty > 1:
            root_label += f"  ({crafts:g} crafts)"
        root_iid = tree.insert("", "end", text=root_label, open=True, tags=("root",))
        self._bd_iid_info[root_iid] = {"type": "root"}
        for child in node["children"]:
            self._insert_node(tree, root_iid, child, queue_id, [], checked)

    def _render_totals(self, tree):
        jobs = get_craft_queue()
        if not jobs:
            tree.insert("", "end", text="Queue is empty.", tags=("section",))
            return
        alt_prefs = get_alt_prefs()
        all_raw: dict = {}
        all_crafted: dict = {}
        for _qid, recipe_id, _rname, output_name, qty in jobs:
            node = resolve_recipe_tree(
                output_name,
                qty_needed=qty,
                _root_recipe_id=recipe_id,
                _alt_prefs=alt_prefs,
            )
            for iname, raw_qty in Overlay.collect_totals(node).items():
                all_raw[iname] = all_raw.get(iname, 0) + raw_qty
            for iname, info in Overlay.collect_intermediates(node).items():
                if iname not in all_crafted:
                    all_crafted[iname] = {"qty": 0.0, "output_qty": info["output_qty"]}
                all_crafted[iname]["qty"] += info["qty"]

        checked = get_queue_checked(self._TOTALS_QID)
        header = tree.insert(
            "", "end", text=f"◆  All Jobs  ({len(jobs)})", open=True, tags=("root",)
        )
        self._bd_iid_info[header] = {"type": "root"}

        if all_crafted:
            craft_hdr = tree.insert(
                header, "end", text="── Crafted ──", open=True, tags=("section",)
            )
            self._bd_iid_info[craft_hdr] = {"type": "root"}
            for iname, info in sorted(all_crafted.items(), key=lambda x: x[0].lower()):
                qty = info["qty"]
                oq = info["output_qty"]
                crafts = math.ceil(qty / oq)
                path_key = f"__craft__|{iname}"
                is_done = path_key in checked
                img = (
                    self._overlay.img_checked
                    if is_done
                    else self._overlay.img_unchecked
                )
                suffix = f"  ({crafts:g} crafts)" if oq > 1 else ""
                iid = tree.insert(
                    craft_hdr,
                    "end",
                    text=f"{qty:g}×  {iname}{suffix}",
                    image=img,
                    open=True,
                    tags=("done" if is_done else "ingredient",),
                )
                self._bd_iid_info[iid] = {
                    "type": "ingredient",
                    "queue_id": self._TOTALS_QID,
                    "path_key": path_key,
                    "checked": is_done,
                }

        raw_hdr = tree.insert(
            header, "end", text="── Raw Materials ──", open=True, tags=("section",)
        )
        self._bd_iid_info[raw_hdr] = {"type": "root"}
        for iname, qty in sorted(all_raw.items(), key=lambda x: x[0].lower()):
            path_key = f"__total__|{iname}"
            is_done = path_key in checked
            img = self._overlay.img_checked if is_done else self._overlay.img_unchecked
            iid = tree.insert(
                raw_hdr,
                "end",
                text=f"{qty:g}×  {iname}",
                image=img,
                open=True,
                tags=("done" if is_done else "ingredient",),
            )
            self._bd_iid_info[iid] = {
                "type": "ingredient",
                "queue_id": self._TOTALS_QID,
                "path_key": path_key,
                "checked": is_done,
            }
            for sector, system_name, planet, status in get_deposits_for_ingredient(
                iname
            ):
                parts = [p for p in (sector, system_name, planet) if p]
                loc_text = " / ".join(parts)
                if status and status not in ("Unknown", ""):
                    loc_text += f"  [{status}]"
                loc_iid = tree.insert(
                    iid, "end", text=f"    📍 {loc_text}", tags=("location",)
                )
                self._bd_iid_info[loc_iid] = {"type": "location"}

    def _insert_node(self, tree, parent_iid, node, queue_id, path_parts, checked, depth=0):
        name = node["name"]
        qty = node["qty"]
        used_recipe = node.get("recipe_name", name)
        path_key = "|".join(path_parts + [name])
        is_done = path_key in checked
        label = f"{qty:g}×  {name}"
        if used_recipe and used_recipe != name:
            label += f"  [{used_recipe}]"
        img = self._overlay.img_checked if is_done else self._overlay.img_unchecked
        iid = tree.insert(
            parent_iid,
            "end",
            text=label,
            image=img,
            open=False,
            tags=("done" if is_done else "ingredient",),
        )
        self._bd_iid_info[iid] = {
            "type": "ingredient",
            "queue_id": queue_id,
            "path_key": path_key,
            "checked": is_done,
        }
        if node["children"]:
            for child in node["children"]:
                self._insert_node(
                    tree, iid, child, queue_id, path_parts + [name], checked, depth + 1
                )
        elif not node["is_recipe"]:
            for sector, system_name, planet, status in get_deposits_for_ingredient(
                name
            ):
                parts = [p for p in (sector, system_name, planet) if p]
                loc_text = " / ".join(parts)
                if status and status not in ("Unknown", ""):
                    loc_text += f"  [{status}]"
                loc_iid = tree.insert(
                    iid, "end", text=f"    📍 {loc_text}", tags=("location",)
                )
                self._bd_iid_info[loc_iid] = {"type": "location"}
        for alt in node.get("alts", []):
            alt_iid = tree.insert(
                iid,
                "end",
                text=f"⟳  {alt['recipe_name']}  (alt — click to use)",
                open=False,
                tags=("alt_header",),
            )
            self._bd_iid_info[alt_iid] = {
                "type": "alt_header",
                "ingredient_name": name,
                "alt_recipe_id": alt["recipe_id"],
            }
            for alt_child in alt["children"]:
                self._insert_node(
                    tree,
                    alt_iid,
                    alt_child,
                    queue_id,
                    path_parts + [f"~{alt['recipe_id']}~{name}"],
                    checked,
                )

    def _on_bd_click(self, event):
        if self._bd_toggled:
            self._bd_toggled = False
            return
        tree = self._bd_tree
        iid = tree.identify_row(event.y)
        if not iid:
            return
        info = self._bd_iid_info.get(iid)
        if not info:
            return
        if info["type"] == "alt_header":
            set_alt_pref(info["ingredient_name"], info["alt_recipe_id"])
            self._refresh_breakdown()
            return
        if info["type"] == "ingredient":
            if "image" not in tree.identify("element", event.x, event.y):
                return
            queue_id = info["queue_id"]
            path_key = info["path_key"]
            is_done = info.get("checked", False)
            toggle_queue_checked(queue_id, path_key, currently_checked=is_done)
            new_done = not is_done
            info["checked"] = new_done
            tree.item(
                iid,
                image=(
                    self._overlay.img_checked
                    if new_done
                    else self._overlay.img_unchecked
                ),
                tags=("done" if new_done else "ingredient",),
            )

    # --- show / hide / pin ---

    def show(self):
        self._win.deiconify()
        self._win.attributes("-topmost", True)

    def hide(self):
        self._win.withdraw()
        self.on_hide_cb()

    def is_visible(self):
        return self._win.state() != "withdrawn"

    def toggle(self):
        if self.is_visible():
            self.hide()
        else:
            self.show()

    @property
    def pinned(self):
        return self._pinned

    def add_job(self, recipe_id, qty=1.0):
        """Add a job externally (e.g. right-click from recipe panel)."""
        add_to_queue(recipe_id, qty)
        self._refresh_job_list()
        if self._mode == "totals":
            self._refresh_breakdown()


class Overlay(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CraftMap Resources")
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.94)
        self.configure(bg="#0d1117")
        self.overrideredirect(True)  # no native title bar -> cleaner overlay
        self.selected_id = None
        self.type_filter_vars = {}  # res_type -> tk.BooleanVar, rebuilt dynamically
        self._hotkey_handle = None
        self._drag_x = 0
        self._drag_y = 0
        self._resize_x = 0
        self._resize_y = 0
        self._resize_w = 0
        self._resize_h = 0
        self._resize_min_w = 640
        self._resize_min_h = 300
        self._search_entry: tk.Entry | None = None
        self._user_sized: bool = False

        cfg = load_config()
        self.toggle_key = cfg.get("toggle_key", "F1")
        self._view_mode: str = cfg.get("view_mode", "resource")
        self._collapsed: set = set(cfg.get("collapsed_nodes", []))
        self._iid_to_key: dict = {}
        self._recipe_selected_id: int | None = None
        self._viewing_recipe_id: int | None = None
        self._recipe_split: int = int(cfg.get("recipe_split", 200))
        self._ing_rows: list = []
        self._recipe_iid_info: dict = {}
        self._bd_toggled: bool = False
        self._queue_panel: "CraftQueuePanel | None" = None
        self.tray_icon: object = None
        self.img_unchecked: tk.PhotoImage
        self.img_checked: tk.PhotoImage
        self._recipe_breakdown_mode: str = "breakdown"
        self._usedin_recipe_id: "int | None" = None
        self._usedin_navigated_away: bool = False

        _x, _y = cfg.get("window_x", 60), cfg.get("window_y", 60)
        _w, _h = cfg.get("window_w"), cfg.get("window_h")
        if _w and _h:
            self.geometry(f"{_w}x{_h}+{_x}+{_y}")
            self._user_sized = True
        else:
            self.geometry(f"+{_x}+{_y}")

        self._build_drag_bar()
        self._build_search()
        self._build_type_filter()
        self._build_tree()
        self._build_form()
        self._build_recipe_panel()
        self._build_resize_grip()

        self._apply_view_visibility()
        self.refresh()
        self.auto_size()
        self._enable_composited()

        self.bind("<Escape>", lambda _e: self.withdraw())

    # ----- drag handling (since title bar is removed) -----
    def _build_drag_bar(self):
        drag_bar = tk.Frame(self, bg="#161b22", height=28)
        drag_bar.pack(fill="x", side="top")

        self._title_label = tk.Label(
            drag_bar,
            text=self._title_text(),
            bg="#161b22",
            fg="#c9d1d9",
            font=("Segoe UI", 9),
        )
        self._title_label.pack(side="left", padx=8)

        close_btn = tk.Button(
            drag_bar,
            text="✕",
            bg="#161b22",
            fg="#c9d1d9",
            bd=0,
            command=self.quit_app,
            font=("Segoe UI", 9),
        )
        close_btn.pack(side="right", padx=4)

        settings_btn = tk.Button(
            drag_bar,
            text="⚙",
            bg="#161b22",
            fg="#8b949e",
            bd=0,
            command=self._open_hotkey_settings,
            font=("Segoe UI", 9),
        )
        settings_btn.pack(side="right", padx=2)

        self._btn_queue_panel = tk.Button(
            drag_bar,
            text="Queue",
            bg="#21262d",
            fg="#8b949e",
            bd=0,
            relief="flat",
            padx=6,
            font=("Segoe UI", 8),
            command=self.toggle_queue_panel,
        )
        self._btn_queue_panel.pack(side="right", padx=(0, 2))

        self._btn_recipe = tk.Button(
            drag_bar,
            text="Recipe",
            bg="#1f6feb" if self._view_mode == "recipe" else "#21262d",
            fg="white" if self._view_mode == "recipe" else "#8b949e",
            bd=0,
            relief="flat",
            padx=6,
            font=("Segoe UI", 8),
            command=lambda: self._set_view("recipe"),
        )
        self._btn_recipe.pack(side="right", padx=(0, 2))

        self._btn_location = tk.Button(
            drag_bar,
            text="Location",
            bg="#1f6feb" if self._view_mode == "location" else "#21262d",
            fg="white" if self._view_mode == "location" else "#8b949e",
            bd=0,
            relief="flat",
            padx=6,
            font=("Segoe UI", 8),
            command=lambda: self._set_view("location"),
        )
        self._btn_location.pack(side="right", padx=(0, 2))

        self._btn_resource = tk.Button(
            drag_bar,
            text="Resource",
            bg="#1f6feb" if self._view_mode == "resource" else "#21262d",
            fg="white" if self._view_mode == "resource" else "#8b949e",
            bd=0,
            relief="flat",
            padx=6,
            font=("Segoe UI", 8),
            command=lambda: self._set_view("resource"),
        )
        self._btn_resource.pack(side="right", padx=(8, 0))

        for widget in (drag_bar, self._title_label):
            widget.bind("<ButtonPress-1>", self._start_move)
            widget.bind("<B1-Motion>", self._do_move)
            widget.bind("<ButtonRelease-1>", lambda _e: self._save_position())

    def _title_text(self):
        return f"⠿  CraftMap Resources   ({self.toggle_key} to hide)"

    def _update_title_bar(self):
        self._title_label.config(text=self._title_text())

    def _open_hotkey_settings(self):
        win = tk.Toplevel(self)
        win.title("Hotkey Settings")
        win.configure(bg="#0d1117")
        win.attributes("-topmost", True)
        win.resizable(False, False)

        tk.Label(
            win,
            text="Hide/show key  (e.g. F1, F2, ctrl+shift+r):",
            bg="#0d1117",
            fg="#c9d1d9",
            font=("Segoe UI", 9),
        ).pack(padx=16, pady=(14, 4), anchor="w")

        entry = tk.Entry(
            win,
            bg="#161b22",
            fg="#c9d1d9",
            insertbackground="#c9d1d9",
            relief="flat",
            font=("Segoe UI", 9),
            width=24,
        )
        entry.insert(0, self.toggle_key)
        entry.pack(padx=16, pady=4, fill="x")
        entry.focus_set()
        entry.select_range(0, "end")

        msg = tk.Label(win, text="", bg="#0d1117", fg="#da3633", font=("Segoe UI", 8))
        msg.pack(padx=16, pady=(0, 4))

        def apply():
            new_key = entry.get().strip()
            if not new_key:
                msg.config(text="Key cannot be empty.")
                return
            try:
                self.change_hotkey(new_key)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                msg.config(text=f"Invalid key: {exc}")
                return
            win.destroy()

        entry.bind("<Return>", lambda _e: apply())

        btns = tk.Frame(win, bg="#0d1117")
        btns.pack(pady=(4, 12), padx=16, fill="x")
        tk.Button(
            btns,
            text="Apply",
            command=apply,
            bg="#1f6feb",
            fg="white",
            relief="flat",
            padx=10,
        ).pack(side="left", padx=(0, 6))
        tk.Button(
            btns,
            text="Cancel",
            command=win.destroy,
            bg="#21262d",
            fg="#c9d1d9",
            relief="flat",
            padx=10,
        ).pack(side="left")

        win.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - win.winfo_reqwidth()) // 2
        y = self.winfo_y() + (self.winfo_height() - win.winfo_reqheight()) // 2
        win.geometry(f"+{x}+{y}")

    def _on_tree_open(self, _event):
        iid = self.tree.focus()
        key = self._iid_to_key.get(iid)
        if key:
            self._collapsed.discard(key)
            self._save_collapsed()

    def _on_tree_close(self, _event):
        iid = self.tree.focus()
        key = self._iid_to_key.get(iid)
        if key:
            self._collapsed.add(key)
            self._save_collapsed()

    def _save_collapsed(self):
        cfg: dict = load_config()
        cfg["collapsed_nodes"] = sorted(self._collapsed)
        save_config(cfg)

    def _save_position(self):
        cfg: dict = load_config()
        cfg["window_x"] = self.winfo_x()
        cfg["window_y"] = self.winfo_y()
        try:
            cfg["recipe_split"] = self._pw_recipe.sash_coord(0)[1]
        except Exception:
            pass
        save_config(cfg)

    def _save_recipe_split(self, _):
        self._save_position()

    def _on_hotkey(self):
        self.after(0, self.toggle)

    def change_hotkey(self, new_key):
        """Re-register the global hotkey and persist to config."""
        if HOTKEY_AVAILABLE:
            if self._hotkey_handle is not None:
                try:
                    keyboard.remove_hotkey(self._hotkey_handle)
                except Exception:  # pylint: disable=broad-exception-caught
                    pass
            self._hotkey_handle = keyboard.add_hotkey(new_key, self._on_hotkey)
        self.toggle_key = new_key
        self._update_title_bar()
        cfg = load_config()
        cfg["toggle_key"] = new_key
        save_config(cfg)

    def register_hotkey(self):
        """Called from main() once the keyboard thread is running."""
        if HOTKEY_AVAILABLE:
            self._hotkey_handle = keyboard.add_hotkey(self.toggle_key, self._on_hotkey)

    def _set_view(self, mode: str):
        self._view_mode = mode
        for btn, key in (
            (self._btn_resource, "resource"),
            (self._btn_location, "location"),
            (self._btn_recipe, "recipe"),
        ):
            btn.config(
                bg="#1f6feb" if mode == key else "#21262d",
                fg="white" if mode == key else "#8b949e",
            )
        cfg: dict = load_config()
        cfg["view_mode"] = mode
        save_config(cfg)
        self._apply_view_visibility()
        self.refresh()

    def _start_move(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _do_move(self, _event):
        x = self.winfo_pointerx() - self._drag_x
        y = self.winfo_pointery() - self._drag_y
        self.geometry(f"+{x}+{y}")

    def _build_resize_grip(self):
        grip = tk.Label(
            self,
            text="◢",
            bg="#0d1117",
            fg="#3b434d",
            font=("Segoe UI", 7),
            cursor="size_nw_se",
        )
        grip.place(relx=1.0, rely=1.0, anchor="se")
        grip.bind("<ButtonPress-1>", self._start_resize)
        grip.bind("<B1-Motion>", self._do_resize)
        grip.bind("<ButtonRelease-1>", self._end_resize)

    def _enable_composited(self):
        if sys.platform != "win32":
            return
        self.update_idletasks()
        hwnd = self.winfo_id()
        style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
        ctypes.windll.user32.SetWindowLongW(hwnd, -20, style | 0x02000000)

    def _start_resize(self, event):
        self._resize_x = event.x_root
        self._resize_y = event.y_root
        self._resize_w = self.winfo_width()
        self._resize_h = self.winfo_height()
        self.update_idletasks()
        self._resize_min_w = max(400, self.winfo_reqwidth())
        self._resize_min_h = max(200, self.winfo_reqheight())

    def _do_resize(self, event):
        dw = event.x_root - self._resize_x
        dh = event.y_root - self._resize_y
        new_w = max(self._resize_min_w, self._resize_w + dw)
        new_h = max(self._resize_min_h, self._resize_h + dh)
        self.geometry(f"{new_w}x{new_h}+{self.winfo_x()}+{self.winfo_y()}")
        self.update_idletasks()
        if sys.platform == "win32":
            # RDW_INVALIDATE|RDW_ERASE|RDW_ALLCHILDREN|RDW_UPDATENOW
            ctypes.windll.user32.RedrawWindow(self.winfo_id(), None, None, 0x0185)

    def _end_resize(self, _event):
        self._user_sized = True
        self._save_size()

    def _save_size(self):
        cfg: dict = load_config()
        cfg["window_w"] = self.winfo_width()
        cfg["window_h"] = self.winfo_height()
        save_config(cfg)

    # ----- search -----
    def _build_search(self):
        frame = tk.Frame(self, bg="#0d1117")
        frame.pack(fill="x", padx=8, pady=(8, 4))
        self._search_frame = frame
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_a: self.refresh())
        entry = tk.Entry(
            frame,
            textvariable=self.search_var,
            bg="#161b22",
            fg="#c9d1d9",
            insertbackground="#c9d1d9",
            relief="flat",
        )
        entry.pack(fill="x", ipady=4)
        self._search_placeholder(entry)

    def _search_placeholder(self, entry):
        ph = "Search resource / sector / system / planet / notes..."
        entry.insert(0, ph)
        entry.config(fg="#6e7681")

        def on_focus_in(_e):
            if entry.get() == ph:
                entry.delete(0, "end")
                entry.config(fg="#c9d1d9")

        def on_focus_out(_e):
            if not entry.get():
                entry.insert(0, ph)
                entry.config(fg="#6e7681")

        entry.bind("<FocusIn>", on_focus_in)
        entry.bind("<FocusOut>", on_focus_out)
        self._search_entry = entry

    def get_search_text(self):
        ph = "Search resource / sector / system / planet / notes..."
        val = self._search_entry.get() if self._search_entry else ""
        return "" if val == ph else val

    # ----- type filter toggles (dynamic, rebuilt from DB contents) -----
    def _build_type_filter(self):
        self.filter_frame = tk.Frame(self, bg="#0d1117")
        self.filter_frame.pack(fill="x", padx=8, pady=(0, 4))
        self._rebuild_type_filter()

    def _rebuild_type_filter(self):
        for w in self.filter_frame.winfo_children():
            w.destroy()

        types = distinct_values("res_type")
        if not types:
            return

        tk.Label(
            self.filter_frame,
            text="Type:",
            bg="#0d1117",
            fg="#8b949e",
            font=("Segoe UI", 8),
        ).pack(side="left", padx=(0, 4))

        for t in types:
            if t not in self.type_filter_vars:
                self.type_filter_vars[t] = tk.BooleanVar(value=True)
            cb = tk.Checkbutton(
                self.filter_frame,
                text=t,
                variable=self.type_filter_vars[t],
                command=self.refresh,
                bg="#0d1117",
                fg="#c9d1d9",
                selectcolor="#161b22",
                activebackground="#0d1117",
                activeforeground="#c9d1d9",
                font=("Segoe UI", 8),
                bd=0,
                highlightthickness=0,
            )
            cb.pack(side="left", padx=2)

        # drop stale type vars that no longer exist in the DB
        for stale in [k for k in self.type_filter_vars if k not in types]:
            del self.type_filter_vars[stale]

    def get_allowed_types(self):
        if not self.type_filter_vars:
            return None  # no types logged yet - no filtering
        return [t for t, v in self.type_filter_vars.items() if v.get()]

    # ----- tree (Type > Resource > Sector > System > Planet) -----
    def _build_tree(self):
        style = ttk.Style()
        style.theme_use("default")
        style.configure(
            "Treeview",
            background="#161b22",
            fieldbackground="#161b22",
            foreground="#c9d1d9",
            rowheight=22,
            borderwidth=0,
        )
        style.configure(
            "Treeview.Heading",
            background="#21262d",
            foreground="#c9d1d9",
            relief="flat",
        )
        style.map("Treeview", background=[("selected", "#1f6feb")])

        style.layout("Thin.Vertical.TScrollbar", [  # type: ignore[arg-type]
            ("Vertical.TScrollbar.trough", {"sticky": "ns", "children": [
                ("Vertical.TScrollbar.thumb", {"expand": "1", "sticky": "nswe"}),
            ]}),
        ])
        style.configure(
            "Thin.Vertical.TScrollbar",
            background="#30363d",
            troughcolor="#161b22",
            bordercolor="#161b22",
            relief="flat",
            width=6,
        )
        style.map("Thin.Vertical.TScrollbar",
            background=[("active", "#484f58"), ("pressed", "#58a6ff")],
        )

        self._tree_frame = tk.Frame(self, bg="#0d1117")
        self._tree_frame.pack(fill="both", expand=True, padx=8, pady=4)

        self._tree_frame.grid_rowconfigure(0, weight=1)
        self._tree_frame.grid_columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(self._tree_frame, show="tree")
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(self._tree_frame, orient="vertical",
                             style="Thin.Vertical.TScrollbar", command=self.tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=_autohide_yscroll(vsb))

        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<<TreeviewOpen>>", self._on_tree_open)
        self.tree.bind("<<TreeviewClose>>", self._on_tree_close)

    # ----- form -----
    def _build_form(self):
        form = tk.Frame(self, bg="#0d1117")
        form.pack(fill="x", padx=8, pady=(4, 8))
        self._form_frame = form

        grid = tk.Frame(form, bg="#0d1117")
        grid.pack(fill="x")

        labels = ["Type", "Resource", "Sector", "System", "Planet", "Status"]
        for i, lbl in enumerate(labels):
            tk.Label(
                grid, text=lbl, bg="#0d1117", fg="#8b949e", font=("Segoe UI", 8)
            ).grid(row=0, column=i, sticky="w", padx=2)

        self.type_box = ttk.Combobox(grid, width=10)
        self.type_box.grid(row=1, column=0, sticky="ew", padx=2)

        self.resource_box = ttk.Combobox(grid, width=11)
        self.resource_box.grid(row=1, column=1, sticky="ew", padx=2)

        self.sector_box = ttk.Combobox(grid, width=10)
        self.sector_box.grid(row=1, column=2, sticky="ew", padx=2)

        self.system_box = ttk.Combobox(grid, width=11)
        self.system_box.grid(row=1, column=3, sticky="ew", padx=2)

        self.planet_box = ttk.Combobox(grid, width=11)
        self.planet_box.grid(row=1, column=4, sticky="ew", padx=2)

        self.status_var = tk.StringVar(value=STATUS_OPTIONS[3])
        status_menu = ttk.Combobox(
            grid,
            textvariable=self.status_var,
            values=STATUS_OPTIONS,
            width=9,
            state="readonly",
        )
        status_menu.grid(row=1, column=5, sticky="ew", padx=2)

        for i in range(6):
            grid.columnconfigure(i, weight=1)

        # cascading filter bindings + live autocomplete
        for box, field in (
            (self.type_box, "type"),
            (self.resource_box, "resource"),
            (self.sector_box, "sector"),
            (self.system_box, "system"),
        ):
            box.bind(
                "<<ComboboxSelected>>", lambda _, f=field: self._filter_dropdowns(f)
            )
            _LiveDropdown(
                box,
                pre_fn=lambda f=field: self._filter_dropdowns(f),
                on_select_fn=lambda _, f=field: self._filter_dropdowns(f),
            )
        _LiveDropdown(self.planet_box)

        # notes
        tk.Label(
            form, text="Notes", bg="#0d1117", fg="#8b949e", font=("Segoe UI", 8)
        ).pack(anchor="w", pady=(6, 0))
        self.notes_entry = tk.Entry(
            form, bg="#161b22", fg="#c9d1d9", insertbackground="#c9d1d9", relief="flat"
        )
        self.notes_entry.pack(fill="x", ipady=3)

        # buttons
        btns = tk.Frame(form, bg="#0d1117")
        btns.pack(fill="x", pady=(8, 0))
        tk.Button(
            btns,
            text="Add",
            command=self.add_entry,
            bg="#238636",
            fg="white",
            relief="flat",
            padx=10,
        ).pack(side="left", padx=2)
        tk.Button(
            btns,
            text="Update",
            command=self.update_entry,
            bg="#1f6feb",
            fg="white",
            relief="flat",
            padx=10,
        ).pack(side="left", padx=2)
        tk.Button(
            btns,
            text="Clear",
            command=self.clear_form,
            bg="#21262d",
            fg="#c9d1d9",
            relief="flat",
            padx=10,
        ).pack(side="left", padx=2)
        tk.Button(
            btns,
            text="Delete",
            command=self.delete_entry,
            bg="#da3633",
            fg="white",
            relief="flat",
            padx=10,
        ).pack(side="right", padx=(2, 18))

    # ----- dropdown refresh (dynamic, no hardcoding) -----
    def refresh_dropdowns(self):
        if not hasattr(self, "resource_box"):
            return  # form not built yet (can fire during early init via search placeholder)
        self._filter_dropdowns()

    def _filter_dropdowns(self, *_) -> None:
        """Two independent cascades: Type→Resource and Sector→System→Planet."""
        t = self.type_box.get().strip()
        s = self.sector_box.get().strip()
        sys_ = self.system_box.get().strip()

        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        def _q(col: str, constraints: dict) -> list:
            active = [(c, v) for c, v in constraints.items() if v]
            q = (
                f"SELECT DISTINCT {col} FROM deposits"
                f" WHERE {col} IS NOT NULL AND {col} != ''"
            )
            if active:
                q += " AND " + " AND ".join(f"{c} = ?" for c, _ in active)
            q += f" ORDER BY {col} COLLATE NOCASE"
            cur.execute(q, [v for _, v in active])
            return [row[0] for row in cur.fetchall()]

        # Chain 1: Type → Resource
        self.type_box["values"] = _q("res_type", {})
        self.resource_box["values"] = _q("resource", {"res_type": t})

        # Chain 2: Sector → System → Planet
        self.sector_box["values"] = _q("sector", {})
        self.system_box["values"] = _q("system_name", {"sector": s})
        self.planet_box["values"] = _q("planet", {"sector": s, "system_name": sys_})

        conn.close()

    # ----- tree + data refresh -----
    def refresh(self):
        if self._view_mode == "recipe":
            if hasattr(self, "_recipe_breakdown_tree"):
                self._refresh_recipe_list()
                self._refresh_recipe_breakdown()
            return
        if not hasattr(self, "tree"):
            return  # widgets not built yet (can fire during early init via search placeholder)
        self.refresh_dropdowns()
        if hasattr(self, "filter_frame"):
            self._rebuild_type_filter()
            self.after(0, self.auto_size)

        allowed_types = (
            self.get_allowed_types() if hasattr(self, "type_filter_vars") else None
        )
        order = "location" if self._view_mode == "location" else "resource"
        rows = fetch_all(
            self.get_search_text().lower() if self._search_entry else "",
            allowed_types,
            order_by=order,
        )

        for row in self.tree.get_children():
            self.tree.delete(row)
        self._iid_to_key = {}

        if self._view_mode == "location":
            self._refresh_location_view(rows)
            self.tree.tag_configure(
                "loc_sec", foreground="#3fb950", font=("Segoe UI", 9, "bold")
            )
            self.tree.tag_configure(
                "loc_sys", foreground="#58a6ff", font=("Segoe UI", 9, "bold")
            )
            self.tree.tag_configure("loc_pla", foreground="#d2a8ff")
            self.tree.tag_configure(
                "loc_sum", foreground="#8b949e", font=("Segoe UI", 9, "italic")
            )
            self.tree.tag_configure("loc_entry", foreground="#c9d1d9")
        else:
            self._refresh_resource_view(rows)
            self.tree.tag_configure(
                "type", foreground="#f0883e", font=("Segoe UI", 9, "bold")
            )
            self.tree.tag_configure(
                "resource", foreground="#58a6ff", font=("Segoe UI", 9, "bold")
            )
            self.tree.tag_configure(
                "sector", foreground="#3fb950", font=("Segoe UI", 9, "bold")
            )
            self.tree.tag_configure(
                "system", foreground="#d2a8ff", font=("Segoe UI", 9, "bold")
            )
            self.tree.tag_configure("planet", foreground="#c9d1d9")

    def _refresh_resource_view(self, rows):
        type_nodes = {}
        resource_nodes = {}
        sector_nodes = {}
        system_nodes = {}

        for (
            row_id,
            res_type,
            resource,
            sector,
            system_name,
            planet,
            status,
            notes,
            _,
        ) in rows:
            type_label = res_type if res_type else "(Uncategorized)"

            if type_label not in type_nodes:
                ck = f"type|{type_label}"
                iid = self.tree.insert(
                    "",
                    "end",
                    text=f"◆ {type_label}",
                    open=(ck not in self._collapsed),
                    tags=("type",),
                )
                self._iid_to_key[iid] = ck
                type_nodes[type_label] = iid
            t_node = type_nodes[type_label]

            if resource:
                res_key = (type_label, resource)
                if res_key not in resource_nodes:
                    ck = f"res|{type_label}|{resource}"
                    iid = self.tree.insert(
                        t_node,
                        "end",
                        text=f"  ▸ {resource}",
                        open=(ck not in self._collapsed),
                        tags=("resource",),
                    )
                    self._iid_to_key[iid] = ck
                    resource_nodes[res_key] = iid
                r_node = resource_nodes[res_key]
            else:
                r_node = t_node

            if sector:
                sec_key = (type_label, resource, sector)
                if sec_key not in sector_nodes:
                    indent = "     " if resource else "   "
                    ck = f"sec|{type_label}|{resource}|{sector}"
                    iid = self.tree.insert(
                        r_node,
                        "end",
                        text=f"{indent}◉ {sector}",
                        open=(ck not in self._collapsed),
                        tags=("sector",),
                    )
                    self._iid_to_key[iid] = ck
                    sector_nodes[sec_key] = iid
                sec_node = sector_nodes[sec_key]
            else:
                sec_node = r_node

            if system_name:
                sys_key = (type_label, resource, sector, system_name)
                if sys_key not in system_nodes:
                    if sector:
                        indent = "        " if resource else "      "
                    elif resource:
                        indent = "     "
                    else:
                        indent = "   "
                    ck = f"sys|{type_label}|{resource}|{sector}|{system_name}"
                    iid = self.tree.insert(
                        sec_node,
                        "end",
                        text=f"{indent}{system_name}",
                        open=(ck not in self._collapsed),
                        tags=("system",),
                    )
                    self._iid_to_key[iid] = ck
                    system_nodes[sys_key] = iid
                s_node = system_nodes[sys_key]
            else:
                s_node = sec_node

            if system_name:
                planet_indent = (
                    ("           " if resource else "         ")
                    if sector
                    else ("        " if resource else "      ")
                )
            elif sector:
                planet_indent = "        " if resource else "      "
            elif resource:
                planet_indent = "     "
            else:
                planet_indent = "   "

            label = f"{planet_indent}{planet}"
            extras = []
            if status and status != "Unknown":
                extras.append(status)
            if notes:
                extras.append(f"[{notes}]")
            if extras:
                label += "  —  " + "  ".join(extras)
            self.tree.insert(
                s_node, "end", iid=str(row_id), text=label, tags=("planet",)
            )

    def _refresh_location_view(self, rows):  # pylint: disable=too-many-locals
        # ---- first pass: collect data ----
        sec_order: list = []
        seen_sec: set = set()
        sec_type_res: dict = {}  # sec -> {rtype -> {resource: True}}

        sec_sys_order: dict = {}  # sec -> [sys, ...]
        seen_sec_sys: set = set()
        sys_type_res: dict = {}  # (sec, sys) -> {rtype -> {resource: True}}

        sys_pla_order: dict = {}  # (sec, sys) -> [pla, ...]
        sec_pla_order: dict = {}  # sec -> [pla, ...]  (no-system planets)
        seen_pla: set = set()
        pla_entries: dict = (
            {}
        )  # (sec, sys, pla) -> [(row_id, res, rtype, status, notes)]

        for (
            row_id,
            res_type,
            resource,
            sector,
            system_name,
            planet,
            status,
            notes,
            _,
        ) in rows:
            sec = sector or "(No Sector)"
            sysn = system_name or ""
            pla = planet or "(No Planet)"
            rtype = res_type or ""
            res = resource or ""

            if sec not in seen_sec:
                seen_sec.add(sec)
                sec_order.append(sec)
                sec_type_res[sec] = {}
                sec_sys_order[sec] = []
                sec_pla_order[sec] = []
            if rtype:
                if rtype not in sec_type_res[sec]:
                    sec_type_res[sec][rtype] = {}
                if res:
                    sec_type_res[sec][rtype][res] = True

            if sysn:
                sys_key = (sec, sysn)
                if sys_key not in seen_sec_sys:
                    seen_sec_sys.add(sys_key)
                    sec_sys_order[sec].append(sysn)
                    sys_type_res[sys_key] = {}
                    sys_pla_order[sys_key] = []
                if rtype:
                    if rtype not in sys_type_res[sys_key]:
                        sys_type_res[sys_key][rtype] = {}
                    if res:
                        sys_type_res[sys_key][rtype][res] = True

            pla_key = (sec, sysn, pla)
            if pla_key not in seen_pla:
                seen_pla.add(pla_key)
                (sys_pla_order[(sec, sysn)] if sysn else sec_pla_order[sec]).append(pla)
                pla_entries[pla_key] = []
            pla_entries[pla_key].append((row_id, res, rtype, status, notes))

        # ---- helpers ----
        _sum_idx = [0]  # mutable counter for unique summary-row iids

        def _insert_summaries(parent_iid: str, type_res: dict) -> None:
            """Insert non-interactive type → resources summary rows under parent."""
            for rtype, res_dict in type_res.items():
                res_list = list(res_dict.keys())
                text = f"{rtype}:  {' · '.join(res_list)}" if res_list else rtype
                _sum_idx[0] += 1
                self.tree.insert(
                    parent_iid,
                    "end",
                    iid=f"__sum_{_sum_idx[0]}",
                    text=text,
                    tags=("loc_sum",),
                )

        def _insert_planet(parent_iid: str, sec: str, sysn: str, pla: str) -> None:
            pla_key = (sec, sysn, pla)
            ck = f"loc_pla|{sec}|{sysn}|{pla}"
            pla_node = self.tree.insert(
                parent_iid,
                "end",
                text=pla,
                open=(ck not in self._collapsed),
                tags=("loc_pla",),
            )
            self._iid_to_key[pla_node] = ck
            for eid, res, rtype, stat, nts in pla_entries.get(pla_key, []):
                parts = []
                if rtype:
                    parts.append(f"{rtype}:")
                if res:
                    parts.append(res)
                if stat and stat not in ("Unknown", ""):
                    parts.append(f"—  {stat}")
                if nts:
                    parts.append(f"[{nts}]")
                self.tree.insert(
                    pla_node,
                    "end",
                    iid=str(eid),
                    text="  ".join(parts) if parts else "(no data)",
                    tags=("loc_entry",),
                )

        # ---- second pass: render ----
        for sec in sec_order:
            ck = f"loc_sec|{sec}"
            sec_node = self.tree.insert(
                "",
                "end",
                text=f"◉  {sec}",
                open=(ck not in self._collapsed),
                tags=("loc_sec",),
            )
            self._iid_to_key[sec_node] = ck
            _insert_summaries(sec_node, sec_type_res[sec])

            for sysn in sec_sys_order.get(sec, []):
                sys_key = (sec, sysn)
                ck = f"loc_sys|{sec}|{sysn}"
                sys_node = self.tree.insert(
                    sec_node,
                    "end",
                    text=sysn,
                    open=(ck not in self._collapsed),
                    tags=("loc_sys",),
                )
                self._iid_to_key[sys_node] = ck
                _insert_summaries(sys_node, sys_type_res.get(sys_key, {}))
                for pla in sys_pla_order.get(sys_key, []):
                    _insert_planet(sys_node, sec, sysn, pla)

            for pla in sec_pla_order.get(sec, []):
                _insert_planet(sec_node, sec, "", pla)

    def on_select(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        item_id = sel[0]
        if not item_id.isdigit():
            return  # header rows aren't real DB rows
        row_id = int(item_id)
        self.selected_id = row_id
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "SELECT res_type, resource, sector, system_name,"
            " planet, status, notes, logged_at FROM deposits WHERE id=?",
            (row_id,),
        )
        row = c.fetchone()
        conn.close()
        if row:
            res_type, resource, sector, system_name, planet, status, notes, _ = row
            self.type_box.set(res_type or "")
            self.resource_box.set(resource or "")
            self.sector_box.set(sector or "")
            self.system_box.set(system_name or "")
            self.planet_box.set(planet or "")
            self.status_var.set(status or STATUS_OPTIONS[3])
            self.notes_entry.delete(0, "end")
            self.notes_entry.insert(0, notes or "")
            self.notes_entry.xview_moveto(1.0)
            self._filter_dropdowns()

    def get_form_values(self):
        return (
            self.type_box.get().strip(),
            self.resource_box.get().strip(),
            self.sector_box.get().strip(),
            self.system_box.get().strip(),
            self.planet_box.get().strip(),
            self.status_var.get(),
            self.notes_entry.get().strip(),
        )

    def add_entry(self):
        res_type, resource, sector, system_name, planet, status, notes = (
            self.get_form_values()
        )
        if not planet:
            messagebox.showwarning("Missing info", "Planet is required.")
            return
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM deposits"
            " WHERE COALESCE(res_type,'')=? AND COALESCE(resource,'')=?"
            " AND COALESCE(sector,'')=? AND COALESCE(system_name,'')=?"
            " AND COALESCE(planet,'')=?",
            (res_type, resource, sector, system_name, planet),
        )
        exists = cur.fetchone()
        conn.close()
        if exists:
            messagebox.showwarning(
                "Duplicate",
                "An entry with the same type, resource, sector, system and planet already exists.",
            )
            return
        logged_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        insert_row(
            res_type, resource, sector, system_name, planet, status, notes, logged_at
        )
        self.clear_form()
        self.refresh()

    def update_entry(self):
        if self.selected_id is None:
            messagebox.showinfo(
                "No selection", "Select an entry in the tree to update first."
            )
            return
        res_type, resource, sector, system_name, planet, status, notes = (
            self.get_form_values()
        )
        if not planet:
            messagebox.showwarning("Missing info", "Planet is required.")
            return
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM deposits"
            " WHERE COALESCE(res_type,'')=? AND COALESCE(resource,'')=?"
            " AND COALESCE(sector,'')=? AND COALESCE(system_name,'')=?"
            " AND COALESCE(planet,'')=? AND id != ?",
            (res_type, resource, sector, system_name, planet, self.selected_id),
        )
        exists = cur.fetchone()
        conn.close()
        if exists:
            messagebox.showwarning(
                "Duplicate", "Another entry with the same combination already exists."
            )
            return
        logged_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        update_row(
            self.selected_id,
            res_type,
            resource,
            sector,
            system_name,
            planet,
            status,
            notes,
            logged_at,
        )
        self.refresh()

    def delete_entry(self):
        if self.selected_id is None:
            messagebox.showinfo(
                "No selection", "Select an entry in the tree to delete first."
            )
            return
        if not messagebox.askyesno("Confirm delete", "Delete this entry?"):
            return
        delete_row(self.selected_id)
        self.clear_form()
        self.refresh()

    def clear_form(self):
        self.selected_id = None
        self.type_box.set("")
        self.resource_box.set("")
        self.sector_box.set("")
        self.system_box.set("")
        self.planet_box.set("")
        self.notes_entry.delete(0, "end")
        self.status_var.set(STATUS_OPTIONS[3])
        for sel in self.tree.selection():
            self.tree.selection_remove(sel)
        self._filter_dropdowns()

    def toggle(self):
        if self.state() == "withdrawn":
            self.deiconify()
            self.attributes("-topmost", True)
            if self._queue_panel and not self._queue_panel.pinned:
                self._queue_panel.show()
        else:
            self.withdraw()
            if self._queue_panel and not self._queue_panel.pinned:
                self._queue_panel.hide()

    def toggle_queue_panel(self):
        if self._queue_panel is None:
            self._queue_panel = CraftQueuePanel(self, self)
            self._queue_panel.on_hide_cb = self._on_queue_panel_hide
            self._btn_queue_panel.config(bg="#1f6feb", fg="white")
        elif self._queue_panel.is_visible():
            self._queue_panel.hide()
        else:
            self._queue_panel.show()
            self._btn_queue_panel.config(bg="#1f6feb", fg="white")

    def _on_queue_panel_hide(self):
        self._btn_queue_panel.config(bg="#21262d", fg="#8b949e")

    def _on_recipe_combo_right_click(self, event):
        if self._recipe_selected_id is None:
            return
        menu = tk.Menu(
            self,
            tearoff=0,
            bg="#21262d",
            fg="#c9d1d9",
            activebackground="#1f6feb",
            activeforeground="white",
        )
        menu.add_command(label="Add to Queue", command=self._add_recipe_to_queue)
        menu.tk_popup(event.x_root, event.y_root)

    def _add_recipe_to_queue(self):
        if self._recipe_selected_id is None:
            return
        if self._queue_panel is None:
            self._queue_panel = CraftQueuePanel(self, self)
            self._queue_panel.on_hide_cb = self._on_queue_panel_hide
        try:
            qty = max(float(self._recipe_qty_var.get()), 0.001)
        except ValueError:
            qty = 1.0
        self._queue_panel.add_job(self._recipe_selected_id, qty)
        self._queue_panel.show()
        self._btn_queue_panel.config(bg="#1f6feb", fg="white")

    def quit_app(self):
        """Fully close the app (not just hide) - also ends the hotkey
        listener thread and the hidden console process."""
        self._save_position()
        if self.tray_icon is not None:
            getattr(self.tray_icon, "stop")()
        try:
            self.destroy()
        finally:
            os._exit(0)

    def auto_size(self):
        """Fit window to content on first show; skipped once user has manually resized."""
        if self._user_sized:
            return
        self.update_idletasks()
        width = max(640, self.winfo_reqwidth())
        height = self.winfo_reqheight()
        x = self.winfo_x()
        y = self.winfo_y()
        self.geometry(f"{width}x{height}+{x}+{y}")

    # ----- recipe panel -----

    def _apply_view_visibility(self):
        is_recipe = self._view_mode == "recipe"
        deposit_frames = [
            self._search_frame,
            self.filter_frame,
            self._tree_frame,
            self._form_frame,
        ]
        if is_recipe:
            for f in deposit_frames:
                f.pack_forget()
            self._recipe_frame.pack(fill="both", expand=True, padx=8, pady=4)
            self._refresh_recipe_list()
        else:
            self._recipe_frame.pack_forget()
            self._search_frame.pack(fill="x", padx=8, pady=(8, 4))
            self.filter_frame.pack(fill="x", padx=8, pady=(0, 4))
            self._tree_frame.pack(fill="both", expand=True, padx=8, pady=4)
            self._form_frame.pack(fill="x", padx=8, pady=(4, 8))

    def _make_checkbox_images(self):
        s = 14
        unc = tk.PhotoImage(width=s, height=s)
        for y in range(s):
            unc.put(" ".join(["#161b22"] * s), to=(0, y))
        for i in range(s):
            unc.put("#6e7681", to=(0, i, 1, i + 1))
            unc.put("#6e7681", to=(s - 1, i, s, i + 1))
            unc.put("#6e7681", to=(i, 0, i + 1, 1))
            unc.put("#6e7681", to=(i, s - 1, i + 1, s))
        chk = tk.PhotoImage(width=s, height=s)
        for y in range(s):
            chk.put(" ".join(["#1f6feb"] * s), to=(0, y))
        for x, y in [
            (2, 7),
            (2, 8),
            (3, 8),
            (3, 9),
            (4, 9),
            (4, 10),
            (5, 10),
            (5, 11),
            (6, 9),
            (6, 10),
            (7, 8),
            (7, 9),
            (8, 7),
            (8, 8),
            (9, 6),
            (9, 7),
            (10, 5),
            (10, 6),
            (11, 4),
            (11, 5),
            (12, 3),
            (12, 4),
        ]:
            chk.put("#ffffff", to=(x, y, x + 1, y + 1))
        self.img_unchecked = unc
        self.img_checked = chk

    def _build_recipe_panel(self):
        self._recipe_frame = tk.Frame(self, bg="#0d1117")
        self._make_checkbox_images()
        # not packed here — _apply_view_visibility handles it

        # --- selector row ---
        sel = tk.Frame(self._recipe_frame, bg="#0d1117")
        sel.pack(fill="x", pady=(4, 2))
        # Left controls — hidden in "Used In" mode
        self._sel_left = tk.Frame(sel, bg="#0d1117")
        self._sel_left.pack(side="left")
        tk.Label(
            self._sel_left, text="Recipe:", bg="#0d1117", fg="#8b949e", font=("Segoe UI", 8)
        ).pack(side="left", padx=(0, 4))
        self._recipe_var = tk.StringVar()
        self._recipe_combo = ttk.Combobox(self._sel_left, textvariable=self._recipe_var, width=28)
        self._recipe_combo.pack(side="left", padx=(0, 6))
        self._recipe_combo.bind("<<ComboboxSelected>>", self._on_recipe_combo_select)
        self._recipe_combo.bind("<Return>", self._on_recipe_combo_select)
        self._recipe_combo.bind("<Button-3>", self._on_recipe_combo_right_click)
        _LiveDropdown(
            self._recipe_combo,
            pre_fn=lambda: self._recipe_combo.configure(
                values=[n for _, n in get_all_recipes()]
            ),
            on_select_fn=self._on_recipe_combo_select,
        )
        tk.Button(
            self._sel_left,
            text="New",
            command=self.clear_recipe_form,
            bg="#21262d",
            fg="#c9d1d9",
            relief="flat",
            bd=0,
            padx=8,
            font=("Segoe UI", 8),
        ).pack(side="left", padx=(0, 8))
        tk.Label(self._sel_left, text="×", bg="#0d1117", fg="#8b949e", font=("Segoe UI", 9)).pack(
            side="left"
        )
        self._recipe_qty_var = tk.StringVar(value="1")
        qty_entry = tk.Entry(
            self._sel_left,
            textvariable=self._recipe_qty_var,
            width=5,
            bg="#161b22",
            fg="#c9d1d9",
            insertbackground="#c9d1d9",
            relief="flat",
            justify="center",
        )
        qty_entry.pack(side="left", ipady=2, padx=(1, 12))
        qty_entry.bind("<Return>", lambda _e: self._refresh_recipe_breakdown())
        qty_entry.bind("<FocusOut>", lambda _e: self._refresh_recipe_breakdown())
        # Breakdown / Totals / Used In toggle (right side)
        self._recipe_breakdown_mode = "breakdown"
        self._btn_bd_breakdown = tk.Button(
            sel,
            text="Breakdown",
            bg="#1f6feb",
            fg="white",
            relief="flat",
            bd=0,
            padx=6,
            font=("Segoe UI", 8),
            command=lambda: self._set_recipe_mode("breakdown"),
        )
        self._btn_bd_breakdown.pack(side="right", padx=(2, 0))
        self._btn_bd_totals = tk.Button(
            sel,
            text="Totals",
            bg="#21262d",
            fg="#8b949e",
            relief="flat",
            bd=0,
            padx=6,
            font=("Segoe UI", 8),
            command=lambda: self._set_recipe_mode("totals"),
        )
        self._btn_bd_totals.pack(side="right", padx=(0, 2))
        self._btn_bd_usedin = tk.Button(
            sel,
            text="Used In",
            bg="#21262d",
            fg="#8b949e",
            relief="flat",
            bd=0,
            padx=6,
            font=("Segoe UI", 8),
            command=lambda: self._set_recipe_mode("usedin"),
        )
        self._btn_bd_usedin.pack(side="right", padx=(0, 2))


        # --- PanedWindow: breakdown tree (top) + edit form (bottom) ---
        self._pw_recipe = tk.PanedWindow(
            self._recipe_frame, orient=tk.VERTICAL,
            bg="#21262d", sashwidth=5, sashrelief="flat",
            sashpad=0, handlesize=0, bd=0,
        )
        self._pw_recipe.pack(fill="both", expand=True, pady=(2, 4))
        self._pw_recipe.bind("<ButtonRelease-1>", self._save_recipe_split)

        # Top pane: breakdown tree
        self._bd_frame = tk.Frame(self._pw_recipe, bg="#0d1117")
        self._pw_recipe.add(self._bd_frame, height=self._recipe_split, minsize=40, stretch="always")
        bd_frame = self._bd_frame
        bd_frame.grid_rowconfigure(0, weight=1)
        bd_frame.grid_columnconfigure(0, weight=1)
        self._recipe_breakdown_tree = ttk.Treeview(bd_frame, show="tree")
        self._recipe_breakdown_tree.grid(row=0, column=0, sticky="nsew")
        bd_vsb = ttk.Scrollbar(bd_frame, orient="vertical",
                                style="Thin.Vertical.TScrollbar",
                                command=self._recipe_breakdown_tree.yview)
        bd_vsb.grid(row=0, column=1, sticky="ns")
        self._recipe_breakdown_tree.configure(yscrollcommand=_autohide_yscroll(bd_vsb))
        self._recipe_breakdown_tree.bind("<ButtonRelease-1>", self._on_breakdown_click)
        self._recipe_breakdown_tree.bind("<Double-Button-1>", self._on_breakdown_double_click)
        self._recipe_breakdown_tree.bind("<<TreeviewOpen>>", self._on_bd_toggled)
        self._recipe_breakdown_tree.bind("<<TreeviewClose>>", self._on_bd_toggled)

        # Bottom pane: separator + edit form + action buttons
        form_pane = tk.Frame(self._pw_recipe, bg="#0d1117")
        self._pw_recipe.add(form_pane, minsize=160, stretch="never")
        # btn_row reserved at the bottom first so it survives pane being resized small
        btn_row = tk.Frame(form_pane, bg="#0d1117")
        btn_row.pack(side="bottom", fill="x", pady=(4, 2))
        tk.Frame(form_pane, bg="#21262d", height=1).pack(fill="x", pady=(0, 6))
        form = tk.Frame(form_pane, bg="#0d1117")
        form.pack(fill="x")

        name_row = tk.Frame(form, bg="#0d1117")
        name_row.pack(fill="x", pady=(0, 4))
        tk.Label(
            name_row, text="Name:", bg="#0d1117", fg="#8b949e", font=("Segoe UI", 8)
        ).pack(side="left", padx=(0, 4))
        self._recipe_name_var = tk.StringVar()
        tk.Entry(
            name_row,
            textvariable=self._recipe_name_var,
            bg="#161b22",
            fg="#c9d1d9",
            insertbackground="#c9d1d9",
            relief="flat",
        ).pack(side="left", fill="x", expand=True, ipady=3, padx=(0, 8))
        tk.Label(
            name_row, text="Produces:", bg="#0d1117", fg="#8b949e", font=("Segoe UI", 8)
        ).pack(side="left", padx=(0, 4))
        self._recipe_output_var = tk.StringVar(value="1")
        tk.Entry(
            name_row,
            textvariable=self._recipe_output_var,
            width=5,
            bg="#161b22",
            fg="#c9d1d9",
            insertbackground="#c9d1d9",
            relief="flat",
        ).pack(side="left", ipady=3)

        item_row = tk.Frame(form, bg="#0d1117")
        item_row.pack(fill="x", pady=(0, 4))
        tk.Label(
            item_row, text="Item:", bg="#0d1117", fg="#8b949e", font=("Segoe UI", 8)
        ).pack(side="left", padx=(0, 4))
        self._recipe_item_var = tk.StringVar()
        self._recipe_item_cb = ttk.Combobox(
            item_row, textvariable=self._recipe_item_var, width=30
        )
        self._recipe_item_cb.pack(side="left", fill="x", expand=True)
        self._recipe_item_cb.bind(
            "<FocusIn>",
            lambda _e: self._recipe_item_cb.configure(values=get_all_output_names()),
        )
        _LiveDropdown(
            self._recipe_item_cb,
            pre_fn=lambda: self._recipe_item_cb.configure(
                values=get_all_output_names()
            ),
        )

        tk.Label(
            form, text="Ingredients:", bg="#0d1117", fg="#8b949e", font=("Segoe UI", 8)
        ).pack(anchor="w", pady=(0, 2))

        # scrollable ingredient rows
        ing_outer = tk.Frame(form, bg="#0d1117")
        ing_outer.pack(fill="x")
        self._ing_canvas = tk.Canvas(ing_outer, bg="#0d1117", highlightthickness=0, height=110)
        ing_vsb = ttk.Scrollbar(ing_outer, orient="vertical",
                                 style="Thin.Vertical.TScrollbar",
                                 command=self._ing_canvas.yview)
        self._ing_inner = tk.Frame(self._ing_canvas, bg="#0d1117")
        self._ing_canvas.pack(side="left", fill="x", expand=True)
        def _ing_yscroll(first, last):
            if float(first) <= 0.0 and float(last) >= 1.0:
                ing_vsb.pack_forget()
            else:
                ing_vsb.pack(side="right", fill="y", before=self._ing_canvas)
            ing_vsb.set(first, last)
        self._ing_canvas.configure(yscrollcommand=_ing_yscroll)
        self._ing_window = self._ing_canvas.create_window(
            (0, 0), window=self._ing_inner, anchor="nw"
        )
        self._ing_inner.bind(
            "<Configure>",
            lambda _e: self._ing_canvas.configure(
                scrollregion=self._ing_canvas.bbox("all") or (0, 0, 0, 0)
            ),
        )
        self._ing_canvas.bind(
            "<Configure>",
            lambda e: self._ing_canvas.itemconfig(self._ing_window, width=e.width),
        )
        self._ing_canvas.bind("<MouseWheel>", self._ing_scroll)
        self._ing_inner.bind("<MouseWheel>", self._ing_scroll)

        tk.Button(
            btn_row,
            text="+ Ingredient",
            command=self._add_ingredient_row,
            bg="#21262d",
            fg="#c9d1d9",
            relief="flat",
            bd=0,
            padx=8,
            font=("Segoe UI", 8),
        ).pack(side="left", padx=(0, 10))
        tk.Button(
            btn_row,
            text="Save",
            command=self.save_recipe_action,
            bg="#238636",
            fg="white",
            relief="flat",
            padx=10,
        ).pack(side="left", padx=2)
        tk.Button(
            btn_row,
            text="Clear",
            command=self.clear_recipe_form,
            bg="#21262d",
            fg="#c9d1d9",
            relief="flat",
            padx=10,
        ).pack(side="left", padx=2)
        tk.Button(
            btn_row,
            text="Delete",
            command=self.delete_recipe_action,
            bg="#da3633",
            fg="white",
            relief="flat",
            padx=10,
        ).pack(side="right", padx=(2, 18))

    def _all_ingredient_options(self):
        produced = get_all_output_names()
        resource_names = distinct_values("resource")
        ingredient_names = distinct_ingredient_names()
        return sorted(set(produced + resource_names + ingredient_names), key=str.lower)

    def _refresh_recipe_list(self):
        if not hasattr(self, "_recipe_combo"):
            return
        names = [n for _, n in get_all_recipes()]
        self._recipe_combo["values"] = names
        if self._recipe_var.get() not in names:
            self._recipe_var.set("")

    def _on_recipe_combo_select(self, _event=None):
        name = self._recipe_var.get()
        recipe_id = get_recipe_by_name(name)
        if recipe_id is None:
            return
        self._recipe_selected_id = recipe_id
        self._viewing_recipe_id = recipe_id
        self._usedin_recipe_id = recipe_id
        self._usedin_navigated_away = False
        self._recipe_name_var.set(name)
        oqty = get_recipe_output_qty(recipe_id)
        self._recipe_output_var.set(f"{oqty:g}")
        self._recipe_item_var.set(get_recipe_output_name(recipe_id))
        self._ing_inner.unbind("<Configure>")
        for child in self._ing_inner.winfo_children():
            child.destroy()
        self._ing_inner.bind(
            "<Configure>",
            lambda _e: self._ing_canvas.configure(
                scrollregion=self._ing_canvas.bbox("all") or (0, 0, 0, 0)
            ),
        )
        self._ing_canvas.configure(scrollregion=(0, 0, 0, 0))
        self._ing_rows = []
        for ing_name, qty in get_recipe_ingredients(recipe_id):
            self._add_ingredient_row(ing_name, qty)
        self._refresh_recipe_breakdown()

    def _ing_scroll(self, event):
        self._ing_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _add_ingredient_row(self, name="", qty=1):
        row_frame = tk.Frame(self._ing_inner, bg="#0d1117")
        row_frame.pack(fill="x", pady=1)
        name_var = tk.StringVar(value=str(name))
        qty_var = tk.StringVar(value=str(qty))
        name_cb = ttk.Combobox(row_frame, textvariable=name_var, width=24)
        name_cb["values"] = self._all_ingredient_options()
        name_cb.pack(side="left", padx=(0, 4))
        _LiveDropdown(
            name_cb,
            pre_fn=lambda cb=name_cb: cb.configure(
                values=self._all_ingredient_options()
            ),
        )
        qty_entry = tk.Entry(
            row_frame,
            textvariable=qty_var,
            width=7,
            bg="#161b22",
            fg="#c9d1d9",
            insertbackground="#c9d1d9",
            relief="flat",
        )
        qty_entry.pack(side="left", padx=(0, 4), ipady=2)
        row = {"name_var": name_var, "qty_var": qty_var, "frame": row_frame}

        def remove():
            self._ing_rows = [x for x in self._ing_rows if x is not row]
            row["frame"].destroy()

        rm_btn = tk.Button(
            row_frame,
            text="×",
            command=remove,
            bg="#0d1117",
            fg="#da3633",
            relief="flat",
            bd=0,
            font=("Segoe UI", 9),
        )
        rm_btn.pack(side="left")
        for w in (row_frame, name_cb, qty_entry, rm_btn):
            w.bind("<MouseWheel>", self._ing_scroll, add=True)
        self._ing_rows.append(row)
        # auto-scroll to bottom so the new row is visible
        self._ing_inner.update_idletasks()
        self._ing_canvas.configure(
            scrollregion=self._ing_canvas.bbox("all") or (0, 0, 0, 0)
        )
        self._ing_canvas.yview_moveto(1.0)

    def _insert_breakdown_node(self, parent_iid, node, recipe_id, path_parts, checked):
        name = node["name"]
        qty = node["qty"]
        used_recipe = node.get("recipe_name", name)
        path_key = "|".join(path_parts + [name])
        is_done = path_key in checked
        qty_str = f"{qty:g}"
        label = f"{qty_str}×  {name}"
        if used_recipe and used_recipe != name:
            label += f"  [{used_recipe}]"
        tag = "done" if is_done else "ingredient"
        img = self.img_checked if is_done else self.img_unchecked
        iid = self._recipe_breakdown_tree.insert(
            parent_iid, "end", text=label, image=img, open=False, tags=(tag,)
        )
        self._recipe_iid_info[iid] = {
            "type": "ingredient",
            "recipe_id": recipe_id,
            "path_key": path_key,
            "checked": is_done,
        }
        if node["children"]:
            for child in node["children"]:
                self._insert_breakdown_node(
                    iid, child, recipe_id, path_parts + [name], checked
                )
        elif not node["is_recipe"]:
            # raw resource leaf — show deposit locations
            locs = get_deposits_for_ingredient(name)
            for sector, system_name, planet, status in locs:
                parts = [p for p in (sector, system_name, planet) if p]
                loc_text = " / ".join(parts)
                if status and status not in ("Unknown", ""):
                    loc_text += f"  [{status}]"
                loc_iid = self._recipe_breakdown_tree.insert(
                    iid, "end", text=f"    📍 {loc_text}", tags=("location",)
                )
                self._recipe_iid_info[loc_iid] = {"type": "location"}
        # Alternate recipes for the same output — collapsed by default; click to select
        for alt in node.get("alts", []):
            alt_iid = self._recipe_breakdown_tree.insert(
                iid,
                "end",
                text=f"⟳  {alt['recipe_name']}  (alt — click to use)",
                open=False,
                tags=("alt_header",),
            )
            self._recipe_iid_info[alt_iid] = {
                "type": "alt_header",
                "ingredient_name": name,
                "alt_recipe_id": alt["recipe_id"],
            }
            for alt_child in alt["children"]:
                self._insert_breakdown_node(
                    alt_iid,
                    alt_child,
                    recipe_id,
                    path_parts + [f"~{alt['recipe_id']}~{name}"],
                    checked,
                )

    def _on_bd_toggled(self, _):
        self._bd_toggled = True

    def _load_recipe_into_form(self, rid: int, rname: str):
        self._recipe_selected_id = rid
        self._viewing_recipe_id = rid
        self._recipe_var.set(rname)
        self._recipe_combo.icursor("end")
        self._recipe_combo.selection_range(0, "end")
        self._recipe_name_var.set(rname)
        oqty = get_recipe_output_qty(rid)
        self._recipe_output_var.set(f"{oqty:g}")
        self._recipe_item_var.set(get_recipe_output_name(rid))
        for row in self._ing_rows:
            row["frame"].destroy()
        self._ing_rows.clear()
        for ing_name, qty in get_recipe_ingredients(rid):
            self._add_ingredient_row(ing_name, qty)

    def _on_breakdown_double_click(self, event):
        tree = self._recipe_breakdown_tree
        iid = tree.identify_row(event.y)
        if not iid:
            return
        info = self._recipe_iid_info.get(iid)
        if not info or info["type"] != "usedin_recipe":
            return
        self._usedin_navigated_away = True
        self._load_recipe_into_form(info["recipe_id"], info["recipe_name"])
        self._set_recipe_mode("breakdown")

    def _on_breakdown_click(self, event):
        tree = self._recipe_breakdown_tree
        iid = tree.identify_row(event.y)
        # Handle usedin_header before the toggle guard: clicking it also collapses
        # the node, which fires <<TreeviewClose>> and sets _bd_toggled before
        # ButtonRelease-1 arrives, so the guard would swallow the click otherwise.
        if iid:
            info = self._recipe_iid_info.get(iid)
            if info and info["type"] == "usedin_header":
                self._bd_toggled = False
                rid = info.get("recipe_id")
                rname = info.get("recipe_name", "")
                if rid is not None and rname:
                    self._load_recipe_into_form(rid, rname)
                return
        if self._bd_toggled:
            self._bd_toggled = False
            return
        if not iid:
            return
        info = self._recipe_iid_info.get(iid)
        if not info:
            return
        if info["type"] == "alt_header":
            set_alt_pref(info["ingredient_name"], info["alt_recipe_id"])
            self._refresh_recipe_breakdown()
            return
        if info["type"] == "usedin_recipe":
            self._load_recipe_into_form(info["recipe_id"], info["recipe_name"])
            return
        if info["type"] != "ingredient":
            return
        if "image" not in tree.identify("element", event.x, event.y):
            return
        recipe_id = self._viewing_recipe_id
        if recipe_id is None:
            return
        path_key = info["path_key"]
        is_done = info.get("checked", False)
        toggle_checked(recipe_id, path_key, currently_checked=is_done)
        new_done = not is_done
        info["checked"] = new_done
        tree.item(
            iid,
            image=self.img_checked if new_done else self.img_unchecked,
            tags=("done" if new_done else "ingredient",),
        )
        tree.tag_configure("ingredient", foreground="#c9d1d9")
        tree.tag_configure("done", foreground="#6e7681")

    def save_recipe_action(self):
        name = self._recipe_name_var.get().strip()
        if not name:
            messagebox.showwarning("Missing info", "Recipe name is required.")
            self.after(0, self._recipe_repaint)
            return
        ingredients = []
        for row in self._ing_rows:
            ing_name = row["name_var"].get().strip()
            qty_str = row["qty_var"].get().strip()
            if not ing_name:
                continue
            try:
                qty = float(qty_str)
            except ValueError:
                messagebox.showwarning(
                    "Invalid quantity", f"Invalid quantity for '{ing_name}'."
                )
                self.after(0, self._recipe_repaint)
                return
            ingredients.append((ing_name, qty))
        if not ingredients:
            messagebox.showwarning("Missing info", "Add at least one ingredient.")
            self.after(0, self._recipe_repaint)
            return
        existing_id = get_recipe_by_name(name)
        if existing_id is not None and existing_id != self._recipe_selected_id:
            messagebox.showwarning(
                "Duplicate", f"A recipe named '{name}' already exists."
            )
            self.after(0, self._recipe_repaint)
            return
        try:
            output_qty = float(self._recipe_output_var.get().strip() or "1")
            if output_qty <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning(
                "Invalid quantity", "Output quantity must be a positive number."
            )
            self.after(0, self._recipe_repaint)
            return
        output_name = self._recipe_item_var.get().strip() or name
        rid = save_recipe(
            self._recipe_selected_id, name, output_qty, ingredients, output_name
        )
        self._recipe_selected_id = rid
        self._viewing_recipe_id = rid
        self._recipe_var.set(name)
        self._refresh_recipe_list()
        self._refresh_recipe_breakdown()
        self.after(0, self._recipe_repaint)

    def _recipe_repaint(self):
        self._recipe_frame.pack_forget()
        self._recipe_frame.pack(fill="both", expand=True, padx=8, pady=4)
        self.focus_force()

    def delete_recipe_action(self):
        if self._recipe_selected_id is None:
            messagebox.showinfo("No selection", "Select a recipe first.")
            self.after(0, self._recipe_repaint)
            return
        if not messagebox.askyesno("Confirm delete", "Delete this recipe?"):
            self.after(0, self._recipe_repaint)
            return
        delete_recipe(self._recipe_selected_id)

        def _finish():
            self.clear_recipe_form()
            self._refresh_recipe_list()
            self._recipe_frame.pack_forget()
            self._recipe_frame.pack(fill="both", expand=True, padx=8, pady=4)
            self.focus_force()

        self.after(0, _finish)

    def clear_recipe_form(self):
        self._recipe_selected_id = None
        self._viewing_recipe_id = None
        if hasattr(self, "_recipe_name_var"):
            self._recipe_name_var.set("")
        if hasattr(self, "_recipe_output_var"):
            self._recipe_output_var.set("1")
        if hasattr(self, "_recipe_item_var"):
            self._recipe_item_var.set("")
        if hasattr(self, "_recipe_var"):
            self._recipe_var.set("")
        if hasattr(self, "_ing_inner"):
            self._ing_inner.unbind("<Configure>")
            for child in self._ing_inner.winfo_children():
                child.destroy()
            self._ing_inner.bind(
                "<Configure>",
                lambda _e: self._ing_canvas.configure(
                    scrollregion=self._ing_canvas.bbox("all") or (0, 0, 0, 0)
                ),
            )
            self._ing_canvas.configure(scrollregion=(0, 0, 0, 0))
        self._ing_rows = []
        if hasattr(self, "_recipe_breakdown_tree"):
            for item in self._recipe_breakdown_tree.get_children():
                self._recipe_breakdown_tree.delete(item)
        self._recipe_iid_info = {}

    def _set_recipe_mode(self, mode: str):
        self._recipe_breakdown_mode = mode
        if mode == "usedin":
            if not self._usedin_navigated_away:
                self._usedin_recipe_id = self._viewing_recipe_id
            else:
                if self._usedin_recipe_id is not None:
                    rname = get_recipe_name(self._usedin_recipe_id)
                    if rname:
                        self._load_recipe_into_form(self._usedin_recipe_id, rname)
            self._usedin_navigated_away = False
        for btn, key in (
            (self._btn_bd_breakdown, "breakdown"),
            (self._btn_bd_totals, "totals"),
            (self._btn_bd_usedin, "usedin"),
        ):
            btn.config(
                bg="#1f6feb" if mode == key else "#21262d",
                fg="white" if mode == key else "#8b949e",
            )
        self._refresh_recipe_breakdown()

    def _refresh_recipe_breakdown(self):
        tree = self._recipe_breakdown_tree
        for item in tree.get_children():
            tree.delete(item)
        self._recipe_iid_info = {}
        tree.tag_configure("root", foreground="#f0883e", font=("Segoe UI", 9, "bold"))
        tree.tag_configure(
            "total_header", foreground="#f0883e", font=("Segoe UI", 9, "bold")
        )
        tree.tag_configure(
            "section", foreground="#8b949e", font=("Segoe UI", 8, "italic")
        )
        tree.tag_configure("ingredient", foreground="#c9d1d9")
        tree.tag_configure("done", foreground="#6e7681")
        tree.tag_configure("location", foreground="#3fb950", font=("Segoe UI", 8))
        tree.tag_configure(
            "alt_header", foreground="#8b949e", font=("Segoe UI", 8, "italic")
        )
        if self._recipe_breakdown_mode == "usedin":
            self._refresh_usedin_view(tree)
            return
        recipe_id = self._viewing_recipe_id
        if recipe_id is None:
            return
        output_name = get_recipe_output_name(recipe_id)
        if not output_name:
            return
        try:
            craft_qty = max(float(self._recipe_qty_var.get()), 0.001)
        except ValueError:
            craft_qty = 1.0
        checked = get_checked_paths(recipe_id)
        alt_prefs = get_alt_prefs()
        node = resolve_recipe_tree(
            output_name,
            qty_needed=craft_qty,
            _root_recipe_id=recipe_id,
            _alt_prefs=alt_prefs,
        )
        if self._recipe_breakdown_mode == "totals":
            self._refresh_totals_view(
                tree, output_name, node, recipe_id, checked, craft_qty
            )
        else:
            oqty = node.get("output_qty", 1.0)
            crafts = math.ceil(craft_qty / oqty)
            if craft_qty == 1.0:
                root_label = (
                    f"◆  {output_name}  ×{oqty:g}" if oqty > 1 else f"◆  {output_name}"
                )
            else:
                root_label = f"◆  {output_name}  ×{craft_qty:g}"
                if crafts > 1 or oqty > 1:
                    root_label += f"  ({crafts:g} crafts)"
            root_iid = tree.insert(
                "", "end", text=root_label, open=True, tags=("root",)
            )
            self._recipe_iid_info[root_iid] = {"type": "root"}
            for child in node["children"]:
                self._insert_breakdown_node(root_iid, child, recipe_id, [], checked)

    def _refresh_usedin_view(self, tree):
        view_id = self._usedin_recipe_id
        item_name = (
            get_recipe_output_name(view_id)
            if view_id is not None
            else None
        ) or ""
        if not item_name:
            tree.insert("", "end", text="Select a recipe above to see where it's used.", tags=("section",))
            return
        rows = get_recipes_using_ingredient(item_name)
        header = tree.insert(
            "",
            "end",
            text=f'Recipes using  "{item_name}"',
            open=True,
            tags=("root",),
        )
        self._recipe_iid_info[header] = {
            "type": "usedin_header",
            "recipe_id": view_id,
            "recipe_name": get_recipe_name(view_id),
        }
        if not rows:
            tree.insert(header, "end", text="  (none found)", tags=("section",))
            return
        for rid, rname, qty, output_name, output_qty in rows:
            label = f"×{qty:g}  →  {rname}"
            if output_name != rname:
                oq_suffix = f"  ×{output_qty:g}" if output_qty != 1 else ""
                label += f"  [{output_name}{oq_suffix}]"
            iid = tree.insert(
                header, "end", text=label, open=False, tags=("ingredient",)
            )
            self._recipe_iid_info[iid] = {
                "type": "usedin_recipe",
                "recipe_id": rid,
                "recipe_name": rname,
            }

    @staticmethod
    def collect_totals(node, totals=None):
        """Aggregate raw-resource leaf quantities across the whole tree."""
        if totals is None:
            totals = {}
        if not node["is_recipe"] and not node["children"]:
            totals[node["name"]] = totals.get(node["name"], 0) + node["qty"]
        for child in node["children"]:
            Overlay.collect_totals(child, totals)
        return totals

    @staticmethod
    def collect_intermediates(node, totals=None):
        """Aggregate sub-recipe quantities across the whole tree (excluding root)."""
        if totals is None:
            totals = {}
        for child in node["children"]:
            if child["is_recipe"]:
                entry = totals.setdefault(
                    child["name"],
                    {
                        "qty": 0.0,
                        "output_qty": child.get("output_qty", 1.0),
                        "alts": child.get("alts", []),
                    },
                )
                entry["qty"] += child["qty"]
            Overlay.collect_intermediates(child, totals)
        return totals

    def _refresh_totals_view(
        self, tree, recipe_name, node, recipe_id, checked, craft_qty=1.0
    ):
        oqty = node.get("output_qty", 1.0)
        if craft_qty == 1.0:
            root_label = (
                f"◆  {recipe_name}  ×{oqty:g}" if oqty > 1 else f"◆  {recipe_name}"
            )
        else:
            crafts = math.ceil(craft_qty / oqty)
            root_label = f"◆  {recipe_name}  ×{craft_qty:g}"
            if crafts > 1 or oqty > 1:
                root_label += f"  ({crafts:g} crafts)"
        header = tree.insert(
            "", "end", text=root_label, open=True, tags=("total_header",)
        )
        self._recipe_iid_info[header] = {"type": "root"}

        def insert_raw(parent, res_name, qty, path_key):
            is_done = path_key in checked
            img = self.img_checked if is_done else self.img_unchecked
            iid = tree.insert(
                parent,
                "end",
                text=f"{qty:g}×  {res_name}",
                image=img,
                open=True,
                tags=("done" if is_done else "ingredient",),
            )
            self._recipe_iid_info[iid] = {
                "type": "ingredient",
                "recipe_id": recipe_id,
                "path_key": path_key,
                "checked": is_done,
            }
            for sector, system_name, planet, status in get_deposits_for_ingredient(
                res_name
            ):
                parts = [p for p in (sector, system_name, planet) if p]
                loc_text = " / ".join(parts)
                if status and status not in ("Unknown", ""):
                    loc_text += f"  [{status}]"
                loc_iid = tree.insert(
                    iid, "end", text=f"    📍 {loc_text}", tags=("location",)
                )
                self._recipe_iid_info[loc_iid] = {"type": "location"}

        intermediates = self.collect_intermediates(node)
        if intermediates:
            craft_hdr = tree.insert(
                header, "end", text="── Crafted ──", open=True, tags=("section",)
            )
            self._recipe_iid_info[craft_hdr] = {"type": "root"}
            for res_name, info in sorted(
                intermediates.items(), key=lambda x: x[0].lower()
            ):
                qty = info["qty"]
                oq = info["output_qty"]
                crafts = math.ceil(qty / oq)
                path_key = f"__craft__|{res_name}"
                is_done = path_key in checked
                img = self.img_checked if is_done else self.img_unchecked
                suffix = f"  ({crafts:g} crafts)" if oq > 1 else ""
                iid = tree.insert(
                    craft_hdr,
                    "end",
                    text=f"{qty:g}×  {res_name}{suffix}",
                    image=img,
                    open=True,
                    tags=("done" if is_done else "ingredient",),
                )
                self._recipe_iid_info[iid] = {
                    "type": "ingredient",
                    "recipe_id": recipe_id,
                    "path_key": path_key,
                    "checked": is_done,
                }
                for alt in info.get("alts", []):
                    alt_iid = tree.insert(
                        iid,
                        "end",
                        text=f"⟳  {alt['recipe_name']}  (alt — click to use)",
                        open=False,
                        tags=("alt_header",),
                    )
                    self._recipe_iid_info[alt_iid] = {
                        "type": "alt_header",
                        "ingredient_name": res_name,
                        "alt_recipe_id": alt["recipe_id"],
                    }

        raw_hdr = tree.insert(
            header, "end", text="── Raw materials ──", open=True, tags=("section",)
        )
        self._recipe_iid_info[raw_hdr] = {"type": "root"}
        for res_name, qty in sorted(
            self.collect_totals(node).items(), key=lambda x: x[0].lower()
        ):
            insert_raw(raw_hdr, res_name, qty, f"__total__|{res_name}")


def _make_tray_image():
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse(
        [2, 2, 61, 61], fill=(13, 17, 23, 255), outline=(31, 111, 235, 255), width=3
    )
    draw.ellipse([20, 20, 43, 43], fill=(31, 111, 235, 255))
    draw.arc([8, 30, 55, 46], start=0, end=180, fill=(201, 209, 217, 220), width=2)
    draw.arc([8, 30, 55, 46], start=180, end=360, fill=(201, 209, 217, 90), width=2)
    return img


def main():
    # Prevent multiple instances via a Windows named mutex.
    ctypes.windll.kernel32.CreateMutexW(None, True, "CraftMapOverlay_SingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        _r = tk.Tk()
        _r.withdraw()
        messagebox.showwarning("CraftMap", "CraftMap is already running.")
        _r.destroy()
        return

    init_db()
    app = Overlay()

    if HOTKEY_AVAILABLE:

        def hotkey_thread():
            app.register_hotkey()
            keyboard.wait()

        t = threading.Thread(target=hotkey_thread, daemon=True)
        t.start()
    else:
        print("NOTE: 'keyboard' module not found, global hotkey disabled.")
        print("Install with: pip install keyboard --break-system-packages")
        print(
            "Use the on-screen ✕ button or Esc key (while focused) to hide the window."
        )

    if PYSTRAY_AVAILABLE:
        menu = pystray.Menu(
            pystray.MenuItem(
                "Show / Hide", lambda: app.after(0, app.toggle), default=True
            ),
            pystray.MenuItem(
                "Craft Queue", lambda: app.after(0, app.toggle_queue_panel)
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", lambda: app.after(0, app.quit_app)),
        )
        _icon = pystray.Icon("CraftMap", _make_tray_image(), "CraftMap", menu)
        app.tray_icon = _icon
        threading.Thread(target=_icon.run, daemon=True).start()

    app.mainloop()


if __name__ == "__main__":
    main()
