import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import * as vscode from 'vscode';

import { getWorkspace } from '../shared/api.js';
import { DEFAULT_BACKEND_HOST, DEFAULT_BACKEND_PORT } from '../shared/constants.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const EXTENSION_ROOT = path.resolve(__dirname, '..', '..');

function getWorkspaceRoot() {
  const folders = vscode.workspace.workspaceFolders ?? [];
  if (folders.length > 0) {
    return folders[0].uri.fsPath;
  }

  return null;
}

function ensureDefaultWorkspaceLayout(workspaceRoot) {
  if (!workspaceRoot) {
    return;
  }

  if (!fs.existsSync(workspaceRoot)) {
    fs.mkdirSync(workspaceRoot, { recursive: true });
  }

  const boxteamDir = path.join(workspaceRoot, '.boxteam');
  if (!fs.existsSync(boxteamDir)) {
    fs.mkdirSync(boxteamDir, { recursive: true });
  }
}

function getPort() {
  const config = vscode.workspace.getConfiguration('vscodeGraphAgent');
  return config.get('port', DEFAULT_BACKEND_PORT);
}

function findProjectRoot() {
  if (fs.existsSync(path.join(EXTENSION_ROOT, 'app', 'main.py'))) {
    return EXTENSION_ROOT;
  }

  throw new Error(`未找到软件根目录下的 app/main.py: ${path.join(EXTENSION_ROOT, 'app', 'main.py')}`);
}

function getProjectPythonCandidates(projectRoot) {
  return [
    path.join(projectRoot, '.venv', 'Scripts', 'python.exe'),
    path.join(projectRoot, '.venv', 'Scripts', 'python'),
    path.join(projectRoot, '.venv', 'bin', 'python'),
    path.join(projectRoot, '.venv', 'bin', 'python3'),
  ];
}

function getPythonVersionFile(projectRoot) {
  return path.join(projectRoot, '.python-version');
}

function resolvePythonPath(projectRoot) {
  const config = vscode.workspace.getConfiguration('vscodeGraphAgent');
  const configuredPath = config.get('pythonPath', 'python');
  const candidates = getProjectPythonCandidates(projectRoot);

  console.log(`[graph-agent] [backend] 解析 Python 路径，projectRoot=${projectRoot}`);
  console.log(`[graph-agent] [backend] 配置项 pythonPath=${configuredPath}`);
  console.log(`[graph-agent] [backend] .python-version=${getPythonVersionFile(projectRoot)} 存在=${fs.existsSync(getPythonVersionFile(projectRoot)) ? '✓ 是' : '✗ 否'}`);
  console.log(`[graph-agent] [backend] 候选 Python 路径：${candidates.map((candidate) => `${candidate}(${fs.existsSync(candidate) ? '存在' : '不存在'})`).join(' | ')}`);

  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) {
      console.log(`[graph-agent] [backend] 命中 Python 候选路径: ${candidate}`);
      return candidate;
    }
  }

  if (configuredPath && configuredPath !== 'python') {
    console.log(`[graph-agent] [backend] 未命中虚拟环境，回退到配置项 pythonPath: ${configuredPath}`);
    return configuredPath;
  }

  console.log(`[graph-agent] [backend] 未命中任何 Python 候选路径，回退到默认值: ${configuredPath}`);
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

function ensureDirSync(dirPath) {
  if (!fs.existsSync(dirPath)) {
    fs.mkdirSync(dirPath, { recursive: true });
  }
}

