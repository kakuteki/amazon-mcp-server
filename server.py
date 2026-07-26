# This is amazon products scraper mcp server
# Build a scraper that can scrape amazon products
# The scraper should be able to scrape the product name, price, and image
import asyncio
import json
import re
import sys
import time
from datetime import date, datetime, timedelta

import httpx
from mcp.server.fastmcp import FastMCP
from bs4 import BeautifulSoup
from urllib.parse import urlparse

# Create a Trello MCP server
mcp = FastMCP(
    "Amazon Scraper", 
    instructions="""
    # Amazon Scraper Server
    
    This server provides access to Amazon products through various tools.
    For search products, identify the keywords and number of results you want to get from the user input
    
    ## Available Tools
    - `search_products(query, ...)` - Search Amazon
    - `scrape_product(product_url, postal_code)` - Full detail for one product
    - `compare_products(product_urls, postal_code)` - Several products side by side

    ## When to use what
    - Deciding between candidates: `compare_products` — one call, one table,
      fewer round trips than N separate scrapes.
    - One product in depth: `scrape_product`
    - Finding candidates: `search_products(query, max_results, sort, min_price,
      max_price, hide_sponsored, pages, max_delivery_days, postal_code)`

    ## Getting an exhaustive sweep
    - One result page is 16-24 rows. `pages=1` is NOT "everything on Amazon".
      For a thorough search pass `pages=3..5`; results are deduplicated by ASIN.
    - `max_delivery_days=3` drops rows that arrive later. Rows with no stated
      date are kept and marked — no date usually means an overseas seller.

    ## Delivery dates
    - Always pass `postal_code`. Estimates are address-dependent and the default
      region can be days off.
    - Delivery lines come back with the parsed date appended, e.g.
      `無料配送 7月29日... [2026-07-29 / 2日後]`, so a deadline can be checked
      without reading Japanese dates by eye.

    ## Beating inflated / sponsored listings
    - Amazon's default order is "featured" and surfaces sponsored + marketplace-reseller
      listings first, which are often price-inflated. To get real market prices:
      - pass `sort="price_asc"` to sort cheapest-first, and/or
      - pass `min_price`/`max_price` (yen) to pin a realistic price band, and/or
      - pass `hide_sponsored=True` to drop paid placements.
    - Search results now include the number of ratings; a trustworthy product has many
      ratings, a thin reseller listing usually has very few.

    ## Choosing between listings
    `scrape_product` returns, besides price and rating:
    - **delivery date / fastest delivery / ship method** — a listing that says
      在庫あり can still show no date at all when it ships from overseas.
    - **ships-from / sold-by / import notice** — a no-name marketplace reseller
      usually means an inflated price, a 並行輸入品, or no warranty.
    - **shipping fee and points** — the headline price is not what you pay.
    - **specification table and all feature bullets** — the only place actual
      numbers appear (PoE class, lens focal length, memory slots, NTSC/PAL).
    - **images** — hero shot plus alternates, for build quality and connectors.

    Delivery dates are computed from the delivery address. This scraper is not
    signed in, so the date is Amazon's default-region estimate — confirm on the
    order screen before committing to a schedule.

    ## Notes
    - No API key required
        """
)

# Constants
# BASE_URL = "https://api.trello.com/1"
# API_KEY = os.getenv("TRELLO_API_KEY")
# API_TOKEN = os.getenv("TRELLO_API_TOKEN")
BASE_URL = "https://www.amazon.co.jp"

# Helper functions

# A small pool of realistic desktop User-Agents. We rotate through these across
# retry attempts to reduce the chance of tripping Amazon's bot-check.
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15',
]


def _make_headers(idx: int) -> dict:
    """Build a browser-like header set, rotating the User-Agent by attempt."""
    ua = USER_AGENTS[idx % len(USER_AGENTS)]
    headers = {
        'User-Agent': ua,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'sec-ch-ua-mobile': '?0',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Cache-Control': 'max-age=0',
        'Device-Memory': '8',
    }
    # Only send Chromium client hints for Chrome UAs (Firefox/Safari don't).
    if 'Chrome/' in ua:
        ver = re.search(r'Chrome/(\d+)', ua)
        v = ver.group(1) if ver else '120'
        headers['sec-ch-ua'] = f'"Chromium";v="{v}", "Google Chrome";v="{v}", "Not_A Brand";v="99"'
        headers['sec-ch-ua-platform'] = '"macOS"' if 'Mac OS' in ua else '"Windows"'
    return headers


# Kept for backward compatibility (unused by fetch now).
BROWSER_HEADERS = _make_headers(0)


class CaptchaBlocked(Exception):
    """Raised when Amazon serves a bot-check / captcha page instead of content."""


# --- Japanese delivery-date parsing -----------------------------------------
#
# Amazon states delivery as free text ("無料配送 7月29日 水曜日にお届け",
# "8月12日-15日にお届け", "本日中にお届け"). Returning that string alone forces
# the caller to eyeball every listing to answer "does it arrive within N days?",
# which is the single most common reason to look at this data at all.

_MD_RE = re.compile(r'(\d{1,2})\s*月\s*(\d{1,2})\s*日')
_SAME_MONTH_RANGE_RE = re.compile(
    r'(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*[-–—〜~]\s*(\d{1,2})\s*日'
)


def _resolve_year(month: int, day: int, today: date) -> date:
    """Attach a year to a bare 月日, assuming Amazon never quotes the past.

    A month more than three behind the current one is next year's (a December
    date quoted in January), otherwise it is this year's.
    """
    year = today.year
    if month < today.month - 3:
        year += 1
    try:
        d = date(year, month, day)
    except ValueError:
        return today
    # A date already well behind today means the year rolled over.
    if (today - d).days > 180:
        d = date(year + 1, month, day)
    return d


