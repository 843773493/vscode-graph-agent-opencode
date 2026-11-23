import React from "react";
import type { SessionViewState } from "../../state/sessionViewState";
import type { TranscriptViewportPosition, TranscriptViewportChangeReason } from "./useTranscriptNavigation";
import type { DisclosureOverrides, TranscriptNodeKey } from "../../state/timeline/transcriptNodes";
import { disclosureKey } from "../../state/timeline/transcriptNodes";

export interface TranscriptView {
  disclosures: DisclosureOverrides;
  setDisclosure: (key: string, value: boolean, defaultValue: boolean) => void;
}
export const TranscriptViewContext = React.createContext<TranscriptView | null>(null);

/** 三层开关只写入编辑器的同一展示态 owner，不在各行复制 open 状态。 */
export function useTranscriptDisclosure(key: TranscriptNodeKey, defaultValue = false, phase?: "live" | "settled") {
  const view = React.useContext(TranscriptViewContext);
  if (!view) throw new Error("transcript 缺少展示态 owner");
  const id = disclosureKey(key, phase);
  const open = view.disclosures[id] ?? defaultValue;
  const setDisclosure = view.setDisclosure;
  const setOpen = React.useCallback((value: boolean) => setDisclosure(id, value, defaultValue), [setDisclosure, id, defaultValue]);
  return [open, setOpen] as const;
}

/** 当前编辑器的单一展示态 owner；预览也复用此入口，但不写持久默认位置。 */
export function useTranscriptView(scope: string, viewState?: SessionViewState | null,
  onViewStateChange?: (position: TranscriptViewportPosition, reason: TranscriptViewportChangeReason) => void,
  capture?: () => TranscriptViewportPosition | undefined) {
  const [disclosureState, setDisclosureState] = React.useState<{scope: string; values: DisclosureOverrides}>({scope: scope, values: viewState?.disclosures ?? {}});
  const interacted = React.useRef(false);
  const hydratedScope = React.useRef(viewState ? scope : null);
  const lastPosition = React.useRef<TranscriptViewportPosition>({reading_anchor: null, follow_latest: true});
  const disclosures = disclosureState.scope === scope ? disclosureState.values : viewState?.disclosures ?? {};
  const valuesRef = React.useRef(disclosures); valuesRef.current = disclosures;
  React.useLayoutEffect(() => {
    interacted.current = false;
    lastPosition.current = viewState ?? {reading_anchor: null, follow_latest: true};
    hydratedScope.current = viewState ? scope : null;
    setDisclosureState(previous => previous.scope === scope ? previous : {scope, values: viewState?.disclosures ?? {}});
  }, [scope]);
  React.useLayoutEffect(() => {
    // 只水合首次异步读取；其它编辑器随后写入的持久默认值不拉动当前视图。
    if (!viewState || hydratedScope.current === scope || interacted.current) return;
    hydratedScope.current = scope;
    setDisclosureState({scope, values: viewState?.disclosures ?? {}});
  }, [viewState, scope]);
  const publishPosition = React.useCallback((position: TranscriptViewportPosition, reason: TranscriptViewportChangeReason) => {
    lastPosition.current = position;
    if (reason !== "layout") interacted.current = true;
    onViewStateChange?.({...position, disclosures: valuesRef.current}, reason);
  }, [onViewStateChange]);
  const setDisclosure = React.useCallback((key: string, value: boolean, defaultValue: boolean) => {
    if ((valuesRef.current[key] ?? defaultValue) === value) return;
    const next = {...valuesRef.current};
    if (value === defaultValue) delete next[key]; else next[key] = value;
    valuesRef.current = next; interacted.current = true;
    setDisclosureState({scope: scope, values: next});
    onViewStateChange?.({...capture?.() ?? lastPosition.current, disclosures: next}, "disclosure");
  }, [scope, onViewStateChange, capture]);
  const transcriptView = React.useMemo(() => ({disclosures, setDisclosure}), [disclosures, setDisclosure]);
  return {transcriptView, publishPosition};
}

/** 只从本次被隐藏的子树收回焦点；Composer 或其它编辑器的焦点保持原位。 */
export function useDisclosureFocus(open: boolean) {
  const root = React.useRef<HTMLElement | null>(null);
  const header = React.useRef<HTMLButtonElement | null>(null);
  const previous = React.useRef(open);
  const tree = root.current?.getRootNode() as Document | ShadowRoot | undefined;
  const focused = tree?.activeElement;
  const restore = previous.current && !open && !!focused && focused !== header.current && root.current?.contains(focused);
  React.useLayoutEffect(() => {
    previous.current = open;
    if (restore) header.current?.focus({preventScroll: true});
  }, [open, restore]);
  return {root, header};
}
