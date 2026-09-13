import argparse
import json
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - dependency is present in normal uv test runs.
    PlaywrightError = Exception
    sync_playwright = None

from amazon_capture.capture import (
    CHROME_PATH,
    DISCOVERY_JS,
    PAYMENT_DISCOVERY_JS,
    RunLogger,
    apply_history_probe_defaults,
    build_order_history_coverage,
    build_launch_options,
    chrome_launch_blocked_by_macos_sandbox,
    next_order_page_url,
    normalize_pause_bounds,
    order_detail_links_from_capture,
    order_detail_probe_links_from_capture,
    order_probe_groups_from_capture,
    orders_url_for_time_filter,
    page_reaches_lookback_cutoff,
    parse_order_placed_date,
    preserve_text,
    readiness_from_state,
    sanitize_discovery,
    sanitize_payment_discovery,
    time_filter_for_lookback_days,
    tracking_links_from_capture,
    tracking_probe_links_from_capture,
)
from amazon_capture.observations import load_capture, normalize_captures
from amazon_capture.quality import RawCaptureQualityError


class AmazonObservationTests(unittest.TestCase):
    def test_detects_codex_macos_seatbelt_sandbox_before_chrome_launch(self):
        self.assertTrue(
            chrome_launch_blocked_by_macos_sandbox(
                {"CODEX_SANDBOX": "seatbelt"},
                platform="darwin",
            )
        )
        self.assertFalse(
            chrome_launch_blocked_by_macos_sandbox(
                {"CODEX_SANDBOX_NETWORK_DISABLED": "1"},
                platform="darwin",
            )
        )

    def test_headless_launch_uses_normal_chrome_user_agent(self):
        options = build_launch_options(headed=False)

        self.assertTrue(options["headless"])
        self.assertIn("Chrome/", options["user_agent"])
        self.assertNotIn("HeadlessChrome", options["user_agent"])
        self.assertIn("--disable-blink-features=AutomationControlled", options["args"])

    def test_headed_launch_does_not_override_user_agent(self):
        options = build_launch_options(headed=True)

        self.assertFalse(options["headless"])
        self.assertNotIn("user_agent", options)
        self.assertNotIn("args", options)

    def evaluate_fixture_js(self, fixture_name, js):
        if sync_playwright is None:
            self.skipTest("Playwright is not installed")
        fixture_path = Path(__file__).with_name("fixtures") / fixture_name
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True,
                    **({"executable_path": str(CHROME_PATH)} if CHROME_PATH.exists() else {}),
                )
                try:
                    page = browser.new_page()
                    page.set_content(fixture_path.read_text(encoding="utf-8"))
                    return page.evaluate(js)
                finally:
                    browser.close()
        except (OSError, PlaywrightError) as exc:
            self.skipTest(f"Playwright/Chrome unavailable: {exc}")

    def test_order_capture_normalizes_field_presence_with_raw_text(self):
        order_id = "111-2222222-3333333"
        capture = {
            "target": "orders",
            "capturedAt": "2026-05-26T00:00:00+00:00",
            "cards": [
                {
                    "index": 0,
                    "orderIds": [order_id],
                    "labels": [
                        f"ORDER PLACED May 26 TOTAL $12.34 SHIP TO Jane Customer "
                        f"ORDER # {order_id} Arriving Thursday Track package View invoice"
                    ],
                    "links": [
                        {"text": "View order details", "href": f"https://www.amazon.com/order-details?orderID={order_id}"},
                        {"text": "Track package", "href": "https://www.amazon.com/progress-tracker/package"},
                        {"text": "View invoice", "href": f"https://www.amazon.com/documents/download?orderID={order_id}"},
                    ],
                    "rawText": f"ORDER # {order_id} Jane Customer TOTAL $12.34",
                    "rawTextLength": 240,
                }
            ],
            "trackingPages": [],
        }

        result = normalize_captures([("raw-captures/orders.json", capture)], generated_at="fixed")
        order = result["observations"]["orders"][0]
        serialized = json.dumps(result)

        self.assertEqual(order["amazonOrderIds"], [order_id])
        self.assertIn(order_id, order["observationId"])
        self.assertEqual(order["confidence"], "high")
        self.assertTrue(order["fieldPresence"]["amount"])
        self.assertTrue(order["fieldPresence"]["ship_to"])
        self.assertTrue(order["fieldPresence"]["tracking_link"])
        self.assertEqual(order["linkCounts"]["tracking"], 1)
        self.assertEqual(order["linkCounts"]["invoice"], 1)
        self.assertIn("arriving", order["statusSignals"])
        self.assertIn("Jane Customer", serialized)

    def test_payment_capture_normalizes_order_amount_and_instrument_signals(self):
        capture = {
            "target": "payments",
            "capturedAt": "2026-05-26T00:00:00+00:00",
            "records": [
                {
                    "index": 0,
                    "orderIds": ["222-3333333-4444444"],
                    "labels": ["Prime Visa ****2222 -$23.45 Pending Order #222-3333333-4444444 Amazon.com"],
                    "links": [
                        {"text": "Order #222-3333333-4444444", "href": "https://www.amazon.com/order-details?orderID=222-3333333-4444444"}
                    ],
                    "rawTextLength": 96,
                }
            ],
        }

        result = normalize_captures([("raw-captures/payments.json", capture)], generated_at="fixed")
        payment = result["observations"]["payments"][0]

        self.assertEqual(payment["amazonOrderIds"], ["222-3333333-4444444"])
        self.assertEqual(payment["confidence"], "high")
        self.assertIn("prime_visa", payment["paymentInstrumentSignals"])
        self.assertIn("pending", payment["transactionSignals"])
        self.assertIn("amazon.com", payment["marketplaceSignals"])
        self.assertTrue(payment["fieldPresence"]["order_link"])

    def test_tracking_pages_preserve_raw_tracking_numbers(self):
        raw_tracking_number = "TBAFIXTURE111111"
        capture = {
            "target": "orders",
            "cards": [],
            "trackingPages": [
                {
                    "labels": [f"Shipped with Amazon Tracking ID: {raw_tracking_number} Ordered Shipped Delivered"],
                    "links": [],
                    "bodyTextLength": 120,
                }
            ],
        }

        result = normalize_captures([("raw-captures/orders.json", capture)], generated_at="fixed")
        tracking = result["observations"]["trackingPages"][0]
        serialized = json.dumps(result)

        self.assertEqual(tracking["trackingIds"], [raw_tracking_number])
        self.assertIn(raw_tracking_number, tracking["observationId"])
        self.assertIn("amazon", tracking["carrierSignals"])
        self.assertIn(raw_tracking_number, serialized)

    def test_capture_preserves_tracking_amount_email_and_phone(self):
        raw_tracking_number = "TBAFIXTURE111111"
        text = (
            f"Tracking ID: {raw_tracking_number} UPS 1Z999AA10123456784 "
            "amount $12.34 person@example.com 415-555-1212"
        )
        preserved = preserve_text(text)

        self.assertEqual(preserved, text)

    def test_tracking_observations_reject_stale_redacted_capture(self):
        capture = {
            "target": "orders",
            "cards": [],
            "trackingPages": [
                {
                    "redactedTextPreview": "Tracking ID: TRACKING_ID_8a7802711549_ID_deadbeefcafe",
                    "links": [],
                    "bodyTextLength": 80,
                }
            ],
        }

        with self.assertRaises(RawCaptureQualityError):
            normalize_captures([("raw-captures/orders.json", capture)], generated_at="fixed")

    def test_capture_merges_duplicate_order_candidates(self):
        raw = {
            "cards": [
                {
                    "index": 0,
                    "orderIds": ["111-2222222-3333333"],
                    "labels": ["ORDER # 111-2222222-3333333 Track package"],
                    "links": [{"text": "Track package", "href": "https://www.amazon.com/track"}],
                    "rawText": "ORDER # 111-2222222-3333333 Track package",
                },
                {
                    "index": 1,
                    "orderIds": ["111-2222222-3333333"],
                    "labels": ["View order details"],
                    "links": [{"text": "View order details", "href": "https://www.amazon.com/order"}],
                    "rawText": "ORDER # 111-2222222-3333333 View order details",
                },
            ]
        }

        safe = sanitize_discovery(raw, include_sensitive_text=False)

        self.assertEqual(safe["candidateCardCount"], 2)
        self.assertEqual(safe["cardCount"], 1)
        self.assertEqual(safe["cards"][0]["duplicateCandidateCount"], 2)
        self.assertEqual(safe["cards"][0]["orderIds"], ["111-2222222-3333333"])
        self.assertIn("rawText", safe["cards"][0])
        self.assertEqual(len(safe["cards"][0]["links"]), 2)

    def test_readiness_rejects_returns_or_support_surface(self):
        self.assertEqual(readiness_from_state({"unsupportedSurface": True, "hasOrders": True}), "unsupported")

    def test_tracking_links_ignore_item_package_popups_and_dedupe(self):
        capture = {
            "cards": [
                {
                    "links": [
                        {
                            "text": "View your item",
                            "href": "https://www.amazon.com/your-orders/pop?orderId=111-2222222-3333333&packageId=1&asin=B000000001",
                        },
                        {
                            "text": "Track package",
                            "href": "https://www.amazon.com/gp/your-account/ship-track?orderId=111-2222222-3333333",
                        },
                    ],
                },
                {
                    "links": [
                        {
                            "text": "Track package",
                            "href": "https://www.amazon.com/gp/your-account/ship-track?orderId=111-2222222-3333333",
                        },
                        {
                            "text": "Track package",
                            "href": "https://www.amazon.com/progress-tracker/package?orderID=222-3333333-4444444",
                        },
                    ],
                },
            ]
        }

        self.assertEqual(
            tracking_links_from_capture(capture, 10),
            [
                "https://www.amazon.com/gp/your-account/ship-track?orderId=111-2222222-3333333",
                "https://www.amazon.com/progress-tracker/package?orderID=222-3333333-4444444",
            ],
        )

    def test_tracking_probe_links_keep_order_page_metadata(self):
        order_id = "111-2222222-3333333"
        capture = {
            "cards": [
                {
                    "index": 7,
                    "pageIndex": 2,
                    "pageUrl": "https://www.amazon.com/your-orders/orders?timeFilter=months-3&startIndex=20",
                    "orderIds": [order_id],
                    "links": [
                        {
                            "text": "Track package",
                            "href": f"https://www.amazon.com/gp/your-account/ship-track?orderID={order_id}",
                        },
                    ],
                }
            ]
        }

        probes = tracking_probe_links_from_capture(capture, 10)

        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0]["sourcePageIndex"], 2)
        self.assertEqual(probes[0]["sourceUrl"], "https://www.amazon.com/your-orders/orders?timeFilter=months-3&startIndex=20")
        self.assertEqual(probes[0]["sourceOrderIds"], [order_id])
        self.assertEqual(probes[0]["sourceCardIndex"], 7)

    def test_order_detail_links_ignore_invoice_documents_and_dedupe(self):
        capture = {
            "cards": [
                {
                    "orderIds": ["111-2222222-3333333"],
                    "links": [
                        {"text": "View order details", "href": "https://www.amazon.com/order-details?orderID=111-2222222-3333333"},
                        {"text": "Order details", "href": "https://www.amazon.com/order-details?ref_=ppx_yo2ov_dt_b_fed_asin_title&orderID=111-2222222-3333333"},
                        {"text": "View invoice", "href": "https://www.amazon.com/documents/download?orderID=111-2222222-3333333"},
                        {"text": "View order details", "href": "https://www.amazon.com/order-details?orderID=111-2222222-3333333"},
                    ],
                }
            ]
        }

        self.assertEqual(
            order_detail_links_from_capture(capture, 10),
            ["https://www.amazon.com/order-details?orderID=111-2222222-3333333"],
        )

    def test_order_detail_probe_links_dedupe_by_order_number(self):
        capture = {
            "cards": [
                {
                    "index": 1,
                    "pageIndex": 0,
                    "pageUrl": "https://www.amazon.com/your-orders/orders?timeFilter=last30",
                    "orderIds": ["111-2222222-3333333"],
                    "links": [
                        {"text": "View order details", "href": "https://www.amazon.com/order-details?orderID=111-2222222-3333333&ref_=a"},
                        {"text": "Order details", "href": "https://www.amazon.com/order-details?orderID=111-2222222-3333333&ref_=b"},
                    ],
                },
                {
                    "index": 2,
                    "pageIndex": 0,
                    "pageUrl": "https://www.amazon.com/your-orders/orders?timeFilter=last30",
                    "orderIds": ["222-3333333-4444444"],
                    "links": [
                        {"text": "View order details", "href": "https://www.amazon.com/order-details?orderID=222-3333333-4444444"},
                    ],
                },
            ]
        }

        probes = order_detail_probe_links_from_capture(capture, 10)

        self.assertEqual([probe["sourceOrderIds"][0] for probe in probes], ["111-2222222-3333333", "222-3333333-4444444"])
        self.assertEqual(probes[0]["sourceUrl"], "https://www.amazon.com/your-orders/orders?timeFilter=last30")

    def test_order_probe_groups_keep_detail_and_tracking_links_by_order(self):
        capture = {
            "cards": [
                {
                    "index": 1,
                    "pageIndex": 0,
                    "pageUrl": "https://www.amazon.com/your-orders/orders?timeFilter=last30",
                    "orderIds": ["111-2222222-3333333"],
                    "links": [
                        {"text": "View order details", "href": "https://www.amazon.com/order-details?orderID=111-2222222-3333333"},
                        {"text": "Track package", "href": "https://www.amazon.com/gp/your-account/ship-track?orderID=111-2222222-3333333"},
                    ],
                },
                {
                    "index": 2,
                    "pageIndex": 0,
                    "pageUrl": "https://www.amazon.com/your-orders/orders?timeFilter=last30",
                    "orderIds": ["222-3333333-4444444"],
                    "links": [
                        {"text": "View order details", "href": "https://www.amazon.com/order-details?orderID=222-3333333-4444444"},
                    ],
                },
            ]
        }

        groups = order_probe_groups_from_capture(capture)

        self.assertEqual([group["sourceOrderIds"][0] for group in groups], ["111-2222222-3333333", "222-3333333-4444444"])
        self.assertEqual(len(groups[0]["orderDetailLinks"]), 1)
        self.assertEqual(len(groups[0]["trackingLinks"]), 1)
        self.assertEqual(groups[0]["sourceUrl"], "https://www.amazon.com/your-orders/orders?timeFilter=last30")

    def test_next_order_page_url_synthesizes_pagination_fallback(self):
        current = "https://www.amazon.com/your-orders/orders?timeFilter=months-3&startIndex=20"

        self.assertEqual(
            next_order_page_url(current, [], [current]),
            "https://www.amazon.com/your-orders/orders?timeFilter=months-3&startIndex=30",
        )

    def test_lookback_days_map_to_supported_time_filters(self):
        self.assertEqual(time_filter_for_lookback_days(29), "last30")
        self.assertEqual(time_filter_for_lookback_days(30), "months-3")
        self.assertEqual(time_filter_for_lookback_days(60), "months-3")
        self.assertEqual(time_filter_for_lookback_days(120, today=date(2026, 5, 28)), "year-2026")

    def test_disabled_next_stops_synthetic_pagination(self):
        current = "https://www.amazon.com/your-orders/orders?timeFilter=last30&startIndex=40"
        self.assertEqual(next_order_page_url(current, [
            {"text": "Next page", "href": "", "disabled": True}], [current]), "")

    def test_empty_page_without_end_marker_is_incomplete(self):
        coverage = build_order_history_coverage(
            lookback_days=30, time_filter="last30", max_pages="auto", max_orders=200,
            raw_captures=[{"cards": []}], card_count=0, card_count_returned=0,
            stop_reason="empty_page_without_end_marker")
        self.assertFalse(coverage["complete"])

    def test_order_page_reaches_lookback_cutoff_from_card_dates(self):
        capture = {
            "cards": [
                {"rawText": "ORDER PLACED June 22, 2026 TOTAL $10.00 ORDER # 111-1111111-1111111"},
                {"rawText": "ORDER PLACED May 18, 2026 TOTAL $20.00 ORDER # 222-2222222-2222222"},
            ]
        }

        self.assertEqual(
            parse_order_placed_date("ORDER PLACED May 18, 2026 TOTAL $20.00"),
            date(2026, 5, 18),
        )
        self.assertTrue(page_reaches_lookback_cutoff(capture, 30, today=date(2026, 6, 22)))
        self.assertFalse(page_reaches_lookback_cutoff(capture, 60, today=date(2026, 6, 22)))

    def test_order_history_coverage_marks_max_order_truncation_incomplete(self):
        coverage = build_order_history_coverage(
            lookback_days=10,
            time_filter="last30",
            max_pages="auto",
            max_orders=200,
            raw_captures=[
                {
                    "cards": [
                        {"rawText": "ORDER PLACED June 22, 2026 TOTAL $10.00 ORDER # 111-1111111-1111111"},
                        {"rawText": "ORDER PLACED June 12, 2026 TOTAL $20.00 ORDER # 222-2222222-2222222"},
                    ]
                }
            ],
            card_count=215,
            card_count_returned=200,
            stop_reason="lookback_cutoff_reached",
            stop_detail={"pageIndex": 0},
            today=date(2026, 6, 25),
        )

        self.assertEqual(coverage["coverageCutoffDate"], "2026-06-15")
        self.assertEqual(coverage["oldestOrderDateReached"], "2026-06-12")
        self.assertEqual(coverage["stopReason"], "lookback_cutoff_reached")
        self.assertTrue(coverage["truncatedByMaxOrders"])
        self.assertFalse(coverage["complete"])

    def test_orders_url_for_time_filter(self):
        self.assertEqual(
            orders_url_for_time_filter("months-3"),
            "https://www.amazon.com/your-orders/orders?timeFilter=months-3",
        )
        self.assertEqual(
            orders_url_for_time_filter("year-2026", start_index=20),
            "https://www.amazon.com/your-orders/orders?timeFilter=year-2026&startIndex=20",
        )

    def test_pause_bounds_are_sane(self):
        self.assertEqual(normalize_pause_bounds(2600, 650), (2600, 2600))
        self.assertEqual(normalize_pause_bounds(-1, 0), (0, 0))

    def test_history_orders_run_auto_enables_uncapped_probes(self):
        args = argparse.Namespace(
            target="orders",
            lookback_days=60,
            max_pages=8,
            max_orders=200,
            capture_tracking_pages=False,
            capture_order_detail_pages=False,
            max_tracking_links=None,
            max_order_detail_links=None,
        )

        apply_history_probe_defaults(args)

        self.assertTrue(args.history_auto_probe_mode)
        self.assertTrue(args.capture_tracking_pages)
        self.assertTrue(args.capture_tracking_pages_auto_enabled)
        self.assertIsNone(args.max_tracking_links)
        self.assertTrue(args.capture_order_detail_pages)
        self.assertTrue(args.capture_order_detail_pages_auto_enabled)
        self.assertIsNone(args.max_order_detail_links)

    def test_lookback_history_run_uses_auto_page_depth_when_omitted(self):
        args = argparse.Namespace(
            target="orders",
            lookback_days=60,
            max_pages=None,
            max_pages_auto=True,
            max_orders=200,
            capture_tracking_pages=False,
            capture_order_detail_pages=False,
            max_tracking_links=None,
            max_order_detail_links=None,
        )

        apply_history_probe_defaults(args)

        self.assertTrue(args.history_auto_probe_mode)
        self.assertTrue(args.max_pages_auto)

    def test_small_tracking_run_keeps_interactive_probe_limit(self):
        args = argparse.Namespace(
            target="orders",
            lookback_days=None,
            max_pages=1,
            max_orders=3,
            capture_tracking_pages=True,
            capture_order_detail_pages=False,
            max_tracking_links=None,
            max_order_detail_links=None,
        )

        apply_history_probe_defaults(args)

        self.assertFalse(args.history_auto_probe_mode)
        self.assertTrue(args.capture_tracking_pages)
        self.assertFalse(args.capture_tracking_pages_auto_enabled)
        self.assertEqual(args.max_tracking_links, 3)
        self.assertEqual(args.max_order_detail_links, 3)

    def test_explicit_history_probe_limit_is_honored(self):
        args = argparse.Namespace(
            target="orders",
            lookback_days=60,
            max_pages=8,
            max_orders=200,
            capture_tracking_pages=False,
            capture_order_detail_pages=False,
            max_tracking_links=25,
            max_order_detail_links=12,
        )

        apply_history_probe_defaults(args)

        self.assertTrue(args.capture_tracking_pages)
        self.assertEqual(args.max_tracking_links, 25)
        self.assertTrue(args.capture_order_detail_pages)
        self.assertEqual(args.max_order_detail_links, 12)

    def test_run_logger_writes_jsonl_events(self):
        with TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "run.jsonl"
            logger = RunLogger(log_path)
            logger.event("sample_event", count=2)
            logger.close()

            records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(records[0]["event"], "sample_event")
        self.assertEqual(records[0]["count"], 2)
        self.assertIn("timestamp", records[0])

    def test_order_fixture_runs_real_discovery_js(self):
        raw = self.evaluate_fixture_js("amazon_orders_list_sample.html", DISCOVERY_JS)

        self.assertEqual(raw["selectorCounts"]["orderCard"], 2)
        self.assertEqual(
            [card["orderIds"][0] for card in raw["cards"]],
            ["111-1111111-1111111", "222-2222222-2222222"],
        )

        safe = sanitize_discovery(raw, include_sensitive_text=False)

        self.assertEqual(safe["cardCount"], 2)
        self.assertEqual(safe["cards"][0]["orderIds"], ["111-1111111-1111111"])
        self.assertTrue(
            any(link["text"] == "Track package" for link in safe["cards"][0]["links"])
        )

    def test_payments_fixture_runs_real_discovery_js(self):
        raw = self.evaluate_fixture_js("amazon_payments_sample.html", PAYMENT_DISCOVERY_JS)

        self.assertEqual(raw["selectorCounts"]["transactionLikeData"], 2)
        self.assertEqual(
            [record["orderIds"][0] for record in raw["records"]],
            ["111-1111111-1111111", "222-2222222-2222222"],
        )

        safe = sanitize_payment_discovery(raw, include_sensitive_text=False)

        self.assertEqual(safe["recordCount"], 2)
        self.assertEqual(safe["records"][1]["orderIds"], ["222-2222222-2222222"])
        self.assertIn("Refund $5.67", safe["records"][1]["rawText"])

    def test_load_capture_rejects_malformed_json(self):
        with TemporaryDirectory() as temp_dir:
            bad_capture = Path(temp_dir) / "bad.json"
            bad_capture.write_text("{", encoding="utf-8")

            with self.assertRaises(SystemExit):
                load_capture(bad_capture)

    def test_sanitize_discovery_attaches_ship_to_popover_text_to_card(self):
        raw = {
            "cards": [
                {
                    "index": 0,
                    "pageIndex": 0,
                    "pageUrl": "https://www.amazon.com/your-orders/orders?timeFilter=last30",
                    "orderIds": ["111-2222222-3333333"],
                    "labels": ["ORDER # 111-2222222-3333333 SHIP TO Jane Customer"],
                    "links": [],
                    "rawText": "ORDER # 111-2222222-3333333 SHIP TO Jane Customer",
                    "shipToPopovers": [
                        {
                            "sourceCardIndex": 0,
                            "sourcePageIndex": 0,
                            "sourceUrl": "https://www.amazon.com/your-orders/orders?timeFilter=last30",
                            "sourceOrderIds": ["111-2222222-3333333"],
                            "triggerText": "SHIP TO Jane Customer",
                            "labels": ["Jane Customer 123 Example Rd New Castle, DE 19720"],
                            "rawText": "Jane Customer 123 Example Rd New Castle, DE 19720",
                        }
                    ],
                }
            ]
        }

        safe = sanitize_discovery(raw, include_sensitive_text=False)
        card = safe["cards"][0]

        self.assertEqual(card["pageIndex"], 0)
        self.assertEqual(card["pageUrl"], "https://www.amazon.com/your-orders/orders?timeFilter=last30")
        self.assertEqual(len(card["shipToPopovers"]), 1)
        self.assertIn("New Castle, DE 19720", card["rawText"])
        self.assertIn("Jane Customer 123 Example Rd New Castle, DE 19720", card["labels"])


if __name__ == "__main__":
    unittest.main()
