import { publicTarget } from "./config.js";
import { HttpError } from "./http.js";

export class TargetFileClient {
  constructor({ targetsConfig, tunnelManager }) {
    this.defaultTargetId = targetsConfig.defaultTargetId;
    this.targets = new Map();
    this.tunnelManager = tunnelManager;

    for (const target of targetsConfig.targets) {
      this.targets.set(target.id, target);
    }
  }

  listTargets() {
    return {
      defaultTargetId: this.defaultTargetId,
      targets: Array.from(this.targets.values(), publicTarget),
    };
  }

  requireTarget(targetId) {
    const resolvedTargetId = targetId || this.defaultTargetId;
    const target = this.targets.get(resolvedTargetId);
    if (!target) {
      throw new HttpError(404, `未知 attach 目标: ${resolvedTargetId}`);
    }
    return target;
  }

  async backendOriginFor(targetId) {
    const target = this.requireTarget(targetId);
    if (target.kind === "local") {
      return target.backendOrigin;
    }
    return this.tunnelManager.backendOriginFor(target);
  }

  publicTarget(targetId) {
    const target = this.requireTarget(targetId);
    return publicTarget(target);
  }
}
