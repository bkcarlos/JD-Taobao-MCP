"""Synthetic followed-product projections and account read boundaries."""
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from jd_taobao_mcp.config import Settings
from jd_taobao_mcp.followed import (FOLLOW_FUNCTION, follow_query_page, followed_request_allowed,
                                   normalize_followed)
from jd_taobao_mcp.safety import SafetyError, is_readable_url
from jd_taobao_mcp.service import ShoppingBrowserService


def query(page=1, **args):
    """Build the observed query using fake public metadata only."""
    return 'https://api.m.jd.com/api?' + urlencode({'functionId': FOLLOW_FUNCTION,
        'body': json.dumps({'pageNo': page, 'pageSize': 20, 'source': 'pc_car_follow_product', **args})})


def fixture(page=1, total=21):
    """Generate fake products with unrelated account fields that must be discarded."""
    return {'success': True, 'content': [
        {'skuId': str(1000+i), 'skuName': f'Example product {i}', 'jdPrice': '25.00',
         'productPriceVO': {'finalPriceVO': {'estimatedPrice': '20.50', 'title': '到手价'}},
         'account': 'FAKE_ACCOUNT', 'skuImage': 'https://private.example/FAKE_TOKEN'}
        for i in range((page-1)*20, min(page*20, total))], 'pin': 'FAKE_ACCOUNT'}


class FollowedContractTests(unittest.TestCase):
    def test_projection_preserves_price_labels_and_excludes_private_fields(self):
        result = normalize_followed(fixture())
        self.assertEqual(len(result['items']), 20)
        self.assertEqual(result['items'][0], {'title': 'Example product 0',
            'product_url': 'https://item.jd.com/1000.html', 'price': 20.5, 'price_label': '到手价'})
        self.assertNotIn('FAKE_', json.dumps(result))
        self.assertFalse(result['partial'])
        self.assertFalse(result['empty_page'])

    def test_invalid_unknown_zero_and_base_prices_remain_distinct(self):
        data = fixture(total=4)
        rows = data['content']
        rows[0]['productPriceVO']['finalPriceVO']['estimatedPrice'] = '0.00'
        rows[1]['productPriceVO'] = None
        rows[2].update(jdPrice='model 123', productPriceVO=[])
        rows[3]['skuId'] = 'javascript:evil()'
        result = normalize_followed(data)
        self.assertEqual([r['price'] for r in result['items']], [0, 25, None])
        self.assertEqual(result['items'][1]['price_label'], '京东价')
        self.assertIsNone(result['items'][2]['price_label'])
        self.assertTrue(result['partial'])
        self.assertFalse(result['empty_page'])

    def test_errors_malformed_rows_and_empty_list_are_not_confused(self):
        for payload in (None, {}, {'success': False}, {'success': True, 'content': None}):
            self.assertFalse(normalize_followed(payload)['success'])
        self.assertEqual(normalize_followed({'code': -14})['status'], 'login_required')
        self.assertTrue(normalize_followed(fixture(total=0))['empty_page'])
        result = normalize_followed({'success': True, 'content': [None, {}, False]})
        self.assertTrue(result['partial'])
        self.assertFalse(result['empty_page'])

    def test_query_rejects_mutations_filters_duplicates_and_unbounded_pagination(self):
        self.assertEqual(follow_query_page(query()), 1)
        for page in (0, 7, True, '1'):
            self.assertIsNone(follow_query_page(query(page)))
        for url in (query(tag=1), query(pageSize=True), query(source='other'),
                    query().replace(FOLLOW_FUNCTION, 'pcCart_jc_follow'),
                    query()+'&functionId=cancelFollow', query()+'&callback=execute',
                    query()+'&body=%7B%7D', query().replace('api.m.jd.com', 'evil.example'),
                    query().replace('/api?', '/other?'), query()+'#fragment'):
            self.assertIsNone(follow_query_page(url))
        for body in ('{"pageNo":1,"pageNo":2,"pageSize":20,"source":"pc_car_follow_product"}',
                     '[["pageNo",1],["pageSize",20],["source","pc_car_follow_product"]]',
                     '{"pageNo":[],"pageSize":20,"source":"pc_car_follow_product"}'):
            self.assertIsNone(follow_query_page('https://api.m.jd.com/api?'+urlencode({
                'functionId': FOLLOW_FUNCTION, 'body': body})))

    def test_resource_policy_keeps_account_routes_and_writes_closed(self):
        def allowed(url, method='GET', kind='fetch', main=True, body=''):
            return followed_request_allowed(url, method=method, resource_type=kind,
                main_document=main, page_number=1, post_data=body)
        self.assertTrue(allowed(query()))
        self.assertFalse(allowed(query(), method='POST'))
        self.assertFalse(allowed(query(), body='functionId=unfollow'))
        self.assertFalse(allowed(query(2)))
        self.assertFalse(allowed(query(), kind='document'))
        self.assertFalse(allowed('https://t.jd.com/follow/product', kind='document'))
        self.assertFalse(is_readable_url('https://t.jd.com/home/follow'))
        self.assertTrue(allowed('https://cart.jd.com/cart_index', kind='document'))
        self.assertFalse(allowed('https://cart.jd.com/cart_index', kind='document', main=False))
        self.assertTrue(allowed('https://storage.360buyimg.com/example.js', kind='script'))
        self.assertFalse(allowed('https://collector.example/leak', kind='image'))


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
