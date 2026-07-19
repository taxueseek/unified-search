#!/usr/bin/env python3
"""诊断版 MCP 服务器——记录所有 stdin 到日志"""
import sys, os, time, json

LOG = os.path.expanduser("~/.kimi/argo_diag.log")
PID = os.getpid()

with open(LOG, "a") as log:
    log.write(f"\n=== PID={PID} START {time.strftime('%H:%M:%S')} ===\n")
    log.write(f"args: {sys.argv}\n")
    log.write(f"python: {sys.executable}\n")
    log.write(f"cwd: {os.getcwd()}\n")
    log.write(f"PATH: {os.environ.get('PATH','')}\n")
    log.flush()

    def _send(data):
        encoded = json.dumps(data, ensure_ascii=False).encode()
        sys.stdout.buffer.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode())
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
        log.write(f"  -> SEND {len(encoded)}B\n")
        log.flush()

    while True:
        header = sys.stdin.buffer.readline()
        if not header:
            log.write("  EOF — stdin closed\n")
            log.flush()
            break
        
        header_str = header.decode("utf-8", errors="replace").strip()
        log.write(f"  <- HEADER: {repr(header_str)}\n")
        log.flush()
        
        if not header_str:
            log.write("    (empty header, skip)\n")
            log.flush()
            continue
        
        if header_str.startswith("Content-Length:"):
            length = int(header_str.split(":")[1].strip())
            sys.stdin.buffer.readline()  # blank line
            body = sys.stdin.buffer.read(length).decode("utf-8")
            log.write(f"  <- BODY({length}B): {body[:200]}\n")
            log.flush()
            try:
                req = json.loads(body)
                method = req.get("method", "?")
                reqid = req.get("id", "?")
                log.write(f"  <- METHOD: {method} id={reqid}\n")
                log.flush()
                
                if method == "initialize":
                    _send({"jsonrpc":"2.0","id":reqid,"result":{
                        "protocolVersion":"2024-11-05",
                        "capabilities":{"tools":{"listChanged":False}},
                        "serverInfo":{"name":"argo-diag","version":"0"}
                    }})
                elif method == "tools/list":
                    _send({"jsonrpc":"2.0","id":reqid,"result":{"tools":[]}})
                elif method.startswith("notifications/"):
                    log.write(f"    notification, no reply\n")
                    log.flush()
                else:
                    log.write(f"    unknown method: {method}\n")
                    log.flush()
            except Exception as e:
                log.write(f"    PARSE ERROR: {e}\n")
                log.flush()
        else:
            # 行模式
            log.write(f"  <- LINE MODE: {header_str[:100]}\n")
            log.flush()
            try:
                req = json.loads(header_str)
                log.write(f"  <- METHOD(line): {req.get('method','?')}\n")
                log.flush()
            except:
                log.write(f"    not JSON: {header_str[:50]}\n")
                log.flush()

