from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.agents.tools.apply_patch import create_apply_patch_tool
from app.tool_testing.definitions import PreparedToolTest, ToolTestEvaluation


@dataclass(frozen=True, slots=True)
class ApplyPatchScenarioCase:
    case_id: str
    title: str
    prompt_template: str
    initial_files: dict[str, str]
    expected_files: dict[str, str | None]
    tool_name: str = "apply_patch"

    def prepare(
        self,
        *,
        workspace_root: Path,
        attempt_root: Path,
        asset_root: Path,
    ) -> PreparedToolTest:
        del workspace_root, asset_root
        attempt_root.mkdir(parents=True)
        for relative_path, content in self.initial_files.items():
            target = attempt_root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        return PreparedToolTest(
            prompt=self.prompt_template,
            tool=create_apply_patch_tool(workspace_root=attempt_root),
        )

    def evaluate(
        self,
        *,
        attempt_root: Path,
        tool_result: object,
    ) -> ToolTestEvaluation:
        del tool_result
        for relative_path, expected_content in self.expected_files.items():
            target = attempt_root / relative_path
            if expected_content is None:
                if target.exists():
                    return ToolTestEvaluation(
                        passed=False,
                        detail=f"预期文件已删除，但仍然存在: {relative_path}",
                    )
                continue
            if not target.is_file():
                return ToolTestEvaluation(
                    passed=False,
                    detail=f"预期文件不存在: {relative_path}",
                )
            actual_content = target.read_text(encoding="utf-8")
            if _without_optional_final_newline(actual_content) != (
                _without_optional_final_newline(expected_content)
            ):
                return ToolTestEvaluation(
                    passed=False,
                    detail=(
                        f"文件内容不符合预期: path={relative_path}, "
                        f"expected={expected_content!r}, actual={actual_content!r}"
                    ),
                )
        return ToolTestEvaluation(passed=True, detail=f"{self.title}结果正确")


