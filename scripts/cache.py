#!/usr/bin/env python3
"""
cache.py — 统一搜索双层缓存引擎

功能：
  L1: 内存 LRU 热缓存（100 条），避免同进程重复查询
  L2: SQLite 持久化缓存（TTL 可配置），跨进程复用
  新增：基于 query domain 的分级 TTL（金融/新闻/实时/通用/研究/常青）

用法：
  from cache import SearchCache

  cache = SearchCache()
  hit = cache.get("python async", "auto", 5, domain="tech")
  if hit:
      print(hit)
  else:
      cache.set("python async", "auto", 5, {"results": [...]}, domain="tech")

CLI：
  python3 cache.py get "python async" auto 5 [domain]
  python3 cache.py set "python auto 5" '{"results": [...]}' [domain]
  python3 cache.py clear --older-than 24
  python3 cache.py stats
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from typing import Optional

# 优先从当前目录导入 config
try:
    from config import get_cache_config
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from config import get_cache_config


# ── 常量 ──────────────────────────────────────────────────────────────────────

DEFAULT_DB_PATH = "~/.cache/unified-search/cache.db"
DEFAULT_TTL = 3600  # 1 小时
MAX_MEMORY_ITEMS = 1000  # 与 cache_strategy l1_memory.max_size 对齐
MAX_DB_SIZE_MB = 500     # 与 cache_strategy l2_sqlite.max_size_mb 对齐
COMPRESSION_THRESHOLD = 1024
COMPRESSION_LEVEL = 6

# 分级 TTL（秒）
CACHE_TIERS = {
    "financial": 300,    # 金融: 5 分钟
    "news": 600,         # 新闻: 10 分钟
    "realtime": 900,     # 实时: 15 分钟
    "general": 3600,     # 通用: 1 小时
    "research": 7200,    # 研究: 2 小时
    "evergreen": 86400,  # 常青: 24 小时
}

# query domain → TTL tier 映射（键与 config.yaml domains.name 对齐）
DOMAIN_TIER_MAP = {
    # config.yaml 正式域名
    "stock_query": "financial",
    "fund_query": "financial",
    "financial_news": "news",
    "zhihu_content": "general",
    "tech_deep": "research",
    "news_realtime": "realtime",
    "general_search": "general",
    # 兼容别名 / 历史值
    "stock": "financial",
    "fund": "financial",
    "fin_news": "news",
    "news": "realtime",
    "hot": "news",
    "zhihu": "general",
    "deep": "research",
    "compare": "research",
    "tech": "research",
    "general": "general",
    "auto": "general",
}


# ── LRU 内存缓存 ───────────────────────────────────────────────────────────────

class LRUCache:
    """基于 dict 的简单 LRU（Python 3.7+ dict 有序）"""

    def __init__(self, max_size: int = MAX_MEMORY_ITEMS):
        self._max_size = max_size
        self._store: OrderedDict[str, dict] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._lock = threading.RLock()

    def get(self, key: str) -> Optional[dict]:
        with self._lock:
            if key in self._store:
                self._hits += 1
                self._store.move_to_end(key)
                return self._store[key]
            self._misses += 1
            return None

    def set(self, key: str, value: dict):
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                self._store[key] = value
            else:
                if len(self._store) >= self._max_size:
                    self._store.popitem(last=False)
                self._store[key] = value

    def clear(self):
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "size": len(self._store),
                "hit_rate": round(
                    self._hits / max(self._hits + self._misses, 1), 3
                ),
            }


# ── SQLite 持久化缓存 ──────────────────────────────────────────────────────────

class SQLiteCache:
    """SQLite 持久化缓存，支持 TTL 过期、大小限制、gzip 压缩、分级 TTL。"""

    SCHEMA_VERSION = 2

    def __init__(self, db_path: str = DEFAULT_DB_PATH, ttl: int = DEFAULT_TTL):
        self._db_path = os.path.expanduser(db_path)
        self._ttl = ttl
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._ensure_dir()
        self._init_db()

    def _ensure_dir(self):
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='search_cache'"
            )
            table_exists = cursor.fetchone() is not None

            # 1. 创建表（可能缺少新列）
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS search_cache (
                    key TEXT PRIMARY KEY,
                    query TEXT NOT NULL,
                    engine TEXT NOT NULL,
                    max_results INTEGER NOT NULL,
                    domain TEXT DEFAULT 'general',
                    value_blob BLOB NOT NULL,
                    compressed INTEGER DEFAULT 0,
                    ttl INTEGER DEFAULT 3600,
                    created_at REAL NOT NULL,
                    accessed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)

            # 2. 迁移：添加可能缺失的列
            if table_exists:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(search_cache)")]
                if "domain" not in cols:
                    conn.execute("ALTER TABLE search_cache ADD COLUMN domain TEXT DEFAULT 'general'")
                if "ttl" not in cols:
                    conn.execute("ALTER TABLE search_cache ADD COLUMN ttl INTEGER DEFAULT 3600")

            # 3. 创建索引
            conn.executescript("""
                CREATE INDEX IF NOT EXISTS idx_search_cache_expires ON search_cache(created_at);
                CREATE INDEX IF NOT EXISTS idx_search_cache_domain ON search_cache(domain);
            """)

            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                ("schema_version", str(self.SCHEMA_VERSION)),
            )

    def _is_expired(self, created_at: float, ttl: int | None = None) -> bool:
        effective_ttl = ttl if ttl is not None else self._ttl
        return (time.time() - created_at) > effective_ttl

    @staticmethod
    def _serialize(value: dict) -> tuple[bytes, int]:
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        if len(raw) > COMPRESSION_THRESHOLD:
            return gzip.compress(raw, COMPRESSION_LEVEL), 1
        return raw, 0

    @staticmethod
    def _deserialize(blob: bytes, compressed: int) -> dict:
        raw = gzip.decompress(blob) if compressed else blob
        return json.loads(raw.decode("utf-8"))

    def get(self, key: str) -> Optional[dict]:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT value_blob, compressed, created_at, ttl FROM search_cache WHERE key = ?",
                    (key,),
                ).fetchone()
                if row is None:
                    self._misses += 1
                    return None
                value_blob, compressed, created_at, ttl = row
                if self._is_expired(created_at, ttl):
                    conn.execute("DELETE FROM search_cache WHERE key = ?", (key,))
                    conn.commit()
                    self._misses += 1
                    return None
                conn.execute(
                    "UPDATE search_cache SET accessed_at = ? WHERE key = ?",
                    (time.time(), key),
                )
                conn.commit()
                self._hits += 1
                return self._deserialize(value_blob, compressed)

    def set(self, key: str, query: str, engine: str, max_results: int,
            value: dict, domain: str = "general", ttl: int | None = None):
        with self._lock:
            blob, compressed = self._serialize(value)
            now = time.time()
            effective_ttl = ttl if ttl is not None else self._ttl
            with self._connect() as conn:
                conn.execute(
                    """INSERT OR REPLACE
                       INTO search_cache(key, query, engine, max_results, domain,
                                        value_blob, compressed, ttl,
                                        created_at, accessed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (key, query, engine, max_results, domain,
                     blob, compressed, effective_ttl, now, now),
                )
                conn.commit()
            self._evict_if_needed()

    def _evict_if_needed(self):
        """超过 100MB 时删除最旧的记录"""
        size_mb = self.size_mb
        if size_mb <= MAX_DB_SIZE_MB:
            return
        with self._connect() as conn:
            target_mb = MAX_DB_SIZE_MB * 0.8
            while self.size_mb > target_mb:
                row = conn.execute(
                    "SELECT key FROM search_cache ORDER BY accessed_at ASC LIMIT 1"
                ).fetchone()
                if not row:
                    break
                conn.execute("DELETE FROM search_cache WHERE key = ?", (row[0],))
                conn.commit()

    def clear(self, older_than_hours: int = 24):
        with self._lock:
            cutoff = time.time() - older_than_hours * 3600
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM search_cache WHERE created_at < ?",
                    (cutoff,),
                )
                conn.commit()

    @property
    def stats(self) -> dict:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*), SUM(LENGTH(value_blob)) FROM search_cache"
                ).fetchone()
                count = row[0] or 0
                total_bytes = row[1] or 0
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(
                    self._hits / max(self._hits + self._misses, 1), 3
                ),
                "size_mb": round(total_bytes / 1024 / 1024, 2),
                "entries": count,
            }

    @property
    def size_mb(self) -> float:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT SUM(LENGTH(value_blob)) FROM search_cache"
            ).fetchone()
        return (row[0] or 0) / 1024 / 1024

    def tier_stats(self) -> list[dict]:
        """按 domain 统计缓存占用。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT domain, COUNT(*), SUM(LENGTH(value_blob)), AVG(ttl) FROM search_cache GROUP BY domain"
            ).fetchall()
        return [
            {
                "domain": domain,
                "entries": cnt,
                "size_kb": round((bytes_ or 0) / 1024, 1),
                "avg_ttl_s": round(avg_ttl or DEFAULT_TTL, 0),
            }
            for domain, cnt, bytes_, avg_ttl in rows
        ]


# ── 双层缓存入口 ───────────────────────────────────────────────────────────────

class SearchCache:
    """
    双层缓存引擎：L1 LRU + L2 SQLite

    缓存键 = SHA256(query + "|" + engine + "|" + str(max_results))[:32]
    支持按 domain 分级 TTL。
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH, ttl: int = DEFAULT_TTL):
        cfg = get_cache_config()
        self._db_path = os.path.expanduser(cfg.get("db_path", db_path))
        self._ttl = cfg.get("ttl", ttl)
        self._max_size_mb = cfg.get("max_size_mb", MAX_DB_SIZE_MB)
        self._l1 = LRUCache(max_size=MAX_MEMORY_ITEMS)
        self._l2 = SQLiteCache(db_path=self._db_path, ttl=self._ttl)

    @staticmethod
    def _key(query: str, engine: str, max_results: int) -> str:
        raw = f"{query}|{engine}|{max_results}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def resolve_ttl(domain: str = "general") -> int:
        """根据 domain 返回 TTL（秒）。"""
        tier = DOMAIN_TIER_MAP.get(domain, "general")
        return CACHE_TIERS.get(tier, DEFAULT_TTL)

    def get(self, query: str, engine: str, max_results: int,
            domain: str = "general") -> Optional[dict]:
        """
        返回缓存命中或 None。
        先查 L1（内存），未命中再查 L2（SQLite）。
        """
        key = self._key(query, engine, max_results)
        hit = self._l1.get(key)
        if hit is not None:
            ttl = hit.get("_ttl", self.resolve_ttl(domain))
            if time.time() - hit.get("_ts", 0) < ttl:
                hit["_cache_level"] = "L1"
                return hit
            self._l1._store.pop(key, None)

        hit = self._l2.get(key)
        if hit is not None:
            self._l1.set(key, hit)
            hit["_cache_level"] = "L2"
            return hit
        return None

    def set(self, query: str, engine: str, max_results: int, results: dict,
            domain: str = "general", ttl: int | None = None):
        """写入双层缓存。"""
        key = self._key(query, engine, max_results)
        effective_ttl = ttl if ttl is not None else self.resolve_ttl(domain)
        value = {**results, "_domain": domain, "_ttl": effective_ttl, "_ts": time.time()}
        self._l1.set(key, value)
        self._l2.set(key, query, engine, max_results, value, domain=domain, ttl=effective_ttl)

    def clear(self, older_than_hours: int = 24):
        """清理过期缓存（仅清 L2，L1 保持热数据）"""
        self._l2.clear(older_than_hours=older_than_hours)

    @property
    def stats(self) -> dict:
        """{'hits': N, 'misses': N, 'size_mb': N, 'l1': {...}, 'l2': {...}}"""
        l1 = self._l1.stats
        l2_stats = self._l2.stats
        tiers = self._l2.tier_stats()
        total_hits = l1["hits"] + l2_stats["hits"]
        total_misses = l1["misses"]
        return {
            "hits": total_hits,
            "misses": total_misses,
            "hit_rate": round(
                total_hits / max(total_hits + total_misses, 1), 3
            ),
            "size_mb": l2_stats["size_mb"],
            "entries": l2_stats["entries"],
            "l1": l1,
            "l2": l2_stats,
            "tiers": tiers,
        }