def parse_delivery_text(text: str, today: date | None = None) -> dict:
    """Turn a Japanese delivery string into concrete dates.

    Returns {'date': ISO earliest, 'latest': ISO latest, 'days': int} where
    `days` counts from `today` to the earliest promised date. Empty dict when
    no date can be read (e.g. seller-shipped listings that state none).
    """
    if not text:
        return {}
    today = today or date.today()

    # Search rows merge both offers into one string:
    #   "無料配送 7月29日にお届け または 最も早い配送 本日中にお届け"
    # The second half is a paid express option. Judging a deadline by it would
    # report "arrives today" for something whose normal delivery is two days
    # out, so only the standard offer counts here.
    head = re.split(r'または|最も早い', text)[0].strip() or text

    if '本日' in head or '今日' in head:
        return {'date': today.isoformat(), 'latest': today.isoformat(), 'days': 0}

    found = [_resolve_year(int(m), int(d), today) for m, d in _MD_RE.findall(head)]

    # Amazon writes same-month ranges with the month omitted on the far side
    # ("8月12日-15日にお届け"). The bare 月日 pattern only sees the first date,
    # so the range end has to be reconstructed.
    span = _SAME_MONTH_RANGE_RE.search(head)
    if span:
        month, d1, d2 = int(span.group(1)), int(span.group(2)), int(span.group(3))
        start = _resolve_year(month, d1, today)
        end_month = month if d2 >= d1 else (month % 12) + 1
        found.append(_resolve_year(end_month, d2, today))
        found.append(start)

    if not found:
        if '明日' in head:
            d = today + timedelta(days=1)
            return {'date': d.isoformat(), 'latest': d.isoformat(), 'days': 1}
        return {}

    earliest, latest = min(found), max(found)
    return {
        'date': earliest.isoformat(),
        'latest': latest.isoformat(),
        'days': (earliest - today).days,
    }


def format_delivery(text: str, parsed: dict) -> str:
    """Render a delivery line with the machine-readable part appended."""
    if not parsed:
        return text
    days = parsed['days']
    when = f"{days}日後" if days > 1 else ("本日" if days <= 0 else "明日")
    span = parsed['date']
    if parsed['latest'] != parsed['date']:
        span += f"〜{parsed['latest']}"
    return f"{text}  [{span} / {when}]"


# --- Page cache --------------------------------------------------------------
#
# Comparing a shortlist means fetching the same ASINs repeatedly. Without a
# cache the server re-requests pages it already has, which is both slow and the
# quickest way to trip Amazon's bot check.

_CACHE: dict[str, tuple[float, str]] = {}
CACHE_TTL_SECONDS = 300


def postal_warning(requested: str) -> str:
    """Warn when a requested postal code could not be applied.

    Silently falling back to Amazon's default region is the dangerous case: the
    dates still look precise, so a caller schedules around another prefecture's
    delivery estimate without ever knowing.
    """
    if not requested or _CURRENT_POSTAL == requested:
        return ""
    return (f"!! 郵便番号 {requested} を反映できませんでした。"
            "お届け日はAmazonの既定地域の推定です（下の Estimate for: 参照）。"
            "締切がある場合は注文画面で確認してください。\n\n")


def _cache_get(url: str) -> str | None:
    hit = _CACHE.get(url)
    if not hit:
        return None
    ts, html = hit
    if time.time() - ts > CACHE_TTL_SECONDS:
        _CACHE.pop(url, None)
        return None
    return html


def _cache_put(url: str, html: str) -> None:
    _CACHE[url] = (time.time(), html)


# A real amazon.co.jp page is 1-2 MB. Anything remotely small is an
# interstitial, a "sorry" stub or a throttle response — none of which contain
# the word "captcha", which is how a blocked fetch used to be mistaken for a
# page with no results.
_MIN_PLAUSIBLE_PAGE_BYTES = 50_000


def _looks_like_captcha(html: str) -> bool:
    low = html.lower()
    if ('captcha' in low
            or 'api-services-support@amazon' in low
            or 'to discuss automated access' in low
            or 'enter the characters you see below' in low):
        return True
    return len(html) < _MIN_PLAUSIBLE_PAGE_BYTES


# Cookies survive between calls so we stop paying the homepage warm-up round
# trip on every single fetch (and look less like a swarm of fresh clients).
_COOKIES = httpx.Cookies()
_CURRENT_POSTAL: str | None = None

# Hard ceiling for one fetch. Without it the retry ladder (5 sessions x 3
# attempts x 20s) can hang a tool call for minutes with no output.
FETCH_BUDGET_SECONDS = 60.0


async def _set_delivery_postal(client: httpx.AsyncClient, postal_code: str,
                               page_html: str, referer: str) -> bool:
    """Best-effort: pin Amazon's delivery estimates to a postal code.

    Delivery dates are computed per address, so without this the caller gets
    estimates for Amazon's default region — which is worse than no date at all,
    because it looks authoritative.

    Reality check: the token has to come from a page we already fetched, because
    the homepage answers automated requests with a 2 KB bot interstitial. Even
    with a valid token the change endpoint returns 200 and leaves the address
    untouched. This therefore usually fails; the return value is what callers
    use to warn instead of quietly reporting the wrong region's dates.
    """
    try:
        token = None
        for pattern in (r'"CSRF_TOKEN"\s*:\s*"([^"]+)"', r'csrfToken"\s*:\s*"([^"]+)"'):
            m = re.search(pattern, page_html)
            if m:
                token = m.group(1)
                break
        if not token:
            return False
        resp = await client.post(
            f"{BASE_URL}/portal-migration/hz/glow/address-change?actionSource=glow",
            headers={
                'anti-csrftoken-a2z': token,
                'Content-Type': 'application/json',
                'Referer': referer,
            },
            content=json.dumps({
                'locationType': 'LOCATION_INPUT',
                'zipCode': postal_code,
                'deviceType': 'web',
                'storeContext': 'generic',
                'pageType': 'Detail',
                'actionSource': 'glow',
            }),
        )
        if resp.status_code != 200:
            return False
        # Verify rather than trust the status code: the endpoint answers 200
        # even when it ignores the request.
        again = await client.get(referer, headers={'Referer': BASE_URL + '/'})
        digits = re.sub(r'\D', '', postal_code)
        return digits and digits in re.sub(r'\D', '', again.text[:400_000])
    except Exception:
        return False


