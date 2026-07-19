#!/usr/bin/env node
/**
 * argo-mcp — 47-engine unified search MCP server
 *
 * Node.js entry point. Spawns the Python MCP server and transparently
 * proxies stdin/stdout, giving Electron-based MCP clients (Grok, Kimi Code,
 * Codex, etc.) a reliable Node.js process to manage.
 *
 * Usage: npx argo-mcp
 *        argo-mcp          (after npm link or global install)
 */

const { spawn } = require('child_process');
const path = require('path');

const PYTHON = '/usr/bin/python3';
const SCRIPT = path.join(__dirname, '..', 'scripts', 'mcp_server.py');

const proc = spawn(PYTHON, [SCRIPT], {
    stdio: ['pipe', 'pipe', 'inherit'],
    env: { ...process.env }
});

// Proxy MCP client stdin → Python server stdin
process.stdin.pipe(proc.stdin);

// Proxy Python server stdout → MCP client stdout
proc.stdout.pipe(process.stdout);

// Python stderr inherits directly (configured via 'inherit' above)

proc.on('error', (err) => {
    console.error(`argo-mcp: failed to start Python server: ${err.message}`);
    process.exit(1);
});

proc.on('exit', (code, signal) => {
    if (signal) {
        process.exit(128 + (signal === 'SIGTERM' ? 15 : signal === 'SIGINT' ? 2 : 1));
    }
    process.exit(code || 0);
});

// Forward termination signals
process.on('SIGTERM', () => proc.kill('SIGTERM'));
process.on('SIGINT', () => proc.kill('SIGINT'));
process.on('SIGHUP', () => proc.kill('SIGHUP'));
