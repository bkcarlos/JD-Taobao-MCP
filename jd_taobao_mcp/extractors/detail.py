from __future__ import annotations

from typing import Any

from playwright.async_api import Page
from ..safety import ensure_product_url, image_link, public_url

from .helpers import compact_text, normalize_url, parse_price


async def extract_product_detail(page: Page, platform: str) -> dict[str, Any]:
    ensure_product_url(page.url)
    selectors = _selectors(platform)
    raw = await page.evaluate(
        r"""
        (selectors) => {
          const clean = (value, max = 500) => {
            const text = String(value || '').replace(/\s+/g, ' ').trim();
            return max && text.length > max ? text.slice(0, max - 1) + '…' : text;
          };
          const firstText = (items) => {
            for (const selector of items) {
              const el = document.querySelector(selector);
              const text = el?.innerText?.trim() || el?.getAttribute?.('content')?.trim();
              if (text) return text;
            }
            return '';
          };
          const jsonLd = [];
          for (const node of document.querySelectorAll('script[type="application/ld+json"]')) {
            try { jsonLd.push(JSON.parse(node.textContent || 'null')); } catch (_) {}
          }
          const specs = [];
          for (const selector of selectors.specs) {
            for (const el of document.querySelectorAll(selector)) {
              const text = (el.innerText || '').trim();
              if (text && text.length < 500) specs.push(text);
              if (specs.length >= 80) break;
            }
            if (specs.length >= 80) break;
          }
          const productParameters = [];
          const parameterSeen = new Set();
          const addParameter = (name, value, group = '') => {
            name = clean(name, 120).replace(/[：:]\s*$/, '');
            value = clean(value, 500);
            group = clean(group, 120);
            if (!name || !value || name === value) return;
            const key = `${group}\u0000${name}\u0000${value}`;
            if (parameterSeen.has(key)) return;
            parameterSeen.add(key);
            productParameters.push({name, value, group});
          };
          const splitParameterText = (text) => {
            text = clean(text, 600);
            const index = text.search(/[：:]/);
            if (index <= 0 || index >= text.length - 1) return null;
            return [text.slice(0, index), text.slice(index + 1)];
          };
          for (const selector of selectors.specs) {
            for (const el of document.querySelectorAll(selector)) {
              const pair = splitParameterText(el.innerText || '');
              if (pair) addParameter(pair[0], pair[1]);
              if (productParameters.length >= 100) break;
            }
            if (productParameters.length >= 100) break;
          }
          for (const section of document.querySelectorAll('.Ptable-item, [class*="Ptable"], [class*="parameter"], [class*="Parameter"]')) {
            const group = clean(section.querySelector('h3, h4, .Ptable-tit')?.innerText || '');
            for (const row of section.querySelectorAll('dl, tr, li')) {
              const name = clean(row.querySelector('dt, th, .name, [class*="name"]')?.innerText || '');
              const value = clean(row.querySelector('dd, td, .value, [class*="value"]')?.innerText || '');
              if (name && value) addParameter(name, value, group);
              const pair = splitParameterText(row.innerText || '');
              if (pair) addParameter(pair[0], pair[1], group);
              if (productParameters.length >= 100) break;
            }
            if (productParameters.length >= 100) break;
          }
          const detailParameters = [];
          const detailParameterSeen = new Set();
          const addDetailParameter = (name, value, group = '商品详情') => {
            name = clean(name, 120).replace(/[：:]\s*$/, '');
            value = clean(value, 500);
            group = clean(group, 120) || '商品详情';
            if (!name || !value || name === value) return;
            const key = `${group}\u0000${name}\u0000${value}`;
            if (detailParameterSeen.has(key)) return;
            detailParameterSeen.add(key);
            detailParameters.push({name, value, group});
          };
          const detailRoots = [
            ...document.querySelectorAll('#detail, #J-detail, .detail, .goods-detail, [class*="detail"], [class*="Detail"]')
          ];
          const rootSeen = new Set();
          for (const root of detailRoots) {
            if (!root || rootSeen.has(root)) continue;
            rootSeen.add(root);
            const rootText = clean(root.innerText || root.textContent || '', 3000);
            if (!rootText.includes('商品详情') && !root.querySelector('.Ptable, .Ptable-item, .p-parameter-list, .parameter2')) continue;
            for (const section of root.querySelectorAll('.Ptable-item, .Ptable, .p-parameter, .parameter2, .p-parameter-list')) {
              const group = clean(section.querySelector('h3, h4, .Ptable-tit')?.innerText || '商品详情');
              for (const row of section.querySelectorAll('dl, tr, li')) {
                const name = clean(row.querySelector('dt, th, .name, [class*="name"]')?.innerText || '');
                const value = clean(row.querySelector('dd, td, .value, [class*="value"]')?.innerText || '');
                if (name && value) addDetailParameter(name, value, group);
                const pair = splitParameterText(row.innerText || '');
                if (pair) addDetailParameter(pair[0], pair[1], group);
                if (detailParameters.length >= 100) break;
              }
              if (detailParameters.length >= 100) break;
            }
            if (detailParameters.length >= 100) break;
          }
          const textOf = (root, selectorList) => {
            for (const selector of selectorList) {
              const el = root.querySelector(selector);
              const text = clean(el?.innerText || el?.textContent || '', 1200);
              if (text) return text;
            }
            return '';
          };
          const helpfulCount = (text) => {
            text = clean(text, 2000);
            const patterns = [
              /(\d+)\s*(?:人)?(?:点赞|赞|有用|认为有用)/,
              /(?:点赞|赞|有用|认为有用)\D{0,8}(\d+)/
            ];
            for (const pattern of patterns) {
              const match = text.match(pattern);
              if (match) return Number(match[1]);
            }
            return null;
          };
          const positiveTerms = ['好用', '满意', '推荐', '不错', '很好', '超香', '方便', '简单', '值得', '性价比', '喜欢', '大品牌', '香甜', '粒粒分明', '完美', '省心', '治愈', '强烈推荐', '闭眼冲', '稳定', '灵敏', '流畅', '可爱', '实用'];
          const negativeTerms = ['不满意', '不好用', '差评', '很差', '垃圾', '失望', '退货', '坏了', '破损', '有异味', '夹生饭', '溢锅', '太慢了', '噪音大', '漏水', '不推荐', '难用', '踩雷', '虚假', '欺骗', '套路'];
          const negationPhrases = ['后悔没有早点', '没有异味', '无异味', '不粘锅', '不夹生', '噪音小', '噪音低', '不费力'];
          const isTrulyNegative = (text, term) => {
            const index = text.indexOf(term);
            if (index < 0) return false;
            if (negationPhrases.some((phrase) => text.includes(phrase))) return false;
            const before = text.slice(Math.max(0, index - 3), index);
            if (/[没无非少小零]$/.test(before)) return false;
            return true;
          };
          const reviewNodes = [];
          const reviewNodeSeen = new Set();
          for (const selector of selectors.review_items) {
            for (const node of document.querySelectorAll(selector)) {
              if (reviewNodeSeen.has(node)) continue;
              reviewNodeSeen.add(node);
              reviewNodes.push(node);
              if (reviewNodes.length >= 80) break;
            }
            if (reviewNodes.length >= 80) break;
          }
          const reviews = [];
          const reviewSeen = new Set();
          for (const node of reviewNodes) {
            const allText = clean(node.innerText || node.textContent || '', 2000);
            let content = textOf(node, selectors.review_content);
            if (!content) content = allText;
            const user = textOf(node, selectors.review_user);
            const time = textOf(node, selectors.review_time);
            const variant = textOf(node, selectors.review_variant);
            content = clean(content.replace(user, '').replace(time, '').replace(variant, ''), 800);
            if (content.length < 8) continue;
            const key = `${user}\u0000${content}`;
            if (reviewSeen.has(key)) continue;
            reviewSeen.add(key);
            const positive = positiveTerms.some((term) => content.includes(term));
            const negative = negativeTerms.some((term) => isTrulyNegative(content, term));
            reviews.push({
              user: clean(user, 80),
              content,
              helpful_count: helpfulCount(allText),
              time: clean(time, 80),
              variant: clean(variant, 160),
              sentiment: negative ? 'negative' : (positive ? 'positive' : '')
            });
          }
          const reviewSort = (a, b) => (b.helpful_count || 0) - (a.helpful_count || 0);
          const highPraiseReviews = reviews
            .filter((review) => review.sentiment === 'positive')
            .sort(reviewSort)
            .slice(0, 5);
          const highDissatisfiedReviews = reviews
            .filter((review) => review.sentiment === 'negative')
            .sort(reviewSort)
            .slice(0, 2);
          const images = [];
          const ogImage = document.querySelector('meta[property="og:image"]')?.content;
          if (ogImage) images.push(ogImage);
          for (const img of document.querySelectorAll('img')) {
            const src = img.getAttribute('data-origin') || img.getAttribute('data-src') || img.getAttribute('data-lazy-img') || img.src;
            const w = img.naturalWidth || img.width || 0;
            const h = img.naturalHeight || img.height || 0;
            if (src && (w >= 200 || h >= 200)) images.push(src);
            if (images.length >= 30) break;
          }
          return {
            title: firstText(selectors.title),
            price_text: firstText(selectors.price),
            shop: firstText(selectors.shop),
            review_text: firstText(selectors.reviews),
            sales_text: firstText(selectors.sales),
            specs,
            product_parameters: productParameters,
            detail_product_parameters: detailParameters,
            high_praise_reviews: highPraiseReviews,
            high_dissatisfied_reviews: highDissatisfiedReviews,
            images,
            json_ld: jsonLd,
            meta: {
              og_title: document.querySelector('meta[property="og:title"]')?.content || '',
              description: document.querySelector('meta[name="description"]')?.content || document.querySelector('meta[property="og:description"]')?.content || '',
              canonical: document.querySelector('link[rel="canonical"]')?.href || ''
            }
          };
        }
        """,
        selectors,
    )

    ensure_product_url(page.url)
    json_product = _find_product_json_ld(raw.get("json_ld", []))
    title = compact_text(
        raw.get("title")
        or raw.get("meta", {}).get("og_title")
        or json_product.get("name"),
        500,
    )
    price_text = compact_text(raw.get("price_text"), 120)
    if not price_text:
        offers = json_product.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if isinstance(offers, dict):
            price_text = compact_text(str(offers.get("price") or offers.get("lowPrice") or ""))

    images: list[str] = []
    json_images = json_product.get("image")
    if isinstance(json_images, str):
        images.append(json_images)
    elif isinstance(json_images, list):
        images.extend(str(item) for item in json_images if item)
    images.extend(str(item) for item in raw.get("images", []) if item)

    normalized_images: list[str] = []
    seen: set[str] = set()
    for image in images:
        normalized = image_link(normalize_url(page.url, image))
        if normalized and normalized not in seen:
            seen.add(normalized)
            normalized_images.append(normalized)
        if len(normalized_images) >= 15:
            break

    specs = []
    seen_specs: set[str] = set()
    for spec in raw.get("specs", []):
        cleaned = compact_text(spec, 500)
        if cleaned and cleaned not in seen_specs:
            seen_specs.add(cleaned)
            specs.append(cleaned)
        if len(specs) >= 50:
            break

    product_parameters = _merge_parameters(
        _normalize_parameters(raw.get("detail_product_parameters", [])),
        _normalize_parameters(raw.get("product_parameters", [])),
    )
    high_praise_reviews = _normalize_reviews(raw.get("high_praise_reviews", []), limit=5)
    high_dissatisfied_reviews = _normalize_reviews(
        raw.get("high_dissatisfied_reviews", []), limit=2
    )
    return {
        "platform": platform,
        "url": public_url(page.url),
        "product_url": public_url(page.url),
        "title": title,
        "price": parse_price(price_text),
        "price_text": price_text,
        "shop": compact_text(raw.get("shop"), 200),
        "review_text": compact_text(raw.get("review_text"), 120),
        "sales_text": compact_text(raw.get("sales_text"), 120),
        "description": compact_text(raw.get("meta", {}).get("description"), 1200),
        "canonical_url": public_url(page.url),
        "specifications": specs,
        "product_parameters": product_parameters,
        "high_praise_reviews": high_praise_reviews,
        "high_dissatisfied_reviews": high_dissatisfied_reviews,
        "images": normalized_images,
    }


