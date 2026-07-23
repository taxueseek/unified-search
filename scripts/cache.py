#!/usr/bin/env python3
"""
cache.py — Unified Search v2 双层缓存引擎

功能：
  L1: 内存 LRU 热缓存（100 条），避免同进程重复查询
  L2: SQLite 持久化缓存（TTL 可配置），跨进程复用
  分级 TTL：financial / news / realtime / general / research / evergreen
  大值 gzip 压缩（> 1KB）
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

try:
    from config import get_cache_config
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from config import get_cache_config


# ── 常量 ──────────────────────────────────────────────────────────────────────

DEFAULT_DB_PATH = "~/.cache/unified-search/cache.db"
DEFAULT_TTL = 3600
MAX_MEMORY_ITEMS = 100
MAX_DB_SIZE_MB = 100
COMPRESSION_THRESHOLD = 1024
COMPRESSION_LEVEL = 6

# 分级 TTL（秒）
CACHE_TIERS = {
    "financial": 300,    # 5 分钟
    "news": 600,         # 10 分钟
    "realtime": 900,     # 15 分钟
    "general": 3600,     # 1 小时
    "research": 7200,    # 2 小时
    "evergreen": 86400,  # 24 小时
}

# 当天缓存策略：非时效性域在当天内延长缓存至日末
# 时效性域（financial/news/realtime）保持短 TTL，确保数据新鲜度
SAME_DAY_ELIGIBLE_TIERS = {"general", "research", "evergreen"}

# query domain → TTL tier 映射
DOMAIN_TIER_MAP = {
    "stock_query": "financial",
    "fund_query": "financial",
    "financial_news": "news",
    "zhihu_content": "general",
    "tech_deep": "research",
    "news_realtime": "realtime",
    "general_search": "general",
    "social": "general",
    "local_chinese": "general",
    "local_news": "news",
    "local_academic": "research",
    "local_code": "research",
    "local_reference": "evergreen",
    "local_general": "general",
    "stock": "financial",
    "fund": "financial",
    "news": "realtime",
    "tech": "research",
    "general": "general",
    "auto": "general",
}


# ── LRU 内存缓存 ───────────────────────────────────────────────────────────────

class LRUCache:
    """基于 OrderedDict 的简单 LRU。"""

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

    def remove(self, key: str) -> None:
        """移除指定键（不存在时静默）。"""
        with self._lock:
            self._store.pop(key, None)

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
                "hit_rate": round(self._hits / max(self._hits + self._misses, 1), 3),
            }


# ── SQLite 持久化缓存 ──────────────────────────────────────────────────────────

class SQLiteCache:
    """SQLite 持久化缓存，支持 TTL 过期、大小限制、gzip 压缩。"""

    SCHEMA_VERSION = 2

    def __init__(self, db_path: str = DEFAULT_DB_PATH, ttl: int = DEFAULT_TTL):
        self._db_path = os.path.expanduser(db_path)
        self._ttl = ttl
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self):
        with self._connect() as conn:
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
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            # 迁移：添加可能缺失的列
            cols = [r[1] for r in conn.execute("PRAGMA table_info(search_cache)")]
            if "domain" not in cols:
                conn.execute("ALTER TABLE search_cache ADD COLUMN domain TEXT DEFAULT 'general'")
            if "ttl" not in cols:
                conn.execute("ALTER TABLE search_cache ADD COLUMN ttl INTEGER DEFAULT 3600")
            conn.executescript("""
                CREATE INDEX IF NOT EXISTS idx_search_cache_expires ON search_cache(created_at);
                CREATE INDEX IF NOT EXISTS idx_search_cache_domain ON search_cache(domain);
            """)
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                         ("schema_version", str(self.SCHEMA_VERSION)))

    def _is_expired(self, created_at: float, ttl: int | None = None) -> bool:
        return (time.time() - created_at) > (ttl if ttl is not None else self._ttl)

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
                conn.execute("UPDATE search_cache SET accessed_at = ? WHERE key = ?",
                             (time.time(), key))
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
                    """INSERT OR REPLACE INTO search_cache
                       (key, query, engine, max_results, domain, value_blob, compressed, ttl, created_at, accessed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (key, query, engine, max_results, domain, blob, compressed, effective_ttl, now, now),
                )
                conn.commit()
            self._evict_if_needed()

    def _evict_if_needed(self):
        """超过 100MB 时删除最旧的记录"""
        if self.size_mb <= MAX_DB_SIZE_MB:
            return
        with self._connect() as conn:
            target_mb = MAX_DB_SIZE_MB * 0.8
            while self.size_mb > target_mb:
                row = conn.execute("SELECT key FROM search_cache ORDER BY accessed_at ASC LIMIT 1").fetchone()
                if not row:
                    break
                conn.execute("DELETE FROM search_cache WHERE key = ?", (row[0],))
                conn.commit()

    def clear(self, older_than_hours: int = 24):
        with self._lock:
            cutoff = time.time() - older_than_hours * 3600
            with self._connect() as conn:
                conn.execute("DELETE FROM search_cache WHERE created_at < ?", (cutoff,))
                conn.commit()

    @property
    def stats(self) -> dict:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute("SELECT COUNT(*), SUM(LENGTH(value_blob)) FROM search_cache").fetchone()
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / max(self._hits + self._misses, 1), 3),
                "size_mb": round((row[1] or 0) / 1024 / 1024, 2),
                "entries": row[0] or 0,
            }

    @property
    def size_mb(self) -> float:
        with self._connect() as conn:
            row = conn.execute("SELECT SUM(LENGTH(value_blob)) FROM search_cache").fetchone()
        return (row[0] or 0) / 1024 / 1024


