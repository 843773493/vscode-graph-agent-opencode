import React from "react";
import { useSessionMessageStream } from "../../../hooks/session/useSessionMessageStream";
import type { SetAppState } from "../../../hooks/contentViewLoaderTypes";

/** 装配 useSessionMessageStream 的 Harness 组件。 */
export function createSessionMessageStreamHarness(
  props: Omit<Parameters<typeof useSessionMessageStream>[0], "setState">,
  setState: SetAppState,
): () => React.ReactNode {
  return function Harness(): React.ReactNode {
    useSessionMessageStream({ ...props, setState });
    return null;
  };
}
