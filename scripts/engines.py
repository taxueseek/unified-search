#!/usr/bin/env python3
"""引擎适配层 v3 — 配置驱动 + 强化错误处理 + 独立日志

设计：
  - 从 config.yaml 的 engines 注册表动态构造引擎调用
  - 支持类型: cli / python / http
  - 路径缺失时由 config.py 标记 enabled=False，不硬编码兜底
  - 所有异常吞没，返回空 list 并记录 stderr 日志
  - 统一入口: search(query, engine, n=5, timeout=8)
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

# 优先从当前目录导入
try:
    from config import load_config, get_engines
    from search_types import normalize_result, SearchResult
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from config import load_config, get_engines
    from search_types import normalize_result, SearchResult


# ── 独立日志 ───────────────────────────────────────────────────────────────────
logger = logging.getLogger("unified_search.engines")
if not logger.handlers:
    logger.setLevel(logging.WARNING)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[engines:%(levelname)s]\t%(message)s"))
    logger.addHandler(handler)


# ── 统一错误处理装饰器 ────────────────────────────────────────────────────────

def safe_search(fn: Callable[..., list[dict[str, Any]]]) -> Callable[..., list[dict[str, Any]]]:
    """统一错误处理装饰器 — 消灭吞没异常。

    所有引擎构建函数（cli/python/http）在注册时经此装饰，确保：
    - subprocess.TimeoutExpired → 记录警告，返回 []
    - FileNotFoundError → 记录警告，返回 []
    - 其他异常 → 记录错误（含 traceback），返回 []
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> list[dict[str, Any]]:
        engine_name = (fn.__name__.replace("_engine", "").strip("_")) or "unknown"
        try:
            return fn(*args, **kwargs)
        except subprocess.TimeoutExpired:
            logger.warning(f"引擎 {engine_name} 超时")
            return []
        except FileNotFoundError as e:
            logger.warning(f"引擎 {engine_name} 命令不存在: {e}")
            return []
        except Exception as e:
            logger.error(f"引擎 {engine_name} 异常: {type(e).__name__}: {e}", exc_info=True)
            return []
    return wrapper


# ── subprocess 调用 ────────────────────────────────────────────────────────────
def _run(cmd: list[str], timeout: float = 8, engine_name: str = "?") -> str:
    """执行命令，超时/异常不吞没，显式记录。"""
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return r.stdout
        elapsed = time.time() - t0
        stderr_tail = (r.stderr or "").strip()[:200]
        logger.warning(
            f"引擎 {engine_name} 失败 (rc={r.returncode}, {elapsed:.2f}s): {stderr_tail}"
        )
        if r.stdout.strip():
            return r.stdout
        return ""
    except subprocess.TimeoutExpired:
        logger.warning(f"引擎 {engine_name} 超时 (>{timeout}s): {' '.join(cmd)}")
        return ""
    except FileNotFoundError as e:
        logger.error(f"引擎 {engine_name} CLI 缺失: {e}")
        return ""
    except Exception as e:
        logger.error(f"引擎 {engine_name} 未知异常: {type(e).__name__}: {e}")
        return ""


# ═══════════════════════════════════════════════════════════════════════════════
# 配置驱动引擎构造
# ═══════════════════════════════════════════════════════════════════════════════

def _resolve_placeholders(template: list[str] | str, query: str, n: int,
                          **extra: Any) -> list[str] | str:
    """替换模板中的占位符：
    - {query} / {n} / {domain} / {sub_domain} 等业务参数
    - {TIMESTAMP} 当前 Unix 时间戳（秒）
    - {ENV_VAR} 环境变量
    - ~ 展开为用户主目录
    """
    if isinstance(template, list):
        return [_resolve_placeholders(item, query, n, **extra) for item in template]

    s = template
    s = s.replace("{query}", query)
    s = s.replace("{n}", str(n))
    s = s.replace("{TIMESTAMP}", str(int(time.time())))
    for key, val in extra.items():
        s = s.replace(f"{{{key}}}", str(val))
    # 展开 ~ 为用户主目录
    if s.startswith("~"):
        s = str(Path.home() / s[1:])
    # 支持 {ENV_VAR} 环境变量
    import re
    env_pattern = re.compile(r"\{([A-Z_][A-Z0-9_]*)\}")
    def replace_env(match):
        var_name = match.group(1)
        return os.environ.get(var_name, match.group(0))
    s = env_pattern.sub(replace_env, s)
    return s


def _build_cli_engine(spec: dict[str, Any]) -> Any:
    """构造 CLI 引擎调用函数。"""
    cmd_template = spec.get("cmd", [])
    search_args_template = spec.get("search_args", [])
    env_overrides = spec.get("env", {})

    @safe_search
    def _engine(query: str, n: int = 5, timeout: float = 8, **kwargs) -> list[dict[str, Any]]:
        cmd = _resolve_placeholders(cmd_template, query, n)
        args = _resolve_placeholders(search_args_template, query, n)
        if not cmd:
            logger.warning("CLI 引擎配置缺少 cmd")
            return []
        full_cmd = cmd + args
        env = os.environ.copy()
        env.update(env_overrides)
        out = _run(full_cmd, timeout=timeout, engine_name=spec.get("_name", "cli"))
        parser = _PARSERS.get(spec.get("_name", ""), _parse_generic)
        return parser(out)

    return _engine


def _build_python_engine(spec: dict[str, Any]) -> Any:
    """构造 Python 引擎调用函数（通过子进程 import 模块执行）。"""
    module = spec.get("module", "tavily")
    client_class = spec.get("client_class", "TavilyClient")
    search_method = spec.get("search_method", "search")
    env_var = spec.get("env_var", "TAVILY_API_KEY")

    @safe_search
    def _engine(query: str, n: int = 5, timeout: float = 10, **kwargs) -> list[dict[str, Any]]:
        code = (
            f"import os,json;from {module} import {client_class};"
            f"c={client_class}(api_key=os.environ.get({json.dumps(env_var)},''));"
            f"r=c.{search_method}(query={json.dumps(query)},max_results={n});"
            f"print(json.dumps(r.get('results',[])))"
        )
        python_bin = spec.get("python_bin", sys.executable)
        out = _run([python_bin, "-c", code], timeout=timeout, engine_name=spec.get("_name", module))
        return _parse_tavily(out)

    return _engine


