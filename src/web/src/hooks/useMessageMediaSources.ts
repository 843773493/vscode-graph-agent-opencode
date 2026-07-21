import React from "react";
import { getSessionAttachmentBlob } from "../api";
import type { MessageMediaItem } from "../utils/messageMedia";

export function useMessageMediaSources(
  items: MessageMediaItem[],
  apiPort: number,
  sessionId: string,
  workspaceId?: string | null,
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
        const blob = await getSessionAttachmentBlob(
          apiPort,
          sessionId,
          item.attachment.file_id,
          workspaceId,
        );
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
      objectUrls.forEach((url) => URL.revokeObjectURL(url));
    };
  }, [apiPort, items, reloadVersion, sessionId, workspaceId]);

  return {
    sources,
    errors,
    reload: () => setReloadVersion((version) => version + 1),
  };
}
