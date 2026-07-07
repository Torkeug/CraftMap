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
from tkinter import font as tkfont
import sqlite3
import os
import sys
import math
import threading
import datetime
import weakref

import win32util


def _relaunch_via_pythonw_if_needed():
    """Suppress the console window by re-exec'ing under pythonw.exe.
    Guarded behind __main__ (not run at import time) so importing this
    module - e.g. from a test suite - doesn't spawn a second process and
    exit."""
    if (
        sys.platform == "win32"
        and not getattr(sys, "frozen", False)
        and not sys.executable.lower().endswith("pythonw.exe")
    ):
        import shutil
        import subprocess

        pythonw = shutil.which("pythonw.exe") or shutil.which("pythonw")
        if pythonw:
            subprocess.Popen([pythonw, os.path.abspath(__file__)] + sys.argv[1:])
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
    if "station" not in recipe_cols:
        c.execute("ALTER TABLE recipes ADD COLUMN station TEXT")
    if "auto_craft_seconds" not in recipe_cols:
        c.execute("ALTER TABLE recipes ADD COLUMN auto_craft_seconds REAL")
    if "manual_craft_seconds" not in recipe_cols:
        c.execute("ALTER TABLE recipes ADD COLUMN manual_craft_seconds REAL")
    if "game_craft_id" not in recipe_cols:
        c.execute("ALTER TABLE recipes ADD COLUMN game_craft_id TEXT")
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
        CREATE TABLE IF NOT EXISTS recipe_outputs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER NOT NULL,
            item_name TEXT NOT NULL,
            quantity REAL NOT NULL DEFAULT 1,
            FOREIGN KEY (recipe_id) REFERENCES recipes(id)
        )
    """)
    # backfill: every pre-existing recipe needs at least one recipe_outputs row,
    # mirroring its old single output_qty/output_name columns
    c.execute(
        "SELECT id, COALESCE(output_name, name), output_qty FROM recipes"
        " WHERE id NOT IN (SELECT DISTINCT recipe_id FROM recipe_outputs)"
    )
    for rid, oname, oqty in c.fetchall():
        c.execute(
            "INSERT INTO recipe_outputs (recipe_id, item_name, quantity) VALUES (?, ?, ?)",
            (rid, oname, oqty),
        )
    c.execute("""
        CREATE TABLE IF NOT EXISTS recipe_stations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER NOT NULL,
            station TEXT NOT NULL,
            auto_craft_seconds REAL,
            manual_craft_seconds REAL,
            FOREIGN KEY (recipe_id) REFERENCES recipes(id)
        )
    """)
    # backfill: every pre-existing recipe with a station needs at least one
    # recipe_stations row, mirroring its old single station/*_craft_seconds columns
    c.execute(
        "SELECT id, station, auto_craft_seconds, manual_craft_seconds FROM recipes"
        " WHERE station IS NOT NULL AND station != ''"
        " AND id NOT IN (SELECT DISTINCT recipe_id FROM recipe_stations)"
    )
    for rid, station, auto_s, manual_s in c.fetchall():
        c.execute(
            "INSERT INTO recipe_stations (recipe_id, station, auto_craft_seconds, manual_craft_seconds)"
            " VALUES (?, ?, ?, ?)",
            (rid, station, auto_s, manual_s),
        )
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
        CREATE TABLE IF NOT EXISTS recipe_station_prefs (
            ingredient_name TEXT PRIMARY KEY,
            station TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'auto'
        )
    """)
    c.execute("PRAGMA table_info(recipe_station_prefs)")
    if "mode" not in [row[1] for row in c.fetchall()]:
        c.execute(
            "ALTER TABLE recipe_station_prefs ADD COLUMN mode TEXT NOT NULL DEFAULT 'auto'"
        )
    c.execute("""
        CREATE TABLE IF NOT EXISTS craft_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER NOT NULL,
            quantity REAL NOT NULL DEFAULT 1,
            FOREIGN KEY (recipe_id) REFERENCES recipes(id)
        )
    """)
    c.execute("PRAGMA table_info(craft_queue)")
    queue_cols = [row[1] for row in c.fetchall()]
    if "station" not in queue_cols:
        c.execute("ALTER TABLE craft_queue ADD COLUMN station TEXT")
    if "combine" not in queue_cols:
        c.execute("ALTER TABLE craft_queue ADD COLUMN combine INTEGER NOT NULL DEFAULT 1")
    if "station_mode" not in queue_cols:
        c.execute(
            "ALTER TABLE craft_queue ADD COLUMN station_mode TEXT NOT NULL DEFAULT 'auto'"
        )
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
    recipe that uses ingredient_name. output_name/output_qty are the recipe's
    primary (first) output."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT r.id, r.name, ri.quantity, ro.item_name, ro.quantity"
        " FROM recipe_ingredients ri"
        " JOIN recipes r ON r.id = ri.recipe_id"
        " JOIN recipe_outputs ro ON ro.recipe_id = r.id"
        " WHERE ri.ingredient_name = ?"
        " AND ro.id = (SELECT MIN(id) FROM recipe_outputs WHERE recipe_id = r.id)"
        " ORDER BY r.name COLLATE NOCASE",
        (ingredient_name,),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def save_recipe(
    recipe_id,
    name,
    outputs,
    ingredients,
    stations,
):
    """Insert (recipe_id=None) or update a recipe, replacing its outputs,
    ingredients, and stations. `outputs` is a non-empty list of
    (item_name, qty) tuples; outputs[0] is the primary output. `stations`
    is a non-empty list of (station, auto_craft_seconds, manual_craft_seconds)
    tuples; stations[0] is the primary station. Returns id."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    primary_name, primary_qty = outputs[0]
    oname = primary_name if primary_name != name else None
    primary_station, primary_auto_s, primary_manual_s = stations[0]
    if recipe_id is None:
        c.execute(
            "INSERT INTO recipes"
            " (name, output_qty, output_name, station, auto_craft_seconds, manual_craft_seconds)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                name,
                primary_qty,
                oname,
                primary_station,
                primary_auto_s,
                primary_manual_s,
            ),
        )
        recipe_id = c.lastrowid
    else:
        c.execute(
            "UPDATE recipes SET name=?, output_qty=?, output_name=?,"
            " station=?, auto_craft_seconds=?, manual_craft_seconds=? WHERE id=?",
            (
                name,
                primary_qty,
                oname,
                primary_station,
                primary_auto_s,
                primary_manual_s,
                recipe_id,
            ),
        )
        c.execute("DELETE FROM recipe_ingredients WHERE recipe_id=?", (recipe_id,))
        c.execute("DELETE FROM recipe_outputs WHERE recipe_id=?", (recipe_id,))
        c.execute("DELETE FROM recipe_stations WHERE recipe_id=?", (recipe_id,))
    for ing_name, qty in ingredients:
        c.execute(
            "INSERT INTO recipe_ingredients (recipe_id, ingredient_name, quantity)"
            " VALUES (?, ?, ?)",
            (recipe_id, ing_name, qty),
        )
    for out_name, out_qty in outputs:
        c.execute(
            "INSERT INTO recipe_outputs (recipe_id, item_name, quantity)"
            " VALUES (?, ?, ?)",
            (recipe_id, out_name, out_qty),
        )
    for st_name, st_auto_s, st_manual_s in stations:
        c.execute(
            "INSERT INTO recipe_stations"
            " (recipe_id, station, auto_craft_seconds, manual_craft_seconds)"
            " VALUES (?, ?, ?, ?)",
            (recipe_id, st_name, st_auto_s, st_manual_s),
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
    """The recipe's primary (first) output item name."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT item_name FROM recipe_outputs WHERE recipe_id=? ORDER BY id LIMIT 1",
        (recipe_id,),
    )
    row = c.fetchone()
    conn.close()
    return row[0] if row else ""


def get_all_output_names():
    """Distinct item names that recipes produce (including secondary/byproduct
    outputs), for autocomplete."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT DISTINCT item_name FROM recipe_outputs ORDER BY 1 COLLATE NOCASE")
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_recipe_output_qty(recipe_id):
    """The recipe's primary (first) output quantity."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT quantity FROM recipe_outputs WHERE recipe_id=? ORDER BY id LIMIT 1",
        (recipe_id,),
    )
    row = c.fetchone()
    conn.close()
    return float(row[0]) if row else 1.0


def get_recipe_outputs(recipe_id):
    """All of a recipe's outputs, ordered with the primary first."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT item_name, quantity FROM recipe_outputs"
        " WHERE recipe_id=? ORDER BY id",
        (recipe_id,),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def get_recipe_meta(recipe_id):
    """Return (station, auto_craft_seconds, manual_craft_seconds) for a
    recipe's primary station."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT station, auto_craft_seconds, manual_craft_seconds"
        " FROM recipes WHERE id=?",
        (recipe_id,),
    )
    row = c.fetchone()
    conn.close()
    return row if row else (None, None, None)


def get_recipe_stations(recipe_id):
    """All of a recipe's usable stations, ordered with the primary first."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT station, auto_craft_seconds, manual_craft_seconds"
        " FROM recipe_stations WHERE recipe_id=? ORDER BY id",
        (recipe_id,),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def get_recipe_station_times(recipe_id, station):
    """Return (auto_craft_seconds, manual_craft_seconds) for one of a
    recipe's stations by name, or None if that recipe has no such station."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT auto_craft_seconds, manual_craft_seconds FROM recipe_stations"
        " WHERE recipe_id=? AND station=? ORDER BY id LIMIT 1",
        (recipe_id, station),
    )
    row = c.fetchone()
    conn.close()
    return tuple(row) if row else None


