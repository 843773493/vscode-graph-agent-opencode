import { normalizeHttpUrl } from "./url.js";

export const TERMINAL_COMMAND_HELP = [
  "help",
  "goto <url>",
  "url",
  "title",
  "reload",
  "back",
  "forward",
  "stop",
  "viewport <width>x<height>",
  "click <x> <y>",
  "type <text>",
  "press <key>",
  "exit",
];

function splitCommand(line) {
  const trimmed = line.trim();
  if (!trimmed) {
    return { command: "noop", rest: "" };
  }

  const firstSpace = trimmed.search(/\s/);
  if (firstSpace === -1) {
    return { command: trimmed.toLowerCase(), rest: "" };
  }
  return {
    command: trimmed.slice(0, firstSpace).toLowerCase(),
    rest: trimmed.slice(firstSpace).trim(),
  };
}

function parseCoordinatePair(rest) {
  const parts = rest.split(/\s+/);
  if (parts.length !== 2) {
    throw new Error("坐标命令需要两个数字参数");
  }
  const x = Number(parts[0]);
  const y = Number(parts[1]);
  if (!Number.isFinite(x) || !Number.isFinite(y)) {
    throw new Error(`非法坐标: ${rest}`);
  }
  return { x, y };
}

function parseViewport(rest) {
  const normalized = rest.replace(/\s+/g, " ").trim();
  const match = normalized.match(/^(\d+)\s*(?:x|\s)\s*(\d+)$/i);
  if (!match) {
    throw new Error("viewport 格式必须是 <width>x<height>");
  }
  const width = Number(match[1]);
  const height = Number(match[2]);
  if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) {
    throw new Error(`非法 viewport: ${rest}`);
  }
  return { width, height };
}

export function parseTerminalCommand(line) {
  const { command, rest } = splitCommand(line);
  if (command === "noop") {
    return { name: "noop" };
  }
  if (command === "help" || command === "?") {
    return { name: "help" };
  }
  if (command === "goto" || command === "open") {
    return { name: "goto", url: normalizeHttpUrl(rest) };
  }
  if (command === "url") {
    return { name: "url" };
  }
  if (command === "title") {
    return { name: "title" };
  }
  if (command === "reload") {
    return { name: "reload" };
  }
  if (command === "back") {
    return { name: "back" };
  }
  if (command === "forward") {
    return { name: "forward" };
  }
  if (command === "stop") {
    return { name: "stop" };
  }
  if (command === "viewport") {
    return { name: "viewport", ...parseViewport(rest) };
  }
  if (command === "click") {
    return { name: "click", ...parseCoordinatePair(rest) };
  }
  if (command === "type") {
    if (!rest) {
      throw new Error("type 命令需要文本参数");
    }
    return { name: "type", text: rest };
  }
  if (command === "press") {
    if (!rest) {
      throw new Error("press 命令需要按键名称");
    }
    return { name: "press", key: rest };
  }
  if (command === "exit" || command === "quit") {
    return { name: "exit" };
  }
  throw new Error(`未知终端命令: ${command}`);
}

export async function executeBrowserCommand(session, command) {
  if (command.name === "noop") {
    return "";
  }
  if (command.name === "help") {
    return TERMINAL_COMMAND_HELP.join("\n");
  }
  if (command.name === "goto") {
    const state = await session.goto(command.url);
    return `已进入 ${state.url}`;
  }
  if (command.name === "url") {
    const state = await session.currentState();
    return state.url;
  }
  if (command.name === "title") {
    const state = await session.currentState();
    return state.title || "(空标题)";
  }
  if (command.name === "reload") {
    const state = await session.reload();
    return `已刷新 ${state.url}`;
  }
  if (command.name === "back") {
    const state = await session.back();
    return `已后退到 ${state.url}`;
  }
  if (command.name === "forward") {
    const state = await session.forward();
    return `已前进到 ${state.url}`;
  }
  if (command.name === "stop") {
    const state = await session.stopLoading();
    return `已停止加载 ${state.url}`;
  }
  if (command.name === "viewport") {
    const state = await session.setViewport(command.width, command.height);
    return `viewport=${state.viewport.width}x${state.viewport.height}`;
  }
  if (command.name === "click") {
    await session.click(command.x, command.y);
    return `已点击 ${command.x},${command.y}`;
  }
  if (command.name === "type") {
    await session.typeText(command.text);
    return `已输入 ${command.text.length} 个字符`;
  }
  if (command.name === "press") {
    await session.pressKey(command.key);
    return `已按下 ${command.key}`;
  }
  if (command.name === "exit") {
    return "exit";
  }
  throw new Error(`未实现终端命令: ${command.name}`);
}
