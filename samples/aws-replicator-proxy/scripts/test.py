#!/usr/bin/env python3
"""Smoke-test the product catalog API (GET + POST + verify).

Usage:
    python scripts/test.py <api-base-url>
    python scripts/test.py https://xxx.lambda-url.us-east-1.on.aws
    python scripts/test.py http://xxx.lambda-url.us-east-1.localhost.localstack.cloud:4566
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any


def _call(method: str, url: str, data: dict | None = None) -> Any:
    body = json.dumps(data).encode() if data else None
    headers = {"Content-Type": "application/json"} if body else {}
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        raise


def main(api_url: str) -> None:
    base = api_url.rstrip("/")
    print(f"Testing API: {base}\n")

    print("--- GET /products ---")
    products = _call("GET", f"{base}/products")
    print(json.dumps(products, indent=2))
    print(f"\n  {len(products)} product(s)")

    print("\n--- POST /products (ephemeral test item) ---")
    test_id = f"smoke-{int(time.time())}"
    item = {
        "id":       test_id,
        "name":     "Smoke Test Item",
        "price":    1.0,
        "category": "Test",
        "inStock":  True,
    }
    created = _call("POST", f"{base}/products", item)
    print(json.dumps(created, indent=2))

    print("\n--- GET /products (verify item present) ---")
    after = _call("GET", f"{base}/products")
    ids   = [p["id"] for p in after]
    if test_id not in ids:
        print(f"FAIL: test item '{test_id}' missing. Got IDs: {ids}", file=sys.stderr)
        sys.exit(1)

    print(f"  OK — {len(after)} product(s), test item confirmed.")

    print(f"\n--- DELETE /products/{test_id} (cleanup) ---")
    _call("DELETE", f"{base}/products/{test_id}")
    print(f"  Removed smoke test item.")

    print("\nAll checks passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("api_url", help="Base URL of the Lambda Function URL endpoint")
    args = parser.parse_args()
    main(args.api_url)
