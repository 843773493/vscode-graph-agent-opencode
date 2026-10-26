import { cpSync } from "node:fs";
import path from "node:path";

const PYTHON_CACHE_DIRECTORY_PATH = `${path.sep}__pycache__${path.sep}`;

export function copyApplicationSources(projectRoot, applicationRoot) {
  const copyOptions = {
    recursive: true,
    filter(source) {
      const relative = path.relative(projectRoot, source);
      const paddedRelative = `${path.sep}${relative}${path.sep}`;
      return (
        !paddedRelative.includes(PYTHON_CACHE_DIRECTORY_PATH) &&
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
  cpSync(
    path.join(projectRoot, "src", "shared"),
    path.join(applicationRoot, "src", "shared"),
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
