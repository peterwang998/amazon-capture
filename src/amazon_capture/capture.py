#!/usr/bin/env python3
"""Read-only Amazon Orders bookkeeping capture.

This script opens a headed Chrome session with a private persistent profile and
captures visible order, tracking, and payment evidence from Amazon pages. It
intentionally does not write to SQLite or create a final data model.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import random
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


RUNTIME_ROOT = Path.cwd()
SHARED_PROFILE_ENV = "DROPSHIP_BROWSER_PROFILE_DIR"
DEFAULT_OUTPUT_DIR = RUNTIME_ROOT / "raw-captures" / "amazon-field-discovery"
DEFAULT_SCREENSHOT_DIR = RUNTIME_ROOT / "screenshots" / "amazon-field-discovery"
DEFAULT_LOG_DIR = RUNTIME_ROOT / "logs" / "amazon-field-discovery"
DEFAULT_ORDERS_URL = "https://www.amazon.com/your-orders/orders?timeFilter=last30"
ORDERS_URL_PATH = "/your-orders/orders"
DEFAULT_PAYMENTS_URL = "https://www.amazon.com/cpe/yourpayments/transactions"
CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
FALLBACK_HEADLESS_CHROME_MAJOR = "149"
CODEX_MACOS_SANDBOX_ENV = "CODEX_SANDBOX"
CODEX_MACOS_SANDBOX_VALUE = "seatbelt"
SANDBOXED_CHROME_EXIT = 70
ORDER_READY_SELECTOR = ".order-card, .js-order-card, [data-order-id]"
PAYMENT_READY_SELECTOR = "body"
DEFAULT_INTERACTIVE_PROBE_LIMIT = 3
BOOKKEEPING_AUTO_ORDER_THRESHOLD = 50
BOOKKEEPING_AUTO_PAGE_SAFETY_CAP = 60

ORDER_ID_RE = re.compile(r"\b\d{3}-\d{7}-\d{7}\b")
AMAZON_TRACKING_RE = re.compile(r"\bTBA[A-Z0-9]{8,}\b", re.IGNORECASE)
UPS_TRACKING_RE = re.compile(r"\b1Z[A-Z0-9]{16}\b", re.IGNORECASE)
ORDER_PLACED_RE = re.compile(r"\bORDER\s+PLACED\s+([A-Za-z]+\.?\s+\d{1,2}(?:,\s*\d{4})?)\b", re.IGNORECASE)


DISCOVERY_JS = r"""
() => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const linesOf = (el) => clean(el.innerText || el.textContent || "")
    .split(/(?<=\.)\s+|\n+/)
    .map(clean)
    .filter(Boolean);
  const textOf = (el) => clean(el.innerText || el.textContent || "");
  const unique = (values) => Array.from(new Set(values.filter(Boolean)));
  const qsa = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const selectorCounts = {
    orderCard: qsa(".order-card").length,
    jsOrderCard: qsa(".js-order-card").length,
    dataOrderId: qsa("[data-order-id]").length,
    orderBoxGroup: qsa(".a-box-group").length,
    orderBox: qsa(".a-box").length,
    trackLinks: qsa('a[href*="track"], a[href*="ship-track"], a[href*="/progress-tracker"], a[href*="package"]').length,
    orderDetailsLinks: qsa('a[href*="order-details"], a[href*="orderID="]').length,
    invoiceLinks: qsa('a[href*="invoice"], a[href*="summary/print"], a[href*="documents/download"]').length,
  };

  const signInIndicators = [
    "#ap_email",
    "#ap_password",
    "#signInSubmit",
    "form[name='signIn']",
    ".auth-pagelet-container",
  ];

  const blockerIndicators = [
    "captcha",
    "enter the characters you see below",
    "two-step verification",
    "multi-factor authentication",
    "approve the notification",
    "verify it's you",
  ];

  const bodyText = textOf(document.body).toLowerCase();
  const auth = {
    signInSelectorFound: signInIndicators.some((selector) => qsa(selector).length > 0),
    blockerTextFound: blockerIndicators.some((needle) => bodyText.includes(needle)),
  };

  const candidateSelectors = [
    ".order-card",
    ".js-order-card",
    "[data-order-id]",
    ".a-box-group",
    ".a-box",
  ];

  const candidateNodes = [];
  for (const selector of candidateSelectors) {
    for (const el of qsa(selector)) {
      const text = textOf(el);
      if (text.length < 80) continue;
      if (!/(order placed|order date|order #|order id|ordered on|delivered|arriving|shipped|track package|view order details)/i.test(text)) {
        continue;
      }
      candidateNodes.push({ selector, el, text });
    }
  }

  const seen = new Set();
  const cards = [];
  for (const candidate of candidateNodes) {
    const fingerprint = candidate.text.slice(0, 500);
    if (seen.has(fingerprint)) continue;
    seen.add(fingerprint);

    const el = candidate.el;
    const links = qsa("a", el)
      .map((a) => ({
        text: clean(a.innerText || a.textContent || a.getAttribute("aria-label") || ""),
        href: a.href || a.getAttribute("href") || "",
      }))
      .filter((link) => link.text || link.href)
      .slice(0, 40);

    const lines = linesOf(el).slice(0, 80);
    const orderIds = unique((candidate.text.match(/\b\d{3}-\d{7}-\d{7}\b/g) || []));
    const labels = unique(lines.filter((line) =>
      /(order placed|total|ship to|delivered|arriving|shipped|track package|view order details|invoice|return|buy again|carrier|tracking)/i.test(line)
    ));

    cards.push({
      index: cards.length,
      sourceSelector: candidate.selector,
      tagName: el.tagName,
      className: el.className || "",
      dataOrderId: el.getAttribute("data-order-id") || "",
      orderIds,
      labels,
      links,
      rawText: candidate.text,
    });
  }

  return {
    capturedAtBrowserTime: new Date().toISOString(),
    url: window.location.href,
    title: document.title,
    selectorCounts,
    auth,
    bodyTextLength: textOf(document.body).length,
    cards,
  };
}
"""


TRACKING_PAGE_JS = r"""
() => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const text = clean(document.body?.innerText || document.body?.textContent || "");
  const lines = text.split(/(?<=\.)\s+|\n+/).map(clean).filter(Boolean).slice(0, 120);
  const links = Array.from(document.querySelectorAll("a"))
    .map((a) => ({
      text: clean(a.innerText || a.textContent || a.getAttribute("aria-label") || ""),
      href: a.href || a.getAttribute("href") || "",
    }))
    .filter((link) => link.text || link.href)
    .slice(0, 80);
  const labels = lines.filter((line) =>
    /(tracking|carrier|delivered|arriving|shipped|out for delivery|package|order|shipment|status)/i.test(line)
  );
  return {
    capturedAtBrowserTime: new Date().toISOString(),
    url: window.location.href,
    title: document.title,
    bodyTextLength: text.length,
    labels,
    links,
    rawText: text,
  };
}
"""


PAYMENT_DISCOVERY_JS = r"""
() => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const textOf = (el) => clean(el.innerText || el.textContent || "");
  const qsa = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const unique = (values) => Array.from(new Set(values.filter(Boolean)));
  const linesOf = (el) => textOf(el).split(/(?<=\.)\s+|\n+/).map(clean).filter(Boolean);

  const selectorCounts = {
    tableRows: qsa("tr").length,
    aRows: qsa(".a-row").length,
    aBoxes: qsa(".a-box").length,
    transactionLikeData: qsa('[data-testid*="transaction"], [data-test*="transaction"], [id*="transaction"], [class*="transaction"]').length,
    orderLinks: qsa('a[href*="orderID="], a[href*="order-details"]').length,
    paymentLinks: qsa('a[href*="payment"], a[href*="transactions"], a[href*="cpe"]').length,
  };

  const signInIndicators = [
    "#ap_email",
    "#ap_password",
    "#signInSubmit",
    "form[name='signIn']",
    ".auth-pagelet-container",
  ];
  const bodyText = textOf(document.body).toLowerCase();
  const auth = {
    signInSelectorFound: signInIndicators.some((selector) => qsa(selector).length > 0),
    blockerTextFound: ["captcha", "two-step verification", "verify it's you", "approve the notification"]
      .some((needle) => bodyText.includes(needle)),
  };

  const candidates = [];
  const selectors = [
    '[data-testid*="transaction"]',
    '[data-test*="transaction"]',
    '[id*="transaction"]',
    '[class*="transaction"]',
    "tr",
    ".a-box",
    ".a-row",
  ];
  for (const selector of selectors) {
    for (const el of qsa(selector)) {
      const text = textOf(el);
      if (text.length < 40 || text.length > 2500) continue;
      if (!/(transaction|payment|visa|mastercard|american express|amex|discover|gift card|refund|charge|charged|ending in|order|posted|\$\d)/i.test(text)) {
        continue;
      }
      candidates.push({ selector, el, text });
    }
  }

  const seen = new Set();
  const records = [];
  for (const candidate of candidates) {
    const fingerprint = candidate.text.slice(0, 500);
    if (seen.has(fingerprint)) continue;
    seen.add(fingerprint);

    const links = qsa("a", candidate.el)
      .map((a) => ({
        text: clean(a.innerText || a.textContent || a.getAttribute("aria-label") || ""),
        href: a.href || a.getAttribute("href") || "",
      }))
      .filter((link) => link.text || link.href)
      .slice(0, 40);
    const lines = linesOf(candidate.el).slice(0, 80);
    const labels = unique(lines.filter((line) =>
      /(transaction|payment|visa|mastercard|american express|amex|discover|gift card|refund|charge|charged|ending in|order|posted|date|amount|card|\$\d)/i.test(line)
    ));
    const orderIds = unique((candidate.text.match(/\b\d{3}-\d{7}-\d{7}\b/g) || []));

    records.push({
      index: records.length,
      sourceSelector: candidate.selector,
      tagName: candidate.el.tagName,
      className: candidate.el.className || "",
      orderIds,
      labels,
      links,
      rawText: candidate.text,
    });
  }

  return {
    capturedAtBrowserTime: new Date().toISOString(),
    url: window.location.href,
    title: document.title,
    selectorCounts,
    auth,
    bodyTextLength: textOf(document.body).length,
    records,
  };
}
"""

AUTH_STATE_JS = r"""
() => {
  const text = (document.body?.innerText || '').toLowerCase();
  const url = window.location.href.toLowerCase();
  const title = document.title.toLowerCase();
  const hasSignIn = !!document.querySelector('#ap_email, #ap_password, #signInSubmit, form[name="signIn"]');
  const blocked = [
    'captcha',
    'enter the characters you see below',
    'two-step verification',
    'multi-factor authentication',
    'approve the notification',
    "verify it's you",
    'one-time password',
    'otp',
  ].some((needle) => text.includes(needle));
  const isOrderHistoryUrl = url.includes('/your-orders/orders');
  const isPaymentsUrl = url.includes('/cpe/yourpayments/transactions');
  const hasOrderHistoryShape = !!document.querySelector('.order-card, .js-order-card, [data-order-id]')
    || /order placed|order #|track package|view order details/.test(text);
  const hasPaymentShape = /transactions|payment method|posted|gift card/.test(text);
  const knownUnsupportedUrl = (
    url.includes('/returns')
    || url.includes('/spr/returns')
    || url.includes('/gp/help')
    || url.includes('/hz/contact-us')
    || url.includes('/gp/css/returns')
  );
  const unsupportedTextOnlySurface = !isOrderHistoryUrl && !isPaymentsUrl && !hasOrderHistoryShape && (
    /return item|return or replace items|item support|product support|get help with order/.test(text + " " + title)
  );
  const unsupportedSurface = knownUnsupportedUrl || unsupportedTextOnlySurface;
  const hasOrders = !unsupportedSurface && (
    (isOrderHistoryUrl && hasOrderHistoryShape)
    || (isPaymentsUrl && hasPaymentShape)
  );
  return { hasSignIn, blocked, hasOrders, unsupportedSurface, url: window.location.href, title: document.title };
}
"""

ORDER_PAGE_LINKS_JS = r"""
() => Array.from(document.querySelectorAll('a[href*="/your-orders/orders"], .a-pagination .a-last.a-disabled, [aria-label*="Next"][aria-disabled="true"]'))
  .map((a) => ({
    text: (a.innerText || a.textContent || a.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim(),
    href: a.href || a.getAttribute("href") || "",
    disabled: a.getAttribute('aria-disabled') === 'true' || !!a.closest('.a-disabled'),
  }))
  .filter((link) => link.disabled || (link.href && /(?:timeFilter|startIndex|pagination|orderFilter)/i.test(link.href + " " + link.text)))
"""

CLICK_LINK_BY_HREF_JS = r"""
(href) => {
  const absolute = (value) => {
    try {
      return new URL(value, window.location.href).href;
    } catch {
      return value || "";
    }
  };
  const target = absolute(href);
  const targetUrl = new URL(target, window.location.href);
  const links = Array.from(document.querySelectorAll("a[href]"));
  const candidates = links.filter((link) => {
    let linkUrl;
    try {
      linkUrl = new URL(link.href || link.getAttribute("href") || "", window.location.href);
    } catch {
      return false;
    }
    return linkUrl.href === target
      || (linkUrl.pathname === targetUrl.pathname && linkUrl.search === targetUrl.search);
  });
  const picked = candidates.find((link) => {
    const rect = link.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }) || candidates[0];
  if (!picked) return false;
  picked.scrollIntoView({block: "center", inline: "center"});
  picked.click();
  return true;
}
"""


CARD_SHIP_TO_DISCOVERY_JS = r"""
async () => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const qsa = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const textOf = (el) => clean(el.innerText || el.textContent || "");
  const unique = (values) => Array.from(new Set(values.filter(Boolean)));
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  };
  const linesOf = (text) => clean(text).split(/(?<=\.)\s+|\n+/).map(clean).filter(Boolean);
  const closePopovers = () => {
    for (const close of qsa(".a-popover-close, button[aria-label*='Close'], button[aria-label*='close']")) {
      if (visible(close)) {
        try { close.click(); } catch {}
      }
    }
    try {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    } catch {}
  };

  const candidateSelectors = [
    ".order-card",
    ".js-order-card",
    "[data-order-id]",
    ".a-box-group",
    ".a-box",
  ];

  const candidateNodes = [];
  for (const selector of candidateSelectors) {
    for (const el of qsa(selector)) {
      const text = textOf(el);
      if (text.length < 80) continue;
      if (!/(order placed|order date|order #|order id|ordered on|delivered|arriving|shipped|track package|view order details)/i.test(text)) {
        continue;
      }
      candidateNodes.push({ selector, el, text });
    }
  }

  const seen = new Set();
  const cards = [];
  for (const candidate of candidateNodes) {
    const fingerprint = candidate.text.slice(0, 500);
    if (seen.has(fingerprint)) continue;
    seen.add(fingerprint);
    cards.push(candidate);
  }

  const popovers = [];
  for (let cardIndex = 0; cardIndex < cards.length; cardIndex += 1) {
    const card = cards[cardIndex];
    const orderIds = unique((card.text.match(/\b\d{3}-\d{7}-\d{7}\b/g) || []));
    const shipToContainers = qsa(".yohtmlc-recipient, [id^='shipToInsertionNode-']", card.el)
      .filter((container) => /\bship\s+to\b|recipient address|shippingAddress/i.test(
        clean([
          container.innerText || container.textContent || "",
          container.id || "",
          container.getAttribute("data-a-popover") || "",
        ].join(" "))
      ));
    const controls = [];
    for (const container of shipToContainers) {
      const trigger = qsa(".a-popover-trigger, [data-action='a-popover'] a, a[href='javascript:void(0)'], button", container)
        .find((candidate) => visible(candidate));
      const declarative = qsa("[data-a-popover]", container)[0];
      let popoverName = "";
      if (declarative) {
        const popoverConfig = declarative.getAttribute("data-a-popover") || "";
        const nameMatch = popoverConfig.match(/"name"\s*:\s*"([^"]+)"/);
        if (nameMatch) popoverName = nameMatch[1];
      }
      const preload = (
        popoverName ? document.getElementById(`a-popover-${popoverName}`) : null
      ) || qsa(".a-popover-preload", container)[0];
      const preloadText = preload ? textOf(preload) : "";
      controls.push({ container, trigger, preloadText });
    }

    for (const control of controls.slice(0, 3)) {
      const triggerText = clean(control.trigger?.innerText || control.trigger?.textContent || control.trigger?.getAttribute("aria-label") || "");
      const preloadText = clean(control.preloadText || "");
      if (preloadText) {
        const labels = unique(linesOf(preloadText).filter((line) =>
          /(ship\s+to|shipping address|delivery address|address|deliver(?:ed)?\s+to|delaware|new castle|,\s*[A-Z]{2}\b|\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b|united states)/i.test(line)
        ));
        popovers.push({
          sourceCardIndex: cardIndex,
          sourceOrderIds: orderIds,
          triggerText,
          captureMethod: "preload",
          labels,
          rawText: preloadText,
          rawTextLength: preloadText.length,
        });
        continue;
      }
      if (!control.trigger) continue;
      const beforeUrl = window.location.href;
      try {
        control.trigger.scrollIntoView({ block: "center", inline: "center" });
        control.trigger.click();
      } catch {
        continue;
      }
      await sleep(500);
      if (window.location.href !== beforeUrl) {
        try { history.back(); } catch {}
        await sleep(500);
        continue;
      }
      const visiblePopoverTexts = qsa(".a-popover, .a-popover-wrapper, .a-popover-inner, [role='dialog'], .a-dropdown, .a-modal-scroller")
        .filter(visible)
        .map(textOf)
        .filter((text) => text && text !== triggerText)
        .filter((text) =>
          /(ship\s+to|shipping address|delivery address|address|deliver(?:ed)?\s+to|delaware|new castle|,\s*[A-Z]{2}\b|\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b)/i.test(text)
        );
      const rawText = unique(visiblePopoverTexts).join("\n\n");
      if (rawText) {
        const labels = unique(linesOf(rawText).filter((line) =>
          /(ship\s+to|shipping address|delivery address|address|deliver(?:ed)?\s+to|delaware|,\s*[A-Z]{2}\b|\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b)/i.test(line)
        ));
        popovers.push({
          sourceCardIndex: cardIndex,
          sourceOrderIds: orderIds,
          triggerText,
          captureMethod: "click",
          labels,
          rawText,
          rawTextLength: rawText.length,
        });
      }
      closePopovers();
      await sleep(150);
    }
  }

  return {
    capturedAtBrowserTime: new Date().toISOString(),
    url: window.location.href,
    title: document.title,
    popovers,
  };
}
"""


def default_profile_dir() -> Path:
    shared_profile = os.environ.get(SHARED_PROFILE_ENV)
    if shared_profile:
        return Path(shared_profile)
    return RUNTIME_ROOT / "private" / "browser-profiles" / "amazon-field-discovery"


DEFAULT_PROFILE_DIR = default_profile_dir()


def chrome_launch_blocked_by_macos_sandbox(
    env: Optional[Mapping[str, str]] = None,
    platform: Optional[str] = None,
) -> bool:
    values = os.environ if env is None else env
    current_platform = sys.platform if platform is None else platform
    return (
        current_platform == "darwin"
        and values.get(CODEX_MACOS_SANDBOX_ENV) == CODEX_MACOS_SANDBOX_VALUE
    )


def sandboxed_chrome_message() -> str:
    return (
        "Refusing to launch Chrome from the Codex macOS seatbelt sandbox "
        f"({CODEX_MACOS_SANDBOX_ENV}={CODEX_MACOS_SANDBOX_VALUE}). Chrome needs "
        "WindowServer and LaunchServices access during startup and can crash when "
        "spawned from that sandbox. Rerun this capture from a normal Terminal, "
        "through the macOS LaunchAgent, or via Codex's approved unsandboxed command path."
    )


def installed_chrome_major_version() -> str:
    info_plist = CHROME_PATH.parents[1] / "Info.plist"
    try:
        with info_plist.open("rb") as handle:
            version = str(plistlib.load(handle).get("CFBundleShortVersionString", ""))
    except (OSError, plistlib.InvalidFileException):
        return FALLBACK_HEADLESS_CHROME_MAJOR
    major = version.split(".", 1)[0]
    return major if major.isdigit() else FALLBACK_HEADLESS_CHROME_MAJOR


def headless_chrome_user_agent() -> str:
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{installed_chrome_major_version()}.0.0.0 Safari/537.36"
    )


def build_launch_options(headed: bool) -> Dict[str, Any]:
    options: Dict[str, Any] = {
        "headless": not headed,
        "viewport": {"width": 1440, "height": 1200},
        "accept_downloads": False,
    }
    if CHROME_PATH.exists():
        options["executable_path"] = str(CHROME_PATH)
    if not headed:
        options["user_agent"] = headless_chrome_user_agent()
        options["args"] = ["--disable-blink-features=AutomationControlled"]
    return options


def now_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


class RunLogger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")

    def event(self, event: str, **fields: Any) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
        }
        record.update(fields)
        self._handle.write(json.dumps(record, sort_keys=True, default=str))
        self._handle.write("\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


def preserve_text(value: str) -> str:
    return value


def preserve_url(value: str) -> str:
    return value


def preserve_links(links: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    preserved = []
    for link in links:
        preserved.append({
            "text": preserve_text(link.get("text", "")),
            "href": preserve_url(link.get("href", "")),
        })
    return preserved


def sanitize_ship_to_popover(raw: Dict[str, Any]) -> Dict[str, Any]:
    raw_text = str(raw.get("rawText", ""))
    return {
        "sourceCardIndex": raw.get("sourceCardIndex"),
        "sourcePageIndex": raw.get("sourcePageIndex"),
        "sourceUrl": preserve_url(str(raw.get("sourceUrl", ""))),
        "sourceOrderIds": [str(order_id) for order_id in raw.get("sourceOrderIds", []) if order_id],
        "triggerText": preserve_text(str(raw.get("triggerText", ""))),
        "captureMethod": preserve_text(str(raw.get("captureMethod", ""))),
        "labels": [preserve_text(str(label)) for label in raw.get("labels", [])],
        "rawTextLength": len(raw_text),
        "textPreview": raw_text[:1200],
        "rawText": raw_text,
    }


def extend_unique_dicts(existing: List[Dict[str, Any]], incoming: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = {json.dumps(value, sort_keys=True, default=str) for value in existing}
    for value in incoming:
        key = json.dumps(value, sort_keys=True, default=str)
        if key not in seen:
            existing.append(value)
            seen.add(key)
    return existing


def dedupe_ship_to_popovers(popovers: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    seen = set()
    for popover in popovers:
        key = (
            str(popover.get("captureMethod", "")),
            str(popover.get("triggerText", "")),
            str(popover.get("rawText", "")),
            tuple(str(value) for value in popover.get("sourceOrderIds", []) if value),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(popover)
    return result


def merge_safe_order_cards(cards: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    by_key: Dict[str, Dict[str, Any]] = {}
    for card in cards:
        order_ids = [str(value) for value in card.get("orderIds", []) if value]
        key = f"order:{order_ids[0]}" if order_ids else f"card:{card.get('index')}"
        existing = by_key.get(key)
        if existing is None:
            card["duplicateCandidateCount"] = 1
            by_key[key] = card
            merged.append(card)
            continue

        existing["duplicateCandidateCount"] = int(existing.get("duplicateCandidateCount", 1)) + 1
        existing["orderIds"] = list(dict.fromkeys(existing.get("orderIds", []) + order_ids))
        existing["orderIdCount"] = max(int(existing.get("orderIdCount", 0) or 0), int(card.get("orderIdCount", 0) or 0))
        existing["labels"] = list(dict.fromkeys(existing.get("labels", []) + card.get("labels", [])))
        existing["shipToPopovers"] = dedupe_ship_to_popovers(
            list(existing.get("shipToPopovers", [])) + list(card.get("shipToPopovers", []))
        )

        seen_links = {
            (link.get("text", ""), link.get("href", ""))
            for link in existing.get("links", [])
        }
        for link in card.get("links", []):
            link_key = (link.get("text", ""), link.get("href", ""))
            if link_key not in seen_links:
                existing.setdefault("links", []).append(link)
                seen_links.add(link_key)

        if int(card.get("rawTextLength", 0) or 0) > int(existing.get("rawTextLength", 0) or 0):
            existing["rawTextLength"] = card.get("rawTextLength", 0)
            existing["textPreview"] = card.get("textPreview", "")
            existing["rawText"] = card.get("rawText", "")
    return merged


def sanitize_discovery(raw: Dict[str, Any], include_sensitive_text: bool) -> Dict[str, Any]:
    cards = []
    for card in raw.get("cards", []):
        raw_text = card.get("rawText", "")
        ship_to_popovers = dedupe_ship_to_popovers(
            [sanitize_ship_to_popover(popover) for popover in card.get("shipToPopovers", [])],
        )
        ship_to_text = "\n\n".join(str(popover.get("rawText", "")) for popover in ship_to_popovers if popover.get("rawText"))
        combined_raw_text = f"{raw_text}\n\nSHIP TO DROPDOWN\n{ship_to_text}" if ship_to_text else raw_text
        combined_labels = [preserve_text(label) for label in card.get("labels", [])]
        for popover in ship_to_popovers:
            combined_labels.extend(popover.get("labels", []))
        safe_card = {
            "index": card.get("index"),
            "pageIndex": card.get("pageIndex"),
            "pageUrl": preserve_url(str(card.get("pageUrl", ""))),
            "sourceSelector": card.get("sourceSelector", ""),
            "tagName": card.get("tagName", ""),
            "className": card.get("className", ""),
            "dataOrderId": card.get("dataOrderId", ""),
            "orderIds": list(dict.fromkeys(str(order_id) for order_id in card.get("orderIds", []) if order_id)),
            "orderIdCount": len(card.get("orderIds", [])),
            "labels": list(dict.fromkeys(combined_labels)),
            "links": preserve_links(card.get("links", [])),
            "shipToPopovers": ship_to_popovers,
            "rawTextLength": len(combined_raw_text),
            "textPreview": combined_raw_text[:1200],
            "rawText": combined_raw_text,
        }
        cards.append(safe_card)

    raw_candidate_count = len(cards)
    cards = merge_safe_order_cards(cards)

    return {
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "capturedAtBrowserTime": raw.get("capturedAtBrowserTime"),
        "url": preserve_url(raw.get("url", "")),
        "title": raw.get("title", ""),
        "selectorCounts": raw.get("selectorCounts", {}),
        "auth": raw.get("auth", {}),
        "bodyTextLength": raw.get("bodyTextLength", 0),
        "cardCount": len(cards),
        "candidateCardCount": raw_candidate_count,
        "cards": cards,
    }


def sanitize_tracking_page(raw: Dict[str, Any], include_sensitive_text: bool) -> Dict[str, Any]:
    safe = {
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "capturedAtBrowserTime": raw.get("capturedAtBrowserTime"),
        "url": preserve_url(raw.get("url", "")),
        "title": raw.get("title", ""),
        "bodyTextLength": raw.get("bodyTextLength", 0),
        "labels": [preserve_text(label) for label in raw.get("labels", [])],
        "links": preserve_links(raw.get("links", [])),
        "textPreview": raw.get("rawText", "")[:1200],
        "rawText": raw.get("rawText", ""),
    }
    return safe


def sanitize_payment_discovery(raw: Dict[str, Any], include_sensitive_text: bool) -> Dict[str, Any]:
    records = []
    for record in raw.get("records", []):
        raw_text = record.get("rawText", "")
        safe_record = {
            "index": record.get("index"),
            "sourceSelector": record.get("sourceSelector", ""),
            "tagName": record.get("tagName", ""),
            "className": record.get("className", ""),
            "orderIds": list(dict.fromkeys(str(order_id) for order_id in record.get("orderIds", []) if order_id)),
            "orderIdCount": len(record.get("orderIds", [])),
            "labels": [preserve_text(label) for label in record.get("labels", [])],
            "links": preserve_links(record.get("links", [])),
            "rawTextLength": len(raw_text),
            "textPreview": raw_text[:1200],
            "rawText": raw_text,
        }
        records.append(safe_record)

    return {
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "capturedAtBrowserTime": raw.get("capturedAtBrowserTime"),
        "url": preserve_url(raw.get("url", "")),
        "title": raw.get("title", ""),
        "selectorCounts": raw.get("selectorCounts", {}),
        "auth": raw.get("auth", {}),
        "bodyTextLength": raw.get("bodyTextLength", 0),
        "recordCount": len(records),
        "records": records,
    }


def inspect_auth_state(page: Any) -> Dict[str, Any]:
    return page.evaluate(AUTH_STATE_JS)


def readiness_from_state(state: Dict[str, Any]) -> str:
    if state.get("unsupportedSurface"):
        return "unsupported"
    if state.get("blocked"):
        return "blocked"
    if state.get("hasOrders") and not state.get("hasSignIn"):
        return "orders"
    if state.get("hasSignIn"):
        return "login_required"
    return "unknown"


def wait_for_orders_or_login(page: Any, timeout_seconds: int, stop_on_security_prompt: bool) -> str:
    deadline = datetime.now().timestamp() + timeout_seconds
    while datetime.now().timestamp() < deadline:
        try:
            state = inspect_auth_state(page)
        except PlaywrightError:
            page.wait_for_timeout(1000)
            continue
        if state.get("blocked") and stop_on_security_prompt:
            return "blocked"
        if state.get("unsupportedSurface"):
            return "unsupported"
        if state.get("hasOrders") and not state.get("hasSignIn"):
            return "orders"
        page.wait_for_timeout(1000)
    return "timeout"


def probe_blocker_from_state(state: Dict[str, Any]) -> str:
    if state.get("blocked"):
        return "blocked"
    if state.get("unsupportedSurface"):
        return "unsupported"
    if state.get("hasSignIn"):
        return "login_required"
    return ""


def order_ids_from_link(link: Dict[str, str]) -> List[str]:
    blob = f"{link.get('text', '')} {link.get('href', '')}"
    return list(dict.fromkeys(ORDER_ID_RE.findall(blob)))


def is_tracking_link(link: Dict[str, str]) -> bool:
    text = str(link.get("text", "")).lower()
    href = str(link.get("href", ""))
    href_lower = href.lower()
    if not href:
        return False
    if "/dp/" in href_lower or "/gp/product/" in href_lower:
        return False
    return (
        "ship-track" in href_lower
        or "progress-tracker" in href_lower
        or "track-package" in href_lower
        or "track package" in text
        or "tracking" in text
    )


def is_order_detail_link(link: Dict[str, str]) -> bool:
    text = str(link.get("text", "")).lower()
    href = str(link.get("href", ""))
    href_lower = href.lower()
    if not href:
        return False
    is_document_link = any(token in href_lower for token in ("invoice", "summary/print", "documents/download"))
    if is_document_link or is_tracking_link(link):
        return False
    return (
        "order details" in text
        or "view order" in text
        or "order-details" in href_lower
        or "/gp/your-account/order-details" in href_lower
    )


def probe_metadata(card: Dict[str, Any], link: Dict[str, str]) -> Dict[str, Any]:
    order_ids = list(dict.fromkeys(
        [str(value) for value in card.get("orderIds", []) if value]
        + order_ids_from_link(link)
    ))
    return {
        "href": link.get("href", ""),
        "text": link.get("text", ""),
        "sourcePageIndex": card.get("pageIndex"),
        "sourceUrl": card.get("pageUrl", ""),
        "sourceOrderIds": order_ids,
        "sourceCardIndex": card.get("index"),
    }


def tracking_probe_links_from_capture(capture: Dict[str, Any], max_links: int) -> List[Dict[str, Any]]:
    links: List[Dict[str, Any]] = []
    seen = set()
    for card in capture.get("cards", []):
        for link in card.get("links", []):
            href = link.get("href", "")
            if not href:
                continue
            if is_tracking_link(link) and href not in seen:
                seen.add(href)
                links.append(probe_metadata(card, link))
            if len(links) >= max_links:
                return links
    return links


def tracking_links_from_capture(capture: Dict[str, Any], max_links: int) -> List[str]:
    return [link["href"] for link in tracking_probe_links_from_capture(capture, max_links)]


def order_detail_probe_links_from_capture(capture: Dict[str, Any], max_links: int) -> List[Dict[str, Any]]:
    links: List[Dict[str, Any]] = []
    seen_order_ids = set()
    seen_hrefs = set()
    for card in capture.get("cards", []):
        for link in card.get("links", []):
            href = link.get("href", "")
            if not href:
                continue
            if not is_order_detail_link(link):
                continue
            order_ids = order_ids_from_link(link) or [str(value) for value in card.get("orderIds", []) if value]
            order_key = order_ids[0] if order_ids else ""
            if order_key:
                if order_key in seen_order_ids:
                    continue
                seen_order_ids.add(order_key)
            elif href in seen_hrefs:
                continue
            seen_hrefs.add(href)
            links.append(probe_metadata(card, link))
            if len(links) >= max_links:
                return links
    return links


def order_detail_links_from_capture(capture: Dict[str, Any], max_links: int) -> List[str]:
    return [link["href"] for link in order_detail_probe_links_from_capture(capture, max_links)]


def attach_card_ship_to_popovers(raw_page_capture: Dict[str, Any], raw_ship_to_capture: Dict[str, Any]) -> None:
    cards = raw_page_capture.get("cards", [])
    for popover in raw_ship_to_capture.get("popovers", []):
        try:
            source_index = int(popover.get("sourceCardIndex"))
        except (TypeError, ValueError):
            continue
        if 0 <= source_index < len(cards):
            cards[source_index].setdefault("shipToPopovers", []).append(popover)


def tracking_probe_links_from_record(record: Dict[str, Any], source_probe: Dict[str, Any]) -> List[Dict[str, Any]]:
    links = []
    seen = set()
    source_order_ids = [str(value) for value in source_probe.get("sourceOrderIds", []) if value]
    for link in record.get("links", []):
        href = str(link.get("href", ""))
        if not href or href in seen or not is_tracking_link(link):
            continue
        seen.add(href)
        probe = {
            "href": href,
            "text": str(link.get("text", "")),
            "sourcePageIndex": source_probe.get("sourcePageIndex"),
            "sourceUrl": source_probe.get("sourceUrl", ""),
            "sourceOrderIds": list(dict.fromkeys(source_order_ids + order_ids_from_link(link))),
            "sourceCardIndex": source_probe.get("sourceCardIndex"),
        }
        links.append(probe)
    return links


def order_probe_groups_from_capture(capture: Dict[str, Any]) -> List[Dict[str, Any]]:
    groups: List[Dict[str, Any]] = []
    by_key: Dict[str, Dict[str, Any]] = {}
    for card in capture.get("cards", []):
        order_ids = [str(value) for value in card.get("orderIds", []) if value]
        key = order_ids[0] if order_ids else f"card:{card.get('index')}"
        group = by_key.get(key)
        if group is None:
            group = {
                "orderKey": key,
                "sourceOrderIds": order_ids,
                "sourcePageIndex": card.get("pageIndex"),
                "sourceUrl": card.get("pageUrl", ""),
                "sourceCardIndex": card.get("index"),
                "orderDetailLinks": [],
                "trackingLinks": [],
            }
            by_key[key] = group
            groups.append(group)
        else:
            group["sourceOrderIds"] = list(dict.fromkeys(group.get("sourceOrderIds", []) + order_ids))

        for link in card.get("links", []):
            if is_order_detail_link(link):
                group["orderDetailLinks"].append(probe_metadata(card, link))
            elif is_tracking_link(link):
                group["trackingLinks"].append(probe_metadata(card, link))

    for group in groups:
        deduped_details = []
        seen_detail_orders = set()
        seen_detail_hrefs = set()
        for probe in group.get("orderDetailLinks", []):
            href = str(probe.get("href", ""))
            order_ids = [str(value) for value in probe.get("sourceOrderIds", []) if value]
            detail_key = order_ids[0] if order_ids else href
            if detail_key in seen_detail_orders or href in seen_detail_hrefs:
                continue
            seen_detail_orders.add(detail_key)
            seen_detail_hrefs.add(href)
            deduped_details.append(probe)
        group["orderDetailLinks"] = deduped_details

        deduped_tracking = []
        seen_tracking_hrefs = set()
        for probe in group.get("trackingLinks", []):
            href = str(probe.get("href", ""))
            if not href or href in seen_tracking_hrefs:
                continue
            seen_tracking_hrefs.add(href)
            deduped_tracking.append(probe)
        group["trackingLinks"] = deduped_tracking
    return groups


def is_bookkeeping_orders_run(args: argparse.Namespace) -> bool:
    return bool(
        args.target == "orders"
        and (
            args.lookback_days is not None
            or getattr(args, "max_pages_auto", False)
            or (args.max_pages or 1) > 1
            or args.max_orders >= BOOKKEEPING_AUTO_ORDER_THRESHOLD
        )
    )


def normalize_probe_limit(value: Optional[int], parser: argparse.ArgumentParser, flag_name: str) -> Optional[int]:
    if value is None:
        return None
    if value < 0:
        parser.error(f"{flag_name} must be non-negative")
    return value


def normalize_max_pages(value: Optional[str], parser: argparse.ArgumentParser) -> tuple[Optional[int], bool]:
    if value is None:
        return None, False
    normalized = str(value).strip().lower()
    if normalized == "auto":
        return None, True
    try:
        pages = int(normalized)
    except ValueError:
        parser.error("--max-pages must be a positive integer or 'auto'")
    if pages <= 0:
        parser.error("--max-pages must be positive")
    return pages, False


def apply_bookkeeping_probe_defaults(args: argparse.Namespace) -> argparse.Namespace:
    bookkeeping_auto = is_bookkeeping_orders_run(args)
    args.bookkeeping_auto_probe_mode = bookkeeping_auto
    args.capture_tracking_pages_auto_enabled = False
    args.capture_order_detail_pages_auto_enabled = False
    if bookkeeping_auto:
        if not args.capture_tracking_pages:
            args.capture_tracking_pages = True
            args.capture_tracking_pages_auto_enabled = True
        if not args.capture_order_detail_pages:
            args.capture_order_detail_pages = True
            args.capture_order_detail_pages_auto_enabled = True
    else:
        if args.max_tracking_links is None:
            args.max_tracking_links = DEFAULT_INTERACTIVE_PROBE_LIMIT
        if args.max_order_detail_links is None:
            args.max_order_detail_links = DEFAULT_INTERACTIVE_PROBE_LIMIT
    return args


def probe_limit_for_log(value: Optional[int]) -> object:
    return "auto" if value is None else value


def page_limit_for_log(args: argparse.Namespace) -> object:
    return "auto" if getattr(args, "max_pages_auto", False) else args.max_pages


def under_probe_limit(count: int, limit: Optional[int]) -> bool:
    return limit is None or count < limit


def reached_probe_limit(count: int, limit: Optional[int]) -> bool:
    return limit is not None and count >= limit


def click_or_goto(page: Any, href: str, min_pause_ms: int, max_pause_ms: int, fixed_settle_ms: int) -> str:
    before_url = page.url
    try:
        clicked = bool(page.evaluate(CLICK_LINK_BY_HREF_JS, href))
    except PlaywrightError:
        clicked = False
    if clicked:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except PlaywrightTimeoutError:
            pass
        variable_pause(page, min_pause_ms, max_pause_ms, fixed_settle_ms)
        if page.url != before_url:
            return "click"
    try:
        page.goto(href, wait_until="domcontentloaded", timeout=60000)
    except PlaywrightError:
        if clicked:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except PlaywrightTimeoutError:
                pass
            variable_pause(page, min_pause_ms, max_pause_ms, fixed_settle_ms)
            if page.url != before_url:
                return "click_navigation_in_progress"
        raise
    variable_pause(page, min_pause_ms, max_pause_ms, fixed_settle_ms)
    return "goto_after_missing_click" if not clicked else "goto_after_click_no_navigation"


def navigate_to_order_history(page: Any, url: str, timeout_seconds: int, stop_on_security_prompt: bool) -> str:
    if not url:
        return "missing_source_url"
    if page.url != url:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except PlaywrightTimeoutError:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except PlaywrightTimeoutError:
                pass
            readiness = wait_for_orders_or_login(page, min(timeout_seconds, 30), stop_on_security_prompt)
            if readiness == "orders":
                return readiness
            return "navigation_timeout"
    return wait_for_orders_or_login(page, min(timeout_seconds, 30), stop_on_security_prompt)


def capture_probe_pages(
    page: Any,
    probe_links: Iterable[Dict[str, Any]],
    probe_kind: str,
    min_pause_ms: int,
    max_pause_ms: int,
    fixed_settle_ms: int,
    include_sensitive_text: bool,
    wait_seconds: int,
    stop_on_security_prompt: bool,
    logger: RunLogger | None = None,
) -> List[Dict[str, Any]]:
    captures: List[Dict[str, Any]] = []
    for index, probe in enumerate(probe_links, start=1):
        href = str(probe.get("href", ""))
        source_url = str(probe.get("sourceUrl", ""))
        if not href:
            continue
        if logger:
            logger.event(
                "probe_start",
                probeKind=probe_kind,
                probeIndex=index,
                href=href,
                sourceUrl=source_url,
                sourcePageIndex=probe.get("sourcePageIndex"),
                sourceOrderIds=probe.get("sourceOrderIds", []),
            )

        source_readiness = navigate_to_order_history(page, source_url, wait_seconds, stop_on_security_prompt)
        if logger:
            logger.event(
                "probe_source_readiness",
                probeKind=probe_kind,
                probeIndex=index,
                readiness=source_readiness,
                pageUrl=page.url,
            )
        if source_readiness != "orders":
            captures.append({
                "url": preserve_url(href),
                "sourceUrl": preserve_url(source_url),
                "sourcePageIndex": probe.get("sourcePageIndex"),
                "sourceOrderIds": probe.get("sourceOrderIds", []),
                "error": f"source_order_history_not_ready:{source_readiness}",
            })
            if logger:
                logger.event(
                    "probe_error",
                    probeKind=probe_kind,
                    probeIndex=index,
                    error=f"source_order_history_not_ready:{source_readiness}",
                    url=href,
                )
            if source_readiness in {"blocked", "login_required", "unsupported"}:
                break
            continue

        try:
            navigation_method = click_or_goto(page, href, min_pause_ms, max_pause_ms, fixed_settle_ms)
            if logger:
                logger.event(
                    "probe_navigated",
                    probeKind=probe_kind,
                    probeIndex=index,
                    navigationMethod=navigation_method,
                    pageUrl=page.url,
                )
            state = inspect_auth_state(page)
            blocker = probe_blocker_from_state(state)
            if blocker:
                captures.append({
                    "url": preserve_url(page.url or href),
                    "sourceUrl": preserve_url(source_url),
                    "sourcePageIndex": probe.get("sourcePageIndex"),
                    "sourceOrderIds": probe.get("sourceOrderIds", []),
                    "navigationMethod": navigation_method,
                    "error": blocker,
                })
                if logger:
                    logger.event(
                        "probe_error",
                        probeKind=probe_kind,
                        probeIndex=index,
                        error=blocker,
                        url=page.url or href,
                    )
                if blocker in {"blocked", "login_required", "unsupported"}:
                    break
            else:
                raw_probe = page.evaluate(TRACKING_PAGE_JS)
                safe_probe = sanitize_tracking_page(raw_probe, include_sensitive_text)
                safe_probe["sourceUrl"] = preserve_url(source_url)
                safe_probe["sourcePageIndex"] = probe.get("sourcePageIndex")
                safe_probe["sourceOrderIds"] = probe.get("sourceOrderIds", [])
                safe_probe["sourceCardIndex"] = probe.get("sourceCardIndex")
                safe_probe["navigationMethod"] = navigation_method
                captures.append(safe_probe)
                if logger:
                    logger.event(
                        "probe_capture",
                        probeKind=probe_kind,
                        probeIndex=index,
                        url=safe_probe.get("url", ""),
                        title=safe_probe.get("title", ""),
                        bodyTextLength=safe_probe.get("bodyTextLength", 0),
                        labelCount=len(safe_probe.get("labels", [])),
                    )
        except PlaywrightTimeoutError:
            captures.append({
                "url": preserve_url(href),
                "sourceUrl": preserve_url(source_url),
                "sourcePageIndex": probe.get("sourcePageIndex"),
                "sourceOrderIds": probe.get("sourceOrderIds", []),
                "error": "timeout",
            })
            if logger:
                logger.event("probe_error", probeKind=probe_kind, probeIndex=index, error="timeout", url=href)
        except PlaywrightError as exc:
            error = f"playwright_error:{str(exc).splitlines()[0][:240]}"
            captures.append({
                "url": preserve_url(page.url or href),
                "sourceUrl": preserve_url(source_url),
                "sourcePageIndex": probe.get("sourcePageIndex"),
                "sourceOrderIds": probe.get("sourceOrderIds", []),
                "error": error,
            })
            if logger:
                logger.event("probe_error", probeKind=probe_kind, probeIndex=index, error=error, url=page.url or href)
        finally:
            if source_url:
                try:
                    navigate_to_order_history(page, source_url, wait_seconds, stop_on_security_prompt)
                    settle_after_readiness(
                        page,
                        "orders",
                        min_pause_ms,
                        max_pause_ms,
                        fixed_settle_ms,
                        scroll_before_capture=False,
                    )
                    if logger:
                        logger.event(
                            "probe_returned_to_source",
                            probeKind=probe_kind,
                            probeIndex=index,
                            sourceUrl=source_url,
                            pageUrl=page.url,
                        )
                except (PlaywrightError, PlaywrightTimeoutError):
                    if logger:
                        logger.event(
                            "probe_return_to_source_error",
                            probeKind=probe_kind,
                            probeIndex=index,
                            sourceUrl=source_url,
                        )
                    pass
    return captures


def capture_one_probe_page(
    page: Any,
    probe: Dict[str, Any],
    probe_kind: str,
    probe_index: int,
    min_pause_ms: int,
    max_pause_ms: int,
    fixed_settle_ms: int,
    include_sensitive_text: bool,
    logger: RunLogger | None = None,
) -> tuple[Dict[str, Any], str]:
    href = str(probe.get("href", ""))
    try:
        navigation_method = click_or_goto(page, href, min_pause_ms, max_pause_ms, fixed_settle_ms)
        if logger:
            logger.event(
                "probe_navigated",
                probeKind=probe_kind,
                probeIndex=probe_index,
                navigationMethod=navigation_method,
                pageUrl=page.url,
            )
        state = inspect_auth_state(page)
        blocker = probe_blocker_from_state(state)
        if blocker:
            return (
                {
                    "url": preserve_url(page.url or href),
                    "sourceUrl": preserve_url(str(probe.get("sourceUrl", ""))),
                    "sourcePageIndex": probe.get("sourcePageIndex"),
                    "sourceOrderIds": probe.get("sourceOrderIds", []),
                    "sourceCardIndex": probe.get("sourceCardIndex"),
                    "navigationMethod": navigation_method,
                    "error": blocker,
                },
                blocker,
            )

        raw_probe = page.evaluate(TRACKING_PAGE_JS)
        safe_probe = sanitize_tracking_page(raw_probe, include_sensitive_text)
        safe_probe["sourceUrl"] = preserve_url(str(probe.get("sourceUrl", "")))
        safe_probe["sourcePageIndex"] = probe.get("sourcePageIndex")
        safe_probe["sourceOrderIds"] = probe.get("sourceOrderIds", [])
        safe_probe["sourceCardIndex"] = probe.get("sourceCardIndex")
        safe_probe["navigationMethod"] = navigation_method
        return safe_probe, ""
    except PlaywrightTimeoutError:
        return (
            {
                "url": preserve_url(href),
                "sourceUrl": preserve_url(str(probe.get("sourceUrl", ""))),
                "sourcePageIndex": probe.get("sourcePageIndex"),
                "sourceOrderIds": probe.get("sourceOrderIds", []),
                "sourceCardIndex": probe.get("sourceCardIndex"),
                "error": "timeout",
            },
            "timeout",
        )
    except PlaywrightError as exc:
        error = f"playwright_error:{str(exc).splitlines()[0][:240]}"
        return (
            {
                "url": preserve_url(page.url or href),
                "sourceUrl": preserve_url(str(probe.get("sourceUrl", ""))),
                "sourcePageIndex": probe.get("sourcePageIndex"),
                "sourceOrderIds": probe.get("sourceOrderIds", []),
                "sourceCardIndex": probe.get("sourceCardIndex"),
                "error": error,
            },
            error,
        )


def capture_order_probe_pages(
    page: Any,
    order_groups: Iterable[Dict[str, Any]],
    capture_tracking_pages: bool,
    max_tracking_links: Optional[int],
    capture_order_detail_pages: bool,
    max_order_detail_links: Optional[int],
    min_pause_ms: int,
    max_pause_ms: int,
    fixed_settle_ms: int,
    include_sensitive_text: bool,
    wait_seconds: int,
    stop_on_security_prompt: bool,
    logger: RunLogger | None = None,
) -> Dict[str, Any]:
    tracking_pages: List[Dict[str, Any]] = []
    order_detail_pages: List[Dict[str, Any]] = []
    order_probe_bundles: List[Dict[str, Any]] = []
    seen_tracking_hrefs = set()
    seen_detail_hrefs = set()
    detail_count = 0
    tracking_count = 0
    order_detail_probe_truncated = False
    tracking_probe_truncated = False

    for order_index, group in enumerate(order_groups, start=1):
        source_url = str(group.get("sourceUrl", ""))
        if not source_url:
            continue
        if logger:
            logger.event(
                "order_probe_start",
                orderProbeIndex=order_index,
                sourceOrderIds=group.get("sourceOrderIds", []),
                sourceUrl=source_url,
                sourceCardIndex=group.get("sourceCardIndex"),
            )

        source_readiness = navigate_to_order_history(page, source_url, wait_seconds, stop_on_security_prompt)
        if logger:
            logger.event(
                "order_probe_source_readiness",
                orderProbeIndex=order_index,
                readiness=source_readiness,
                pageUrl=page.url,
            )

        bundle = {
            "orderProbeIndex": order_index,
            "sourceOrderIds": group.get("sourceOrderIds", []),
            "sourcePageIndex": group.get("sourcePageIndex"),
            "sourceUrl": preserve_url(source_url),
            "sourceCardIndex": group.get("sourceCardIndex"),
            "orderDetailPageIndexes": [],
            "trackingPageIndexes": [],
            "errors": [],
        }
        if source_readiness != "orders":
            error = f"source_order_history_not_ready:{source_readiness}"
            bundle["errors"].append(error)
            order_probe_bundles.append(bundle)
            if logger:
                logger.event("order_probe_error", orderProbeIndex=order_index, error=error, sourceUrl=source_url)
            if source_readiness in {"blocked", "login_required", "unsupported"}:
                break
            continue

        tracking_candidates: List[Dict[str, Any]] = list(group.get("trackingLinks", []))
        if capture_order_detail_pages and not under_probe_limit(detail_count, max_order_detail_links):
            if group.get("orderDetailLinks"):
                order_detail_probe_truncated = True
        if capture_order_detail_pages and under_probe_limit(detail_count, max_order_detail_links):
            for detail_probe in group.get("orderDetailLinks", []):
                href = str(detail_probe.get("href", ""))
                if not href or href in seen_detail_hrefs:
                    continue
                if reached_probe_limit(detail_count, max_order_detail_links):
                    order_detail_probe_truncated = True
                    break
                seen_detail_hrefs.add(href)
                detail_count += 1
                if logger:
                    logger.event(
                        "probe_start",
                        probeKind="order_detail",
                        probeIndex=detail_count,
                        href=href,
                        sourceUrl=source_url,
                        sourcePageIndex=detail_probe.get("sourcePageIndex"),
                        sourceOrderIds=detail_probe.get("sourceOrderIds", []),
                    )
                detail_page, status = capture_one_probe_page(
                    page,
                    detail_probe,
                    "order_detail",
                    detail_count,
                    min_pause_ms,
                    max_pause_ms,
                    fixed_settle_ms,
                    include_sensitive_text,
                    logger=logger,
                )
                detail_page_index = len(order_detail_pages)
                order_detail_pages.append(detail_page)
                bundle["orderDetailPageIndexes"].append(detail_page_index)
                if status:
                    bundle["errors"].append(f"order_detail:{status}")
                    if logger:
                        logger.event("probe_error", probeKind="order_detail", probeIndex=detail_count, error=status, url=detail_page.get("url", href))
                    if status in {"blocked", "login_required", "unsupported"}:
                        order_probe_bundles.append(bundle)
                        return {
                            "trackingPages": tracking_pages,
                            "orderDetailPages": order_detail_pages,
                            "orderProbeBundles": order_probe_bundles,
                            "trackingProbeTruncated": tracking_probe_truncated,
                            "orderDetailProbeTruncated": order_detail_probe_truncated,
                            "trackingProbeCount": tracking_count,
                            "orderDetailProbeCount": detail_count,
                        }
                else:
                    if logger:
                        logger.event(
                            "probe_capture",
                            probeKind="order_detail",
                            probeIndex=detail_count,
                            url=detail_page.get("url", ""),
                            title=detail_page.get("title", ""),
                            bodyTextLength=detail_page.get("bodyTextLength", 0),
                            labelCount=len(detail_page.get("labels", [])),
                        )
                    if capture_tracking_pages:
                        tracking_candidates.extend(tracking_probe_links_from_record(detail_page, detail_probe))

        if capture_tracking_pages and not under_probe_limit(tracking_count, max_tracking_links):
            if any(str(probe.get("href", "")) and str(probe.get("href", "")) not in seen_tracking_hrefs for probe in tracking_candidates):
                tracking_probe_truncated = True
        if capture_tracking_pages and under_probe_limit(tracking_count, max_tracking_links):
            for tracking_probe in tracking_candidates:
                href = str(tracking_probe.get("href", ""))
                if not href or href in seen_tracking_hrefs:
                    continue
                if reached_probe_limit(tracking_count, max_tracking_links):
                    tracking_probe_truncated = True
                    break
                seen_tracking_hrefs.add(href)
                tracking_count += 1
                if logger:
                    logger.event(
                        "probe_start",
                        probeKind="tracking",
                        probeIndex=tracking_count,
                        href=href,
                        sourceUrl=source_url,
                        sourcePageIndex=tracking_probe.get("sourcePageIndex"),
                        sourceOrderIds=tracking_probe.get("sourceOrderIds", []),
                    )
                tracking_page, status = capture_one_probe_page(
                    page,
                    tracking_probe,
                    "tracking",
                    tracking_count,
                    min_pause_ms,
                    max_pause_ms,
                    fixed_settle_ms,
                    include_sensitive_text,
                    logger=logger,
                )
                tracking_page_index = len(tracking_pages)
                tracking_pages.append(tracking_page)
                bundle["trackingPageIndexes"].append(tracking_page_index)
                if status:
                    bundle["errors"].append(f"tracking:{status}")
                    if logger:
                        logger.event("probe_error", probeKind="tracking", probeIndex=tracking_count, error=status, url=tracking_page.get("url", href))
                    if status in {"blocked", "login_required", "unsupported"}:
                        order_probe_bundles.append(bundle)
                        return {
                            "trackingPages": tracking_pages,
                            "orderDetailPages": order_detail_pages,
                            "orderProbeBundles": order_probe_bundles,
                            "trackingProbeTruncated": tracking_probe_truncated,
                            "orderDetailProbeTruncated": order_detail_probe_truncated,
                            "trackingProbeCount": tracking_count,
                            "orderDetailProbeCount": detail_count,
                        }
                elif logger:
                    logger.event(
                        "probe_capture",
                        probeKind="tracking",
                        probeIndex=tracking_count,
                        url=tracking_page.get("url", ""),
                        title=tracking_page.get("title", ""),
                        bodyTextLength=tracking_page.get("bodyTextLength", 0),
                        labelCount=len(tracking_page.get("labels", [])),
                    )

        order_probe_bundles.append(bundle)
        if logger:
            logger.event(
                "order_probe_done",
                orderProbeIndex=order_index,
                orderDetailPageIndexes=bundle["orderDetailPageIndexes"],
                trackingPageIndexes=bundle["trackingPageIndexes"],
                errors=bundle["errors"],
            )

    return {
        "trackingPages": tracking_pages,
        "orderDetailPages": order_detail_pages,
        "orderProbeBundles": order_probe_bundles,
        "trackingProbeTruncated": tracking_probe_truncated,
        "orderDetailProbeTruncated": order_detail_probe_truncated,
        "trackingProbeCount": tracking_count,
        "orderDetailProbeCount": detail_count,
    }


def start_index_from_url(url: str) -> int:
    try:
        parts = urlsplit(url)
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if key == "startIndex":
                return int(value)
    except ValueError:
        return 0
    return 0


def next_order_page_url(current_url: str, links: Iterable[Dict[str, str]], seen_urls: Iterable[str]) -> str:
    links = list(links)
    if any(link.get('disabled') and 'next' in str(link.get('text', '')).lower() for link in links):
        return ""
    seen = {urlsplit(url).path + "?" + urlsplit(url).query for url in seen_urls if url}
    current_start = start_index_from_url(current_url)
    candidates = []
    for link in links:
        href = link.get("href", "")
        if not href:
            continue
        absolute = urljoin(current_url, href)
        parts = urlsplit(absolute)
        key = parts.path + "?" + parts.query
        if key in seen:
            continue
        if "/your-orders/orders" not in parts.path:
            continue
        start_index = start_index_from_url(absolute)
        if start_index <= current_start:
            continue
        text = link.get("text", "")
        candidates.append((0 if "next" in text.lower() else 1, start_index, absolute))
    if not candidates:
        parts = urlsplit(current_url)
        if "/your-orders/orders" in parts.path:
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            if query.get("timeFilter"):
                query["startIndex"] = str(current_start + 10)
                query.pop("ref_", None)
                return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
        return ""
    return sorted(candidates)[0][2]


def combine_selector_counts(captures: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    totals: Dict[str, int] = {}
    for capture in captures:
        for key, value in capture.get("selectorCounts", {}).items():
            if isinstance(value, int):
                totals[key] = totals.get(key, 0) + value
    return totals


def orders_url_for_time_filter(time_filter: str, start_index: int = 0) -> str:
    query = {"timeFilter": time_filter}
    if start_index > 0:
        query["startIndex"] = str(start_index)
    return urlunsplit(("https", "www.amazon.com", ORDERS_URL_PATH, urlencode(query), ""))


def time_filter_from_orders_url(url: str) -> str:
    for key, value in parse_qsl(urlsplit(url).query):
        if key == "timeFilter":
            return value
    return ""


def time_filter_for_lookback_days(days: int, today: date | None = None) -> str:
    # Our cutoff includes the date exactly N days ago. Amazon's last30 window
    # can exclude that boundary, so use a wider source window at 30 days and
    # stop on the locally verified order-date cutoff.
    if days < 30:
        return "last30"
    if days <= 90:
        return "months-3"
    current = today or datetime.now(timezone.utc).date()
    return f"year-{current.year}"


def parse_order_placed_date(text: str, today: date | None = None) -> Optional[date]:
    match = ORDER_PLACED_RE.search(text or "")
    if not match:
        return None
    raw = re.sub(r"\s+", " ", match.group(1).replace(".", "")).strip()
    current = today or datetime.now(timezone.utc).date()
    candidates = [raw]
    if not re.search(r"\b\d{4}\b", raw):
        candidates.append(f"{raw}, {current.year}")
    for candidate in candidates:
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
            try:
                parsed = datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
            if parsed > current + timedelta(days=7):
                parsed = parsed.replace(year=parsed.year - 1)
            return parsed
    return None


def oldest_order_date_from_page_capture(capture: Dict[str, Any], today: date | None = None) -> Optional[date]:
    dates: List[date] = []
    for card in capture.get("cards", []):
        texts = [str(card.get("rawText", "")), str(card.get("textPreview", ""))]
        texts.extend(str(label) for label in card.get("labels", []) if label)
        for text in texts:
            parsed = parse_order_placed_date(text, today=today)
            if parsed:
                dates.append(parsed)
                break
    return min(dates) if dates else None


def page_reaches_lookback_cutoff(capture: Dict[str, Any], lookback_days: Optional[int], today: date | None = None) -> bool:
    if not lookback_days:
        return False
    current = today or datetime.now(timezone.utc).date()
    cutoff = current - timedelta(days=lookback_days)
    oldest = oldest_order_date_from_page_capture(capture, today=current)
    return bool(oldest and oldest <= cutoff)


def build_order_history_coverage(
    *,
    lookback_days: Optional[int],
    time_filter: str,
    max_pages: object,
    max_orders: int,
    raw_captures: List[Dict[str, Any]],
    card_count: int,
    card_count_returned: int,
    stop_reason: str,
    stop_detail: Optional[Dict[str, Any]] = None,
    today: date | None = None,
) -> Dict[str, Any]:
    current = today or datetime.now(timezone.utc).date()
    oldest_dates = [
        parsed
        for capture in raw_captures
        for parsed in [oldest_order_date_from_page_capture(capture, today=current)]
        if parsed
    ]
    cutoff = current - timedelta(days=lookback_days) if lookback_days else None
    truncated_by_max_orders = card_count_returned < card_count
    incomplete_stop_reasons = {
        "max_pages_reached",
        "pagination_links_error",
        "pagination_readiness_not_orders",
        "empty_page_without_end_marker",
    }
    complete = not truncated_by_max_orders and stop_reason not in incomplete_stop_reasons
    return {
        "requestedLookbackDays": lookback_days,
        "timeFilter": time_filter,
        "coverageCutoffDate": cutoff.isoformat() if cutoff else "",
        "oldestOrderDateReached": min(oldest_dates).isoformat() if oldest_dates else "",
        "pagesScanned": len(raw_captures),
        "maxPages": max_pages,
        "maxOrders": max_orders,
        "cardCount": card_count,
        "cardCountReturned": card_count_returned,
        "truncatedByMaxOrders": truncated_by_max_orders,
        "stopReason": stop_reason or "unknown",
        "stopDetail": stop_detail or {},
        "complete": complete,
    }


def normalize_pause_bounds(min_pause_ms: int, max_pause_ms: int) -> tuple[int, int]:
    lower = max(0, min_pause_ms)
    upper = max(0, max_pause_ms)
    if upper < lower:
        upper = lower
    return lower, upper


def variable_pause(page: Any, min_pause_ms: int, max_pause_ms: int, fixed_settle_ms: int = 0) -> None:
    if fixed_settle_ms > 0:
        page.wait_for_timeout(fixed_settle_ms)
        return
    lower, upper = normalize_pause_bounds(min_pause_ms, max_pause_ms)
    if upper <= 0:
        return
    page.wait_for_timeout(random.randint(lower, upper))


def wait_for_capture_selector(page: Any, target: str, timeout_ms: int = 5000) -> None:
    selector = ORDER_READY_SELECTOR if target == "orders" else PAYMENT_READY_SELECTOR
    try:
        page.wait_for_selector(selector, timeout=timeout_ms)
    except PlaywrightTimeoutError:
        pass


def scroll_like_reader(page: Any, min_pause_ms: int, max_pause_ms: int) -> None:
    try:
        scroll_height = int(page.evaluate("() => document.documentElement.scrollHeight || document.body.scrollHeight || 0") or 0)
        viewport_height = int(page.evaluate("() => window.innerHeight || 0") or 0)
    except PlaywrightError:
        return
    if scroll_height <= viewport_height + 200:
        return
    page.mouse.wheel(0, random.randint(280, 860))
    variable_pause(page, min_pause_ms, max_pause_ms)
    if random.random() < 0.55:
        page.mouse.wheel(0, -random.randint(80, 260))
        variable_pause(page, min_pause_ms // 2, max_pause_ms // 2)


def settle_after_readiness(
    page: Any,
    target: str,
    min_pause_ms: int,
    max_pause_ms: int,
    fixed_settle_ms: int,
    scroll_before_capture: bool,
) -> None:
    wait_for_capture_selector(page, target)
    variable_pause(page, min_pause_ms, max_pause_ms, fixed_settle_ms)
    if scroll_before_capture and target == "orders":
        scroll_like_reader(page, min_pause_ms, max_pause_ms)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Amazon Orders read-only bookkeeping capture.")
    parser.add_argument("--target", choices=["orders", "payments"], default="orders", help="Capture target.")
    parser.add_argument("--url", default=None, help="URL to open. Overrides --time-filter and --lookback-days.")
    parser.add_argument("--time-filter", default=None, help="Amazon order-history timeFilter, such as last30, months-3, or year-2026.")
    parser.add_argument("--lookback-days", type=int, default=None, help="Convenience order-history lookback. 60 maps to Amazon's months-3 filter.")
    parser.add_argument("--profile-dir", default=str(DEFAULT_PROFILE_DIR), help=f"Persistent browser profile path. Defaults to ${SHARED_PROFILE_ENV} when set.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Ignored capture output directory.")
    parser.add_argument("--screenshot-dir", default=str(DEFAULT_SCREENSHOT_DIR), help="Ignored screenshot directory.")
    parser.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR), help="Ignored JSONL run log output directory.")
    display = parser.add_mutually_exclusive_group()
    display.add_argument("--headed", dest="headed", action="store_true", default=True, help="Run with a visible browser window. Default.")
    display.add_argument("--headless", dest="headed", action="store_false", help="Run without a visible browser window.")
    parser.add_argument("--wait-for-login", action="store_true", help="Compatibility flag; headed runs now wait automatically when login is needed.")
    parser.add_argument("--wait-seconds", type=int, default=600, help="Manual login wait timeout.")
    parser.add_argument("--stop-on-security-prompt", action="store_true", help="Exit on MFA, CAPTCHA, or device verification text.")
    parser.add_argument("--settle-ms", type=int, default=0, help="Compatibility fixed wait after page readiness. Prefer the variable pause range.")
    parser.add_argument("--min-pause-ms", type=int, default=650, help="Minimum variable pause after page actions.")
    parser.add_argument("--max-pause-ms", type=int, default=2600, help="Maximum variable pause after page actions.")
    parser.add_argument("--no-scroll-before-capture", action="store_true", help="Skip the light page scroll before capturing order pages.")
    parser.add_argument("--max-orders", type=int, default=10, help="Limit captured order cards in output.")
    parser.add_argument(
        "--max-pages",
        default=None,
        help=(
            "Order history pages to crawl when --target orders. Use 'auto' to crawl until "
            "the lookback cutoff is reached. Defaults to auto for --lookback-days runs and 1 otherwise."
        ),
    )
    parser.add_argument("--include-sensitive-text", action="store_true", help="Deprecated no-op. Raw local audit text is always included.")
    parser.add_argument("--save-html", action="store_true", help="Save full page HTML to ignored output.")
    parser.add_argument("--screenshot", action="store_true", help="Save a page screenshot to ignored output.")
    parser.add_argument("--capture-tracking-pages", action="store_true", help="Visit visible tracking links and capture tracking evidence.")
    parser.add_argument(
        "--max-tracking-links",
        type=int,
        default=None,
        help=(
            "Tracking links to probe. Defaults to all discovered links for bookkeeping-shaped "
            "orders runs, or 3 for small interactive runs."
        ),
    )
    parser.add_argument("--capture-order-detail-pages", action="store_true", help="Visit visible order-detail links for ship-to and order evidence.")
    parser.add_argument(
        "--max-order-detail-links",
        type=int,
        default=None,
        help=(
            "Order-detail links to probe. Defaults to all discovered links for bookkeeping-shaped "
            "orders runs, or 3 for small interactive runs."
        ),
    )
    args = parser.parse_args()
    if args.lookback_days is not None and args.lookback_days <= 0:
        parser.error("--lookback-days must be positive")
    explicit_max_pages = args.max_pages
    args.max_pages, args.max_pages_auto = normalize_max_pages(args.max_pages, parser)
    if explicit_max_pages is None and args.target == "orders" and args.lookback_days is not None:
        args.max_pages_auto = True
    if args.max_pages is None and not args.max_pages_auto:
        args.max_pages = 1
    args.max_tracking_links = normalize_probe_limit(args.max_tracking_links, parser, "--max-tracking-links")
    args.max_order_detail_links = normalize_probe_limit(args.max_order_detail_links, parser, "--max-order-detail-links")
    return apply_bookkeeping_probe_defaults(args)


def target_url_from_args(args: argparse.Namespace) -> str:
    if args.url:
        return args.url
    if args.target == "payments":
        return DEFAULT_PAYMENTS_URL
    if args.time_filter:
        return orders_url_for_time_filter(args.time_filter)
    if args.lookback_days is not None:
        return orders_url_for_time_filter(time_filter_for_lookback_days(args.lookback_days))
    return DEFAULT_ORDERS_URL


def main() -> int:
    args = parse_args()
    if chrome_launch_blocked_by_macos_sandbox():
        print(sandboxed_chrome_message(), file=sys.stderr)
        return SANDBOXED_CHROME_EXIT

    target_url = target_url_from_args(args)
    min_pause_ms, max_pause_ms = normalize_pause_bounds(args.min_pause_ms, args.max_pause_ms)
    scroll_before_capture = not args.no_scroll_before_capture
    output_dir = Path(args.output_dir)
    profile_dir = Path(args.profile_dir)
    screenshot_dir = Path(args.screenshot_dir)
    log_dir = Path(args.log_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    launch_options = build_launch_options(args.headed)

    slug = now_slug()
    log_path = log_dir / f"{slug}-{args.target}-field-discovery.jsonl"
    logger = RunLogger(log_path)
    print(f"Run log: {display_path(log_path)}", file=sys.stderr, flush=True)
    logger.event(
        "run_start",
        target=args.target,
        targetUrl=target_url,
        headed=bool(args.headed),
        profileDir=str(profile_dir),
        outputDir=str(output_dir),
        maxOrders=args.max_orders,
        maxPages=page_limit_for_log(args),
        maxPagesAuto=bool(args.max_pages_auto),
        maxPagesSafetyCap=BOOKKEEPING_AUTO_PAGE_SAFETY_CAP if args.max_pages_auto else None,
        captureTrackingPages=bool(args.capture_tracking_pages),
        captureTrackingPagesAutoEnabled=bool(args.capture_tracking_pages_auto_enabled),
        maxTrackingLinks=probe_limit_for_log(args.max_tracking_links),
        captureOrderDetailPages=bool(args.capture_order_detail_pages),
        captureOrderDetailPagesAutoEnabled=bool(args.capture_order_detail_pages_auto_enabled),
        maxOrderDetailLinks=probe_limit_for_log(args.max_order_detail_links),
        bookkeepingAutoProbeMode=bool(args.bookkeeping_auto_probe_mode),
        minPauseMs=min_pause_ms,
        maxPauseMs=max_pause_ms,
        fixedSettleMs=max(0, args.settle_ms),
    )
    with sync_playwright() as playwright:
        logger.event("browser_launch_start", launchOptions=launch_options)
        context = playwright.chromium.launch_persistent_context(str(profile_dir), **launch_options)
        page = context.pages[0] if context.pages else context.new_page()
        logger.event("browser_launch_done", existingPages=len(context.pages), initialPageUrl=page.url)
        page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
        logger.event("initial_goto", url=target_url, pageUrl=page.url)

        readiness = "not_waited"
        try:
            readiness = readiness_from_state(inspect_auth_state(page))
        except PlaywrightError:
            readiness = "unknown"
        logger.event("readiness", phase="initial", readiness=readiness, pageUrl=page.url)

        needs_human_login = readiness in {"blocked", "login_required", "unknown"}
        if needs_human_login and args.headed:
            logger.event("human_login_wait_start", readiness=readiness, waitSeconds=args.wait_seconds)
            print(
                f"Opened Amazon {args.target}. Use the browser window if Amazon asks for sign-in or verification; "
                "capture will continue once page content is visible.",
                flush=True,
            )
            readiness = wait_for_orders_or_login(page, args.wait_seconds, args.stop_on_security_prompt)
            logger.event("human_login_wait_done", readiness=readiness, pageUrl=page.url)
            if readiness == "blocked":
                print("Stopped: Amazon presented CAPTCHA, MFA, or verification text.", file=sys.stderr)
                logger.event("run_stop", reason="blocked", pageUrl=page.url)
                logger.close()
                context.close()
                return 2
            if readiness == "unsupported":
                print("Stopped: Amazon opened a returns/support page instead of order history.", file=sys.stderr)
                logger.event("run_stop", reason="unsupported", pageUrl=page.url)
                logger.close()
                context.close()
                return 4
            if readiness == "timeout":
                print("Stopped: timed out waiting for order content.", file=sys.stderr)
                logger.event("run_stop", reason="timeout_waiting_for_order_content", pageUrl=page.url)
                logger.close()
                context.close()
                return 3
        elif needs_human_login:
            print(
                "Stopped: saved Amazon session is not ready. Re-run with --headed and complete sign-in or verification once; "
                "the persistent profile should reuse that session on later runs.",
                file=sys.stderr,
            )
            logger.event("run_stop", reason="saved_session_not_ready", readiness=readiness, pageUrl=page.url)
            logger.close()
            context.close()
            return 3

        initial_readiness = readiness
        final_readiness = readiness
        pagination_readiness = []
        if args.target == "payments":
            logger.event("payment_capture_wait_start", pageUrl=page.url)
            settle_after_readiness(
                page,
                "payments",
                min_pause_ms,
                max_pause_ms,
                args.settle_ms,
                scroll_before_capture=False,
            )
            logger.event("payment_capture_start", pageUrl=page.url)
            raw_capture = page.evaluate(PAYMENT_DISCOVERY_JS)
            logger.event(
                "payment_capture_done",
                url=raw_capture.get("url", page.url),
                recordCount=len(raw_capture.get("records", [])),
                selectorCounts=raw_capture.get("selectorCounts", {}),
            )
            safe_capture = sanitize_payment_discovery(raw_capture, args.include_sensitive_text)
            safe_capture["target"] = "payments"
            safe_capture["records"] = safe_capture["records"][: args.max_orders]
            safe_capture["recordCountReturned"] = len(safe_capture["records"])
        else:
            raw_captures = []
            seen_page_urls = []
            pagination_stop_reason = ""
            pagination_stop_detail: Dict[str, Any] = {}
            max_pages = BOOKKEEPING_AUTO_PAGE_SAFETY_CAP if args.max_pages_auto else max(1, args.max_pages or 1)
            for page_index in range(max_pages):
                logger.event("order_page_wait_start", pageIndex=page_index, pageUrl=page.url)
                settle_after_readiness(
                    page,
                    "orders",
                    min_pause_ms,
                    max_pause_ms,
                    args.settle_ms,
                    scroll_before_capture=scroll_before_capture,
                )
                logger.event("order_page_capture_start", pageIndex=page_index, pageUrl=page.url)
                raw_page_capture = page.evaluate(DISCOVERY_JS)
                page_url = raw_page_capture.get("url", page.url)
                logger.event("order_page_ship_to_capture_start", pageIndex=page_index, pageUrl=page.url)
                try:
                    raw_ship_to_capture = page.evaluate(CARD_SHIP_TO_DISCOVERY_JS)
                    attach_card_ship_to_popovers(raw_page_capture, raw_ship_to_capture)
                    logger.event(
                        "order_page_ship_to_capture_done",
                        pageIndex=page_index,
                        url=raw_ship_to_capture.get("url", page.url),
                        popoverCount=len(raw_ship_to_capture.get("popovers", [])),
                    )
                except PlaywrightError as exc:
                    logger.event(
                        "order_page_ship_to_capture_error",
                        pageIndex=page_index,
                        error=str(exc).splitlines()[0][:240],
                    )
                oldest_page_order_date = oldest_order_date_from_page_capture(raw_page_capture)
                logger.event(
                    "order_page_capture_done",
                    pageIndex=page_index,
                    url=page_url,
                    cardCount=len(raw_page_capture.get("cards", [])),
                    selectorCounts=raw_page_capture.get("selectorCounts", {}),
                    auth=raw_page_capture.get("auth", {}),
                    oldestOrderDate=oldest_page_order_date.isoformat() if oldest_page_order_date else "",
                )
                seen_page_urls.append(page_url)
                card_offset = sum(len(capture.get("cards", [])) for capture in raw_captures)
                for card in raw_page_capture.get("cards", []):
                    card["index"] = card_offset + int(card.get("index", 0) or 0)
                    card["pageIndex"] = page_index
                    card["pageUrl"] = page_url
                    for popover in card.get("shipToPopovers", []):
                        try:
                            popover["sourceCardIndex"] = card_offset + int(popover.get("sourceCardIndex", 0) or 0)
                        except (TypeError, ValueError):
                            popover["sourceCardIndex"] = card.get("index")
                        popover["sourcePageIndex"] = page_index
                        popover["sourceUrl"] = page_url
                raw_captures.append(raw_page_capture)

                # An empty wrapper can still satisfy Amazon's readiness selector.
                # Do not synthesize another offset indefinitely or call missing
                # page content complete coverage without an observed end marker.
                if not raw_page_capture.get("cards"):
                    pagination_stop_reason = "empty_page_without_end_marker"
                    pagination_stop_detail = {"pageIndex": page_index, "pageUrl": page_url}
                    logger.event("pagination_empty_page", **pagination_stop_detail)
                    break

                if args.max_pages_auto and page_reaches_lookback_cutoff(raw_page_capture, args.lookback_days):
                    cutoff_date = datetime.now(timezone.utc).date() - timedelta(days=args.lookback_days or 0)
                    pagination_stop_reason = "lookback_cutoff_reached"
                    pagination_stop_detail = {
                        "pageIndex": page_index,
                        "oldestOrderDate": oldest_page_order_date.isoformat() if oldest_page_order_date else "",
                        "cutoffDate": cutoff_date.isoformat(),
                    }
                    logger.event(
                        "pagination_auto_lookback_reached",
                        pageIndex=page_index,
                        pageUrl=page_url,
                        oldestOrderDate=oldest_page_order_date.isoformat() if oldest_page_order_date else "",
                        cutoffDate=cutoff_date.isoformat(),
                    )
                    break
                if page_index >= max_pages - 1:
                    pagination_stop_reason = "max_pages_reached"
                    pagination_stop_detail = {
                        "pageIndex": page_index,
                        "maxPages": page_limit_for_log(args),
                        "safetyCap": BOOKKEEPING_AUTO_PAGE_SAFETY_CAP if args.max_pages_auto else None,
                    }
                    logger.event(
                        "pagination_max_pages_reached",
                        pageIndex=page_index,
                        pageUrl=page_url,
                        maxPages=page_limit_for_log(args),
                        safetyCap=BOOKKEEPING_AUTO_PAGE_SAFETY_CAP if args.max_pages_auto else None,
                    )
                    break
                try:
                    page_links = page.evaluate(ORDER_PAGE_LINKS_JS)
                except PlaywrightError:
                    pagination_stop_reason = "pagination_links_error"
                    pagination_stop_detail = {"pageIndex": page_index, "pageUrl": page.url}
                    logger.event("pagination_links_error", pageIndex=page_index, pageUrl=page.url)
                    break
                next_url = next_order_page_url(page_url, page_links, seen_page_urls)
                if not next_url:
                    pagination_stop_reason = "no_next_page"
                    pagination_stop_detail = {
                        "pageIndex": page_index,
                        "pageUrl": page_url,
                        "candidateLinkCount": len(page_links),
                    }
                    logger.event("pagination_no_next", pageIndex=page_index, pageUrl=page_url, candidateLinkCount=len(page_links))
                    break
                logger.event("pagination_next", fromPageIndex=page_index, fromUrl=page_url, nextUrl=next_url)
                try:
                    page.goto(next_url, wait_until="domcontentloaded", timeout=60000)
                    page_readiness = wait_for_orders_or_login(page, args.wait_seconds, args.stop_on_security_prompt)
                except PlaywrightTimeoutError:
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=10000)
                    except PlaywrightTimeoutError:
                        pass
                    page_readiness = wait_for_orders_or_login(page, args.wait_seconds, args.stop_on_security_prompt)
                    if page_readiness != "orders":
                        page_readiness = "navigation_timeout"
                final_readiness = page_readiness
                logger.event("pagination_readiness", pageIndex=page_index + 1, url=next_url, readiness=page_readiness, pageUrl=page.url)
                pagination_readiness.append({
                    "pageIndex": page_index + 1,
                    "url": preserve_url(next_url),
                    "readiness": page_readiness,
                })
                if page_readiness != "orders":
                    pagination_stop_reason = "pagination_readiness_not_orders"
                    pagination_stop_detail = {
                        "pageIndex": page_index + 1,
                        "url": next_url,
                        "readiness": page_readiness,
                    }
                    break

            raw_capture = {
                "capturedAtBrowserTime": raw_captures[0].get("capturedAtBrowserTime") if raw_captures else "",
                "url": raw_captures[0].get("url", page.url) if raw_captures else page.url,
                "title": raw_captures[0].get("title", "") if raw_captures else "",
                "selectorCounts": combine_selector_counts(raw_captures),
                "auth": raw_captures[-1].get("auth", {}) if raw_captures else {},
                "bodyTextLength": sum(capture.get("bodyTextLength", 0) for capture in raw_captures),
                "cards": [card for capture in raw_captures for card in capture.get("cards", [])],
            }
            safe_capture = sanitize_discovery(raw_capture, args.include_sensitive_text)
            safe_capture["target"] = "orders"
            safe_capture["cards"] = safe_capture["cards"][: args.max_orders]
            safe_capture["cardCountReturned"] = len(safe_capture["cards"])
            safe_capture["orderHistoryCoverage"] = build_order_history_coverage(
                lookback_days=args.lookback_days,
                time_filter=time_filter_from_orders_url(target_url),
                max_pages=page_limit_for_log(args),
                max_orders=args.max_orders,
                raw_captures=raw_captures,
                card_count=int(safe_capture.get("cardCount", 0) or 0),
                card_count_returned=int(safe_capture.get("cardCountReturned", 0) or 0),
                stop_reason=pagination_stop_reason or "unknown",
                stop_detail=pagination_stop_detail,
            )
            safe_capture["orderPages"] = [
                {
                    "pageIndex": index,
                    "url": preserve_url(capture.get("url", "")),
                    "cardCount": len(capture.get("cards", [])),
                }
                for index, capture in enumerate(raw_captures)
            ]
        safe_capture["readiness"] = initial_readiness
        safe_capture["initialReadiness"] = initial_readiness
        safe_capture["finalReadiness"] = final_readiness
        if pagination_readiness:
            safe_capture["paginationReadiness"] = pagination_readiness
        safe_capture["rawDataPolicy"] = "raw_local_audit_values_preserved"
        safe_capture["rawTextIncluded"] = True
        safe_capture["timingPolicy"] = {
            "headed": bool(args.headed),
            "fixedSettleMs": max(0, args.settle_ms),
            "minPauseMs": min_pause_ms,
            "maxPauseMs": max_pause_ms,
            "scrollBeforeCapture": bool(scroll_before_capture),
        }
        safe_capture["probePolicy"] = {
            "bookkeepingAutoProbeMode": bool(args.bookkeeping_auto_probe_mode),
            "captureTrackingPages": bool(args.capture_tracking_pages),
            "captureTrackingPagesAutoEnabled": bool(args.capture_tracking_pages_auto_enabled),
            "maxTrackingLinks": probe_limit_for_log(args.max_tracking_links),
            "trackingProbeTruncated": False,
            "trackingProbeCount": 0,
            "captureOrderDetailPages": bool(args.capture_order_detail_pages),
            "captureOrderDetailPagesAutoEnabled": bool(args.capture_order_detail_pages_auto_enabled),
            "maxOrderDetailLinks": probe_limit_for_log(args.max_order_detail_links),
            "orderDetailProbeTruncated": False,
            "orderDetailProbeCount": 0,
        }

        tracking_pages = []
        order_detail_pages = []
        order_probe_bundles = []
        if args.target == "orders" and (args.capture_tracking_pages or args.capture_order_detail_pages):
            order_probe_groups = order_probe_groups_from_capture(safe_capture)
            logger.event(
                "order_probes_start",
                orderCount=len(order_probe_groups),
                captureTrackingPages=bool(args.capture_tracking_pages),
                captureOrderDetailPages=bool(args.capture_order_detail_pages),
                maxTrackingLinks=probe_limit_for_log(args.max_tracking_links),
                maxOrderDetailLinks=probe_limit_for_log(args.max_order_detail_links),
            )
            order_probe_result = capture_order_probe_pages(
                page,
                order_probe_groups,
                bool(args.capture_tracking_pages),
                args.max_tracking_links,
                bool(args.capture_order_detail_pages),
                args.max_order_detail_links,
                min_pause_ms,
                max_pause_ms,
                args.settle_ms,
                args.include_sensitive_text,
                args.wait_seconds,
                args.stop_on_security_prompt,
                logger=logger,
            )
            tracking_pages = order_probe_result.get("trackingPages", [])
            order_detail_pages = order_probe_result.get("orderDetailPages", [])
            order_probe_bundles = order_probe_result.get("orderProbeBundles", [])
            safe_capture["probePolicy"]["trackingProbeTruncated"] = bool(order_probe_result.get("trackingProbeTruncated"))
            safe_capture["probePolicy"]["orderDetailProbeTruncated"] = bool(order_probe_result.get("orderDetailProbeTruncated"))
            safe_capture["probePolicy"]["trackingProbeCount"] = order_probe_result.get("trackingProbeCount", len(tracking_pages))
            safe_capture["probePolicy"]["orderDetailProbeCount"] = order_probe_result.get("orderDetailProbeCount", len(order_detail_pages))
            logger.event(
                "order_probes_done",
                orderCount=len(order_probe_bundles),
                trackingPages=len(tracking_pages),
                orderDetailPages=len(order_detail_pages),
                trackingProbeTruncated=bool(order_probe_result.get("trackingProbeTruncated")),
                orderDetailProbeTruncated=bool(order_probe_result.get("orderDetailProbeTruncated")),
            )
        safe_capture["schemaVersion"] = "amazon-capture/v1"
        safe_capture["trackingPages"] = tracking_pages
        logger.event("tracking_probes_done", count=len(tracking_pages))
        safe_capture["orderDetailPages"] = order_detail_pages
        logger.event("order_detail_probes_done", count=len(order_detail_pages))
        safe_capture["orderProbeBundles"] = order_probe_bundles

        if args.target == "orders" and safe_capture.get("orderPages"):
            first_order_page_url = str(safe_capture["orderPages"][0].get("url", ""))
            try:
                navigate_to_order_history(page, first_order_page_url, args.wait_seconds, args.stop_on_security_prompt)
                settle_after_readiness(
                    page,
                    "orders",
                    min_pause_ms,
                    max_pause_ms,
                    args.settle_ms,
                    scroll_before_capture=False,
                )
            except (PlaywrightError, PlaywrightTimeoutError):
                pass

        if args.screenshot:
            screenshot_path = screenshot_dir / f"{slug}-{args.target}.png"
            page.screenshot(path=str(screenshot_path), full_page=True)
            safe_capture["screenshotPath"] = display_path(screenshot_path)
            logger.event("screenshot_written", path=display_path(screenshot_path))

        if args.save_html:
            html_path = output_dir / f"{slug}-{args.target}.html"
            html_path.write_text(page.content(), encoding="utf-8")
            safe_capture["htmlPath"] = display_path(html_path)
            logger.event("html_written", path=display_path(html_path))

        output_path = output_dir / f"{slug}-{args.target}-field-discovery.json"
        safe_capture["logPath"] = display_path(log_path)
        output_path.write_text(json.dumps(safe_capture, indent=2, sort_keys=True), encoding="utf-8")
        logger.event(
            "capture_written",
            path=display_path(output_path),
            cards=safe_capture.get("cardCountReturned", 0),
            records=safe_capture.get("recordCountReturned", 0),
            trackingPages=len(safe_capture.get("trackingPages", [])),
            orderDetailPages=len(safe_capture.get("orderDetailPages", [])),
            orderHistoryCoverage=safe_capture.get("orderHistoryCoverage", {}),
        )
        context.close()

    logger.event("run_complete", output=display_path(output_path), log=display_path(log_path))
    logger.close()
    print(json.dumps({
        "output": display_path(output_path),
        "log": display_path(log_path),
        "cards": safe_capture.get("cardCountReturned", 0),
        "records": safe_capture.get("recordCountReturned", 0),
        "tracking_pages": len(safe_capture.get("trackingPages", [])),
        "order_detail_pages": len(safe_capture.get("orderDetailPages", [])),
        "target": safe_capture.get("target"),
        "selector_counts": safe_capture.get("selectorCounts", {}),
        "auth": safe_capture.get("auth", {}),
        "order_history_coverage": safe_capture.get("orderHistoryCoverage", {}),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
