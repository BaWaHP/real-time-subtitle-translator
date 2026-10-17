"""Parse browser/WebVTT and public YouTube subtitle formats without disk I/O."""
from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET

from .types import Cue, clean_text

MAX_CUES = 50_000


def normalize_cues(values) -> list[Cue]:
    result = {}
    for value in values:
        cue = value if isinstance(value, Cue) else Cue.from_dict(value)
        if cue:
            result[cue.key] = cue
        if len(result) >= MAX_CUES:
            break
    return sorted(result.values(), key=lambda cue: (cue.start, cue.end))


def subtitle_segments(cues: list[Cue]) -> list[Cue]:
    """Merge adjacent/rolling captions to reduce translation requests.

    Preserve gaps and leave long cues at their original media times.
    """
    result: list[Cue] = []
    for cue in cues:
        if not result:
            result.append(cue)
            continue
        previous = result[-1]
        span = max(previous.end, cue.end) - previous.start
        if cue.start - previous.end > .25 or span > 4.5 or len(previous.text) + len(cue.text) > 600:
            result.append(cue)
            continue
        left, right = previous.text.split(), cue.text.split()
        common = 0
        if cue.start < previous.end:
            for count in range(1, min(40, len(left), len(right)) + 1):
                if [word.casefold() for word in left[-count:]] == [word.casefold() for word in right[:count]]:
                    common = count
        combined = " ".join(left + right[common:])
        result[-1] = Cue(previous.start, max(previous.end, cue.end), combined)
    return result


def _timestamp(value: str) -> float:
    fields = value.strip().replace(",", ".").split(":")
    return sum(float(value) * 60**index for index, value in enumerate(reversed(fields)))


def _text(value: str) -> str:
    return clean_text(html.unescape(re.sub(r"<[^>]*>", "", value)))


def parse_captions(raw: str, extension: str = "") -> list[Cue]:
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("字幕文件过大，已停止读取。")
    values = []
    if extension == "json3" or raw.lstrip().startswith("{"):
        body = json.loads(raw)
        events = body.get("events", [])
        for index, event in enumerate(events[:MAX_CUES]):
            if not event.get("segs"):
                continue
            start = float(event.get("tStartMs", 0)) / 1000
            duration = float(event.get("dDurationMs", 0)) / 1000
            if duration <= 0 and index + 1 < len(events):
                duration = float(events[index + 1].get("tStartMs", 0)) / 1000 - start
            text = _text("".join(str(part.get("utf8", "")) for part in event["segs"]))
            values.append({"start": start, "end": start + max(.1, duration), "text": text})
    elif "<transcript" in raw or "<timedtext" in raw:
        # ElementTree does not resolve external entities or fetch external DTDs.
        root = ET.fromstring(raw)
        for item in root.iter():
            if item.tag == "text":
                start = float(item.get("start", 0))
                end = start + float(item.get("dur", 0))
            elif item.tag == "p" and item.get("t") is not None:
                start = float(item.get("t", 0)) / 1000
                end = start + float(item.get("d", 0)) / 1000
            else:
                continue
            values.append({"start": start, "end": end, "text": _text("".join(item.itertext()))})
    else:
        for block in re.split(r"\n\s*\n", raw.replace("\r\n", "\n")):
            lines = block.splitlines()
            for index, line in enumerate(lines):
                if "-->" not in line:
                    continue
                left, right = line.split("-->", 1)
                try:
                    start, end = _timestamp(left), _timestamp(right.strip().split()[0])
                except ValueError:
                    break
                values.append({"start": start, "end": end, "text": _text(" ".join(lines[index + 1:]))})
                break
    return normalize_cues(values)