def get_all_stations():
    """Distinct craft stations already in use, for autocomplete - no hardcoded
    lists, grows automatically as recipes are tagged with a station."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT DISTINCT station FROM recipe_stations ORDER BY station COLLATE NOCASE"
    )
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]


def delete_recipe(recipe_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM recipe_ingredients WHERE recipe_id=?", (recipe_id,))
    c.execute("DELETE FROM recipe_outputs WHERE recipe_id=?", (recipe_id,))
    c.execute("DELETE FROM recipe_stations WHERE recipe_id=?", (recipe_id,))
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


def set_checked_many(recipe_id, path_keys, checked):
    """Set (not toggle) every path_key in path_keys to the same checked
    state in one go - used to cascade a step's checkbox onto its whole
    subtree instead of toggling each descendant individually."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if checked:
        c.executemany(
            "INSERT OR REPLACE INTO recipe_checked (recipe_id, path_key) VALUES (?, ?)",
            [(recipe_id, pk) for pk in path_keys],
        )
    else:
        c.executemany(
            "DELETE FROM recipe_checked WHERE recipe_id=? AND path_key=?",
            [(recipe_id, pk) for pk in path_keys],
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


def get_station_prefs():
    """Return {ingredient_name: (station, mode)} of user-chosen preferred
    crafting stations and craft mode ('auto'/'manual'), same idea as
    get_alt_prefs but for which station/mode (rather than which alternate
    recipe) to use for an ingredient."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT ingredient_name, station, mode FROM recipe_station_prefs")
    prefs = {name: (station, mode) for name, station, mode in c.fetchall()}
    conn.close()
    return prefs


def set_station_pref(ingredient_name, station, mode="auto"):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO recipe_station_prefs (ingredient_name, station, mode)"
        " VALUES (?, ?, ?)",
        (ingredient_name, station, mode),
    )
    conn.commit()
    conn.close()


def clear_station_pref(ingredient_name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "DELETE FROM recipe_station_prefs WHERE ingredient_name=?", (ingredient_name,)
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
    """Return [(queue_id, recipe_id, recipe_name, output_name, quantity,
    station, combine, station_mode), ...]. output_name is the recipe's
    primary (first) output. station is the station chosen for this job
    (None = the recipe's primary/default station); station_mode is which of
    that station's auto/manual times to use. combine is whether this job's
    numbers count toward the Totals view's combined "All Jobs" aggregate."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT cq.id, cq.recipe_id, r.name, ro.item_name, cq.quantity,"
        " cq.station, cq.combine, cq.station_mode"
        " FROM craft_queue cq"
        " JOIN recipes r ON r.id = cq.recipe_id"
        " JOIN recipe_outputs ro ON ro.recipe_id = r.id"
        " WHERE ro.id = (SELECT MIN(id) FROM recipe_outputs WHERE recipe_id = r.id)"
        " ORDER BY cq.id"
    )
    rows = c.fetchall()
    conn.close()
    return rows


def add_to_queue(recipe_id, quantity=1.0, station=None):
    """Add a job, merging into an existing queue entry for the same recipe
    AND station (bumping its quantity) instead of creating a duplicate row -
    queuing a recipe/station that's already queued should read as "craft
    more of it", not a second identical entry, and this also preserves that
    entry's checked ingredient state instead of resetting it in a fresh row.
    The same recipe queued at a *different* station is a distinct job."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, quantity FROM craft_queue WHERE recipe_id=? AND station IS ?",
        (recipe_id, station),
    )
    existing = c.fetchone()
    if existing:
        queue_id, existing_qty = existing
        c.execute(
            "UPDATE craft_queue SET quantity=? WHERE id=?",
            (existing_qty + quantity, queue_id),
        )
    else:
        c.execute(
            "INSERT INTO craft_queue (recipe_id, quantity, station) VALUES (?, ?, ?)",
            (recipe_id, quantity, station),
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


def update_queue_station(queue_id, station, mode="auto"):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE craft_queue SET station=?, station_mode=? WHERE id=?",
        (station, mode, queue_id),
    )
    conn.commit()
    conn.close()


def update_queue_combine(queue_id, combine):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE craft_queue SET combine=? WHERE id=?", (1 if combine else 0, queue_id)
    )
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


def clear_queue_checked(queue_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM queue_checked WHERE queue_id=?", (queue_id,))
    conn.commit()
    conn.close()


def set_queue_checked_many(queue_id, path_keys, checked):
    """Set (not toggle) every path_key in path_keys to the same checked
    state in one go - used to cascade a step's checkbox onto its whole
    subtree instead of toggling each descendant individually."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if checked:
        c.executemany(
            "INSERT OR REPLACE INTO queue_checked (queue_id, path_key) VALUES (?, ?)",
            [(queue_id, pk) for pk in path_keys],
        )
    else:
        c.executemany(
            "DELETE FROM queue_checked WHERE queue_id=? AND path_key=?",
            [(queue_id, pk) for pk in path_keys],
        )
    conn.commit()
    conn.close()


# ---------- Recipe tree resolution ----------


def _load_recipe_data():
    """Load all recipes, outputs, and ingredients in a few queries."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, name FROM recipes")
    recipe_name_by_id = {rid: rname for rid, rname in c.fetchall()}
    c.execute(
        "SELECT id, station, auto_craft_seconds, manual_craft_seconds FROM recipes"
    )
    recipe_meta_by_id = {
        rid: {
            "station": station,
            "auto_craft_seconds": auto_s,
            "manual_craft_seconds": manual_s,
        }
        for rid, station, auto_s, manual_s in c.fetchall()
    }
    # Order by recipe id ASC so the first (oldest) recipe for each output item
    # is the default.
    c.execute(
        "SELECT ro.recipe_id, ro.item_name, ro.quantity"
        " FROM recipe_outputs ro JOIN recipes r ON r.id = ro.recipe_id"
        " ORDER BY r.id ASC, ro.id ASC"
    )
    recipe_map = {}  # item_name / recipe_name → first recipe_id producing it
    outputs_by_recipe = {}  # recipe_id → [(item_name, qty), ...], index 0 = primary
    alts_by_output = {}  # item_name → [(rid, recipe_name, qty_for_that_item), ...]
    for rid, item_name, qty in c.fetchall():
        outputs_by_recipe.setdefault(rid, []).append((item_name, float(qty)))
        rname = recipe_name_by_id.get(rid, item_name)
        alts_by_output.setdefault(item_name, []).append((rid, rname, float(qty)))
        if item_name not in recipe_map:  # first by id wins as default
            recipe_map[item_name] = rid
    # Also index by recipe name so ingredients can reference alternates by name
    for rid, rname in recipe_name_by_id.items():
        if rname not in recipe_map:
            recipe_map[rname] = rid
    c.execute(
        "SELECT recipe_id, ingredient_name, quantity FROM recipe_ingredients ORDER BY id"
    )
    ing_map: dict = {}
    for rid, ing_name, qty in c.fetchall():
        ing_map.setdefault(rid, []).append((ing_name, qty))
    c.execute(
        "SELECT recipe_id, station, auto_craft_seconds, manual_craft_seconds"
        " FROM recipe_stations ORDER BY id"
    )
    stations_by_recipe: dict = {}
    for rid, station, auto_s, manual_s in c.fetchall():
        stations_by_recipe.setdefault(rid, []).append((station, auto_s, manual_s))
    conn.close()
    return (
        recipe_map,
        ing_map,
        outputs_by_recipe,
        alts_by_output,
        recipe_name_by_id,
        recipe_meta_by_id,
        stations_by_recipe,
    )


def resolve_recipe_tree(
    name,
    qty_needed=1.0,
    _visited=None,
    _recipe_map=None,
    _ing_map=None,
    _outputs_by_recipe=None,
    _root_recipe_id=None,
    _alts_by_output=None,
    _recipe_name_by_id=None,
    _recipe_meta_by_id=None,
    _alt_prefs=None,
    _stations_by_recipe=None,
    _station_prefs=None,
):
    """
    Recursively build a breakdown tree for `name`.
    Returns: {'name', 'qty', 'is_recipe', 'output_qty', 'recipe_name', 'children',
              'alts', 'byproducts', 'station', 'auto_craft_seconds',
              'manual_craft_seconds', 'stations'}
    'alts' lists every other recipe producing the same output — shown as collapsible branches.
    'byproducts' lists this recipe's other outputs (besides `name`), scaled to
    the same craft count — populated for multi-output recipes.
    'stations' lists every usable station for this node's recipe (station,
    auto_craft_seconds, manual_craft_seconds), so the UI can offer a picker.
    _root_recipe_id: forces a specific recipe at the top level (for alternate recipe views).
    _alt_prefs: {ingredient_name: recipe_id} of user-selected alternate recipes.
    _station_prefs: {ingredient_name: station} of user-selected preferred stations.
    """
    if _recipe_map is None or _ing_map is None or _outputs_by_recipe is None:
        (
            _recipe_map,
            _ing_map,
            _outputs_by_recipe,
            _alts_by_output,
            _recipe_name_by_id,
            _recipe_meta_by_id,
            _stations_by_recipe,
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
    byproducts = []
    output_qty = 1.0
    used_recipe_name = name
    station = None
    auto_craft_seconds = None
    manual_craft_seconds = None
    craft_mode = "auto"
    stations: list = []
    if is_recipe:
        recipe_outputs = _outputs_by_recipe.get(recipe_id, [(name, 1.0)])
        output_names = [n for n, _ in recipe_outputs]
        actual_output = name if name in output_names else output_names[0]
        output_qty = next(q for n, q in recipe_outputs if n == actual_output)
        used_recipe_name = (_recipe_name_by_id or {}).get(recipe_id, name)
        meta = (_recipe_meta_by_id or {}).get(recipe_id, {})
        station = meta.get("station")
        auto_craft_seconds = meta.get("auto_craft_seconds")
        manual_craft_seconds = meta.get("manual_craft_seconds")
        craft_mode = "auto" if auto_craft_seconds else "manual"
        stations = (_stations_by_recipe or {}).get(recipe_id, [])
        pref = (_station_prefs or {}).get(name)
        pref_station, pref_mode = pref if pref else (None, None)
        if pref_station:
            for st_name, st_auto, st_manual in stations:
                if st_name == pref_station:
                    station, auto_craft_seconds, manual_craft_seconds = (
                        st_name,
                        st_auto,
                        st_manual,
                    )
                    craft_mode = pref_mode or ("auto" if st_auto else "manual")
                    break
        crafts = math.ceil(qty_needed / output_qty)
        byproducts = [
            {"name": n, "qty": crafts * q}
            for n, q in recipe_outputs
            if n != actual_output
        ]
        sub_visited = _visited | {name}
        for ing_name, ing_qty in _ing_map.get(recipe_id, []):
            child = resolve_recipe_tree(
                ing_name,
                crafts * ing_qty,
                sub_visited,
                _recipe_map,
                _ing_map,
                _outputs_by_recipe,
                _alts_by_output=_alts_by_output,
                _recipe_name_by_id=_recipe_name_by_id,
                _recipe_meta_by_id=_recipe_meta_by_id,
                _alt_prefs=_alt_prefs,
                _stations_by_recipe=_stations_by_recipe,
                _station_prefs=_station_prefs,
            )
            children.append(child)
        # Find every other recipe that produces the same output
        for alt_rid, alt_rname, alt_oqty in (_alts_by_output or {}).get(
            actual_output, []
        ):
            if alt_rid == recipe_id:
                continue
            alt_crafts = math.ceil(qty_needed / alt_oqty)
            alt_outputs = _outputs_by_recipe.get(alt_rid, [(actual_output, alt_oqty)])
            alt_byproducts = [
                {"name": n, "qty": alt_crafts * q}
                for n, q in alt_outputs
                if n != actual_output
            ]
            alt_children = []
            for ing_name, ing_qty in _ing_map.get(alt_rid, []):
                alt_child = resolve_recipe_tree(
                    ing_name,
                    alt_crafts * ing_qty,
                    sub_visited,
                    _recipe_map,
                    _ing_map,
                    _outputs_by_recipe,
                    _alts_by_output=_alts_by_output,
                    _recipe_name_by_id=_recipe_name_by_id,
                    _recipe_meta_by_id=_recipe_meta_by_id,
                    _alt_prefs=_alt_prefs,
                    _stations_by_recipe=_stations_by_recipe,
                    _station_prefs=_station_prefs,
                )
                alt_children.append(alt_child)
            alts.append(
                {
                    "recipe_id": alt_rid,
                    "recipe_name": alt_rname,
                    "output_qty": alt_oqty,
                    "children": alt_children,
                    "byproducts": alt_byproducts,
                    "stations": (_stations_by_recipe or {}).get(alt_rid, []),
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
        "byproducts": byproducts,
        "station": station,
        "auto_craft_seconds": auto_craft_seconds,
        "manual_craft_seconds": manual_craft_seconds,
        "craft_mode": craft_mode,
        "stations": stations,
    }


def _node_crafts(node):
    """Number of separate craft cycles needed to cover node['qty'], given its
    own output_qty per craft. 0 for raw (non-recipe) nodes."""
    if not node.get("is_recipe"):
        return 0
    return math.ceil(node["qty"] / node.get("output_qty", 1.0))


def _node_active_seconds(node):
    """(seconds, mode) for this node's currently active craft mode - the
    per-craft time, not yet scaled by how many crafts are needed."""
    mode = node.get("craft_mode", "auto")
    seconds = (
        node.get("auto_craft_seconds")
        if mode == "auto"
        else node.get("manual_craft_seconds")
    )
    return seconds, mode


def _node_own_time(node):
    """Total seconds this node's own craft step takes across every craft it
    needs (per-craft time x crafts needed) - 0 for raw nodes or ones with no
    timing data. This is the number that was previously shown un-scaled,
    which made e.g. '4x Titanium Part Casing' look like it only took one
    casing's craft time instead of four."""
    seconds, _mode = _node_active_seconds(node)
    if not seconds:
        return 0.0
    return seconds * _node_crafts(node)


def _node_path_key(node, path_parts):
    return "|".join(path_parts + [node["name"]])


def _subtree_remaining_seconds(node, path_parts, checked):
    """Sum of _node_own_time across this node and every descendant. A
    checked path_key means its whole subtree is considered done (its own
    time, and everything under it, drops out) rather than just itself."""
    if _node_path_key(node, path_parts) in checked:
        return 0.0
    total = _node_own_time(node)
    for child in node["children"]:
        total += _subtree_remaining_seconds(child, path_parts + [node["name"]], checked)
    return total


def _collect_path_keys(node, path_parts):
    """Every path_key in this node's own subtree, including itself - matches
    the scheme used when inserting breakdown-tree rows, so checking a step
    can cascade the same checked state onto everything it depends on."""
    keys = [_node_path_key(node, path_parts)]
    for child in node["children"]:
        keys.extend(_collect_path_keys(child, path_parts + [node["name"]]))
    return keys


def _node_has_step_options(node):
    """Whether this node has an alternate recipe or more than one usable
    (station, mode) combination - i.e. whether its _StepPopup would show
    anything at all."""
    if not node.get("is_recipe"):
        return False
    if node.get("alts"):
        return True
    modes_available = sum(
        (1 if st_auto else 0) + (1 if st_manual else 0)
        for _name, st_auto, st_manual in node.get("stations", [])
    )
    return modes_available > 1


# ---------- UI ----------

# Subtle border for the flat-styled custom widgets (tk.Entry/tk.Button don't
# get one from the OS theme the way ttk widgets do) - gives them a more
# "finished" look than the borderless relief="flat" default. Entries light up
# with the accent color on focus; buttons keep a static border regardless.
_BORDER = "#30363d"
_BORDER_FOCUS = "#388bfd"


def _bordered_entry(parent, **kwargs):
    kwargs.setdefault("highlightthickness", 1)
    kwargs.setdefault("highlightbackground", _BORDER)
    kwargs.setdefault("highlightcolor", _BORDER_FOCUS)
    return tk.Entry(parent, **kwargs)


def _strip_focus(layout):
    """Drop the dashed keyboard-focus rectangle ttk draws inside a button,
    splicing its children (the padding/label) up into its place instead of
    just deleting them."""
    result = []
    for name, opts in layout:
        new_opts = dict(opts)
        if "children" in new_opts:
            new_opts["children"] = _strip_focus(new_opts["children"])
        if "focus" in name.lower():
            result.extend(new_opts.get("children", []))
        else:
            result.append((name, new_opts))
    return result