async def _fetch_once(url: str, postal_code: str | None) -> str:
    """Single fetch attempt ladder (no overall timeout applied here)."""
    global _CURRENT_POSTAL

    for session_attempt in range(5):
        headers = _make_headers(session_attempt)
        async with httpx.AsyncClient(
            follow_redirects=True, headers=headers, timeout=20.0, cookies=_COOKIES
        ) as client:
            html = None
            for attempt in range(3):
                extra = {'Referer': BASE_URL + '/'} if attempt else {}
                response = await client.get(url, headers=extra)
                if response.status_code == 200:
                    html = response.text
                    break
                await asyncio.sleep(1.0 + attempt)

            # Applying a postal code needs a token that only a real page carries,
            # so this runs after the first successful fetch, not before it.
            if (html and postal_code and postal_code != _CURRENT_POSTAL
                    and not _looks_like_captcha(html)):
                if await _set_delivery_postal(client, postal_code, html, url):
                    _CURRENT_POSTAL = postal_code
                    retry = await client.get(url, headers={'Referer': BASE_URL + '/'})
                    if retry.status_code == 200:
                        html = retry.text

            _COOKIES.update(client.cookies)

            if html is not None and not _looks_like_captcha(html):
                return html

        # Landed on captcha or non-200; a stale session is a likely cause.
        _COOKIES.clear()
        _CURRENT_POSTAL = None
        await asyncio.sleep(2.0 + 2.0 * session_attempt)

    raise CaptchaBlocked(
        "Amazon returned a bot-check (captcha) page. Try again in a little while."
    )


async def fetch_amazon_page(url: str, postal_code: str = "", use_cache: bool = True) -> str:
    """Fetch an Amazon page, with caching and a hard time budget.

    Amazon returns 503 to bare requests and intermittently serves a captcha
    page (HTTP 200) to traffic it thinks is automated, so we retry with rotated
    User-Agents and a fresh session. Results are cached briefly because
    comparing a shortlist re-reads the same pages.
    """
    cache_key = f"{postal_code}|{url}"
    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    try:
        html = await asyncio.wait_for(
            _fetch_once(url, postal_code or None), timeout=FETCH_BUDGET_SECONDS
        )
    except asyncio.TimeoutError:
        raise CaptchaBlocked(
            f"Amazon did not respond within {FETCH_BUDGET_SECONDS:.0f}s "
            "(likely rate limiting). Try again in a little while."
        )

    _cache_put(cache_key, html)
    return html

def clean_price(price_text: str) -> str:
    """Format a price string, preserving the currency actually shown.

    The previous version stripped every non-digit and re-attached "￥"
    unconditionally, so a listing quoted in another currency came back looking
    like yen, and a price *range* ("￥3,000 - ￥5,000") collapsed into one
    meaningless number.
    """
    value, currency = parse_price(price_text)
    if value is None:
        return "Price not available"
    return f"{currency}{value:,}"


def parse_price(price_text: str) -> tuple[int | None, str]:
    """Return (amount, currency symbol) for the first price in the text."""
    if not price_text:
        return None, '￥'
    text = price_text.strip()
    currency = '￥'
    if '$' in text or 'USD' in text.upper():
        currency = '$'
    elif '€' in text:
        currency = '€'
    m = re.search(r'(\d[\d,]*)', text)
    if not m:
        return None, currency
    digits = m.group(1).replace(',', '')
    try:
        return int(digits), currency
    except ValueError:
        return None, currency

