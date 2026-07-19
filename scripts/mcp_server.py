#!/usr/bin/env python3
"""
mcp_server.py — Argo MCP 服务层

将 research/evidence/clarify 三个工具暴露为 MCP tool，
通过 JSON-RPC over stdio 与 Grok/Claude 等客户端通信。

用法：
  python3 mcp_server.py                    # 启动 MCP stdio 服务
  python3 mcp_server.py --test             # 本地测试模式
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

# 进程级共享缓存：同一 MCP 会话中 search/evidence/research 共用
_cache_instance = None

def _get_cache():
    global _cache_instance
    if _cache_instance is None:
        from cache import SearchCache
        _cache_instance = SearchCache()
    return _cache_instance


# ── 工具定义（MCP schema） ────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "argo_search",
        "description": "统一搜索引擎：47 个引擎（22 远程 + 25 本地）统一搜索，支持 TF-IDF 语义路由 + RRF 多引擎融合 + Bocha 语义精排 + 双层缓存。适用于所有通用搜索场景：查资料、找答案、搜新闻、学术检索、代码搜索、中文内容搜索等。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索查询词（支持中英文）"
                },
                "engine": {
                    "type": "string",
                    "description": "指定搜索引擎（默认 auto，可选 anysearch/zhihu/eastmoney/arxiv/wigolo/duckduckgo/byted/bocha/tavily/github/wikipedia/semantic_scholar/local_search 等）",
                    "default": "auto"
                },
                "max_results": {
                    "type": "integer",
                    "description": "最大结果数（默认 5）",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 20
                },
                "depth": {
                    "type": "string",
                    "enum": ["fast", "balanced", "deep"],
                    "description": "搜索深度（默认 fast）",
                    "default": "fast"
                },
                "mode": {
                    "type": "string",
                    "enum": ["fast", "auto", "deep", "budget"],
                    "description": "预算模式：fast=免费优先, auto=成本感知, deep=质量优先, budget=配额控制（默认 auto）",
                    "default": "auto"
                },
                "skip_cache": {
                    "type": "boolean",
                    "description": "跳过缓存（默认 false）",
                    "default": False
                },
                "summary": {
                    "type": "boolean",
                    "description": "精简模式：snippet 截断到 80 字符，节省 LLM token（默认 false）",
                    "default": False
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "argo_research",
        "description": "深度研究：将复杂查询分解为子问题，多源并行采集，输出综合报告+引用+知识缺口。适用于学术综述、事实核查、竞品分析、技术选型等需要多步搜索的场景。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "研究查询（可以是复杂的、多步骤的问题）"
                },
                "num_sub_queries": {
                    "type": "integer",
                    "description": "子查询数量（默认4，最大8）",
                    "default": 4,
                    "minimum": 2,
                    "maximum": 8
                },
                "max_results": {
                    "type": "integer",
                    "description": "每个子查询最大结果数（默认5）",
                    "default": 5
                },
                "depth": {
                    "type": "string",
                    "enum": ["fast", "balanced", "deep"],
                    "description": "搜索深度（默认balanced）",
                    "default": "balanced"
                },
                "mode": {
                    "type": "string",
                    "enum": ["fast", "auto", "deep", "budget"],
                    "description": "预算模式（默认auto）",
                    "default": "auto"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "argo_evidence",
        "description": "来源可信度评估：对搜索结果进行权威性+时效性+交叉验证的综合评分，输出每个结果的可信度分解。适用于事实核查、高后果决策、学术引用等需要评估来源可靠性的场景。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索查询词（用于交叉验证）"
                },
                "results_json": {
                    "type": "string",
                    "description": "搜索结果 JSON 字符串（可选；为空时自动调用 super_search 搜索）。格式：{\"results\": [{\"title\": \"...\", \"url\": \"...\", \"snippet\": \"...\", \"source\": \"...\", \"score\": 0.8}]}"
                },
                "max_results": {
                    "type": "integer",
                    "description": "自动搜索时的最大结果数（默认 10，仅在 results_json 为空时有效）",
                    "default": 10
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "argo_clarify",
        "description": "意图消歧：分析查询中的歧义词、多义实体，给出意图分类和推荐搜索策略。适用于查询含歧义词（如「苹果」=公司/水果、「Python」=语言/蛇）或意图不明确的场景。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "需要消歧的搜索查询"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "argo_crawl",
        "description": "站点级爬取：通过 sitemap.xml 或 BFS 策略批量抓取站点页面，输出页面 URL、正文片段和深度。适用于整站内容审计、站内多页对比、批量抓取等场景。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "目标站点根 URL（如 https://docs.python.org/）"
                },
                "strategy": {
                    "type": "string",
                    "enum": ["sitemap", "bfs"],
                    "description": "爬取策略（默认 bfs）",
                    "default": "bfs"
                },
                "max_pages": {
                    "type": "integer",
                    "description": "最大抓取页面数（默认 10，sitemap 策略默认 20）",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 50
                },
                "max_depth": {
                    "type": "integer",
                    "description": "BFS 最大深度（默认 2）",
                    "default": 2,
                    "minimum": 1,
                    "maximum": 5
                },
                "timeout": {
                    "type": "integer",
                    "description": "单页超时秒数（默认 8）",
                    "default": 8,
                    "minimum": 3,
                    "maximum": 30
                }
            },
            "required": ["url"]
        }
    },
    {
        "name": "argo_extract",
        "description": "结构化数据提取：从页面 HTML 中抽取表格、<meta> 元数据、OpenGraph、JSON-LD 等结构化信息。适用于价格表抽取、SEO 元数据分析、富媒体结构化数据解析等场景。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "目标页面 URL"
                },
                "mode": {
                    "type": "string",
                    "enum": ["tables", "metadata", "jsonld", "all"],
                    "description": "提取模式（默认 all）",
                    "default": "all"
                }
            },
            "required": ["url"]
        }
    },
]


# ── 工具执行 ──────────────────────────────────────────────────────────────────

def execute_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """执行 MCP 工具。"""
    try:
        if name == "argo_search":
            from search import super_search
            result = super_search(
                query=arguments["query"],
                engine=arguments.get("engine", "auto"),
                n=arguments.get("max_results", 5),
                skip_cache=arguments.get("skip_cache", False),
                timeout=arguments.get("timeout", 10),
                depth=arguments.get("depth", "fast"),
                mode=arguments.get("mode", "auto"),
                cache=_get_cache(),
            )
            # 精简模式：截断 snippet
            if arguments.get("summary", False):
                for r in result.get("results", []):
                    if r.get("snippet"):
                        r["snippet"] = r["snippet"][:80]
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

        elif name == "argo_research":
            from research import deep_research
            result = deep_research(
                query=arguments["query"],
                num_sub_queries=arguments.get("num_sub_queries", 4),
                max_results=arguments.get("max_results", 5),
                depth=arguments.get("depth", "balanced"),
                mode=arguments.get("mode", "auto"),
            )
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

        elif name == "argo_evidence":
            from evidence import compute_credibility
            results_json_str = arguments.get("results_json", "")
            # results_json 为空时自动调用 super_search 获取结果
            if not results_json_str or not results_json_str.strip():
                from search import super_search
                search_result = super_search(
                    query=arguments["query"],
                    n=arguments.get("max_results", 10),
                    depth="fast",
                    mode="auto",
                    cache=_get_cache(),
                )
                results = search_result.get("results", [])
            else:
                results_data = json.loads(results_json_str)
                results = results_data.get("results", [])
            result = compute_credibility(results, arguments["query"])
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

        elif name == "argo_clarify":
            from clarify import analyze_query, recommend_routing
            analysis = analyze_query(arguments["query"])
            routing = recommend_routing(analysis)
            analysis["routing"] = routing
            return {"content": [{"type": "text", "text": json.dumps(analysis, ensure_ascii=False, indent=2)}]}

        elif name == "argo_crawl":
            from crawl import crawl_bfs, crawl_sitemap
            strategy = arguments.get("strategy", "bfs")
            max_pages = arguments.get("max_pages", 10)
            max_depth = arguments.get("max_depth", 2)
            timeout = arguments.get("timeout", 8)
            if strategy == "sitemap":
                result = crawl_sitemap(arguments["url"], max_pages=max_pages, timeout=timeout)
            else:
                result = crawl_bfs(arguments["url"], max_pages=max_pages, max_depth=max_depth, timeout=timeout)
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

        elif name == "argo_extract":
            from extract import extract_tables, extract_metadata, extract_jsonld
            from fetch import fetch_page
            mode = arguments.get("mode", "all")
            fetch_result = fetch_page(arguments["url"], max_chars=50000, timeout=15, raw=True)
            if not fetch_result["success"]:
                return {"content": [{"type": "text", "text": json.dumps({"error": fetch_result.get("error", "fetch failed")}, ensure_ascii=False)}], "isError": True}
            html = fetch_result["html"]
            output = {}
            if mode in ("tables", "all"):
                output["tables"] = extract_tables(html)
            if mode in ("metadata", "all"):
                output["metadata"] = extract_metadata(html)
            if mode in ("jsonld", "all"):
                output["jsonld"] = extract_jsonld(html)
            output["url"] = fetch_result["url"]
            return {"content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False, indent=2)}]}

        else:
            return {"error": {"code": -32601, "message": f"Unknown tool: {name}"}}

    except Exception as e:
        return {
            "content": [{"type": "text", "text": json.dumps({"error": {"code": -32000, "message": f"{type(e).__name__}: {e}"}}, ensure_ascii=False)}],
            "isError": True
        }


# ── MCP JSON-RPC 处理 ────────────────────────────────────────────────────────

def handle_rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """处理 JSON-RPC 请求。"""
    if method == "initialize":
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": "argo",
                "version": "1.0.1"
            },
            "instructions": "Argo MCP 提供 6 个工具：argo_search（47 引擎统一搜索）、argo_research（深度研究）、argo_evidence（可信度评估）、argo_clarify（意图消歧）、argo_crawl（站点爬取）、argo_extract（结构化数据提取）。底层使用 47 个搜索引擎的统一搜索基础设施，支持 TF-IDF 语义路由、RRF 多引擎融合、Bocha 语义精排、双层缓存和成本感知预算控制。"
        }

    elif method == "tools/list":
        return {"tools": TOOLS}

    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        return execute_tool(tool_name, arguments)

    elif method == "ping":
        return {}

    elif method.startswith("notifications/"):
        # 通知消息无需回复
        return None

    else:
        return {"error": {"code": -32601, "message": f"Method not found: {method}"}}


def run_stdio():
    """运行 MCP stdio 服务。MCP 帧协议：Content-Length: N\\r\\n\\r\\n{json}"""
    import sys, os, time as _time
    try:
        with open(os.path.expanduser("~/.kimi/argo_diag.log"), "a") as _log:
            _log.write(f"=== PID={os.getpid()} ENTER run_stdio {_time.strftime('%H:%M:%S')} ===\n")
            _log.write(f"python={sys.executable} cwd={os.getcwd()}\n")
            _log.flush()
    except: pass
    
    while True:
        try:
            # 读取 Content-Length 头
            header = sys.stdin.buffer.readline()
            if not header:
                break  # EOF
            header_str = header.decode("utf-8", errors="replace").strip()
            if not header_str:
                continue
            if not header_str.startswith("Content-Length:"):
                # 兼容行模式（某些客户端不发 Content-Length）
                try:
                    request = json.loads(header_str)
                except json.JSONDecodeError:
                    _send_error(None, -32700, "Parse error")
                    continue
            else:
                length = int(header_str.split(":")[1].strip())
                sys.stdin.buffer.readline()  # skip blank line
                body = sys.stdin.buffer.read(length).decode("utf-8")
                request = json.loads(body)

            method = request.get("method", "")
            params = request.get("params", {})
            request_id = request.get("id")

            response = handle_rpc(method, params)

            # 通知消息无需回复
            if response is None:
                continue

            if request_id is not None:
                response["jsonrpc"] = "2.0"
                response["id"] = request_id
                _send_response(response)

        except json.JSONDecodeError:
            _send_error(None, -32700, "Parse error")
        except Exception as e:
            _send_error(None, -32000, f"Internal error: {e}")


def _send_response(response: dict):
    """发送 MCP 帧响应。"""
    data = json.dumps(response, ensure_ascii=False)
    encoded = data.encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode())
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def _send_error(request_id, code: int, message: str):
    """发送 JSON-RPC 错误响应。"""
    resp = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message}
    }
    _send_response(resp)


# ── 测试模式 ──────────────────────────────────────────────────────────────────

def test_mode():
    """本地测试。"""
    print("=== Argo MCP 工具测试 ===\n")

    # 测试 search
    print("--- argo_search 测试（fast模式）---")
    result = execute_tool("argo_search", {
        "query": "Python async best practices",
        "max_results": 3,
        "depth": "fast",
        "mode": "fast",
    })
    print(result["content"][0]["text"][:500])
    print()

    # 测试 clarify
    print("--- clarify 测试 ---")
    result = execute_tool("argo_clarify", {"query": "Python 吞苹果 兼容吗"})
    print(result["content"][0]["text"][:500])
    print()

    # 测试 research（快速模式）
    print("--- research 测试（fast模式）---")
    result = execute_tool("argo_research", {
        "query": "React Server Components 2025 生产环境案例",
        "num_sub_queries": 2,
        "max_results": 3,
        "depth": "fast",
        "mode": "fast",
    })
    text = result["content"][0]["text"]
    # 只打印前 500 字符
    print(text[:500])
    print()

    # 测试 evidence
    print("--- evidence 测试 ---")
    sample_results = json.dumps({
        "results": [
            {"title": "Python docs", "url": "https://docs.python.org", "snippet": "Official Python documentation", "source": "wikipedia", "score": 0.9},
            {"title": "Some blog", "url": "https://random-blog.com/python", "snippet": "Python tutorial", "source": "duckduckgo", "score": 0.6},
        ]
    })
    result = execute_tool("argo_evidence", {"query": "Python tutorial", "results_json": sample_results})
    print(result["content"][0]["text"][:500])


# ── 入口 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--test" in sys.argv:
        test_mode()
    else:
        run_stdio()
