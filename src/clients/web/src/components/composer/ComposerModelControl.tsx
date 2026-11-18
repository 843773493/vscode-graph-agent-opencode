import { t } from "../../../../shared/core/i18n/index.js";
import type { ReactNode } from "react";
import type { Agent, ModelReference } from "../../types/backend";
import { sameModel } from "../../utils/modelReference";

/** 模型选择交给宿主原生 Quick Pick；此处只显示后端当前绑定。 */
export default function ComposerModelControl({ models, currentModel, onOpen }: {
  models: Agent["models"];
  currentModel: ModelReference | null;
  onOpen(): void;
}): ReactNode {
  const current = models.find(model => sameModel(model.model_ref, currentModel));
  const identity = currentModel ? `${currentModel.provider_id} / ${currentModel.model_id}` : "";
  const label = current?.name ?? current?.model ?? (identity || t("composer.model.unavailable"));
  const error = current?.configuration_error?.trim() || (currentModel && !current ? t("composer.model.unavailable") : undefined);
  return <div className="composer-model-control"><button type="button"
    className={`composer-model-pill${error ? " configuration-error" : ""}`}
    title={error ? t("composer.model.currentError", error) : t("composer.model.selectWithId",
      current?.name ? `${label} · ${current.model}` : label, identity)}
    aria-label={error ? t("composer.model.selectError", label, error) : t("composer.model.select", label)}
    aria-haspopup="dialog" onClick={onOpen}>
    <span className="codicon codicon-chip composer-picker-button-icon" aria-hidden="true" />
    <span className="composer-model-label">{label}</span>
    {error ? <span className="codicon codicon-error" aria-hidden="true" /> : null}
  </button></div>;
}
