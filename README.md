# CraftMap

A Windows desktop overlay for tracking resource deposits and crafting recipes
while playing SpaceCraft, plus a companion browser-based ship builder for
designing ships against the game's real part catalogue.

See [NOTICE.md](NOTICE.md) for important information on licensing — this
repository mixes original code (MIT-licensed) with reference content extracted
from the game itself (not covered by that license).

## Overlay

A single-file Python/tkinter app that sits always-on-top over the game window
(borderless mode) and toggles visible/hidden via a global hotkey (default: F1).

**Run from source:**
```
python overlay.py
```
On Windows, the script auto-relaunches itself via `pythonw.exe` to suppress
the console window. Run as administrator if the global hotkey fails to
register.

**Install the only non-stdlib dependency:**
```
pip install -r requirements.txt
```

**Build a standalone executable:**
```
build.bat
```
Runs PyInstaller (`--onefile --noconsole`) and copies the output to
`CraftMap.exe` in the project root.

Tracks resource deposit locations (type, sector, system, planet, status) and
crafting recipes with recursive ingredient-tree resolution, alternate-recipe
selection, and persistent checkbox/progress state — all backed by a local
SQLite database (`resources.db`).

## Ship Builder

A Three.js browser-based ship designer in `shipbuilder/`.

**Launch:**
```
cd shipbuilder
python -m http.server 8765
```
then open `http://localhost:8765`, or just double-click `shipbuilder/start.bat`.

Full part catalogue (hull frames, cockpits, engines, wings, modules) with
real dimensions and mesh sizes derived from the game's own files, a
module-slot system for internal components, and a live ship-stats panel
(structure, propulsion, power, cargo, combat).

## Reverse-engineering tools

`tools/` contains the scripts used to extract and convert game assets from
the game's `res.pak` (mesh format decoding, pak archive parsing, material
color extraction, etc.), along with detailed format notes in
`tools/hmd_format_notes.md`.
