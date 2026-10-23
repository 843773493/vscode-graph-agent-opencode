import { createWriteStream, existsSync, mkdirSync, rmSync, statSync } from "node:fs";
import { open } from "node:fs/promises";
import path from "node:path";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";

import {
  PYTHON_RUNTIME,
  PYTHON_RUNTIME_WINDOWS_X64,
  NODE_RUNTIME_WINDOWS_X64,
} from "./versions.mjs";
import { sha256File } from "./runtime-release-assets.mjs";

export async function downloadPinnedLinuxPython(downloadRoot) {
  mkdirSync(downloadRoot, { recursive: true });
  const archivePath = path.join(downloadRoot, PYTHON_RUNTIME.archive);
  if (!existsSync(archivePath)) {
    const controller = new AbortController();
    try {
      const response = await fetch(PYTHON_RUNTIME.url, { signal: controller.signal });
      if (!response.ok) {
        throw new Error(
          `下载 Python 运行时失败: HTTP ${response.status} ${response.statusText}`,
        );
      }
      if (response.body === null) throw new Error("下载 Python 运行时响应没有 body");
      await pipeline(Readable.fromWeb(response.body), createWriteStream(archivePath));
    } catch (downloadError) {
      try {
        rmSync(archivePath, { force: true });
      } catch (cleanupError) {
        throw new AggregateError([downloadError, cleanupError], "Python运行时下载及部分归档清理均失败", { cause: downloadError });
      }
      throw downloadError;
    } finally {
      controller.abort();
    }
  }
  const digest = await sha256File(archivePath);
  if (digest !== PYTHON_RUNTIME.sha256) {
    throw new Error(
      `Python 运行时摘要不匹配: expected=${PYTHON_RUNTIME.sha256} actual=${digest}`,
    );
  }
  return archivePath;
}

export async function downloadPinnedWindowsPython(pythonDownloadRoot) {
  mkdirSync(pythonDownloadRoot, { recursive: true });
  const archivePath = path.join(
    pythonDownloadRoot,
    PYTHON_RUNTIME_WINDOWS_X64.archive,
  );
  let offset = 0;
  if (existsSync(archivePath)) {
    const existingDigest = await sha256File(archivePath);
    if (existingDigest === PYTHON_RUNTIME_WINDOWS_X64.sha256) {
      return archivePath;
    }
    offset = statSync(archivePath).size;
  }
  let completed = false;
  for (let attempt = 1; attempt <= 5 && !completed; attempt += 1) {
    const controller = new AbortController();
    let retry = false;
    try {
      let response;
      try {
        response = await fetch(PYTHON_RUNTIME_WINDOWS_X64.url, {
          headers: offset > 0 ? { Range: `bytes=${offset}-` } : {},
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(120_000)]),
        });
      } catch (error) {
        if (attempt === 5) throw error;
        retry = true;
        continue;
      }
      if (response.status === 416 && offset > 0) {
        completed = true;
        continue;
      }
      if (!response.ok) {
        throw new Error(
          `下载 Windows Python 运行时失败: HTTP ${response.status} ${response.statusText}`,
        );
      }
      const append = offset > 0 && response.status === 206;
      const file = await open(archivePath, append ? "a" : "w");
      try {
        if (response.body === null) {
          throw new Error("下载 Windows Python 运行时响应没有 body");
        }
        await pipeline(Readable.fromWeb(response.body), file.createWriteStream());
        completed = true;
      } catch (error) {
        if (attempt === 5) throw error;
        retry = true;
      } finally {
        await file.close();
        // 续传依据实际落盘长度，不能把响应chunk长度当成已写入字节数。
        offset = statSync(archivePath).size;
      }
    } finally {
      controller.abort();
      if (retry) await new Promise((resolve) => setTimeout(resolve, 1_000));
    }
  }
  if (!completed) {
    throw new Error("下载 Windows Python 运行时在重试后仍未完成");
  }
  const digest = await sha256File(archivePath);
  if (digest !== PYTHON_RUNTIME_WINDOWS_X64.sha256) {
    throw new Error(
      `Windows Python 运行时摘要不匹配: expected=${PYTHON_RUNTIME_WINDOWS_X64.sha256} actual=${digest}`,
    );
  }
  return archivePath;
}

export async function downloadPinnedWindowsNode(nodeDownloadRoot) {
  mkdirSync(nodeDownloadRoot, { recursive: true });
  const archivePath = path.join(nodeDownloadRoot, NODE_RUNTIME_WINDOWS_X64.archive);
  if (existsSync(archivePath)) {
    const existingDigest = await sha256File(archivePath);
    if (existingDigest === NODE_RUNTIME_WINDOWS_X64.sha256) {
      return archivePath;
    }
  }
  const controller = new AbortController();
  try {
    const response = await fetch(NODE_RUNTIME_WINDOWS_X64.url, {
      signal: AbortSignal.any([controller.signal, AbortSignal.timeout(120_000)]),
    });
    if (!response.ok) {
      throw new Error(
        `下载 Windows Node 运行时失败: HTTP ${response.status} ${response.statusText}`,
      );
    }
    if (response.body === null) throw new Error("下载 Windows Node 运行时响应没有 body");
    await pipeline(Readable.fromWeb(response.body), createWriteStream(archivePath));
  } finally {
    controller.abort();
  }
  const digest = await sha256File(archivePath);
  if (digest !== NODE_RUNTIME_WINDOWS_X64.sha256) {
    throw new Error(
      `Windows Node 运行时摘要不匹配: expected=${NODE_RUNTIME_WINDOWS_X64.sha256} actual=${digest}`,
    );
  }
  return archivePath;
}
