#!/usr/bin/env python3
"""
轻量级本地搜索引擎 v3 — 24 个可用引擎
"""

import json
import os
import sys
import time
import urllib.request
import urllib.parse
import ssl
from pathlib import Path
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

# 禁用 SSL 证书验证
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

# 请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/json,application/xml",
    "Accept-Language": "en-US,en;q=0.9",
}

# 24 个可用引擎
ENGINES = {
    # 通用搜索（6 个）
    "bing": {"url": "https://www.bing.com/search?q={query}&count={n}", "parser": "html"},
    "duckduckgo": {"url": "https://api.duckduckgo.com/?q={query}&format=json", "parser": "json"},
    "google": {"url": "https://www.google.com/search?q={query}&num={n}", "parser": "html"},
    "mojeek": {"url": "https://www.mojeek.com/search?q={query}", "parser": "html"},
    "yandex": {"url": "https://yandex.com/search/?text={query}", "parser": "html"},
    "startpage": {"url": "https://www.startpage.com/do/search?q={query}", "parser": "html"},
    
    # 中文搜索（2 个）
    "baidu": {"url": "https://www.baidu.com/s?wd={query}", "parser": "html"},
    "sogou": {"url": "https://www.sogou.com/web?query={query}", "parser": "html"},
    
    # 学术搜索（5 个）
    "arxiv": {"url": "http://export.arxiv.org/api/query?search_query={query}&max_results={n}", "parser": "xml"},
    "pubmed": {"url": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term={query}&retmax={n}", "parser": "xml"},
    "crossref": {"url": "https://api.crossref.org/works?query={query}&rows={n}", "parser": "json"},
    "openalex": {"url": "https://api.openalex.org/works?search={query}&per_page={n}", "parser": "json"},
    "dblp": {"url": "https://dblp.org/search/publ/api?q={query}&format=json&h={n}", "parser": "json"},
    
    # 新闻搜索（3 个）
    "bing_news": {"url": "https://www.bing.com/news/search?q={query}&format=rss", "parser": "xml"},
    "google_news": {"url": "https://news.google.com/rss/search?q={query}", "parser": "xml"},
    "duckduckgo_news": {"url": "https://news.duckduckgo.com/news?q={query}", "parser": "html"},
    
    # 代码搜索（7 个）
    "github": {"url": "https://api.github.com/search/repositories?q={query}&per_page={n}", "parser": "json"},
    "stackoverflow": {"url": "https://api.stackexchange.com/2.3/search?order=desc&sort=relevance&intitle={query}&site=stackoverflow&pagesize={n}", "parser": "json"},
    "gitlab": {"url": "https://gitlab.com/api/v4/projects?search={query}&per_page={n}", "parser": "json"},
    "npm": {"url": "https://registry.npmjs.org/-/v1/search?text={query}&size={n}", "parser": "json"},
    "docker_hub": {"url": "https://hub.docker.com/v2/search/repositories/?query={query}&page_size={n}", "parser": "json"},
    "pypi": {"url": "https://pypi.org/search/?q={query}", "parser": "html"},
    "arch_wiki": {"url": "https://wiki.archlinux.org/api.php?action=opensearch&search={query}&limit={n}&format=json", "parser": "json"},
    
    # 百科/知识（3 个）
    "wikipedia": {"url": "https://en.wikipedia.org/api/rest_v1/page/summary/{query}", "parser": "json"},
    "wiktionary": {"url": "https://en.wiktionary.org/api/rest_v1/page/definition/{query}", "parser": "json"},
    "wikiquote": {"url": "https://en.wikiquote.org/w/api.php?action=query&titles={query}&prop=extracts&exintro=true&format=json", "parser": "json"},
    
    # 其他垂直（2 个）
    "imdb": {"url": "https://v2.sg.media-imdb.com/suggestion/{first_char}/{query}.json", "parser": "json"},
    "goodreads": {"url": "https://www.goodreads.com/search?q={query}", "parser": "html"},
}

def search_engine(engine_name: str, query: str, n: int = 5, timeout: int = 5) -> List[Dict]:
    """搜索单个引擎"""
    if engine_name not in ENGINES:
        return []
    
    config = ENGINES[engine_name]
    url = config["url"].replace("{query}", urllib.parse.quote(query)).replace("{n}", str(n))
    
    # 特殊处理
    if "{first_char}" in url:
        url = url.replace("{first_char}", query[0].lower())
    
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
            data = resp.read().decode("utf-8", errors="ignore")
            parser = config["parser"]
            
            if parser == "json":
                return parse_json(engine_name, data)
            elif parser == "xml":
                return parse_xml(engine_name, data)
            else:
                return parse_html(engine_name, data)
    except Exception:
        return []

def parse_json(engine_name: str, data: str) -> List[Dict]:
    """解析 JSON 响应"""
    try:
        json_data = json.loads(data)
        results = []
        
        if engine_name == "duckduckgo":
            if json_data.get("Abstract"):
                results.append({
                    "title": json_data.get("Heading", ""),
                    "url": json_data.get("AbstractURL", ""),
                    "snippet": json_data.get("Abstract", "")[:300],
                    "source": engine_name
                })
            for topic in json_data.get("RelatedTopics", [])[:5]:
                if isinstance(topic, dict) and topic.get("Text"):
                    results.append({
                        "title": topic.get("Text", "")[:100],
                        "url": topic.get("FirstURL", ""),
                        "snippet": topic.get("Text", "")[:300],
                        "source": engine_name
                    })
        
        elif engine_name == "github":
            for repo in json_data.get("items", [])[:5]:
                results.append({
                    "title": repo.get("full_name", ""),
                    "url": repo.get("html_url", ""),
                    "snippet": repo.get("description", "")[:300] if repo.get("description") else "",
                    "source": engine_name
                })
        
        elif engine_name == "stackoverflow":
            for item in json_data.get("items", [])[:5]:
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("link", ""),
                    "snippet": item.get("body", "")[:300] if item.get("body") else "",
                    "source": engine_name
                })
        
        elif engine_name == "crossref":
            for item in json_data.get("message", {}).get("items", [])[:5]:
                title = item.get("title", [""])[0] if item.get("title") else ""
                results.append({
                    "title": title[:200],
                    "url": item.get("URL", ""),
                    "snippet": item.get("abstract", "")[:300] if item.get("abstract") else "",
                    "source": engine_name
                })
        
        elif engine_name == "wikipedia":
            results.append({
                "title": json_data.get("title", ""),
                "url": json_data.get("content_urls", {}).get("desktop", {}).get("page", ""),
                "snippet": json_data.get("extract", "")[:300],
                "source": engine_name
            })
        
        elif engine_name == "openalex":
            for item in json_data.get("results", [])[:5]:
                title = item.get("display_name", "")
                landing = item.get("primary_location", {}) or {}
                url = landing.get("landing_page_url", "") if isinstance(landing, dict) else ""
                results.append({
                    "title": title[:200],
                    "url": url or f"https://openalex.org/{item.get('id','')}",
                    "snippet": (item.get("abstract_inverted_index") or {}).get("abstract", "")[:300] if isinstance(item.get("abstract_inverted_index"), dict) else "",
                    "source": engine_name
                })
        
        elif engine_name == "dblp":
            hits = json_data.get("result", {}).get("hits", {}).get("hit", [])
            for item in hits[:5]:
                info = item.get("info", {})
                results.append({
                    "title": (info.get("title", "") or "")[:200],
                    "url": (info.get("url", "") or ""),
                    "snippet": (info.get("venue", "") or "")[:300],
                    "source": engine_name
                })
        
        elif engine_name == "docker_hub":
            for item in json_data.get("results", [])[:5]:
                results.append({
                    "title": item.get("name", ""),
                    "url": f"https://hub.docker.com/r/{item.get('name','')}",
                    "snippet": (item.get("short_description", "") or "")[:300],
                    "source": engine_name
                })
        
        elif engine_name == "arch_wiki":
            # opensearch: [query, [titles], [descriptions], [urls]]
            titles = json_data[1] if len(json_data) > 1 and isinstance(json_data[1], list) else []
            urls = json_data[3] if len(json_data) > 3 and isinstance(json_data[3], list) else []
            for i, t in enumerate(titles[:5]):
                results.append({
                    "title": t[:200],
                    "url": urls[i] if i < len(urls) else "",
                    "snippet": "",
                    "source": engine_name
                })
        
        return results[:5]
    except:
        return []

