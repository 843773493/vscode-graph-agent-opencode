import type { WorkspaceFileNode } from "../../types/backend";

export const MAX_FILE_TREE_CACHED_DIRECTORIES = 256;
export const MAX_FILE_TREE_CACHED_NODES = 20_000;
export const RESTORED_DIRECTORY_CONCURRENCY = 3;

export interface DirectoryCacheEntry {
  items: WorkspaceFileNode[];
  loading: boolean;
  error: string | null;
  truncated: boolean;
  nextCursor: string | null;
  stale: boolean;
  lastAccessedAt: number;
}

export function pruneDirectoryCache(
  cache: Record<string, DirectoryCacheEntry>,
  protectedPaths: ReadonlySet<string>,
  maxDirectories = MAX_FILE_TREE_CACHED_DIRECTORIES,
  maxNodes = MAX_FILE_TREE_CACHED_NODES,
): Record<string, DirectoryCacheEntry> {
  const entries = Object.entries(cache);
  let directoryCount = entries.length;
  let nodeCount = entries.reduce((total, [, entry]) => total + entry.items.length, 0);
  if (directoryCount <= maxDirectories && nodeCount <= maxNodes) {
    return cache;
  }

  const candidates = entries
    .filter(([path, entry]) => !protectedPaths.has(path) && !entry.loading)
    .sort((left, right) => left[1].lastAccessedAt - right[1].lastAccessedAt);
  const next = { ...cache };
  for (const [path, entry] of candidates) {
    if (directoryCount <= maxDirectories && nodeCount <= maxNodes) {
      break;
    }
    delete next[path];
    directoryCount -= 1;
    nodeCount -= entry.items.length;
  }
  return next;
}

export async function runWithConcurrency<T>(
  items: readonly T[],
  concurrency: number,
  worker: (item: T) => Promise<void>,
): Promise<void> {
  if (!Number.isInteger(concurrency) || concurrency < 1) {
    throw new Error(`目录恢复并发数必须是正整数: ${concurrency}`);
  }
  let nextIndex = 0;
  const runWorker = async () => {
    while (nextIndex < items.length) {
      const item = items[nextIndex];
      nextIndex += 1;
      if (item !== undefined) {
        await worker(item);
      }
    }
  };
  await Promise.all(
    Array.from(
      { length: Math.min(concurrency, items.length) },
      () => runWorker(),
    ),
  );
}

export async function restoreDirectoriesInOrder(
  paths: readonly string[],
  load: (path: string) => Promise<boolean>,
  parentOf: (path: string) => string,
  concurrency = RESTORED_DIRECTORY_CONCURRENCY,
): Promise<void> {
  const byDepth = new Map<number, string[]>();
  for (const path of paths) {
    let depth = 0;
    let current = path;
    const visited = new Set<string>();
    while (current && !visited.has(current)) {
      visited.add(current);
      depth += 1;
      current = parentOf(current);
    }
    const level = byDepth.get(depth) ?? [];
    level.push(path);
    byDepth.set(depth, level);
  }

  const failed = new Set<string>();
  const depths = [...byDepth.keys()].sort((left, right) => left - right);
  for (const depth of depths) {
    const level = byDepth.get(depth) ?? [];
    await runWithConcurrency(level, concurrency, async (path) => {
      let ancestor = parentOf(path);
      const visited = new Set<string>();
      while (ancestor && !visited.has(ancestor)) {
        if (failed.has(ancestor)) {
          failed.add(path);
          return;
        }
        visited.add(ancestor);
        ancestor = parentOf(ancestor);
      }
      if (!await load(path)) {
        failed.add(path);
      }
    });
  }
}
