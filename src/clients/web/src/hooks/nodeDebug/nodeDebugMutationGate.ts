export type NodeDebugMutationMode = "action" | "loading";

export interface NodeDebugMutation {
  readonly ownerKey: string;
  readonly ownerGeneration: number;
  readonly mutationGeneration: number;
  readonly mode: NodeDebugMutationMode;
}

export interface NodeDebugMutationGateSnapshot {
  readonly ownerKey: string;
  readonly ownerGeneration: number;
  readonly mutationGeneration: number;
}

export interface NodeDebugBusyFlags {
  readonly actionBusy: boolean;
  readonly loading: boolean;
}

export interface NodeDebugMutationReleaseResult {
  readonly released: boolean;
  readonly wasCurrent: boolean;
  readonly isCurrentOwner: boolean;
}

/**
 * 管理调试 owner 与 mutation 的代际、锁和忙碌状态。
 *
 * 该对象不依赖 React，owner 切换后旧锁仍保留到请求 settle，避免切回
 * 同一 owner 时重复发起后端 mutation。
 */
export class NodeDebugMutationGate {
  private currentOwnerKey: string;
  private currentOwnerGeneration = 0;
  private currentMutationGeneration = 0;
  private readonly mutationLocks = new Map<string, NodeDebugMutation>();

  constructor(ownerKey: string) {
    this.currentOwnerKey = ownerKey;
  }

  get ownerKey(): string {
    return this.currentOwnerKey;
  }

  get ownerGeneration(): number {
    return this.currentOwnerGeneration;
  }

  get mutationGeneration(): number {
    return this.currentMutationGeneration;
  }

  switchOwner(ownerKey: string): boolean {
    if (this.currentOwnerKey === ownerKey) return false;
    this.currentOwnerKey = ownerKey;
    this.currentOwnerGeneration += 1;
    return true;
  }

  captureSnapshot(ownerKey = this.currentOwnerKey): NodeDebugMutationGateSnapshot {
    return {
      ownerKey,
      ownerGeneration: this.currentOwnerGeneration,
      mutationGeneration: this.currentMutationGeneration,
    };
  }

  beginMutation(ownerKey: string, mode: NodeDebugMutationMode): NodeDebugMutation | null {
    if (this.mutationLocks.has(ownerKey)) return null;
    const mutation: NodeDebugMutation = {
      ownerKey,
      ownerGeneration: this.currentOwnerGeneration,
      mutationGeneration: ++this.currentMutationGeneration,
      mode,
    };
    this.mutationLocks.set(ownerKey, mutation);
    return mutation;
  }

  isCurrentOwner(ownerKey: string, ownerGeneration: number): boolean {
    return this.currentOwnerKey === ownerKey
      && this.currentOwnerGeneration === ownerGeneration;
  }

  isCurrentSnapshot(
    snapshot: NodeDebugMutationGateSnapshot,
    requireUnlocked = false,
  ): boolean {
    return this.isCurrentOwner(snapshot.ownerKey, snapshot.ownerGeneration)
      && this.currentMutationGeneration === snapshot.mutationGeneration
      && (!requireUnlocked || !this.mutationLocks.has(snapshot.ownerKey));
  }

  isCurrentMutation(
    mutation: Pick<NodeDebugMutation, "ownerKey" | "ownerGeneration" | "mutationGeneration">,
  ): boolean {
    const current = this.mutationLocks.get(mutation.ownerKey);
    return this.isCurrentOwner(mutation.ownerKey, mutation.ownerGeneration)
      && this.currentMutationGeneration === mutation.mutationGeneration
      && current?.ownerKey === mutation.ownerKey
      && current.ownerGeneration === mutation.ownerGeneration
      && current.mutationGeneration === mutation.mutationGeneration;
  }

  hasMutation(ownerKey = this.currentOwnerKey): boolean {
    return this.mutationLocks.has(ownerKey);
  }

  releaseMutation(mutation: NodeDebugMutation): NodeDebugMutationReleaseResult {
    if (this.mutationLocks.get(mutation.ownerKey) !== mutation) {
      return {
        released: false,
        wasCurrent: false,
        isCurrentOwner: this.currentOwnerKey === mutation.ownerKey,
      };
    }
    const wasCurrent = this.isCurrentMutation(mutation);
    this.mutationLocks.delete(mutation.ownerKey);
    return {
      released: true,
      wasCurrent,
      isCurrentOwner: this.currentOwnerKey === mutation.ownerKey,
    };
  }

  busyFlags(visible: boolean): NodeDebugBusyFlags {
    if (!visible) return { actionBusy: false, loading: false };
    const mutation = this.mutationLocks.get(this.currentOwnerKey);
    return {
      actionBusy: mutation?.mode === "action",
      loading: mutation?.mode === "loading",
    };
  }
}
