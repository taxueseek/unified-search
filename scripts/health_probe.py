#!/usr/bin/env python3
"""
health_probe.py — 引擎健康探针（v2.5 新增）

对 HTTP 引擎做可达性检查，结果写入 SQLite 供 route.py 查询。
- 启动时全量探测
- 每 5 分钟后台自动刷新
- 连续 2 次失败标记 unavailable，1 次成功恢复

用法：
  python3 health_probe.py          # 单次探测
  python3 health_probe.py --watch  # 后台持续监控（每 5 分钟）
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    from config import load_config, get_engines
except ImportError:
    from config import load_config, get_engines


# ── 路径 ──────────────────────────────────────────────────────────────────────

DB_PATH = Path.home() / ".cache" / "unified-search" / "health.db"
PROBE_INTERVAL = 300  # 5 分钟
PROBE_TIMEOUT = 1.5  # 单次探测超时（快，不能拖慢启动）
MAX_FAILURES = 2  # 连续失败次数阈值


# ── SQLite 存储 ───────────────────────────────────────────────────────────────

def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db():
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS engine_health (
                engine TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'unknown',
                consecutive_failures INTEGER DEFAULT 0,
                last_probe_at REAL DEFAULT 0,
                last_latency_ms REAL DEFAULT 0,
                last_error TEXT DEFAULT ''
            )
        """)


def _probe_http(url: str, timeout: float = PROBE_TIMEOUT) -> tuple[bool, float, str]:
    """探测 HTTP 引擎：发起 HEAD 请求检查可达性。"""
    try:
        req = urllib.request.Request(url, method="HEAD")
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed = (time.time() - t0) * 1000
            return True, elapsed, ""
    except Exception as e:
        return False, 0, str(e)


def _probe_cli(cmd: list[str], timeout: float = 2) -> tuple[bool, float, str]:
    """探测 CLI 引擎：检查命令是否存在。"""
    import subprocess
    if not cmd:
        return False, 0, "empty command"
    exe = cmd[0]
    if isinstance(exe, str) and exe.startswith("~"):
        exe = str(Path(exe).expanduser())
    try:
        result = subprocess.run(["which", exe], capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return True, 0, ""
        return False, 0, f"command not found: {exe}"
    except Exception as e:
        return False, 0, str(e)


def probe_all_engines():
    """探测所有已启用的引擎。"""
    _init_db()
    cfg = load_config()
    engines = get_engines(cfg)
    results = {}

    for name, spec in engines.items():
        engine_type = spec.get("type", "")
        url = spec.get("url", "")
        cmd = spec.get("cmd", [])

        if engine_type == "http" and url:
            ok, latency, err = _probe_http(url)
        elif engine_type == "cli" and cmd:
            ok, latency, err = _probe_cli(cmd)
        else:
            continue

        # 更新数据库
        with _connect() as conn:
            row = conn.execute(
                "SELECT consecutive_failures FROM engine_health WHERE engine = ?",
                (name,),
            ).fetchone()

            if ok:
                new_status = "available"
                new_failures = 0
            else:
                prev_failures = row[0] if row else 0
                new_failures = prev_failures + 1
                new_status = "unavailable" if new_failures >= MAX_FAILURES else "degraded"

            conn.execute(
                """INSERT OR REPLACE INTO engine_health
                   (engine, status, consecutive_failures, last_probe_at, last_latency_ms, last_error)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (name, new_status, new_failures, time.time(), latency, err),
            )
            conn.commit()

        results[name] = {
            "status": "available" if ok else "unavailable",
            "latency_ms": round(latency, 1),
            "error": err,
        }

    return results


def get_engine_status(engine: str) -> dict[str, Any]:
    """查询单个引擎的健康状态。"""
    _init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT status, consecutive_failures, last_probe_at, last_latency_ms FROM engine_health WHERE engine = ?",
            (engine,),
        ).fetchone()
    if not row:
        return {"status": "unknown", "consecutive_failures": 0, "available": True}
    return {
        "status": row[0],
        "consecutive_failures": row[1],
        "last_probe_at": row[2],
        "last_latency_ms": row[3],
        "available": row[0] != "unavailable",
    }


def get_all_status() -> dict[str, dict[str, Any]]:
    """获取所有引擎的健康状态。"""
    _init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT engine, status, consecutive_failures, last_probe_at, last_latency_ms FROM engine_health"
        ).fetchall()
    return {
        row[0]: {
            "status": row[1],
            "consecutive_failures": row[2],
            "last_probe_at": row[3],
            "last_latency_ms": row[4],
            "available": row[1] != "unavailable",
        }
        for row in rows
    }


def watch_background():
    """后台持续监控模式。在后台线程中运行，不阻塞 MCP 启动。"""
    def _loop():
        # 首次探测延迟 10 秒，让 MCP 先完成 initialize 握手
        time.sleep(10)
        try:
            probe_all_engines()
        except Exception:
            pass
        while True:
            time.sleep(PROBE_INTERVAL)
            try:
                probe_all_engines()
            except Exception:
                pass

    t = threading.Thread(target=_loop, daemon=True)
    t.start()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--watch" in sys.argv:
        print("启动后台健康监控（每 5 分钟）...", file=sys.stderr)
        watch_background()
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            pass
    else:
        results = probe_all_engines()
        print(json.dumps(results, ensure_ascii=False, indent=2))
