import assert from "node:assert/strict";
import test from "node:test";

import { lastNonEmptyInputLine, latestCommandExitCode, stripCommandMarkerLines } from "./commandText.js";

test("退出码取最后完整匹配，尾部残缺或非法码不覆盖已有结果", () => {
  const marker = "__BOXTEAM_CMD_DONE_abc__";
  const cases = [
    ["", null],
    [`正文🙂\r\n${marker}:`, null],
    [`${marker}:-1\n${marker}:oops`, null],
    [`${marker}:0\r\n${marker}:17\r${marker}:`, 17],
    [`${marker}:17\n${marker}:-2\n${marker}:no`, 17],
    [`${marker}:17\n${marker}:000`, 0],
  ];
  for (const [input, expected] of cases) assert.equal(latestCommandExitCode(input), expected);
});

test("指定命令隔离其他完成标记，恢复读取可取不同命令的最后退出码", () => {
  const input = "__BOXTEAM_CMD_DONE_abc__:7\n__BOXTEAM_CMD_DONE_def__:9\n";
  assert.equal(latestCommandExitCode(input, "__BOXTEAM_CMD_DONE_abc__"), 7);
  assert.equal(latestCommandExitCode(input, "__BOXTEAM_CMD_DONE_fff__"), null);
  assert.equal(latestCommandExitCode(input), 9);
});

test("退出码解析保留ANSI与换行处理，显式标记按字面量匹配", () => {
  const marker = "__BOXTEAM_CMD_DONE_a.b+[]()__";
  const input = `标题\x1b]window\x07\r\n\x1b[31m${marker}\x1b[0m:12\r\x1b]other\x1b\\${marker}:23\n__BOXTEAM_CMD_DONE_axbbb__:99`;
  assert.equal(latestCommandExitCode(input, marker), 23);
  assert.equal(latestCommandExitCode("__BOXTEAM_CMD_DONE_axbbb__:99", marker), null);
});

test("输入标签取最后非空CR或LF行，保留行内空白、ANSI及Unicode正文", () => {
  const cases = [
    ["", null],
    [" \t\v\r\n\u00a0\u2028\ufeff", null],
    ["  echo 你好🙂  \r\n", "echo 你好🙂"],
    ["旧\n\n  新  \n\t\n", "新"],
    ["旧\r  新  \r\u00a0", "新"],
    ["旧\r\n新\r末尾\n \t", "末尾"],
    ["\ufeff  首\u00a0中\t尾  \u2028", "首\u00a0中\t尾"],
    ["首\u2028尾\v中\u2029末", "首\u2028尾\v中\u2029末"],
    ["  \x1b[31m红色\x1b[0m  \r\n", "\x1b[31m红色\x1b[0m"],
    ["旧\r\n\ud800\udfff\ud800\0\t", "\ud800\udfff\ud800\0"],
  ];
  for (const [input, expected] of cases) assert.equal(lastNonEmptyInputLine(input), expected);
});

test("无命令标记时保留空正文、混合换行、ANSI与孤立代理项", () => {
  for (const value of ["", "原文😀\r更新\r\n\x1b[31m红色\x1b[0m\n\ud800\udfff\ud800", "x\n".repeat(131072)]) {
    assert.equal(stripCommandMarkerLines(value), value);
  }
});

test("标记所在整行连同各自换行一起删除，其余正文不归一化", () => {
  const input = "保留\r\n提示 __BOXTEAM_CMD_START_abc__ 尾部\r\n\x1b[32m正文😀\x1b[0m\n__BOXTEAM_CMD_DONE_abc__:7\n最后";
  assert.equal(stripCommandMarkerLines(input), "保留\r\n\x1b[32m正文😀\x1b[0m\n最后");
});

test("开头、结尾和连续标记行不产生空白行或吞掉相邻行", () => {
  assert.equal(stripCommandMarkerLines("__BOXTEAM_CMD_START_a__\n正文\n__BOXTEAM_CMD_DONE_a__:0"), "正文\n");
  assert.equal(stripCommandMarkerLines("__BOXTEAM_CMD_START_a__\r\n__BOXTEAM_CMD_DONE_a__:0\n"), "");
  assert.equal(stripCommandMarkerLines("前\n标记 __BOXTEAM_CMD_DONE_a__:0"), "前\n");
});

test("同一行多个标记只删除该行，保留后续所有普通行", () => {
  const input = "前\n__BOXTEAM_CMD_START_a__ __BOXTEAM_CMD_DONE_a__:0\r\n后\r\n";
  assert.equal(stripCommandMarkerLines(input), "前\n后\r\n");
});

test("单独CR不是分行边界，含标记的整条LF行全部删除", () => {
  assert.equal(stripCommandMarkerLines("前\n覆盖前\r__BOXTEAM_CMD_DONE_a__:0\r覆盖后\n尾\r"), "前\n尾\r");
});

test("展示过滤识别已出现的协议前缀，不要求完整ID，其他相似文本保留", () => {
  const input = "__BOXTEAM_CMD_START_\n__BOXTEAM_CMD_DONE_\r\n__BOXTEAM_CMD_START\n__BOXTEAM_CMD_DONE\n__BOXTEAM_CMD_STATUS_a__\n";
  assert.equal(stripCommandMarkerLines(input), "__BOXTEAM_CMD_START\n__BOXTEAM_CMD_DONE\n__BOXTEAM_CMD_STATUS_a__\n");
});

test("大量普通行夹少量标记时正文及每行分隔符精确保留", () => {
  const before = "普通正文😀\r\n".repeat(8000);
  const after = "\x1b[33m另一行\x1b[0m\n".repeat(8000);
  const input = `${before}提示 __BOXTEAM_CMD_START_abc__\r\n${after}__BOXTEAM_CMD_DONE_abc__:0`;
  assert.equal(stripCommandMarkerLines(input), before + after);
});
