import { cpSync } from "node:fs";
import path from "node:path";

export function copyApplicationSources(projectRoot, applicationRoot) {
  const copyOptions = {
    recursive: true,
    filter(source) {
      const relative = path.relative(projectRoot, source);
      return (
        !relative.split(path.sep).includes("__pycache__") &&
        !relative.endsWith(".pyc")
      );
    },
  };
  cpSync(
    path.join(projectRoot, "app"),
    path.join(applicationRoot, "app"),
    copyOptions,
  );
  cpSync(
    path.join(projectRoot, "configs"),
    path.join(applicationRoot, "configs"),
    copyOptions,
  );
  cpSync(
    path.join(projectRoot, "resources"),
    path.join(applicationRoot, "resources"),
    copyOptions,
  );
  for (const service of ["terminal", "browser"]) {
    cpSync(
      path.join(projectRoot, "src", "workspace-services", service, "server"),
      path.join(applicationRoot, "src", "workspace-services", service, "server"),
      copyOptions,
    );
    cpSync(
      path.join(projectRoot, "src", "workspace-services", service, "protocol"),
      path.join(applicationRoot, "src", "workspace-services", service, "protocol"),
      copyOptions,
    );
  }
  cpSync(
    path.join(projectRoot, "src", "workspace-services", "terminal", "shared"),
    path.join(applicationRoot, "src", "workspace-services", "terminal", "shared"),
    copyOptions,
  );
  cpSync(
    path.join(projectRoot, "src", "workspace-services", "protocol"),
    path.join(applicationRoot, "src", "workspace-services", "protocol"),
    copyOptions,
  );
  cpSync(
    path.join(projectRoot, "pyproject.toml"),
    path.join(applicationRoot, "pyproject.toml"),
  );
}
