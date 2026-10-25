"""Offline cart contracts and request-boundary regressions using fake data only."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from jd_taobao_mcp.cart import CART_FUNCTION, JD_CART_URL, cart_request_allowed, normalize_cart
from jd_taobao_mcp.safety import is_allowed_url, is_readable_url
from jd_taobao_mcp.service import ShoppingBrowserService
from jd_taobao_mcp.config import Settings


def fixture():
    """A synthetic response with private fields that must never reach the client."""
    return {
        "success": True, "code": 0,
        "resultData": {"isEmptyCart": False, "pin": "FAKE_ACCOUNT", "address": "FAKE_ADDRESS"},
        "businessData": {
            "page": {"totalPageNum": 1, "hashValue": "FAKE_SECRET"},
            "cartPage": {"vendorList": [{"title": "Example store", "items": [{
                "skuId": "123", "name": "Example product", "num": 2, "price": 49.50,
                "priceShow": "¥49.50", "priceDes": "优惠价", "checked": True,
                "propertyTags": {"color": "Black", "size": "Small", "c": "FAKE_INTERNAL"},
                "skuUuid": "FAKE_IDENTIFIER", "customInfoNew": {"name": "FAKE_PERSON"},
            }]}]},
            "balanceInfo": {"priceShow": "¥99.00", "address": "FAKE_ADDRESS"},
        },
    }


class CartContractTests(unittest.TestCase):
    def test_output_is_field_limited_and_amounts_are_not_multiplied_twice(self):
        result = normalize_cart(fixture(), 50)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["items"][0]["quantity"], 2)
        self.assertEqual(result["items"][0]["unit_price"], 49.5)
        self.assertEqual(result["items"][0]["variant"], "Black Small")
        self.assertEqual(result["selected_total"], 99.0)
        self.assertTrue(result["items"][0]["selected"])
        self.assertNotIn("FAKE_", json.dumps(result))

    def test_missing_or_conditional_prices_stay_unknown(self):
        for value in ("无货", "满300减50", "99起", "NaN", "-1", "1,2", True):
            data = fixture()
            row = data["businessData"]["cartPage"]["vendorList"][0]["items"][0]
            row.update(priceShow=value, price=None, num=True, checked="false")
            result = normalize_cart(data, 50)["items"][0]
            self.assertIsNone(result["unit_price"])
            self.assertIsNone(result["quantity"])
            self.assertIsNone(result["selected"])

    def test_empty_is_distinct_from_failed_or_unrecognized_responses(self):
        data = fixture()
        data["resultData"]["isEmptyCart"] = True
        data["businessData"]["cartPage"]["vendorList"] = []
        self.assertEqual(normalize_cart(data, 50)["status"], "empty")
        data["resultData"]["isEmptyCart"] = False
        self.assertFalse(normalize_cart(data, 50)["success"])
        for invalid in ({}, None, {"success": False, "message": "FAKE_ACCOUNT"},
                        {"success": True, "code": 0, "resultData": {}, "businessData": {}}):
            result = normalize_cart(invalid, 50)
            self.assertFalse(result["success"])
            self.assertNotIn("FAKE_", json.dumps(result))

    def test_partial_cart_marks_pagination_limits_and_skipped_rows(self):
        for reason in ("pagination", "limit", "unknown-row"):
            data = fixture()
            rows = data["businessData"]["cartPage"]["vendorList"][0]["items"]
            if reason == "pagination":
                data["businessData"]["page"]["totalPageNum"] = 2
            elif reason == "limit":
                rows.append(copy.deepcopy(rows[0]))
            else:
                rows.append({"skuId": "https://evil.example", "name": "Invalid"})
            result = normalize_cart(data, 1)
            self.assertTrue(result["partial"])
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["count"], 1)

    def test_query_policy_rejects_mutations_redirects_and_account_pages(self):
        api = "https://api.m.jd.com/api?functionId="
        def allowed(url, method="POST", resource_type="xhr", main=False, body=""):
            return cart_request_allowed(url, method=method, resource_type=resource_type,
                                        main_document=main, post_data=body)
        self.assertTrue(allowed(api + CART_FUNCTION))
        self.assertTrue(allowed(JD_CART_URL, "GET", "document", True))
        self.assertFalse(is_allowed_url(JD_CART_URL))
        self.assertFalse(is_readable_url(JD_CART_URL))
        for name in ("cartAdd", "cartRemove", "changeSkuNum", "cartCheckSingle", "cartCheckAll", "cartSwitch"):
            self.assertFalse(allowed(api + "pcCart_jc_" + name))
        for url in ("https://trade.jd.com/shopping/order/getOrderInfo.action", "https://order.jd.com/",
                    JD_CART_URL + "?action=delete", "http://cart.jd.com/cart_index",
                    "https://user:pass@cart.jd.com/cart_index", "https://cart.jd.com.evil.example/cart_index"):
            self.assertFalse(allowed(url, "GET", "document", True))
        self.assertFalse(allowed(JD_CART_URL, "GET", "document", False))
        self.assertFalse(allowed(api + CART_FUNCTION, "GET", "script"))
        self.assertFalse(allowed(api + CART_FUNCTION + "&functionId=pcCart_jc_cartRemove"))
        self.assertFalse(allowed(api + CART_FUNCTION, body="functionId=pcCart_jc_cartRemove"))
        self.assertFalse(allowed(api + CART_FUNCTION, body='{"functionId":"pcCart_jc_cartRemove"}'))
        self.assertFalse(allowed("https://collector.example/pixel", "GET", "image"))
        self.assertTrue(allowed("https://storage.360buyimg.com/fixture.js", "GET", "script"))


class CartServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_platform_and_limit_are_validated_before_browser_access(self):
        service = ShoppingBrowserService(Settings.from_env())
        service.jd_browser.get_cart = AsyncMock(return_value={"success": True})
        for platform, limit in (("taobao", 50), ("jd", 0), ("jd", 101), ("jd", True)):
            with self.assertRaises(ValueError):
                await service.get_cart(platform, limit)
        service.jd_browser.get_cart.assert_not_awaited()
        await service.get_cart("jd", 5)
        service.jd_browser.get_cart.assert_awaited_once_with(5)

    async def test_no_login_or_active_human_login_prevents_cart_read(self):
        import time
        from jd_taobao_mcp.safety import SafetyError
        service = ShoppingBrowserService(Settings.from_env())
        browser = service.jd_browser
        browser._active_page_unlocked = AsyncMock()
        browser._context = SimpleNamespace(cookies=AsyncMock(return_value=[]))
        with patch("jd_taobao_mcp.browser.read_jd_cart", new_callable=AsyncMock) as read:
            self.assertEqual((await browser.get_cart(50))["status"], "login_required")
            read.assert_not_awaited()
            browser.login_until = time.monotonic() + 60
            with self.assertRaises(SafetyError):
                await browser.get_cart(50)
            read.assert_not_awaited()
