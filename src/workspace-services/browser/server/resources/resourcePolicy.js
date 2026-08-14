export const DEFAULT_RESOURCE_POLICY = Object.freeze({
  hotSoftLimit: 8,
  frozenSoftLimit: 16,
  residentSoftLimit: 24,
  idleFreezeMs: 10 * 60_000,
  capacityFreezeIdleMs: 60_000,
  warningFreezeMs: 60_000,
  criticalFreezeMs: 30_000,
  emergencyFreezeMs: 0,
  minimumDiscardIdleMs: 10 * 60_000,
  criticalDiscardIdleMs: 2 * 60_000,
  minimumFrozenBeforeDiscardMs: 30_000,
  emergencyDiscardIdleMs: 0,
  emergencyFrozenBeforeDiscardMs: 0,
});

const PRESSURE_FREEZE_IDLE = Object.freeze({
  normal: DEFAULT_RESOURCE_POLICY.idleFreezeMs,
  warning: DEFAULT_RESOURCE_POLICY.warningFreezeMs,
  critical: DEFAULT_RESOURCE_POLICY.criticalFreezeMs,
  emergency: DEFAULT_RESOURCE_POLICY.emergencyFreezeMs,
});

const HOT_STATES = new Set(["active", "background"]);
const FROZEN_STATES = new Set(["frozen"]);
const RESIDENT_STATES = new Set([
  "active",
  "background",
  "frozen",
  "freezing",
  "restoring",
  "discarding",
]);
const SOFT_REASON_PREFIXES = [
  "keep_alive",
  "websocket_",
  "media_playing:",
  "webrtc_media_live:",
  "picture_in_picture:",
];

function timestampMs(value) {
  const parsed = Date.parse(value || "");
  return Number.isFinite(parsed) ? parsed : 0;
}

export function latestActivityMs(snapshot) {
  return Math.max(
    timestampMs(snapshot.last_user_interaction_at),
    timestampMs(snapshot.last_agent_operation_at),
    timestampMs(snapshot.last_attach_at),
    timestampMs(snapshot.created_at),
  );
}

function isSoftReason(reason) {
  return SOFT_REASON_PREFIXES.some((prefix) => String(reason).startsWith(prefix));
}

function hardProtectionReasons(snapshot) {
  if (Array.isArray(snapshot.resource_protections)) {
    return snapshot.resource_protections
      .filter((protection) => protection?.class === "hard")
      .map((protection) => protection.code);
  }
  if (Array.isArray(snapshot.resource_hard_protection_reasons)) {
    return snapshot.resource_hard_protection_reasons;
  }
  return Array.isArray(snapshot.resource_protection_reasons)
    ? snapshot.resource_protection_reasons.filter((reason) => !isSoftReason(reason))
    : [];
}

function softProtectionReasons(snapshot) {
  const reasons = Array.isArray(snapshot.resource_protections)
    ? snapshot.resource_protections
        .filter((protection) => protection?.class === "soft")
        .map((protection) => protection.code)
    : Array.isArray(snapshot.resource_soft_protection_reasons)
    ? [...snapshot.resource_soft_protection_reasons]
    : (Array.isArray(snapshot.resource_protection_reasons)
        ? snapshot.resource_protection_reasons.filter(isSoftReason)
        : []);
  if (snapshot.resource_policy === "keep_alive" && !reasons.includes("keep_alive")) {
    reasons.push("keep_alive");
  }
  return reasons;
}

function compareCandidates(left, right) {
  if (left.lastActivityMs !== right.lastActivityMs) {
    return left.lastActivityMs - right.lastActivityMs;
  }
  return String(left.browserId).localeCompare(String(right.browserId));
}

function capacityCounts(candidates) {
  return candidates.reduce((counts, candidate) => {
    const state = candidate.snapshot.resource_state;
    if (HOT_STATES.has(state)) counts.hot += 1;
    if (FROZEN_STATES.has(state)) counts.frozen += 1;
    if (RESIDENT_STATES.has(state)) counts.resident += 1;
    return counts;
  }, { hot: 0, frozen: 0, resident: 0 });
}

function capacityOverflow(counts, policy) {
  return {
    hot: Math.max(0, counts.hot - policy.hotSoftLimit),
    frozen: Math.max(0, counts.frozen - policy.frozenSoftLimit),
    resident: Math.max(0, counts.resident - policy.residentSoftLimit),
  };
}

function hasOverflow(overflow) {
  return overflow.hot > 0 || overflow.frozen > 0 || overflow.resident > 0;
}

function candidateProtection(candidate, allowSoftProtection) {
  const hard = hardProtectionReasons(candidate.snapshot);
  const soft = softProtectionReasons(candidate.snapshot);
  return {
    hard,
    soft,
    protected: hard.length > 0 || (!allowSoftProtection && soft.length > 0),
  };
}

