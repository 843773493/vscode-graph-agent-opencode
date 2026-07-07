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
    title: "退出提示",
    description: "Show how to exit the web UI.",
  },
  {
    id: "new",
    command: "/new",
    title: "新建会话",
    description: "Start a new chat. 可输入名称。",
  },
  {
    id: "rename",
    command: "/rename",
    title: "命名会话",
    description: "Rename the current chat.",
  },
  {
    id: "init",
    command: "/init",
    title: "初始化 AGENTS.md",
    description: "暂未接入 Web 前端。",
    disabled: true,
  },
  {
    id: "clear",
    command: "/clear",
    title: "清空输入",
    description: "Clear the composer.",
  },
  {
    id: "copy",
    command: "/copy",
    title: "复制回复",
    description: "Copy the last assistant response.",
  },
  {
    id: "raw",
    command: "/raw",
    title: "事件视图",
    description: "Toggle raw trace event view.",
  },
  {
    id: "model",
    command: "/model",
    title: "打开 Agent 配置",
    description: "当前没有独立模型选择，模型由 Agent 配置决定。",
  },
  {
    id: "theme",
    command: "/theme",
    title: "切换主题",
    description: "暂未接入 Web 前端。",
    disabled: true,
  },
  {
    id: "default",
    command: "/default",
    title: "默认视图",
    description: "Show conversation timeline.",
  },
  {
    id: "events",
    command: "/events",
    title: "事件视图",
    description: "Show trace events.",
  },
  {
    id: "requests",
    command: "/requests",
    title: "请求视图",
    description: "查看当前会话历史 LLM 请求记录。",
  },
  {
    id: "resources",
    command: "/resources",
    title: "资源视图",
    description: "查看当前会话后台任务和快捷操作。",
  },
  {
    id: "state",
    command: "/state",
    title: "Agent 视图",
    description: "Show Agent State messages.",
  },
  {
    id: "view",
    command: "/view",
    title: "选择视图",
    description: "Open the view picker.",
  },
  {
    id: "compact",
    command: "/compact",
    title: "压缩上下文",
    description: "Compact the current session.",
  },
  {
    id: "agent",
    command: "/agent",
    title: "选择 Agent",
    description: "Open the agent picker.",
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
