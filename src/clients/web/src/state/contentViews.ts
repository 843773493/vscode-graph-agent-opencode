import { t } from "../../../shared/core/i18n/index.js";
import type { ConversationContentView } from "../types/frontend";

export type ViewOption = {
  id: ConversationContentView;
  label: string;
  description: string;
};

export const VIEW_OPTIONS: ViewOption[] = [
  {
    id: "default",
    label: t("views.default"),
    description: t("views.defaultDescription"),
  },
  {
    id: "events",
    label: t("views.events"),
    description: t("views.eventsDescription"),
  },
  {
    id: "requests",
    label: t("views.requests"),
    description: t("views.requestsDescription"),
  },
  {
    id: "changes",
    label: t("views.changes"),
    description: t("views.changesDescription"),
  },
  {
    id: "agent",
    label: t("views.agent"),
    description: t("views.agentDescription"),
  },
  // TODO: 后续添加更多视图时，在这里扩展菜单项并接入对应面板。
];

export const COMPOSER_VIEW_OPTIONS = VIEW_OPTIONS.filter(
  (option) => option.id !== "changes",
);
