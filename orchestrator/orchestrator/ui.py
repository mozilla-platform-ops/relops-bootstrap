"""
Presentation layer for the reprovision CLI — the "nuts and bolts, but beautiful" look.

Everything routes through one rich Console, which auto-degrades to plain text when stdout
isn't a TTY (CI / pipes), so this changes appearance only, never behavior. The palette is the
classic 1977–1998 Apple six-stripe rainbow (we are, after all, reprovisioning Macs).

Vocabulary the steps use:
  banner()  — the striped Apple + title, once per full run
  step()    — a rainbow section header (cycles the six colors)
  wire()    — the *actual* mechanism under a step (real ssh command / API call), dimmed
  ok/info/warn — result lines
  waiting() — an animated spinner (+ optional ETA bar) for a blocking poll
  summary() — the closing rainbow line with total elapsed
"""

from __future__ import annotations

import itertools
import time
from contextlib import contextmanager

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

console = Console(highlight=False)

# Classic Apple six-color logo palette, top stripe → bottom stripe.
GREEN, YELLOW, ORANGE, RED, PURPLE, BLUE = (
    "#61BB46", "#FDB827", "#F5821F", "#E03A3E", "#963D97", "#009DDC",
)
PALETTE = [GREEN, YELLOW, ORANGE, RED, PURPLE, BLUE]

# The apple, banded into the six stripes.
_APPLE = [
    ("                    'c.", GREEN),
    ("                 ,xNMM.", GREEN),
    ("               .OMMMMo", GREEN),
    ('               lMM"', GREEN),
    ("     .;loddo:.  .olloddol;.", GREEN),
    ("   cKMMMMMMMMMMNWMMMMMMMMMM0:", YELLOW),
    (" .KMMMMMMMMMMMMMMMMMMMMMMMWd.", YELLOW),
    (" XMMMMMMMMMMMMMMMMMMMMMMMX.", ORANGE),
    (";MMMMMMMMMMMMMMMMMMMMMMMM:", ORANGE),
    (":MMMMMMMMMMMMMMMMMMMMMMMM:", RED),
    (".MMMMMMMMMMMMMMMMMMMMMMMMX.", RED),
    (" kMMMMMMMMMMMMMMMMMMMMMMMMWd.", PURPLE),
    (" .XMMMMMMMMMMMMMMMMMMMMMMMMMMk", PURPLE),
    ("  .XMMMMMMMMMMMMMMMMMMMMMMMMK.", PURPLE),
    ("    kMMMMMMMMMMMMMMMMMMMMMMd", BLUE),
    ("     ;KMMMMMMMWXXWMMMMMMMk.", BLUE),
    ("       .cooc,.    .,coo:.", BLUE),
]

_step_color = itertools.cycle(PALETTE)
_current_color = PALETTE[0]
_step_n = 0
_total_steps = 0


def flow(phases: list[str]) -> None:
    """Print the whole pipeline up front — so an audience sees every phase *before* it runs,
    not one opaque 'reprovisioning…' — and arm [n/N] numbering on step(). Cosmetic only.

    Also resets the step counter + color cycle, so a long-lived runner that drives many
    reprovisions has each one start fresh at phase 1 / the green stripe.
    """
    global _step_n, _total_steps, _step_color
    _step_n = 0
    _total_steps = len(phases)
    _step_color = itertools.cycle(PALETTE)
    rendered = " [dim]→[/] ".join(
        f"[{PALETTE[i % len(PALETTE)]}]{p}[/]" for i, p in enumerate(phases)
    )
    console.print(f"  [dim]orchestration ·[/] {rendered}")
    console.print(
        f"  [dim]{_total_steps} phases · Taskcluster · SimpleMDM (EACS) · SSH/PAM · mTLS vault · Puppet[/]"
    )
    console.print()


def banner(hostname: str, role: str, pool: str) -> None:
    console.print()
    for text, color in _APPLE:
        console.print(f"  {text}", style=color)
    console.print()
    console.print("  [bold]reprovision[/]  [dim]· zero-touch EACS → prod for Mozilla CI Macs[/]")
    console.print(f"  [dim]host[/] [bold]{hostname}[/]   [dim]role[/] {role}   [dim]pool[/] {pool}")
    console.print()


def step(name: str, desc: str = "") -> None:
    """A rainbow section header; each call advances to the next Apple stripe color.

    When flow() armed a total, the header is numbered `▸ [n/N] NAME` so every line reads
    as one phase of a known-length pipeline — not a standalone action."""
    global _current_color, _step_n
    _current_color = next(_step_color)
    _step_n += 1
    label = f"[{_step_n}/{_total_steps}] {name}" if _total_steps else name
    console.print(f"[bold {_current_color}]▸ {label}[/]")
    if desc:
        console.print(f"  [dim]{desc}[/]")


def wire(cmd: str) -> None:
    """Surface the real mechanism under a step — the actual ssh command or API call."""
    console.print(f"  [{_current_color}]»[/] [dim]{cmd}[/]")


def ok(msg: str) -> None:
    console.print(f"  [green]✓[/] {msg}")


def info(msg: str) -> None:
    console.print(f"  [dim]· {msg}[/]")


def warn(msg: str) -> None:
    console.print(f"  [yellow]▲[/] {msg}")


def err(msg: str) -> None:
    console.print(f"[bold red]✗[/] {msg}")


def _mmss(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


@contextmanager
def waiting(desc: str, *, eta_seconds: int | None = None):
    """Animated spinner (+ ETA bar when eta_seconds is given) for a blocking poll.

    Yields tick(msg=None, frac=None): call it each iteration to refresh the status line. The
    spinner and clock animate on their own via rich's refresh thread. Pass `frac` (0..1) to
    drive the bar/clock explicitly (used by the demo to simulate a 5-minute wait quickly);
    otherwise real elapsed time is used.
    """
    color = _current_color
    columns = [
        SpinnerColumn(spinner_name="dots", style=color),
        TextColumn("[progress.description]{task.description}"),
    ]
    if eta_seconds:
        columns += [
            BarColumn(bar_width=24, complete_style=color, finished_style=color),
            TextColumn("[dim]{task.fields[clock]}[/]"),
        ]
    else:
        columns += [TimeElapsedColumn()]
    start = time.monotonic()
    with Progress(*columns, console=console, transient=True) as progress:
        task = progress.add_task(desc, total=eta_seconds, clock=f"0:00 / ~{_mmss(eta_seconds or 0)}")

        def tick(msg: str | None = None, frac: float | None = None) -> None:
            shown = (frac * eta_seconds) if (frac is not None and eta_seconds) else (time.monotonic() - start)
            kw: dict = {}
            if msg is not None:
                kw["description"] = msg
            if eta_seconds:
                kw["completed"] = min(shown, eta_seconds)
                kw["clock"] = f"{_mmss(shown)} / ~{_mmss(eta_seconds)}"
            progress.update(task, **kw)

        yield tick


def summary(hostname: str, elapsed_seconds: float, *, quarantined: bool) -> None:
    stripes = "".join(f"[{c}]█[/]" for c in PALETTE)
    state = "still quarantined" if quarantined else "returned to service"
    console.print()
    console.print(f"  {stripes}  [bold green]{hostname} reprovisioned[/] [dim]in {_mmss(elapsed_seconds)} · {state}[/]")
    console.print(
        "  [dim]end-to-end: quarantine → drain → EACS wipe → DEP re-enroll → SecureToken →"
        " Bootstrap Token → self-provision (mTLS vault · puppet · Taskcluster)[/]"
    )
    console.print()