# ── 双层缓存入口 ───────────────────────────────────────────────────────────────

class SearchCache:
    """
    双层缓存引擎：L1 LRU + L2 SQLite
    缓存键 = SHA256(query + "|" + engine + "|" + str(max_results) + "|" + domain)[:32]

    缓存策略：
      - 时效性域（financial/news/realtime）：短 TTL，确保数据新鲜度
      - 非时效性域（general/research/evergreen）：当天内相同查询命中缓存，
        避免重复拉取，显著降低等待时间
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH, ttl: int = DEFAULT_TTL):
        cfg = get_cache_config()
        self._db_path = os.path.expanduser(cfg.get("db_path", db_path))
        self._ttl = cfg.get("ttl", ttl)
        self._l1 = LRUCache(max_size=MAX_MEMORY_ITEMS)
        self._l2 = SQLiteCache(db_path=self._db_path, ttl=self._ttl)

    @staticmethod
    def _key(query: str, engine: str, max_results: int, domain: str = "general",
             mode: str = "auto") -> str:
        """生成缓存键，包含 domain + mode 防止跨域和跨预算模式缓存污染。"""
        raw = f"{query}|{engine}|{max_results}|{domain}|{mode}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def resolve_ttl(domain: str = "general") -> int:
        """根据 domain 返回基础 TTL（秒）。"""
        tier = DOMAIN_TIER_MAP.get(domain, "general")
        return CACHE_TIERS.get(tier, DEFAULT_TTL)

    @staticmethod
    def _seconds_until_end_of_day() -> int:
        """计算距离当天 23:59:59 的剩余秒数。"""
        import datetime
        now = datetime.datetime.now()
        end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)
        return max(int((end_of_day - now).total_seconds()), 60)

    def _resolve_effective_ttl(self, domain: str, base_ttl: int | None = None) -> int:
        """解析有效 TTL：非时效性域在当天内延长缓存至日末。

        策略：
          - base_ttl 显式传入时，信任调用者的意图（如 ttl=0 强制过期）
          - base_ttl 为 None（使用默认 TTL）时，非时效性域延长至日末，
            确保当天内相同查询命中缓存，避免重复拉取
        """
        tier = DOMAIN_TIER_MAP.get(domain, "general")
        ttl = base_ttl if base_ttl is not None else self.resolve_ttl(domain)
        if tier in SAME_DAY_ELIGIBLE_TIERS and base_ttl is None:
            return max(ttl, self._seconds_until_end_of_day())
        return ttl

    def get(self, query: str, engine: str, max_results: int,
            domain: str = "general", mode: str = "auto") -> Optional[dict]:
        """先查 L1，未命中再查 L2。缓存键含 mode 防跨预算模式污染。"""
        key = self._key(query, engine, max_results, domain, mode)
        hit = self._l1.get(key)
        if hit is not None:
            # L1 层做简单的 TTL 检查：显式 ttl=0 或已过期则跳过
            ttl = hit.get("_ttl", 0)
            if ttl > 0 and time.time() - hit.get("_ts", 0) < ttl:
                hit["_cache_level"] = "L1"
                return hit
            self._l1.remove(key)

        hit = self._l2.get(key)
        if hit is not None:
            self._l1.set(key, hit)
            hit["_cache_level"] = "L2"
            return hit
        return None

    def set(self, query: str, engine: str, max_results: int, results: dict,
            domain: str = "general", ttl: int | None = None, mode: str = "auto"):
        """写入双层缓存，含 mode 维度防跨预算模式污染。"""
        key = self._key(query, engine, max_results, domain, mode)
        effective_ttl = self._resolve_effective_ttl(domain, ttl)
        value = {**results, "_domain": domain, "_ttl": effective_ttl, "_ts": time.time()}
        self._l1.set(key, value)
        self._l2.set(key, query, engine, max_results, value, domain=domain, ttl=effective_ttl)

    def clear(self, older_than_hours: int = 24):
        self._l2.clear(older_than_hours=older_than_hours)

    @property
    def stats(self) -> dict:
        l1 = self._l1.stats
        l2 = self._l2.stats
        total_hits = l1["hits"] + l2["hits"]
        return {
            "hits": total_hits,
            "misses": l1["misses"],
            "hit_rate": round(total_hits / max(total_hits + l1["misses"], 1), 3),
            "size_mb": l2["size_mb"],
            "entries": l2["entries"],
            "l1": l1,
            "l2": l2,
        }


# ── CLI 入口 ──────────────────────────────────────────────────────────────────

def _cli():
    import argparse
    parser = argparse.ArgumentParser(description="Unified Search v2 缓存管理")
    sub = parser.add_subparsers(dest="cmd")
    p_get = sub.add_parser("get")
    p_get.add_argument("query")
    p_get.add_argument("engine", nargs="?", default="auto")
    p_get.add_argument("max_results", nargs="?", type=int, default=5)
    p_get.add_argument("--domain", default="general")
    p_set = sub.add_parser("set")
    p_set.add_argument("query")
    p_set.add_argument("engine")
    p_set.add_argument("max_results", type=int)
    p_set.add_argument("value_json")
    p_set.add_argument("--domain", default="general")
    p_clear = sub.add_parser("clear")
    p_clear.add_argument("--older-than", type=int, default=24)
    sub.add_parser("stats")
    args = parser.parse_args()
    cache = SearchCache()
    if args.cmd == "get":
        hit = cache.get(args.query, args.engine, args.max_results, domain=args.domain)
        print(json.dumps({"hit": hit is not None, "data": hit}, ensure_ascii=False))
    elif args.cmd == "set":
        cache.set(args.query, args.engine, args.max_results, json.loads(args.value_json), domain=args.domain)
        print('{"ok": true}')
    elif args.cmd == "clear":
        cache.clear(older_than_hours=args.older_than)
        print('{"ok": true}')
    elif args.cmd == "stats":
        print(json.dumps(cache.stats, ensure_ascii=False, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
