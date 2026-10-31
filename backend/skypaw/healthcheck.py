"""Container HEALTHCHECK: exit 0 if GET /healthz on $PORT answers {"status": "ok"}."""

from __future__ import annotations

import json
import os
import sys
import urllib.request


def main() -> int:
    port = os.environ.get("PORT", "7860")
    url = f"http://127.0.0.1:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=4) as resp:  # noqa: S310 - fixed local URL
            ok = resp.status == 200 and json.load(resp).get("status") == "ok"
    except Exception as exc:  # noqa: BLE001 - any failure means unhealthy
        print(f"unhealthy: {exc}", file=sys.stderr)  # noqa: T201
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
