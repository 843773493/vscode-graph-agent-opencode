import { describe, expect, test } from "bun:test";

import { HttpRequestError } from "../api/http";
import { errorDisplayMessage, errorMessage } from "./errorMessage";

function httpError(status: number, statusText: string, detail: unknown): HttpRequestError {
  return new HttpRequestError(status, statusText, detail, "/api/v1/workspace/files");
}

describe("errorDisplayMessage 按状态码补可执行提示", () => {
  test("403 前置权限提示并保留原始异常文本", () => {
    const text = errorDisplayMessage(
      httpError(403, "Forbidden", "文件树路径无访问权限: /root/secret"),
    );
    expect(text).toContain("没有访问权限");
    expect(text).toContain("请求失败 403 Forbidden");
    expect(text).toContain("文件树路径无访问权限: /root/secret");
  });

  test("404 前置不存在提示并保留原始异常文本", () => {
    const text = errorDisplayMessage(
      httpError(404, "Not Found", "文件树路径不存在: src/gone"),
    );
    expect(text).toContain("目标不存在");
    expect(text).toContain("请求失败 404 Not Found");
    expect(text).toContain("文件树路径不存在: src/gone");
  });

  test("未覆盖的状态码不臆造提示，退化为原始异常文本", () => {
    const error = httpError(507, "Insufficient Storage", "磁盘已满");
    expect(errorDisplayMessage(error)).toBe(errorMessage(error));
    expect(errorDisplayMessage(error)).not.toContain("没有访问权限");
  });

  test("401 前置可执行的重新登录提示并保留原始异常文本", () => {
    const text = errorDisplayMessage(
      httpError(401, "Unauthorized", "user_session_required"),
    );
    expect(text).toContain("登录");
    expect(text).toContain("请求失败 401 Unauthorized");
    expect(text).toContain("user_session_required");
  });

  test("非 HttpRequestError 不带状态码，退化为原始异常文本", () => {
    expect(errorDisplayMessage(new Error("剪贴板不可用"))).toBe("剪贴板不可用");
    expect(errorDisplayMessage("裸字符串错误")).toBe("裸字符串错误");
  });
});
