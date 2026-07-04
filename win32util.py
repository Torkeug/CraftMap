# pylint: disable=missing-function-docstring
"""Win32 interop helpers for the overlay window: hwnd resolution, OS focus
detection, click-through, composited-window flicker reduction, and the
single-instance mutex check.

Centralized here instead of scattered ctypes.windll calls throughout
overlay.py, because that scattering is exactly what caused a real bug: two
call sites used different hwnd semantics (an inner content window vs. the
actual top-level ancestor) for what was supposed to be the same "the
overlay's window" concept, and it took real debugging to notice. Keeping a
single set of prototypes and functions here means every caller shares the
same hwnd semantics by construction.
"""

import ctypes

GA_ROOT = 2
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
WS_EX_COMPOSITED = 0x02000000
# SWP_NOSIZE|SWP_NOMOVE|SWP_NOZORDER|SWP_NOACTIVATE|SWP_FRAMECHANGED
_SWP_FLAGS = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020
# RDW_INVALIDATE|RDW_ERASE|RDW_ALLCHILDREN|RDW_UPDATENOW
_RDW_FLAGS = 0x0185
ERROR_ALREADY_EXISTS = 183

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_user32.GetAncestor.restype = ctypes.c_void_p
_user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_int]
_user32.GetForegroundWindow.restype = ctypes.c_void_p
_user32.GetWindowThreadProcessId.restype = ctypes.c_uint32
_user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_user32.AttachThreadInput.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int]
_user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
_user32.BringWindowToTop.argtypes = [ctypes.c_void_p]


def root_hwnd(widget):
    """winfo_id() on Windows returns an inner content-window handle, not the
    real top-level HWND Windows uses for Z-order/hit-testing/activation -
    walk up to the actual root."""
    hwnd = widget.winfo_id()
    root = _user32.GetAncestor(hwnd, GA_ROOT)
    return root or hwnd


def hwnd_is_foreground(hwnd) -> bool:
    """True if hwnd currently owns the OS foreground/keyboard focus.

    Deliberately a raw Win32 check rather than Tk's own focus_get(): Tk's
    focus tracking is Tcl-internal bookkeeping that gets updated the moment
    something calls focus()/focus_force(), regardless of whether the OS
    actually granted that window the focus. For an overrideredirect popup
    whose focus is grabbed programmatically (see force_foreground_window)
    instead of by a normal user click, that bookkeeping can drift from
    reality and never correct itself, silently leaving click-through stuck.
    Comparing against GetForegroundWindow() has no such gap - it always
    reflects what Windows itself considers focused."""
    if not hwnd:
        return False
    try:
        return _user32.GetForegroundWindow() == hwnd
    except Exception:
        return False


def force_foreground_window(hwnd) -> bool:
    """Robustly make hwnd the OS foreground window, and report whether it
    actually worked.

    A plain SetForegroundWindow() call is routinely ignored by Windows'
    foreground-lock heuristic unless it originates from the thread that
    currently owns the input focus - which is exactly what happens when this
    is called from a global hotkey callback: the hotkey fires on a
    background hook thread and is marshalled onto the Tk loop via `after`,
    several steps removed from the original keypress. Temporarily attaching
    our input queue to the current foreground thread's is the standard
    workaround."""
    fg_hwnd = _user32.GetForegroundWindow()
    fg_thread = _user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
    cur_thread = _kernel32.GetCurrentThreadId()

    attached = False
    try:
        if fg_thread and fg_thread != cur_thread:
            attached = bool(_user32.AttachThreadInput(fg_thread, cur_thread, True))
        _user32.BringWindowToTop(hwnd)
        _user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    finally:
        if attached:
            _user32.AttachThreadInput(fg_thread, cur_thread, False)

    return hwnd_is_foreground(hwnd)


def set_click_through(hwnd, enabled: bool):
    """Toggle WS_EX_TRANSPARENT on a window so mouse input (including which
    cursor the OS displays) passes through to whatever is beneath it instead
    of being intercepted by this window."""
    style = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if enabled:
        new_style = style | WS_EX_TRANSPARENT | WS_EX_LAYERED
    else:
        new_style = style & ~WS_EX_TRANSPARENT
    if new_style != style:
        _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_style)
        # Nudge Windows to re-evaluate hit-testing for the new extended
        # style immediately, instead of waiting on some unrelated message.
        # NOACTIVATE is essential here: without it this call itself steals
        # focus back, immediately undoing the very unfocus transition that
        # triggered it.
        _user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, _SWP_FLAGS)


def enable_composited(hwnd):
    """Set WS_EX_COMPOSITED to reduce resize/redraw flicker."""
    style = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_COMPOSITED)


def redraw_window(hwnd):
    _user32.RedrawWindow(hwnd, None, None, _RDW_FLAGS)


def check_single_instance(mutex_name="CraftMapOverlay_SingleInstance") -> bool:
    """True if this is the only running instance. Holds a named mutex for
    the lifetime of the process as the actual enforcement mechanism; the
    return value just reports whether we won the race."""
    _kernel32.CreateMutexW(None, True, mutex_name)
    return _kernel32.GetLastError() != ERROR_ALREADY_EXISTS
