#!/usr/bin/env python3
"""
chrome_cdp.py — 零依赖 Chrome DevTools Protocol 驱动

用 Python stdlib（socket + ssl + http.client）直接驱动 Chrome，
实现 Hound actions.py 的页面交互能力：
  - click / fill / scroll / press / wait / wait_selector
  - evaluate JavaScript
  - 截图
  - 页面导航 + 内容提取

不依赖 Playwright / Patchright / websocket-client / selenium。
Chrome 进程由调用方管理，本模块只负责 CDP 协议通信。

CDP 协议：https://chromedevtools.github.io/devtools-protocol/
帧格式：JSON over HTTP（长连接用于事件）+ WebSocket 风格双向通信。
这里用「每命令新建 TCP 连接」的简化模式（Stateless CDP），
避免实现 WebSocket 握手的复杂性。

用法：
    from chrome_cdp import ChromeCDP
    cdp = ChromeCDP(port=9222)
    cdp.navigate("https://example.com")
    cdp.wait_for_selector(".content")
    html = cdp.get_html()
    cdp.click("button.load-more")
"""

from __future__ import annotations

import json
import socket
import ssl
import subprocess
import time
import os
import signal
import hashlib
import struct
import re
from typing import Any
from urllib.parse import urlparse


# ─── HTTP/1.1 帧解析（用于从 CDP 流中分离 JSON 消息）────────────────────────

def _http_read_response(sock: socket.socket, timeout: float = 10.0) -> dict:
    """从 socket 读取一个完整的 HTTP/1.1 响应（含 chunked body）。"""
    sock.settimeout(timeout)
    data = b""
    # 读 headers 直到 \r\n\r\n
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk

    if not data:
        return {"status": 0, "headers": {}, "body": b""}

    header_end = data.index(b"\r\n\r\n")
    header_bytes = data[:header_end].decode("utf-8", errors="replace")
    body = data[header_end + 4:]

    # 解析 status + headers
    lines = header_header = header_bytes.split("\r\n")
    status_line = lines[0]
    status = int(status_line.split()[1])
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    # chunked transfer-encoding
    if headers.get("transfer-encoding") == "chunked":
        decoded = b""
        while body:
            # chunk size 行
            crlf = body.index(b"\r\n")
            size_hex = body[:crlf].decode("ascii").split(";")[0].strip()
            chunk_size = int(size_hex, 16)
            if chunk_size == 0:
                break
            decoded += body[crlf + 2:crlf + 2 + chunk_size]
            body = body[crlf + 2 + chunk_size + 2:]  # skip chunk + \r\n
        body = decoded
    elif "content-length" in headers:
        content_length = int(headers["content-length"])
        while len(body) < content_length:
            chunk = sock.recv(min(65536, content_length - len(body)))
            if not chunk:
                break
            body += chunk
        body = body[:content_length]

    return {"status": status, "headers": headers, "body": body}


