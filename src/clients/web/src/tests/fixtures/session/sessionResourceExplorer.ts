import React, { useSyncExternalStore } from "react";
import type { GatewayWorkspaceRequestTarget } from "../../../api/workspaceIdentity";
import {
  useSessionGeneratorResources,
} from "../../../hooks/sessionResourceExplorer/useSessionGeneratorResources";
import { useSessionResourceExplorer } from "../../../hooks/session/useSessionResourceExplorer";
import { createEmptyGeneratorResourcesController } from "./appState";
import { testWorkspaceRequestTargetsFor } from "./gatewayEnvironment";
import { mountReactTestHarness } from "./reactHarness";

type ResourceExplorerProps = Omit<
  Parameters<typeof useSessionResourceExplorer>[0],
  "generatorResources"
>;

export type SessionResourceExplorerController = ReturnType<
  typeof useSessionResourceExplorer
>;

/** 探索器用例的公共 props；各用例只覆盖自己关心的字段。 */
export function createSessionResourceExplorerProps(
  overrides: Partial<ResourceExplorerProps> = {},
): ResourceExplorerProps {
  const activeWorkspaceId = overrides.activeWorkspaceId === undefined
    ? "ws-test"
    : overrides.activeWorkspaceId;
  return {
    apiPort: 49_400,
    workspaces: overrides.workspaces ?? testWorkspaceRequestTargetsFor(activeWorkspaceId),
    activeWorkspaceId,
    searchOpen: false,
    searchQuery: "",
    currentSessionId: "",
    workspaceNavigationSyncKey: "ws-test",
    catalogSyncKeys: new Map(),
    catalogRefreshVersions: new Map(),
    ...overrides,
  };
}

/**
 * 装配 useSessionResourceExplorer 的 Harness。liveGeneratorResources 为真时
 * 由真实 useSessionGeneratorResources 提供资源，否则使用空控制器；两者在
 * Harness 外分派，避免条件调用 hook。
 */
function useSessionResourceExplorerHarness(options: {
  props: ResourceExplorerProps;
  liveGeneratorResources?: boolean;
  onExplorer?: (explorer: SessionResourceExplorerController) => void;
}): () => React.ReactNode {
  const { props, onExplorer } = options;
  const subscribe = props.sessionCatalogOutboxRuntime?.subscribe ?? (() => () => undefined);
  const getSnapshot = props.sessionCatalogOutboxRuntime?.getSnapshot ?? (() => props.sessionCatalogOutboxSnapshot);
  if (options.liveGeneratorResources) {
    return function Harness(): React.ReactNode {
      const generatorResources = useSessionGeneratorResources(props.apiPort);
      const snapshot = useSyncExternalStore(subscribe, getSnapshot);
      const explorer = useSessionResourceExplorer({ ...props, sessionCatalogOutboxSnapshot: snapshot, generatorResources });
      onExplorer?.(explorer);
      return null;
    };
  }
  // 空控制器必须是稳定引用，否则每次渲染都换对象会让 explorer 的依赖反复失效。
  const generatorResources = createEmptyGeneratorResourcesController();
  return function Harness(): React.ReactNode {
    const snapshot = useSyncExternalStore(subscribe, getSnapshot);
    const explorer = useSessionResourceExplorer({
      ...props,
      sessionCatalogOutboxSnapshot: snapshot,
      generatorResources,
    });
    onExplorer?.(explorer);
    return null;
  };
}

/**
 * 挂载资源探索器并暴露句柄读取器，收敛「onExplorer 收集 + 空句柄检查」样板。
 * liveGeneratorResources 为真时由真实 useSessionGeneratorResources 供资源。
 */
export async function mountSessionResourceExplorerHarness(options: {
  props?: Partial<ResourceExplorerProps>;
  liveGeneratorResources?: boolean;
  flushes?: number;
}): Promise<{
  explorer: () => SessionResourceExplorerController;
  unmount: () => void;
}> {
  let explorer: SessionResourceExplorerController | null = null;
  const Harness = useSessionResourceExplorerHarness({
    props: createSessionResourceExplorerProps(options.props),
    liveGeneratorResources: options.liveGeneratorResources,
    onExplorer: (value) => {
      explorer = value;
    },
  });
  const unmount = await mountReactTestHarness(Harness, options.flushes ?? 2);
  if (!explorer) throw new Error("useSessionResourceExplorer Harness 未完成渲染");
  return { explorer: () => explorer!, unmount };
}

/**
 * 直播生成器资源版本的探索器 Harness 工厂：用例用 syncKey/currentSessionId
 * 驱动 props 更新，句柄通过 onExplorer 回传。
 */
export function createLiveSessionResourceExplorerHarness(options: {
  apiPort: number;
  activeWorkspaceId?: string | null;
  workspaceNavigationSyncKey?: string;
  onExplorer: (explorer: SessionResourceExplorerController) => void;
}): (props: { syncKey?: string; currentSessionId?: string }) => React.ReactNode {
  const activeWorkspaceId = options.activeWorkspaceId ?? "ws-test";
  return function Explorer(props): React.ReactNode {
    const generatorResources = useSessionGeneratorResources(options.apiPort);
    const explorer = useSessionResourceExplorer({
      ...createSessionResourceExplorerProps({
        apiPort: options.apiPort,
        activeWorkspaceId,
        workspaceNavigationSyncKey:
          props.syncKey ?? options.workspaceNavigationSyncKey ?? "ws-test",
        currentSessionId: props.currentSessionId ?? "",
      }),
      generatorResources,
    });
    options.onExplorer(explorer);
    return null;
  };
}
