import React from "react";
import { getSessionAttachmentBlob } from "../api";
import type { MessageMediaItem } from "../utils/messageMedia";

const THUMBNAIL_CACHE_MAX_ENTRIES = 96;
const thumbnailBlobCache = new Map<string, Promise<Blob>>();

function cachedThumbnailBlob(key: string, loader: () => Promise<Blob>): Promise<Blob> {
  const cached = thumbnailBlobCache.get(key);
  if (cached) {
    thumbnailBlobCache.delete(key);
    thumbnailBlobCache.set(key, cached);
    return cached;
  }
  const pending = loader().catch((error: unknown) => {
    thumbnailBlobCache.delete(key);
    throw error;
  });
  thumbnailBlobCache.set(key, pending);
  while (thumbnailBlobCache.size > THUMBNAIL_CACHE_MAX_ENTRIES) {
    const oldestKey = thumbnailBlobCache.keys().next().value;
    if (typeof oldestKey !== "string") break;
    thumbnailBlobCache.delete(oldestKey);
  }
  return pending;
}

export function useMessageMediaSources(
  items: MessageMediaItem[],
  apiPort: number,
  sessionId: string,
  workspaceId?: string | null,
  variant: "thumbnail" | "original" = "thumbnail",
): {
  sources: ReadonlyMap<string, string>;
  errors: ReadonlyMap<string, string>;
  reload: () => void;
} {
  const [sources, setSources] = React.useState<ReadonlyMap<string, string>>(new Map());
  const [errors, setErrors] = React.useState<ReadonlyMap<string, string>>(new Map());
  const [reloadVersion, setReloadVersion] = React.useState(0);

  React.useEffect(() => {
    let active = true;
    const controller = new AbortController();
    const objectUrls: string[] = [];
    const nextSources = new Map<string, string>();
    const nextErrors = new Map<string, string>();
    setErrors(new Map());

    void Promise.all(items.map(async (item) => {
      if (item.kind !== "image") {
        return;
      }
      if (item.attachment.data_url?.startsWith("data:image/")) {
        nextSources.set(item.id, item.attachment.data_url);
        return;
      }
      try {
        const loadBlob = () => getSessionAttachmentBlob(
            apiPort,
            sessionId,
            item.attachment.file_id,
            workspaceId,
            {
              variant,
              signal: variant === "original" ? controller.signal : undefined,
            },
          );
        const blob = variant === "thumbnail"
          ? await cachedThumbnailBlob(
              [apiPort, workspaceId ?? "local", sessionId, item.attachment.file_id].join("::"),
              loadBlob,
            )
          : await loadBlob();
        const objectUrl = URL.createObjectURL(blob);
        objectUrls.push(objectUrl);
        nextSources.set(item.id, objectUrl);
      } catch (error) {
        nextErrors.set(item.id, error instanceof Error ? error.message : String(error));
      }
    })).then(() => {
      if (!active) {
        objectUrls.forEach((url) => URL.revokeObjectURL(url));
        return;
      }
      setSources(nextSources);
      setErrors(nextErrors);
    });

    return () => {
      active = false;
      controller.abort();
      objectUrls.forEach((url) => URL.revokeObjectURL(url));
    };
  }, [apiPort, items, reloadVersion, sessionId, variant, workspaceId]);

  return {
    sources,
    errors,
    reload: () => setReloadVersion((version) => version + 1),
  };
}
