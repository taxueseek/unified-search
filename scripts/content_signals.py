"""Argo 内容质量信号模块 — 内联移植自 Hound envelope.py。

4 个纯计算信号 + 1 个整合入口，全部基于 stdlib + re：
  classify_source         域名权威分类
  compute_freshness       时效性（年龄 + stale）
  detect_page_type        页面结构类型
  compute_content_quality 内容质量评分
主入口：analyze_fetch_result(url, html, content, metadata) -> dict。
零外部依赖，单次调用微秒级。
"""
from __future__ import annotations

import re
from datetime import datetime, date, timezone
from typing import Any
from urllib.parse import urlparse

# ── 全局常量 ────────────────────────────────────────────────────────

STALE_DAYS = 365

_NEWS_DOMAINS = (
    "nytimes.com", "bbc.com", "bbc.co.uk", "reuters.com", "theguardian.com",
    "washingtonpost.com", "bloomberg.com", "apnews.com", "aljazeera.com",
    "cnbc.com", "ft.com", "economist.com", "techcrunch.com", "theverge.com",
    "arstechnica.com", "wired.com", "nature.com", "science.org",
)
_QA_DOMAINS = (
    "stackoverflow.com", "stackexchange.com", "serverfault.com",
    "superuser.com", "mathoverflow.com", "askubuntu.com",
)
_GITHUB_DOMAINS = (
    "github.com", "raw.githubusercontent.com", "gist.github.com",
)

_DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%B %d, %Y", "%b %d, %Y",
    "%d %B %Y", "%d %b %Y",
)

_FORUM_MARKERS = (
    "phpbb", "discourse", 'class="forum', 'id="forum',
    'class="thread', 'class="post-body', 'class="message-body', "data-post-id",
)
_QA_MARKERS = (
    "stackoverflow", "stackexchange", 'class="question',
    'class="answer', "data-answerid", "data-questionid",
)
_DOCS_MARKERS = (
    "mkdocs", "docusaurus", "readthedocs", "sphinx-document",
    "algolia-docsearch", "md-nav", "theme-doc", 'class="rst-content"',
    "wy-nav-side",
)
_PAYWALL_MARKERS = (
    "subscribe to continue", "subscribe to read",
    "this article is for subscribers",
    "create a free account to continue", "sign in to continue reading",
    "you've reached your free article limit", "subscriber-only content",
    "premium content", "paywall",
)

_META_REFRESH_RE = re.compile(
    r'<meta\b[^>]*?http-equiv=["\']refresh["\'][^>]*?content=["\'][^"\']*url=',
    re.IGNORECASE,
)
_JS_REDIRECT_RE = re.compile(
    r'(?:location\.href\s*=|location\.replace|window\.location\s*=)',
    re.IGNORECASE,
)
_ANCHOR_RE = re.compile(r'<a\b[^>]*?href=["\']([^"\']+)["\']', re.IGNORECASE)
_STRIP_BLOCK_RE = re.compile(
    r'<(nav|header|footer|aside|script|style|noscript)\b[^>]*>.*?</\1>',
    re.IGNORECASE | re.DOTALL,
)
_ARTICLE_TAG_RE = re.compile(r'<article\b', re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)
_WS_RE = re.compile(r"\s+")


# ── 1. 域名权威分类 ─────────────────────────────────────────────────

