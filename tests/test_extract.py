"""Offline regression tests for the Amazon scraper.

Amazon changes its markup without warning, and a scraper that breaks silently
returns empty fields that look like "this product has no delivery date" rather
than "the selector stopped matching". These tests run against saved HTML so a
broken selector fails loudly and without touching the network.

Run:  .venv\\Scripts\\python.exe tests\\test_extract.py

Refresh fixtures (needs network) with tests/refresh_fixtures.py when Amazon's
layout genuinely changes and the extractor has been updated to match.
"""

import gzip
import pathlib
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import server  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

_failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  ok   {message}")
    else:
        print(f"  FAIL {message}")
        _failures.append(message)


def load(name: str) -> str:
    """Read a fixture. Stored gzipped: raw Amazon HTML is ~1.8 MB per page."""
    gz = FIXTURES / (name + ".gz")
    if gz.exists():
        with gzip.open(gz, "rt", encoding="utf-8") as f:
            return f.read()
    plain = FIXTURES / name
    if plain.exists():
        return plain.read_text(encoding="utf-8")
    raise SystemExit(f"missing fixture {gz}; run tests/refresh_fixtures.py")


def test_delivery_parsing() -> None:
    print("delivery date parsing")
    today = date(2026, 7, 27)

    p = server.parse_delivery_text("無料配送 7月29日 水曜日にお届け", today)
    check(p.get("date") == "2026-07-29", "single date -> 2026-07-29")
    check(p.get("days") == 2, "single date -> 2 days out")

    p = server.parse_delivery_text("無料配送 8月12日-15日にお届け", today)
    check(p.get("date") == "2026-08-12" and p.get("latest") == "2026-08-15",
          "range -> earliest and latest")

    p = server.parse_delivery_text("最も早い配送 本日中にお届け", today)
    check(p.get("days") == 0, "本日中 -> 0 days")

    p = server.parse_delivery_text("配送 明日にお届け", today)
    check(p.get("days") == 1, "明日 -> 1 day")

    # A combined row quotes free delivery first and a paid express option after
    # "または/最も早い". The deadline must be judged on the free offer.
    p = server.parse_delivery_text(
        "無料配送 7月29日 水曜日にお届けまたは 最も早い 配送 本日中にお届け", today)
    check(p.get("date") == "2026-07-29" and p.get("days") == 2,
          "paid express option does not override the standard date")

    # A January quote for a December date belongs to the previous year's tail,
    # not eleven months in the future.
    p = server.parse_delivery_text("1月5日にお届け", date(2026, 12, 20))
    check(p.get("date") == "2027-01-05", "year rolls over at December")

    check(server.parse_delivery_text("") == {}, "empty text -> no date")
    check(server.parse_delivery_text("出品者による発送") == {}, "no date -> empty")


def test_price_parsing() -> None:
    print("price parsing")
    check(server.parse_price("￥6,510")[0] == 6510, "yen with comma")
    check(server.parse_price("$129.99")[1] == "$", "usd keeps its symbol")
    check(server.clean_price("￥6,510") == "￥6,510", "round trips")
    check(server.parse_price("")[0] is None, "empty -> None")


def test_domestic_product() -> None:
    print("product page (domestic, Amazon-fulfilled)")
    d = server.extract_product_data(load("product_domestic.html"),
                                    "https://www.amazon.co.jp/dp/B0H1N2N5XQ")
    check(d["asin"] == "B0H1N2N5XQ", "asin")
    check(d["price_value"] is not None and d["price_value"] > 0, "price is numeric")
    check(d["name"] != "Product name not found", "name found")
    check(bool(d["delivery"]), "delivery line present")
    check(bool(d["delivery_parsed"]), "delivery date parsed")
    check(d["ship_method"] is not None, "ship method present")
    check(d["sold_by"] is not None, "sold_by present")
    check(len(d["features"]) >= 3, f"feature bullets captured ({len(d['features'])})")
    check(len(d["specs"]) >= 5, f"spec rows captured ({len(d['specs'])})")
    check(len(d["images"]) >= 2, f"images captured ({len(d['images'])})")
    check(d["import_note"] is None, "domestic listing is not flagged as import")


def test_import_product() -> None:
    print("product page (overseas seller)")
    d = server.extract_product_data(load("product_import.html"),
                                    "https://www.amazon.co.jp/dp/B0D97X6CJ4")
    check(d["asin"] == "B0D97X6CJ4", "asin")
    check(d["import_note"] is not None, "import listing IS flagged")
    check(not d["delivery_parsed"], "no delivery date is reported as absent")
    check(len(d["specs"]) >= 5, "spec rows captured")


def test_search_results() -> None:
    print("search results page")
    rows = server.extract_search_results(load("search_results.html"), 30)
    check(len(rows) >= 10, f"rows extracted ({len(rows)})")
    check(all(r["url"].startswith("https://") for r in rows), "urls absolute")
    check(sum(1 for r in rows if r["price_value"]) >= 5, "prices numeric")
    check(sum(1 for r in rows if r.get("asin")) >= 10, "asins present")
    check(sum(1 for r in rows if r.get("delivery")) >= 3, "delivery text present")
    check(sum(1 for r in rows if r.get("delivery_parsed")) >= 3, "delivery dates parsed")


def main() -> int:
    for fn in (test_delivery_parsing, test_price_parsing, test_domestic_product,
               test_import_product, test_search_results):
        fn()
    print()
    if _failures:
        print(f"{len(_failures)} check(s) FAILED")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
