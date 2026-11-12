import { t } from "../../../../shared/core/i18n/index.js";
import type { ReactNode } from "react";
import type { InternalExecutionDisplayPart } from "../../types/backend";

export default function InternalExecutionTranscriptItem({
  displayPart,
}: {
  displayPart: InternalExecutionDisplayPart;
}): ReactNode {
  return (
    <article className="chat-internal-execution-display" aria-label={t("chat.generatedSession")}>
      <span className="chat-internal-execution-label">{t("chat.generatedSession")}</span>
      <p className="chat-internal-execution-text">{displayPart.text}</p>
    </article>
  );
}
