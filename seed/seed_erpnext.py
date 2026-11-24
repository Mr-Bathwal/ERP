"""Create the master data in a real ERPNext, idempotently.

Run it twice and nothing is duplicated: every record is checked before it is
created. That is not politeness, it is the difference between a seed script
and a migration script. During an implementation this gets run after every
site rebuild, and one that creates a seventh supplier each time is worse
than no script at all.

The masters here are the same ones `FakeERPClient.demo_company()` holds, and
a test asserts they agree. A suite that passes against invented masters and
fails against the seeded ones has told you nothing.

    export ERPNEXT_URL=http://localhost:8080
    export ERPNEXT_API_KEY=...  ERPNEXT_API_SECRET=...
    python seed/seed_erpnext.py
    python seed/seed_erpnext.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from procureflow.config import settings  # noqa: E402
from procureflow.erp.client import ERPError  # noqa: E402
from procureflow.erp.fake import demo_company  # noqa: E402

COMPANY = settings.company
ABBR = settings.abbr
WAREHOUSE = settings.warehouse
PRICE_LIST = "Standard Buying"


def _masters() -> tuple[list[dict], list[dict], list[dict]]:
    """Read the canonical masters out of the fake, so the two cannot drift."""
    f = demo_company()
    suppliers = [{"supplier_name": r["name"],
                  "supplier_group": r.get("supplier_group", "Raw Material"),
                  "country": r.get("country", "India"),
                  "preferred": bool(r.get("preferred"))}
                 for r in f.docs["Supplier"].values()]
    items = [{"item_code": r["name"], "item_name": r.get("item_name", r["name"]),
              "description": r.get("description", ""),
              "stock_uom": r.get("stock_uom", "Nos"),
              "item_group": r.get("item_group", "Raw Material")}
             for r in f.docs["Item"].values()]
    prices = [{"item_code": r["item_code"], "price_list": PRICE_LIST,
               "price_list_rate": r["price_list_rate"], "currency": "INR"}
              for r in f.docs["Item Price"].values()]
    return suppliers, items, prices


class Seeder:
    """Thin REST wrapper. Deliberately not the agent's ERPClient.

    Seeding writes doctypes the agent is forbidden to touch -- Supplier,
    Item, Warehouse -- and that allowlist is a safety property worth keeping
    narrow. Widening it so one setup script can share the class would make
    the agent able to create suppliers, which is precisely what R6 exists to
    prevent.
    """

    def __init__(self, dry_run: bool = False) -> None:
        import httpx
        self.dry = dry_run
        self.base = settings.erp_url.rstrip("/")
        self.c = httpx.Client(
            base_url=self.base, timeout=30.0,
            headers={"Authorization": f"token {settings.erp_key}:{settings.erp_secret}",
                     "Accept": "application/json",
                     "Content-Type": "application/json"})
        self.created = 0
        self.skipped = 0

    def exists(self, doctype: str, name: str) -> bool:
        r = self.c.get(f"/api/resource/{doctype}/{name}")
        if r.status_code == 404:
            return False
        if r.status_code >= 400:
            raise ERPError(f"{doctype} {name}: HTTP {r.status_code} {r.text[:160]}")
        return True

    def ensure(self, doctype: str, name: str, payload: dict[str, Any]) -> None:
        if self.exists(doctype, name):
            self.skipped += 1
            print(f"  = {doctype:<14} {name}")
            return
        if self.dry:
            self.created += 1
            print(f"  + {doctype:<14} {name}   (dry run)")
            return
        r = self.c.post(f"/api/resource/{doctype}", json=payload)
        if r.status_code >= 400:
            raise ERPError(f"{doctype} {name}: HTTP {r.status_code} {r.text[:240]}")
        self.created += 1
        print(f"  + {doctype:<14} {name}")

    def run(self) -> int:
        suppliers, items, prices = _masters()

        print(f"\nERPNext at {self.base}")
        print(f"Company    {COMPANY} ({ABBR})\n")

        print("Warehouse")
        self.ensure("Warehouse", WAREHOUSE, {
            "warehouse_name": WAREHOUSE.split(" - ")[0], "company": COMPANY,
            "is_group": 0})

        print("\nSupplier groups")
        self.ensure("Supplier Group", "Raw Material",
                    {"supplier_group_name": "Raw Material"})

        print("\nSuppliers")
        for s in suppliers:
            self.ensure("Supplier", s["supplier_name"], {
                "supplier_name": s["supplier_name"],
                "supplier_group": s["supplier_group"],
                "country": s["country"],
                "supplier_type": "Company",
                # ERPNext has no "preferred" field out of the box. The policy
                # names the two preferred suppliers by name (R2), so this is
                # recorded where a buyer will see it rather than invented as
                # a custom field a fresh site would not have.
                "supplier_details": ("Preferred supplier under procurement policy R2."
                                     if s["preferred"] else ""),
            })

        print("\nItem groups")
        self.ensure("Item Group", "Raw Material",
                    {"item_group_name": "Raw Material", "is_group": 0,
                     "parent_item_group": "All Item Groups"})

        print("\nItems")
        for it in items:
            self.ensure("Item", it["item_code"], {
                "item_code": it["item_code"], "item_name": it["item_name"],
                "description": it["description"], "item_group": it["item_group"],
                "stock_uom": it["stock_uom"], "is_stock_item": 1,
                "is_purchase_item": 1,
                "item_defaults": [{"company": COMPANY, "default_warehouse": WAREHOUSE}],
            })

        print("\nBuying price list")
        self.ensure("Price List", PRICE_LIST, {
            "price_list_name": PRICE_LIST, "buying": 1, "selling": 0,
            "currency": "INR", "enabled": 1})

        print("\nItem prices")
        for p in prices:
            # Item Price is named by a hash, so existence is checked by
            # filter rather than by name.
            r = self.c.get("/api/resource/Item Price",
                           params={"filters": f'[["item_code","=","{p["item_code"]}"],'
                                              f'["price_list","=","{PRICE_LIST}"]]',
                                   "limit_page_length": 1})
            if r.status_code < 400 and (r.json().get("data") or []):
                self.skipped += 1
                print(f"  = Item Price     {p['item_code']}")
                continue
            if self.dry:
                self.created += 1
                print(f"  + Item Price     {p['item_code']}   (dry run)")
                continue
            rr = self.c.post("/api/resource/Item Price", json=p)
            if rr.status_code >= 400:
                raise ERPError(f"Item Price {p['item_code']}: {rr.text[:200]}")
            self.created += 1
            print(f"  + Item Price     {p['item_code']} @ {p['price_list_rate']}")

        print(f"\n  {self.created} created, {self.skipped} already present"
              f"{' (dry run: nothing was written)' if self.dry else ''}\n")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed ERPNext master data")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not settings.erp_configured:
        print("ERPNEXT_API_KEY / ERPNEXT_API_SECRET are not set.\n"
              "Generate them in ERPNext: User > Administrator > API Access > "
              "Generate Keys, then put them in .env", file=sys.stderr)
        return 2
    try:
        return Seeder(dry_run=args.dry_run).run()
    except ERPError as e:
        print(f"\nseeding stopped: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
