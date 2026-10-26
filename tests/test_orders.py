"""Synthetic order projections, session redirects and read-only boundary tests."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from jd_taobao_mcp.config import Settings
from jd_taobao_mcp.orders import (ORDER_FUNCTION, auth_url_allowed, normalize_orders,
                                 order_request_allowed, query_page, sync_order_session)
from jd_taobao_mcp.service import ShoppingBrowserService
from jd_taobao_mcp.safety import SafetyError, is_readable_url


def fixture(page=1, total=21):
    """Generate fake orders including unrelated private fields and differing amounts."""
    return {"success": True, "data": {"totalNum": total, "orderList": [
        {"orderId": str(1000+i), "dateSubmit": "2026-01-02 03:04:05",
         "consigneeInfo": {"customerName": "FAKE_PERSON", "mobile": "FAKE_PHONE", "address": "FAKE_ADDRESS"},
         "statusInfo": {"orderState": 18, "orderStateName": "已完成"},
         "itemPayInfo": {"fistPrice": "25.00", "fistTxt": "总额", "price": 999, "shouldPay": 7},
         "wareList": [{"name": "Example item", "num": 2, "productId": 123, "giftWare": False},
                      {"name": "Example gift", "num": 1, "productId": 456, "giftWare": True}],
         "operateButtonList": [{"buttonUrl": "https://private.example/FAKE_TOKEN"}]}
        for i in range((page-1)*20, min(page*20, total))],
        "jimiOrderMap": {"FAKE_ID": {"receiverInfo": "FAKE_PRIVATE"}}}}


class OrderContractTests(unittest.TestCase):
    def test_only_selected_fields_are_returned_and_amounts_keep_their_label(self):
        result, identifiers = normalize_orders(fixture())
        self.assertEqual(len(result["orders"]), 20)
        self.assertEqual(result["reported_total"], 21)
        self.assertFalse(result["partial"])
        row = result["orders"][0]
        self.assertEqual(row["amount"], 25)
        self.assertEqual(row["amount_label"], "总额")
        self.assertEqual(row["placed_at"], "2026-01-02T03:04:05+08:00")
        self.assertTrue(row["items"][1]["is_gift"])
        self.assertEqual(identifiers[0], "1000")
        self.assertNotIn("FAKE_", json.dumps(result))
        self.assertNotIn("orderId", json.dumps(result))

    def test_unknown_amount_is_not_replaced_by_another_payment_field(self):
        data = fixture(total=1)
        row = data["data"]["orderList"][0]
        row["itemPayInfo"].pop("fistPrice")
        row["statusInfo"] = {"orderState": -1, "orderStateName": "已取消"}
        row["wareList"][0].update(num=True, giftWare="false", productId=0)
        result, _ = normalize_orders(data)
        self.assertIsNone(result["orders"][0]["amount"])
        self.assertEqual(result["orders"][0]["status"], "已取消")
        self.assertIsNone(result["orders"][0]["items"][0]["quantity"])
        self.assertIsNone(result["orders"][0]["items"][0]["product_url"])
        self.assertTrue(result["partial"])

    def test_virtual_order_status_uses_only_the_provider_status_label(self):
        data = fixture(total=1)
        data["data"]["orderList"][0]["statusInfo"] = {"orderState": 18}
        data["data"]["jimiOrderMap"] = {"1000": {"orderStatus": {"name": "充值成功"},
                                                "receiverInfo": {"name": "FAKE_PERSON"}}}
        result, _ = normalize_orders(data)
        self.assertEqual(result["orders"][0]["status"], "充值成功")
        self.assertNotIn("FAKE_", json.dumps(result))

    def test_empty_invalid_and_missing_merchandise_are_distinct(self):
        self.assertTrue(normalize_orders(fixture(total=0))[0]["success"])
        for payload in (None, {}, {"success": False}, {"success": True, "data": {"totalNum": 1, "orderList": []}}):
            self.assertFalse(normalize_orders(payload)[0]["success"])
        data = fixture(total=1)
        data["data"]["orderList"][0]["wareList"] = []
        self.assertTrue(normalize_orders(data)[0]["partial"])

    def test_query_rejects_mutations_duplicates_wrong_periods_and_page_limits(self):
        url = f"https://api.m.jd.com/api?functionId={ORDER_FUNCTION}"
        body = urlencode({"body": json.dumps({"page": 1, "s": 4096, "d": 1})})
        self.assertEqual(query_page(url, body), 1)
        self.assertEqual(query_page(url.replace("api.m.jd.com", "color.jd.hk"), body), 1)
        for invalid in (url.replace(ORDER_FUNCTION, "pcorderop_cancelOrder"),
                        url+"&functionId=pcorder_center_deleteOrder", url.replace("/api?", "/other?"),
                        url.replace("api.m.jd.com", "evil.example")):
            self.assertIsNone(query_page(invalid, body))
        for args in ({"page": 0, "s": 4096, "d": 1}, {"page": 6, "s": 4096, "d": 1},
                     {"page": True, "s": 4096, "d": 1}, {"page": 1, "s": 4096, "d": 2},
                     {"page": 1, "s": 4096, "d": 1, "orderId": "FAKE_ID"}):
            self.assertIsNone(query_page(url, urlencode({"body": json.dumps(args)})))
        for value in ('{"page":1,"page":2,"s":4096,"d":1}', '[["page",1],["s",4096],["d",1]]'):
            self.assertIsNone(query_page(url, urlencode({"body": value})))
        self.assertIsNone(query_page(url, body+"&functionId=pcorder_center_deleteOrder"))

    def test_documents_and_login_sync_cannot_escape_to_other_account_endpoints(self):
        base = "https://order.jd.com/center/list.action"
        self.assertFalse(is_readable_url(base))
        self.assertTrue(auth_url_allowed(base))
        self.assertTrue(auth_url_allowed("https://passport.jd.com/new/login.aspx?"+urlencode({"ReturnUrl": base})))
        self.assertTrue(auth_url_allowed("https://sso.jd.hk/sso/sync/redirect?"+urlencode({"ReturnUrl": base, "c": "FAKE_TOKEN"})))
        for invalid in (base+"?delete=1", "https://order.jd.com/pay", "https://user:pass@order.jd.com/center/list.action",
                        "https://passport.jd.com/new/login.aspx?"+urlencode({"ReturnUrl": "https://evil.example"})):
            self.assertFalse(auth_url_allowed(invalid))
        doc = base+"?d=1&s=4096&page=1"
        def allowed(url, method="GET", kind="document", main=True):
            return order_request_allowed(url, method=method, resource_type=kind, main_document=main,
                                         document_url=doc, page_number=1)
        self.assertTrue(allowed(doc))
        self.assertFalse(allowed(doc, main=False))
        self.assertFalse(allowed(base+"?d=1&s=4096&page=2"))
        self.assertFalse(allowed("https://collector.example/pixel", kind="image"))
        self.assertTrue(allowed("https://storage.360buyimg.com/fixture.js", kind="script"))


class OrderAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_sync_inspects_every_redirect_and_never_exports_tokens(self):
        destinations = ["https://order.jd.hk/center/list.action",
            "https://passport.jd.com/new/login.aspx?"+urlencode({"ReturnUrl": "https://order.jd.hk/center/list.action"}),
            "https://sso.jd.hk/sso/sync/redirect?"+urlencode({"ReturnUrl": "https://order.jd.hk/center/list.action", "c": "FAKE_TOKEN"}),
            "https://order.jd.hk/center/list.action"]
        responses = [SimpleNamespace(status=302, headers={"location": u}, dispose=AsyncMock()) for u in destinations]
        responses.append(SimpleNamespace(status=200, headers={}, dispose=AsyncMock()))
        context = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(side_effect=responses)))
        result = await sync_order_session(context, 1000)
        self.assertEqual(result, (destinations[0], "ok"))
        for call in context.request.get.call_args_list:
            self.assertEqual(call.kwargs["max_redirects"], 0)
        for response in responses:
            response.dispose.assert_awaited_once()
        context.request.get = AsyncMock(return_value=SimpleNamespace(status=302,
            headers={"location": "https://evil.example/FAKE_TOKEN"}, dispose=AsyncMock()))
        self.assertEqual(await sync_order_session(context, 1000), (None, "orders_unavailable"))
        context.request.get.assert_awaited_once()

    async def test_limits_login_and_active_human_session_are_enforced(self):
        service = ShoppingBrowserService(Settings.from_env())
        browser = service.jd_browser
        browser.get_orders = AsyncMock(return_value={"success": True})
        for platform, count in (("taobao", 10), ("jd", True), ("jd", 0), ("jd", 101)):
            with self.assertRaises(ValueError):
                await service.get_orders(platform, count)
        browser.get_orders.assert_not_awaited()
        del browser.get_orders
        browser._active_page_unlocked = AsyncMock()
        browser._context = SimpleNamespace(cookies=AsyncMock(return_value=[]))
        with patch("jd_taobao_mcp.browser.read_jd_orders", new_callable=AsyncMock) as read:
            self.assertEqual((await browser.get_orders(10))["status"], "login_required")
            import time
            browser.login_until = time.monotonic()+60
            with self.assertRaises(SafetyError):
                await browser.get_orders(10)
            read.assert_not_awaited()
