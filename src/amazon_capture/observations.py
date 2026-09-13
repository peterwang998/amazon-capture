#!/usr/bin/env python3
"""Normalize saved Amazon captures while preserving identifiers and provenance."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple


RUNTIME_ROOT = Path.cwd()
from .quality import assert_raw_capture_payload

DEFAULT_OUTPUT_DIR = RUNTIME_ROOT / "raw-captures" / "amazon-observations"
SCHEMA_VERSION = "amazon-observations/v0.1"

RAW_ORDER_ID_RE = re.compile(r"\b\d{3}-\d{7}-\d{7}\b")
AMAZON_TRACKING_RE = re.compile(r"\bTBA[A-Z0-9]{8,}\b", re.IGNORECASE)
UPS_TRACKING_RE = re.compile(r"\b1Z[A-Z0-9]{16}\b", re.IGNORECASE)
AMOUNT_VALUE_RE = re.compile(r"[-+]?\$[0-9][0-9,.]*")

STATUS_PATTERNS = {
    "ordered": re.compile(r"\border(?:ed| placed)\b", re.IGNORECASE),
    "arriving": re.compile(r"\barriving\b", re.IGNORECASE),
    "shipped": re.compile(r"\bshipped\b", re.IGNORECASE),
    "out_for_delivery": re.compile(r"\bout for delivery\b", re.IGNORECASE),
    "delivered": re.compile(r"\bdelivered\b", re.IGNORECASE),
    "delayed": re.compile(r"\bdelayed\b", re.IGNORECASE),
    "cancelled": re.compile(r"\bcancell?ed\b", re.IGNORECASE),
}

PAYMENT_INSTRUMENT_PATTERNS = {
    "prime_visa": re.compile(r"\bprime visa\b", re.IGNORECASE),
    "visa": re.compile(r"\bvisa\b", re.IGNORECASE),
    "mastercard": re.compile(r"\bmastercard\b", re.IGNORECASE),
    "amex": re.compile(r"\b(?:american express|amex)\b", re.IGNORECASE),
    "discover": re.compile(r"\bdiscover\b", re.IGNORECASE),
    "gift_card": re.compile(r"\bgift card\b", re.IGNORECASE),
    "amazon_points": re.compile(r"\b(?:amazon )?points\b", re.IGNORECASE),
    "store_card": re.compile(r"\bstore card\b", re.IGNORECASE),
}

PAYMENT_STATUS_PATTERNS = {
    "pending": re.compile(r"\bpending\b", re.IGNORECASE),
    "refund": re.compile(r"\brefund(?:ed)?\b", re.IGNORECASE),
    "charge": re.compile(r"\bcharg(?:e|ed)\b", re.IGNORECASE),
    "posted": re.compile(r"\bposted\b", re.IGNORECASE),
}

CARRIER_PATTERNS = {
    "amazon": re.compile(r"\b(?:shipped with amazon|tracking id:\s*tba|TBA[A-Z0-9]{8,})\b", re.IGNORECASE),
    "ups": re.compile(r"\b(?:ups|1Z[A-Z0-9]{16})\b", re.IGNORECASE),
    "fedex": re.compile(r"\bfedex\b", re.IGNORECASE),
    "usps": re.compile(r"\busps\b", re.IGNORECASE),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def readable_token(value: object) -> str:
    token = re.sub(r"\s+", "-", str(value).strip())
    token = re.sub(r"[^A-Za-z0-9_.:/=@+-]+", "-", token)
    return token.strip("-")


def stable_id(prefix: str, *parts: object) -> str:
    tokens = [readable_token(part) for part in parts if part is not None]
    suffix = "__".join(token for token in tokens if token)
    return f"{prefix}:{suffix}" if suffix else prefix


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def compact_counter(counter: Counter[str]) -> Dict[str, int]:
    return {key: counter[key] for key in sorted(counter) if counter[key]}


def unique_sorted(values: Iterable[str]) -> List[str]:
    return sorted({value for value in values if value})


def all_record_text(record: Dict[str, Any]) -> str:
    parts: List[str] = []
    parts.extend(str(label) for label in record.get("labels", []))
    parts.append(str(record.get("textPreview", "")))
    parts.append(str(record.get("rawText", "")))
    for link in record.get("links", []):
        parts.append(str(link.get("text", "")))
        parts.append(str(link.get("href", "")))
    return " ".join(parts)


def text_preview(record: Dict[str, Any]) -> str:
    raw_text = str(record.get("rawText", ""))
    return str(record.get("textPreview") or raw_text[:1200] or "")


def extract_order_ids(record: Dict[str, Any], blob: str) -> List[str]:
    values = [str(value) for value in record.get("orderIds", []) if value]
    values.extend(match.group(0) for match in RAW_ORDER_ID_RE.finditer(blob))
    return unique_sorted(values)


def extract_tracking_ids(blob: str) -> List[str]:
    values: List[str] = []
    values.extend(match.group(0).upper() for match in AMAZON_TRACKING_RE.finditer(blob))
    values.extend(match.group(0).upper() for match in UPS_TRACKING_RE.finditer(blob))
    return unique_sorted(values)


def match_names(patterns: Dict[str, re.Pattern[str]], blob: str) -> List[str]:
    return [name for name, pattern in patterns.items() if pattern.search(blob)]


def classify_link(link: Dict[str, str]) -> str:
    text = f"{link.get('text', '')} {link.get('href', '')}".lower()
    if any(token in text for token in ("track", "ship-track", "progress-tracker", "package")):
        return "tracking"
    if any(token in text for token in ("invoice", "summary/print", "documents/download")):
        return "invoice"
    if any(token in text for token in ("payment", "transactions", "cpe/yourpayments")):
        return "payment"
    if any(token in text for token in ("order-details", "orderid=", "view order details")):
        return "order_details"
    if any(token in text for token in ("return", "replace")):
        return "return_or_replace"
    if any(token in text for token in ("review", "buy it again", "product")):
        return "product_action"
    return "other"


def link_counts(links: Sequence[Dict[str, str]]) -> Dict[str, int]:
    return compact_counter(Counter(classify_link(link) for link in links))


def observation_confidence(required: Sequence[bool], optional: Sequence[bool]) -> str:
    if all(required) and any(optional):
        return "high"
    if any(required):
        return "medium"
    return "low"


def normalize_order_card(card: Dict[str, Any], source_path: str, capture: Dict[str, Any]) -> Dict[str, Any]:
    blob = all_record_text(card)
    counts = link_counts(card.get("links", []))
    order_ids = extract_order_ids(card, blob)
    tracking_ids = extract_tracking_ids(blob)
    status_signals = match_names(STATUS_PATTERNS, blob)
    carrier_signals = match_names(CARRIER_PATTERNS, blob)

    field_presence = {
        "order_id": bool(order_ids),
        "amount": bool(AMOUNT_VALUE_RE.search(blob)),
        "ship_to": "ship to" in blob.lower(),
        "tracking_id": bool(tracking_ids),
        "tracking_link": counts.get("tracking", 0) > 0,
        "order_details_link": counts.get("order_details", 0) > 0,
        "invoice_link": counts.get("invoice", 0) > 0,
        "delivery_status": any(signal in status_signals for signal in ("arriving", "shipped", "out_for_delivery", "delivered")),
    }
    notes = []
    if not field_presence["order_id"]:
        notes.append("missing_order_id")
    if field_presence["tracking_link"] and not field_presence["tracking_id"]:
        notes.append("tracking_link_requires_page_probe")

    return {
        "observationId": stable_id("amazon_order_observation", source_path, card.get("index"), *order_ids),
        "source": {
            "capturePath": source_path,
            "target": capture.get("target") or "orders",
            "recordType": "order_card",
            "recordIndex": card.get("index"),
        },
        "amazonOrderIds": order_ids,
        "trackingIds": tracking_ids,
        "labels": card.get("labels", []),
        "links": card.get("links", []),
        "textPreview": text_preview(card),
        "statusSignals": status_signals,
        "carrierSignals": carrier_signals,
        "linkCounts": counts,
        "fieldPresence": field_presence,
        "rawTextLength": card.get("rawTextLength", 0),
        "confidence": observation_confidence(
            required=[field_presence["order_id"]],
            optional=[field_presence["tracking_link"], field_presence["order_details_link"], field_presence["invoice_link"]],
        ),
        "notes": notes,
    }


def normalize_tracking_page(page: Dict[str, Any], source_path: str, index: int) -> Dict[str, Any]:
    blob = all_record_text(page)
    counts = link_counts(page.get("links", []))
    tracking_ids = extract_tracking_ids(blob)
    status_signals = match_names(STATUS_PATTERNS, blob)
    carrier_signals = match_names(CARRIER_PATTERNS, blob)

    field_presence = {
        "tracking_id": bool(tracking_ids),
        "carrier": bool(carrier_signals),
        "delivery_status": bool(status_signals),
        "order_details_link": counts.get("order_details", 0) > 0,
    }
    return {
        "observationId": stable_id("amazon_tracking_observation", source_path, index, *tracking_ids),
        "source": {
            "capturePath": source_path,
            "target": "orders",
            "recordType": "tracking_page",
            "recordIndex": index,
        },
        "trackingIds": tracking_ids,
        "labels": page.get("labels", []),
        "links": page.get("links", []),
        "textPreview": text_preview(page),
        "statusSignals": status_signals,
        "carrierSignals": carrier_signals,
        "linkCounts": counts,
        "fieldPresence": field_presence,
        "bodyTextLength": page.get("bodyTextLength", 0),
        "confidence": observation_confidence(
            required=[field_presence["tracking_id"]],
            optional=[field_presence["carrier"], field_presence["delivery_status"]],
        ),
        "notes": [] if field_presence["tracking_id"] else ["missing_tracking_id"],
    }


def normalize_payment_record(record: Dict[str, Any], source_path: str, capture: Dict[str, Any]) -> Dict[str, Any]:
    blob = all_record_text(record)
    counts = link_counts(record.get("links", []))
    order_ids = extract_order_ids(record, blob)
    instrument_signals = match_names(PAYMENT_INSTRUMENT_PATTERNS, blob)
    transaction_signals = match_names(PAYMENT_STATUS_PATTERNS, blob)
    marketplace_signals = []
    lowered = blob.lower()
    if "amazon.com" in lowered:
        marketplace_signals.append("amazon.com")
    if "amzn mktp" in lowered:
        marketplace_signals.append("amzn_mktp_us")

    field_presence = {
        "order_id": bool(order_ids),
        "amount": bool(AMOUNT_VALUE_RE.search(blob)),
        "payment_instrument": bool(instrument_signals),
        "order_link": counts.get("order_details", 0) > 0,
        "refund_signal": "refund" in transaction_signals,
        "pending_signal": "pending" in transaction_signals,
    }
    notes = []
    if not field_presence["order_id"]:
        notes.append("missing_order_id")
    if not field_presence["amount"]:
        notes.append("missing_amount_signal")

    return {
        "observationId": stable_id("amazon_payment_observation", source_path, record.get("index"), *order_ids),
        "source": {
            "capturePath": source_path,
            "target": capture.get("target") or "payments",
            "recordType": "payment_record",
            "recordIndex": record.get("index"),
        },
        "amazonOrderIds": order_ids,
        "labels": record.get("labels", []),
        "links": record.get("links", []),
        "textPreview": text_preview(record),
        "paymentInstrumentSignals": instrument_signals,
        "transactionSignals": transaction_signals,
        "marketplaceSignals": marketplace_signals,
        "linkCounts": counts,
        "fieldPresence": field_presence,
        "rawTextLength": record.get("rawTextLength", 0),
        "confidence": observation_confidence(
            required=[field_presence["order_id"], field_presence["amount"]],
            optional=[field_presence["payment_instrument"], field_presence["order_link"]],
        ),
        "notes": notes,
    }


def summarize_capture(capture: Dict[str, Any], source_path: str) -> Dict[str, Any]:
    target = capture.get("target") or ("payments" if capture.get("records") else "orders")
    summary = {
        "capturePath": source_path,
        "target": target,
        "capturedAt": capture.get("capturedAt"),
        "readiness": capture.get("readiness"),
        "auth": capture.get("auth", {}),
        "selectorCounts": capture.get("selectorCounts", {}),
        "orderCardCount": len(capture.get("cards", [])),
        "paymentRecordCount": len(capture.get("records", [])),
        "trackingPageCount": len(capture.get("trackingPages", [])),
    }
    if capture.get("initialReadiness"):
        summary["initialReadiness"] = capture.get("initialReadiness")
    if capture.get("finalReadiness"):
        summary["finalReadiness"] = capture.get("finalReadiness")
    if capture.get("screenshotPath"):
        summary["screenshotPath"] = capture.get("screenshotPath")
    return summary


def normalize_captures(
    source_captures: Sequence[Tuple[str, Dict[str, Any]]],
    generated_at: str | None = None,
) -> Dict[str, Any]:
    orders = []
    tracking_pages = []
    payments = []
    source_summaries = []

    for source_path, capture in source_captures:
        assert_raw_capture_payload(capture, source=source_path)
        target = capture.get("target") or ("payments" if capture.get("records") else "orders")
        source_summaries.append(summarize_capture(capture, source_path))
        if target == "payments":
            payments.extend(
                normalize_payment_record(record, source_path, capture)
                for record in capture.get("records", [])
            )
        else:
            orders.extend(
                normalize_order_card(card, source_path, capture)
                for card in capture.get("cards", [])
            )
            tracking_pages.extend(
                normalize_tracking_page(page, source_path, index)
                for index, page in enumerate(capture.get("trackingPages", []))
            )

    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": generated_at or now_iso(),
        "sourceCaptures": source_summaries,
        "observations": {
            "orders": orders,
            "trackingPages": tracking_pages,
            "payments": payments,
        },
        "coverage": build_coverage(orders, tracking_pages, payments),
    }


def count_presence(records: Sequence[Dict[str, Any]], field: str) -> int:
    return sum(1 for record in records if record.get("fieldPresence", {}).get(field))


def build_coverage(
    orders: Sequence[Dict[str, Any]],
    tracking_pages: Sequence[Dict[str, Any]],
    payments: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "orders": {
            "observed": len(orders),
            "withOrderId": count_presence(orders, "order_id"),
            "withAmountSignal": count_presence(orders, "amount"),
            "withTrackingLink": count_presence(orders, "tracking_link"),
            "withTrackingId": count_presence(orders, "tracking_id"),
            "withOrderDetailsLink": count_presence(orders, "order_details_link"),
            "withInvoiceLink": count_presence(orders, "invoice_link"),
        },
        "trackingPages": {
            "observed": len(tracking_pages),
            "withTrackingId": count_presence(tracking_pages, "tracking_id"),
            "withCarrierSignal": count_presence(tracking_pages, "carrier"),
            "withDeliveryStatus": count_presence(tracking_pages, "delivery_status"),
        },
        "payments": {
            "observed": len(payments),
            "withOrderId": count_presence(payments, "order_id"),
            "withAmountSignal": count_presence(payments, "amount"),
            "withPaymentInstrument": count_presence(payments, "payment_instrument"),
            "withOrderLink": count_presence(payments, "order_link"),
            "pending": count_presence(payments, "pending_signal"),
            "refunds": count_presence(payments, "refund_signal"),
        },
    }


def load_capture(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc


def default_output_path(output_dir: Path) -> Path:
    return output_dir / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-amazon-observations.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize Amazon order and payment captures.")
    parser.add_argument("captures", nargs="+", help="Amazon capture JSON paths.")
    parser.add_argument("--output", help="Output JSON path. Defaults under raw-captures/amazon-observations/.")
    parser.add_argument("--output-dir", default=str(Path.cwd() / "raw-captures" / "amazon-observations"), help="Default output directory.")
    parser.add_argument("--stdout", action="store_true", help="Write normalized JSON to stdout instead of a file.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_captures = []
    for capture_arg in args.captures:
        path = Path(capture_arg)
        source_captures.append((display_path(path), load_capture(path)))

    observations = normalize_captures(source_captures)
    output_json = json.dumps(observations, indent=2, sort_keys=True)
    if args.stdout:
        print(output_json)
        return 0

    output_path = Path(args.output) if args.output else default_output_path(Path(args.output_dir))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output_json + "\n", encoding="utf-8")
    print(json.dumps({
        "output": display_path(output_path),
        "source_captures": len(source_captures),
        "orders": len(observations["observations"]["orders"]),
        "tracking_pages": len(observations["observations"]["trackingPages"]),
        "payments": len(observations["observations"]["payments"]),
        "coverage": observations["coverage"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
