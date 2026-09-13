# Capture and observation contracts

`amazon-capture` writes a JSON object representing one target and prints a summary
whose `output` points to that file. Paths can be relative to the invocation's
working directory. Capture consumers should retain the complete original bytes
and capture time for audit and cross-day comparisons.

Orders contain `target: "orders"`, `capturedAt`, `cards`, `trackingPages`,
`orderDetailPages`, and coverage/probe metadata when available. Cards retain visible
text, discovered links, order identities and item information. Tracking pages
carry the URL that identifies the order/package being inspected. Never associate
one package's tracking with a sibling merely because they share an order.

Payments contain `target: "payments"`, `capturedAt`, and `records`, together with
page and authentication/readiness metadata. Amount signs and original strings
remain available as observed on the source page.

The additive `schemaVersion: "amazon-capture/v1"` identifies newly emitted captures.
Historical captures without this marker are accepted by the normalization API.
Page-specific evidence is intentionally extensible; consumers should ignore
unknown fields, preserve raw values, and check coverage before inferring absence.

`normalize_captures` returns `schemaVersion: "amazon-observations/v0.1"`,
`generatedAt`, `sourceCaptures`, `observations` and `coverage`. The observations
object contains `orders`, `trackingPages`, and `payments` arrays. Identifiers and
provenance connect each observation to its source. Normalization preserves distinct observations across captures. Applications can
use the identifiers, timestamps and provenance for their own processing.

New captures describe automatic page probing with `historyAutoProbeMode` in
`probePolicy`. This flag is descriptive; consumers should use the explicit probe
limits and coverage fields when deciding whether a capture is complete.
