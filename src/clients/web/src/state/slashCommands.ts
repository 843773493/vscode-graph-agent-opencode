import { t } from "../../../shared/core/i18n/index.js";
export interface SlashCommandOption {
  id: string;
  command: string;
  title: string;
  description: string;
  disabled?: boolean;
}

export const COMPOSER_SLASH_COMMANDS: SlashCommandOption[] = [
  {
    id: "quit",
    command: "/quit",
    title: t("slash.quit.title"),
    description: t("slash.quit.description"),
  },
  {
    id: "new",
    command: "/new",
    title: t("slash.new.title"),
    description: t("slash.new.description"),
  },
  {
    id: "rename",
    command: "/rename",
    title: t("slash.rename.title"),
    description: t("slash.rename.description"),
  },
  {
    id: "init",
    command: "/init",
    title: t("slash.init.title"),
    description: t("slash.init.description"),
    disabled: true,
  },
  {
    id: "clear",
    command: "/clear",
    title: t("slash.clear.title"),
    description: t("slash.clear.description"),
  },
  {
    id: "copy",
    command: "/copy",
    title: t("slash.copy.title"),
    description: t("slash.copy.description"),
  },
  {
    id: "raw",
    command: "/raw",
    title: t("slash.raw.title"),
    description: t("slash.raw.description"),
  },
  {
    id: "model",
    command: "/model",
    title: t("slash.model.title"),
    description: t("slash.model.description"),
  },
  {
    id: "theme",
    command: "/theme",
    title: t("slash.theme.title"),
    description: t("slash.theme.description"),
    disabled: true,
  },
  {
    id: "default",
    command: "/default",
    title: t("slash.default.title"),
    description: t("slash.default.description"),
  },
  {
    id: "events",
    command: "/events",
    title: t("slash.events.title"),
    description: t("slash.events.description"),
  },
  {
    id: "requests",
    command: "/requests",
    title: t("slash.requests.title"),
    description: t("slash.requests.description"),
  },
  {
    id: "resources",
    command: "/resources",
    title: t("slash.resources.title"),
    description: t("slash.resources.description"),
  },
  {
    id: "changes",
    command: "/changes",
    title: t("slash.changes.title"),
    description: t("slash.changes.description"),
  },
  {
    id: "state",
    command: "/state",
    title: t("slash.state.title"),
    description: t("slash.state.description"),
  },
  {
    id: "view",
    command: "/view",
    title: t("slash.view.title"),
    description: t("slash.view.description"),
  },
  {
    id: "compact",
    command: "/compact",
    title: t("slash.compact.title"),
    description: t("slash.compact.description"),
  },
  {
    id: "goal",
    command: "/goal",
    title: t("slash.goal.title"),
    description: t("slash.goal.description"),
  },
  {
    id: "skill-untrack",
    command: "/skill-untrack",
    title: t("slash.skill-untrack.title"),
    description: t("slash.skill-untrack.description"),
  },
  {
    id: "agent",
    command: "/agent",
    title: t("slash.agent.title"),
    description: t("slash.agent.description"),
  },
];

export function slashQueryFromInput(inputValue: string): string | null {
  if (!inputValue.startsWith("/")) {
    return null;
  }
  const commandToken = inputValue.slice(1).match(/^\S*/)?.[0] ?? "";
  return commandToken.toLowerCase();
}

export function matchingSlashCommands(
  commands: SlashCommandOption[],
  query: string | null,
): SlashCommandOption[] {
  if (query === null) {
    return [];
  }
  return commands.filter((command) => command.command.slice(1).startsWith(query));
}

export function nextEnabledSlashCommandIndex(
  commands: SlashCommandOption[],
  currentIndex: number,
  direction: 1 | -1,
): number {
  if (commands.length === 0) {
    return 0;
  }
  for (let offset = 1; offset <= commands.length; offset += 1) {
    const nextIndex =
      (currentIndex + direction * offset + commands.length) % commands.length;
    if (!commands[nextIndex]?.disabled) {
      return nextIndex;
    }
  }
  return 0;
}

export function firstEnabledSlashCommandIndex(commands: SlashCommandOption[]): number {
  const index = commands.findIndex((command) => !command.disabled);
  return index === -1 ? 0 : index;
}

export function getSlashCommandArgs(inputValue: string, command: string): string {
  const trimmedInput = inputValue.trim();
  if (!trimmedInput.startsWith(command)) {
    return "";
  }

  const nextChar = trimmedInput.charAt(command.length);
  if (nextChar && !/\s/.test(nextChar)) {
    return "";
  }

  return trimmedInput.slice(command.length).trim();
}
