import { t } from "../../../../shared/core/i18n/index.js";
import { useState, type ReactNode } from "react";
import type { Agent, ModelReference } from "../../types/backend";
import { sameModel } from "../../utils/modelReference";

/** 日常选择锚定输入区；Shift 点击仍进入宿主 Quick Input。 */
export default function ComposerModelControl({ models, currentModel, onOpen }: {
  models: Agent["models"];
  currentModel: ModelReference | null;
  onOpen(anchor?: HTMLButtonElement): void | Promise<void>;
}): ReactNode {
  const [open, setOpen] = useState(false);
  const current = models.find(model => sameModel(model.model_ref, currentModel));
  const identity = currentModel ? `${currentModel.provider_id} / ${currentModel.model_id}` : "";
  const label = current?.name ?? current?.model ?? (identity || t("composer.model.unavailable"));
  const error = current?.configuration_error?.trim() || (currentModel && !current ? t("composer.model.unavailable") : undefined);
  return <div className="composer-model-control"><button type="button"
    className={`composer-model-pill${error ? " configuration-error" : ""}`}
    title={error ? t("composer.model.currentError", error) : t("composer.model.selectWithId",
      current?.name ? `${label} · ${current.model}` : label, identity)}
    aria-label={error ? t("composer.model.selectError", label, error) : t("composer.model.select", label)}
    aria-haspopup="listbox" aria-expanded={open} onClick={async event => {
      setOpen(true);
      try { await onOpen(event.shiftKey ? undefined : event.currentTarget); }
      finally { setOpen(false); }
    }}>
    <span className="codicon codicon-chip composer-picker-button-icon" aria-hidden="true" />
    <span className="composer-model-label">{label}</span>
    <span className="codicon codicon-chevron-down" aria-hidden="true" />
    {error ? <span className="codicon codicon-error" aria-hidden="true" /> : null}
  </button></div>;
}