def _configure_button_styles(style: ttk.Style) -> None:
    """Named ttk.TButton styles used everywhere instead of classic tk.Button:
    a real hover/press state (which a flat tk.Button can't give without extra
    binds) reads as much more "finished" than a static border. Grouped by
    color role; Tab/JobRemove additionally react to a manually-toggled
    "selected" widget state (see _set_mode/_set_view/_set_recipe_mode and the
    job-row remove button)."""
    # Every "X.TButton" style below inherits the base "TButton" layout, so
    # stripping the focus ring once here covers all of them.
    style.layout("TButton", _strip_focus(style.layout("TButton")))

    def role(name, bg, fg, hover, pressed, font=("Segoe UI", 9), padding=(10, 5)):
        style.configure(
            f"{name}.TButton",
            background=bg,
            foreground=fg,
            bordercolor=_BORDER,
            borderwidth=1,
            relief="raised",
            font=font,
            padding=padding,
        )
        style.map(
            f"{name}.TButton",
            background=[("pressed", pressed), ("active", hover)],
            bordercolor=[("focus", _BORDER_FOCUS)],
            relief=[("pressed", "sunken"), ("!pressed", "raised")],
        )

    role("Neutral", "#21262d", "#c9d1d9", "#30363d", "#161b22")
    role(
        "NeutralSmall",
        "#21262d",
        "#c9d1d9",
        "#30363d",
        "#161b22",
        font=("Segoe UI", 8),
        padding=(6, 4),
    )
    role("Success", "#238636", "white", "#2ea043", "#1a7431")
    role(
        "SuccessSmall",
        "#238636",
        "white",
        "#2ea043",
        "#1a7431",
        font=("Segoe UI", 8),
        padding=(6, 3),
    )
    role("Danger", "#da3633", "white", "#f85149", "#b62324")
    role("Accent", "#1f6feb", "white", "#388bfd", "#1158c7")

    # Toggle-style tab buttons: one style, "selected" state manually set via
    # .state(["selected"]) / .state(["!selected"]) at each toggle site.
    style.configure(
        "Tab.TButton",
        background="#21262d",
        foreground="#8b949e",
        bordercolor=_BORDER,
        borderwidth=1,
        relief="raised",
        font=("Segoe UI", 8),
        padding=(6, 3),
    )
    style.map(
        "Tab.TButton",
        background=[("selected", "#1f6feb"), ("active", "#30363d")],
        foreground=[("selected", "white")],
        bordercolor=[("selected", "#1f6feb")],
        relief=[("selected", "sunken"), ("!selected", "raised")],
    )

    # Minimal icon buttons (titlebar ✕/📌/⚙, row "×" removers) - no static
    # border so they blend into their bar/row at rest, just a hover highlight.
    # width=1 is essential here: ttk's "default" theme otherwise reserves a
    # much wider button (~65px, vs. ~13px with this set) for a single-glyph
    # label regardless of how tight `padding` is - width is what actually
    # controls it. Without this, a long titlebar title text can squeeze the
    # pin button out of the packed row entirely.
    def icon_role(name, bg, fg, hover):
        style.configure(
            f"{name}.TButton",
            background=bg,
            foreground=fg,
            borderwidth=0,
            relief="flat",
            font=("Segoe UI", 9),
            padding=(2, 1),
            width=3,
        )
        style.map(f"{name}.TButton", background=[("active", hover)])

    icon_role("IconClose", "#161b22", "#c9d1d9", "#21262d")
    icon_role("IconSettings", "#161b22", "#8b949e", "#21262d")

    style.configure(
        "IconPin.TButton",
        background="#161b22",
        foreground="#6e7681",
        borderwidth=0,
        relief="flat",
        font=("Segoe UI", 9),
        padding=(2, 1),
        width=3,
    )
    style.map(
        "IconPin.TButton",
        background=[("active", "#21262d")],
        foreground=[("selected", "#f0883e")],
    )

    style.configure(
        "Remove.TButton",
        background="#0d1117",
        foreground="#da3633",
        borderwidth=0,
        relief="flat",
        font=("Segoe UI", 9),
        padding=(2, 0),
        width=2,
    )
    style.map("Remove.TButton", background=[("active", "#21262d")])

    style.configure(
        "JobRemove.TButton",
        background="#161b22",
        foreground="#da3633",
        borderwidth=0,
        relief="flat",
        font=("Segoe UI", 9),
        padding=(2, 0),
        width=2,
    )
    style.map(
        "JobRemove.TButton",
        background=[("selected", "#1f6feb"), ("active", "#21262d")],
        foreground=[("selected", "#ffaaaa")],
    )


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

    # Tracked so an open popup can be force-closed from outside (e.g. when
    # switching view-mode tabs pack_forgets the combobox's frame - that
    # unmaps the box itself but not this separately-toplevel'd popup).
    _instances: "weakref.WeakSet[_LiveDropdown]" = weakref.WeakSet()

    def __init__(self, box: ttk.Combobox, pre_fn=None, on_select_fn=None):
        self._box = box
        self._pre_fn = pre_fn
        self._on_select = on_select_fn
        self._win: tk.Toplevel | None = None
        self._lb: tk.Listbox | None = None
        self._padding_applied = False
        _LiveDropdown._instances.add(self)

        self._readonly = "readonly" in str(box.cget("state"))
        if self._readonly:
            # Fixed-choice picker (e.g. Status): there's no text to
            # position a cursor in or select, so any click just toggles
            # the popup, same as clicking the arrow button.
            box.bind("<ButtonPress-1>", self._on_readonly_press)
        else:
            box.bind("<ButtonPress-1>", self._on_box_press)
            box.bind("<Double-Button-1>", self._on_box_double)
            box.bind("<Triple-Button-1>", self._on_box_triple)
        box.bind("<KeyRelease>", self._on_key, add=True)
        box.bind("<FocusOut>", lambda _e: box.after(150, self._maybe_hide), add=True)
        box.bind("<Escape>", self._on_box_escape, add=True)
        box.bind("<Down>", self._on_down, add=True)
        box.bind("<Return>", self._on_return, add=True)
        box.bind("<Configure>", self._reposition_arrow, add=True)
        box.bind("<Destroy>", lambda _: self._arrow_btn.destroy(), add=True)
        # The popup is a separate Toplevel positioned in screen coordinates,
        # so dragging the window it belongs to doesn't move it along -
        # <Configure> on the toplevel fires for every move/resize, so use it
        # to keep the popup glued to the combobox while open.
        box.winfo_toplevel().bind("<Configure>", self._on_toplevel_configure, add=True)
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

    def _on_readonly_press(self, _event):
        self._on_arrow_click()
        return "break"

    def _on_box_press(self, event):
        # ttk's own <ButtonPress-1> class binding posts the native dropdown
        # list when it thinks the click landed on the (visually removed)
        # arrow element - on Windows this hit-test is theme-driven and can
        # misfire a pixel or two off from where the arrow overlay button
        # actually is (e.g. right at the bottom edge of the field), popping
        # the ugly native list instead of ours. Replicate plain entry-click
        # behavior ourselves and swallow the event so that class binding
        # (and its native post) never runs; opening our popup stays the
        # overlay arrow button's job (see _on_arrow_click).
        box = self._box
        box.focus_set()
        try:
            box.icursor(box.index(f"@{event.x}"))
            box.selection_clear()
        except tk.TclError:
            pass
        return "break"

    def _on_box_double(self, event):
        # Word-select on double-click, replicated for the same reason as
        # _on_box_press: it must live on the widget-level bindtag so it
        # pre-empts (and its "break" doesn't get skipped by) the plain
        # <ButtonPress-1> override above.
        box = self._box
        box.focus_set()
        text = box.get()
        idx = box.index(f"@{event.x}")
        start = idx
        while start > 0 and text[start - 1] not in " \t":
            start -= 1
        end = idx
        while end < len(text) and text[end] not in " \t":
            end += 1
        box.icursor(end)
        box.selection_range(start, end)
        return "break"

    def _on_box_triple(self, _event):
        box = self._box
        box.focus_set()
        box.selection_range(0, "end")
        box.icursor("end")
        return "break"

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
        vals = list(self._box["values"])
        if "readonly" in str(self._box.cget("state")):
            # Fixed-choice picker (e.g. Status): always offer every option
            # rather than filtering by the already-selected value.
            shown = vals
        else:
            typed = self._box.get()
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
        self._lb.bind("<Escape>", self._on_lb_escape)
        self._lb.bind("<KeyPress>", self._lb_keypress)

    def _reposition(self):
        b = self._box
        b.update_idletasks()
        x, y = b.winfo_rootx(), b.winfo_rooty() + b.winfo_height()
        # Size to the longest visible entry, not just the combobox's own
        # width - otherwise long/similar names (e.g. "...Output I" vs
        # "...Output II") get truncated identically and can't be told apart.
        lb_font = tkfont.Font(font=self._lb.cget("font"))  # type: ignore[union-attr]
        longest = max(
            (lb_font.measure(v) for v in self._lb.get(0, "end")),  # type: ignore[union-attr]
            default=0,
        )
        w = max(b.winfo_width(), min(longest + 24, 520), 120)
        # Measure the real per-row pixel height from Tk's own layout rather
        # than assuming one - a hardcoded guess (previously 20px) can badly
        # overshoot the actual rendered row height (~15-17px for "Segoe UI"
        # 9 on this system), padding the popup with visibly blank rows.
        n = min(8, self._lb.size())  # type: ignore[union-attr]
        bbox = self._lb.bbox(0) if n else None  # type: ignore[union-attr]
        row_h = bbox[3] if bbox else lb_font.metrics("linespace") + 2
        h = n * row_h + 4
        self._win.geometry(f"{w}x{h}+{x}+{y}")  # type: ignore[union-attr]

    def _on_toplevel_configure(self, _event=None):
        if (
            self._win is not None
            and self._win.winfo_exists()
            and self._win.winfo_ismapped()
        ):
            self._reposition()

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
        else:
            if self._pre_fn:
                self._pre_fn()
            self._refresh()
        return "break"

    def _on_return(self, _event):
        if self._win and self._win.winfo_viewable() and self._lb:
            self._lb_pick()

    def _maybe_hide(self):
        f = self._box.focus_get()
        if f is not self._box and f is not self._lb:
            self.hide()

    def _on_box_escape(self, _event):
        # Only swallow Escape (stopping it from also hitting the window's
        # own Escape-to-hide binding) when the popup is actually open - an
        # idle combobox with nothing showing shouldn't block that.
        was_open = (
            self._win is not None
            and self._win.winfo_exists()
            and self._win.winfo_ismapped()
        )
        self.hide()
        return "break" if was_open else None

    def _on_lb_escape(self, _event):
        self.hide()
        return "break"

    def hide(self):
        if self._win and self._win.winfo_exists():
            self._win.withdraw()

    @classmethod
    def hide_all(cls):
        for inst in list(cls._instances):
            inst.hide()


def _autohide_yscroll(sb):
    """yscrollcommand callback that hides the scrollbar when all content is visible."""

    def _cmd(first, last):
        if float(first) <= 0.0 and float(last) >= 1.0:
            sb.grid_remove()
        else:
            sb.grid()
        sb.set(first, last)

    return _cmd


_root_hwnd = win32util.root_hwnd
_hwnd_is_foreground = win32util.hwnd_is_foreground
_force_foreground_window = win32util.force_foreground_window
_set_click_through = win32util.set_click_through


def _format_duration(seconds):
    """12s / 1m 15s / 1h 1m 1s / 12h - drops zero-valued higher units, never
    drops seconds entirely unless another unit is present."""
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s or not parts:
        parts.append(f"{s}s")
    return " ".join(parts)


def _remaining_part(seconds):
    """Droppable label part showing subtree time remaining - the actionable
    number, so it's dropped later than the raw per-craft rate under space
    pressure."""
    if not seconds or seconds <= 0:
        return None
    return f"  {_format_duration(seconds)} left"


def _format_craft_meta_suffix(station, per_craft_seconds, mode="auto", remaining_seconds=None):
    """Terse ' @ Station · Auto  12s left  12s/craft' suffix for a
    breakdown-tree label. per_craft_seconds is the time for ONE craft cycle,
    not scaled by how many crafts are needed - see _node_own_time for the
    scaled total, which is what remaining_seconds should carry."""
    parts = []
    if station:
        parts.append(f"@ {station} · {mode.capitalize()}")
    rem = _remaining_part(remaining_seconds)
    if rem:
        parts.append(rem.strip())
    if per_craft_seconds:
        parts.append(f"{_format_duration(per_craft_seconds)}/craft")
    if not parts:
        return ""
    return "  " + "  ".join(parts)


def _format_byproducts_suffix(byproducts):
    """Terse '  (+2 Silicium Ingot)' suffix listing a recipe's other outputs."""
    if not byproducts:
        return ""
    parts = [f"+{b['qty']:g} {b['name']}" for b in byproducts]
    return "  (" + ", ".join(parts) + ")"


def _craft_meta_parts(station, per_craft_seconds, mode="auto", remaining_seconds=None):
    """Station/remaining/rate as separately-droppable label parts, most
    important first. per_craft_seconds is the time for ONE craft cycle;
    remaining_seconds is the already-scaled subtree total (see
    _subtree_remaining_seconds) and is kept longer than the raw rate since
    it's the more actionable number."""
    parts = []
    if station:
        parts.append(f"  @ {station} · {mode.capitalize()}")
    rem = _remaining_part(remaining_seconds)
    if rem:
        parts.append(rem)
    if per_craft_seconds:
        parts.append(f"  {_format_duration(per_craft_seconds)}/craft")
    return parts


def _byproducts_part(byproducts):
    if not byproducts:
        return None
    parts = [f"+{b['qty']:g} {b['name']}" for b in byproducts]
    return "  (" + ", ".join(parts) + ")"


def _wrap_label(base, optional_parts, available_px, font):
    """base is always shown first; optional_parts (priority order, most
    important first) are appended to line 1 while they still fit within
    available_px. Anything left over is wrapped onto a second line instead
    of being silently dropped, so station/time/byproduct info stays visible
    rather than disappearing on a narrow window. Only ellipsis-truncates (as
    a last resort) if a single line still can't fit on its own - the tree
    this is used in has a taller-than-normal row height (see "Wrapped.
    Treeview" style) to fit the second line."""

    def _truncate(text):
        if font.measure(text) <= available_px:
            return text
        while text and font.measure(text + "…") > available_px:
            text = text[:-1]
        return text + "…"

    line1 = base
    remaining = list(optional_parts)
    while remaining:
        candidate = line1 + remaining[0]
        if font.measure(candidate) <= available_px:
            line1 = candidate
            remaining.pop(0)
        else:
            break
    line1 = _truncate(line1)
    if not remaining:
        return line1
    line2 = "    " + "  ".join(p.strip() for p in remaining)
    return line1 + "\n" + _truncate(line2)


