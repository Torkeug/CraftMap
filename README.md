# CraftMap

A Windows desktop overlay for tracking resource deposits and crafting recipes
while playing SpaceCraft.

See [NOTICE.md](NOTICE.md) for licensing information.

A companion browser-based ship builder for designing ships against the
game's real part catalogue lives in a separate repository:
[SpaceCraft-ShipBuilder](https://github.com/Torkeug/SpaceCraft-ShipBuilder).

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