def classify_source(url: str) -> dict:
    """URL 域名权威分类。

    返回 {"source_type", "is_official"}。
    source_type: gov/edu/github/news/blog/forum/qa/docs-site/ecommerce/unknown
    is_official: 仅对 .gov/.edu/github/厂商 docs 判定 True。
    """
    if not url:
        return {"source_type": "unknown", "is_official": False}
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return {"source_type": "unknown", "is_official": False}
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    if ":" in host:
        host = host.split(":", 1)[0]
    if not host:
        return {"source_type": "unknown", "is_official": False}

    if host.endswith(".gov") or host == "gov" or ".gov." in host:
        return {"source_type": "gov", "is_official": True}
    if host.endswith(".edu") or host.endswith(".ac.uk") or re.search(r"\.ac\.[a-z]{2}$", host):
        return {"source_type": "edu", "is_official": True}
    if host in _GITHUB_DOMAINS or host.endswith(".github.io"):
        return {"source_type": "github", "is_official": True}
    if host.startswith(("docs.", "developer.", "developers.")):
        return {"source_type": "docs-site", "is_official": True}
    if host in _QA_DOMAINS or host.endswith(".stackexchange.com") or host.endswith(".stackoverflow.com"):
        return {"source_type": "qa", "is_official": False}
    if any(m in host for m in ("forum", "forums", "community", "discourse", "board")):
        return {"source_type": "forum", "is_official": False}
    if host in ("reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com") or host.endswith(".reddit.com"):
        return {"source_type": "forum", "is_official": False}
    if (
        host.startswith("blog.")
        or host in ("medium.com", "wordpress.com", "substack.com")
        or host.endswith((".substack.com", ".medium.com"))
    ):
        return {"source_type": "blog", "is_official": False}
    if host.startswith(("shop.", "store.")) or host in ("amazon.com", "ebay.com") or host.endswith(".shop"):
        return {"source_type": "ecommerce", "is_official": False}
    if any(host == d or host.endswith("." + d) for d in _NEWS_DOMAINS):
        return {"source_type": "news", "is_official": False}
    return {"source_type": "unknown", "is_official": False}


# ── 2. 时效性 ────────────────────────────────────────────────────────

def _parse_date(s: str) -> date | None:
    """解析日期：ISO 8601 (含 Z/offset)、压缩 YYYYMMDD、英文格式。"""
    if not s or not s.strip():
        return None
    s = s.strip()
    if re.fullmatch(r"\d{8}", s):
        try:
            return datetime.strptime(s, "%Y%m%d").date()
        except ValueError:
            return None
    for cand in (s, s[:10]):
        try:
            return datetime.fromisoformat(cand.replace("Z", "+00:00")).date()
        except ValueError:
            continue
    for fmt in _DATE_FORMATS:
        for cand in (s, s[:32]):
            try:
                return datetime.strptime(cand, fmt).date()
            except ValueError:
                continue
    return None


def compute_freshness(meta_dates: dict, fetched_at: str = None) -> dict:
    """计算内容年龄。

    输入 {"published_time", "modified_time", "date"}；
    偏好 modified > published > date；
    返回 {"content_age_days", "is_stale"}，无日期返回 (-1, False)。
    """
    if not meta_dates:
        return {"content_age_days": -1, "is_stale": False}
    date_str = (
        meta_dates.get("modified_time")
        or meta_dates.get("published_time")
        or meta_dates.get("date")
        or ""
    )
    content_date = _parse_date(date_str) if date_str else None
    if content_date is None:
        return {"content_age_days": -1, "is_stale": False}
    fetched_date = _parse_date(fetched_at) if fetched_at else datetime.now(timezone.utc).date()
    delta = (fetched_date - content_date).days
    if delta < 0:
        return {"content_age_days": -1, "is_stale": False}
    return {"content_age_days": delta, "is_stale": delta > STALE_DAYS}


# ── 3. 页面结构类型检测 ─────────────────────────────────────────────

def _count_content_links(html: str, host: str) -> int:
    """统计主内容区同域链接数（剥除 nav/header/footer 等 chrome）。"""
    stripped = _STRIP_BLOCK_RE.sub("", html)
    count = 0
    for m in _ANCHOR_RE.finditer(stripped):
        href = (m.group(1) or "").strip()
        if not href:
            continue
        low = href.lower()
        if low.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        if href.startswith(("/", "?")):
            count += 1
            continue
        try:
            h = urlparse(href).netloc.lower()
        except Exception:
            continue
        if h and (h == host or h.endswith("." + host)):
            count += 1
    return count