def _http_request(host: str, port: int, path: str,
                  method: str = "GET", body: str = "",
                  headers: dict | None = None, timeout: float = 10.0) -> dict:
    """发送 HTTP/1.1 请求并读取完整响应。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect((host, port))

    hdrs = {"Host": host, "Connection": "close"}
    if headers:
        hdrs.update(headers)
    if body:
        hdrs["Content-Length"] = str(len(body.encode()))

    request_line = f"{method} {path} HTTP/1.1\r\n"
    header_block = "".join(f"{k}: {v}\r\n" for k, v in hdrs.items())
    raw = request_line + header_block + "\r\n"
    if body:
        raw += body

    sock.sendall(raw.encode())
    resp = _http_read_response(sock, timeout)
    sock.close()
    return resp


# ─── WebSocket 握手（CDP 必须用 WS 双向通信）────────────────────────────────
# 但 CDP 也支持 HTTP 长轮询模式获取事件。
# 这里实现一个「最小 WS 客户端」：只发文本帧、收文本帧。

def _ws_handshake(sock: socket.socket, path: str, host: str) -> None:
    """完成 WebSocket 握手。"""
    key = os.urandom(16)
    import base64
    ws_key = base64.b64encode(key).decode()

    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {ws_key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"\r\n"
    )
    sock.sendall(request.encode())

    # 读响应
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk


def _ws_send(sock: socket.socket, payload: str) -> None:
    """发送一个 WebSocket 文本帧（masked）。"""
    data = payload.encode("utf-8")
    frame = bytearray()
    frame.append(0x81)  # FIN + text opcode

    mask_key = os.urandom(4)
    length = len(data)
    if length < 126:
        frame.append(0x80 | length)  # mask bit set
    elif length < 65536:
        frame.append(0x80 | 126)
        frame.extend(struct.pack(">H", length))
    else:
        frame.append(0x80 | 127)
        frame.extend(struct.pack(">Q", length))

    frame.extend(mask_key)
    masked = bytearray(b ^ mask_key[i % 4] for i, b in enumerate(data))
    frame.extend(masked)
    sock.sendall(frame)


def _ws_recv(sock: socket.socket, timeout: float = 5.0) -> str | None:
    """接收一个 WebSocket 帧，返回文本 payload。"""
    sock.settimeout(timeout)
    try:
        header = sock.recv(2)
        if len(header) < 2:
            return None
        opcode = header[0] & 0x0F
        masked = (header[1] & 0x80) != 0
        length = header[1] & 0x7F

        if length == 126:
            length = struct.unpack(">H", sock.recv(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", sock.recv(8))[0]

        if masked:
            mask_key = sock.recv(4)

        payload = b""
        while len(payload) < length:
            chunk = sock.recv(min(65536, length - len(payload)))
            if not chunk:
                break
            payload += chunk

        if masked:
            payload = bytearray(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        if opcode == 0x1:  # text
            return payload.decode("utf-8", errors="replace")
        return None
    except socket.timeout:
        return None


# ─── CDP 命令发送器（HTTP 模式，无 WS 事件订阅）─────────────────────────────

def _cdp_send_http(host: str, port: int, session_id: str,
                   method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
    """通过 HTTP 发送 CDP 命令（JSON-RPC over HTTP）。

    Chrome 的 /json/protocol 是只读的，但每个 session 的 CDP 命令
    必须通过 WebSocket 发送。所以这里退化为：
    1. 用 HTTP GET /json 获取 ws endpoint
    2. 用最小 WS 客户端发命令
    """
    # 获取 ws path
    resp = _http_request(host, port, f"/json/protocol", timeout=5)
    # 不需要这个，直接建 WS 连接
    return {}


# ─── 最小 CDP WebSocket 客户端 ───────────────────────────────────────────────

class _CDPSession:
    """单个 target 的 CDP WebSocket 连接。"""

    def __init__(self, host: str, port: int, ws_path: str, timeout: float = 15.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._sock.connect((host, port))
        _ws_handshake(self._sock, ws_path, host)
        self._msg_id = 0
        self._pending: dict[int, str] = {}  # id -> 已收数据

    def send(self, method: str, params: dict | None = None) -> dict | None:
        """发送 CDP 命令并等待对应 id 的响应。

        Chrome 在 enable Runtime/Page 后会持续推送事件帧（executionContextCreated、
        frameStartedNavigating 等），这些帧没有 id 字段。必须持续读取直到找到
        匹配 id 的响应帧，或超时。
        """
        self._msg_id += 1
        msg_id = self._msg_id
        cmd = json.dumps({"id": msg_id, "method": method, "params": params or {}})
        _ws_send(self._sock, cmd)

        # 持续读取帧，跳过事件帧，找到匹配 id 的响应
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            text = _ws_recv(self._sock, timeout=min(1.0, remaining))
            if text is None:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue
            # 事件帧（无 id）→ 忽略继续
            if "id" not in msg:
                continue
            if msg.get("id") == msg_id:
                if "error" in msg:
                    return {"error": msg["error"]}
                return msg.get("result", {})
        return {"error": "timeout", "method": method}

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass


# ─── Chrome 进程管理 ─────────────────────────────────────────────────────────

class _ChromeProcess:
    """启动/管理 headless Chrome 进程。"""

    def __init__(self, port: int = 0, chrome_path: str | None = None):
        self.port = port or self._find_free_port()
        self.chrome_path = chrome_path or self._find_chrome()
        self._proc: subprocess.Popen | None = None
        self._user_data_dir = f"/tmp/argo_chrome_{self.port}"

    @staticmethod
    def _find_free_port() -> int:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    @staticmethod
    def _find_chrome() -> str:
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
        raise FileNotFoundError("Chrome not found. Install Chrome or set chrome_path.")

    def start(self) -> None:
        """启动 headless Chrome with remote debugging。"""
        import shutil
        # 清理旧 profile（避免 Hangouts 这类背景页）
        if os.path.isdir(self._user_data_dir):
            shutil.rmtree(self._user_data_dir, ignore_errors=True)

        cmd = [
            self.chrome_path,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--disable-background-networking",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self._user_data_dir}",
            "--window-size=1280,800",
            "about:blank",
        ]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 等待 CDP ready
        for _ in range(30):
            time.sleep(0.3)
            try:
                resp = _http_request("localhost", self.port, "/json/version", timeout=2)
                if resp["status"] == 200 and resp["body"]:
                    return
            except Exception:
                pass
        raise RuntimeError(f"Chrome CDP failed to start on port {self.port}")

    def stop(self) -> None:
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()


# ─── 主 API ──────────────────────────────────────────────────────────────────

class ChromeCDP:
    """高级 CDP 封装：页面交互 + 内容提取。

    实现 Hound actions.py 的能力：
    - navigate(url)
    - click(selector)
    - fill(selector, text)
    - press(key)
    - scroll(times)
    - wait(ms)
    - wait_selector(selector)
    - evaluate(js)
    - get_html()
    - get_text()
    - screenshot(path)
    """

    def __init__(self, port: int = 0, chrome_path: str | None = None,
                 auto_start: bool = True):
        self._chrome = _ChromeProcess(port=port, chrome_path=chrome_path)
        self._session: _CDPSession | None = None
        self._target_id: str | None = None
        if auto_start:
            self.start()

    def start(self) -> None:
        """启动 Chrome 并建立 CDP 会话。"""
        self._chrome.start()
        # 获取 page target
        resp = _http_request("localhost", self._chrome.port, "/json", timeout=5)
        targets = json.loads(resp["body"])
        page_targets = [t for t in targets if t.get("type") == "page"]
        if not page_targets:
            raise RuntimeError("No page target found")
        self._target_id = page_targets[0]["id"]
        ws_url = page_targets[0]["webSocketDebuggerUrl"]
        # 格式：ws://hostname:PORT/path → 提取 /path
        # 找到 "ws://" 后的第三个 "/" 即为 path 开始
        scheme_end = ws_url.index("://") + 3
        host_end = ws_url.index("/", scheme_end)
        ws_path = ws_url[host_end:]
        self._session = _CDPSession("localhost", self._chrome.port, ws_path)
        # 启用 Runtime + Page domain
        self._session.send("Page.enable")
        self._session.send("Runtime.enable")

    def stop(self) -> None:
        if self._session:
            self._session.close()
            self._session = None
        self._chrome.stop()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stop()

    # ── 导航 ──

    def navigate(self, url: str, wait_until: str = "networkidle") -> None:
        """导航到 URL，可选等待加载状态。"""
        self._session.send("Page.navigate", {"url": url})
        if wait_until == "networkidle":
            self._wait_network_idle(timeout=10)
        elif wait_until == "load":
            self.wait_for_event("Page.loadEventFired", timeout=10)

    def _wait_network_idle(self, timeout: float = 10.0) -> None:
        """简单等待：sleep + 检查 document.readyState。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.evaluate("document.readyState")
            if r == "complete":
                # 再等一小段时间让异步请求完成
                time.sleep(0.5)
                return
            time.sleep(0.3)

    def wait_for_event(self, event_method: str, timeout: float = 10.0) -> bool:
        """等待特定 CDP 事件。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            text = _ws_recv(self._session._sock, timeout=min(1.0, deadline - time.time()))
            if text:
                try:
                    msg = json.loads(text)
                    if msg.get("method") == event_method:
                        return True
                except json.JSONDecodeError:
                    pass
        return False

    # ── 页面交互 ──

    def click(self, selector: str) -> bool:
        """点击匹配 CSS 选择器的元素。"""
        js = f"""
        (() => {{
            const el = document.querySelector('{selector}');
            if (!el) return false;
            el.click();
            return true;
        }})()
        """
        r = self.evaluate(js)
        return r is True

    def fill(self, selector: str, text: str) -> bool:
        """填写 input/textarea 的值并触发 input 事件。"""
        escaped = text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        js = f"""
        (() => {{
            const el = document.querySelector('{selector}');
            if (!el) return false;
            el.value = '{escaped}';
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            return true;
        }})()
        """
        r = self.evaluate(js)
        return r is True

    def press(self, key: str) -> None:
        """模拟按键（Enter、Tab、Escape 等）。"""
        key_map = {"Enter": "Enter", "Tab": "Tab", "Escape": "Escape",
                   "Backspace": "Backspace", "ArrowDown": "ArrowDown"}
        kp = key_map.get(key, key)
        self._session.send("Input.dispatchKeyEvent", {
            "type": "keyDown",
            "key": kp,
            "code": kp,
            "windowsVirtualKeyCode": 13 if key == "Enter" else 0,
        })
        self._session.send("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": kp, "code": kp,
        })

    def scroll(self, times: int = 3) -> None:
        """向下滚动页面。"""
        for _ in range(times):
            self.evaluate("window.scrollBy(0, window.innerHeight)")
            time.sleep(0.3)

    def wait(self, ms: int) -> None:
        """等待指定毫秒。"""
        time.sleep(ms / 1000.0)

    def wait_selector(self, selector: str, timeout: float = 10.0) -> bool:
        """等待选择器匹配的元素出现。"""
        deadline = time.time() + timeout
        js = f"document.querySelector('{selector}') !== null"
        while time.time() < deadline:
            r = self.evaluate(js)
            if r is True:
                return True
            time.sleep(0.3)
        return False

    def evaluate(self, expression: str) -> Any:
        """在页面上下文执行 JS 表达式并返回结果。"""
        r = self._session.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
        })
        if r and "result" in r:
            val = r["result"].get("value")
            return val
        return None

    # ── 内容提取 ──

    def get_html(self) -> str:
        """获取当前页面完整 HTML。"""
        r = self.evaluate("document.documentElement.outerHTML")
        return r or ""

    def get_text(self) -> str:
        """获取页面可见文本。"""
        r = " " * 0
        r = self.evaluate("document.body ? document.body.innerText : ''")
        return r or ""

    def get_title(self) -> str:
        """获取页面标题。"""
        r = self.evaluate("document.title")
        return r or ""

    def get_url(self) -> str:
        """获取当前 URL。"""
        r = self.evaluate("window.location.href")
        return r or ""

    # ── 截图 ──

    def screenshot(self, path: str | None = None, full_page: bool = False) -> str:
        """截图并保存为 PNG，返回文件路径。"""
        params: dict[str, Any] = {"format": "png"}
        if full_page:
            # 获取页面完整高度
            height = self.evaluate("document.documentElement.scrollHeight") or 800
            self._session.send("Emulation.setDeviceMetricsOverride", {
                "width": 1280, "height": int(height), "deviceScaleFactor": 1, "mobile": False
            })
            params["clip"] = {"x": 0, "y": 0, "width": 1280, "height": int(height), "scale": 1}

        r = self._session.send("Page.captureScreenshot", params)
        if r and "data" in r:
            import base64
            data = base64.b64decode(r["data"])
            path = path or f"/tmp/argo_screenshot_{int(time.time())}.png"
            with open(path, "wb") as f:
                f.write(data)
            if full_page:
                self._session.send("Emulation.clearDeviceMetricsOverride")
            return path
        return ""

    # ── 高级：执行 actions 序列 ──

    def execute_actions(self, actions: list[dict]) -> dict:
        """执行 Hound 风格的 actions 数组。

        支持的 action 类型：
        - {"click": "selector"}
        - {"fill": {"selector": "...", "text": "..."}}
        - {"press": "Enter"}
        - {"scroll": 3}
        - {"wait": 1000}  # 毫秒
        - {"wait_selector": ".content"}
        - {"evaluate": "document.title"}

        返回 {"success": bool, "results": [...], "error": str|None}
        """
        results = []
        for action in actions:
            try:
                if "click" in action:
                    ok = self.click(action["click"])
                    results.append({"click": action["click"], "ok": ok})
                elif "fill" in action:
                    params = action["fill"]
                    ok = self.fill(params["selector"], params["text"])
                    results.append({"fill": params["selector"], "ok": ok})
                elif "press" in action:
                    self.press(action["press"])
                    results.append({"press": action["press"], "ok": True})
                elif "scroll" in action:
                    self.scroll(action["scroll"])
                    results.append({"scroll": action["scroll"], "ok": True})
                elif "wait" in action:
                    self.wait(action["wait"])
                    results.append({"wait": action["wait"], "ok": True})
                elif "wait_selector" in action:
                    ok = self.wait_selector(action["wait_selector"])
                    results.append({"wait_selector": action["wait_selector"], "ok": ok})
                elif "evaluate" in action:
                    val = self.evaluate(action["evaluate"])
                    results.append({"evaluate": action["evaluate"], "result": val})
                else:
                    results.append({"action": action, "ok": False, "error": "unknown action"})
            except Exception as e:
                results.append({"action": action, "ok": False, "error": str(e)[:100]})
                return {"success": False, "results": results, "error": str(e)[:100]}

        all_ok = all(r.get("ok", True) for r in results)
        return {"success": all_ok, "results": results}


# ─── CLI 测试 ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"

    with ChromeCDP() as cdp:
        print(f"Navigating to {url}...")
        cdp.navigate(url)
        print(f"Title: {cdp.get_title()}")
        print(f"URL: {cdp.get_url()}")
        text = cdp.get_text()
        print(f"Text length: {len(text)}")
        print(f"First 200 chars: {text[:200]}")