def parse_xml(engine_name: str, data: str) -> List[Dict]:
    """解析 XML 响应"""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(data)
        results = []
        
        if engine_name == "arxiv":
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            for entry in root.findall(".//atom:entry", ns):
                title = entry.findtext("atom:title", "", ns).strip()
                summary = entry.findtext("atom:summary", "", ns).strip()
                entry_id = entry.findtext("atom:id", "", ns)
                if title:
                    results.append({
                        "title": title.replace("\n", " ")[:200],
                        "url": entry_id,
                        "snippet": summary.replace("\n", " ")[:300],
                        "source": "arxiv"
                    })
        
        elif engine_name == "pubmed":
            for id_elem in root.findall(".//Id"):
                results.append({
                    "title": f"PubMed ID: {id_elem.text}",
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{id_elem.text}/",
                    "snippet": "",
                    "source": "pubmed"
                })
        
        return results[:5]
    except:
        return []

def parse_html(engine_name: str, data: str) -> List[Dict]:
    """解析 HTML 响应（简化版）"""
    import re
    results = []
    
    # 提取标题和链接
    title_pattern = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>([^<]+)</a>', re.I)
    matches = title_pattern.findall(data)[:10]
    
    for url, title in matches:
        if title.strip() and len(title.strip()) > 5:
            results.append({
                "title": title.strip()[:200],
                "url": url,
                "snippet": "",
                "source": engine_name
            })
    
    return results[:5]

