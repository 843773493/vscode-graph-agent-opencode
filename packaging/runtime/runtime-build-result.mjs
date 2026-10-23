import { rmSync, writeFileSync } from "node:fs";

import { computeBuildInputFingerprint } from "./build-input-fingerprint.mjs";

export function beginRuntimeBuild({
  projectRoot,
  targetPlatform,
  buildResultPath,
}) {
  const sourceFingerprint = computeBuildInputFingerprint({
    projectRoot,
    targetPlatform,
  });
  rmSync(buildResultPath, { force: true });
  return sourceFingerprint;
}

export function publishRuntimeBuildResult({
  projectRoot,
  targetPlatform,
  buildResultPath,
  startingSourceFingerprint,
  metadata,
}) {
  const sourceFingerprint = computeBuildInputFingerprint({
    projectRoot,
    targetPlatform,
  });
  if (sourceFingerprint !== startingSourceFingerprint) {
    throw new Error(
      `构建期间输入源码发生变化: target=${targetPlatform} before=${startingSourceFingerprint} after=${sourceFingerprint}`,
    );
  }

  const { platform, version, ...details } = metadata;
  writeFileSync(
    buildResultPath,
    `${JSON.stringify(
      {
        platform,
        version,
        source_fingerprint: startingSourceFingerprint,
        ...details,
      },
      null,
      2,
    )}\n`,
  );
}