def extract_product_data(html_content: str, url: str) -> dict:
    """Extract product information from Amazon page HTML"""
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Initialize product data
    product_data = {
        'name': 'Product name not found',
        'asin': None,
        'brand': None,
        'price': 'Price not available',
        'price_value': None,
        'shipping_fee': None,
        'shipping_fee_value': None,
        'total_value': None,
        'points': None,
        'stock_left': None,
        'image_url': 'Image not found',
        'images': [],
        'rating': 'Rating not available',
        'reviews_count': 'Reviews not available',
        'availability': 'Availability not found',
        'delivery': None,
        'delivery_parsed': {},
        'fastest_delivery': None,
        'fastest_parsed': {},
        'ship_method': None,
        'delivery_address': None,
        'ships_from': None,
        'sold_by': None,
        'import_note': None,
        'features': [],
        'specs': {},
        'description': 'Description not available',
        'url': url
    }
    
    try:
        # Extract product name
        name_selectors = [
            '#productTitle',
            'h1.a-size-large',
            '.a-size-large.product-title-word-break',
            'h1[data-automation-id="product-title"]'
        ]
        
        for selector in name_selectors:
            name_elem = soup.select_one(selector)
            if name_elem:
                product_data['name'] = name_elem.get_text().strip()
                break
        
        # Extract price. The full offscreen price comes first: `.a-price-whole`
        # holds only the integer part and silently drops everything else.
        price_selectors = [
            '.a-price .a-offscreen',
            '.a-price-range .a-price-range-min .a-offscreen',
            '[data-a-color="price"] .a-offscreen',
            '.a-price-whole',
        ]

        for selector in price_selectors:
            price_elem = soup.select_one(selector)
            if price_elem:
                product_data['price'] = clean_price(price_elem.get_text())
                product_data['price_value'] = parse_price(price_elem.get_text())[0]
                break
        
        # Extract image URL
        image_selectors = [
            '#landingImage',
            '#imgBlkFront',
            '.a-dynamic-image',
            '[data-old-hires]'
        ]
        
        for selector in image_selectors:
            img_elem = soup.select_one(selector)
            if img_elem:
                img_url = img_elem.get('src') or img_elem.get('data-old-hires')
                if img_url:
                    if img_url.startswith('//'):
                        img_url = 'https:' + img_url
                    product_data['image_url'] = img_url
                    break
        
        # Extract rating
        rating_selectors = [
            '.a-icon-alt',
            '[data-hook="rating-out-of-text"]',
            '.a-icon-star-small .a-icon-alt'
        ]
        
        for selector in rating_selectors:
            rating_elem = soup.select_one(selector)
            if rating_elem:
                rating_text = rating_elem.get_text()
                rating_match = re.search(r'(\d+\.?\d*)', rating_text)
                if rating_match:
                    product_data['rating'] = f"{rating_match.group(1)} out of 5"
                    break
        
        # Extract reviews count
        reviews_selectors = [
            '#acrCustomerReviewText',
            '[data-hook="total-review-count"]',
            '.a-size-base.s-underline-text'
        ]
        
        for selector in reviews_selectors:
            reviews_elem = soup.select_one(selector)
            if reviews_elem:
                reviews_text = reviews_elem.get_text()
                reviews_match = re.search(r'(\d+(?:,\d+)*)', reviews_text)
                if reviews_match:
                    product_data['reviews_count'] = f"{reviews_match.group(1)} reviews"
                    break
        
        # Extract availability
        availability_selectors = [
            '#availability .a-size-medium',
            '#availability span',
            '.a-size-medium.a-color-success'
        ]
        
        for selector in availability_selectors:
            avail_elem = soup.select_one(selector)
            if avail_elem:
                product_data['availability'] = avail_elem.get_text().strip()
                break

        # "残り2点" as a number, so the caller can warn about thin stock instead
        # of hoping someone reads the sentence.
        left = re.search(r'残り\s*(\d+)\s*点', product_data['availability'])
        if left:
            product_data['stock_left'] = int(left.group(1))
        
        # Extract delivery date ("無料配送 ○月○日 ...").
        # This is the single most decision-relevant field when the caller has a
        # deadline, and it is NOT present in `availability` (which only says
        # 在庫あり / 残りN点). Amazon renders it in a few different slots
        # depending on layout, so try each.
        delivery_selectors = [
            '#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE',
            '#deliveryBlockMessage',
            '#delivery-block-message',
            '#ddmDeliveryMessage',
            '#mir-layout-DELIVERY_BLOCK',
        ]
        for selector in delivery_selectors:
            elem = soup.select_one(selector)
            if elem:
                text = ' '.join(elem.get_text().split())
                if text:
                    product_data['delivery'] = text[:160]
                    product_data['delivery_parsed'] = parse_delivery_text(text)
                    break

        # "最も早い配送" (paid/express) line, when Amazon offers one.
        fastest_selectors = [
            '#mir-layout-DELIVERY_BLOCK-slot-SECONDARY_DELIVERY_MESSAGE_LARGE',
            '#fastest-delivery-message',
            '#fastestDeliveryMessage',
        ]
        for selector in fastest_selectors:
            elem = soup.select_one(selector)
            if elem:
                text = ' '.join(elem.get_text().split())
                if text:
                    product_data['fastest_delivery'] = text[:160]
                    product_data['fastest_parsed'] = parse_delivery_text(text)
                    break

        # Shipping method. Global-store / marketplace listings often render no
        # delivery-date block at all in the server HTML; "出品者による発送"
        # here is itself the answer to "why is this one slow?".
        ship_elem = soup.select_one('#DELIVERY_JP') or soup.select_one('#shippingMessageInsideBuyBox_feature_div')
        if ship_elem:
            text = ' '.join(ship_elem.get_text().split())
            if text:
                product_data['ship_method'] = text[:120]

        # Which address the delivery estimate was computed for. Without a signed-in
        # session Amazon picks a default region, so the caller must not read the
        # date as being for their own address.
        addr_elem = soup.select_one('#contextualIngressPtLabel_deliveryShortLine')
        if addr_elem:
            text = ' '.join(addr_elem.get_text().split())
            text = text.replace('お届け先の更新', '').replace('-', ' ').strip()
            if text:
                product_data['delivery_address'] = text[:80]

        # Ships-from / sold-by. A marketplace reseller (無名業者) is the usual
        # cause of inflated prices, 並行輸入品 and missing warranty, so surface
        # it next to the price rather than making the caller guess.
        for attr, key in (('出荷元', 'ships_from'), ('販売元', 'sold_by')):
            elem = soup.select_one(f'.tabular-buybox-text[tabular-attribute-name="{attr}"]')
            if elem:
                text = ' '.join(elem.get_text().split())
                if text and text != attr:
                    product_data[key] = text[:80]

        if not product_data['sold_by']:
            seller_elem = soup.select_one('#sellerProfileTriggerId')
            if seller_elem:
                product_data['sold_by'] = ' '.join(seller_elem.get_text().split())[:80]

        if not product_data['ships_from'] and not product_data['sold_by']:
            merchant = soup.select_one('#merchant-info')
            if merchant:
                text = ' '.join(merchant.get_text().split())
                if text:
                    product_data['sold_by'] = text[:160]

        # ASIN / brand. ASIN normalises the URL; brand is the first thing a
        # caller checks when telling a real product from a relabelled clone.
        m = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', url)
        if m:
            product_data['asin'] = m.group(1)
        else:
            asin_elem = soup.select_one('input#ASIN')
            if asin_elem and asin_elem.get('value'):
                product_data['asin'] = asin_elem.get('value')

        byline = soup.select_one('#bylineInfo')
        if byline:
            brand = ' '.join(byline.get_text().split())
            brand = re.sub(r'^(ブランド|Brand)\s*[:：]\s*', '', brand)
            product_data['brand'] = brand[:80]

        # Shipping fee. The headline price is not the price you pay; a ￥999
        # 配送料 changes the ranking between two near-identical listings.
        buybox = soup.select_one('#desktop_buybox') or soup.select_one('#rightCol') or soup
        buybox_text = ' '.join(buybox.get_text().split())
        fee = re.search(r'配送料\s*[￥¥]\s*([\d,]+)', buybox_text)
        if fee:
            product_data['shipping_fee'] = f"￥{fee.group(1)}"
            product_data['shipping_fee_value'] = int(fee.group(1).replace(',', ''))
        elif '無料配送' in buybox_text or '配送料無料' in buybox_text:
            product_data['shipping_fee'] = '無料'
            product_data['shipping_fee_value'] = 0

        # What the caller actually pays. Comparing headline prices alone ranked a
        # ￥9,900 + ￥999 shipping listing above a ￥10,500 free-shipping one.
        if product_data['price_value'] is not None:
            product_data['total_value'] = (
                product_data['price_value'] + (product_data['shipping_fee_value'] or 0)
            )

        pts = re.search(r'ポイント[:：]?\s*([\d,]+)\s*pt', buybox_text)
        if pts:
            product_data['points'] = f"{pts.group(1)}pt"

        # Import / overseas-dispatch notice. A listing fulfilled from abroad can
        # show no delivery date at all, which is exactly how a "在庫あり" item
        # ends up arriving weeks later.
        #
        # Scope this to the buy box + delivery block only. Amazon repeats
        # "海外配送" in site-wide navigation, so scanning the whole page marks
        # every listing as an import.
        notice_scope = buybox_text
        for sel in ('#mir-layout-DELIVERY_BLOCK', '#deliveryBlockContainer', '#merchant-info'):
            elem = soup.select_one(sel)
            if elem:
                notice_scope += ' ' + ' '.join(elem.get_text().split())
        for phrase in ('関税・輸入手数料デポジット', '輸入関税', '海外から発送',
                       'この商品は海外', '輸入品'):
            if phrase in notice_scope:
                product_data['import_note'] = phrase
                break
        # Global Store listings are sold by an overseas Amazon entity; that is
        # the reliable signal, not the wording.
        if not product_data['import_note']:
            sold = product_data.get('sold_by') or ''
            if re.match(r'^Amazon\s+(US|UK|DE|FR|IT|ES|CN|CA)\b', sold):
                product_data['import_note'] = f'海外拠点からの販売 ({sold})'

        # All feature bullets, not just the first one. The single-bullet version
        # silently hid the specs that decide a purchase (PoE class, NTSC/PAL,
        # memory slots, lens focal length).
        bullets = []
        for li in soup.select('#feature-bullets .a-list-item'):
            t = ' '.join(li.get_text().split())
            if t and t not in bullets:
                bullets.append(t)
        product_data['features'] = bullets[:15]

        # Technical spec tables (メーカー / 型番 / 寸法 / 発売日 ...). This is the
        # only place on the page where numbers are stated rather than marketed.
        specs = {}
        for table in soup.select('#productDetails_techSpec_section_1, '
                                 '#productDetails_techSpec_section_2, '
                                 '#productDetails_detailBullets_sections1, '
                                 'table.a-keyvalue'):
            for row in table.select('tr'):
                key_el = row.select_one('th')
                val_el = row.select_one('td')
                if key_el and val_el:
                    k = ' '.join(key_el.get_text().split())
                    v = ' '.join(val_el.get_text().split())
                    if k and v and k not in specs:
                        specs[k] = v[:120]
        # Newer layout renders the same data as a bullet list instead of a table.
        for li in soup.select('#detailBullets_feature_div li'):
            t = ' '.join(li.get_text().replace('‎', '').replace('‏', '').split())
            if ':' in t or '：' in t:
                k, _, v = t.replace('：', ':').partition(':')
                k, v = k.strip(), v.strip()
                if k and v and k not in specs:
                    specs[k] = v[:120]
        product_data['specs'] = dict(list(specs.items())[:30])

        # Additional images. Judging build quality (metal vs plastic, connector
        # types, mounting bracket) needs more than the hero shot.
        images = []
        hero = product_data.get('image_url')
        if hero and hero != 'Image not found':
            images.append(hero)
        for img in soup.select('#altImages img'):
            src = img.get('src')
            if not src:
                continue
            # Thumbnails are ..._SS40_.jpg; strip the size modifier for full size.
            full = re.sub(r'\._[^.]+_\.', '.', src)
            if full not in images:
                images.append(full)
        product_data['images'] = images[:6]

        # Extract description (full block, not the first paragraph only)
        desc_elem = soup.select_one('#productDescription')
        if desc_elem:
            product_data['description'] = ' '.join(desc_elem.get_text().split())[:1500]
        else:
            for selector in ('#productDescription p', '.a-expander-content p'):
                elem = soup.select_one(selector)
                if elem:
                    product_data['description'] = ' '.join(elem.get_text().split())[:1500]
                    break
            else:
                if bullets:
                    product_data['description'] = bullets[0][:1500]

    except Exception as e:
        product_data['error'] = f"Error parsing product data: {str(e)}"
    
    return product_data