def _selectors(platform: str) -> dict[str, list[str]]:
    if platform == "jd":
        return {
            "title": [".top-name", ".sku-name", "#name h1", "meta[property='og:title']"],
            "price": [
                ".product-price-panel .product-price--main",
                ".p-price .price",
                ".summary-price .price",
            ],
            "shop": [".name a", ".shop-name", "[class*='shopName']", "[class*='shop-name']"],
            "reviews": [".comment-count .count", "#comment-count", "[class*='comment']"],
            "sales": ["[class*='sales']", "[class*='deal']"],
            "specs": ["#detail .p-parameter-list li", ".parameter2 li", ".Ptable-item", "[class*='parameter'] li"],
            "review_items": [".comment-item", ".J-comment-item", "[class*='comment-item']", "[class*='CommentItem']"],
            "review_user": [".user-info", ".nickname", "[class*='user']", "[class*='User']"],
            "review_content": [".comment-con", ".comment-content", "[class*='content']", "[class*='Content']"],
            "review_time": [".order-info", ".comment-time", "[class*='time']", "[class*='Time']"],
            "review_variant": [".order-info", ".comment-column", "[class*='sku']", "[class*='Sku']"],
        }
    return {
        "title": ["h1", "[class*='Title']", "[class*='title'] h1", "meta[property='og:title']"],
        "price": [
            ".tb-rmb-num",
            "[class*='Price']",
            "[class*='price']",
            "[data-testid*='price']",
        ],
        "shop": [".shop-name", "[class*='Shop']", "[class*='shop'] [class*='name']"],
        "reviews": ["[class*='review']", "[class*='comment']"],
        "sales": ["[class*='sales']", "[class*='sold']", "[class*='deal']"],
        "specs": ["[class*='Parameter'] li", "[class*='parameter'] li", ".attributes-list li", ".tb-attributes li"],
        "review_items": ["[class*='review-item']", "[class*='comment-item']", "[class*='ReviewItem']", "[class*='CommentItem']"],
        "review_user": ["[class*='user']", "[class*='User']", "[class*='nick']", "[class*='Nick']"],
        "review_content": ["[class*='content']", "[class*='Content']", "[class*='review']", "[class*='comment']"],
        "review_time": ["[class*='time']", "[class*='Time']", "[class*='date']", "[class*='Date']"],
        "review_variant": ["[class*='sku']", "[class*='Sku']", "[class*='spec']", "[class*='Spec']"],
    }