export function chooseResourcePlan(snapshots, {
  pressureLevel = "normal",
  nowMs = Date.now(),
  policy = DEFAULT_RESOURCE_POLICY,
  allowDiscard = true,
  maxActions = Number.POSITIVE_INFINITY,
} = {}) {
  const effective = { ...DEFAULT_RESOURCE_POLICY, ...policy };
  const allowSoftProtection = ["critical", "emergency"].includes(pressureLevel);
  const freezeIdleMs = pressureLevel === "normal"
    ? effective.idleFreezeMs
    : {
        ...PRESSURE_FREEZE_IDLE,
        warning: effective.warningFreezeMs,
        critical: effective.criticalFreezeMs,
        emergency: effective.emergencyFreezeMs,
      }[pressureLevel];
  const candidates = snapshots.map((snapshot) => {
    const activityMs = latestActivityMs(snapshot);
    const candidate = {
      snapshot,
      browserId: snapshot.browser_id,
      lastActivityMs: activityMs,
      idleMs: Math.max(0, nowMs - activityMs),
    };
    return { ...candidate, protection: candidateProtection(candidate, allowSoftProtection) };
  });
  const initialCounts = capacityCounts(candidates);
  const simulatedCounts = { ...initialCounts };
  const actions = [];
  const severePressure = ["critical", "emergency"].includes(pressureLevel);
  const discardIdleMs = pressureLevel === "emergency"
    ? effective.emergencyDiscardIdleMs
    : (pressureLevel === "critical"
        ? effective.criticalDiscardIdleMs
        : effective.minimumDiscardIdleMs);
  const frozenBeforeDiscardMs = pressureLevel === "emergency"
    ? effective.emergencyFrozenBeforeDiscardMs
    : effective.minimumFrozenBeforeDiscardMs;

  const discardCandidates = candidates
    .filter(({ snapshot, protection }) => snapshot.resource_state === "frozen"
      && snapshot.client_count === 0
      && !protection.protected
      && nowMs - timestampMs(snapshot.frozen_at) >= frozenBeforeDiscardMs)
    .sort(compareCandidates);
  let planTruncated = false;
  if (allowDiscard) {
    for (const candidate of discardCandidates) {
      const overflow = capacityOverflow(simulatedCounts, effective);
      const capacityDriven = overflow.frozen > 0 || overflow.resident > 0;
      if (!capacityDriven && (!severePressure || candidate.idleMs < discardIdleMs)) continue;
      if (actions.length >= maxActions) {
        planTruncated = true;
        break;
      }
      actions.push({
        action: "discard",
        browserId: candidate.browserId,
        idleMs: candidate.idleMs,
        reason: capacityDriven ? "resident_capacity" : `memory_pressure:${pressureLevel}`,
      });
      simulatedCounts.frozen -= 1;
      simulatedCounts.resident -= 1;
    }
  }

  const freezeCandidates = candidates
    .filter(({ snapshot, protection }) => HOT_STATES.has(snapshot.resource_state)
      && snapshot.client_count === 0
      && !protection.protected)
    .sort(compareCandidates);
  for (const candidate of freezeCandidates) {
    const overflow = capacityOverflow(simulatedCounts, effective);
    const capacityDriven = overflow.hot > 0;
    const thresholdMs = capacityDriven
      ? Math.min(freezeIdleMs, effective.capacityFreezeIdleMs)
      : freezeIdleMs;
    if (candidate.idleMs < thresholdMs) continue;
    if (pressureLevel === "normal" && simulatedCounts.frozen >= effective.frozenSoftLimit) {
      break;
    }
    if (actions.length >= maxActions) {
      planTruncated = true;
      break;
    }
    actions.push({
      action: "freeze",
      browserId: candidate.browserId,
      idleMs: candidate.idleMs,
      reason: capacityDriven ? "hot_capacity" : `memory_pressure:${pressureLevel}`,
    });
    simulatedCounts.hot -= 1;
    simulatedCounts.frozen += 1;
  }

  const initialOverflow = capacityOverflow(initialCounts, effective);
  const remainingOverflow = capacityOverflow(simulatedCounts, effective);
  const protectedResidents = candidates.filter(({ snapshot, protection }) => (
    RESIDENT_STATES.has(snapshot.resource_state) && protection.protected
  )).length;
  const transitioningResidents = candidates.filter(({ snapshot }) => (
    ["freezing", "restoring", "discarding"].includes(snapshot.resource_state)
  )).length;
  const notYetEligible = candidates.filter(({ snapshot, idleMs, protection }) => {
    if (protection.protected) return false;
    if (HOT_STATES.has(snapshot.resource_state)) {
      return idleMs < Math.min(freezeIdleMs, effective.capacityFreezeIdleMs);
    }
    if (snapshot.resource_state === "frozen") {
      return idleMs < discardIdleMs
        || nowMs - timestampMs(snapshot.frozen_at) < frozenBeforeDiscardMs;
    }
    return false;
  }).length;
  return {
    actions,
    capacity: {
      limits: {
        hot: effective.hotSoftLimit,
        frozen: effective.frozenSoftLimit,
        resident: effective.residentSoftLimit,
      },
      before: initialCounts,
      after_plan: simulatedCounts,
      overflow_before: initialOverflow,
      overflow_after_plan: remainingOverflow,
      protected_residents: protectedResidents,
      blockers: {
        protected: protectedResidents,
        not_yet_eligible: notYetEligible,
        transitioning: transitioningResidents,
      },
      protected_capacity_overflow: hasOverflow(remainingOverflow)
        && actions.length === 0
        && protectedResidents > 0
        && notYetEligible === 0
        && transitioningResidents === 0,
    },
    has_backlog: planTruncated,
  };
}

export function chooseResourceAction(snapshots, options = {}) {
  return chooseResourcePlan(snapshots, { ...options, maxActions: 1 }).actions[0] || null;
}
