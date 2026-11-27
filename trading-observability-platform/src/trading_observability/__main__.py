"""Allow execution via `python3 -m trading_observability`."""

from __future__ import annotations

from trading_observability.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
