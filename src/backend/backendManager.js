import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';

import * as vscode from 'vscode';

import { getWorkspace } from '../shared/api.js';
import { DEFAULT_BACKEND_HOST, DEFAULT_BACKEND_PORT } from '../shared/constants.js';

function getWorkspaceRoot() {
  const folders = vscode.workspace.workspaceFolders ?? [];
  if (folders.length === 0) {
    throw new Error('当前没有打开工作区，无法启动本地后端');
  }

  return folders[0].uri.fsPath;
}

function getPort() {
  const config = vscode.workspace.getConfiguration('vscodeGraphAgent');
  return config.get('port', DEFAULT_BACKEND_PORT);
}

function findProjectRoot(workspaceRoot) {
  let current = path.resolve(workspaceRoot);

  while (true) {
    if (fs.existsSync(path.join(current, 'app', 'main.py'))) {
      return current;
    }

    const parent = path.dirname(current);
    if (parent === current) {
      break;
    }

    current = parent;
  }

  return path.resolve(workspaceRoot);
}

function getProjectPythonCandidates(projectRoot) {
  return [
    path.join(projectRoot, '.venv', 'Scripts', 'python.exe'),
    path.join(projectRoot, '.venv', 'Scripts', 'python'),
    path.join(projectRoot, '.venv', 'bin', 'python'),
    path.join(projectRoot, '.venv', 'bin', 'python3'),
  ];
}

function resolvePythonPath(projectRoot) {
  const config = vscode.workspace.getConfiguration('vscodeGraphAgent');
  const configuredPath = config.get('pythonPath', 'python');
  const candidates = getProjectPythonCandidates(projectRoot);

  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }

  if (configuredPath && configuredPath !== 'python') {
    return configuredPath;
  }

  return configuredPath;
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function createTailBuffer(limit = 20) {
  const lines = [];

  return {
    push(chunk) {
      const text = String(chunk).replace(/\r\n/g, '\n');
      for (const line of text.split('\n')) {
        if (!line) {
          continue;
        }

        lines.push(line);
        if (lines.length > limit) {
          lines.shift();
        }
      }
    },
    toString() {
      return lines.length ? lines.join('\n') : '(empty)';
    },
  };
}

function formatCommandLine(pythonPath, args, cwd, workspaceRoot, port) {
  return [
    `pythonPath=${pythonPath}`,
    `cwd=${cwd}`,
    `workspaceRoot=${workspaceRoot}`,
    `port=${port}`,
    `command=${pythonPath} ${args.join(' ')}`,
  ].join(' | ');
}

async function probeBackend(port) {
  try {
    const workspace = await getWorkspace(port);
    return { port, workspace };
  } catch {
    return null;
  }
}

function isPortFree(port) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();

    server.unref();
    server.once('error', (error) => {
      if (error && error.code === 'EADDRINUSE') {
        resolve(false);
        return;
      }

      reject(error);
    });

    server.listen(port, '127.0.0.1', () => {
      server.close(() => resolve(true));
    });
  });
}

async function findAvailablePort(preferredPort) {
  if (await isPortFree(preferredPort)) {
    return preferredPort;
  }

  for (let port = preferredPort + 1; port <= preferredPort + 50; port += 1) {
    if (await isPortFree(port)) {
      return port;
    }
  }

  throw new Error(`无法找到可用端口，起始端口为 ${preferredPort}`);
}

export class BackendManager {
  constructor(outputChannel) {
    this.outputChannel = outputChannel;
    this.process = null;
    this.readyPromise = null;
    this.port = getPort();
    this.workspaceRoot = null;
    this.stdoutTail = createTailBuffer();
    this.stderrTail = createTailBuffer();
  }

  log(message) {
    this.outputChannel.appendLine(`[graph-agent] ${message}`);
  }

  async ensureStarted() {
    if (this.readyPromise) {
      this.log(`复用现有后端实例`);
      return this.readyPromise;
    }

    this.workspaceRoot = getWorkspaceRoot();
    this.projectRoot = findProjectRoot(this.workspaceRoot);
    this.outputChannel.show(true);
    this.log(`========== 启动后端进程 ==========`);
    this.log(`首选后端端口: ${this.port}`);
    this.log(`工作区根目录: ${this.workspaceRoot}`);
    this.log(`项目根目录: ${this.projectRoot}`);

    const candidatePorts = [...new Set([this.port, 8000])];
    this.log(`将要探测的端口列表: ${candidatePorts.join(', ')}`);

    for (const candidatePort of candidatePorts) {
      this.log(`--> 探测端口 ${candidatePort}: http://127.0.0.1:${candidatePort}/api/v1/workspace`);
      const existing = await probeBackend(candidatePort);
      if (existing) {
        this.port = candidatePort;
        this.log(`✓ 检测到已存在的后端实例，使用端口 ${candidatePort}`);
        this.readyPromise = Promise.resolve(existing);
        return this.readyPromise;
      }
      this.log(`× 端口 ${candidatePort} 上未发现可用后端`);
    }

    this.port = await findAvailablePort(this.port);
    this.log(`>> 选择新端口: ${this.port}`);
    this.readyPromise = this.startAndWait();
    return this.readyPromise;
  }

