import { createRuntimeLogger } from "@boxteam/plugin-sdk/logging";
import { stat } from "node:fs/promises";
import path from "node:path";

const logger = createRuntimeLogger("terminal", "terminal.terminalSession");

export function recordTerminalFailure(error, ...diagnostics) {
  try {
    logger.error(...diagnostics);
  } catch (diagnosticError) {
    return new AggregateError([error, diagnosticError], error.message, { cause: error });
  }
  return error;
}

// 终端工作目录唯一解析入口：把持久记录里的工作区内相对路径（或调用方传入的
// 相对/绝对路径）还原为本次调用栈内的绝对路径。绝对路径 MUST NOT 落盘。
export async function resolveTerminalCwd(workspaceRoot, cwdRelative) {
  const rawCwd = typeof cwdRelative === "string" && cwdRelative.trim() !== ""
    ? cwdRelative.trim()
    : ".";
  const resolved = path.resolve(workspaceRoot, rawCwd);
  let directoryStat;
  try {
    directoryStat = await stat(resolved);
  } catch (error) {
    const cause = recordTerminalFailure(error, "[terminal-session] 终端工作目录检查失败", { cwd: resolved }, error);
    const message = new Set(["ENOENT", "ENOTDIR"]).has(error.code)
      ? "终端工作目录不存在或不是目录"
      : "终端工作目录检查失败";
    throw new Error(message, { cause });
  }
  if (!directoryStat.isDirectory()) {
    throw recordTerminalFailure(new Error("终端工作目录不存在或不是目录"),
      "[terminal-session] 终端工作目录不是目录", { cwd: resolved });
  }
  return resolved;
}

export function resolveShell() {
  if (process.platform === "win32") {
    return process.env.COMSPEC || "cmd.exe";
  }
  return process.env.SHELL || "/bin/bash";
}

export function shellArgs() {
  if (process.platform === "win32") {
    return [];
  }
  return ["-i"];
}