# Helper functions for search results

def extract_search_results(html_content: str, max_results: int) -> list:
    """Extract product information from Amazon search results"""
    soup = BeautifulSoup(html_content, 'html.parser')
    products = []
    
    # Find product containers
    product_containers = soup.select('[data-component-type="s-search-result"]')
    
    for container in product_containers[:max_results]:
        try:
            product = {
                'name': 'Product name not found',
                'price': 'Price not available',
                'price_value': None,
                'image_url': 'Image not found',
                'rating': 'Rating not available',
                'reviews_count': None,
                'sponsored': False,
                'delivery': None,
                'delivery_parsed': {},
                'stock_left': None,
                'asin': None,
                'url': 'URL not found'
            }

            # Extract product name
            name_elem = container.select_one('a h2 span')
            if name_elem:
                product['name'] = name_elem.get_text().strip()

            # Extract product URL.
            # Prefer the ASIN (data-asin) to build a clean /dp/ URL instead of
            # the noisy sponsored-click redirect links.
            asin = container.get('data-asin')
            if asin:
                product['asin'] = asin
                product['url'] = f"{BASE_URL}/dp/{asin}"
            else:
                url_elem = container.select_one('a')
                if url_elem:
                    product_url = url_elem.get('href')
                    if product_url:
                        if product_url.startswith('/'):
                            product_url = BASE_URL + product_url
                        product['url'] = product_url

            # Extract price. Prefer the full offscreen price (handles all
            # layouts); fall back to the whole-number span.
            price_elem = (
                container.select_one('.a-price .a-offscreen')
                or container.select_one('.a-price-whole')
            )
            if price_elem:
                product['price'] = clean_price(price_elem.get_text())
                pv = re.sub(r'[^\d]', '', price_elem.get_text())
                product['price_value'] = int(pv) if pv else None

            # Extract image
            img_elem = container.select_one('img.s-image')
            if img_elem:
                img_url = img_elem.get('src')
                if img_url:
                    product['image_url'] = img_url

            # Extract rating (stars). Amazon renders "5つ星のうち4.2" (JP) or
            # "4.2 out of 5 stars" (EN); a naive \d+ grabs the leading "5", so
            # pull the number that follows "のうち" / precedes "out of".
            rating_elem = container.select_one('.a-icon-alt')
            if rating_elem:
                rt = rating_elem.get_text()
                m = re.search(r'のうち\s*([\d.]+)', rt) or re.search(r'([\d.]+)\s*out of', rt)
                if m:
                    product['rating'] = f"{m.group(1)} out of 5"

            # Extract number of ratings — a thin/reseller listing usually has
            # very few, an established product has many. Key signal for the
            # caller to judge trustworthiness. Use the specific underline span
            # only, and sanity-cap to avoid picking up concatenated price digits.
            reviews_elem = container.select_one('span.a-size-base.s-underline-text')
            if reviews_elem:
                rc = re.sub(r'[^\d]', '', reviews_elem.get_text())
                if rc and int(rc) <= 9_999_999:
                    product['reviews_count'] = int(rc)

            # Sponsored / ad flag (down-rank these — they are paid placement,
            # not organic relevance or price). Match on the ad markup only; the
            # old fallback scanned the first 120 characters of body text and so
            # dropped any product whose own title mentioned スポンサー.
            label = container.select_one('.puis-sponsored-label-text') \
                or container.select_one('.s-sponsored-label-text') \
                or container.select_one('[aria-label="スポンサー"]') \
                or container.select_one('[data-component-type="sp-sponsored-result"]')
            if label:
                product['sponsored'] = True

            # Delivery / availability hint (e.g. "無料配送 ○月○日").
            delivery_elem = container.select_one('[data-cy="delivery-recipe"]')
            if delivery_elem:
                text = ' '.join(delivery_elem.get_text().split())[:100]
                product['delivery'] = text
                product['delivery_parsed'] = parse_delivery_text(text)

            # Thin stock is a scheduling risk, not a detail.
            stock = re.search(r'残り\s*(\d+)\s*点', container.get_text())
            if stock:
                product['stock_left'] = int(stock.group(1))

            products.append(product)

        except Exception as e:
            print(f"Error extracting product data: {str(e)}")

    return products