  async startAndWait() {
    if (this.process) {
      this.log(`>> 后端进程已存在，等待就绪...`);
      return this.waitForReady();
    }

    const workspaceRoot = getWorkspaceRoot();
    const projectRoot = this.projectRoot ?? findProjectRoot(workspaceRoot);
    const pythonPath = resolvePythonPath(projectRoot);
    const args = ['-m', 'uvicorn', 'app.main:app', '--host', DEFAULT_BACKEND_HOST, '--port', String(this.port)];
    const cwd = projectRoot;

    this.stdoutTail = createTailBuffer();
    this.stderrTail = createTailBuffer();

    this.log(`========== 启动后端进程 ==========`);
    this.log(`Python 路径: ${pythonPath}`);
    this.log(`Python 存在: ${fs.existsSync(pythonPath) ? '✓ 是' : '✗ 否'}`);
    this.log(`项目根目录(cwd): ${cwd}`);
    this.log(`启动命令: ${pythonPath} ${args.join(' ')}`);
    this.log(`环境变量: WORKSPACE_ROOT=${workspaceRoot}`);

    if (!fs.existsSync(pythonPath)) {
      const error = new Error(`Python 路径不存在: ${pythonPath}`);
      this.log(`✗ ${error.message}`);
      throw error;
    }

    this.log(`>> 创建子进程...`);
    this.process = spawn(pythonPath, args, {
      cwd,
      env: {
        ...process.env,
        WORKSPACE_ROOT: workspaceRoot,
      },
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });

    this.process.once('spawn', () => {
      this.log(`✓ 子进程已创建，pid=${this.process?.pid ?? 'unknown'}`);
    });

    this.process.stdout?.on('data', (chunk) => {
      this.stdoutTail.push(chunk);
      const text = chunk.toString().trim();
      if (text) {
        this.log(`[backend stdout] ${text}`);
      }
    });

    this.process.stderr?.on('data', (chunk) => {
      this.stderrTail.push(chunk);
      const text = chunk.toString().trim();
      if (text) {
        this.log(`[backend stderr] ${text}`);
      }
    });

    const processFailure = new Promise((_, reject) => {
      this.process.once('error', (error) => {
        const reason = error instanceof Error ? error.stack ?? error.message : String(error);
        const diagnostic = [
          `后端进程启动错误: ${reason}`,
          `stdout_tail:\n${this.stdoutTail.toString()}`,
          `stderr_tail:\n${this.stderrTail.toString()}`,
        ].join('\n\n');
        this.log(`✗ ${diagnostic}`);
        reject(error instanceof Error ? error : new Error(String(error)));
      });

      this.process.once('exit', (code, signal) => {
        if (code !== 0) {
          const diagnostic = [
            `后端进程提前退出: code=${code}, signal=${signal ?? ''}`,
            `stdout_tail:\n${this.stdoutTail.toString()}`,
            `stderr_tail:\n${this.stderrTail.toString()}`,
          ].join('\n\n');
          this.log(`✗ ${diagnostic}`);
          reject(new Error(diagnostic));
        }
      });
    });

    this.process.on('exit', (code, signal) => {
      this.log(`后端进程退出: code=${code}, signal=${signal ?? ''}`);
      this.process = null;
      this.readyPromise = null;
    });

    this.log(`>> 等待后端就绪（最多60次，间隔500ms）...`);
    return Promise.race([this.waitForReady(), processFailure]);
  }

  async waitForReady() {
    this.log(`>> 开始后端就绪探测（最多60次，间隔500ms）`);
    for (let attempt = 1; attempt <= 60; attempt += 1) {
      try {
        this.log(`>> 探测尝试 ${attempt}/60: GET /api/v1/workspace`);
        const workspace = await getWorkspace(this.port);
        this.log(`✓ 后端就绪! 探测成功: http://127.0.0.1:${this.port}/api/v1/workspace`);
        this.log(`   workspace: ${JSON.stringify(workspace).slice(0, 150)}`);
        return { port: this.port, workspace };
      } catch (error) {
        const errorMsg = error instanceof Error ? error.message : String(error);
        this.log(`× 探测失败(${attempt}/60): ${errorMsg}`);
        if (attempt >= 5) {
          // 每5次提示一次等待
          this.log(`>> 继续等待... (已等待 ${(attempt * 0.5).toFixed(1)}秒)`);
        }
        await wait(500);
      }
    }

    const diagnostic = [
      '!!! 本地后端启动超时，未能完成 workspace 就绪探测',
      `port=${this.port}`,
      `stdout_tail:\n${this.stdoutTail.toString()}`,
      `stderr_tail:\n${this.stderrTail.toString()}`,
    ].join('\n\n');
    this.log(`✗ ${diagnostic}`);
    throw new Error(diagnostic);
  }

  dispose() {
    if (this.process) {
      this.log(`释放后端进程，pid=${this.process.pid ?? 'unknown'}`);
      this.process.kill();
      this.process = null;
    }

    this.readyPromise = null;
  }
}
