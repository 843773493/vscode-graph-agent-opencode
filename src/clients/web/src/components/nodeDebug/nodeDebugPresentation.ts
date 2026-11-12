import { t } from "../../../../shared/core/i18n/index.js";
import type {
  NodeDebugActionRecord,
  NodeDebugLaunchProfile,
  NodeDebugState,
} from "../../types/backend";

export function nodeDebugStatusLabel(status: NodeDebugState["status"]): string {
  return {
    idle: t("debug.status.idle"),
    starting: t("debug.status.starting"),
    running: t("debug.status.running"),
    paused: t("debug.status.paused"),
    stopping: t("debug.status.stopping"),
    exited: t("debug.status.exited"),
    failed: t("debug.status.failed"),
    reconcile_required: t("debug.status.reconcile"),
  }[status];
}

export function nodeDebugActionActor(action: NodeDebugActionRecord): string {
  return action.actor === "ai" ? "AI" : action.actor === "system" ? t("debug.actor.system") : t("debug.actor.user");
}

export function nodeDebugProfileLabel(profile: NodeDebugLaunchProfile): string {
  const support = profile.supported ? "" : t("debug.profile.unsupported");
  return `${profile.name} · ${profile.runtime}${support}`;
}

export function nodeDebugPauseReasonLabel(reason: string): string {
  return {
    other: t("debug.pause.breakpoint"),
    exception: t("debug.pause.exception"),
    step: t("debug.pause.step"),
    pause: t("debug.pause.manual"),
  }[reason] ?? reason;
}
