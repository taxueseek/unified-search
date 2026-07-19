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
const fs = require('fs');
const os = require('os');

const LOG = path.join(os.homedir(), '.kimi', 'argo_diag.log');
fs.mkdirSync(path.dirname(LOG), { recursive: true });
fs.appendFileSync(LOG, `=== NODE PID=${process.pid} START ${new Date().toTimeString().split(' ')[0]} ===\n`);
fs.appendFileSync(LOG, `node: ${process.execPath}\n`);
fs.appendFileSync(LOG, `cwd: ${process.cwd()}\n`);
fs.appendFileSync(LOG, `PATH: ${(process.env.PATH || '').slice(0,200)}\n`);

const PYTHON = '/usr/bin/python3';
const SCRIPT = path.join(__dirname, '..', 'scripts', 'mcp_server.py');

fs.appendFileSync(LOG, `spawning: ${PYTHON} ${SCRIPT}\n`);

const proc = spawn(PYTHON, [SCRIPT], {
    stdio: ['pipe', 'pipe', 'inherit'],
    env: { ...process.env }
});

proc.on('error', (err) => {
    fs.appendFileSync(LOG, `ERROR spawn: ${err.message}\n`);
    console.error(`argo-mcp: failed to start Python server: ${err.message}`);
    process.exit(1);
});

proc.on('exit', (code, signal) => {
    fs.appendFileSync(LOG, `Python exited: code=${code} signal=${signal}\n`);
    if (signal) {
        process.exit(128 + (signal === 'SIGTERM' ? 15 : signal === 'SIGINT' ? 2 : 1));
    }
    process.exit(code || 0);
});

fs.appendFileSync(LOG, `Python PID=${proc.pid} spawned, proxying stdio\n`);

// Forward termination signals
process.on('SIGTERM', () => proc.kill('SIGTERM'));
process.on('SIGINT', () => proc.kill('SIGINT'));
process.on('SIGHUP', () => proc.kill('SIGHUP'));
