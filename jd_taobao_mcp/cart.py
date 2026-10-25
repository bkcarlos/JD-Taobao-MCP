"""Isolated, field-limited reads of the signed-in JD cart."""
from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qs

from playwright.async_api import BrowserContext, Error as PlaywrightError, Route

from .extractors.helpers import compact_text
from .safety import _parse_https, product_link

JD_CART_URL = "https://cart.jd.com/cart_index"
CART_FUNCTION = "pcCart_jc_getCurrentCart"


def cart_request_allowed(url: str, *, method: str, resource_type: str,
                         main_document: bool, post_data: str = "") -> bool:
    """Permit the fixed cart query and static assets, never cart mutations."""
    parsed = _parse_https(url)
    if parsed is None:
        return False
    if resource_type == "document":
        return main_document and method == "GET" and url == JD_CART_URL
    if parsed.hostname == "api.m.jd.com":
        # Check both sources together so duplicate/conflicting function IDs fail closed.
        if post_data and ("=" not in post_data or post_data.lstrip().startswith(("{", "["))):
            return False
        query = parse_qs(parsed.query, keep_blank_values=True)
        body = parse_qs(post_data, keep_blank_values=True)
        functions = query.get("functionId", []) + body.get("functionId", [])
        return (parsed.path == "/api" and method == "POST"
                and resource_type in {"xhr", "fetch"} and functions == [CART_FUNCTION])
    return (method == "GET" and resource_type in {"script", "stylesheet", "image", "font"}
            and (parsed.hostname == "360buyimg.com" or parsed.hostname.endswith(".360buyimg.com")))


def cart_failure(reason: str) -> dict[str, Any]:
    """Return fixed failure metadata instead of provider errors or account data."""
    return {"success": False, "platform": "jd", "status": reason, "items": [], "count": 0,
            "requires_login": reason == "login_required"}


def _text(value: Any, limit: int) -> str:
    return compact_text(value, limit) if isinstance(value, str) else ""


def _money(value: Any) -> float | None:
    """Accept a complete monetary amount, never numbers embedded in arbitrary text."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = str(value).strip()
    if not re.fullmatch(r"[¥￥]?\s*(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?", text):
        return None
    try:
        amount = Decimal(text.lstrip("¥￥").strip().replace(",", ""))
        return float(amount) if 0 <= amount <= 1_000_000_000 else None
    except InvalidOperation:
        return None


def normalize_cart(payload: Any, max_items: int) -> dict[str, Any]:
    """Select cart merchandise fields; discard account, address and raw API metadata."""
    if not isinstance(payload, dict) or payload.get("success") is not True or payload.get("code") != 0:
        return cart_failure("cart_unavailable")
    result = payload.get("resultData")
    business = payload.get("businessData")
    if not isinstance(result, dict) or not isinstance(business, dict):
        return cart_failure("unrecognized_cart_response")
    error = result.get("errInfo")
    if isinstance(error, dict) and error.get("errCode", 0) != 0:
        return cart_failure("cart_unavailable")
    cart_page = business.get("cartPage")
    vendors = cart_page.get("vendorList") if isinstance(cart_page, dict) else None
    empty = result.get("isEmptyCart") is True
    if not isinstance(vendors, list):
        if not empty:
            return cart_failure("unrecognized_cart_response")
        vendors = []
    items = []
    partial = False
    for vendor in vendors:
        rows = vendor.get("items") if isinstance(vendor, dict) else None
        if not isinstance(rows, list):
            partial = True
            continue
        for row in rows:
            if not isinstance(row, dict):
                partial = True
                continue
            sku = str(row.get("skuId", ""))
            title = _text(row.get("name"), 500)
            if not re.fullmatch(r"[0-9]{1,30}", sku) or not title:
                partial = True
                continue
            quantity = row.get("num")
            if type(quantity) is not int or not 1 <= quantity <= 1_000_000:
                quantity = None
            tags = row.get("propertyTags")
            variant = " ".join(_text(v, 100) for k, v in list(tags.items())[:10] if k not in {"c", "d"}) if isinstance(tags, dict) else ""
            price_text = _text(row.get("priceShow"), 80)
            items.append({
                "product_url": product_link(f"https://item.jd.com/{sku}.html"),
                "title": title, "variant": variant.strip(), "quantity": quantity,
                "unit_price": _money(price_text if price_text else row.get("price")),
                "price_text": price_text, "price_label": _text(row.get("priceDes"), 80),
                "selected": row.get("checked") if type(row.get("checked")) is bool else None,
                "shop": _text(vendor.get("title"), 120),
            })
    if not items and not empty:
        return cart_failure("unrecognized_cart_response")
    if empty and items:
        return cart_failure("unrecognized_cart_response")
    page = business.get("page")
    pages = page.get("totalPageNum") if isinstance(page, dict) else None
    partial |= (not empty and (type(pages) is not int or pages > 1)) or len(items) > max_items
    balance = business.get("balanceInfo")
    selected_total = _money(balance.get("priceShow")) if isinstance(balance, dict) else None
    return {
        "success": True, "platform": "jd", "status": "partial" if partial else "empty" if empty else "ok",
        "items": items[:max_items], "count": min(len(items), max_items), "partial": partial,
        "selected_total": selected_total, "currency": "CNY",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "note": "只读购物车。selected_total 仅为已勾选商品的页面估价；最终实付以结算页为准。partial=true 表示未返回全部条目。",
    }


async def read_jd_cart(parent: BrowserContext, max_items: int, timeout_ms: int) -> dict[str, Any]:
    """Read in an ephemeral context with only the cart query allowed, then close it."""
    if parent.browser is None:
        return cart_failure("browser_unavailable")
    context = await parent.browser.new_context(
        locale="zh-CN", timezone_id="Asia/Shanghai", accept_downloads=False,
        service_workers="block", viewport={"width": 1440, "height": 1000},
    )
    completed = asyncio.get_running_loop().create_future()
    try:
        await context.add_cookies(await parent.cookies("https://cart.jd.com"))
        page = await context.new_page()

        async def guard(route: Route) -> None:
            request = route.request
            is_cart_query = request.resource_type in {"xhr", "fetch"}
            if not cart_request_allowed(request.url, method=request.method,
                                        resource_type=request.resource_type,
                                        main_document=request.frame == page.main_frame,
                                        post_data=request.post_data or ""):
                with suppress(PlaywrightError):
                    await route.abort("blockedbyclient")
                return
            response = None
            try:
                response = await route.fetch(max_redirects=0, timeout=timeout_ms)
                if 300 <= response.status < 400:
                    await route.abort("blockedbyclient")
                    return
                if is_cart_query and not completed.done():
                    body = await response.body()
                    if response.status != 200 or len(body) > 5_000_000:
                        data = cart_failure("cart_unavailable")
                    else:
                        try:
                            data = normalize_cart(json.loads(body), max_items)
                        except (ValueError, TypeError):
                            data = cart_failure("unrecognized_cart_response")
                    if not completed.done():
                        completed.set_result(data)
                await route.fulfill(response=response)
            except PlaywrightError:
                with suppress(PlaywrightError):
                    await route.abort("failed")
            finally:
                if response is not None:
                    with suppress(PlaywrightError):
                        await response.dispose()

        await context.route("**/*", guard)
        await context.route_web_socket("**/*", lambda socket: socket.close())
        async with asyncio.timeout(timeout_ms / 1000):
            await page.goto(JD_CART_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            return await completed
    except (TimeoutError, PlaywrightError):
        return cart_failure("cart_unavailable")
    finally:
        completed.cancel()
        await context.close()
