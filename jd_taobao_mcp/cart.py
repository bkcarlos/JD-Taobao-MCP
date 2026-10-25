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


def _quantity(value: Any) -> int | None:
    """Keep provider quantities distinct from line counts and unknown values."""
    return value if type(value) is int and 1 <= value <= 1_000_000 else None


def _nested_details(raw: dict[str, Any], depth: int = 0) -> tuple[list, list, bool]:
    """Read bundle components and displayed gift groups without unrelated metadata."""
    if depth >= 4:
        return [], [], True
    components, gifts = [], []
    partial = any(raw.get(key) for key in ("Gifts", "giftPools", "manGifts", "sipGifts"))
    children = raw.get("items") or []
    groups = raw.get("giftGroupInfosShow") or []
    if not isinstance(children, list) or not isinstance(groups, list):
        return [], [], True
    for entry in children[:100]:
        child = entry.get("item") if isinstance(entry, dict) else None
        if not isinstance(child, dict):
            partial = True
            continue
        sku = str(child.get("Id", ""))
        title = _text(child.get("Name"), 500)
        if not re.fullmatch(r"[0-9]{1,30}", sku) or not title:
            partial = True
            continue
        nested, child_gifts, incomplete = _nested_details(child, depth + 1)
        partial |= incomplete
        # Raw Price can be the standalone reference price, not the bundle allocation.
        price_text = _text(child.get("PriceShow"), 80)
        components.append({"product_url": product_link(f"https://item.jd.com/{sku}.html"),
                           "title": title, "quantity": _quantity(child.get("Num")),
                           "unit_price": _money(price_text), "price_text": price_text,
                           "components": nested, "gifts": child_gifts})
    for group in groups[:100]:
        rows = group.get("giftInfos") if isinstance(group, dict) else None
        if not isinstance(rows, list):
            partial = True
            continue
        group_quantity = 0
        for gift in rows[:100]:
            if not isinstance(gift, dict) or gift.get("couponInfo"):
                partial = True
                continue
            sku = str(gift.get("id", ""))
            title = _text(gift.get("name"), 500)
            quantity = _quantity(gift.get("giftNum"))
            if not re.fullmatch(r"[0-9]{1,30}", sku) or not title or quantity is None:
                partial = True
                continue
            group_quantity += quantity
            gifts.append({"product_url": product_link(f"https://item.jd.com/{sku}.html"),
                          "title": title, "quantity": quantity})
        expected = group.get("giftNum")
        partial |= len(rows) > 100 or (type(expected) is int and expected != group_quantity)
    return components, gifts, partial or len(children) > 100 or len(groups) > 100


def _match_raw_item(row: dict, vendor: dict, raw_vendors: Any) -> dict | None:
    """Join the public merchandise projection to its raw parent, never to a child SKU."""
    if not isinstance(raw_vendors, list) or vendor.get("vendorId") is None:
        return None
    matches = []
    for raw_vendor in raw_vendors:
        if not isinstance(raw_vendor, dict) or raw_vendor.get("vendorId") != vendor["vendorId"]:
            continue
        entries = raw_vendor.get("sorted")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            raw = entry.get("item") if isinstance(entry, dict) else None
            if not isinstance(raw, dict):
                continue
            # JD uses the virtual SKU for a bundle, not its internal bundle Id.
            sku = raw.get("vskuId") if entry.get("itemType") == 4 else raw.get("Id")
            if str(sku) == str(row.get("skuId")):
                matches.append(raw)
    exact = [raw for raw in matches if row.get("skuUuid") and raw.get("skuUuid") == row["skuUuid"]]
    matches = exact or matches
    return matches[0] if len(matches) == 1 else None


def _nested_counts(items: list[dict]) -> tuple[int, int, int]:
    """Count child rows and gift units without adding bundle contents to parent units."""
    components = gift_count = gift_quantity = 0
    for item in items:
        children, gifts = item.get("components", []), item.get("gifts", [])
        nested_components, nested_gifts, nested_quantity = _nested_counts(children)
        components += len(children) + nested_components
        gift_count += len(gifts) + nested_gifts
        gift_quantity += sum(gift["quantity"] for gift in gifts) + nested_quantity
    return components, gift_count, gift_quantity


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
    cart_info = result.get("cartInfo")
    raw_vendors = cart_info.get("vendors") if isinstance(cart_info, dict) else None
    details_complete = True
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
            quantity = _quantity(row.get("num"))
            tags = row.get("propertyTags")
            variant = " ".join(_text(v, 100) for k, v in list(tags.items())[:10] if k not in {"c", "d"}) if isinstance(tags, dict) else ""
            price_text = _text(row.get("priceShow"), 80)
            raw = _match_raw_item(row, vendor, raw_vendors)
            components, gifts, incomplete = _nested_details(raw) if raw is not None else ([], [], True)
            if row.get("itemType") == 4 and not components:
                incomplete = True
            details_complete &= not incomplete
            items.append({
                "product_url": product_link(f"https://item.jd.com/{sku}.html"),
                "title": title, "variant": variant.strip(), "quantity": quantity,
                "unit_price": _money(price_text if price_text else row.get("price")),
                "price_text": price_text, "price_label": _text(row.get("priceDes"), 80),
                "selected": row.get("checked") if type(row.get("checked")) is bool else None,
                "shop": _text(vendor.get("title"), 120),
                "kind": "bundle" if row.get("itemType") == 4 else "product",
                "components": components, "gifts": gifts,
            })
    if not items and not empty:
        return cart_failure("unrecognized_cart_response")
    if empty and items:
        return cart_failure("unrecognized_cart_response")
    page = business.get("page")
    pages = page.get("totalPageNum") if isinstance(page, dict) else None
    reported_count = cart_info.get("cartShowNum") if isinstance(cart_info, dict) else None
    count_mismatch = type(reported_count) is int and reported_count != len(items)
    partial |= (not empty and (type(pages) is not int or pages != 1)) or len(items) > max_items
    partial |= not details_complete or count_mismatch
    balance = business.get("balanceInfo")
    selected_total = _money(balance.get("priceShow")) if isinstance(balance, dict) else None
    returned = items[:max_items]
    component_count, gift_count, gift_quantity = _nested_counts(returned)
    return {
        "success": True, "platform": "jd", "status": "partial" if partial else "empty" if empty else "ok",
        "items": returned, "count": len(returned), "partial": partial,
        "main_quantity": sum(i["quantity"] for i in returned) if all(i["quantity"] is not None for i in returned) else None,
        "bundle_count": sum(i["kind"] == "bundle" for i in returned),
        "component_count": component_count, "gift_count": gift_count, "gift_quantity": gift_quantity,
        "details_complete": details_complete,
        "selected_total": selected_total, "currency": "CNY",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "note": "count 是主条目数，套装计为一个主条目；components 是套装内明细，gifts 是附属赠品。gift_count 是赠品明细条数，gift_quantity 是赠品件数。必须同时报告主条目和赠品；不能把 count 称为全部商品件数。组件价格未知时不能用单买参考价替代套装分摊价。selected_total 仅为已勾选商品估价，不重复加上组件或赠品。partial=true 或 details_complete=false 时不得称列表完整；最终实付以结算页为准。",
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
