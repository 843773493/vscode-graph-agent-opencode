import React from "react";
import {
  selectVisibleTurnDetailBatches,
  type TurnVisibleRange,
} from "../../state/session/turnDetailHydration";
import type { ConversationView } from "../../types/frontend";

interface DetailHydrationScope {
  key: string;
  pendingTurnIds: Set<string>;
}

export function useVisibleTurnDetailHydration({
  sessionId,
  timelineGeneration,
  projectionEpoch,
  conversations,
  firstItemIndex,
  loadingTurnIds,
  onLoadTurnDetails,
}: {
  sessionId: string;
  timelineGeneration: number;
  projectionEpoch: number | null;
  conversations: ConversationView[];
  firstItemIndex: number;
  loadingTurnIds: readonly string[];
  onLoadTurnDetails: (turnIds: string[]) => Promise<void>;
}): {
  detailHydrationError: string | null;
  clearDetailHydrationError: () => void;
  hydrateVisibleTurns: (range: TurnVisibleRange) => void;
} {
  const [detailHydrationError, setDetailHydrationError] = React.useState<string | null>(null);
  const scopeKey = `${sessionId}:${timelineGeneration}:${projectionEpoch ?? "none"}`;
  const scopeRef = React.useRef<DetailHydrationScope>({
    key: scopeKey,
    pendingTurnIds: new Set(),
  });
  if (scopeRef.current.key !== scopeKey) {
    scopeRef.current = {
      key: scopeKey,
      pendingTurnIds: new Set(),
    };
  }

  React.useEffect(() => {
    setDetailHydrationError(null);
  }, [scopeKey]);

  const hydrateVisibleTurns = React.useCallback((range: TurnVisibleRange) => {
    const requestScope = scopeRef.current;
    const batches = selectVisibleTurnDetailBatches({
      conversations,
      range,
      firstItemIndex,
      loadingTurnIds: [
        ...loadingTurnIds,
        ...requestScope.pendingTurnIds,
      ],
    });
    for (const batch of batches) {
      for (const turnId of batch) {
        requestScope.pendingTurnIds.add(turnId);
      }
      void onLoadTurnDetails(batch)
        .then(() => {
          if (scopeRef.current === requestScope) {
            setDetailHydrationError(null);
          }
        })
        .catch((error: unknown) => {
          if (scopeRef.current === requestScope) {
            setDetailHydrationError(
              error instanceof Error ? error.message : String(error),
            );
          }
        })
        .finally(() => {
          if (scopeRef.current === requestScope) {
            for (const turnId of batch) {
              requestScope.pendingTurnIds.delete(turnId);
            }
          }
        });
    }
  }, [conversations, firstItemIndex, loadingTurnIds, onLoadTurnDetails]);

  return {
    detailHydrationError,
    clearDetailHydrationError: React.useCallback(
      () => setDetailHydrationError(null),
      [],
    ),
    hydrateVisibleTurns,
  };
}
