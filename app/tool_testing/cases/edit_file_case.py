from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.prebuilt.tool_node import ToolRuntime

from app.agents.workspace_backend import build_workspace_backend
from app.agents.workspace_filesystem_tools import configure_workspace_filesystem_tools
from app.core.hashing import sha256_hex
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceNavigation,
)
from app.services.infrastructure.workspace_files.text_owner import (
    WorkspaceTextMutationOwner,
)
from app.tool_testing.definitions import PreparedToolTest, ToolTestEvaluation
from app.tool_testing.sandbox import ToolTestFilePaths


@dataclass(frozen=True, slots=True)
class EditFileScenarioCase:
    case_id: str
    title: str
    workspace_relative_path: str
    old_string: str
    new_string: str
    expected_content: str
    replace_all: bool = False
    tool_name: str = "edit_file"

    def prepare(
        self,
        *,
        workspace_root: Path,
        attempt_root: Path,
        asset_root: Path,
    ) -> PreparedToolTest:
        seed_directory = asset_root / "edit_file" / "exact_replacement"
        if not seed_directory.is_dir():
            raise FileNotFoundError(f"edit_file 测试资源不存在: {seed_directory}")

        fixture_file_path = seed_directory / self.workspace_relative_path
        if not fixture_file_path.is_file():
            raise FileNotFoundError(f"edit_file 测试文件不存在: {fixture_file_path}")
        file_paths = ToolTestFilePaths(workspace_root=workspace_root, attempt_root=attempt_root)
        shutil.copy2(seed_directory / "AGENTS.md", attempt_root / "AGENTS.md")
        attempt_file_path = attempt_root / self.workspace_relative_path
        attempt_file_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fixture_file_path, attempt_file_path)
        version = "sha256:" + sha256_hex(attempt_file_path.read_bytes())
        expected_arguments = {
            "file_path": self.workspace_relative_path,
            "expected_version": version,
            "old_string": self.old_string,
            "new_string": self.new_string,
            "replace_all": self.replace_all,
        }
        prompt = (
            f"必须只调用一次 edit_file 修改 {self.workspace_relative_path}。\n"
            "工具调用参数必须与下面的 JSON 对象完全一致；JSON 字符串中的转义字符"
            "需要按 JSON 规则解码为实际参数值：\n"
            f"{json.dumps(expected_arguments, ensure_ascii=False, indent=2)}\n"
            "不要把 Markdown 标记或额外分隔符放进参数，不要修改文件中的其他任何内容，"
            "也不要只在正文中描述操作。"
        )
        tool, runtime = _select_tool(file_paths)
        return PreparedToolTest(
            prompt=prompt,
            tool=tool,
            injected_arguments={"runtime": runtime},
        )

    def evaluate(
        self,
        *,
        attempt_root: Path,
        tool_result: object,
    ) -> ToolTestEvaluation:
        actual_content = (attempt_root / self.workspace_relative_path).read_text(
            encoding="utf-8"
        )
        if actual_content != self.expected_content:
            return ToolTestEvaluation(
                passed=False,
                detail=(
                    "文件内容不符合预期: "
                    f"expected={self.expected_content!r}, actual={actual_content!r}"
                ),
            )
        return ToolTestEvaluation(passed=True, detail=f"{self.title}结果正确")


