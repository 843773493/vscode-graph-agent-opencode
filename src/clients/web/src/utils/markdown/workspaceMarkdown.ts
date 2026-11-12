import { t } from "../../../../shared/core/i18n/index.js";
import { resourceFileRootUri } from "../workspaceFileReferences";

export type WorkspaceMarkdownTarget =
  | { kind: "external"; href: string }
  | { kind: "anchor"; href: string }
  | { kind: "workspace"; path: string; fragment: string };

const URI_SCHEME = /^[a-z][a-z0-9+.-]*:/i;

export function resolveWorkspaceMarkdownTarget(
  markdownPath: string,
  target: string,
): WorkspaceMarkdownTarget {
  const trimmed = target.trim();
  if (!trimmed) {
    throw new Error(t("workspace.markdown.errors.emptyAddress"));
  }
  if (trimmed.startsWith("#")) {
    return { kind: "anchor", href: trimmed };
  }
  if (trimmed.startsWith("boxteam://")) {
    const index = trimmed.indexOf("#");
    return { kind: "workspace", path: index < 0 ? trimmed : trimmed.slice(0, index), fragment: index < 0 ? "" : trimmed.slice(index) };
  }
  if ((URI_SCHEME.test(trimmed) && !/^[A-Za-z]:[\\/]/.test(trimmed)) || trimmed.startsWith("//")) {
    return { kind: "external", href: trimmed };
  }

  const hashIndex = trimmed.indexOf("#");
  const fragment = hashIndex >= 0 ? trimmed.slice(hashIndex) : "";
  const withoutFragment = hashIndex >= 0 ? trimmed.slice(0, hashIndex) : trimmed;
  const queryIndex = withoutFragment.indexOf("?");
  const encodedPath = queryIndex >= 0
    ? withoutFragment.slice(0, queryIndex)
    : withoutFragment;
  let decodedPath: string;
  try {
    decodedPath = decodeURIComponent(encodedPath);
  } catch (error) {
    throw new Error(t("workspace.markdown.errors.invalidEscape", target), { cause: error });
  }

  if (decodedPath.startsWith("/") || /^[A-Za-z]:[\\/]/.test(decodedPath)) {
    return { kind: "workspace", path: decodedPath, fragment };
  }
  const resourceRootUri = resourceFileRootUri(markdownPath);
  if (resourceRootUri === null) throw new Error(t("workspace.markdown.errors.fileMissingUri"));
  const resourceRootEndIndex = resourceRootUri.length;
  if (resourceRootEndIndex === markdownPath.length) throw new Error(t("workspace.markdown.errors.addressNotFile"));
  const baseSegments = markdownPath.slice(resourceRootEndIndex + 1).split("/").slice(0, -1).map(decodeURIComponent);
  for (const segment of decodedPath.replace(/\\/g, "/").split("/")) {
    if (!segment || segment === ".") {
      continue;
    }
    if (segment === "..") {
      if (baseSegments.length === 0) {
        throw new Error(t("workspace.markdown.errors.outsideWorkspace", target));
      }
      baseSegments.pop();
      continue;
    }
    baseSegments.push(segment);
  }
  if (baseSegments.length === 0) {
    throw new Error(t("workspace.markdown.errors.targetNotFile", target));
  }
  return {
    kind: "workspace",
    path: resourceRootUri + "/" + baseSegments.map(encodeURIComponent).join("/"),
    fragment,
  };
}