# ── CLI 入口 ───────────────────────────────────────────────────────────────────

def _cli():
    import argparse

    parser = argparse.ArgumentParser(description="Unified Search 缓存管理")
    sub = parser.add_subparsers(dest="cmd")

    p_get = sub.add_parser("get", help="查询缓存")
    p_get.add_argument("query")
    p_get.add_argument("engine", nargs="?", default="auto")
    p_get.add_argument("max_results", nargs="?", type=int, default=5)
    p_get.add_argument("--domain", default="general", help="查询域（影响 TTL）")

    p_set = sub.add_parser("set", help="写入缓存")
    p_set.add_argument("query")
    p_set.add_argument("engine")
    p_set.add_argument("max_results", type=int)
    p_set.add_argument("value_json")
    p_set.add_argument("--domain", default="general")

    p_clear = sub.add_parser("clear", help="清理过期缓存")
    p_clear.add_argument("--older-than", type=int, default=24,
                         help="清理 N 小时前的缓存（默认 24）")

    sub.add_parser("stats", help="显示缓存统计")

    args = parser.parse_args()
    cache = SearchCache()

    if args.cmd == "get":
        hit = cache.get(args.query, args.engine, args.max_results, domain=args.domain)
        print(json.dumps({"hit": hit is not None, "data": hit},
                         ensure_ascii=False))
    elif args.cmd == "set":
        value = json.loads(args.value_json)
        cache.set(args.query, args.engine, args.max_results, value,
                  domain=args.domain)
        print(json.dumps({"ok": True}))
    elif args.cmd == "clear":
        cache.clear(older_than_hours=args.older_than)
        print(json.dumps({"ok": True, "older_than_hours": args.older_than}))
    elif args.cmd == "stats":
        print(json.dumps(cache.stats, ensure_ascii=False, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