def _load_env_file(env_path: str) -> dict[str, str]:
    """从 .env 文件加载环境变量（兼容 anysearch-skill 的 .env 格式）。"""
    result = {}
    try:
        with open(env_path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip().lstrip("\ufeff")
                value = value.strip().strip("\"'")
                if key and value:
                    result[key] = value
    except (OSError, UnicodeDecodeError):
        pass
    return result


def _get_api_key(api_key_env: str) -> str:
    """获取 API Key：环境变量 > .env 文件（多路径搜索）。

    搜索路径（按优先级）：
    1. 系统环境变量
    2. 当前脚本目录/.env
    3. 当前脚本目录/../.env
    4. ~/.agents/skills/*/.env（扫描所有已安装的 skill）
    """
    # 1. 优先从环境变量读取
    value = os.environ.get(api_key_env, "")
    if value:
        return value

    # 2. 从 .env 文件读取
    script_dir = Path(__file__).parent
    env_paths = [
        script_dir / ".env",
        script_dir.parent / ".env",
        script_dir.parent / "local-search" / ".env",
    ]
    # 3. 扫描 ~/.agents/skills/*/.env
    agents_dir = Path.home() / ".agents" / "skills"
    if agents_dir.is_dir():
        for skill_dir in agents_dir.iterdir():
            if skill_dir.is_dir():
                env_paths.append(skill_dir / ".env")

    for env_path in env_paths:
        if env_path.is_file():
            env_data = _load_env_file(str(env_path))
            if api_key_env in env_data:
                return env_data[api_key_env]

    return ""


def _build_http_engine(spec: dict[str, Any]) -> Any:
    """构造 HTTP 引擎调用函数（支持 POST JSON body / GET query param）。

    新增支持：
    - 嵌套 body 模板（JSON-RPC 等复杂结构）
    - API Key 自动注入（环境变量 + .env 文件）
    """
    url_template = spec.get("url", "")
    headers = spec.get("headers", {"Content-Type": "application/json"})
    body_template = spec.get("body", {})
    method = spec.get("method", "POST")
    query_param = spec.get("query_param", "")
    fmt = spec.get("format", "")
    timeout = spec.get("timeout", 8)
    # API Key 注入配置
    api_key_header = spec.get("_api_key_header", "")
    api_key_env = spec.get("_api_key_env", "")
    api_key_prefix = spec.get("_api_key_prefix", "")
    admin_reset = spec.get("admin_reset_breakers", False)
    admin_reset_on_error = spec.get("admin_reset_on_error", False)
    admin_url = spec.get("admin_url", "")
    admin_token_path = spec.get("admin_token_path", "")

    # 熔断器上次重置时间（避免重复重置）
    _last_reset_time: float = 0
    _reset_cooldown: float = 60.0  # 60 秒内不重复重置

    @safe_search
    def _engine(
        query: str,
        n: int = 5,
        _timeout: float | None = None,
        depth: str = "fast",
        domain: str | None = None,
        profile: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        nonlocal _last_reset_time
        to = _timeout or timeout
        engine_name = spec.get("_name", "")

        # 仅在出错后重置熔断器（smarter reset）
        should_reset = False
        if admin_reset_on_error and admin_url and admin_token_path:
            # 检查上次引擎调用是否失败（通过全局状态）
            if time.time() - _last_reset_time > _reset_cooldown:
                should_reset = True
        elif admin_reset and admin_url and admin_token_path:
            should_reset = True

        if should_reset:
            token_path = Path(admin_token_path)
            if token_path.exists():
                token = token_path.read_text().strip()
                if token:
                    _run(["curl", "-s", "-X", "POST", admin_url,
                          "-H", f"Authorization: Bearer {token}"],
                         timeout=3, engine_name="wigolo-admin")
                    _last_reset_time = time.time()

        if method.upper() == "GET":
            # GET 请求：将查询参数拼接到 URL
            import urllib.parse as urlparse_mod
            resolved_url = _resolve_placeholders(url_template, query, n)
            param_name = query_param or "q"
            separator = "&" if "?" in resolved_url else "?"
            full_url = f"{resolved_url}{separator}{param_name}={urlparse_mod.quote(query)}"
            if fmt:
                full_url += f"&format={fmt}"
            # 支持 extra_params：额外 URL 查询参数（值支持占位符替换）
            extra_params = spec.get("extra_params", {})
            for k, v in extra_params.items():
                resolved_v = _resolve_placeholders(str(v), query, n)
                full_url += f"&{k}={urlparse_mod.quote(resolved_v)}"
            try:
                req = urllib.request.Request(full_url, headers={
                    k: _resolve_placeholders(v, query, n) for k, v in headers.items()
                })
                with urllib.request.urlopen(req, timeout=to) as resp:
                    raw = resp.read().decode("utf-8")
                    # 按引擎名选择解析器，禁止硬编码（曾导致 uapi source=wigolo）
                    parser = _PARSERS.get(engine_name, _parse_generic)
                    if fmt == "xml":
                        return _ensure_engine_source(parser(raw), engine_name)
                    data = json.loads(raw)
                    return _ensure_engine_source(parser(data), engine_name)
            except urllib.error.URLError as e:
                logger.warning(f"HTTP GET 引擎连接失败: {e.reason}")
                return []
            except urllib.error.HTTPError as e:
                logger.warning(f"HTTP GET 引擎 HTTP {e.code}: {e.reason}")
                return []
            except Exception as e:
                logger.warning(f"HTTP GET 引擎未知异常: {type(e).__name__}: {e}")
                return []

        # POST 请求（支持嵌套 body + API Key 注入）
        def _resolve_nested(obj: Any, query: str, n: int) -> Any:
            """递归解析嵌套 body 模板中的占位符。"""
            if isinstance(obj, str):
                return _resolve_placeholders(obj, query, n)
            elif isinstance(obj, dict):
                return {k: _resolve_nested(v, query, n) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [_resolve_nested(item, query, n) for item in obj]
            return obj

        body = _resolve_nested(body_template, query, n)

        # API Key 注入：从环境变量或 .env 文件读取并注入到请求头
        resolved_headers = {k: _resolve_placeholders(v, query, n) for k, v in headers.items()}
        if api_key_header and api_key_env:
            api_key_value = _get_api_key(api_key_env)
            if api_key_value:
                resolved_headers[api_key_header] = f"{api_key_prefix}{api_key_value}"

        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url_template,
            data=payload,
            headers=resolved_headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                # 按引擎名选择解析器，禁止硬编码（曾导致 uapi source=wigolo）
                parser = _PARSERS.get(engine_name, _parse_generic)
                return _ensure_engine_source(parser(data), engine_name)
        except urllib.error.URLError as e:
            logger.warning(f"HTTP 引擎连接失败: {e.reason}")
            return []
        except urllib.error.HTTPError as e:
            logger.warning(f"HTTP 引擎 HTTP {e.code}: {e.reason}")
            return []
        except TimeoutError:
            logger.warning(f"HTTP 引擎超时（{to}s）")
            return []
        except Exception as e:
            logger.warning(f"HTTP 引擎未知异常: {type(e).__name__}: {e}")
            return []

    return _engine


def _build_local_engine(spec: dict[str, Any]) -> Any:
    """构造 T2 local-search 引擎调用函数。

    通过子进程导入 search_v3 模块，调用 search() 函数。
    规格字段：
      - search_v3_path: search_v3.py 路径（默认项目内 local-search/search_v3.py）
      - max_engines: 最大并行引擎数（默认 3）
      - categories: 可选，按类别过滤引擎列表
    """
    search_v3_path = spec.get("search_v3_path", str(Path(__file__).resolve().parent.parent / "local-search" / "search_v3.py"))
    max_engines = spec.get("max_engines", 3)
    categories = spec.get("categories", None)
    engine_name = spec.get("_name", "local")

    @safe_search
    def _engine(query: str, n: int = 5, timeout: float = 8, **kwargs) -> list[dict[str, Any]]:
        resolved_path = _resolve_placeholders(search_v3_path, query, n)
        cats_json = json.dumps(categories) if categories else "None"

        code = (
            f"import sys,json;from pathlib import Path;"
            f"p=Path({json.dumps(resolved_path)});"
            f"sys.path.insert(0,str(p.parent));"
            f"from search_v3 import search as local_search;"
            f"r=local_search(query={json.dumps(query)},max_engines={max_engines});"
            f"print(json.dumps(r,ensure_ascii=False))"
        )

        python_bin = spec.get("python_bin", sys.executable)
        out = _run([python_bin, "-c", code], timeout=timeout, engine_name=engine_name)
        return _parse_local(out)

    return _engine


_BUILDERS = {
    "cli": _build_cli_engine,
    "python": _build_python_engine,
    "http": _build_http_engine,
    "t2_local": _build_local_engine,
}


# ═══════════════════════════════════════════════════════════════════════════════
# 结果解析器
# ═══════════════════════════════════════════════════════════════════════════════

def _ensure_engine_source(
    results: list[dict[str, Any]] | Any, engine_name: str
) -> list[dict[str, Any]]:
    """纠正结果 source，避免 HTTP 解析器错标（如 uapi→wigolo）。

    规则：
      - 空 / generic → 设为引擎名
      - source 既不等于引擎名、也不以「引擎名/」开头 → 纠正为引擎名
      - wigolo_npx 允许保留 wigolo/... 子源标注
    """
    if not isinstance(results, list) or not engine_name:
        return results if isinstance(results, list) else []
    for r in results:
        if not isinstance(r, dict) or "error" in r:
            continue
        src = str(r.get("source") or "")
        if engine_name == "wigolo_npx" and src.startswith("wigolo"):
            continue
        if not src or src == "generic":
            r["source"] = engine_name
        elif src != engine_name and not src.startswith(engine_name + "/"):
            r["source"] = engine_name
    return results


def _parse_anysearch(data: Any) -> list[dict[str, Any]]:
    """解析 AnySearch 响应（支持 JSON-RPC 和 CLI 文本格式）。

    JSON-RPC 响应格式：
      {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": "..."}]}}
    CLI 文本格式：
      ## Search Results (N results, Xms)
      ### N. title
      - **URL**: https://...
    """
    # JSON-RPC 响应（HTTP 直连）
    if isinstance(data, dict):
        # JSON-RPC 格式
        if "result" in data or "jsonrpc" in data:
            result = data.get("result", {})
            content = result.get("content", [])
            text_content = ""
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_content = item.get("text", "")
                    break
            if not text_content:
                # 尝试直接从 result 中提取
                text_content = json.dumps(result, ensure_ascii=False)

            # 解析文本内容（可能是 JSON 数组或 Markdown 格式）
            text_content = text_content.strip()
            if text_content.startswith("["):
                try:
                    items = json.loads(text_content)
                    if isinstance(items, list):
                        return [
                            {
                                "title": item.get("title", "")[:200],
                                "url": item.get("url", ""),
                                "snippet": item.get("snippet", "")[:300],
                                "source": "anysearch",
                                "score": item.get("score", max(1.0 - i * 0.1, 0.1)),
                            }
                            for i, item in enumerate(items)
                            if isinstance(item, dict) and (item.get("title") or item.get("url"))
                        ][:10]
                except json.JSONDecodeError:
                    pass

            # Markdown 格式解析
            results, cur = [], {}
            seen_url = False
            for line in text_content.split("\n"):
                s = line.strip()
                if s.startswith("### "):
                    if cur:
                        results.append(cur)
                    title = s[4:].strip()
                    import re as _re
                    title = _re.sub(r'^\d+\.\s*', '', title)
                    cur = {"title": title, "source": "anysearch",
                           "score": max(1.0 - len(results) * 0.1, 0.1)}
                    seen_url = False
                elif s.startswith("- **URL**: ") and cur:
                    cur["url"] = s[11:].strip()
                    seen_url = True
                elif s.startswith("- ") and not s.startswith("- **") and seen_url and cur:
                    content = s[2:].strip()
                    content = ' '.join(content.split())
                    cur["snippet"] = content[:300]
                    seen_url = False
            if cur:
                results.append(cur)
            return results[:10]

        # 直接 JSON 结果格式
        if "results" in data:
            items = data.get("results", [])
            if isinstance(items, list):
                return [
                    {
                        "title": item.get("title", "")[:200],
                        "url": item.get("url", ""),
                        "snippet": item.get("snippet", item.get("content", ""))[:300],
                        "source": "anysearch",
                        "score": item.get("score", max(1.0 - i * 0.1, 0.1)),
                    }
                    for i, item in enumerate(items)
                    if isinstance(item, dict)
                ][:10]

    # CLI 文本格式（向后兼容）
    if isinstance(data, str):
        results, cur = [], {}
        seen_url = False
        for line in data.split("\n"):
            s = line.strip()
            if s.startswith("### "):
                if cur:
                    results.append(cur)
                title = s[4:].strip()
                import re as _re
                title = _re.sub(r'^\d+\.\s*', '', title)
                cur = {"title": title, "source": "anysearch",
                       "score": max(1.0 - len(results) * 0.1, 0.1)}
                seen_url = False
            elif s.startswith("- **URL**: ") and cur:
                cur["url"] = s[11:].strip()
                seen_url = True
            elif s.startswith("- ") and not s.startswith("- **") and seen_url and cur:
                content = s[2:].strip()
                content = ' '.join(content.split())
                cur["snippet"] = content[:300]
                seen_url = False
        if cur:
            results.append(cur)
        return results[:10]

    return []


def _parse_tavily(text: str) -> list[dict[str, Any]]:
    try:
        items = json.loads(text.strip())
        return [
            {"title": i.get("title", ""), "url": i.get("url", ""),
             "snippet": i.get("content", "")[:200], "score": i.get("score", 0.5),
             "source": "tavily"}
            for i in items if isinstance(i, dict)
        ]
    except Exception:
        return []


def _parse_zhihu(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text)
        return [
            {"title": i.get("title", ""), "url": i.get("url", ""),
             "snippet": i.get("summary", "")[:200],
             "meta": {"author": i.get("author", ""), "votes": i.get("votes", 0)},
             "source": "zhihu"}
            for i in data.get("items", [])
        ]
    except Exception:
        return []


def _parse_byted(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析字节搜索 API 响应。

    响应结构：{ResponseMetadata: {...}, Result: {WebResults: [{Title, Url, Summary, SiteName}]}}
    """
    results = []
    result_obj = data.get("Result", {})
    if isinstance(result_obj, dict):
        web_results = result_obj.get("WebResults", [])
        for r in web_results:
            if isinstance(r, dict):
                results.append({
                    "title": r.get("Title", ""),
                    "url": r.get("Url", ""),
                    "snippet": r.get("Summary", "")[:300],
                    "source": "byted",
                })
    return results[:10]


def _parse_duckduckgo(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 DuckDuckGo Instant Answer API 响应。"""
    results: list[dict[str, Any]] = []

    # 提取 Abstract（如果有）
    abstract = data.get("Abstract", "")
    if abstract:
        results.append({
            "title": data.get("Heading", "DuckDuckGo Answer"),
            "url": data.get("AbstractURL", ""),
            "snippet": abstract[:300],
            "source": "duckduckgo",
        })

    # 提取 RelatedTopics
    for topic in data.get("RelatedTopics", [])[:5]:
        if isinstance(topic, dict) and "Text" in topic:
            results.append({
                "title": topic.get("Text", "")[:100],
                "url": topic.get("FirstURL", ""),
                "snippet": topic.get("Text", "")[:300],
                "source": "duckduckgo",
            })

    return results[:5]


def _parse_uapi(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 UAPI 搜索响应。"""
    results = data.get("results", [])
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""),
         "snippet": r.get("snippet", "")[:300],
         "source": "uapi"}
        for r in results if isinstance(r, dict)
    ][:10]


def _parse_crossref(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 Crossref API 响应。

    响应结构：
      {"message": {"items": [{"DOI", "title", "author", "is-referenced-by-count",
                               "container-title", "URL", "abstract"}]}}
    """
    results: list[dict[str, Any]] = []
    message = data.get("message", {})
    items = message.get("items", [])
    if not isinstance(items, list):
        return []

    for item in items:
        if not isinstance(item, dict):
            continue
        doi = item.get("DOI", "")
        titles = item.get("title", [])
        title = titles[0] if isinstance(titles, list) and titles else str(titles) if titles else ""
        url = item.get("URL", "") or f"https://doi.org/{doi}" if doi else ""
        cited_by = item.get("is-referenced-by-count", 0)
        container = item.get("container-title", [])
        venue = container[0] if isinstance(container, list) and container else ""

        # 作者
        authors_raw = item.get("author", [])
        authors = []
        if isinstance(authors_raw, list):
            for a in authors_raw[:5]:
                if isinstance(a, dict):
                    given = a.get("given", "")
                    family = a.get("family", "")
                    name = f"{given} {family}".strip()
                    if name:
                        authors.append(name)
        author_str = ", ".join(authors)

        # 摘要（Crossref 有时含 HTML 标签）
        abstract = item.get("abstract", "") or ""
        if abstract:
            import re as _re
            abstract = _re.sub(r"<[^>]+>", "", abstract)[:200]

        snippet_parts = [abstract] if abstract else []
        if cited_by:
            snippet_parts.append(f"引用: {cited_by}")
        if author_str:
            snippet_parts.append(f"作者: {author_str}")
        if venue:
            snippet_parts.append(f"期刊: {venue}")
        snippet = " | ".join(snippet_parts)

        results.append({
            "title": title,
            "url": url,
            "snippet": snippet[:300],
            "score": min(1.0, cited_by / 500) if cited_by else 0.5,
            "source": "crossref",
            "meta": {
                "doi": doi,
                "cited_by": cited_by,
                "authors": authors,
                "venue": venue,
            },
        })
    return results[:10]


def _parse_github(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 GitHub Search API 响应。

    响应结构（code search）：
      {"items": [{"name", "path", "repository": {"full_name", "html_url"}, "html_url"}]}
    响应结构（repository search）：
      {"items": [{"name", "full_name", "html_url", "description", "stargazers_count", "language"}]}
    """
    results: list[dict[str, Any]] = []
    items = data.get("items", [])
    if not isinstance(items, list):
        return []

    for item in items:
        if not isinstance(item, dict):
            continue

        # 代码搜索结果
        if "repository" in item and isinstance(item["repository"], dict):
            repo = item["repository"]
            file_name = item.get("name", "")
            path = item.get("path", "")
            repo_name = repo.get("full_name", "")
            html_url = item.get("html_url", "")
            results.append({
                "title": f"{repo_name}/{path}" if path else file_name,
                "url": html_url,
                "snippet": f"仓库: {repo_name} | 路径: {path}",
                "source": "github",
                "meta": {"type": "code", "repo": repo_name, "file": file_name},
            })
        # 仓库搜索结果
        elif "full_name" in item:
            repo_name = item.get("full_name", "")
            desc = item.get("description", "") or ""
            stars = item.get("stargazers_count", 0)
            lang = item.get("language", "") or ""
            html_url = item.get("html_url", "")
            snippet_parts = [desc[:200]] if desc else []
            if lang:
                snippet_parts.append(f"语言: {lang}")
            if stars:
                snippet_parts.append(f"Stars: {stars}")
            results.append({
                "title": repo_name,
                "url": html_url,
                "snippet": " | ".join(snippet_parts)[:300],
                "score": min(1.0, stars / 10000) if stars else 0.5,
                "source": "github",
                "meta": {"type": "repo", "stars": stars, "language": lang},
            })
    return results[:10]


def _parse_wikipedia(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 Wikipedia Search API 响应。

    响应结构：
      {"query": {"search": [{"title", "snippet", "pageid"}]}}
    """
    results: list[dict[str, Any]] = []
    query_obj = data.get("query", {})
    search_items = query_obj.get("search", [])
    if not isinstance(search_items, list):
        return []

    for item in search_items:
        if not isinstance(item, dict):
            continue
        title = item.get("title", "")
        snippet = item.get("snippet", "") or ""
        pageid = item.get("pageid", 0)
        # 清理 HTML 标签
        import re as _re
        snippet_clean = _re.sub(r"<[^>]+>", "", snippet)
        url = f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}" if title else ""

        results.append({
            "title": title,
            "url": url,
            "snippet": snippet_clean[:300],
            "source": "wikipedia",
            "meta": {"pageid": pageid},
        })
    return results[:10]


def _parse_metaso(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析秘塔搜索 API 响应。

    响应结构（chat_completions 格式）：
      {"choices": [{"message": {"content": "...", "search_results": [...]}}]}
    或直接搜索结果：
      {"results": [{"title", "url", "snippet"}]}
    """
    results: list[dict[str, Any]] = []

    # 尝试 chat_completions 格式
    choices = data.get("choices", [])
    if choices and isinstance(choices, list):
        msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        search_results = msg.get("search_results", [])
        if search_results and isinstance(search_results, list):
            for item in search_results:
                if not isinstance(item, dict):
                    continue
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "snippet": (item.get("snippet", "") or item.get("content", ""))[:300],
                    "source": "metaso",
                })
            return results[:10]

    # 尝试直接结果格式
    items = data.get("results", data.get("data", []))
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": (item.get("snippet", "") or item.get("content", ""))[:300],
                "source": "metaso",
            })
    return results[:10]


def _parse_wolframalpha(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 WolframAlpha Full Results API 响应。

    响应结构：
      {"queryresult": {"pods": [{"title", "subpods": [{"plaintext"}]}]}}
    """
    results: list[dict[str, Any]] = []
    query_result = data.get("queryresult", {})
    pods = query_result.get("pods", [])
    if not isinstance(pods, list):
        return []

    for pod in pods:
        if not isinstance(pod, dict):
            continue
        pod_title = pod.get("title", "")
        subpods = pod.get("subpods", [])
        if not isinstance(subpods, list):
            continue
        for sub in subpods:
            if not isinstance(sub, dict):
                continue
            plaintext = sub.get("plaintext", "") or ""
            if not plaintext or plaintext == "":
                continue
            # 跳过输入回显 pod
            if pod_title.lower() in ("input", "input interpretation"):
                continue
            results.append({
                "title": pod_title,
                "url": "",
                "snippet": plaintext[:300],
                "score": 0.9,
                "source": "wolframalpha",
            })
    return results[:5]


def _parse_brave(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 Brave Search API 响应。

    响应结构：
      {"web": {"results": [{"title", "url", "description"}]}}
    """
    results: list[dict[str, Any]] = []
    web = data.get("web", {})
    items = web.get("results", []) if isinstance(web, dict) else []
    if not isinstance(items, list):
        return []

    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": (item.get("description", "") or "")[:300],
            "score": max(0.8 - i * 0.05, 0.3),
            "source": "brave",
        })
    return results[:10]


def _parse_bocha(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析博查搜索 API 响应。

    响应结构：
      {"webPages": {"value": [{"title", "url", "snippet", "summary", "datePublished", "siteName"}]}}
    """
    results: list[dict[str, Any]] = []
    web_pages = data.get("webPages", {})
    if isinstance(web_pages, dict):
        pages = web_pages.get("value", [])
    elif isinstance(web_pages, list):
        pages = web_pages
    else:
        pages = []

    for page in pages:
        if not isinstance(page, dict):
            continue
        title = page.get("title", "")
        url = page.get("url", "")
        snippet = page.get("snippet", "") or page.get("summary", "")
        date_published = page.get("datePublished", "")
        site_name = page.get("siteName", "")
        if not title and not url:
            continue
        results.append({
            "title": title,
            "url": url,
            "snippet": snippet[:300],
            "source": "bocha",
            "meta": {
                "date": date_published,
                "site": site_name,
            },
        })
    return results[:10]


def _parse_openalex(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 OpenAlex API 响应。

    响应结构：
      {"results": [{"title", "doi", "cited_by_count", "authorships", "primary_location", ...}]}
    """
    results: list[dict[str, Any]] = []
    papers = data.get("results", [])
    if not isinstance(papers, list):
        return []

    for paper in papers:
        if not isinstance(paper, dict):
            continue
        title = paper.get("title", "")
        doi = paper.get("doi", "") or ""
        cited_by = paper.get("cited_by_count", 0)

        # 获取作者
        authorships = paper.get("authorships", [])
        authors = [
            a.get("author", {}).get("display_name", "")
            for a in authorships
            if isinstance(a, dict) and isinstance(a.get("author"), dict)
        ][:5]
        author_str = ", ".join(authors)

        # 获取发表期刊/来源
        primary_loc = paper.get("primary_location", {}) or {}
        source = primary_loc.get("source", {}) or {}
        venue = source.get("display_name", "")

        # 获取 PDF / 开放获取链接
        best_oa = paper.get("best_oa_location", {}) or {}
        pdf_url = best_oa.get("pdf_url", "") or ""
        landing = best_oa.get("landing_page_url", "") or ""
        url = pdf_url or landing or doi

        # 摘要（OpenAlex 有时分段存储）
        abstract_inv = paper.get("abstract_inverted_index", {}) or {}
        if abstract_inv:
            # 反转索引 → 正序文本
            word_positions: list[tuple[int, str]] = []
            for word, positions in abstract_inv.items():
                if isinstance(positions, list):
                    for pos in positions:
                        word_positions.append((pos, word))
            word_positions.sort(key=lambda x: x[0])
            abstract = " ".join(w for _, w in word_positions)
        else:
            abstract = ""

        snippet_parts = [abstract[:200]] if abstract else []
        if cited_by:
            snippet_parts.append(f"引用: {cited_by}")
        if author_str:
            snippet_parts.append(f"作者: {author_str}")
        if venue:
            snippet_parts.append(f"来源: {venue}")
        snippet = " | ".join(snippet_parts)

        results.append({
            "title": title,
            "url": url,
            "snippet": snippet[:300],
            "score": min(1.0, cited_by / 500) if cited_by else 0.5,
            "source": "openalex",
            "meta": {
                "doi": doi,
                "cited_by": cited_by,
                "authors": authors,
                "venue": venue,
            },
        })
    return results[:10]


def _parse_felo(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 Felo Search API 响应。

    响应结构：
      {status: "ok", data: {answer: "AI答案", resources: [{link, title, snippet}]}}
    """
    if not isinstance(data, dict):
        return []

    payload = data.get("data", data)
    if not isinstance(payload, dict):
        return []

    results: list[dict[str, Any]] = []

    answer = payload.get("answer") or ""
    if isinstance(answer, str) and answer.strip():
        results.append({
            "title": "Felo AI Answer",
            "url": "",
            "snippet": answer.strip()[:300],
            "score": 0.9,
            "source": "felo",
            "metadata": {"type": "answer"},
        })

    resources = payload.get("resources") or []
    if not isinstance(resources, list):
        resources = []

    for i, r in enumerate(resources):
        if not isinstance(r, dict):
            continue
        title = r.get("title") or ""
        url = r.get("link") or r.get("url") or ""
        snippet = r.get("snippet") or r.get("content") or ""
        if not title and not url and not snippet:
            continue
        results.append({
            "title": title or url or "Felo Result",
            "url": url,
            "snippet": (snippet[:300] if isinstance(snippet, str) else ""),
            "score": max(0.8 - i * 0.05, 0.3),
            "source": "felo",
        })

    return results[:10]


def _parse_semantic_scholar(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 Semantic Scholar API 响应。"""
    papers = data.get("data") or []
    if not isinstance(papers, list):
        return []
    results: list[dict[str, Any]] = []

    for paper in papers:
        if not isinstance(paper, dict):
            continue

        title = paper.get("title", "")
        abstract = paper.get("abstract", "") or ""
        citation_count = paper.get("citationCount", 0)

        # 获取 PDF 链接
        pdf_info = paper.get("openAccessPdf", {})
        url = pdf_info.get("url", "") if isinstance(pdf_info, dict) else ""

        # 获取作者
        authors = paper.get("authors", [])
        author_names = [a.get("name", "") for a in authors if isinstance(a, dict)][:3]
        author_str = ", ".join(author_names)

        snippet = f"{abstract[:200]}"
        if citation_count:
            snippet += f" [引用: {citation_count}]"
        if author_str:
            snippet += f" [作者: {author_str}]"

        results.append({
            "title": title,
            "url": url,
            "snippet": snippet[:300],
            "score": min(1.0, citation_count / 1000) if citation_count else 0.5,
            "source": "semantic_scholar",
        })

    return results[:10]


def _parse_arxiv(text: str) -> list[dict[str, Any]]:
    """解析 arXiv Atom XML 响应，提取论文信息。"""
    import xml.etree.ElementTree as ET

    results: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}

        entries = root.findall(".//atom:entry", ns)
        if not entries:
            entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")

        for entry in entries:
            title = entry.findtext("atom:title", "", ns).strip()
            summary = entry.findtext("atom:summary", "", ns).strip()

            # 提取 arxiv ID 作为 URL
            entry_id = entry.findtext("atom:id", "", ns)
            url = entry_id if entry_id else ""

            # 提取 PDF 链接
            for link in entry.findall("atom:link", ns):
                if link.get("title") == "pdf":
                    url = link.get("href", url)
                    break

            if title:
                results.append({
                    "title": title.replace("\n", " ")[:200],
                    "url": url,
                    "snippet": summary.replace("\n", " ")[:300],
                    "source": "arxiv",
                })
    except ET.ParseError:
        pass

    return results


def _parse_eastmoney(text: str) -> list[dict[str, Any]]:
    """解析 EastMoney 输出，提取结构化资讯。

    支持三种格式：
    1. 嵌套 JSON 格式（data.data.llmSearchResponse.data）
    2. 简单 JSON 格式（items/results 列表）
    3. 文本格式（[INV_NEWS] 或 📰 开头，后跟 URL 和摘要行）
    """
    # 先尝试 JSON 解析
    try:
        data = json.loads(text.strip())
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            # 尝试多种嵌套路径
            items = (
                data.get("items") or
                data.get("results") or
                data.get("data", {}).get("data", {}).get("llmSearchResponse", {}).get("data", []) or
                data.get("data", {}).get("llmSearchResponse", {}).get("data", []) or
                []
            )
        else:
            items = []
        if items:
            return [
                {"title": i.get("title", ""), "url": i.get("jumpUrl", i.get("url", "")),
                 "snippet": i.get("summary", i.get("content", ""))[:300],
                 "source": "eastmoney"}
                for i in items if isinstance(i, dict)
            ]
    except Exception:
        pass

    # 文本格式解析：提取 [INV_NEWS] / 📰 开头的结构化资讯
    results: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            if current:
                results.append(current)
                current = {}
            continue
        # 匹配 [INV_NEWS] 或 📰 开头的行作为新条目起点
        if line.startswith("[INV_NEWS]") or line.startswith("📰"):
            if current:
                results.append(current)
            title = line.replace("[INV_NEWS]", "").replace("📰", "").strip()
            # 跳过纯统计行（如 "找到 15 条资讯"）
            if re.match(r"^找到\s*\d+\s*条", title):
                current = {}
                continue
            current = {"title": title[:200], "source": "eastmoney"}
        elif line.startswith("http"):
            current["url"] = line
        elif current and "title" in current and "snippet" not in current:
            current["snippet"] = line[:300]
    if current:
        results.append(current)
    # 过滤掉无 title 的脏条目
    return [r for r in results if r.get("title")][:10]


def _parse_wigolo(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw = data.get("results", data.get("data", []))
    if isinstance(raw, dict):
        raw = raw.get("results", [])
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""),
         "snippet": r.get("snippet", "")[:200],
         "score": r.get("relevance_score", r.get("score", 0.5)),
         "source": "wigolo"}
        for r in raw if isinstance(r, dict)
    ]


def _parse_generic(text: str) -> list[dict[str, Any]]:
    """对未知 CLI 引擎的通用解析：优先 JSON，其次按行拆分。"""
    try:
        data = json.loads(text.strip())
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            return data.get("results", data.get("items", data.get("data", [])))
    except Exception:
        pass
    results = []
    for line in text.split("\n"):
        line = line.strip()
        if line and len(line) > 5:
            results.append({"title": line[:120], "snippet": line[:300], "source": "generic"})
    return results[:10]


def _parse_searxng(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 SearXNG JSON 响应。"""
    results = data.get("results", [])
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""),
         "snippet": r.get("content", "")[:300],
         "score": r.get("score", 0.5),
         "source": f"searxng/{r.get('engine', '?')}"}
        for r in results if isinstance(r, dict)
    ][:10]


def _parse_wigolo_npx(text: str) -> list[dict[str, Any]]:
    """解析 npx wigolo search --json 输出。"""
    try:
        data = json.loads(text.strip())
    except Exception:
        return []
    results = data.get("results", data.get("data", []))
    if isinstance(results, dict):
        results = results.get("results", [])
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""),
         "snippet": r.get("snippet", r.get("content", ""))[:300],
         "score": r.get("relevance_score", r.get("score", 0.5)),
         "source": f"wigolo/{r.get('source', r.get('engine', '?'))}"}
        for r in results if isinstance(r, dict) and r.get("title")
    ][:10]


def _parse_local(text: str) -> list[dict[str, Any]]:
    """解析 local-search search_v3 的 JSON 输出。

    输入格式（search_v3.search() 返回值）：
      {"query": "...", "query_type": "...", "engines_used": [...], "results": [...], "count": N}
    """
    try:
        data = json.loads(text.strip())
        if isinstance(data, dict):
            results = data.get("results", [])
            # 确保每个结果有 source 字段
            for r in results:
                if isinstance(r, dict) and "source" not in r:
                    r["source"] = "local/" + r.get("source", "unknown")
            return results[:10]
        return []
    except Exception:
        return []


def _parse_searxng(text: str) -> list[dict[str, Any]]:
    """解析 searxng_bridge 的 JSON 输出。

    输入格式（searxng_proxy_search() 返回值）：
      {"query": "...", "results": [...], "engines_used": [...], "count": N, "elapsed_ms": N, "error": null}
    """
    try:
        data = json.loads(text.strip())
        if isinstance(data, dict):
            if data.get("error"):
                return []  # SearXNG 不可达或报错
            return data.get("results", [])[:10]
        return []
    except Exception:
        return []


_PARSERS = {
    "anysearch": _parse_anysearch,
    "tavily": _parse_tavily,
    "zhihu": _parse_zhihu,
    "eastmoney": _parse_eastmoney,
    "arxiv": _parse_arxiv,
    "wigolo": _parse_wigolo,
    "wigolo_npx": _parse_wigolo_npx,
    "searxng": _parse_searxng,
    "byted": _parse_byted,
    "duckduckgo": _parse_duckduckgo,
    "uapi": _parse_uapi,
    "semantic_scholar": _parse_semantic_scholar,
    "felo": _parse_felo,
    "bocha": _parse_bocha,
    "openalex": _parse_openalex,
    "crossref": _parse_crossref,
    "github": _parse_github,
    "wikipedia": _parse_wikipedia,
    "metaso": _parse_metaso,
    "wolframalpha": _parse_wolframalpha,
    "brave": _parse_brave,
    "local": _parse_local,
    "searxng_bridge": _parse_searxng,
}


# ═══════════════════════════════════════════════════════════════════════════════
# 引擎注册表
# ═══════════════════════════════════════════════════════════════════════════════

_engine_registry: dict[str, Any] = {}
_engine_registry_loaded = False


def _load_registry():
    """从 config.yaml 构建引擎函数注册表。"""
    global _engine_registry, _engine_registry_loaded
    if _engine_registry_loaded:
        return

    cfg = load_config()
    engines = get_engines(cfg)
    registry = {}

    for name, spec in engines.items():
        spec = dict(spec)
        spec["_name"] = name
        engine_type = spec.get("type", "cli")
        builder = _BUILDERS.get(engine_type)
        if builder:
            registry[name] = builder(spec)
        else:
            logger.warning(f"未知引擎类型: {engine_type} (引擎 {name})")

    _engine_registry = registry
    _engine_registry_loaded = True


def get_registry() -> dict[str, Any]:
    """返回引擎名到调用函数的映射。"""
    _load_registry()
    return _engine_registry


def available_engines() -> list[str]:
    """返回当前可用的引擎列表。"""
    return sorted(get_registry().keys())


# ═══════════════════════════════════════════════════════════════════════════════
# 统一入口
# ═══════════════════════════════════════════════════════════════════════════════

def search(
    query: str,
    engine: str,
    n: int = 5,
    timeout: float = 8,
    depth: str = "fast",
    domain: str | None = None,
    profile: str | None = None,
) -> list[dict[str, Any]]:
    """统一引擎调用入口；失败返回空 list，不抛异常；记录耗时。

    domain/profile 用于 wigolo 场景化（category / 子引擎选择）。
    """
    registry = get_registry()
    fn = registry.get(engine)
    if not fn:
        logger.warning(f"未知引擎: {engine}")
        return []

    t0 = time.time()
    try:
        results = fn(
            query, n, timeout, depth=depth, domain=domain, profile=profile
        )
    except TypeError:
        # 兼容未实现 depth/domain 参数的引擎
        try:
            results = fn(query, n, timeout, depth=depth)
        except TypeError:
            try:
                results = fn(query, n, timeout)
            except Exception as e:
                logger.error(f"引擎 {engine} 顶层异常: {type(e).__name__}: {e}")
                results = []
        except Exception as e:
            logger.error(f"引擎 {engine} 顶层异常: {type(e).__name__}: {e}")
            results = []
    except Exception as e:
        logger.error(f"引擎 {engine} 顶层异常: {type(e).__name__}: {e}")
        results = []

    elapsed = time.time() - t0
    if results and isinstance(results, list):
        # 使用 normalize_result 统一字段格式
        normalized = []
        for r in results:
            if "error" in r:
                normalized.append(r)
                continue
            sr = normalize_result(r, engine)
            d = sr.to_dict()
            d["_engine"] = engine
            d["_elapsed"] = round(elapsed, 3)
            normalized.append(d)
        return normalized
    return results if isinstance(results, list) else []


# ═══════════════════════════════════════════════════════════════════════════════
# Bocha Semantic Reranker — 结果精排层
# ═══════════════════════════════════════════════════════════════════════════════

def rerank_results(query: str, results: list[dict[str, Any]],
                   top_n: int = 10, timeout: float = 5) -> list[dict[str, Any]]:
    """使用博查语义排序模型对搜索结果进行二次精排。

    将 RRF 融合后的结果按 query 做语义相关性重排序，提升最终排序质量。
    失败时静默返回原始结果（不阻断搜索流程）。
    """
    if not results or len(results) <= 1:
        return results

    api_key = os.environ.get("BOCHA_API_KEY", "")
    if not api_key:
        return results

    # 构建文档列表：用 title + snippet 拼接
    documents = []
    for r in results:
        title = r.get("title", "")
        snippet = r.get("snippet", "")
        doc_text = f"{title} {snippet}".strip()
        if doc_text:
            documents.append(doc_text)
        else:
            documents.append(title or "empty")

    if not documents:
        return results

    payload = json.dumps({
        "model": "gte-rerank",
        "query": query,
        "documents": documents[:50],  # API 最多 50 个文档
        "top_n": min(top_n, len(documents)),
        "return_documents": False,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.bocha.cn/v1/rerank",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            rerank_data = data.get("data", {})
            rerank_results_list = rerank_data.get("results", [])

            if not rerank_results_list:
                return results

            # 按 rerank score 重建排序
            scored_results = []
            for rr in rerank_results_list:
                idx = rr.get("index", -1)
                score = rr.get("relevance_score", 0)
                if 0 <= idx < len(results):
                    item = dict(results[idx])
                    item["rerank_score"] = round(score, 4)
                    # 综合得分：rerank_score 为主，原始 score 为辅
                    orig_score = item.get("score", 0) or 0
                    item["score"] = round(score * 0.7 + orig_score * 0.3, 4)
                    scored_results.append(item)

            if scored_results:
                scored_results.sort(key=lambda x: x.get("score", 0), reverse=True)
                return scored_results[:top_n]
            return results

    except Exception as e:
        logger.warning(f"Reranker 失败，使用原始排序: {e}")
        return results


# ── CLI 调试用 ─────────────────────────────────────────────────────────────────
def _cli():
    import argparse

    parser = argparse.ArgumentParser(description="引擎适配层调试")
    parser.add_argument("query", nargs="?", default=None)
    parser.add_argument("--engine", "-e", default="anysearch")
    parser.add_argument("-n", type=int, default=5)
    parser.add_argument("--timeout", "-t", type=float, default=8)
    parser.add_argument("--list", action="store_true", help="列出可用引擎")
    args = parser.parse_args()

    if args.list:
        print(json.dumps(available_engines(), ensure_ascii=False, indent=2))
        return

    if args.query is None:
        parser.error("必须提供 query 或使用 --list")

    results = search(args.query, args.engine, args.n, args.timeout)
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()