# Formatting functions

def format_search_results(products: list, query: str, note: str = "") -> str:
    """Format search results for display"""
    if not products:
        # Say *why* there is nothing. An empty list after filtering is a very
        # different fact from "Amazon sells nothing matching this".
        msg = f"No products found for '{query}'"
        if note:
            msg += f"\n_{note}_\nフィルタを緩めるか、pages を増やして再検索してください。"
        return msg

    result = f"# Search Results for '{query}'\n"
    if note:
        result += f"_{note}_\n"
    result += "\n"
    for i, product in enumerate(products):
        tag = " [スポンサー]" if product.get('sponsored') else ""
        result += f"## {i+1}. {product['name']}{tag}\n"
        result += f"Price: {product['price']}\n"
        rc = product.get('reviews_count')
        rating = product['rating']
        if rc is not None:
            result += f"Rating: {rating} ({rc:,} 件)\n"
        else:
            result += f"Rating: {rating}\n"
        if product.get('delivery'):
            result += f"Delivery: {format_delivery(product['delivery'], product.get('delivery_parsed') or {})}\n"
        if product.get('stock_left') is not None and product['stock_left'] <= 5:
            result += f"!! Low stock: {product['stock_left']} left\n"
        result += f"URL: {product['url']}\n\n"

    return result