class _StepPopup:
    """Click-to-open popup listing every alternate recipe and station/mode
    choice for one recipe-tree step, in a single control - replaces the old
    always-expanded alt_header/station_header child rows, which only ever
    surfaced station choices for the most basic crafting tier and forced
    alt-recipe and station pickers to be two unrelated UI mechanisms."""

    _active: "_StepPopup | None" = None

    @classmethod
    def show(cls, anchor_widget, x, y, node, on_alt, on_station):
        cls.hide_any()
        cls._active = cls(anchor_widget, x, y, node, on_alt, on_station)

    @classmethod
    def hide_any(cls):
        if cls._active is not None:
            cls._active._destroy()  # pylint: disable=protected-access
            cls._active = None

    def __init__(self, anchor_widget, x, y, node, on_alt, on_station):
        self._win = win = tk.Toplevel(anchor_widget)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        frm = tk.Frame(
            win, bg="#1a2029", highlightbackground="#3a4350", highlightthickness=1
        )
        frm.pack(fill="both", expand=True)

        def add_label(text):
            tk.Label(
                frm,
                text=text,
                bg="#1a2029",
                fg="#5b6470",
                font=("Segoe UI", 7),
                anchor="w",
            ).pack(fill="x", padx=8, pady=(6, 1))

        def add_option(label, selected, command):
            row = tk.Label(
                frm,
                text=("●  " if selected else "    ") + label,
                bg="#242c3d" if selected else "#1a2029",
                fg="#dce8ff" if selected else "#c9d1d9",
                font=("Segoe UI", 8),
                anchor="w",
                padx=8,
                pady=3,
                cursor="hand2",
            )
            row.pack(fill="x")

            def _enter(_e, r=row):
                r.configure(bg="#2a3244")

            def _leave(_e, r=row, sel=selected):
                r.configure(bg="#242c3d" if sel else "#1a2029")

            def _pick(_e, cmd=command):
                cmd()
                self._destroy()

            row.bind("<Enter>", _enter)
            row.bind("<Leave>", _leave)
            row.bind("<ButtonRelease-1>", _pick)

        alts = node.get("alts", [])
        if alts:
            add_label("ALTERNATE RECIPE")
            for alt in alts:
                add_option(
                    alt["recipe_name"],
                    False,
                    lambda rid=alt["recipe_id"], rname=alt["recipe_name"]: on_alt(
                        rid, rname
                    ),
                )

        stations = node.get("stations", [])
        modes_available = sum(
            (1 if st_auto else 0) + (1 if st_manual else 0)
            for _n, st_auto, st_manual in stations
        )
        if modes_available > 1:
            if alts:
                tk.Frame(frm, bg="#30363d", height=1).pack(fill="x", padx=4, pady=3)
            add_label("STATION & MODE")
            cur_station = node.get("station")
            cur_mode = node.get("craft_mode", "auto")
            for st_name, st_auto, st_manual in stations:
                if st_auto:
                    add_option(
                        f"{st_name} · Auto  ({_format_duration(st_auto)}/craft)",
                        st_name == cur_station and cur_mode == "auto",
                        lambda s=st_name: on_station(s, "auto"),
                    )
                if st_manual:
                    add_option(
                        f"{st_name} · Manual  ({_format_duration(st_manual)}/craft)",
                        st_name == cur_station and cur_mode == "manual",
                        lambda s=st_name: on_station(s, "manual"),
                    )

        win.update_idletasks()
        w = max(win.winfo_reqwidth(), 180)
        h = win.winfo_reqheight()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        x = min(x, sw - w)
        if y + h > sh:
            y = max(0, y - h)
        win.geometry(f"{w}x{h}+{x}+{y}")
        win.deiconify()
        win.lift()
        # Deliberately never grabs real OS focus (no focus_force()) - this
        # app is click-through whenever it doesn't have OS foreground focus
        # (see Overlay._sync_all_input_passthrough), so a popup that stole
        # focus made the window that spawned it go click-through the moment
        # it opened. Same reason _LiveDropdown's combobox popup never grabs
        # focus either. Dismissal is instead a plain click-away catcher on
        # the owning window - any click that reaches it is by construction
        # outside this separate Toplevel.
        self._anchor = anchor_widget
        self._root = anchor_widget.winfo_toplevel()
        self._click_bind_id = self._root.bind(
            "<ButtonPress-1>", self._on_outside_click, add="+"
        )
        def _on_escape(_e):
            self._destroy()
            return "break"

        self._escape_bind_id = anchor_widget.bind("<Escape>", _on_escape, add="+")

    def _on_outside_click(self, _event):
        self._destroy()

    def _destroy(self):
        try:
            self._root.unbind("<ButtonPress-1>", self._click_bind_id)
        except tk.TclError:
            pass
        try:
            self._anchor.unbind("<Escape>", self._escape_bind_id)
        except tk.TclError:
            pass
        try:
            self._win.destroy()
        except tk.TclError:
            pass
        if _StepPopup._active is self:
            _StepPopup._active = None


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
        # (queue_id, recipe_id, output_name, qty, station, station_mode)
        self._selected_job = None
        self._job_frames: dict = {}
        self._bd_iid_info: dict = {}
        self._bd_toggled = False
        self._drag_x = self._drag_y = 0
        self._resize_x = self._resize_y = 0
        self._resize_w = self._resize_h = 0
        self._passthrough = False
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
        self._win.bind("<Escape>", lambda _e: self.dismiss())

    def _build_ui(self):
        # --- drag bar ---
        drag = tk.Frame(self._win, bg="#161b22", height=28)
        drag.pack(fill="x")

        # Buttons packed before the title label so pack() reserves their
        # space first - a long title (e.g. "(alt+twosuperior to hide)")
        # then just clips itself against whatever's left, instead of
        # squeezing a later-packed button out of the row entirely.
        ttk.Button(
            drag, text="✕", style="IconClose.TButton", command=self.hide
        ).pack(side="right", padx=4)

        self._pin_btn = ttk.Button(
            drag, text="📌", style="IconPin.TButton", command=self._toggle_pin
        )
        self._pin_btn.state(["selected" if self._pinned else "!selected"])
        self._pin_btn.pack(side="right", padx=2)

        self._title_label = tk.Label(
            drag,
            text=self._title_text(),
            bg="#161b22",
            fg="#c9d1d9",
            font=("Segoe UI", 9),
        )
        self._title_label.pack(side="left", padx=8)

        for widget in (drag, self._title_label):
            widget.bind("<ButtonPress-1>", self._start_move)
            widget.bind("<B1-Motion>", self._do_move)
            widget.bind("<ButtonRelease-1>", lambda _e: self._save_pos())

        # --- mode tabs (own row, not the titlebar - a long "(F1 to hide)"
        # title-state suffix can otherwise grow enough to hide these) ---
        tab_row = tk.Frame(self._win, bg="#0d1117")
        tab_row.pack(fill="x", padx=6, pady=(4, 0))

        self._btn_queue = ttk.Button(
            tab_row,
            text="Queue",
            style="Tab.TButton",
            command=lambda: self._set_mode("queue"),
        )
        self._btn_queue.state(["selected" if self._mode == "queue" else "!selected"])
        self._btn_queue.pack(side="left", padx=(0, 2))

        self._btn_totals = ttk.Button(
            tab_row,
            text="Totals",
            style="Tab.TButton",
            command=lambda: self._set_mode("totals"),
        )
        self._btn_totals.state(["selected" if self._mode == "totals" else "!selected"])
        self._btn_totals.pack(side="left", padx=(0, 2))

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
        # Expand into whatever width the (resizable) queue panel has, rather
        # than a fixed 20-char box - long/similar recipe names (e.g.
        # "...Output I" vs "...Output II") are indistinguishable when cut off.
        self._add_recipe_cb.pack(side="left", padx=(0, 4), fill="x", expand=True)
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
        _bordered_entry(
            add_row,
            textvariable=self._add_qty_var,
            width=7,
            bg="#161b22",
            fg="#c9d1d9",
            insertbackground="#c9d1d9",
            relief="flat",
            justify="center",
        ).pack(side="left", padx=(0, 4), ipady=2)

        ttk.Button(
            add_row, text="+ Add", command=self._add_job, style="SuccessSmall.TButton"
        ).pack(side="left")

        ttk.Button(
            add_row,
            text="Clear done",
            command=self._clear_all_done,
            style="NeutralSmall.TButton",
        ).pack(side="right")

        # --- station row (recipes with more than one usable station let
        # you pick which one this queued job uses; blank = the recipe's
        # primary/default station) ---
        station_row = tk.Frame(self._win, bg="#0d1117")
        station_row.pack(side="bottom", fill="x", padx=6, pady=(0, 4))
        tk.Label(
            station_row,
            text="Station:",
            bg="#0d1117",
            fg="#8b949e",
            font=("Segoe UI", 8),
        ).pack(side="left", padx=(0, 4))
        self._add_station_var = tk.StringVar()
        self._add_station_cb = ttk.Combobox(
            station_row, textvariable=self._add_station_var, width=18
        )
        # No fill/expand: station names are always short, so stretching this
        # to the full row width (like the recipe-name box deliberately does)
        # just made the dropdown popup balloon out to match - see _reposition.
        self._add_station_cb.pack(side="left")
        _LiveDropdown(self._add_station_cb)
        self._add_recipe_cb.bind(
            "<FocusOut>", self._refresh_add_station_options, add="+"
        )
        self._add_recipe_cb.bind(
            "<Return>", self._refresh_add_station_options, add="+"
        )
        _LiveDropdown(
            self._add_recipe_cb,
            pre_fn=lambda: self._add_recipe_cb.configure(
                values=[n for _, n in get_all_recipes()]
            ),
            on_select_fn=lambda _v: self._refresh_add_station_options(),
        )

        # --- PanedWindow: job list (top pane) + breakdown tree (bottom pane) ---
        self._pw = tk.PanedWindow(
            self._win,
            orient=tk.VERTICAL,
            bg="#21262d",
            sashwidth=5,
            sashrelief="flat",
            sashpad=0,
            handlesize=0,
            bd=0,
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
        jvsb = ttk.Scrollbar(
            job_frame,
            orient="vertical",
            style="Thin.Vertical.TScrollbar",
            command=self._job_canvas.yview,
        )
        jvsb.grid(row=0, column=1, sticky="ns")
        self._job_canvas.configure(yscrollcommand=_autohide_yscroll(jvsb))
        self._job_inner = tk.Frame(self._job_canvas, bg="#0d1117")
        _jwin = self._job_canvas.create_window(
            (0, 0), window=self._job_inner, anchor="nw"
        )
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
        self._bd_font = tkfont.nametofont("TkDefaultFont")
        # A named style (inherits everything else from the base "Treeview"
        # style by ttk's dotted-name convention) with a taller row height -
        # overflow text wraps onto a second line (see _wrap_label) rather
        # than being truncated/dropped, so this tree needs room for it.
        linespace = self._bd_font.metrics("linespace")
        ttk.Style(self._win).configure("Wrapped.Treeview", rowheight=linespace * 2 + 8)
        self._bd_tree = ttk.Treeview(bd_frame, show="tree", style="Wrapped.Treeview")
        self._bd_tree.grid(row=0, column=0, sticky="nsew")
        self._bd_vsb = ttk.Scrollbar(
            bd_frame,
            orient="vertical",
            style="Thin.Vertical.TScrollbar",
            command=self._bd_tree.yview,
        )
        self._bd_vsb.grid(row=0, column=1, sticky="ns")
        self._bd_tree.configure(yscrollcommand=_autohide_yscroll(self._bd_vsb))
        self._bd_tree.bind("<ButtonRelease-1>", self._on_bd_click)
        self._bd_resize_job = None
        self._bd_tree.bind("<Configure>", self._on_bd_tree_configure, add="+")
        self._bd_root_open = True
        self._bd_tree.bind("<<TreeviewOpen>>", self._on_bd_toggled)
        self._bd_tree.bind("<<TreeviewClose>>", self._on_bd_toggled)

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
        grip = tk.Label(
            btm,
            text="◢",
            bg="#0d1117",
            fg="#3b434d",
            font=("Segoe UI", 7),
            cursor="size_nw_se",
        )
        grip.pack(side="right", padx=2, pady=1)
        grip.bind("<ButtonPress-1>", self._start_resize)
        grip.bind("<B1-Motion>", self._do_resize)
        grip.bind("<ButtonRelease-1>", self._end_resize)

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
        self._win.geometry(
            f"{new_w}x{new_h}+{self._win.winfo_x()}+{self._win.winfo_y()}"
        )
        self._win.update_idletasks()
        if sys.platform == "win32":
            win32util.redraw_window(self._win.winfo_id())

    def _end_resize(self, _):
        self._save_pos()

    # --- pin / mode ---

    def _toggle_pin(self):
        self._pinned = not self._pinned
        self._pin_btn.state(["selected" if self._pinned else "!selected"])
        self.update_title_bar()
        cfg: dict = load_config()
        cfg["queue_pinned"] = self._pinned
        save_config(cfg)
        if not self._pinned and self._overlay.state() == "withdrawn":
            self.hide()

    def _set_mode(self, mode):
        self._mode = mode
        self._btn_queue.state(["selected" if mode == "queue" else "!selected"])
        self._btn_totals.state(["selected" if mode == "totals" else "!selected"])
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
        for queue_id, recipe_id, _, output_name, qty, station, combine, station_mode in jobs:
            self._build_job_row(
                queue_id, recipe_id, output_name, qty, station, combine, station_mode
            )
        self._job_canvas.configure(
            scrollregion=self._job_canvas.bbox("all") or (0, 0, 0, 0)
        )

    def _build_job_row(
        self, queue_id, recipe_id, output_name, qty, station, combine, station_mode="auto"
    ):
        is_sel = self._selected_job is not None and self._selected_job[0] == queue_id
        bg = "#1f6feb" if is_sel else "#161b22"
        row = tk.Frame(self._job_inner, bg=bg, cursor="hand2")
        row.pack(fill="x", pady=1)
        self._job_frames[queue_id] = row

        combine_img = (
            self._overlay.img_checked if combine else self._overlay.img_unchecked
        )
        combine_lbl = tk.Label(row, image=combine_img, bg=bg, cursor="hand2")
        combine_lbl.pack(side="left", padx=(4, 2))
        combine_lbl.bind(
            "<ButtonPress-1>",
            lambda _e, qid=queue_id, cur=combine: self._toggle_combine(qid, cur),
        )
        combine_lbl.bind("<MouseWheel>", self._job_scroll, add=True)

        lbl = tk.Label(
            row,
            text=output_name,
            bg=bg,
            fg="white" if is_sel else "#c9d1d9",
            font=("Segoe UI", 8),
            anchor="w",
        )
        lbl.pack(side="left", padx=(0, 2), pady=3, fill="x", expand=True)

        rm_btn = ttk.Button(
            row,
            text="×",
            style="JobRemove.TButton",
            command=lambda qid=queue_id: self._remove_job(qid),
        )
        rm_btn.state(["selected" if is_sel else "!selected"])
        rm_btn.pack(side="right", padx=4)
        rm_btn.bind("<MouseWheel>", self._job_scroll, add=True)

        qty_var = tk.StringVar(value=f"{qty:g}")
        qty_e = _bordered_entry(
            row,
            textvariable=qty_var,
            width=6,
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
        qty_e.bind("<MouseWheel>", self._job_scroll, add=True)

        def _on_click(
            _ev,
            qid=queue_id,
            rid=recipe_id,
            oname=output_name,
            qv=qty_var,
            st=station,
            stm=station_mode,
        ):
            self._select_job(qid, rid, oname, qv, st, stm)

        for w in (row, lbl):
            w.bind("<ButtonPress-1>", _on_click)
            w.bind("<MouseWheel>", self._job_scroll, add=True)

    def _select_job(
        self, queue_id, recipe_id, output_name, qty_var, station=None, station_mode="auto"
    ):
        try:
            qty = max(float(qty_var.get()), 0.001)
        except ValueError:
            qty = 1.0
        self._selected_job = (queue_id, recipe_id, output_name, qty, station, station_mode)
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
            self._selected_job = (old[0], old[1], old[2], qty, old[4], old[5])
            self._refresh_breakdown()

    def _update_station(self, queue_id, station, mode="auto"):
        station = station or None
        update_queue_station(queue_id, station, mode)
        if self._selected_job and self._selected_job[0] == queue_id:
            old = self._selected_job
            self._selected_job = (old[0], old[1], old[2], old[3], station, mode)
        if self._mode == "queue":
            self._refresh_breakdown()

    def _toggle_combine(self, queue_id, currently_combine):
        update_queue_combine(queue_id, not currently_combine)
        self._refresh_job_list()
        if self._mode == "totals":
            self._refresh_breakdown()

    def _remove_job(self, queue_id):
        remove_from_queue(queue_id)
        if self._selected_job and self._selected_job[0] == queue_id:
            self._selected_job = None
        self._refresh_job_list()
        self._refresh_breakdown()

    def _refresh_add_station_options(self, _event=None):
        name = self._add_recipe_var.get().strip()
        recipe_id = get_recipe_by_name(name)
        stations = get_recipe_stations(recipe_id) if recipe_id is not None else []
        values = [s[0] for s in stations]
        self._add_station_cb.configure(values=values)
        # Default to the recipe's primary station rather than leaving this
        # blank, so the field always shows what will actually be used.
        if self._add_station_var.get() not in values:
            self._add_station_var.set(values[0] if values else "")

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
        station = self._add_station_var.get().strip() or None
        add_to_queue(recipe_id, qty, station)
        self._add_recipe_var.set("")
        self._add_qty_var.set("1")
        self._add_station_var.set("")
        self._add_station_cb.configure(values=[])
        self._refresh_job_list()
        if self._mode == "totals":
            self._refresh_breakdown()

    def _clear_all_done(self):
        for queue_id, *_ in get_craft_queue():
            clear_queue_checked(queue_id)
        clear_queue_checked(self._TOTALS_QID)
        self._refresh_breakdown()

    # --- breakdown / totals tree ---

    def _on_bd_tree_configure(self, _event=None):
        if self._bd_resize_job is not None:
            self._win.after_cancel(self._bd_resize_job)
        self._bd_resize_job = self._win.after(150, self._refresh_breakdown)

    def _available_label_px(self, depth=0):
        width = self._bd_tree.winfo_width()
        if width <= 1:
            width = 260
        # The scrollbar auto-hides/shows (see _autohide_yscroll) based on
        # whether content overflows - but that decision, and the resulting
        # grid reflow of the tree's own width, only resolves after this
        # same insert pass finishes populating the tree. Reserving its width
        # unconditionally (whether mapped right now or not) avoids sizing
        # labels against a wider "no scrollbar yet" measurement that then
        # gets clipped once the scrollbar actually appears.
        width -= self._bd_vsb.winfo_reqwidth()
        return max(60, width - 20 - 16 * depth)

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
        queue_id, recipe_id, output_name, qty, job_station, job_station_mode = (
            self._selected_job
        )
        alt_prefs = get_alt_prefs()
        station_prefs = get_station_prefs()
        node = resolve_recipe_tree(
            output_name,
            qty_needed=qty,
            _root_recipe_id=recipe_id,
            _alt_prefs=alt_prefs,
            _station_prefs=station_prefs,
        )
        if job_station:
            times = get_recipe_station_times(recipe_id, job_station)
            if times:
                node["station"] = job_station
                node["auto_craft_seconds"], node["manual_craft_seconds"] = times
                if job_station_mode == "manual" and times[1]:
                    node["craft_mode"] = "manual"
                elif job_station_mode == "auto" and times[0]:
                    node["craft_mode"] = "auto"
                else:
                    node["craft_mode"] = "auto" if times[0] else "manual"
        checked = get_queue_checked(queue_id)
        oqty = node.get("output_qty", 1.0)
        crafts = math.ceil(qty / oqty)
        base_label = f"◆  {output_name}  ×{qty:g}"
        if crafts > 1 or oqty > 1:
            base_label += f"  ({crafts:g} crafts)"
        active_seconds, active_mode = _node_active_seconds(node)
        remaining = _subtree_remaining_seconds(node, [], checked)
        optional_parts = _craft_meta_parts(
            node.get("station"), active_seconds, active_mode, remaining
        )
        byproducts_part = _byproducts_part(node.get("byproducts"))
        if byproducts_part:
            optional_parts.append(byproducts_part)
        # Alts are deliberately excluded from the root's own options: a
        # queued job is tied to the specific recipe_id it was queued with,
        # so switching to a wholly different recipe here doesn't apply the
        # way it does for a sub-ingredient - only station/mode does.
        root_modes_available = sum(
            (1 if st_auto else 0) + (1 if st_manual else 0)
            for _name, st_auto, st_manual in node.get("stations", [])
        )
        root_has_options = root_modes_available > 1
        if root_has_options:
            base_label += "  ▾"
        root_label = _wrap_label(
            base_label, optional_parts, self._available_label_px(0), self._bd_font
        )
        root_path_key = _node_path_key(node, [])
        root_is_done = root_path_key in checked
        root_img = (
            self._overlay.img_checked if root_is_done else self._overlay.img_unchecked
        )
        root_iid = tree.insert(
            "",
            "end",
            iid="bd_root",
            text=root_label,
            image=root_img,
            open=self._bd_root_open,
            tags=("root",),
        )
        self._bd_iid_info[root_iid] = {
            "type": "ingredient",
            "queue_id": queue_id,
            "path_key": root_path_key,
            "checked": root_is_done,
            "node": node,
            "path_parts": [],
            "has_options": root_has_options,
            "is_queue_root": True,
        }
        for child in node["children"]:
            self._insert_node(tree, root_iid, child, queue_id, [], checked)

    def _render_totals(self, tree):
        jobs = get_craft_queue()
        if not jobs:
            tree.insert("", "end", text="Queue is empty.", tags=("section",))
            return
        alt_prefs = get_alt_prefs()
        station_prefs = get_station_prefs()
        all_raw: dict = {}
        all_crafted: dict = {}
        per_job = []
        combined_count = 0
        for qid, recipe_id, rname, output_name, qty, station, combine, station_mode in jobs:
            node = resolve_recipe_tree(
                output_name,
                qty_needed=qty,
                _root_recipe_id=recipe_id,
                _alt_prefs=alt_prefs,
                _station_prefs=station_prefs,
            )
            if station:
                times = get_recipe_station_times(recipe_id, station)
                if times:
                    node["station"] = station
                    node["auto_craft_seconds"], node["manual_craft_seconds"] = times
                    if station_mode == "manual" and times[1]:
                        node["craft_mode"] = "manual"
                    elif station_mode == "auto" and times[0]:
                        node["craft_mode"] = "auto"
                    else:
                        node["craft_mode"] = "auto" if times[0] else "manual"
            job_raw = Overlay.collect_totals(node)
            job_crafted = Overlay.collect_basic_crafted(node)
            per_job.append((qid, rname, qty, job_crafted, job_raw))
            if not combine:
                continue
            combined_count += 1
            for iname, raw_qty in job_raw.items():
                all_raw[iname] = all_raw.get(iname, 0) + raw_qty
            for iname, info in job_crafted.items():
                entry = all_crafted.setdefault(
                    iname,
                    {
                        "qty": 0.0,
                        "output_qty": info["output_qty"],
                        "raw_names": set(),
                        "station": info.get("station"),
                        "auto_craft_seconds": info.get("auto_craft_seconds"),
                        "byproducts": {},
                    },
                )
                entry["qty"] += info["qty"]
                entry["raw_names"].update(info["raw_names"])
                for bp in info.get("byproducts", []):
                    entry["byproducts"][bp["name"]] = (
                        entry["byproducts"].get(bp["name"], 0.0) + bp["qty"]
                    )

        for entry in all_crafted.values():
            entry["byproducts"] = [
                {"name": n, "qty": q} for n, q in sorted(entry["byproducts"].items())
            ]

        header = tree.insert(
            "",
            "end",
            iid="bd_root",
            text=f"◆  All Jobs  ({combined_count})",
            open=self._bd_root_open,
            tags=("root",),
        )
        self._bd_iid_info[header] = {"type": "root"}
        self._insert_totals_sections(
            tree, header, self._TOTALS_QID, all_crafted, all_raw
        )

        if len(jobs) > 1:
            per_hdr = tree.insert(
                "", "end", text="── Per Recipe ──", open=False, tags=("section",)
            )
            self._bd_iid_info[per_hdr] = {"type": "root"}
            for qid, rname, qty, job_crafted, job_raw in per_job:
                job_root = tree.insert(
                    per_hdr,
                    "end",
                    text=f"◆  {rname}  ×{qty:g}",
                    open=False,
                    tags=("root",),
                )
                self._bd_iid_info[job_root] = {"type": "root"}
                self._insert_totals_sections(tree, job_root, qid, job_crafted, job_raw)

    def _insert_totals_sections(self, tree, parent_iid, queue_id, crafted, raw):
        """Crafted section lists only the most basic crafted items — recipes
        built entirely from raw materials — with assembly recipes collapsed
        past (see Overlay.collect_basic_crafted). Each entry has a dropdown
        to the raw materials/locations it's built from."""
        checked = get_queue_checked(queue_id)

        if crafted:
            craft_hdr = tree.insert(
                parent_iid, "end", text="── Crafted ──", open=True, tags=("section",)
            )
            self._bd_iid_info[craft_hdr] = {"type": "root"}
            for iname, info in sorted(crafted.items(), key=lambda x: x[0].lower()):
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
                has_options = _node_has_step_options(info)
                base_label = f"{qty:g}×  {iname}"
                if oq > 1:
                    base_label += f"  ({crafts:g} crafts)"
                if has_options:
                    base_label += "  ▾"
                active_seconds, active_mode = _node_active_seconds(info)
                own_time = (active_seconds * crafts) if active_seconds else 0.0
                remaining = 0.0 if is_done else own_time
                optional_parts = _craft_meta_parts(
                    info.get("station"), active_seconds, active_mode, remaining
                )
                byproducts_part = _byproducts_part(info.get("byproducts"))
                if byproducts_part:
                    optional_parts.append(byproducts_part)
                label = _wrap_label(
                    base_label,
                    optional_parts,
                    self._available_label_px(2),
                    self._bd_font,
                )
                iid = tree.insert(
                    craft_hdr,
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
                    "node": info,
                    "ingredient_name": iname,
                    "has_options": has_options,
                    "flat": True,
                }
                for raw_name in sorted(info.get("raw_names", [])):
                    raw_iid = tree.insert(
                        iid, "end", text=f"    {raw_name}", tags=("location",)
                    )
                    self._bd_iid_info[raw_iid] = {"type": "location"}
                    for (
                        sector,
                        system_name,
                        planet,
                        status,
                    ) in get_deposits_for_ingredient(raw_name):
                        parts = [p for p in (sector, system_name, planet) if p]
                        loc_text = " / ".join(parts)
                        if status and status not in ("Unknown", ""):
                            loc_text += f"  [{status}]"
                        loc_iid = tree.insert(
                            raw_iid,
                            "end",
                            text=f"      📍 {loc_text}",
                            tags=("location",),
                        )
                        self._bd_iid_info[loc_iid] = {"type": "location"}

        self._insert_raw_totals(tree, parent_iid, queue_id, raw, checked)

    def _insert_raw_totals(self, tree, parent_iid, queue_id, raw, checked):
        raw_hdr = tree.insert(
            parent_iid, "end", text="── Raw Materials ──", open=True, tags=("section",)
        )
        self._bd_iid_info[raw_hdr] = {"type": "root"}
        for iname, qty in sorted(raw.items(), key=lambda x: x[0].lower()):
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
                "queue_id": queue_id,
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

    def _insert_node(
        self, tree, parent_iid, node, queue_id, path_parts, checked, depth=0
    ):
        name = node["name"]
        qty = node["qty"]
        used_recipe = node.get("recipe_name", name)
        path_key = _node_path_key(node, path_parts)
        is_done = path_key in checked
        has_options = _node_has_step_options(node)
        base_label = f"{qty:g}×  {name}"
        if used_recipe and used_recipe != name:
            base_label += f"  [{used_recipe}]"
        if has_options:
            base_label += "  ▾"
        active_seconds, active_mode = _node_active_seconds(node)
        remaining = _subtree_remaining_seconds(node, path_parts, checked)
        optional_parts = _craft_meta_parts(
            node.get("station"), active_seconds, active_mode, remaining
        )
        byproducts_part = _byproducts_part(node.get("byproducts"))
        if byproducts_part:
            optional_parts.append(byproducts_part)
        label = _wrap_label(
            base_label,
            optional_parts,
            self._available_label_px(depth + 1),
            self._bd_font,
        )
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
            "node": node,
            "path_parts": path_parts,
            "has_options": has_options,
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

    def _open_step_popup(self, tree, iid, info):
        node = info["node"]
        bbox = tree.bbox(iid)
        if not bbox:
            return
        x = tree.winfo_rootx() + bbox[0]
        y = tree.winfo_rooty() + bbox[1] + bbox[3]
        is_queue_root = info.get("is_queue_root", False)
        # Totals mode's aggregated entries don't carry their own "name" (see
        # collect_basic_crafted) - the loop that inserted them stashed it
        # separately as ingredient_name instead.
        ingredient_name = info.get("ingredient_name") or node.get("name")
        # A queued job is tied to the specific recipe_id it was queued
        # with, so there's no meaningful alt-recipe switch for its root -
        # only offer the station/mode section for it.
        popup_node = {**node, "alts": []} if is_queue_root else node

        def on_alt(alt_recipe_id, _alt_recipe_name):
            set_alt_pref(ingredient_name, alt_recipe_id)
            self._refresh_breakdown()

        def on_station(station, mode):
            if is_queue_root:
                self._update_station(info["queue_id"], station, mode)
            else:
                set_station_pref(ingredient_name, station, mode)
            self._refresh_breakdown()

        _StepPopup.show(tree, x, y, popup_node, on_alt, on_station)

    def _on_bd_toggled(self, event):
        # Every checkbox click fully rebuilds this tree (see _on_bd_click's
        # cascade-check), which re-inserts the root row fresh each time -
        # remember whatever open/closed state the user last set for it here
        # so a rebuild doesn't silently re-expand it right back.
        self._bd_toggled = True
        tree = event.widget
        if tree.exists("bd_root"):
            self._bd_root_open = bool(tree.item("bd_root", "open"))

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
        if info["type"] == "ingredient":
            if "image" in tree.identify("element", event.x, event.y):
                queue_id = info["queue_id"]
                new_done = not info.get("checked", False)
                node = info.get("node")
                if info.get("flat") or node is None:
                    # Totals mode already flattens the tree into one entry
                    # per basic-crafted item name, so there's no subtree
                    # structure left to cascade into - just this one entry.
                    path_keys = [info["path_key"]]
                else:
                    path_keys = _collect_path_keys(node, info["path_parts"])
                set_queue_checked_many(queue_id, path_keys, new_done)
                self._refresh_breakdown()
                return
            if info.get("has_options") and info.get("node") is not None:
                self._open_step_popup(tree, iid, info)

    # --- show / hide / pin ---

    def show(self):
        self._win.deiconify()
        self._win.attributes("-topmost", True)

    def hide(self):
        self._win.withdraw()
        self.on_hide_cb()

    def dismiss(self):
        """Hide this window the way an ambient dismiss gesture should -
        this window's own Escape binding and Overlay.hide() (when it takes
        the unpinned queue down with it) both call this, so "pin means stay
        visible" is enforced in exactly one place. The X button and
        explicit show/hide toggles still call hide()/show() directly, since
        those are deliberate actions that should override the pin."""
        if not self._pinned:
            self.hide()

    def is_visible(self):
        return self._win.state() != "withdrawn"

    def toggle(self):
        if self.is_visible():
            self.hide()
        else:
            self.show()

    def has_os_focus(self):
        return _hwnd_is_foreground(_root_hwnd(self._win))

    def grab_os_focus(self):
        """Pull real OS input focus onto this window - see
        Overlay._grab_os_focus for why the raw Win32 foreground grab (not
        Tk's focus_force()) is what actually matters here."""
        if sys.platform == "win32":
            _force_foreground_window(_root_hwnd(self._win))
        self._win.focus_force()

    def set_input_passthrough(self, enabled: bool):
        """Make this window click-through (or not). Driven by the overlay's
        combined focus state (see Overlay._sync_all_input_passthrough) so
        that focusing either the main window or the queue panel counts as
        the whole app being focused, rather than tracked independently.
        While click-through, mouse clicks and the OS cursor pass straight
        to whatever is beneath (the game) instead of this window."""
        if self._win.state() == "withdrawn":
            return
        if sys.platform != "win32":
            return
        if enabled != self._passthrough:
            self._passthrough = enabled
            _set_click_through(_root_hwnd(self._win), enabled)
            self.update_title_bar()

    def _title_text(self):
        if self._passthrough:
            return f"⠿  Craft Queue   ({self._overlay.toggle_key} to focus)"
        if self._pinned:
            return "⠿  Craft Queue"
        return f"⠿  Craft Queue   ({self._overlay.toggle_key} to hide)"

    def update_title_bar(self):
        self._title_label.config(text=self._title_text())

    @property
    def pinned(self):
        return self._pinned

    def add_job(self, recipe_id, qty=1.0):
        """Add a job externally (e.g. right-click from recipe panel)."""
        add_to_queue(recipe_id, qty)
        self._refresh_job_list()
        if self._mode == "totals":
            self._refresh_breakdown()


# Tk keysym -> modifier name used in `keyboard` library hotkey strings
# (e.g. "ctrl+shift+r"), for the press-to-capture hotkey recorder below.
_MODIFIER_KEYSYMS = {
    "Control_L": "ctrl",
    "Control_R": "ctrl",
    "Shift_L": "shift",
    "Shift_R": "shift",
    "Alt_L": "alt",
    "Alt_R": "alt",
    "Super_L": "windows",
    "Super_R": "windows",
    "Win_L": "windows",
    "Win_R": "windows",
}
_MODIFIER_ORDER = ["ctrl", "alt", "shift", "windows"]

# Tk keysyms whose `keyboard` library name isn't just the lowercased keysym.
_KEYSYM_TO_KEY_NAME = {
    "Return": "enter",
    "KP_Enter": "enter",
    "space": "space",
    "BackSpace": "backspace",
    "Tab": "tab",
    "Delete": "delete",
    "Insert": "insert",
    "Home": "home",
    "End": "end",
    "Prior": "page up",
    "Next": "page down",
    "Up": "up",
    "Down": "down",
    "Left": "left",
    "Right": "right",
    "Caps_Lock": "caps lock",
    "Scroll_Lock": "scroll lock",
    "Num_Lock": "num lock",
    "Print": "print screen",
    "Pause": "pause",
}


def _keysym_to_key_name(keysym):
    return _KEYSYM_TO_KEY_NAME.get(keysym, keysym.lower())


class Overlay(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CraftMap Resources")
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.94)
        self.configure(bg="#0d1117")
        self.overrideredirect(True)  # no native title bar -> cleaner overlay
        style = ttk.Style(self)
        style.theme_use("default")
        _configure_button_styles(style)
        self.selected_id = None
        self.type_filter_vars = {}  # res_type -> tk.BooleanVar, rebuilt dynamically
        self._hotkey_handle = None
        self._hotkey_suppressed = False
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
        self._queue_panel_was_visible = False
        self.tray_icon: object = None
        self.img_unchecked: tk.PhotoImage
        self.img_checked: tk.PhotoImage
        self._recipe_breakdown_mode: str = "breakdown"
        self._usedin_recipe_id: "int | None" = None
        self._usedin_navigated_away: bool = False
        self._passthrough = False

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

        self.bind("<Escape>", lambda _e: self.hide())

        self.bind_all("<FocusIn>", self._on_focus_event, add="+")
        self.bind_all("<FocusOut>", self._on_focus_event, add="+")
        self.after(200, self._poll_input_passthrough)

    # ----- input passthrough (click-through while unfocused, so mouse
    # clicks and the OS cursor go straight to the game underneath instead of
    # being intercepted by the overlay - the overlay only ever intercepts
    # them while it actually has OS focus) -----
    def _on_focus_event(self, _event=None):
        self.after(50, self._sync_all_input_passthrough)

    def _poll_input_passthrough(self):
        """Periodically re-check focus state, since this is an
        overrideredirect popup: Windows doesn't reliably deliver
        WM_KILLFOCUS/<FocusOut> to it when a foreign window steals the OS
        foreground (e.g. clicking the game behind it), so the <FocusOut>
        binding above alone can miss the transition and leave the overlay
        stuck intercepting clicks. Polling is the only reliable fallback.
        Wrapped in try/except so one bad tick (e.g. a widget mid-teardown)
        can't silently kill this self-rescheduling loop forever."""
        try:
            self._sync_all_input_passthrough()
        except Exception:
            pass
        self.after(250, self._poll_input_passthrough)

    def _sync_all_input_passthrough(self):
        # Focusing either the main window or the queue panel counts as the
        # whole app being focused, so both toggle passthrough together.
        # Checked at the Win32 level (see _hwnd_is_foreground), not via Tk's
        # own focus_get() bookkeeping, which can drift from what Windows
        # actually considers focused.
        focused = _hwnd_is_foreground(_root_hwnd(self))
        if not focused and self._queue_panel is not None:
            focused = self._queue_panel.has_os_focus()

        if self.state() != "withdrawn" and sys.platform == "win32":
            if self._passthrough != (not focused):
                self._passthrough = not focused
                _set_click_through(_root_hwnd(self), self._passthrough)
                self._update_title_bar()

        if self._queue_panel is not None:
            self._queue_panel.set_input_passthrough(not focused)

    def _grab_os_focus(self):
        """Pull real OS input focus onto the overlay so it's ready to use
        (this also disables click-through on the next passthrough sync),
        without needing a blind click - clicking no longer focuses the
        overlay once it's click-through, so F1 is the way back in.

        The Win32 foreground grab (_force_foreground_window) is what
        actually matters here, not Tk's focus_force(): the hotkey fires on
        a background thread and reaches here via `after`, well removed from
        the original keypress, which is exactly the case Windows' foreground
        -lock heuristic is designed to block. A plain SetForegroundWindow()
        call in that situation is routinely a no-op - it doesn't error, it
        just silently does nothing, leaving the window looking focused to
        our own bookkeeping while Windows itself never moved focus at all.
        focus_force() is still called after, so Tk hands keyboard input to
        the right widget once the window is genuinely foreground."""
        if sys.platform == "win32":
            _force_foreground_window(_root_hwnd(self))
        self.focus_force()

    # ----- drag handling (since title bar is removed) -----
    def _build_drag_bar(self):
        drag_bar = tk.Frame(self, bg="#161b22", height=28)
        drag_bar.pack(fill="x", side="top")

        # Buttons packed before the title label so pack() reserves their
        # space first - a long title text then just clips itself against
        # whatever's left, instead of squeezing a later-packed button out
        # of the row entirely.
        close_btn = ttk.Button(
            drag_bar, text="✕", style="IconClose.TButton", command=self.quit_app
        )
        close_btn.pack(side="right", padx=4)

        settings_btn = ttk.Button(
            drag_bar,
            text="⚙",
            style="IconSettings.TButton",
            command=self._open_hotkey_settings,
        )
        settings_btn.pack(side="right", padx=2)

        self._title_label = tk.Label(
            drag_bar,
            text=self._title_text(),
            bg="#161b22",
            fg="#c9d1d9",
            font=("Segoe UI", 9),
        )
        self._title_label.pack(side="left", padx=8)

        for widget in (drag_bar, self._title_label):
            widget.bind("<ButtonPress-1>", self._start_move)
            widget.bind("<B1-Motion>", self._do_move)
            widget.bind("<ButtonRelease-1>", lambda _e: self._save_position())

        # --- view/panel tabs (own row, not the titlebar - a long
        # "(F1 to hide)" title-state suffix can otherwise grow enough to
        # hide these) ---
        tab_row = tk.Frame(self, bg="#0d1117")
        tab_row.pack(fill="x", side="top", padx=8, pady=(4, 0))

        self._btn_resource = ttk.Button(
            tab_row,
            text="Resource",
            style="Tab.TButton",
            command=lambda: self._set_view("resource"),
        )
        self._btn_resource.state(
            ["selected" if self._view_mode == "resource" else "!selected"]
        )
        self._btn_resource.pack(side="left", padx=(0, 2))

        self._btn_location = ttk.Button(
            tab_row,
            text="Location",
            style="Tab.TButton",
            command=lambda: self._set_view("location"),
        )
        self._btn_location.state(
            ["selected" if self._view_mode == "location" else "!selected"]
        )
        self._btn_location.pack(side="left", padx=(0, 2))

        self._btn_recipe = ttk.Button(
            tab_row,
            text="Recipe",
            style="Tab.TButton",
            command=lambda: self._set_view("recipe"),
        )
        self._btn_recipe.state(
            ["selected" if self._view_mode == "recipe" else "!selected"]
        )
        self._btn_recipe.pack(side="left", padx=(0, 2))

        self._btn_queue_panel = ttk.Button(
            tab_row, text="Queue", style="Tab.TButton", command=self.toggle_queue_panel
        )
        self._btn_queue_panel.state(["!selected"])
        self._btn_queue_panel.pack(side="left", padx=(8, 2))

    def _title_text(self):
        action = "focus" if self._passthrough else "hide"
        return f"⠿  CraftMap Resources   ({self.toggle_key} to {action})"

    def _update_title_bar(self):
        self._title_label.config(text=self._title_text())

    def _open_hotkey_settings(self):
        win = tk.Toplevel(self)
        win.configure(bg="#0d1117")
        win.overrideredirect(True)  # match the app: no native title bar
        win.attributes("-topmost", True)

        def _bring_to_front():
            # Two topmost overrideredirect windows (this dialog and the
            # main overlay) don't have a guaranteed relative order in
            # Windows' topmost band - the main overlay's own periodic
            # focus/passthrough bookkeeping can end up re-asserting itself
            # above this dialog. Force it back after every action that's
            # been observed to trigger that (open, entering listen mode,
            # and re-registering the global hotkey via change_hotkey).
            win.lift()
            win32util.force_foreground_window(win32util.root_hwnd(win))

        drag_bar = tk.Frame(win, bg="#161b22", height=28)
        drag_bar.pack(fill="x", side="top")

        title_label = tk.Label(
            drag_bar,
            text="Hotkey Settings",
            bg="#161b22",
            fg="#c9d1d9",
            font=("Segoe UI", 9),
        )
        title_label.pack(side="left", padx=8)

        def _restore_hotkey():
            # If listening was in progress (or a rebind attempt failed to
            # register), the current hotkey may currently be un-registered
            # - make sure the app doesn't end up with none at all.
            if HOTKEY_AVAILABLE and self._hotkey_handle is None:
                self._hotkey_handle = keyboard.add_hotkey(
                    self.toggle_key, self._on_hotkey
                )

        def _close_dialog():
            _restore_hotkey()
            win.destroy()

        ttk.Button(
            drag_bar, text="✕", style="IconClose.TButton", command=_close_dialog
        ).pack(side="right", padx=4)

        drag = {"x": 0, "y": 0}

        def _start_move(event):
            drag["x"], drag["y"] = event.x, event.y

        def _do_move(_event):
            x = win.winfo_pointerx() - drag["x"]
            y = win.winfo_pointery() - drag["y"]
            win.geometry(f"+{x}+{y}")

        for widget in (drag_bar, title_label):
            widget.bind("<ButtonPress-1>", _start_move)
            widget.bind("<B1-Motion>", _do_move)

        win.bind("<Escape>", lambda _e: _close_dialog())

        body = tk.Frame(win, bg="#0d1117")
        body.pack(fill="both", expand=True)

        tk.Label(
            body,
            text="Hide/show key",
            bg="#0d1117",
            fg="#c9d1d9",
            font=("Segoe UI", 9),
        ).pack(padx=16, pady=(14, 4), anchor="w")

        display = tk.Label(
            body,
            text=self.toggle_key,
            bg="#161b22",
            fg="#c9d1d9",
            font=("Segoe UI", 11, "bold"),
            anchor="center",
            padx=8,
            pady=10,
        )
        display.pack(padx=16, pady=4, fill="x")

        msg = tk.Label(body, text="", bg="#0d1117", fg="#da3633", font=("Segoe UI", 8))
        msg.pack(padx=16, pady=(2, 4), anchor="w")

        mods: list[str] = []

        def _combo(extra=None):
            parts = [m for m in _MODIFIER_ORDER if m in mods]
            if extra:
                parts.append(extra)
            return "+".join(parts)

        def _stop_listening():
            win.unbind("<KeyPress>")
            win.unbind("<KeyRelease>")
            rebind_btn.configure(text="Rebind", style="Neutral.TButton")

        def _on_key_press(event):
            mod = _MODIFIER_KEYSYMS.get(event.keysym)
            if mod:
                if mod not in mods:
                    mods.append(mod)
                display.config(text=_combo() + "+...")
                _bring_to_front()
                return "break"
            new_key = _combo(_keysym_to_key_name(event.keysym))
            mods.clear()
            _stop_listening()
            try:
                self.change_hotkey(new_key)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                msg.config(text=f"Invalid key: {exc}")
                display.config(text=self.toggle_key)
                _restore_hotkey()
            else:
                msg.config(text="")
                display.config(text=new_key)
            _bring_to_front()
            return "break"

        def _on_key_release(event):
            mod = _MODIFIER_KEYSYMS.get(event.keysym)
            if mod and mod in mods:
                mods.remove(mod)
                display.config(text=_combo() + "+..." if mods else "Press a key...")
            # Releasing any key while listening (even ones we don't track,
            # e.g. the combo's own final key) has been observed to let the
            # main overlay's focus/passthrough polling re-assert itself
            # above this dialog - pull it back every time.
            _bring_to_front()
            return "break"

        def _start_listening():
            # Stop listening to the *current* global hotkey while capturing
            # a new one - otherwise pressing keys that happen to match it
            # (an easy edge case: rebinding onto the same combo, or just
            # habit) fires it mid-capture, which shows/focuses the main
            # overlay and buries this dialog under it.
            if HOTKEY_AVAILABLE and self._hotkey_handle is not None:
                try:
                    keyboard.remove_hotkey(self._hotkey_handle)
                except Exception:  # pylint: disable=broad-exception-caught
                    pass
                self._hotkey_handle = None
            mods.clear()
            msg.config(text="")
            display.config(text="Press a key...")
            rebind_btn.configure(text="Press a key... (Esc to cancel)", style="Accent.TButton")
            win.bind("<KeyPress>", _on_key_press)
            win.bind("<KeyRelease>", _on_key_release)
            win.focus_set()
            _bring_to_front()

        btn_row = tk.Frame(body, bg="#0d1117")
        btn_row.pack(padx=16, pady=(4, 12), fill="x")

        rebind_btn = ttk.Button(
            btn_row, text="Rebind", command=_start_listening, style="Neutral.TButton"
        )
        rebind_btn.pack(side="left", fill="x", expand=True)

        ttk.Button(
            btn_row, text="Close", command=_close_dialog, style="Neutral.TButton"
        ).pack(side="left", padx=(6, 0))

        win.update_idletasks()
        w = max(300, win.winfo_reqwidth())
        h = win.winfo_reqheight()
        x = self.winfo_x() + (self.winfo_width() - w) // 2
        y = self.winfo_y() + (self.winfo_height() - h) // 2
        win.geometry(f"{w}x{h}+{x}+{y}")
        _bring_to_front()

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
        if self._hotkey_suppressed:
            return
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
            # The keys making up new_key are still physically held right
            # now (the user just pressed them to capture this combo) - the
            # `keyboard` library can treat that as an immediate trigger the
            # instant it's registered, firing toggle() and stealing OS focus
            # out from under the settings dialog that's still open. Ignore
            # hotkey fires for a short guard window after every rebind.
            self._hotkey_suppressed = True
            self.after(500, lambda: setattr(self, "_hotkey_suppressed", False))
        self.toggle_key = new_key
        self._update_title_bar()
        if self._queue_panel is not None:
            self._queue_panel.update_title_bar()
        cfg = load_config()
        cfg["toggle_key"] = new_key
        save_config(cfg)

    def register_hotkey(self):
        """Called from main() once the keyboard thread is running."""
        if HOTKEY_AVAILABLE:
            self._hotkey_handle = keyboard.add_hotkey(self.toggle_key, self._on_hotkey)

    def _set_view(self, mode: str):
        _LiveDropdown.hide_all()
        self._view_mode = mode
        for btn, key in (
            (self._btn_resource, "resource"),
            (self._btn_location, "location"),
            (self._btn_recipe, "recipe"),
        ):
            btn.state(["selected" if mode == key else "!selected"])
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
            win32util.redraw_window(self.winfo_id())

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
        entry = _bordered_entry(
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

        style.layout(
            "Thin.Vertical.TScrollbar",
            [  # type: ignore[arg-type]
                (
                    "Vertical.TScrollbar.trough",
                    {
                        "sticky": "ns",
                        "children": [
                            (
                                "Vertical.TScrollbar.thumb",
                                {"expand": "1", "sticky": "nswe"},
                            ),
                        ],
                    },
                ),
            ],
        )
        style.configure(
            "Thin.Vertical.TScrollbar",
            background="#30363d",
            troughcolor="#161b22",
            bordercolor="#161b22",
            relief="flat",
            width=6,
        )
        style.map(
            "Thin.Vertical.TScrollbar",
            background=[("active", "#484f58"), ("pressed", "#58a6ff")],
        )

        self._tree_frame = tk.Frame(self, bg="#0d1117")
        self._tree_frame.pack(fill="both", expand=True, padx=8, pady=4)

        self._tree_frame.grid_rowconfigure(0, weight=1)
        self._tree_frame.grid_columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(self._tree_frame, show="tree")
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(
            self._tree_frame,
            orient="vertical",
            style="Thin.Vertical.TScrollbar",
            command=self.tree.yview,
        )
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
        _LiveDropdown(status_menu)

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
        self.notes_entry = _bordered_entry(
            form, bg="#161b22", fg="#c9d1d9", insertbackground="#c9d1d9", relief="flat"
        )
        self.notes_entry.pack(fill="x", ipady=3)

        # buttons
        btns = tk.Frame(form, bg="#0d1117")
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(
            btns, text="Add", command=self.add_entry, style="Success.TButton"
        ).pack(side="left", padx=2)
        ttk.Button(
            btns, text="Update", command=self.update_entry, style="Accent.TButton"
        ).pack(side="left", padx=2)
        ttk.Button(
            btns, text="Clear", command=self.clear_form, style="Neutral.TButton"
        ).pack(side="left", padx=2)
        ttk.Button(
            btns, text="Delete", command=self.delete_entry, style="Danger.TButton"
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
            if (
                self._queue_panel
                and self._queue_panel.pinned
                and self._queue_panel.is_visible()
                and not self._queue_panel.has_os_focus()
            ):
                # The pinned queue panel is still up on its own with the
                # main window hidden - the first F1 press should hand it
                # focus, not also unhide the main overlay. A second press
                # (queue now focused, main still hidden) falls through below.
                self._queue_panel.grab_os_focus()
                self._sync_all_input_passthrough()
                return

            self.deiconify()
            self.attributes("-topmost", True)
            self._grab_os_focus()
            self._sync_all_input_passthrough()
            if (
                self._queue_panel
                and not self._queue_panel.pinned
                and self._queue_panel_was_visible
            ):
                self._queue_panel.show()
            return

        focused = _hwnd_is_foreground(_root_hwnd(self))
        if not focused and self._queue_panel is not None:
            focused = self._queue_panel.has_os_focus()

        if not focused:
            # Visible but click-through (unfocused) - the hotkey's job here
            # is to hand focus back, not hide a window the user can still see.
            self._grab_os_focus()
            self._sync_all_input_passthrough()
            return

        self.hide()

    def hide(self):
        self.withdraw()
        if self._queue_panel:
            self._queue_panel_was_visible = self._queue_panel.is_visible()
            self._queue_panel.dismiss()

    def toggle_queue_panel(self):
        if self._queue_panel is None:
            self._queue_panel = CraftQueuePanel(self, self)
            self._queue_panel.on_hide_cb = self._on_queue_panel_hide
            self._btn_queue_panel.state(["selected"])
        elif self._queue_panel.is_visible():
            self._queue_panel.hide()
        else:
            self._queue_panel.show()
            self._btn_queue_panel.state(["selected"])

    def _on_queue_panel_hide(self):
        self._btn_queue_panel.state(["!selected"])

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
        self._btn_queue_panel.state(["selected"])

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
            self._sel_left,
            text="Recipe:",
            bg="#0d1117",
            fg="#8b949e",
            font=("Segoe UI", 8),
        ).pack(side="left", padx=(0, 4))
        self._recipe_var = tk.StringVar()
        self._recipe_combo = ttk.Combobox(
            self._sel_left, textvariable=self._recipe_var, width=28
        )
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
        ttk.Button(
            self._sel_left,
            text="New",
            command=self.clear_recipe_form,
            style="NeutralSmall.TButton",
        ).pack(side="left", padx=(0, 8))
        tk.Label(
            self._sel_left, text="×", bg="#0d1117", fg="#8b949e", font=("Segoe UI", 9)
        ).pack(side="left")
        self._recipe_qty_var = tk.StringVar(value="1")
        qty_entry = _bordered_entry(
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
        self._btn_bd_breakdown = ttk.Button(
            sel,
            text="Breakdown",
            style="Tab.TButton",
            command=lambda: self._set_recipe_mode("breakdown"),
        )
        self._btn_bd_breakdown.state(["selected"])
        self._btn_bd_breakdown.pack(side="right", padx=(2, 0))
        self._btn_bd_totals = ttk.Button(
            sel,
            text="Totals",
            style="Tab.TButton",
            command=lambda: self._set_recipe_mode("totals"),
        )
        self._btn_bd_totals.state(["!selected"])
        self._btn_bd_totals.pack(side="right", padx=(0, 2))
        self._btn_bd_usedin = ttk.Button(
            sel,
            text="Used In",
            style="Tab.TButton",
            command=lambda: self._set_recipe_mode("usedin"),
        )
        self._btn_bd_usedin.state(["!selected"])
        self._btn_bd_usedin.pack(side="right", padx=(0, 2))

        # --- PanedWindow: breakdown tree (top) + edit form (bottom) ---
        self._pw_recipe = tk.PanedWindow(
            self._recipe_frame,
            orient=tk.VERTICAL,
            bg="#21262d",
            sashwidth=5,
            sashrelief="flat",
            sashpad=0,
            handlesize=0,
            bd=0,
        )
        self._pw_recipe.pack(fill="both", expand=True, pady=(2, 4))
        self._pw_recipe.bind("<ButtonRelease-1>", self._save_recipe_split)

        # Top pane: breakdown tree
        self._bd_frame = tk.Frame(self._pw_recipe, bg="#0d1117")
        self._pw_recipe.add(
            self._bd_frame, height=self._recipe_split, minsize=40, stretch="always"
        )
        bd_frame = self._bd_frame
        bd_frame.grid_rowconfigure(0, weight=1)
        bd_frame.grid_columnconfigure(0, weight=1)
        self._recipe_breakdown_tree = ttk.Treeview(bd_frame, show="tree")
        self._recipe_breakdown_tree.grid(row=0, column=0, sticky="nsew")
        bd_vsb = ttk.Scrollbar(
            bd_frame,
            orient="vertical",
            style="Thin.Vertical.TScrollbar",
            command=self._recipe_breakdown_tree.yview,
        )
        bd_vsb.grid(row=0, column=1, sticky="ns")
        self._recipe_breakdown_tree.configure(yscrollcommand=_autohide_yscroll(bd_vsb))
        self._recipe_breakdown_tree.bind("<ButtonRelease-1>", self._on_breakdown_click)
        self._recipe_breakdown_tree.bind(
            "<Double-Button-1>", self._on_breakdown_double_click
        )
        self._recipe_root_open = True
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
        _bordered_entry(
            name_row,
            textvariable=self._recipe_name_var,
            bg="#161b22",
            fg="#c9d1d9",
            insertbackground="#c9d1d9",
            relief="flat",
        ).pack(side="left", fill="x", expand=True, ipady=3, padx=(0, 8))

        self._station_inner = tk.Frame(form, bg="#0d1117")
        self._station_inner.pack(fill="x", pady=(0, 4))
        # Header labels share this grid with the row widgets below (added in
        # _add_station_row) so their columns line up exactly regardless of
        # font metrics - packing them in a separate sibling frame can't
        # guarantee that.
        tk.Label(
            self._station_inner,
            text="Stations:",
            bg="#0d1117",
            fg="#8b949e",
            font=("Segoe UI", 8),
        ).grid(row=0, column=0, padx=(0, 4), pady=(0, 2), sticky="w")
        tk.Label(
            self._station_inner,
            text="Auto (s)",
            bg="#0d1117",
            fg="#6e7681",
            font=("Segoe UI", 7),
            anchor="w",
        ).grid(row=0, column=1, padx=(0, 4), pady=(0, 2), sticky="w")
        tk.Label(
            self._station_inner,
            text="Manual (s)",
            bg="#0d1117",
            fg="#6e7681",
            font=("Segoe UI", 7),
            anchor="w",
        ).grid(row=0, column=2, padx=(0, 4), pady=(0, 2), sticky="w")
        self._station_rows: list = []
        self._add_station_row()

        tk.Label(
            form, text="Outputs:", bg="#0d1117", fg="#8b949e", font=("Segoe UI", 8)
        ).pack(anchor="w", pady=(0, 2))
        self._out_inner = tk.Frame(form, bg="#0d1117")
        self._out_inner.pack(fill="x", pady=(0, 4))
        self._out_rows: list = []
        self._add_output_row()

        tk.Label(
            form, text="Ingredients:", bg="#0d1117", fg="#8b949e", font=("Segoe UI", 8)
        ).pack(anchor="w", pady=(0, 2))

        # scrollable ingredient rows
        ing_outer = tk.Frame(form, bg="#0d1117")
        ing_outer.pack(fill="x")
        self._ing_canvas = tk.Canvas(
            ing_outer, bg="#0d1117", highlightthickness=0, height=110
        )
        ing_vsb = ttk.Scrollbar(
            ing_outer,
            orient="vertical",
            style="Thin.Vertical.TScrollbar",
            command=self._ing_canvas.yview,
        )
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

        ttk.Button(
            btn_row,
            text="+ Station",
            command=self._add_station_row,
            style="NeutralSmall.TButton",
        ).pack(side="left", padx=(0, 6))
        ttk.Button(
            btn_row,
            text="+ Output",
            command=self._add_output_row,
            style="NeutralSmall.TButton",
        ).pack(side="left", padx=(0, 6))
        ttk.Button(
            btn_row,
            text="+ Ingredient",
            command=self._add_ingredient_row,
            style="NeutralSmall.TButton",
        ).pack(side="left", padx=(0, 10))
        ttk.Button(
            btn_row,
            text="Save",
            command=self.save_recipe_action,
            style="Success.TButton",
        ).pack(side="left", padx=2)
        ttk.Button(
            btn_row,
            text="Clear",
            command=self.clear_recipe_form,
            style="Neutral.TButton",
        ).pack(side="left", padx=2)
        ttk.Button(
            btn_row,
            text="Delete",
            command=self.delete_recipe_action,
            style="Danger.TButton",
        ).pack(side="right", padx=(2, 18))

    def _all_ingredient_options(self):
        produced = get_all_output_names()
        resource_names = distinct_values("resource")
        ingredient_names = distinct_ingredient_names()
        return sorted(set(produced + resource_names + ingredient_names), key=str.lower)

    def _add_output_row(self, name="", qty=1):
        row_frame = tk.Frame(self._out_inner, bg="#0d1117")
        row_frame.pack(fill="x", pady=1)
        name_var = tk.StringVar(value=str(name))
        qty_var = tk.StringVar(value=str(qty))
        name_cb = ttk.Combobox(row_frame, textvariable=name_var, width=24)
        name_cb["values"] = get_all_output_names()
        name_cb.pack(side="left", padx=(0, 4))
        _LiveDropdown(
            name_cb,
            pre_fn=lambda cb=name_cb: cb.configure(values=get_all_output_names()),
        )
        qty_entry = _bordered_entry(
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
            if len(self._out_rows) <= 1:
                return
            self._out_rows = [x for x in self._out_rows if x is not row]
            row["frame"].destroy()

        rm_btn = ttk.Button(
            row_frame, text="×", command=remove, style="Remove.TButton"
        )
        rm_btn.pack(side="left")
        self._out_rows.append(row)

    def _clear_output_rows(self):
        for row in self._out_rows:
            row["frame"].destroy()
        self._out_rows = []

    def _add_station_row(self, station="", auto="", manual=""):
        station_var = tk.StringVar(value=str(station))
        auto_var = tk.StringVar(value=str(auto))
        manual_var = tk.StringVar(value=str(manual))
        station_cb = ttk.Combobox(self._station_inner, textvariable=station_var, width=14)
        station_cb["values"] = get_all_stations()
        _LiveDropdown(
            station_cb,
            pre_fn=lambda cb=station_cb: cb.configure(values=get_all_stations()),
        )
        auto_entry = _bordered_entry(
            self._station_inner,
            textvariable=auto_var,
            width=6,
            bg="#161b22",
            fg="#c9d1d9",
            insertbackground="#c9d1d9",
            relief="flat",
        )
        manual_entry = _bordered_entry(
            self._station_inner,
            textvariable=manual_var,
            width=6,
            bg="#161b22",
            fg="#c9d1d9",
            insertbackground="#c9d1d9",
            relief="flat",
        )
        row = {
            "station_var": station_var,
            "auto_var": auto_var,
            "manual_var": manual_var,
            "station_widget": station_cb,
            "auto_widget": auto_entry,
            "manual_widget": manual_entry,
        }

        def remove():
            if len(self._station_rows) <= 1:
                return
            self._station_rows = [x for x in self._station_rows if x is not row]
            station_cb.destroy()
            auto_entry.destroy()
            manual_entry.destroy()
            row["remove_widget"].destroy()
            self._relayout_station_rows()

        rm_btn = ttk.Button(
            self._station_inner, text="×", command=remove, style="Remove.TButton"
        )
        row["remove_widget"] = rm_btn
        self._station_rows.append(row)
        self._relayout_station_rows()

    def _relayout_station_rows(self):
        # Row 0 is the Auto/Manual header; rows shift up here after a
        # removal so no gap is left where a deleted row used to be.
        for i, row in enumerate(self._station_rows):
            r = i + 1
            row["station_widget"].grid(row=r, column=0, padx=(0, 4), pady=1, sticky="w")
            row["auto_widget"].grid(
                row=r, column=1, padx=(0, 4), pady=1, ipady=2, sticky="w"
            )
            row["manual_widget"].grid(
                row=r, column=2, padx=(0, 4), pady=1, ipady=2, sticky="w"
            )
            row["remove_widget"].grid(row=r, column=3, pady=1, sticky="w")

    def _clear_station_rows(self):
        for row in self._station_rows:
            row["station_widget"].destroy()
            row["auto_widget"].destroy()
            row["manual_widget"].destroy()
            row["remove_widget"].destroy()
        self._station_rows = []

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
        self._clear_station_rows()
        for st_name, auto_s, manual_s in get_recipe_stations(recipe_id):
            self._add_station_row(
                st_name,
                f"{auto_s:g}" if auto_s is not None else "",
                f"{manual_s:g}" if manual_s is not None else "",
            )
        if not self._station_rows:
            self._add_station_row()
        self._clear_output_rows()
        for out_name, out_qty in get_recipe_outputs(recipe_id):
            self._add_output_row(out_name, out_qty)
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
        qty_entry = _bordered_entry(
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

        rm_btn = ttk.Button(
            row_frame, text="×", command=remove, style="Remove.TButton"
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
        path_key = _node_path_key(node, path_parts)
        is_done = path_key in checked
        has_options = _node_has_step_options(node)
        qty_str = f"{qty:g}"
        label = f"{qty_str}×  {name}"
        if used_recipe and used_recipe != name:
            label += f"  [{used_recipe}]"
        if has_options:
            label += "  ▾"
        active_seconds, active_mode = _node_active_seconds(node)
        remaining = _subtree_remaining_seconds(node, path_parts, checked)
        label += _format_craft_meta_suffix(
            node.get("station"), active_seconds, active_mode, remaining
        )
        label += _format_byproducts_suffix(node.get("byproducts"))
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
            "node": node,
            "path_parts": path_parts,
            "has_options": has_options,
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

    def _open_recipe_step_popup(self, tree, iid, info):
        node = info["node"]
        bbox = tree.bbox(iid)
        if not bbox:
            return
        x = tree.winfo_rootx() + bbox[0]
        y = tree.winfo_rooty() + bbox[1] + bbox[3]

        is_root = info.get("is_root", False)
        # Totals mode's aggregated entries don't carry their own "name" (see
        # collect_basic_crafted) - stashed separately as ingredient_name.
        ingredient_name = info.get("ingredient_name") or node.get("name")

        def on_alt(alt_recipe_id, alt_recipe_name):
            # The root recipe is forced via _root_recipe_id, so a generic
            # alt_pref (keyed by ingredient name) wouldn't take effect here
            # the way it does for a sub-ingredient - switch the whole panel
            # to viewing that other recipe instead, same as double-clicking
            # a "used in" result.
            if is_root:
                self._load_recipe_into_form(alt_recipe_id, alt_recipe_name)
            else:
                set_alt_pref(ingredient_name, alt_recipe_id)
            self._refresh_recipe_breakdown()

        def on_station(station, mode):
            set_station_pref(ingredient_name, station, mode)
            self._refresh_recipe_breakdown()

        _StepPopup.show(tree, x, y, node, on_alt, on_station)

    def _on_bd_toggled(self, event):
        # Every checkbox click fully rebuilds this tree (see
        # _on_breakdown_click's cascade-check), which re-inserts the root
        # row fresh each time - remember whatever open/closed state the
        # user last set for it here so a rebuild doesn't silently
        # re-expand it right back.
        self._bd_toggled = True
        tree = event.widget
        if tree.exists("recipe_root"):
            self._recipe_root_open = bool(tree.item("recipe_root", "open"))

    def _load_recipe_into_form(self, rid: int, rname: str):
        self._recipe_selected_id = rid
        self._viewing_recipe_id = rid
        self._recipe_var.set(rname)
        self._recipe_combo.icursor("end")
        self._recipe_combo.selection_range(0, "end")
        self._recipe_name_var.set(rname)
        self._clear_station_rows()
        for st_name, auto_s, manual_s in get_recipe_stations(rid):
            self._add_station_row(
                st_name,
                f"{auto_s:g}" if auto_s is not None else "",
                f"{manual_s:g}" if manual_s is not None else "",
            )
        if not self._station_rows:
            self._add_station_row()
        self._clear_output_rows()
        for out_name, out_qty in get_recipe_outputs(rid):
            self._add_output_row(out_name, out_qty)
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
        if info["type"] == "usedin_recipe":
            self._load_recipe_into_form(info["recipe_id"], info["recipe_name"])
            return
        if info["type"] != "ingredient":
            return
        recipe_id = self._viewing_recipe_id
        if recipe_id is None:
            return
        if "image" in tree.identify("element", event.x, event.y):
            node = info.get("node")
            new_done = not info.get("checked", False)
            if info.get("flat") or node is None:
                # Totals mode already flattens the tree into one entry per
                # basic-crafted item name, so there's no subtree structure
                # left to cascade into - just this one entry.
                path_keys = [info["path_key"]]
            else:
                path_keys = _collect_path_keys(node, info["path_parts"])
            set_checked_many(recipe_id, path_keys, new_done)
            self._refresh_recipe_breakdown()
            return
        if info.get("has_options") and info.get("node") is not None:
            self._open_recipe_step_popup(tree, iid, info)

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
        outputs = []
        for row in self._out_rows:
            out_name = row["name_var"].get().strip()
            qty_str = row["qty_var"].get().strip()
            if not out_name:
                continue
            try:
                qty = float(qty_str)
                if qty <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showwarning(
                    "Invalid quantity", f"Invalid quantity for output '{out_name}'."
                )
                self.after(0, self._recipe_repaint)
                return
            outputs.append((out_name, qty))
        if not outputs:
            messagebox.showwarning("Missing info", "Add at least one output.")
            self.after(0, self._recipe_repaint)
            return
        existing_id = get_recipe_by_name(name)
        if existing_id is not None and existing_id != self._recipe_selected_id:
            messagebox.showwarning(
                "Duplicate", f"A recipe named '{name}' already exists."
            )
            self.after(0, self._recipe_repaint)
            return
        stations = []
        try:
            for row in self._station_rows:
                st_name = row["station_var"].get().strip()
                if not st_name:
                    continue
                auto_str = row["auto_var"].get().strip()
                auto_s = float(auto_str) if auto_str else None
                manual_str = row["manual_var"].get().strip()
                manual_s = float(manual_str) if manual_str else None
                stations.append((st_name, auto_s, manual_s))
        except ValueError:
            messagebox.showwarning(
                "Invalid time", "Craft time must be a number of seconds."
            )
            self.after(0, self._recipe_repaint)
            return
        if not stations:
            messagebox.showwarning("Missing info", "Add at least one station.")
            self.after(0, self._recipe_repaint)
            return
        rid = save_recipe(
            self._recipe_selected_id,
            name,
            outputs,
            ingredients,
            stations,
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
        if hasattr(self, "_station_rows"):
            self._clear_station_rows()
            self._add_station_row()
        if hasattr(self, "_out_rows"):
            self._clear_output_rows()
            self._add_output_row()
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
            btn.state(["selected" if mode == key else "!selected"])
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
        station_prefs = get_station_prefs()
        node = resolve_recipe_tree(
            output_name,
            qty_needed=craft_qty,
            _root_recipe_id=recipe_id,
            _alt_prefs=alt_prefs,
            _station_prefs=station_prefs,
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
            root_has_options = _node_has_step_options(node)
            if root_has_options:
                root_label += "  ▾"
            active_seconds, active_mode = _node_active_seconds(node)
            root_remaining = _subtree_remaining_seconds(node, [], checked)
            root_label += _format_craft_meta_suffix(
                node.get("station"), active_seconds, active_mode, root_remaining
            )
            root_label += _format_byproducts_suffix(node.get("byproducts"))
            root_path_key = _node_path_key(node, [])
            root_is_done = root_path_key in checked
            root_img = self.img_checked if root_is_done else self.img_unchecked
            root_iid = tree.insert(
                "",
                "end",
                iid="recipe_root",
                text=root_label,
                image=root_img,
                open=self._recipe_root_open,
                tags=("root",),
            )
            self._recipe_iid_info[root_iid] = {
                "type": "ingredient",
                "recipe_id": recipe_id,
                "path_key": root_path_key,
                "checked": root_is_done,
                "node": node,
                "path_parts": [],
                "has_options": root_has_options,
                "is_root": True,
            }
            for child in node["children"]:
                self._insert_breakdown_node(root_iid, child, recipe_id, [], checked)

    def _refresh_usedin_view(self, tree):
        view_id = self._usedin_recipe_id
        item_name = (
            get_recipe_output_name(view_id) if view_id is not None else None
        ) or ""
        if not item_name:
            tree.insert(
                "",
                "end",
                text="Select a recipe above to see where it's used.",
                tags=("section",),
            )
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
            oq_suffix = f"  ×{output_qty:g}" if output_qty != 1 else ""
            if output_name != rname:
                label += f"  [{output_name}{oq_suffix}]"
            elif oq_suffix:
                label += oq_suffix
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
    def collect_basic_crafted(node, totals=None):
        """Aggregate only the most basic crafted items across the whole tree —
        recipes built entirely from raw materials, with no recipe of their
        own among their ingredients. Assembly recipes (built from other
        recipes) are transparently collapsed past so only the base crafting
        tier surfaces, instead of every intermediate level."""
        if totals is None:
            totals = {}
        for child in node["children"]:
            if not child["is_recipe"]:
                continue
            if any(c["is_recipe"] for c in child["children"]):
                Overlay.collect_basic_crafted(child, totals)
            else:
                entry = totals.setdefault(
                    child["name"],
                    {
                        "is_recipe": True,
                        "qty": 0.0,
                        "output_qty": child.get("output_qty", 1.0),
                        "alts": child.get("alts", []),
                        "raw_names": sorted({c["name"] for c in child["children"]}),
                        "station": child.get("station"),
                        "stations": child.get("stations", []),
                        "auto_craft_seconds": child.get("auto_craft_seconds"),
                        "manual_craft_seconds": child.get("manual_craft_seconds"),
                        "craft_mode": child.get("craft_mode", "auto"),
                        "byproducts": child.get("byproducts", []),
                    },
                )
                entry["qty"] += child["qty"]
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
        root_has_options = _node_has_step_options(node)
        if root_has_options:
            root_label += "  ▾"
        root_active_seconds, root_active_mode = _node_active_seconds(node)
        root_remaining = _subtree_remaining_seconds(node, [], checked)
        root_label += _format_craft_meta_suffix(
            node.get("station"), root_active_seconds, root_active_mode, root_remaining
        )
        root_label += _format_byproducts_suffix(node.get("byproducts"))
        root_path_key = _node_path_key(node, [])
        root_is_done = root_path_key in checked
        root_img = self.img_checked if root_is_done else self.img_unchecked
        header = tree.insert(
            "",
            "end",
            iid="recipe_root",
            text=root_label,
            image=root_img,
            open=self._recipe_root_open,
            tags=("total_header",),
        )
        self._recipe_iid_info[header] = {
            "type": "ingredient",
            "recipe_id": recipe_id,
            "path_key": root_path_key,
            "checked": root_is_done,
            "node": node,
            "path_parts": [],
            "has_options": root_has_options,
            "is_root": True,
        }

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

        basic = self.collect_basic_crafted(node)
        if basic:
            craft_hdr = tree.insert(
                header, "end", text="── Crafted ──", open=True, tags=("section",)
            )
            self._recipe_iid_info[craft_hdr] = {"type": "root"}
            for res_name, info in sorted(basic.items(), key=lambda x: x[0].lower()):
                qty = info["qty"]
                oq = info["output_qty"]
                crafts = math.ceil(qty / oq)
                path_key = f"__craft__|{res_name}"
                is_done = path_key in checked
                img = self.img_checked if is_done else self.img_unchecked
                has_options = _node_has_step_options(info)
                suffix = f"  ({crafts:g} crafts)" if oq > 1 else ""
                if has_options:
                    suffix += "  ▾"
                active_seconds, active_mode = _node_active_seconds(info)
                own_time = (active_seconds * crafts) if active_seconds else 0.0
                remaining = 0.0 if is_done else own_time
                suffix += _format_craft_meta_suffix(
                    info.get("station"), active_seconds, active_mode, remaining
                )
                suffix += _format_byproducts_suffix(info.get("byproducts"))
                iid = tree.insert(
                    craft_hdr,
                    "end",
                    text=f"{qty:g}×  {res_name}{suffix}",
                    image=img,
                    open=False,
                    tags=("done" if is_done else "ingredient",),
                )
                self._recipe_iid_info[iid] = {
                    "type": "ingredient",
                    "recipe_id": recipe_id,
                    "path_key": path_key,
                    "checked": is_done,
                    "node": info,
                    "ingredient_name": res_name,
                    "has_options": has_options,
                    "flat": True,
                }
                for raw_name in info["raw_names"]:
                    raw_iid = tree.insert(
                        iid, "end", text=f"    {raw_name}", tags=("location",)
                    )
                    self._recipe_iid_info[raw_iid] = {"type": "location"}
                    for (
                        sector,
                        system_name,
                        planet,
                        status,
                    ) in get_deposits_for_ingredient(raw_name):
                        parts = [p for p in (sector, system_name, planet) if p]
                        loc_text = " / ".join(parts)
                        if status and status not in ("Unknown", ""):
                            loc_text += f"  [{status}]"
                        loc_iid = tree.insert(
                            raw_iid,
                            "end",
                            text=f"      📍 {loc_text}",
                            tags=("location",),
                        )
                        self._recipe_iid_info[loc_iid] = {"type": "location"}

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
    if not win32util.check_single_instance():
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
    _relaunch_via_pythonw_if_needed()
    main()