def detect_page_type(html: str, url: str = "") -> dict:
    """从 HTML 判断页面结构类型。

    返回 {"page_type", "confidence"}。
    page_type: article/list/forum/qa/docs/paywall/redirect/unknown。
    错误推导信号（js_shell/auth_wall）不在此处检测——由上层错误回调 override。
    """
    if not html or not html.strip():
        return {"page_type": "unknown", "confidence": 0.0}
    low = html.lower()
    # 跳转
    if _META_REFRESH_RE.search(low):
        return {"page_type": "redirect", "confidence": 0.9}
    text_len_approx = len(re.sub(r"<[^>]+>", "", low))
    if _JS_REDIRECT_RE.search(low) and text_len_approx < 500:
        return {"page_type": "redirect", "confidence": 0.85}
    # paywall
    if any(m in low for m in _PAYWALL_MARKERS):
        return {"page_type": "paywall", "confidence": 0.9}
    # 结构化标记
    if any(m in low for m in _QA_MARKERS):
        return {"page_type": "qa", "confidence": 0.85}
    if any(m in low for m in _FORUM_MARKERS):
        return {"page_type": "forum", "confidence": 0.8}
    if any(m in low for m in _DOCS_MARKERS):
        return {"page_type": "docs", "confidence": 0.85}
    # list 页：多同域链接 + 文本少
    host = ""
    if url:
        try:
            host = urlparse(url).netloc.lower().split(":", 1)[0]
        except Exception:
            host = ""
    if host and not _ARTICLE_TAG_RE.search(low):
        n_links = _count_content_links(html, host)
        if n_links >= 20 and (text_len_approx < 1500 or text_len_approx / n_links < 200):
            return {"page_type": "list", "confidence": 0.75}
    # article
    if _ARTICLE_TAG_RE.search(low):
        return {"page_type": "article", "confidence": 0.8}
    return {"page_type": "unknown", "confidence": 0.4}


# ── 4. 内容质量评分 ─────────────────────────────────────────────────

# GEO 实证：数字/定义/对比/how-to 与吸收深度正相关；纯 Q&A 格式无优势（甚至略负）
_NUM_RE = re.compile(
    r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*(?:%|％|亿|万|万亿|pct|bp|元|美元|吨|倍)"
    r"|(?:环比|同比|较上[季年]度?)[^\n]{0,12}[+\-＋－]?\d"
    r"|Q[1-4]\b|20\d{2}\s*年",
    re.I,
)
_DEF_RE = re.compile(
    r"(?:是指|定义为|所谓|即指|指的是|是一种|可定义为"
    r"|definition of|is defined as|refers to)",
    re.I,
)
_CMP_RE = re.compile(
    r"(?:对比|比较|相较|相比|环比|同比|分别|vs\.?|versus|versus|"
    r"增持|减持|上升|下降|提升|回落|高于|低于|超过|不及)",
    re.I,
)
_HOWTO_RE = re.compile(
    r"(?:步骤|如何|怎么做|操作建议|方法如下|第[一二三四五六七八九十1-9][步、.]"
    r"|step\s*\d|how to|tutorial)",
    re.I,
)
_QA_FMT_RE = re.compile(
    r"(?:^|\n)\s*(?:Q\s*[:：]|A\s*[:：]|问\s*[:：]|答\s*[:：])"
    r"|class=[\"']question[\"']|class=[\"']answer[\"']"
    r"|(?:怎么样|好不好|靠谱吗)\s*$",
    re.I | re.M,
)
_DISCLOSE_RE = re.compile(
    r"(?:截至|根据|数据显示|研究报告|披露|公告|季报|年报|来源[：:])",
    re.I,
)


