export function describeCommandCheckResult(result) {
  const stderr = result.stderr?.trim();
  return {
    succeeded: !result.error && result.status === 0,
    details: [
      result.error
        ? `启动错误=${result.error.code ?? "unknown"}: ${result.error.message}`
        : null,
      `退出码=${result.status ?? "null"}`,
      `信号=${result.signal ?? "null"}`,
      stderr ? `stderr=${stderr}` : null,
    ].filter(Boolean).join("；"),
  };
}

export function resolveDockerCommandPrefix({
  platform = process.platform,
  checkCommand,
  sudoEnvironmentPairs,
}) {
  const dockerCheck = checkCommand("docker", ["version"]);
  if (dockerCheck.succeeded) {
    return ["docker"];
  }

  const failures = [`docker: ${dockerCheck.details}`];
  if (platform === "linux") {
    const sudoArguments = ["-n", "env", ...sudoEnvironmentPairs, "docker", "version"];
    const sudoCheck = checkCommand("sudo", sudoArguments);
    if (sudoCheck.succeeded) {
      return ["sudo", "-n", "env", ...sudoEnvironmentPairs, "docker"];
    }
    failures.push(`sudo: ${sudoCheck.details}`);
  }

  throw new Error(`无法访问 Docker daemon：${failures.join("；")}`);
}
