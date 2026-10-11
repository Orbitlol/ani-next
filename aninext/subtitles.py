"""Subtitle handling: parse, convert and shift SRT/VTT/ASS with no dependencies.

Useful on Termux where external subtitle tools are rarely installed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import log


@dataclass
class Cue:
    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def parse_timestamp(text: str) -> float:
    """Seconds from '00:01:02,500', '1:02.5', '02:03', '62.5' and friends.

    Everything after a '.'/',' is read as a *fraction* of a second, so both
    '1:02.5' and '1:02,500' mean 62.5 seconds.
    """
    raw = str(text or "").strip().replace(",", ".")
    if not raw:
        return 0.0
    if ":" not in raw:
        try:
            return float(raw)
        except ValueError:
            m = re.search(r"\d+(?:\.\d+)?", raw)
            return float(m.group(0)) if m else 0.0

    parts = raw.split(":")
    seconds = 0.0
    for index, part in enumerate(parts):
        whole_raw, _, frac_raw = (part or "").partition(".")
        whole_match = re.search(r"\d+", whole_raw)
        value = float(whole_match.group(0)) if whole_match else 0.0
        if index == len(parts) - 1:  # the tail may carry a fraction of a second
            frac_digits = re.sub(r"\D", "", frac_raw)
            if frac_digits:
                value += float(f"0.{frac_digits}")
        seconds = seconds * 60 + value
    return seconds


def format_timestamp(seconds: float, *, comma: bool = True, millis: bool = True) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    whole = int(secs)
    ms = int(round((secs - whole) * 1000))
    if ms == 1000:
        whole += 1
        ms = 0
    sep = "," if comma else "."
    if millis:
        return f"{hours:02d}:{minutes:02d}:{whole:02d}{sep}{ms:03d}"
    return f"{hours:02d}:{minutes:02d}:{whole:02d}"


def detect_format(text: str) -> str:
    head = text.lstrip()[:600]
    if head.startswith("WEBVTT"):
        return "vtt"
    if "[Script Info]" in head or re.search(r"^Dialogue:", head, re.M):
        return "ass"
    if "-->" in head:
        return "srt"
    return "srt"


def parse(text: str, fmt: str | None = None) -> list[Cue]:
    fmt = fmt or detect_format(text)
    if fmt == "vtt":
        return _parse_vtt(text)
    if fmt == "ass":
        return _parse_ass(text)
    return _parse_srt(text)


def _clean_block(lines: list[str]) -> str:
    text = "\n".join(lines).strip()
    text = re.sub(r"<[^>]+>", "", text)                    # vtt/srt inline tags
    text = re.sub(r"\{\\[^}]*\}", "", text)                # ass overrides
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    return text.strip()


def _parse_srt(text: str) -> list[Cue]:
    cues: list[Cue] = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        if re.match(r"^\d+$", lines[0].strip()):
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            continue
        start_raw, _, end_raw = lines[0].partition("-->")
        try:
            cues.append(Cue(parse_timestamp(start_raw), parse_timestamp(end_raw.split()[0]),
                            _clean_block(lines[1:])))
        except (ValueError, IndexError):
            continue
    return cues


def _parse_vtt(text: str) -> list[Cue]:
    text = re.sub(r"^WEBVTT.*?(\n\n|$)", "", text, flags=re.S | re.M)
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        if "-->" not in lines[0] and len(lines) > 1 and "-->" in lines[1]:
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            continue
        start_raw, _, rest = lines[0].partition("-->")
        end_token = rest.strip().split()[0] if rest.strip() else ""
        try:
            cues.append(Cue(parse_timestamp(start_raw), parse_timestamp(end_token), _clean_block(lines[1:])))
        except (ValueError, IndexError):
            continue
    return cues


def _parse_ass(text: str) -> list[Cue]:
    cues: list[Cue] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if not line.startswith("Dialogue:"):
            continue
        parts = line[len("Dialogue:"):].split(",", 9)
        if len(parts) < 10:
            continue
        try:
            start = parse_timestamp(parts[1])
            end = parse_timestamp(parts[2])
        except ValueError:
            continue
        body = parts[9].replace("\\N", "\n").replace("\\n", "\n")
        cues.append(Cue(start, end, _clean_block([body])))
    return cues


# --------------------------------------------------------------------------
# writers
# --------------------------------------------------------------------------

def to_srt(text_or_cues: str | list[Cue], fmt: str | None = None, *, shift: float = 0.0) -> str:
    cues = parse(text_or_cues, fmt) if isinstance(text_or_cues, str) else list(text_or_cues)
    return cues_to_srt(cues, shift=shift)


def cues_to_srt(cues: list[Cue], *, shift: float = 0.0) -> str:
    out: list[str] = []
    for index, cue in enumerate(cues, start=1):
        start = max(0.0, cue.start + shift)
        end = max(start + 0.05, cue.end + shift)
        out.append(f"{index}\n{format_timestamp(start)} --> {format_timestamp(end)}\n{cue.text}\n")
    return "\n".join(out)


def to_vtt(text_or_cues: str | list[Cue], fmt: str | None = None, *, shift: float = 0.0) -> str:
    cues = parse(text_or_cues, fmt) if isinstance(text_or_cues, str) else list(text_or_cues)
    lines = ["WEBVTT", ""]
    for index, cue in enumerate(cues, start=1):
        start = max(0.0, cue.start + shift)
        end = max(start + 0.05, cue.end + shift)
        lines.append(str(index))
        lines.append(
            f"{format_timestamp(start, comma=False)} --> {format_timestamp(end, comma=False)}"
        )
        lines.append(cue.text)
        lines.append("")
    return "\n".join(lines)


def convert_file(path: Path, *, to: str = "srt", shift: float = 0.0, encoding: str = "utf-8") -> Path:
    raw = path.read_text(encoding, errors="replace")
    fmt = detect_format(raw)
    cues = parse(raw, fmt)
    if shift:
        cues = [Cue(max(0.0, c.start + shift), max(0.0, c.end + shift), c.text) for c in cues]
    if to == "srt":
        content, suffix = cues_to_srt(cues), ".srt"
    elif to == "vtt":
        content, suffix = to_vtt(cues), ".vtt"
    else:
        raise ValueError(f"unsupported target format: {to}")
    out = path.with_suffix(suffix)
    out.write_text(content, "utf-8")
    log.debug("converted %s -> %s (%d cues)", path.name, out.name, len(cues))
    return out


def stats(text: str, fmt: str | None = None) -> dict:
    cues = parse(text, fmt)
    words = sum(len(c.text.split()) for c in cues)
    duration = max((c.end for c in cues), default=0.0)
    return {
        "cues": len(cues),
        "words": words,
        "duration": duration,
        "reading_speed": round(words / (duration / 60), 1) if duration else 0.0,
    }