def format_product_details(product: dict) -> str:
    """Format product details for display"""
    result = f"# {product['name']}\n\n"
    if product.get('asin'):
        result += f"ASIN: {product['asin']}\n"
    if product.get('brand'):
        result += f"Brand: {product['brand']}\n"
    result += f"Price: {product['price']}\n"
    if product.get('shipping_fee'):
        result += f"Shipping fee: {product['shipping_fee']}\n"
    if product.get('total_value') is not None and product.get('shipping_fee_value'):
        result += f"Total (price + shipping): ￥{product['total_value']:,}\n"
    if product.get('points'):
        result += f"Points: {product['points']}\n"
    result += f"Rating: {product['rating']}\n"
    result += f"Reviews: {product['reviews_count']}\n"
    result += f"Availability: {product['availability']}\n"
    if product.get('stock_left') is not None and product['stock_left'] <= 5:
        result += f"!! Low stock: {product['stock_left']} left\n"
    if product.get('delivery'):
        result += f"Delivery: {format_delivery(product['delivery'], product.get('delivery_parsed') or {})}\n"
    if product.get('fastest_delivery'):
        result += f"Fastest delivery: {format_delivery(product['fastest_delivery'], product.get('fastest_parsed') or {})}\n"
    if not product.get('delivery') and product.get('ship_method'):
        result += f"Delivery: (date not shown) {product['ship_method']}\n"
    elif product.get('ship_method'):
        result += f"Ship method: {product['ship_method']}\n"
    if product.get('delivery_address'):
        result += f"Estimate for: {product['delivery_address']}\n"
    if product.get('ships_from'):
        result += f"Ships from: {product['ships_from']}\n"
    if product.get('sold_by'):
        result += f"Sold by: {product['sold_by']}\n"
    if product.get('import_note'):
        result += f"Import notice: {product['import_note']}\n"

    if product.get('specs'):
        result += "\n## Specifications\n"
        for k, v in product['specs'].items():
            result += f"- {k}: {v}\n"

    if product.get('features'):
        result += "\n## Feature bullets\n"
        for f in product['features']:
            result += f"- {f}\n"

    result += f"\nDescription: {product['description']}\n"

    if product.get('images'):
        result += "\n## Images\n"
        for img in product['images']:
            result += f"- {img}\n"

    result += f"\nURL: {product['url']}\n"

    return result

# Tools

@mcp.tool()
async def scrape_product(product_url: str, postal_code: str = "") -> str:
    """Scrape product information from an Amazon product URL.

    Returns everything needed to choose between listings:
      - identity: name, ASIN, brand
      - cost: price, shipping fee, points
      - trust: rating, review count, ships-from / sold-by, import notice
      - schedule: availability, delivery date, fastest delivery, ship method
      - substance: full specification table, all feature bullets, description
      - images: hero image plus the alternate shots

    Pass `postal_code` (e.g. "120-0002") to pin delivery estimates to the real
    delivery address. Without it Amazon quotes its default region, which looks
    authoritative and is wrong. The reported "Estimate for:" line always states
    the address the date actually came from.
    """
    try:
        # Validate URL
        parsed_url = urlparse(product_url)
        if 'amazon' not in parsed_url.netloc.lower():
            return "ERROR: Please provide a valid Amazon product URL"

        html_content = await fetch_amazon_page(product_url, postal_code=postal_code)
        product_data = extract_product_data(html_content, product_url)
        return postal_warning(postal_code) + format_product_details(product_data)

    except CaptchaBlocked as e:
        return f"ERROR: {e}"
    except httpx.HTTPStatusError as e:
        return f"ERROR HTTP: {e.response.status_code} - {e.response.reason_phrase}"
    except httpx.RequestError as e:
        return f"ERROR request: {str(e)}"
    except Exception as e:
        return f"ERROR scraping product: {str(e)}"


@mcp.tool()
async def compare_products(product_urls: list[str], postal_code: str = "") -> str:
    """Fetch several Amazon products at once and show them side by side.

    Choosing between listings is the normal case, and doing it one call at a
    time multiplies round trips (and the chance of tripping the bot check).
    This fetches each product with a pause between requests, reuses the cache,
    and prints a comparison table (total cost, delivery, seller, stock) before
    the full details.

    Args:
        product_urls: Amazon product URLs or bare ASINs.
        postal_code: delivery postal code for accurate delivery estimates.
    """
    if not product_urls:
        return "ERROR: no product URLs given"

    rows: list[dict] = []
    errors: list[str] = []
    for i, raw_url in enumerate(product_urls[:10]):
        url = raw_url.strip()
        if re.fullmatch(r'[A-Z0-9]{10}', url):
            url = f"{BASE_URL}/dp/{url}"
        try:
            html = await fetch_amazon_page(url, postal_code=postal_code)
            rows.append(extract_product_data(html, url))
        except CaptchaBlocked as e:
            errors.append(f"{url}: {e}")
        except Exception as e:
            errors.append(f"{url}: {type(e).__name__}: {e}")
        if i < len(product_urls) - 1:
            await asyncio.sleep(1.0)

    if not rows:
        return "ERROR: could not fetch any product\n" + "\n".join(errors)

    out = "# Comparison\n\n"
    out += "| # | 商品 | 価格 | 送料 | 合計 | 到着 | 発送/販売 | 在庫 |\n"
    out += "|---|---|---:|---:|---:|---|---|---|\n"
    for i, p in enumerate(rows, 1):
        name = (p['name'][:38] + '…') if len(p['name']) > 39 else p['name']
        total = f"￥{p['total_value']:,}" if p.get('total_value') is not None else '-'
        parsed = p.get('delivery_parsed') or {}
        if parsed:
            eta = f"{parsed['date']} ({parsed['days']}日後)"
        else:
            eta = '日付表示なし'
        seller = p.get('sold_by') or p.get('ship_method') or '-'
        stock = (f"残り{p['stock_left']}" if p.get('stock_left') is not None
                 else p.get('availability', '-'))
        out += (f"| {i} | {name} | {p['price']} | {p.get('shipping_fee') or '-'} | "
                f"{total} | {eta} | {seller[:20]} | {stock[:12]} |\n")

    if errors:
        out += "\n取得できなかったもの:\n" + "\n".join(f"- {e}" for e in errors) + "\n"

    out += "\n---\n\n"
    for p in rows:
        out += format_product_details(p) + "\n---\n\n"
    return out