def detect_query_type(query: str) -> str:
    """检测查询类型"""
    chinese_chars = sum(1 for c in query if '\u4e00' <= c <= '\u9fff')
    if chinese_chars > len(query) * 0.3:
        return "chinese"
    
    academic_keywords = ["paper", "arxiv", "research", "study", "论文", "学术"]
    if any(kw in query.lower() for kw in academic_keywords):
        return "academic"
    
    news_keywords = ["news", "latest", "today", "新闻", "最新"]
    if any(kw in query.lower() for kw in news_keywords):
        return "news"
    
    code_keywords = ["code", "api", "function", "error", "github", "代码"]
    if any(kw in query.lower() for kw in code_keywords):
        return "code"
    
    return "general"

def search(query: str, max_engines: int = 3) -> Dict[str, Any]:
    """执行搜索"""
    query_type = detect_query_type(query)
    
    # 根据查询类型选择引擎
    if query_type == "chinese":
        engines = ["sogou", "bing", "duckduckgo"]
    elif query_type == "academic":
        engines = ["arxiv", "crossref", "pubmed", "openalex", "dblp"]
    elif query_type == "news":
        engines = ["bing_news", "google_news", "duckduckgo_news"]
    elif query_type == "code":
        engines = ["github", "stackoverflow", "gitlab"]
    else:
        engines = ["bing", "duckduckgo", "wikipedia"]
    
    # 并行搜索
    all_results = []
    with ThreadPoolExecutor(max_workers=max_engines) as executor:
        futures = {executor.submit(search_engine, engine, query, 5, 5): engine 
                  for engine in engines[:max_engines]}
        for future in as_completed(futures, timeout=10):
            engine_name = futures[future]
            try:
                results = future.result()
                all_results.extend(results)
            except Exception:
                pass
    
    # 去重
    seen_urls = set()
    unique_results = []
    for r in all_results:
        url = r.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_results.append(r)
    
    return {
        "query": query,
        "query_type": query_type,
        "engines_used": engines[:max_engines],
        "results": unique_results[:10],
        "count": len(unique_results[:10])
    }

# ═══════════════════════════════════════════════════════════════════════════════
# 引擎元数据与健康检查（供 unified-search T2 层调用）
# ═══════════════════════════════════════════════════════════════════════════════

