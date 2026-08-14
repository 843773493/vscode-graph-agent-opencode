import { turnIdsInvalidatedByEvents } from "../../state/session/turnTimeline";
import { isJobTerminalTraceType } from "../../state/traceEvents";
import type { SessionStreamEvent } from "../../types/backend";

export interface TurnRefreshPlan {
  genericTurnIds: string[];
  terminalEventIndex: number;
}

export function planTurnRefreshes(
  events: readonly SessionStreamEvent[],
): TurnRefreshPlan {
  let terminalEventIndex = -1;
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (isJobTerminalTraceType(events[index].type)) {
      terminalEventIndex = index;
      break;
    }
  }
  const terminalTurnId = terminalEventIndex === -1
    ? null
    : events[terminalEventIndex].job_id;
  return {
    terminalEventIndex,
    genericTurnIds: turnIdsInvalidatedByEvents(events).filter(
      (turnId) => turnId !== terminalTurnId,
    ),
  };
}
