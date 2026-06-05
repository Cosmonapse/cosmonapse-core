"""
cosmo._install
==============

Helpers that make `pip install -e <cosmonapse>` feel like a "real" install
on a fresh machine by also putting the Python Scripts/bin directory on the
user's persistent PATH so the `cosmo` command is callable from any shell.

This lives in the ``cosmo`` CLI package  -  not in the ``cosmonapse`` SDK  - 
because manipulating the user's shell configuration is CLI/installer
behaviour, not something an imported library should ever do.

Two entry points are exposed by pyproject.toml:

* ``cosmonapse-init-path``  -  a console script created by pip at install time.
* ``python -m cosmo._install``  -  works even before PATH has been updated.

The same module is also invoked automatically by the top-level installer
script (``cosmonapse-core/install.py``) right after it runs
``pip install -e``.

Public surface:

    update_path()          -> bool        # add scripts dir to persistent PATH
    scripts_dir()          -> pathlib.Path
    main(argv=None)        -> int         # CLI entrypoint
"""
from __future__ import annotations

import argparse
import os
import sys
import sysconfig
from pathlib import Path
from typing import Iterable

__all__ = ["update_path", "scripts_dir", "main"]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def scripts_dir() -> Path:
    """Return the directory pip uses for console scripts in the active env.

    This is the directory that contains ``cosmo`` (or ``cosmo.exe`` on
    Windows) after ``pip install -e .`` has run.
    """
    # sysconfig knows the canonical location for the running interpreter,
    # including the venv it's executing from.
    paths = sysconfig.get_paths()
    # 'scripts' is what gets used for console_scripts; fall back to
    # the legacy distutils name on truly ancient pythons.
    scripts = paths.get("scripts") or paths.get("Scripts")
    if not scripts:
        # Very unlikely fallback.
        scripts = str(Path(sys.executable).parent)
    return Path(scripts).resolve()


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------
def _update_path_windows(target: Path) -> bool:
    """Append *target* to HKCU\\Environment\\Path and broadcast the change."""
    import winreg  # type: ignore[import-not-found]
    import ctypes
    from ctypes import wintypes

    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE
    )
    try:
        try:
            current, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current = ""

        target_str = str(target)
        entries = [e for e in current.split(os.pathsep) if e]
        if any(os.path.normcase(e.rstrip("\\")) == os.path.normcase(target_str.rstrip("\\")) for e in entries):
            print(f"[cosmonapse] PATH already contains {target_str}")
            return False

        new_value = os.pathsep.join(entries + [target_str])
        # REG_EXPAND_SZ preserves things like %USERPROFILE% in the user's PATH.
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new_value)
    finally:
        winreg.CloseKey(key)

    # Broadcast WM_SETTINGCHANGE so newly-spawned processes pick up the change.
    HWND_BROADCAST = 0xFFFF
    WM_SETTINGCHANGE = 0x001A
    SMTO_ABORTIFHUNG = 0x0002
    result = wintypes.LPARAM()
    ctypes.windll.user32.SendMessageTimeoutW(
        HWND_BROADCAST,
        WM_SETTINGCHANGE,
        0,
        "Environment",
        SMTO_ABORTIFHUNG,
        5000,
        ctypes.byref(result),
    )

    print(f"[cosmonapse] Added to user PATH: {target_str}")
    print("[cosmonapse] Open a *new* terminal (or sign out / in) for the change to take effect.")
    return True


# ---------------------------------------------------------------------------
# macOS / Linux
# ---------------------------------------------------------------------------
def _shell_rc_candidates() -> Iterable[Path]:
    home = Path.home()
    # Order matters: zsh first on macOS, bash on linux, then fallbacks.
    seen: set[Path] = set()
    for name in (".zshrc", ".bashrc", ".bash_profile", ".profile"):
        p = home / name
        if p not in seen:
            seen.add(p)
            yield p


def _update_path_posix(target: Path) -> bool:
    target_str = str(target)
    marker = "# >>> cosmonapse PATH >>>"
    end_marker = "# <<< cosmonapse PATH <<<"
    block = (
        f"\n{marker}\n"
        f'export PATH="{target_str}:$PATH"\n'
        f"{end_marker}\n"
    )

    updated_any = False
    for rc in _shell_rc_candidates():
        if not rc.exists():
            # Only create .zshrc / .bashrc if the shell that owns them is in use;
            # otherwise we'd be littering the home directory.
            if rc.name not in {".zshrc", ".bashrc"}:
                continue
            shell = os.environ.get("SHELL", "")
            if rc.name == ".zshrc" and not shell.endswith("zsh"):
                continue
            if rc.name == ".bashrc" and not shell.endswith("bash"):
                continue
            rc.touch()

        content = rc.read_text(encoding="utf-8", errors="replace") if rc.exists() else ""
        if marker in content:
            # Already managed  -  refresh the block in case the target changed.
            before, _, rest = content.partition(marker)
            _, _, after = rest.partition(end_marker)
            new_content = before.rstrip() + block + after.lstrip()
            if new_content != content:
                rc.write_text(new_content, encoding="utf-8")
                print(f"[cosmonapse] Refreshed cosmonapse PATH block in {rc}")
                updated_any = True
            else:
                print(f"[cosmonapse] {rc} already up to date")
            continue

        # Skip if the user already has this dir on PATH some other way.
        if f'PATH="{target_str}:' in content or f"PATH={target_str}:" in content:
            print(f"[cosmonapse] {rc} already references {target_str}")
            continue

        rc.write_text(content.rstrip() + block, encoding="utf-8")
        print(f"[cosmonapse] Added cosmonapse PATH block to {rc}")
        updated_any = True

    if updated_any:
        print(
            "[cosmonapse] Open a new terminal (or `source` your shell rc) "
            "for the change to take effect."
        )
    else:
        print("[cosmonapse] No shell rc file needed updating.")
    return updated_any


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def update_path(target: Path | None = None) -> bool:
    """Add the active env's scripts directory to the user's persistent PATH.

    Returns True if any change was made.
    """
    target = (target or scripts_dir()).resolve()
    if not target.exists():
        print(f"[cosmonapse] Scripts directory does not exist yet: {target}", file=sys.stderr)
        return False

    if os.name == "nt":
        return _update_path_windows(target)
    return _update_path_posix(target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cosmonapse-init-path",
        description=(
            "Add the active Python environment's scripts directory to your "
            "persistent PATH so the `cosmo` command is callable from any shell."
        ),
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="Show the scripts directory without modifying PATH.",
    )
    args = parser.parse_args(argv)

    target = scripts_dir()
    print(f"[cosmonapse] Active scripts dir: {target}")
    if args.print_only:
        return 0

    changed = update_path(target)
    return 0 if changed or target in [Path(p) for p in os.environ.get("PATH", "").split(os.pathsep)] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
