# Amazon Capture

A local, read-only Python toolkit for capturing Amazon order history, package
items and quantities, tracking details, and payment activity.

## Install

Requires Python 3.11 or newer. Install the wheel from the latest GitHub release,
or clone this repository and run:

```sh
python -m pip install .
python -m playwright install chromium
```

The collector uses installed Google Chrome on macOS when available and Chromium
otherwise. The test suite runs on macOS and Linux.

## Capture

```sh
amazon-capture --target orders --lookback-days 30 --max-orders 200 --headed --stop-on-security-prompt
amazon-capture --target payments --max-orders 100 --headed --stop-on-security-prompt
```

On the first headed run, sign in through the browser window. Complete any
security prompts yourself. Capture operations read the pages you can access.

Paths default to `raw-captures/`, `private/browser-profiles/`, `screenshots/`, and
`logs/` beneath the current working directory. Override them with `--output-dir`,
`--profile-dir`, `--screenshot-dir`, and `--log-dir`. Set
`AMAZON_CAPTURE_PROFILE_DIR` to choose a default browser profile; an explicit
`--profile-dir` takes precedence. Configuration is resolved for each invocation.

Small interactive runs have bounded page probes. Larger order-history runs
follow tracking and detail links and paginate toward the requested cutoff.
Coverage metadata describes the pages and date range actually captured.

## Use from Python

Normalize saved captures without launching a browser:

```python
from pathlib import Path
from amazon_capture.observations import load_capture, normalize_captures

path = Path("raw-captures/orders.json")
result = normalize_captures([(str(path), load_capture(path))])
```

The result contains `schemaVersion`, `sourceCaptures`, `observations`, and
`coverage`. Original identifiers and source provenance remain available.
See [the output contract](docs/contract.md) for details.

## Modules

- `config`: runtime directories and environment settings.
- `page_scripts`: Amazon page extraction functions, independent of orchestration.
- `capture`: browser navigation, pagination, page probes, and the capture CLI.
- `observations`: normalization of saved captures and the observation CLI.
- `quality`: validation of capture contents.

The package depends only on Playwright and the Python standard library.

## Development

```sh
python -m pip install .
python -m unittest discover -s tests
python -m pip install build
python -m build
```

Tests use synthetic records and local HTML fixtures. Release builds contain
source, documentation, and those fixtures; runtime directories are excluded.
