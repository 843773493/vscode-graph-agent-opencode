import { createHash } from "node:crypto";
import {
  cpSync,
  createReadStream,
  createWriteStream,
  existsSync,
  readFileSync,
  rmSync,
} from "node:fs";
import path from "node:path";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";

import * as tar from "tar";

const packageRoot = path.resolve(process.cwd());
const packageJsonPath = path.join(packageRoot, "package.json");
const archivePath = path.join(
  packageRoot,
  `.boxteam-runtime-${process.pid}-${Date.now()}.tgz`,
);
const downloadTimeoutMs = 15 * 60 * 1000;

function readReleaseAsset() {
  const packageJson = JSON.parse(readFileSync(packageJsonPath, "utf8"));
  const asset = packageJson.boxteam_runtime?.release_asset;
  if (
    asset === null ||
    typeof asset !== "object" ||
    typeof asset.url !== "string" ||
    typeof asset.sha256 !== "string" ||
    asset.url.trim() === "" ||
    !/^[0-9a-f]{64}$/i.test(asset.sha256)
  ) {
    throw new Error(`BoxTeam runtime 下载元数据无效: ${packageJsonPath}`);
  }
  return Object.freeze({
    url: asset.url,
    sha256: asset.sha256.toLowerCase(),
  });
}

function copyLocalAsset(sourcePath) {
  if (!existsSync(sourcePath)) {
    throw new Error(`BoxTeam 本地 runtime asset 不存在: ${sourcePath}`);
  }
  cpSync(sourcePath, archivePath);
}

async function downloadRemoteAsset(url) {
  const controller = new AbortController();
  try {
    const response = await fetch(url, {
      signal: AbortSignal.any([
        controller.signal, AbortSignal.timeout(downloadTimeoutMs),
      ]),
    });
    if (!response.ok) {
      throw new Error(
        `下载 BoxTeam runtime asset 失败: HTTP ${response.status} ${response.statusText} url=${url}`,
      );
    }
    if (response.body === null) {
      throw new Error(`下载 BoxTeam runtime asset 没有响应体: url=${url}`);
    }
    await pipeline(Readable.fromWeb(response.body), createWriteStream(archivePath));
  } finally {
    // 头部拒绝或文件打开失败也取消未消费的正文；文件和流由pipeline释放。
    controller.abort();
  }
}

async function sha256File(filePath) {
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(filePath)) {
    hash.update(chunk);
  }
  return hash.digest("hex");
}

async function installRuntime() {
  const releaseAsset = readReleaseAsset();
  const localAssetPath = process.env.BOXTEAM_RUNTIME_ASSET_PATH?.trim();
  const overrideUrl = process.env.BOXTEAM_RUNTIME_ASSET_URL?.trim();
  if (localAssetPath && overrideUrl) {
    throw new Error(
      "BOXTEAM_RUNTIME_ASSET_PATH 与 BOXTEAM_RUNTIME_ASSET_URL 不能同时设置",
    );
  }

  if (localAssetPath) {
    await copyLocalAsset(path.resolve(localAssetPath));
  } else {
    await downloadRemoteAsset(overrideUrl || releaseAsset.url);
  }

  const actualSha256 = await sha256File(archivePath);
  if (actualSha256 !== releaseAsset.sha256) {
    throw new Error(
      `BoxTeam runtime asset 摘要不匹配: expected=${releaseAsset.sha256} actual=${actualSha256}`,
    );
  }

  await tar.x({
    cwd: packageRoot,
    file: archivePath,
    strip: 1,
    filter: (entryPath) => entryPath !== "package/package.json",
  });
  if (!existsSync(path.join(packageRoot, "runtime-manifest.json"))) {
    throw new Error(
      `BoxTeam runtime asset 解压后缺少 runtime-manifest.json: ${packageRoot}`,
    );
  }
}

const failures = [];
try {
  await installRuntime();
} catch (error) {
  failures.push(error);
}
try {
  rmSync(archivePath, { force: true });
} catch (error) {
  failures.push(error);
}
if (failures.length === 1) throw failures[0];
if (failures.length > 1) {
  throw new AggregateError(failures, "BoxTeam runtime 安装及暂存归档清理均失败", { cause: failures[0] });
}
