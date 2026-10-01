"""RolloutStorage 注解可解析性回归合同。

背景：storage/service.py 的 `_append_v2_records_transaction` 曾引用未导入的
`CanonicalItemRecord`。由于模块带 `from __future__ import annotations`，注解只在
运行期惰性求值，正常调用不报错，但任何 `typing.get_type_hints` 解析入口
（FastAPI/Pydantic/文档生成/序列化框架的常见反射点）都会抛 `NameError`。
本测试锁定本模块全部注解都能解析，防止同类悬空注解再次混入。
"""

from __future__ import annotations

import inspect
import typing

import app.services.infrastructure.rollout_context.storage.service as storage_service


def _module_callables() -> list[object]:
    """收集本模块自身定义的所有函数与方法（不含导入进来的外部对象）。"""
    module = storage_service
    items: list[object] = []
    for obj in vars(module).values():
        if inspect.isfunction(obj) and obj.__module__ == module.__name__:
            items.append(obj)
        elif inspect.isclass(obj) and obj.__module__ == module.__name__:
            for member in vars(obj).values():
                if inspect.isfunction(member):
                    items.append(member)
    return items


def test_storage_service_module_annotations_all_resolve() -> None:
    """本模块函数/方法的注解必须全部可在运行期解析，不允许悬空名字。"""
    module = storage_service
    assert _module_callables(), "未采集到任何函数，测试覆盖断言失效"
    failures: list[str] = []
    for obj in _module_callables():
        try:
            typing.get_type_hints(obj, globalns=vars(module))
        except NameError as exc:
            failures.append(f"{obj.__qualname__}: {exc}")
    assert failures == [], f"存在无法解析的注解: {failures}"


