"""Seed the product catalog with a realistic dataset.

Run against a live product-service:

    python scripts/seed_catalog.py --base-url http://localhost:8001 --token <admin-jwt>

Seed data is generated deterministically (fixed RNG seed) so that load-test and
analytics numbers are reproducible between runs.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import urllib.error
import urllib.request

RNG_SEED = 20260825

CATALOG: dict[str, list[tuple[str, str, int, int]]] = {
    # category: (name, brand, price_rupees, weight_grams)
    "Electronics": [
        ("Samsung 55in Crystal UHD TV", "Samsung", 48999, 15200),
        ("Sony WH-1000XM5 Headphones", "Sony", 29990, 250),
        ("Apple iPhone 15 128GB", "Apple", 71999, 171),
        ("Dell Inspiron 15 Laptop", "Dell", 54990, 1830),
        ("boAt Airdopes 141 Earbuds", "boAt", 1299, 38),
        ("Canon EOS 1500D DSLR", "Canon", 34999, 770),
    ],
    "Home Appliances": [
        ("LG 7kg Front Load Washing Machine", "LG", 32490, 62000),
        ("Prestige Induction Cooktop", "Prestige", 2799, 2400),
        ("Philips Air Fryer HD9200", "Philips", 8499, 4600),
        ("Bajaj 1200mm Ceiling Fan", "Bajaj", 1899, 4200),
        ("Havells Instant Water Heater", "Havells", 4299, 5100),
    ],
    "Groceries": [
        ("Aashirvaad Atta 10kg", "Aashirvaad", 520, 10000),
        ("Tata Salt 1kg", "Tata", 28, 1000),
        ("Fortune Sunflower Oil 5L", "Fortune", 899, 5000),
        ("Amul Butter 500g", "Amul", 285, 500),
        ("Tata Tea Premium 1kg", "Tata", 545, 1000),
        ("Maggi Noodles 12-pack", "Nestle", 168, 840),
    ],
    "Fashion": [
        ("Levis 511 Slim Jeans", "Levis", 3299, 620),
        ("Nike Revolution 6 Running Shoes", "Nike", 3495, 620),
        ("Allen Solly Cotton Shirt", "Allen Solly", 1799, 280),
        ("Fastrack Analog Watch", "Fastrack", 1595, 90),
    ],
    "Sports and Outdoors": [
        ("SG Cricket Bat English Willow", "SG", 8999, 1250),
        ("Nivia Football Size 5", "Nivia", 799, 430),
        ("Yonex Badminton Racquet", "Yonex", 2299, 88),
        ("Decathlon Yoga Mat 8mm", "Domyos", 1299, 1400),
    ],
}


def _post(base_url: str, path: str, token: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def sku_for(category: str, index: int) -> str:
    prefix = "".join(word[0] for word in category.split())[:3].upper()
    return f"{prefix}-{index:04d}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the RetailPulse catalog.")
    parser.add_argument("--base-url", default="http://localhost:8001")
    parser.add_argument("--token", required=True, help="Admin JWT.")
    args = parser.parse_args()

    random.seed(RNG_SEED)

    created = skipped = failed = 0
    index = 1
    for category, items in CATALOG.items():
        for name, brand, price, weight in items:
            payload = {
                "sku": sku_for(category, index),
                "name": name,
                "description": f"{name} from {brand}.",
                "category": category,
                "brand": brand,
                "price": f"{price}.00",
                "currency": "INR",
                "weight_grams": weight,
            }
            status, body = _post(args.base_url, "/products", args.token, payload)
            if status == 201:
                created += 1
            elif status == 409:
                skipped += 1  # re-running the seed must be safe
            else:
                failed += 1
                print(f"  FAILED {payload['sku']}: {status} {body}", file=sys.stderr)
            index += 1

    total = sum(len(v) for v in CATALOG.values())
    print(
        f"Seeded catalog: {created} created, {skipped} already present, "
        f"{failed} failed (of {total} across {len(CATALOG)} categories)."
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
