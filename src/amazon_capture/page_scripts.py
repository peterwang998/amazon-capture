"""Read-only page extractors consumed by the browser capture module."""

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