_SORT_MAP = {
    'relevance': 'relevanceblender',
    'featured': 'relevanceblender',
    'price_asc': 'price-asc-rank',
    'price_desc': 'price-desc-rank',
    'review': 'review-rank',
    'newest': 'date-desc-rank',
}


@mcp.tool()
async def search_products(
    query: str,
    max_results: int = 5,
    sort: str = "relevance",
    min_price: int = 0,
    max_price: int = 0,
    hide_sponsored: bool = False,
    pages: int = 1,
    max_delivery_days: int = 0,
    postal_code: str = "",
) -> str:
    """Search for products on Amazon and return results.

    Args:
        query: search keywords.
        max_results: how many results to return.
        sort: one of relevance | price_asc | price_desc | review | newest.
            Use price_asc to bypass sponsored/inflated listings that Amazon's
            default (featured) ordering surfaces first.
        min_price: minimum price in yen (0 = no minimum).
        max_price: maximum price in yen (0 = no maximum). Combine with a sort to
            pin a realistic price band, e.g. min_price=25000 max_price=45000.
        hide_sponsored: drop paid ad placements from the results.
        pages: how many result pages to walk (1 page is roughly 16-24 rows).
            Raise this for an exhaustive sweep; one page is not "everything".
        max_delivery_days: keep only rows arriving within this many days.
            0 disables the filter. Rows with no stated date are kept and marked,
            because "no date" usually means an overseas seller, not "fast".
        postal_code: delivery postal code (e.g. "120-0002"). Delivery estimates
            are address-dependent; without this you get Amazon's default region.
    """
    try:
        # Construct search URL with optional sort + price filters.
        from urllib.parse import quote_plus
        params = [f"k={quote_plus(query)}"]
        s = _SORT_MAP.get(sort.lower().strip())
        if s:
            params.append(f"s={s}")
        if min_price and min_price > 0:
            params.append(f"low-price={int(min_price)}")
        if max_price and max_price > 0:
            params.append(f"high-price={int(max_price)}")
        base_query = "&".join(params)

        # Walk result pages. Over-fetch per page so post-filtering still leaves
        # enough rows, and dedupe by ASIN because Amazon repeats listings.
        raw: list[dict] = []
        seen: set[str] = set()
        page_count = max(1, min(int(pages), 10))
        for page in range(1, page_count + 1):
            url = f"{BASE_URL}/s?{base_query}" + (f"&page={page}" if page > 1 else "")
            html_content = await fetch_amazon_page(url, postal_code=postal_code)
            rows = extract_search_results(html_content, 60)
            if not rows:
                break
            for r in rows:
                key = r.get('asin') or r.get('url')
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                raw.append(r)
            if page < page_count:
                await asyncio.sleep(1.0)

        if hide_sponsored:
            raw = [p for p in raw if not p.get('sponsored')]

        # Re-apply the price band ourselves. Amazon quietly ignores low-price /
        # high-price for some queries, which used to let out-of-band rows through.
        dropped_price = 0
        if min_price or max_price:
            kept = []
            for p in raw:
                v = p.get('price_value')
                if v is None:
                    kept.append(p)
                    continue
                if min_price and v < min_price:
                    dropped_price += 1
                    continue
                if max_price and v > max_price:
                    dropped_price += 1
                    continue
                kept.append(p)
            raw = kept

        dropped_slow = 0
        if max_delivery_days and max_delivery_days > 0:
            kept = []
            for p in raw:
                parsed = p.get('delivery_parsed') or {}
                if not parsed:
                    kept.append(p)  # unknown date: keep, the caller must judge
                elif parsed['days'] <= max_delivery_days:
                    kept.append(p)
                else:
                    dropped_slow += 1
            raw = kept

        products = raw[:max_results]

        # Build a one-line note describing the effective query.
        bits = []
        if s and sort.lower().strip() not in ('relevance', 'featured'):
            bits.append(f"sort={sort}")
        if min_price:
            bits.append(f">= ￥{int(min_price):,}")
        if max_price:
            bits.append(f"<= ￥{int(max_price):,}")
        if hide_sponsored:
            bits.append("スポンサー除外")
        if page_count > 1:
            bits.append(f"{page_count}ページ走査")
        if max_delivery_days:
            bits.append(f"{max_delivery_days}日以内着")
        if postal_code:
            bits.append(f"〒{postal_code}")
        note = ("フィルタ: " + ", ".join(bits)) if bits else ""
        if dropped_price or dropped_slow:
            extras = []
            if dropped_price:
                extras.append(f"価格帯外 {dropped_price}件")
            if dropped_slow:
                extras.append(f"納期超過 {dropped_slow}件")
            note += (" / " if note else "") + "除外: " + ", ".join(extras)

        return postal_warning(postal_code) + format_search_results(products, query, note)

    except CaptchaBlocked as e:
        return f"ERROR: {e}"
    except Exception as e:
        return f"ERROR searching products: {str(e)}"


if __name__ == "__main__":
    # stdout IS the protocol channel for stdio transport. Anything printed here
    # lands in the middle of the JSON-RPC stream and the client drops the
    # connection, so status messages must go to stderr.
    print("Starting Amazon Products MCP server...", file=sys.stderr)
    mcp.run(transport="stdio") 