# 引擎元数据（基于实测数据）
_ENGINE_METADATA = {
    # 通用搜索（6 个）
    "bing":          {"category": "general", "latency_ms": 610,  "status": "ok", "zone": "global"},
    "duckduckgo":    {"category": "general", "latency_ms": 1013, "status": "ok", "zone": "global"},
    "google":        {"category": "general", "latency_ms": 4476, "status": "slow", "zone": "global", "note": "中国可能不可达"},
    "mojeek":        {"category": "general", "latency_ms": 2121, "status": "ok", "zone": "global"},
    "yandex":        {"category": "general", "latency_ms": 3071, "status": "ok", "zone": "global"},
    "startpage":     {"category": "general", "latency_ms": 3515, "status": "ok", "zone": "global"},
    # 中文搜索（2 个 — Baidu 已被 Sogou 取代为推荐引擎）
    "baidu":         {"category": "chinese", "latency_ms": 10680, "status": "degraded", "zone": "china", "note": "CAPTCHA 拦截严重，不再推荐，由 Sogou 替代"},
    "sogou":         {"category": "chinese", "latency_ms": 910,   "status": "ok", "zone": "china", "recommended": True, "note": "最佳中文搜索"},
    # 学术搜索（3 个）
    "arxiv":         {"category": "academic", "latency_ms": 3403, "status": "ok", "zone": "global"},
    "pubmed":        {"category": "academic", "latency_ms": 1516, "status": "ok", "zone": "global"},
    "crossref":      {"category": "academic", "latency_ms": 2094, "status": "ok", "zone": "global"},
    # 新闻搜索（3 个）
    "bing_news":     {"category": "news", "latency_ms": 782,  "status": "ok", "zone": "global"},
    "google_news":   {"category": "news", "latency_ms": 4006, "status": "ok", "zone": "global"},
    "duckduckgo_news": {"category": "news", "latency_ms": 3392, "status": "ok", "zone": "global"},
    # 代码搜索（7 个）
    "github":        {"category": "code", "latency_ms": 1414, "status": "ok", "zone": "global"},
    "stackoverflow": {"category": "code", "latency_ms": 1981, "status": "ok", "zone": "global"},
    "gitlab":        {"category": "code", "latency_ms": 1947, "status": "ok", "zone": "global"},
    "npm":           {"category": "code", "latency_ms": 2389, "status": "ok", "zone": "global"},
    "docker_hub":    {"category": "code", "latency_ms": 0,    "status": "unknown", "zone": "global"},
    "pypi":          {"category": "code", "latency_ms": 0,    "status": "unknown", "zone": "global"},
    "arch_wiki":     {"category": "code", "latency_ms": 0,    "status": "unknown", "zone": "global"},
    # 百科/知识（3 个）
    "wikipedia":     {"category": "wiki", "latency_ms": 747,  "status": "ok", "zone": "global"},
    "wiktionary":    {"category": "wiki", "latency_ms": 1070, "status": "ok", "zone": "global"},
    "wikiquote":     {"category": "wiki", "latency_ms": 2231, "status": "ok", "zone": "global"},
    # 其他垂直（2 个）
    "imdb":          {"category": "vertical", "latency_ms": 2637, "status": "ok", "zone": "global"},
    "goodreads":     {"category": "vertical", "latency_ms": 489,  "status": "ok", "zone": "global"},
    # 新增学术（2 个）
    "openalex":      {"category": "academic", "latency_ms": 0,    "status": "unknown", "zone": "global"},
    "dblp":          {"category": "academic", "latency_ms": 0,    "status": "unknown", "zone": "global"},
}


def get_available_engines(category: Optional[str] = None) -> list:
    """返回当前可用引擎列表及元数据。

    Args:
        category: 可选，按类别过滤（general/chinese/academic/news/code/wiki/vertical）

    Returns:
        [{"name": "bing", "category": "general", "latency_ms": 610, "status": "ok", ...}, ...]

    用于 unified-search T2 层注册表和路由决策。
    """
    engines = []
    for name, meta in _ENGINE_METADATA.items():
        if name not in ENGINES:
            continue  # 引擎定义不在当前版本中
        if category and meta.get("category") != category:
            continue
        engines.append({
            "name": name,
            **meta,
        })
    return engines


def check_engine_health(engine_name: str, timeout: int = 5) -> dict:
    """快速探测单个引擎是否健康可用。

    Args:
        engine_name: 引擎名
        timeout: 探测超时（秒）

    Returns:
        {
            "engine": "bing",
            "available": True/False,
            "latency_ms": 610,
            "error": null / "错误信息",
            "sample": ...  # 仅 available=True 时包含一条样例结果
        }

    用作 unified-search 降级链中的健康检查。
    """
    if engine_name not in ENGINES:
        return {
            "engine": engine_name,
            "available": False,
            "latency_ms": 0,
            "error": f"未知引擎: {engine_name}",
        }

    meta = _ENGINE_METADATA.get(engine_name, {})

    # Baidu 特殊处理：已知 CAPTCHA 问题，直接标记 degraded 不做探测
    if engine_name == "baidu":
        return {
            "engine": engine_name,
            "available": False,
            "latency_ms": meta.get("latency_ms", 0),
            "error": "CAPTCHA 拦截，已降级",
            "status": "degraded",
        }

    t0 = time.time()
    try:
        results = search_engine(engine_name, "test", n=1, timeout=timeout)
        elapsed = round((time.time() - t0) * 1000)
        if results and len(results) > 0:
            return {
                "engine": engine_name,
                "available": True,
                "latency_ms": elapsed,
                "error": None,
                "sample": results[0],
                "status": meta.get("status", "ok"),
            }
        else:
            return {
                "engine": engine_name,
                "available": False,
                "latency_ms": elapsed,
                "error": "返回空结果",
                "status": "degraded",
            }
    except Exception as e:
        elapsed = round((time.time() - t0) * 1000)
        return {
            "engine": engine_name,
            "available": False,
            "latency_ms": elapsed,
            "error": str(e),
            "status": "unavailable",
        }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 search_v3.py <查询>")
        sys.exit(1)
    
    query = " ".join(sys.argv[1:])
    result = search(query)
    print(json.dumps(result, ensure_ascii=False, indent=2))