function getHostLogPath() {
  return path.join(process.env.USERPROFILE ?? os.homedir(), '.boxteams', 'logs', 'vscode_host.log');
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

function formatLogHeader(kind, extras = []) {
  const lines = [
    `========== ${kind} ==========`,
    `timestamp=${new Date().toISOString()}`,
    ...extras,
  ];

  return `${lines.join('\n')}\n`;
}

function getRuntimeAppLogPath() {
  return path.join(process.env.USERPROFILE ?? os.homedir(), '.boxteams', 'logs', 'vscode_runtime_app.log');
}

function writeFileOverwritten(filePath, content) {
  if (!filePath) {
    return;
  }

  ensureDirSync(path.dirname(filePath));
  fs.writeFileSync(filePath, content, { encoding: 'utf8' });
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

function terminateProcessByPid(pid, signal = 'SIGTERM') {
  try {
    process.kill(pid, signal);
    return true;
  } catch (error) {
    if (error && typeof error === 'object' && 'code' in error && error.code === 'ESRCH') {
      return false;
    }

    throw error;
  }
}

async function killProcessOnPort(port, logger) {
  if (process.platform === 'win32') {
    const command = `Get-NetTCPConnection -LocalPort ${port} -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique`;
    const child = spawn('powershell.exe', ['-NoProfile', '-Command', command], { stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
    const output = [];
    const errors = [];

    child.stdout.on('data', (chunk) => output.push(String(chunk)));
    child.stderr.on('data', (chunk) => errors.push(String(chunk)));

    const exitCode = await new Promise((resolve) => {
      child.on('close', (code) => resolve(code ?? 0));
    });

    if (exitCode !== 0) {
      logger?.(`启动前清理端口 ${port} 时 PowerShell 退出码异常: ${exitCode}，stderr=${errors.join('').trim() || '(empty)'}`);
      return false;
    }

    const pidList = output.join('').split(/\s+/).map((value) => Number.parseInt(value, 10)).filter((value) => Number.isInteger(value) && value > 0);
    if (pidList.length === 0) {
      return false;
    }

    let killedAny = false;
    for (const pid of pidList) {
      logger?.(`启动前尝试释放端口 ${port}，目标 PID=${pid}`);
      if (!terminateProcessByPid(pid, 'SIGTERM')) {
        continue;
      }

      killedAny = true;
      for (let attempt = 1; attempt <= 10; attempt += 1) {
        if (await isPortFree(port)) {
          logger?.(`端口 ${port} 已释放`);
          return true;
        }

        if (attempt === 5) {
          terminateProcessByPid(pid, 'SIGKILL');
        }

        await wait(200);
      }
    }

    return killedAny;
  }

  return false;
}

export class BackendManager {
  constructor(outputChannel) {
    this.outputChannel = outputChannel;
    this.process = null;
    this.readyPromise = null;
    this.readyState = 'idle';
    this.port = getPort();
    this.workspaceRoot = null;
    this.stdoutTail = createTailBuffer();
    this.stderrTail = createTailBuffer();
    this.hostLogPath = null;
    this.runtimeAppLogPath = null;
  }

  log(message) {
    this.outputChannel.appendLine(`[graph-agent] ${message}`);
    this.appendHostLog(`[graph-agent] ${message}`);
  }

  appendHostLog(line) {
    if (!this.hostLogPath) {
      return;
    }

    try {
      ensureDirSync(path.dirname(this.hostLogPath));
      fs.appendFileSync(this.hostLogPath, `${line}\n`, { encoding: 'utf8' });
    } catch (error) {
      this.outputChannel.appendLine(`[graph-agent] [vscode_host.log 写入失败] ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  resetHostLog() {
    if (!this.hostLogPath) {
      return;
    }

    try {
      ensureDirSync(path.dirname(this.hostLogPath));
      fs.writeFileSync(this.hostLogPath, '', { encoding: 'utf8' });
    } catch (error) {
      this.outputChannel.appendLine(`[graph-agent] [vscode_host.log 清空失败] ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  writeHostLogHeader() {
    if (!this.hostLogPath) {
      return;
    }

    try {
      ensureDirSync(path.dirname(this.hostLogPath));
      fs.appendFileSync(
        this.hostLogPath,
        formatLogHeader('extension host console 日志', [
          `workspaceRoot=${this.workspaceRoot ?? '(unknown)'}`,
          `projectRoot=${this.projectRoot ?? '(unknown)'}`,
          `port=${this.port}`,
        ]),
        { encoding: 'utf8' },
      );
    } catch (error) {
      this.outputChannel.appendLine(`[graph-agent] [vscode_host.log 写入失败] ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  resetRuntimeAppLog() {
    const runtimeAppLogPath = getRuntimeAppLogPath();
    if (!runtimeAppLogPath) {
      return;
    }

    try {
      ensureDirSync(path.dirname(runtimeAppLogPath));
      fs.writeFileSync(runtimeAppLogPath, '', { encoding: 'utf8' });
    } catch (error) {
      this.outputChannel.appendLine(`[graph-agent] [vscode_runtime_app.log 清空失败] ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  writeRuntimeAppLogCommandBlock(commandLine, cwd, workspaceRoot) {
    if (!this.runtimeAppLogPath) {
      return;
    }

    try {
      ensureDirSync(path.dirname(this.runtimeAppLogPath));
      fs.appendFileSync(
        this.runtimeAppLogPath,
        formatLogHeader('backend runtime app 启动命令信息', [
          `COMMAND=${commandLine}`,
          `CWD=${cwd}`,
          `WORKSPACE_ROOT=${workspaceRoot ?? ''}`,
        ]),
        { encoding: 'utf8' },
      );
    } catch (error) {
      this.outputChannel.appendLine(`[graph-agent] [vscode_runtime_app.log 写入失败] ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  createRuntimeAppShellCommand(pythonPath, args, runtimeAppLogPath) {
    const quotedCommand = [this.quoteForShell(pythonPath), ...args.map((arg) => this.quoteForShell(arg))].join(' ');
    const quotedLogPath = this.quoteForShell(runtimeAppLogPath);
    return `${quotedCommand} 1>> ${quotedLogPath} 2>>&1`;
  }

  quoteForShell(value) {
    return `"${String(value).replace(/"/g, '\\"')}"`;
  }

  async ensureStarted() {
    if (this.readyState === 'ready' && this.readyPromise) {
      this.log(`复用现有后端实例`);
      return this.readyPromise;
    }

    if (this.readyState === 'starting' && this.readyPromise) {
      this.log(`复用正在启动中的后端实例`);
      return this.readyPromise;
    }

    this.readyState = 'starting';
    this.hostLogPath = getHostLogPath();
    this.runtimeAppLogPath = getRuntimeAppLogPath();
    this.projectRoot = findProjectRoot();
    this.workspaceRoot = getWorkspaceRoot();
    ensureDefaultWorkspaceLayout(this.workspaceRoot);
    this.outputChannel.show(true);
    this.resetHostLog();
    this.log(`========== 启动后端进程 ==========`);
    this.log(`调试信息: 当前工作区 folders=${(vscode.workspace.workspaceFolders ?? []).map((folder) => folder.uri.fsPath).join(', ') || '(empty)'}`);
    this.log(`首选后端端口: ${this.port}`);
    this.log(`工作区根目录: ${this.workspaceRoot}`);
    this.log(`项目根目录: ${this.projectRoot}`);
    this.log(`软件根目录: ${EXTENSION_ROOT}`);
    this.log(`vscode_host.log 路径: ${this.hostLogPath ?? '(未设置)'}`);
    this.log(`vscode_runtime_app.log 路径: ${this.runtimeAppLogPath ?? '(未设置)'}`);
    this.log(`调试信息: workspaceRoot=${this.workspaceRoot}, projectRoot=${this.projectRoot}`);
    this.writeHostLogHeader();

    this.log(`启动前尝试清理首选端口占用: ${this.port}`);
    const portFreed = await killProcessOnPort(this.port, (message) => this.log(message));
    if (portFreed) {
      this.log(`启动前端口 ${this.port} 已成功清理`);
    } else {
      this.log(`启动前端口 ${this.port} 未发现可清理的监听进程，或清理未成功`);
    }

    const candidatePorts = [...new Set([this.port, 8000])];
    this.log(`将要探测的端口列表: ${candidatePorts.join(', ')}`);

    for (const candidatePort of candidatePorts) {
      this.log(`--> 探测端口 ${candidatePort}: http://127.0.0.1:${candidatePort}/api/v1/workspace`);
      const existing = await probeBackend(candidatePort);
      if (existing) {
        this.port = candidatePort;
        this.log(`✓ 检测到已存在的后端实例，使用端口 ${candidatePort}`);
        this.readyState = 'ready';
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

    const projectRoot = this.projectRoot ?? findProjectRoot();
    const pythonPath = resolvePythonPath(projectRoot);
    const args = ['-m', 'uvicorn', 'app.main:app', '--host', DEFAULT_BACKEND_HOST, '--port', String(this.port)];
    const cwd = projectRoot;

    this.resetRuntimeAppLog();

    this.log(`========== 启动后端进程 ==========`);
    this.log(`Python 路径: ${pythonPath}`);
    this.log(`Python 存在: ${fs.existsSync(pythonPath) ? '✓ 是' : '✗ 否'}`);
    this.log(`项目根目录(cwd): ${cwd}`);
    this.log(`调试信息: projectRoot 下 app/main.py=${fs.existsSync(path.join(projectRoot, 'app', 'main.py')) ? '存在' : '不存在'}`);
    this.log(`调试信息: projectRoot 下 .venv\\Scripts\\python.exe=${fs.existsSync(path.join(projectRoot, '.venv', 'Scripts', 'python.exe')) ? '存在' : '不存在'}`);
    this.log(`调试信息: projectRoot 下 .venv\\bin\\python=${fs.existsSync(path.join(projectRoot, '.venv', 'bin', 'python')) ? '存在' : '不存在'}`);
    this.log(`启动命令: ${pythonPath} ${args.join(' ')}`);
    this.log(`环境变量: WORKSPACE_ROOT=${this.workspaceRoot ?? '(未设置，交由后端默认工作区处理)'}`);
    this.writeRuntimeAppLogCommandBlock(`${pythonPath} ${args.join(' ')}`, cwd, this.workspaceRoot);

    if (!fs.existsSync(pythonPath)) {
      const error = new Error(`Python 路径不存在: ${pythonPath}`);
      this.log(`✗ ${error.message}`);
      throw error;
    }

    const runtimeAppLogPath = this.runtimeAppLogPath;
    const command = this.createRuntimeAppShellCommand(pythonPath, args, runtimeAppLogPath);

    this.log(`>> 创建子进程...`);
    this.process = spawn(command, {
      cwd,
      env: {
        ...process.env,
        ...(this.workspaceRoot ? { WORKSPACE_ROOT: this.workspaceRoot } : {}),
      },
      shell: true,
      stdio: 'ignore',
      windowsHide: true,
    });

    this.process.once('spawn', () => {
      this.log(`✓ 子进程已创建，pid=${this.process?.pid ?? 'unknown'}`);
    });

    const processFailure = new Promise((_, reject) => {
      this.process.once('error', (error) => {
        const reason = error instanceof Error ? error.stack ?? error.message : String(error);
        const diagnostic = `后端进程启动错误: ${reason}`;
        this.log(`✗ ${diagnostic}`);
        reject(error instanceof Error ? error : new Error(String(error)));
      });

      this.process.once('exit', (code, signal) => {
        if (code !== 0) {
          const diagnostic = `后端进程提前退出: code=${code}, signal=${signal ?? ''}`;
          this.log(`✗ ${diagnostic}`);
          reject(new Error(diagnostic));
        }
      });
    });

    this.process.on('exit', (code, signal) => {
      this.log(`后端进程退出: code=${code}, signal=${signal ?? ''}`);
      this.process = null;
      this.readyPromise = null;
      this.readyState = 'idle';
      this.log(`后端运行时日志尾部: ${this.readRuntimeAppLogTail()}`);
    });

    this.log(`>> 等待后端就绪（最多60次，间隔1000ms）...`);
    return Promise.race([this.waitForReady(), processFailure]);
  }

  readRuntimeAppLogTail() {
    if (!this.runtimeAppLogPath || !fs.existsSync(this.runtimeAppLogPath)) {
      return '(runtime app log unavailable)';
    }

    try {
      const content = fs.readFileSync(this.runtimeAppLogPath, 'utf8');
      const lines = content.replace(/\r\n/g, '\n').split('\n').filter(Boolean);
      return lines.slice(-20).join('\\n') || '(empty)';
    } catch (error) {
      return `(failed to read runtime app log tail: ${error instanceof Error ? error.message : String(error)})`;
    }
  }

  async waitForReady() {
    this.log(`>> 开始后端就绪探测（最多60次，间隔1000ms）`);
    for (let attempt = 1; attempt <= 60; attempt += 1) {
      try {
        this.log(`>> 探测尝试 ${attempt}/60: GET /api/v1/workspace`);
        const workspace = await getWorkspace(this.port);
        this.log(`✓ 后端就绪! 探测成功: http://127.0.0.1:${this.port}/api/v1/workspace`);
        this.log(`   workspace: ${JSON.stringify(workspace).slice(0, 150)}`);
        this.readyState = 'ready';
        return { port: this.port, workspace };
      } catch (error) {
        const errorMsg = error instanceof Error ? error.message : String(error);
        this.log(`× 探测失败(${attempt}/60): ${errorMsg}`);
        if (attempt >= 5) {
          // 每5次提示一次等待
          this.log(`>> 继续等待... (已等待 ${(attempt * 1).toFixed(1)}秒)`);
        }
        await wait(1000);
      }
    }

    const diagnostic = [
      '!!! 本地后端启动超时，未能完成 workspace 就绪探测',
      `port=${this.port}`,
    ].join('\n\n');
    this.log(`✗ ${diagnostic}`);
    this.readyState = 'idle';
    throw new Error(diagnostic);
  }

  dispose() {
    if (this.process) {
      this.log(`释放后端进程，pid=${this.process.pid ?? 'unknown'}`);
      this.process.kill();
      this.process = null;
    }

    this.readyPromise = null;
    this.readyState = 'idle';
  }
}