def score_evidence_density(text: str, title: str = "") -> dict:
    """证据密度评分（snippet 或正文均可）。

    第一性：Agent 需要的不是「被检索到」，而是「可抽取、可核对的证据块」。
    返回布尔特征 + absorption_score ∈ [0,1]。
    """
    body = f"{title or ''}\n{text or ''}"
    has_numbers = bool(_NUM_RE.search(body))
    has_definition = bool(_DEF_RE.search(body))
    has_comparison = bool(_CMP_RE.search(body))
    has_howto = bool(_HOWTO_RE.search(body))
    has_disclose = bool(_DISCLOSE_RE.search(body))
    is_qa_format = bool(_QA_FMT_RE.search(body)) or bool(
        re.search(r"[?？]\s*$", (title or "").strip())
    )

    score = 0.15
    if has_numbers:
        score += 0.22
    if has_definition:
        score += 0.18
    if has_comparison:
        score += 0.16
    if has_howto:
        score += 0.12
    if has_disclose:
        score += 0.08
    if len(body) >= 80:
        score += 0.05
    if is_qa_format:
        score -= 0.08  # GEO: 纯 Q&A 格式平均吸收略负

    return {
        "has_numbers": has_numbers,
        "has_definition": has_definition,
        "has_comparison": has_comparison,
        "has_howto": has_howto,
        "has_disclose": has_disclose,
        "is_qa_format": is_qa_format,
        "absorption_score": round(min(max(score, 0.0), 1.0), 3),
    }


def compute_content_quality(content: str, title: str = "") -> dict:
    """去 HTML 后文本质量评分。

    返回 quality_score / content_ok / word_count / text_density / has_structure
    + 证据密度字段（GEO 吸收信号）。
    content_ok = quality_score > 0.3 且 word_count > 50。
    """
    clean = _TAG_RE.sub(" ", content or "")
    clean = _WS_RE.sub(" ", clean).strip()
    word_count = len(clean)
    text_len = len(clean.replace(" ", ""))
    raw_len = max(len(content or ""), 1)
    text_density = text_len / raw_len
    has_structure = bool(
        re.search(r"</?(p|li|h[1-6]|pre|blockquote|section|div)\b", content or "", re.IGNORECASE)
    )
    # 长度分：50-1500 字线性增长（降权，避免 SEO 水文仅靠长度胜出）
    length_score = min(max((word_count - 50) / 1000, 0.0), 1.0)
    density_score = min(text_density / 0.5, 1.0)
    structure_score = 0.2 if has_structure else 0.0
    title_bonus = 0.0
    if title:
        title_words = [w for w in _WS_RE.split(title.lower()) if len(w) > 1]
        if title_words:
            hits = sum(1 for w in title_words if w in clean.lower())
            title_bonus = min(hits / len(title_words), 1.0) * 0.1

    evidence = score_evidence_density(clean, title)
    # 权重：长度 0.2 + 密度 0.2 + 结构 0.2 + 证据 0.3 + 标题对齐 0.1
    quality_score = min(
        length_score * 0.2
        + density_score * 0.2
        + structure_score
        + evidence["absorption_score"] * 0.3
        + title_bonus,
        1.0,
    )
    return {
        "quality_score": round(quality_score, 3),
        "content_ok": quality_score > 0.3 and word_count > 50,
        "word_count": word_count,
        "text_density": round(text_density, 3),
        "has_structure": has_structure,
        **evidence,
    }


# ── 5. 整合入口 ─────────────────────────────────────────────────────

def analyze_fetch_result(url: str, html: str, content: str, metadata: dict | None = None) -> dict:
    """综合所有信号返回完整质量信封。"""
    metadata = metadata or {}
    return {
        "source": classify_source(url),
        "freshness": compute_freshness(metadata),
        "page_type": detect_page_type(html, url),
        "quality": compute_content_quality(content, metadata.get("title", "")),
    }


if __name__ == "__main__":
    test_url = "https://docs.python.org/3/library/asyncio.html"
    body_text = "word " * 200
    test_html = (
        '<html><head><title>asyncio</title></head><body><article><h1>asyncio</h1>'
        "<p>" + body_text + "</p>"
        + '<a href="/library/a.html">a</a>' * 5
        + '<nav><a href="/">home</a></nav>'
        + "</article></body></html>"
    )
    test_content = body_text
    test_meta = {"title": "asyncio", "published_time": "2024-01-15", "modified_time": "2025-06-01"}
    import json
    print(json.dumps(analyze_fetch_result(test_url, test_html, test_content, test_meta), ensure_ascii=False, indent=2))
