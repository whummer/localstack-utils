#!/usr/bin/env python3
"""Seed DynamoDB with sample products.

Usage:
    python scripts/seed.py [aws|local]
"""

import argparse
import os
from decimal import Decimal

import boto3

PRODUCTS = [
    {"id": "prod-001", "name": "Wireless Headphones", "price": Decimal("79.99"),  "category": "Electronics",  "inStock": True},
    {"id": "prod-002", "name": "Ergonomic Mouse",      "price": Decimal("45.00"),  "category": "Accessories",  "inStock": True},
    {"id": "prod-003", "name": "USB-C Hub (7-port)",   "price": Decimal("34.99"),  "category": "Accessories",  "inStock": False},
    {"id": "prod-004", "name": "4K Webcam",             "price": Decimal("129.00"), "category": "Electronics",  "inStock": True},
]


def main(mode: str) -> None:
    region     = os.environ.get("AWS_REGION", "us-east-1")
    table_name = os.environ.get("TABLE_NAME", "product-catalog-products")

    kwargs: dict = {"region_name": region}
    if mode == "local":
        kwargs["endpoint_url"]        = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
        kwargs["aws_access_key_id"]     = "test"
        kwargs["aws_secret_access_key"] = "test"

    dynamodb = boto3.resource("dynamodb", **kwargs)
    table = dynamodb.Table(table_name)

    print(f"Seeding '{table_name}' ({mode})...")
    for product in PRODUCTS:
        table.put_item(Item=product)
        print(f"  + {product['name']} — ${product['price']}")

    print(f"\nDone. {len(PRODUCTS)} products seeded.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["aws", "local"], nargs="?", default="local",
                        help="Target environment (default: local)")
    args = parser.parse_args()
    main(args.mode)
