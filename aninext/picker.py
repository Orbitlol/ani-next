"""Interactive pickers: fzf when it is installed, a numbered prompt otherwise.

`aninext <name>` is built from these - one picker for search results, one for
seasons (only when the provider exposes them) and one for episodes.

fzf is used when it is available (`pkg install fzf`), because it gives fuzzy
filtering, arrow keys and a preview pane for the synopsis.  When fzf is missing
nothing breaks: at a keyboard you get a numbered prompt, and when there is no
keyboard at all (a pipe, a script, cron) the list is printed and the caller
falls back to its non-interactive behaviour instead of hanging on a prompt.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from . import log
from .paths import which

__all__ = ["Pick", "interactive", "fzf_available", "fzf_path", "pick_one", "pick_many"]

FZF_HINT = "type to filter · enter to select · esc to cancel"


@dataclass
class Pick:
    """One selectable row."""

    key: str                     # what the caller gets back (id / url / number)
    label: str                   # one-line label shown in the list
    preview: str = ""            # multi-line text for the fzf preview pane
    search: str = ""             # extra text fzf may match on but does not show
    data: object = None          # anything the caller wants back (e.g. Episode)

    def line(self, index: int) -> str:
        """Tab separated: fzf shows the label but matches on both columns."""
        hidden = f" {self.search}" if self.search else ""
        return f"{index}\t{self.label}{hidden}"


# --------------------------------------------------------------------------
# environment
# --------------------------------------------------------------------------

def fzf_path() -> str | None:
    return which("fzf")


def fzf_available() -> bool:
    if os.environ.get("ANINEXT_NO_FZF"):
        return False
    return fzf_path() is not None


def interactive() -> bool:
    """True when a human is at the keyboard (so prompting is safe)."""
    if os.environ.get("ANINEXT_NO_PROMPT"):
        return False
    try:
        return bool(sys.stdin) and bool(sys.stdout) and sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):  # closed/odd streams
        return False


def _tty_path() -> str | None:
    """A usable controlling terminal for fzf (it needs one for the UI)."""
    try:
        fd = os.open("/dev/tty", os.O_RDWR)
        os.close(fd)
        return "/dev/tty"
    except OSError:
        return None


def _ask(prompt: str) -> str:  # indirection so tests can drive the prompt
    return input(prompt)


# --------------------------------------------------------------------------
# picking
# --------------------------------------------------------------------------

def pick_one(entries: Sequence[Pick], *, prompt: str = "Select", header: str = "",
             preview: bool = True) -> Pick | None:
    chosen = _pick(list(entries), prompt=prompt, header=header, preview=preview, multi=False)
    return chosen[0] if chosen else None


def pick_many(entries: Sequence[Pick], *, prompt: str = "Select", header: str = "",
              preview: bool = False) -> list[Pick]:
    return _pick(list(entries), prompt=prompt, header=header, preview=preview, multi=True) or []


def _pick(entries: list[Pick], *, prompt: str, header: str, preview: bool, multi: bool) -> list[Pick] | None:
    if not entries:
        return None
    if fzf_available() and _tty_path():
        result = _fzf(entries, prompt=prompt, header=header, preview=preview, multi=multi)
        if result is not None:
            return result
        # fzf failed for an unexpected reason - fall through to the prompt
    if not interactive():
        return None
    return _numbered(entries, prompt=prompt, multi=multi)


def _fzf(entries: list[Pick], *, prompt: str, header: str, preview: bool, multi: bool) -> list[Pick] | None:
    exe = fzf_path()
    if not exe:
        return None
    lines = [e.line(i) for i, e in enumerate(entries)]
    # fzf renders its interface on /dev/tty (its documented default), so piping
    # the list into stdin is safe and the picker still appears on screen.
    cmd = [
        exe,
        "--height=45%",
        "--reverse",
        "--delimiter=\t",
        "--with-nth=2..",
        "--prompt", f"{prompt} > ",
        "--header", header or FZF_HINT,
    ]
    if multi:
        cmd += ["--multi"]
    tmpdir: tempfile.TemporaryDirectory | None = None
    if preview and any(e.preview for e in entries):
        tmpdir = tempfile.TemporaryDirectory(prefix="aninext-preview-")
        for index, entry in enumerate(entries):
            Path(tmpdir.name, str(index)).write_text(entry.preview or entry.label, "utf-8")
        cmd += [
            "--preview", f"cat '{tmpdir.name}'/{{1}}",
            "--preview-window", "down:45%:wrap",
        ]

    options = os.environ.get("ANINEXT_FZF_OPTIONS", "").split()
    cmd += options
    try:
        proc = subprocess.run(
            cmd,
            input="\n".join(lines) + "\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except OSError as exc:
        log.debug("fzf failed: %s", exc)
        return None
    finally:
        if tmpdir is not None:
            tmpdir.cleanup()

    if proc.returncode != 0:      # 1 = nothing matched, 130 = cancelled
        return None
    picked: list[Pick] = []
    for line in proc.stdout.splitlines():
        try:
            index = int(line.split("\t", 1)[0])
        except (ValueError, IndexError):
            continue
        if 0 <= index < len(entries) and entries[index] not in picked:
            picked.append(entries[index])
    return picked or None


def _parse_selection(answer: str, count: int, *, multi: bool) -> list[int] | None:
    """'2', '1,3', '2-4', 'all' -> 0-based indexes; None when it is not a number."""
    text = answer.strip().lower()
    if not text:
        return []
    if text in ("all", "*"):
        return list(range(count))
    if not re.fullmatch(r"[\d\s,\-]+", text):
        return None
    out: list[int] = []
    for chunk in text.replace(" ", ",").split(","):
        if not chunk:
            continue
        if "-" in chunk:
            start, _, end = chunk.partition("-")
            try:
                first, last = int(start), int(end)
            except ValueError:
                return None
            step = 1 if last >= first else -1
            for number in range(first, last + step, step):
                if 1 <= number <= count and number - 1 not in out:
                    out.append(number - 1)
            continue
        try:
            number = int(chunk)
        except ValueError:
            return None
        if 1 <= number <= count and number - 1 not in out:
            out.append(number - 1)
    if not multi:
        return out[:1]
    return out


def _numbered(entries: list[Pick], *, prompt: str, multi: bool) -> list[Pick] | None:
    width = len(str(len(entries)))
    log.plain("")
    for index, entry in enumerate(entries, start=1):
        log.plain(f"  {str(index).rjust(width)}.  {entry.label}")

    hint = "numbers, comma separated, e.g. 1,3-5" if multi else "a number"
    attempts = 0
    while attempts < 3:
        attempts += 1
        try:
            answer = _ask(f"\n{prompt} [{hint}, empty to cancel]: ").strip()
        except (EOFError, KeyboardInterrupt):
            log.plain("")
            return None
        if not answer or answer.lower() in ("q", "quit", "x", "n", "no"):
            return None
        indexes = _parse_selection(answer, len(entries), multi=multi)
        if indexes is None:
            # not a number: treat it as a filter over the labels
            words = [w for w in re.split(r"\s+", answer.lower()) if w]
            matches = [
                i for i, e in enumerate(entries)
                if all(w in f"{e.label} {e.search}".lower() for w in words)
            ]
            if len(matches) == 1:
                indexes = matches
            elif matches:
                _pick_from_matches(entries, matches)
                continue
            else:
                log.warn("nothing matched %r", answer)
                continue
        if not indexes:
            log.warn("pick a number between 1 and %d", len(entries))
            continue
        return [entries[i] for i in indexes]
    log.warn("giving up")
    return None


def _pick_from_matches(entries: list[Pick], matches: list[int]) -> None:
    log.plain("")
    for i in matches:
        log.plain(f"  {str(i + 1).rjust(len(str(len(entries))))}.  {entries[i].label}")
    log.plain("")


def describe_entries(entries: Iterable[Pick], *, numbered: bool = True) -> list[str]:
    """Render entries as plain lines (used for the non-interactive fallback)."""
    out = []
    for index, entry in enumerate(entries, start=1):
        out.append(f"{index}. {entry.label}" if numbered else entry.label)
    return out
