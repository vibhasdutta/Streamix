"""Startup dependency checker for Streamix.

Detects the OS and verifies that all required external tools (uv, mpv, ngrok
authtoken) are present.  When something is missing the checker prints a
helpful Rich table with OS-specific install instructions but never blocks
startup – the user can always continue.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from rich.align import Align
from rich.console import Console
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from shared.utils.os_detector import OS, current_os, IS_WINDOWS, IS_MACOS, IS_LINUX


# ─────────────────────────────────────────────────────────────
# Install instructions per OS  (kept current as of April 2026)
# ─────────────────────────────────────────────────────────────

_INSTALL_COMMANDS: dict[str, dict[OS, list[str]]] = {
    "uv": {
        OS.WINDOWS: [
            "powershell -c \"irm https://astral.sh/uv/install.ps1 | iex\"",
            "scoop install uv",
            "winget install astral-sh.uv",
        ],
        OS.MACOS: [
            "brew install uv",
            "curl -LsSf https://astral.sh/uv/install.sh | sh",
        ],
        OS.LINUX: [
            "curl -LsSf https://astral.sh/uv/install.sh | sh",
            "sudo snap install astral-uv --classic",
        ],
    },
    "mpv": {
        OS.WINDOWS: [
            "scoop install mpv",
            "choco install mpv",
            "Download from https://mpv.io/installation/",
        ],
        OS.MACOS: [
            "brew install mpv",
        ],
        OS.LINUX: [
            "sudo apt install mpv",
            "sudo pacman -S mpv",
            "sudo snap install mpv",
        ],
    },
    "ngrok_auth": {
        OS.WINDOWS: [
            "ngrok config add-authtoken <YOUR_TOKEN>",
            "Get token → https://dashboard.ngrok.com/get-started/your-authtoken",
        ],
        OS.MACOS: [
            "ngrok config add-authtoken <YOUR_TOKEN>",
            "Get token → https://dashboard.ngrok.com/get-started/your-authtoken",
        ],
        OS.LINUX: [
            "ngrok config add-authtoken <YOUR_TOKEN>",
            "Get token → https://dashboard.ngrok.com/get-started/your-authtoken",
        ],
    },
}


# ─────────────────────────────────────────────────────────────
# Tool detection helpers
# ─────────────────────────────────────────────────────────────

def _find_uv() -> Optional[str]:
    """Return the path to `uv` or None."""
    return shutil.which("uv")


def _find_mpv() -> Optional[str]:
    """Return the path to `mpv` or None.

    Checks PATH first, then platform-specific common locations.
    """
    cmd = shutil.which("mpv")
    if cmd:
        return cmd

    possible: list[str] = []

    if IS_WINDOWS:
        home = os.path.expanduser("~")
        possible.extend([
            # Scoop (user)
            os.path.join(home, "scoop", "apps", "mpv", "current", "mpv.exe"),
            # Scoop (global)
            os.path.join("C:\\", "ProgramData", "scoop", "apps", "mpv", "current", "mpv.exe"),
            # Common manual installs
            "C:\\Program Files\\mpv\\mpv.exe",
            "C:\\Program Files\\MPV Player\\mpv.exe",
            "C:\\mpv\\mpv.exe",
            # Relative to project
            "./mpv.exe",
            "bin/mpv.exe",
            # WindowsApps (rare)
            os.path.join(home, "AppData", "Local", "Microsoft", "WindowsApps", "mpv.exe"),
        ])
    elif IS_MACOS:
        possible.extend([
            "/opt/homebrew/bin/mpv",      # Homebrew ARM (Apple Silicon)
            "/usr/local/bin/mpv",         # Homebrew Intel
            "/opt/local/bin/mpv",         # MacPorts
        ])
    else:  # Linux
        possible.extend([
            "/usr/bin/mpv",
            "/usr/local/bin/mpv",
            "/snap/bin/mpv",
        ])

    for path in possible:
        if os.path.isfile(path):
            return path
    return None


def _check_ngrok_authtoken() -> bool:
    """Return True if pyngrok / ngrok has a configured authtoken."""
    # Method 1: Check pyngrok's own config
    try:
        from pyngrok import conf as ngrok_conf
        default = ngrok_conf.get_default()
        if default.auth_token:
            return True
    except Exception:
        pass

    # Method 2: Read the ngrok YAML config file directly
    try:
        import yaml  # pyngrok bundles PyYAML
    except ImportError:
        yaml = None

    config_paths = []
    if IS_WINDOWS:
        appdata = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        config_paths.append(os.path.join(appdata, "ngrok", "ngrok.yml"))
        # Legacy
        config_paths.append(os.path.join(os.path.expanduser("~"), ".ngrok2", "ngrok.yml"))
    elif IS_MACOS:
        config_paths.append(os.path.expanduser("~/Library/Application Support/ngrok/ngrok.yml"))
        config_paths.append(os.path.expanduser("~/.ngrok2/ngrok.yml"))
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        config_paths.append(os.path.join(xdg, "ngrok", "ngrok.yml"))
        config_paths.append(os.path.expanduser("~/.ngrok2/ngrok.yml"))

    for cfg_path in config_paths:
        if not os.path.isfile(cfg_path):
            continue
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                content = f.read()
            # Quick string check — avoid yaml dependency requirement
            if "authtoken:" in content:
                # Ensure it's not just the key with an empty value
                for line in content.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("authtoken:"):
                        val = stripped.split(":", 1)[1].strip()
                        if val and val != '""' and val != "''":
                            return True
        except Exception:
            continue

    return False


# ─────────────────────────────────────────────────────────────
# Main checker
# ─────────────────────────────────────────────────────────────

@dataclass
class DepResult:
    """Result for a single dependency check."""
    name: str
    found: bool
    path: Optional[str] = None
    hint: str = ""
    install_cmds: list[str] = field(default_factory=list)
    category: str = "tool"  # "tool" | "config"


# ─────────────────────────────────────────────────────────────
# Dependency definitions (name, check function, hint, key)
# ─────────────────────────────────────────────────────────────

_DEP_CHECKS = [
    {
        "name": "uv",
        "label": "Package Manager (uv)",
        "check": _find_uv,
        "hint": "Python dependency manager -- all libraries managed by uv.",
        "key": "uv",
    },
    {
        "name": "mpv",
        "label": "Video Player (mpv)",
        "check": _find_mpv,
        "hint": "Video player -- required for all anime playback.",
        "key": "mpv",
    },
    {
        "name": "ngrok authtoken",
        "label": "ngrok/ngrok authtoken",
        "check": _check_ngrok_authtoken,
        "hint": "Required for hosting public Watch Parties.",
        "key": "ngrok_auth",
    },
]


def _build_status_table(
    checked: list[tuple[dict, DepResult | None]],
    current_checking: str | None = None,
) -> Table:
    """Build a Rich table showing current check progress."""
    table = Table(
        show_header=True,
        header_style="bold magenta",
        box=None,
        padding=(0, 2),
        expand=False,
    )
    table.add_column("", width=5, justify="center")
    table.add_column("Component", style="bold", min_width=25)
    table.add_column("Status", min_width=10)
    table.add_column("Details", style="dim")

    for dep_def, result in checked:
        if result is None:
            # Currently checking this one
            table.add_row(
                "[yellow]...[/yellow]",
                dep_def["label"],
                "[yellow]Checking[/yellow]",
                "",
            )
        elif result.found:
            table.add_row(
                "[bold green]OK[/bold green]",
                dep_def["label"],
                "[green]Found[/green]",
                result.path or "Configured",
            )
        else:
            table.add_row(
                "[bold red]FAIL[/bold red]",
                dep_def["label"],
                "[red]Missing[/red]",
                result.hint,
            )

    return table


def run_startup_check(console: Console | None = None) -> tuple[bool, list[DepResult]]:
    """Run the dependency check with a live animated UI.

    Shows each dependency being checked one-by-one with progressive updates.
    Returns (all_ok, results).
    """
    import time
    from rich.live import Live
    from rich.panel import Panel
    from rich.columns import Columns
    from rich.spinner import Spinner

    if console is None:
        console = Console()

    os_labels = {
        OS.WINDOWS: "Windows",
        OS.MACOS: "macOS",
        OS.LINUX: "Linux",
    }
    os_label = os_labels.get(current_os, str(current_os))

    console.print()
    console.print(Rule("[bold cyan]STREAMIX System Check[/bold cyan]", style="dim cyan"))
    console.print(Align.center(f"[dim]Platform:[/dim] [bold cyan]{os_label}[/bold cyan]"))
    console.print()

    results: list[DepResult] = []
    checked: list[tuple[dict, DepResult | None]] = []

    with Live(console=console, refresh_per_second=12, transient=True) as live:
        for dep_def in _DEP_CHECKS:
            # Show spinner for current check
            checked.append((dep_def, None))

            # Build display: table + spinner
            table = _build_status_table(checked)
            spinner = Spinner("dots", text=f"[dim]Checking {dep_def['label']}...[/dim]", style="cyan")
            layout = Columns([table, spinner], padding=2, expand=False)
            live.update(Align.center(layout))
            time.sleep(0.4)  # Brief visual pause so user sees the check happening

            # Actually run the check
            check_result = dep_def["check"]()
            is_found = check_result is not None and check_result is not False

            dep_result = DepResult(
                name=dep_def["name"],
                found=is_found,
                path=check_result if isinstance(check_result, str) else None,
                hint=dep_def["hint"],
                install_cmds=_INSTALL_COMMANDS.get(dep_def["key"], {}).get(current_os, []),
                category="config" if dep_def["key"] == "ngrok_auth" else "tool",
            )
            results.append(dep_result)

            # Replace the "checking" entry with the resolved result
            checked[-1] = (dep_def, dep_result)

            # Update display with result
            table = _build_status_table(checked)
            live.update(Align.center(table))
            time.sleep(0.25)

    # ── Final static output ──
    all_ok = all(r.found for r in results)
    missing = [r for r in results if not r.found]

    # Print the final resolved table (non-transient)
    final_table = _build_status_table([(d, r) for d, r in checked])
    console.print(Align.center(final_table))
    console.print()

    if all_ok:
        console.print(Align.center("[bold green]All systems ready![/bold green]"))
        console.print()
    else:
        console.print(Rule("[bold yellow]Missing Dependencies[/bold yellow]", style="yellow"))
        console.print()
        for dep in missing:
            console.print(f"  [bold red]X {dep.name}[/bold red]  --  {dep.hint}")
            if dep.install_cmds:
                console.print(f"    [dim]Install with any of:[/dim]")
                for cmd in dep.install_cmds:
                    console.print(f"      [cyan]->[/cyan] [bold]{cmd}[/bold]")
            console.print()
        console.print(Rule(style="dim"))
        console.print()

    return all_ok, results