def create_edit_file_cases() -> list[EditFileScenarioCase]:
    return [
        EditFileScenarioCase(
            case_id="edit_file_exact_multiline",
            title="多行精确替换",
            workspace_relative_path="target.txt",
            old_string='status = "draft"\nowner = "local-agent"',
            new_string='status = "ready"\nowner = "tool-test"',
            expected_content=(
                'project = "BoxTeam"\nstatus = "ready"\nowner = "tool-test"\n'
                'mode = "local"\n'
            ),
        ),
        EditFileScenarioCase(
            case_id="edit_file_unicode",
            title="Unicode 文本替换",
            workspace_relative_path="unicode.txt",
            old_string="你好，旧世界！",
            new_string="你好，新世界！",
            expected_content="标题：问候\n你好，新世界！\n状态：完成\n",
        ),
        EditFileScenarioCase(
            case_id="edit_file_path_with_spaces",
            title="含空格路径替换",
            workspace_relative_path="notes with spaces.txt",
            old_string="phase: planned",
            new_string="phase: shipped",
            expected_content="release: summer\nphase: shipped\nchannel: stable\n",
        ),
        EditFileScenarioCase(
            case_id="edit_file_preserve_indentation",
            title="保留缩进替换",
            workspace_relative_path="indentation.py",
            old_string="    if enabled:\n        return 1",
            new_string="    if enabled:\n        return 2",
            expected_content=(
                "def calculate(enabled: bool) -> int:\n"
                "    if enabled:\n"
                "        return 2\n"
                "    return 0\n"
            ),
        ),
        EditFileScenarioCase(
            case_id="edit_file_replace_all",
            title="全部重复项替换",
            workspace_relative_path="repeated.txt",
            old_string="ENV=staging",
            new_string="ENV=production",
            expected_content=(
                "service=api\nENV=production\nservice=worker\nENV=production\n"
                "service=scheduler\nENV=production\n"
            ),
            replace_all=True,
        ),
        EditFileScenarioCase(
            case_id="edit_file_delete_block",
            title="删除文本块",
            workspace_relative_path="delete_block.txt",
            old_string="\n[deprecated]\nenabled=true\n",
            new_string="",
            expected_content="[current]\nenabled=true\nowner=team\n",
        ),
        EditFileScenarioCase(
            case_id="edit_file_json_fragment",
            title="JSON 片段替换",
            workspace_relative_path="settings.json",
            old_string='  "theme": "light",\n  "autosave": false',
            new_string='  "theme": "dark",\n  "autosave": true',
            expected_content=(
                "{\n  \"name\": \"workspace\",\n  \"theme\": \"dark\",\n"
                "  \"autosave\": true\n}\n"
            ),
        ),
        EditFileScenarioCase(
            case_id="edit_file_markdown_symbols",
            title="Markdown 符号替换",
            workspace_relative_path="README.md",
            old_string="- [ ] run `uv sync`\n- [ ] run `pytest -q`",
            new_string="- [x] run `uv sync`\n- [x] run `pytest -q`",
            expected_content=(
                "# Checklist\n\n- [x] run `uv sync`\n- [x] run `pytest -q`\n\nDone.\n"
            ),
        ),
        EditFileScenarioCase(
            case_id="edit_file_tabs",
            title="Tab 缩进替换",
            workspace_relative_path="Makefile",
            old_string="build:\n\ttool compile --debug",
            new_string="build:\n\ttool compile --release",
            expected_content=(
                "prepare:\n\ttool prepare\n\nbuild:\n\ttool compile --release\n"
            ),
        ),
        EditFileScenarioCase(
            case_id="edit_file_punctuation",
            title="标点及转义字符替换",
            workspace_relative_path="punctuation.js",
            old_string='const pattern = "^foo\\\\d+$";',
            new_string='const pattern = "^bar\\\\d+$";',
            expected_content=(
                'export function matches(value) {\n  const pattern = "^bar\\\\d+$";\n'
                "  return new RegExp(pattern).test(value);\n}\n"
            ),
        ),
    ]


def _select_tool(file_paths: ToolTestFilePaths) -> tuple[BaseTool, ToolRuntime]:
    workspace_root, workspace_id = file_paths.workspace, file_paths.workspace_id
    middleware = FilesystemMiddleware(backend=build_workspace_backend(workspace_root))
    navigation = ResourceNavigation(
        workspace_root=workspace_root,
        workspace_id=workspace_id,
        file_paths=file_paths,
    )
    configure_workspace_filesystem_tools(
        middleware,
        workspace_root=workspace_root,
        resource_navigation=navigation,
        text_mutations=WorkspaceTextMutationOwner(paths=file_paths),
    )
    matches = [tool for tool in middleware.tools if tool.name == "edit_file"]
    if len(matches) != 1:
        raise RuntimeError(
            "FilesystemMiddleware 中 edit_file 工具数量异常: "
            f"expected=1, actual={len(matches)}"
        )
    tool = matches[0]
    if not isinstance(tool, StructuredTool):
        raise TypeError(
            "FilesystemMiddleware 的 edit_file 不是 StructuredTool: "
            f"actual={type(tool).__name__}"
        )
    if tool.func is None or tool.coroutine is None:
        raise RuntimeError("FilesystemMiddleware 的 edit_file 缺少同步或异步实现")

    runtime = ToolRuntime(
        state={},
        context=None,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="model-tool-test-edit-file",
        store=None,
        tools=[tool],
    )
    return tool, runtime
