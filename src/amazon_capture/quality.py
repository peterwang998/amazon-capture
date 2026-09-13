#!/usr/bin/env python3
"""Validate that browser captures contain raw local audit values.

The current workflow is local-only and intentionally keeps raw order numbers,
tracking numbers, amounts, and account/order signals for auditability. Older
captures from earlier scraper versions may contain redacted or hashed
placeholders; those captures are stale input and should not be imported.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Sequence


STALE_VALUE_PATTERNS: Sequence[tuple[str, re.Pattern[str]]] = (
    ("redacted_marker", re.compile(r"\b[A-Z0-9_]*REDACTED[A-Z0-9_:-]*\b", re.IGNORECASE)),
    ("hashed_order_id", re.compile(r"\b(?:AMAZON_)?ORDER_ID_[0-9a-f]{8,}\b", re.IGNORECASE)),
    ("hashed_tracking_id", re.compile(r"\bTRACKING_ID_[0-9a-f]{8,}(?:_ID_[0-9a-f]{8,})?\b", re.IGNORECASE)),
    ("hashed_value", re.compile(r"\bVALUE_[0-9a-f]{8,}\b", re.IGNORECASE)),
    ("hashed_number", re.compile(r"\b(?:NUMBER|LAST4|PHONE|EMAIL|ADDRESS)_[0-9a-f]{8,}\b", re.IGNORECASE)),
)

STALE_KEY_RE = re.compile(r"redacted|hash(?:es)?$", re.IGNORECASE)


@dataclass(frozen=True)
class QualityFinding:
    source: str
    path: str
    kind: str
    preview: str

    def as_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "path": self.path,
            "kind": self.kind,
            "preview": self.preview,
        }


class RawCaptureQualityError(ValueError):
    def __init__(self, findings: Sequence[QualityFinding]):
        self.findings = list(findings)
        preview = "; ".join(
            f"{finding.source}:{finding.path}:{finding.kind}" for finding in self.findings[:5]
        )
        extra = "" if len(self.findings) <= 5 else f"; +{len(self.findings) - 5} more"
        super().__init__(f"stale redacted/hashed capture values found: {preview}{extra}")


def compact_preview(value: object, limit: int = 180) -> str:
    preview = re.sub(r"\s+", " ", str(value)).strip()
    return preview if len(preview) <= limit else preview[: limit - 1] + "..."


def scan_raw_capture_payload(
    payload: Any,
    *,
    source: str = "<memory>",
    max_findings: int = 50,
) -> List[QualityFinding]:
    findings: List[QualityFinding] = []

    def add(path: str, kind: str, value: object) -> None:
        if len(findings) < max_findings:
            findings.append(QualityFinding(source, path, kind, compact_preview(value)))

    def walk(value: Any, path: str) -> None:
        if len(findings) >= max_findings:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                key_text = str(key)
                child_path = f"{path}.{key_text}" if path else key_text
                if STALE_KEY_RE.search(key_text):
                    add(child_path, "stale_key", key_text)
                walk(child, child_path)
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")
            return
        if isinstance(value, str):
            for kind, pattern in STALE_VALUE_PATTERNS:
                match = pattern.search(value)
                if match:
                    add(path, kind, match.group(0))
                    break

    walk(payload, "$")
    return findings


def assert_raw_capture_payload(payload: Any, *, source: str = "<memory>") -> None:
    findings = scan_raw_capture_payload(payload, source=source)
    if findings:
        raise RawCaptureQualityError(findings)


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc


def scan_capture_file(path: Path) -> List[QualityFinding]:
    return scan_raw_capture_payload(load_json(path), source=str(path))


def assert_raw_capture_file(path: Path) -> None:
    findings = scan_capture_file(path)
    if findings:
        raise RawCaptureQualityError(findings)


def assert_raw_capture_files(paths: Iterable[Path | None]) -> None:
    findings: List[QualityFinding] = []
    for path in paths:
        if not path:
            continue
        findings.extend(scan_capture_file(path))
    if findings:
        raise RawCaptureQualityError(findings)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("captures", nargs="+", type=Path, help="JSON capture files to validate.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a short text summary.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    findings: List[QualityFinding] = []
    for capture in args.captures:
        findings.extend(scan_capture_file(capture))

    if args.json:
        print(json.dumps({"ok": not findings, "findings": [finding.as_dict() for finding in findings]}, indent=2))
    elif findings:
        for finding in findings:
            print(f"{finding.source}: {finding.path}: {finding.kind}: {finding.preview}")
    else:
        print(f"raw capture quality ok: {len(args.captures)} file(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
