#!/usr/bin/env python3
"""crawl.py — 站点级爬取"""
import json, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse
from fetch import fetch_page

def crawl_sitemap(url, max_pages=20, timeout=10):
    """从 sitemap.xml 爬取"""
    sitemap_url = urljoin(url, '/sitemap.xml')
    result = fetch_page(sitemap_url, max_chars=50000, timeout=timeout, raw=True)
    if not result['success']:
        # 尝试 robots.txt 指定的 sitemap
        robots_url = urljoin(url, '/robots.txt')
        robots_result = fetch_page(robots_url, max_chars=10000, timeout=timeout, raw=True)
        if robots_result['success'] and 'Sitemap:' in robots_result.get('html', ''):
            import re as _re
            sitemap_urls = _re.findall(r'Sitemap:\s*(.+)', robots_result['html'])
            if sitemap_urls:
                sitemap_url = sitemap_urls[0].strip()
                result = fetch_page(sitemap_url, max_chars=50000, timeout=timeout, raw=True)
    if not result.get('success') or not result.get('html'):
        return {'url': url, 'pages': [], 'total': 0, 'error': 'sitemap not found or empty'}
    html = result.get('html', result.get('content', ''))
    # 同时尝试从 raw content 提取（兼容 XML 被 ContentExtractor 过滤的情况）
    urls = re.findall(r'<loc>\s*(.*?)\s*</loc>', html)
    if not urls:
        urls = re.findall(r'<loc>(.*?)</loc>', result.get('content', ''))
    urls = urls[:max_pages]
    pages = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(fetch_page, u, 2000, timeout): u for u in urls}
        for fut in as_completed(futures, timeout=timeout*2):
            try:
                r = fut.result()
                if r['success']:
                    pages.append({'url': r['url'], 'content': r['content'][:500], 'depth': 0})
            except: pass
    return {'url': url, 'pages': pages, 'total': len(pages), 'elapsed_ms': int((time.time())*1000)}

def crawl_bfs(url, max_pages=10, max_depth=2, timeout=8):
    """BFS 爬取"""
    visited = set()
    pages = []
    queue = [(url, 0)]
    while queue and len(pages) < max_pages:
        current_url, depth = queue.pop(0)
        if current_url in visited or depth > max_depth:
            continue
        visited.add(current_url)
        result = fetch_page(current_url, 2000, timeout)
        if result['success']:
            pages.append({'url': current_url, 'content': result['content'][:500], 'depth': depth})
            links = re.findall(r'href=["\']([^"\'#]+)', result['content'])
            for link in links[:5]:
                full = urljoin(current_url, link)
                if urlparse(full).netloc == urlparse(url).netloc and full not in visited:
                    queue.append((full, depth+1))
    return {'url': url, 'pages': pages, 'total': len(pages)}

if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('url')
    p.add_argument('--strategy', default='bfs', choices=['sitemap','bfs'])
    p.add_argument('--max-pages', type=int, default=10)
    p.add_argument('--max-depth', type=int, default=2)
    args = p.parse_args()
    if args.strategy == 'sitemap':
        r = crawl_sitemap(args.url, args.max_pages)
    else:
        r = crawl_bfs(args.url, args.max_pages, args.max_depth)
    print(json.dumps(r, ensure_ascii=False, indent=2)[:2000])
