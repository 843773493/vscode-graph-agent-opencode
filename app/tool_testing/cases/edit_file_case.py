from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil

from deepagents.backends import LocalShellBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.prebuilt.tool_node import ToolRuntime

from app.tool_testing.definitions import PreparedToolTest, ToolTestEvaluation


@dataclass(frozen=True, slots=True)
class EditFileScenarioCase:
    case_id: str
    title: str
    file_name: str
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
        source = asset_root / "edit_file" / "exact_replacement"
        if not source.is_dir():
            raise FileNotFoundError(f"edit_file 测试资源不存在: {source}")
        shutil.copytree(source, attempt_root)

        target = attempt_root / self.file_name
        if not target.is_file():
            raise FileNotFoundError(f"edit_file 测试文件不存在: {target}")
        virtual_path = "/" + target.relative_to(workspace_root).as_posix()
        expected_arguments = {
            "file_path": virtual_path,
            "old_string": self.old_string,
            "new_string": self.new_string,
            "replace_all": self.replace_all,
        }
        prompt = (
            f"必须只调用一次 edit_file 修改 {virtual_path}。\n"
            "工具调用参数必须与下面的 JSON 对象完全一致；JSON 字符串中的转义字符"
            "需要按 JSON 规则解码为实际参数值：\n"
            f"{json.dumps(expected_arguments, ensure_ascii=False, indent=2)}\n"
            "不要把 Markdown 标记或额外分隔符放进参数，不要修改文件中的其他任何内容，"
            "也不要只在正文中描述操作。"
        )
        tool, runtime = _select_tool(workspace_root)
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
        actual = (attempt_root / self.file_name).read_text(encoding="utf-8")
        if actual != self.expected_content:
            return ToolTestEvaluation(
                passed=False,
                detail=(
                    "文件内容不符合预期: "
                    f"expected={self.expected_content!r}, actual={actual!r}"
                ),
            )
        return ToolTestEvaluation(passed=True, detail=f"{self.title}结果正确")


def create_edit_file_cases() -> list[EditFileScenarioCase]:
    return [
        EditFileScenarioCase(
            case_id="edit_file_exact_multiline",
            title="多行精确替换",
            file_name="target.txt",
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
            file_name="unicode.txt",
            old_string="你好，旧世界！",
            new_string="你好，新世界！",
            expected_content="标题：问候\n你好，新世界！\n状态：完成\n",
        ),
        EditFileScenarioCase(
            case_id="edit_file_path_with_spaces",
            title="含空格路径替换",
            file_name="notes with spaces.txt",
            old_string="phase: planned",
            new_string="phase: shipped",
            expected_content="release: summer\nphase: shipped\nchannel: stable\n",
        ),
        EditFileScenarioCase(
            case_id="edit_file_preserve_indentation",
            title="保留缩进替换",
            file_name="indentation.py",
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
            file_name="repeated.txt",
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
            file_name="delete_block.txt",
            old_string="\n[deprecated]\nenabled=true\n",
            new_string="",
            expected_content="[current]\nenabled=true\nowner=team\n",
        ),
        EditFileScenarioCase(
            case_id="edit_file_json_fragment",
            title="JSON 片段替换",
            file_name="settings.json",
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
            file_name="README.md",
            old_string="- [ ] run `uv sync`\n- [ ] run `pytest -q`",
            new_string="- [x] run `uv sync`\n- [x] run `pytest -q`",
            expected_content=(
                "# Checklist\n\n- [x] run `uv sync`\n- [x] run `pytest -q`\n\nDone.\n"
            ),
        ),
        EditFileScenarioCase(
            case_id="edit_file_tabs",
            title="Tab 缩进替换",
            file_name="Makefile",
            old_string="build:\n\ttool compile --debug",
            new_string="build:\n\ttool compile --release",
            expected_content=(
                "prepare:\n\ttool prepare\n\nbuild:\n\ttool compile --release\n"
            ),
        ),
        EditFileScenarioCase(
            case_id="edit_file_punctuation",
            title="标点及转义字符替换",
            file_name="punctuation.js",
            old_string='const pattern = "^foo\\\\d+$";',
            new_string='const pattern = "^bar\\\\d+$";',
            expected_content=(
                'export function matches(value) {\n  const pattern = "^bar\\\\d+$";\n'
                "  return new RegExp(pattern).test(value);\n}\n"
            ),
        ),
    ]


def _select_tool(workspace_root: Path) -> tuple[BaseTool, ToolRuntime]:
    backend = LocalShellBackend(root_dir=workspace_root, virtual_mode=True)
    middleware = FilesystemMiddleware(backend=backend)
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