def create_apply_patch_cases() -> list[ApplyPatchScenarioCase]:
    return [
        ApplyPatchScenarioCase(
            case_id="apply_patch_update_single_file",
            title="更新单个文件",
            prompt_template=(
                "必须真实调用一次 apply_patch，修改工作区相对路径 target.txt。"
                "文件当前是 alpha、beta 两行；把 beta 改成 gamma，并在后面新增 delta。"
                "路径不能以 / 开头，不要调用其它工具。"
            ),
            initial_files={"target.txt": "alpha\nbeta\n"},
            expected_files={"target.txt": "alpha\ngamma\ndelta\n"},
        ),
        ApplyPatchScenarioCase(
            case_id="apply_patch_add_nested_file",
            title="新增嵌套文件",
            prompt_template=(
                "必须真实调用一次 apply_patch，新增工作区相对路径 src/generated.py。"
                "内容精确为两行：def answer(): 和四个空格缩进的 return 42。"
                "使用 Add File，路径不能以 / 开头。"
            ),
            initial_files={},
            expected_files={"src/generated.py": "def answer():\n    return 42\n"},
        ),
        ApplyPatchScenarioCase(
            case_id="apply_patch_delete_file",
            title="删除文件",
            prompt_template=(
                "必须真实调用一次 apply_patch，删除工作区相对路径 obsolete.txt。"
                "使用 Delete File，路径不能以 / 开头。"
            ),
            initial_files={"obsolete.txt": "obsolete\n"},
            expected_files={"obsolete.txt": None},
        ),
        ApplyPatchScenarioCase(
            case_id="apply_patch_move_and_update",
            title="移动并更新文件",
            prompt_template=(
                "必须真实调用一次 apply_patch，把 source.py 移动到 renamed.py，"
                "同时把 return \"old\" 改成 return \"new\"。必须使用 Move to，所有路径都是"
                "工作区相对路径且不能以 / 开头。"
            ),
            initial_files={"source.py": "def value():\n    return \"old\"\n"},
            expected_files={
                "source.py": None,
                "renamed.py": "def value():\n    return \"new\"\n",
            },
        ),
        ApplyPatchScenarioCase(
            case_id="apply_patch_multi_file_transaction",
            title="多文件事务",
            prompt_template=(
                "只调用一次 apply_patch，在同一个补丁中完成三个操作：把 config.txt "
                "中的 enabled=false 改成 enabled=true；删除 legacy.txt；新增 "
                "notes.txt，内容为 migrated。路径不能以 / 开头。"
            ),
            initial_files={
                "config.txt": "name=boxteam\nenabled=false\n",
                "legacy.txt": "remove me\n",
            },
            expected_files={
                "config.txt": "name=boxteam\nenabled=true\n",
                "legacy.txt": None,
                "notes.txt": "migrated\n",
            },
        ),
        ApplyPatchScenarioCase(
            case_id="apply_patch_repeated_context_hints",
            title="重复上下文提示定位",
            prompt_template=(
                "必须真实调用一次 apply_patch 修改 classes.py，只把 class Second 的 value "
                "返回值从 same 改成 changed，class First 保持不变。使用 @@ class Second: 和 "
                "@@     def value(self): 两级提示定位，路径不能以 / 开头。"
            ),
            initial_files={
                "classes.py": (
                    "class First:\n"
                    "    def value(self):\n"
                    "        return \"same\"\n\n"
                    "class Second:\n"
                    "    def value(self):\n"
                    "        return \"same\"\n"
                )
            },
            expected_files={
                "classes.py": (
                    "class First:\n"
                    "    def value(self):\n"
                    "        return \"same\"\n\n"
                    "class Second:\n"
                    "    def value(self):\n"
                    "        return \"changed\"\n"
                )
            },
        ),
        ApplyPatchScenarioCase(
            case_id="apply_patch_multiple_hunks",
            title="同文件多段修改",
            prompt_template=(
                "必须真实调用一次 apply_patch 修改 document.txt，用两个 @@ hunk："
                "把 title=draft 改成 title=release，把 status=pending 改成 status=ready；"
                "中间内容保持不变，路径不能以 / 开头。"
            ),
            initial_files={
                "document.txt": "title=draft\nowner=team\nversion=1\nstatus=pending\n"
            },
            expected_files={
                "document.txt": "title=release\nowner=team\nversion=1\nstatus=ready\n"
            },
        ),
        ApplyPatchScenarioCase(
            case_id="apply_patch_unicode_content",
            title="更新 Unicode 内容",
            prompt_template=(
                "必须真实调用一次 apply_patch 修改 greeting.txt，把精确文本“你好，旧世界”"
                "改为“你好，新世界”，路径不能以 / 开头。"
            ),
            initial_files={"greeting.txt": "你好，旧世界\n"},
            expected_files={"greeting.txt": "你好，新世界\n"},
        ),
        ApplyPatchScenarioCase(
            case_id="apply_patch_path_with_spaces",
            title="修改含空格的文件路径",
            prompt_template=(
                "必须真实调用一次 apply_patch 修改工作区相对路径 "
                "docs/release notes.txt，把 status=draft 改成 status=final。"
                "文件路径包含空格且不能以 / 开头。"
            ),
            initial_files={"docs/release notes.txt": "version=1\nstatus=draft\n"},
            expected_files={"docs/release notes.txt": "version=1\nstatus=final\n"},
        ),
        ApplyPatchScenarioCase(
            case_id="apply_patch_preserve_indentation",
            title="保持 Python 缩进",
            prompt_template=(
                "必须真实调用一次 apply_patch 修改 service.py，在 run 方法中把 result = 1 "
                "改成 result = 2，并在下一行新增同级缩进的 return result。"
                "保持四空格缩进，路径不能以 / 开头。"
            ),
            initial_files={
                "service.py": "class Service:\n    def run(self):\n        result = 1\n"
            },
            expected_files={
                "service.py": (
                    "class Service:\n"
                    "    def run(self):\n"
                    "        result = 2\n"
                    "        return result\n"
                )
            },
        ),
    ]


def _without_optional_final_newline(content: str) -> str:
    return content.removesuffix("\n")
