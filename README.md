# Amazon Capture

Read-only local capture of Amazon order history, package items and quantities,
tracking pages, order details, and payment activity. Runs independently of any
ledger, bank scraper or receiving service.

## Install

Requires Python 3.11 or newer. Install the wheel from the GitHub release, or clone
this repository and run:

```sh
python -m pip install .
python -m playwright install chromium
```

On macOS the collector uses installed Google Chrome when available. macOS is the
currently validated platform; Chromium fallback is available on other systems.

## Capture

```sh
amazon-capture --target orders --lookback-days 30 --max-orders 200 --headed --stop-on-security-prompt
amazon-capture --target payments --max-orders 100 --headed --stop-on-security-prompt
```

The first headed run allows you to sign in yourself using a private persistent
browser profile. Security prompts are handled by the user. The collector does not
place orders, cancel purchases, change payment methods, or submit account changes.

Defaults are relative to the current working directory: `raw-captures/` for JSON,
`private/browser-profiles/` for browser state, and `logs/` for execution logs.
Set `--profile-dir`, `--output-dir`, and `--log-dir` to override them. Existing
integrations may provide `DROPSHIP_BROWSER_PROFILE_DIR` for a shared profile.
Captures contain original order/payment identifiers and may include addresses.
Keep runtime files private; do not commit or upload captures or browser profiles.

Small interactive runs have bounded probes. A bookkeeping run with a lookback
and larger order limit automatically follows package tracking/detail links and
paginates toward the requested cutoff. Inspect coverage and truncation metadata
instead of treating an absent row as a cancelled or missing order.

## Output contract

The last stdout JSON object identifies the capture `output` path, target, record
counts, authentication state, and coverage. See [the capture contract](docs/contract.md).
Normalize captured files without opening a browser:

```sh
amazon-observations --help
```

Python consumers can call `amazon_capture.observations.normalize_captures` with a
sequence of `(source_path, capture_dict)` pairs. Each normalized result records
`schemaVersion`, source provenance, observations and field coverage. Financial
matching, order-history storage and P&L are responsibilities of the consuming app.

## Development

```sh
python -m pip install .
python -m unittest discover -s tests
python -m pip install build
python -m build
```

Tests use synthetic records and local HTML fixtures only. The release workflow
builds wheel and source archives after tests; real account data is never a fixture.