def _normalize_parameters(items: list[Any]) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        name = compact_text(str(item.get("name") or ""), 120)
        value = compact_text(str(item.get("value") or ""), 500)
        group = compact_text(str(item.get("group") or ""), 120)
        if not name or not value:
            continue
        key = (group, name, value)
        if key in seen:
            continue
        seen.add(key)
        output.append({"name": name, "value": value, "group": group})
        if len(output) >= 80:
            break
    return output


def _normalize_reviews(items: list[Any], *, limit: int = 5) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        user = compact_text(str(item.get("user") or ""), 80)
        content = compact_text(str(item.get("content") or ""), 800)
        if not content:
            continue
        key = (user, content)
        if key in seen:
            continue
        seen.add(key)
        helpful_count = item.get("helpful_count")
        if not isinstance(helpful_count, int):
            helpful_count = None
        output.append(
            {
                "user": user,
                "content": content,
                "helpful_count": helpful_count,
                "time": compact_text(str(item.get("time") or ""), 80),
                "variant": compact_text(str(item.get("variant") or ""), 160),
            }
        )
        if len(output) >= limit:
            break
    return output


def _merge_parameters(*sources: list[dict[str, str]]) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    seen_names: set[str] = set()
    seen_pairs: set[tuple[str, str, str]] = set()
    for source in sources:
        for item in source:
            name = compact_text(item.get("name"), 120)
            value = compact_text(item.get("value"), 500)
            group = compact_text(item.get("group"), 120) or "商品详情"
            if not name or not value:
                continue
            key = (group, name, value)
            if key in seen_pairs or name in seen_names:
                continue
            seen_pairs.add(key)
            seen_names.add(name)
            output.append({"name": name, "value": value, "group": group})
            if len(output) >= 80:
                return output
    return output


def _find_product_json_ld(nodes: list[Any]) -> dict[str, Any]:
    queue: list[Any] = list(nodes)
    while queue:
        node = queue.pop(0)
        if isinstance(node, list):
            queue.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        node_type = node.get("@type")
        if node_type == "Product" or (
            isinstance(node_type, list) and "Product" in node_type
        ):
            return node
        graph = node.get("@graph")
        if isinstance(graph, list):
            queue.extend(graph)
    return {}
