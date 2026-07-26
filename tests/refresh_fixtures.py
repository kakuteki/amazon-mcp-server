"""Re-download the HTML fixtures used by test_extract.py (needs network).

Only run this when Amazon's layout has genuinely changed and the extractor has
been updated to match. Refreshing fixtures to make a red test go green hides
exactly the breakage the tests exist to catch.

Run:  .venv\\Scripts\\python.exe tests\\refresh_fixtures.py
"""

import asyncio
import gzip
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import server  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

TARGETS = {
    # A domestic, Amazon-fulfilled listing: has a delivery date, ship method,
    # spec table and feature bullets.
    "product_domestic.html": "https://www.amazon.co.jp/dp/B0H1N2N5XQ",
    # An overseas-seller listing: states no delivery date at all. Keeps the
    # "absent date is reported as absent" behaviour honest.
    "product_import.html": "https://www.amazon.co.jp/dp/B0D97X6CJ4",
    "search_results.html":
        "https://www.amazon.co.jp/s?k=AHD+%E9%98%B2%E7%8A%AF%E3%82%AB%E3%83%A1%E3%83%A9"
        "+%E5%B1%8B%E5%A4%96&s=price-asc-rank",
}


async def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, url in TARGETS.items():
        html = await server.fetch_amazon_page(url, use_cache=False)
        out = FIXTURES / (name + ".gz")
        with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as f:
            f.write(html)
        print(f"{name}: {len(html):,} bytes -> {out.stat().st_size:,} gzipped")
        await asyncio.sleep(2.0)


if __name__ == "__main__":
    asyncio.run(main())
