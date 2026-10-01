"""非 canonical 身份豁免枚举与边界机械核对（§7.1c、§7.2）。

本 capability 只把「唯一 id 工厂产出、进入 canonical 校验器命名空间的
session/thread 身份」约束为 UUIDv7 位 profile。§7.1 已声明两类**非 canonical
身份**显式豁免、允许继续使用 UUIDv4：

1. Node 服务进程（`BOXTEAM_NODE_BIN`，无 `crypto.randomUUIDv7`）生成的
   `term_`/`browser_`/`screenshot_`/`download_`/`page_`/`preset_`、以及浏览器
   （无 `Bun.randomUUIDv7`）生成的 `inline:` 附件 file id；
2. **运行时非工厂前缀** `runtime_lease_<uuid4>` 与 `target_generation_<uuid4>`
   （§7.1c）：二者经实测**不是** `IdentifierPrefix` 工厂前缀，MUST NOT 被当作
   v4 残留误列入迁移面。

本文件做机械核对：豁免项 MUST 能对应到具名生成点（Node 服务 / 浏览器 /
gateway 运行时），且 canonical 校验器 MUST 拒绝它们作为 session/thread 身份
（§7.2）。豁免 MUST NOT 扩张到 canonical id 面。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import get_args
from uuid import uuid4

import pytest

from app.core.identifier import IdentifierPrefix
from app.core.session_catalog_store import validate_session_id, validate_thread_id


@dataclass(frozen=True, slots=True)
class Exemption:
    """一条非 canonical 身份豁免：命名载体 + 生成点 + 样例 id 形态。"""

    kind: str
    prefix: str
    carrier: str
    marker: str


# Node 服务进程（无 crypto.randomUUIDv7）与浏览器（无 Bun.randomUUIDv7）
# 生成的本地 id（§7.1）。每条含具名载体与 §7.1 声明的「非 canonical」注释锚点。
_JS_EXEMPTIONS: tuple[Exemption, ...] = (
    Exemption("node-terminal", "term_", "src/workspace-services/terminal/server/terminalManager.js", "非 canonical"),
    Exemption("node-browser", "browser_", "src/workspace-services/browser/server/browserManager.js", "非 canonical"),
    Exemption("node-browser", "screenshot_", "src/workspace-services/browser/server/browserStateStore.js", "非 canonical"),
    Exemption("node-browser", "download_", "src/workspace-services/browser/server/browserStateStore.js", "非 canonical"),
    Exemption("node-browser", "page_", "src/workspace-services/browser/server/browserSession.js", "非 canonical"),
    Exemption("node-browser", "preset_", "src/workspace-services/browser/server/browserSession.js", "非 canonical"),
    Exemption("browser", "inline:", "src/clients/web/src/utils/media/mediaAttachments.ts", "非 canonical"),
)

# 运行时非工厂前缀（§7.1c）：实测不是 IdentifierPrefix 工厂前缀。
_RUNTIME_EXEMPTIONS: tuple[Exemption, ...] = (
    Exemption("runtime-non-factory", "runtime_lease_", "app/gateway/registry/crud.py", "uuid4().hex"),
    Exemption("runtime-non-factory", "target_generation_", "app/gateway/registry/crud.py", "uuid4().hex"),
)


def _repo_root() -> Path:
    """从运行时工作目录定位仓库根（AGENTS.md：基于 cwd，不用 parents 上溯）。"""
    root = Path.cwd()
    assert (root / "app").is_dir(), f"必须从仓库根运行: {root}"
    return root


def _sample_id(prefix: str) -> str:
    """按豁免前缀构造一个 UUIDv4 位 profile 的非 canonical 样例 id。"""
    payload = uuid4().hex
    if prefix == "inline:":
        return f"inline:{uuid4()}:note.png"
    return f"{prefix}{payload}"


# ----------------------------------------------------------------------
# §7.1c 运行时非工厂前缀豁免枚举
# ----------------------------------------------------------------------


@pytest.mark.parametrize("exemption", _RUNTIME_EXEMPTIONS, ids=lambda e: e.prefix)
def test_runtime_non_factory_prefix_is_not_a_factory_prefix(exemption: Exemption) -> None:
    """§7.1c：这两个运行时前缀 MUST NOT 出现在 IdentifierPrefix 工厂前缀全集。"""
    prefixes = set(get_args(IdentifierPrefix))
    assert exemption.prefix not in prefixes
    # 去掉尾随下划线后也不是工厂前缀（防「带回车/变体绕过」）。
    assert exemption.prefix.rstrip("_-") not in prefixes


@pytest.mark.parametrize("exemption", _RUNTIME_EXEMPTIONS, ids=lambda e: e.prefix)
def test_runtime_exemption_has_named_generation_site(exemption: Exemption) -> None:
    """§7.1c：豁免项 MUST 给出具名载体与生成点；二者由 UUIDv4 直接拼装，
    不经过唯一 id 工厂（故不属于 canonical 面，也 MUST NOT 误入迁移面）。"""
    source = (_repo_root() / exemption.carrier).read_text(encoding="utf-8")
    assert f"{exemption.prefix}{{uuid4().hex}}" in source, (
        f"{exemption.carrier} 未见 {exemption.prefix} 的 uuid4 生成点"
    )
    assert exemption.marker in source


def test_exemption_enumeration_covers_all_runtime_prefixes() -> None:
    """§7.1c：豁免枚举 MUST 显式登记这两者（防漏登记或改名后失联）。"""
    registered = {item.prefix for item in _RUNTIME_EXEMPTIONS}
    assert registered == {"runtime_lease_", "target_generation_"}


# ----------------------------------------------------------------------
# §7.1 JS/Node/浏览器豁免的具名生成点核对
# ----------------------------------------------------------------------


@pytest.mark.parametrize("exemption", _JS_EXEMPTIONS, ids=lambda e: e.prefix)
def test_js_exemption_has_named_generation_site(exemption: Exemption) -> None:
    """§7.1：每个 JS/Node/浏览器豁免 id MUST 对应一个具名生成点，且带
    「非 canonical」声明锚点。"""
    source = (_repo_root() / exemption.carrier).read_text(encoding="utf-8")
    assert exemption.marker in source, (
        f"{exemption.carrier} 缺少 「{exemption.marker}」 声明锚点"
    )
    assert exemption.prefix in source, (
        f"{exemption.carrier} 未见 {exemption.prefix} 生成点"
    )


# ----------------------------------------------------------------------
# §7.2 canonical 校验器 MUST 拒绝非 canonical id 作为 session/thread 身份
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "exemption",
    _JS_EXEMPTIONS + _RUNTIME_EXEMPTIONS,
    ids=lambda e: e.prefix,
)
def test_canonical_validator_rejects_non_canonical_id(exemption: Exemption) -> None:
    """§7.2：非 canonical id MUST 被 canonical 校验器拒绝为 session/thread 身份。"""
    sample = _sample_id(exemption.prefix)
    with pytest.raises(ValueError):
        validate_session_id(sample)
    with pytest.raises(ValueError):
        validate_thread_id(sample)


def test_exemption_must_not_expand_to_canonical_ids() -> None:
    """§7.2：豁免 MUST NOT 扩张——非 canonical id 不得借豁免进入 canonical 面。"""
    for exemption in _JS_EXEMPTIONS + _RUNTIME_EXEMPTIONS:
        sample = _sample_id(exemption.prefix)
        for validator in (validate_session_id, validate_thread_id):
            with pytest.raises(ValueError):
                validator(sample)


def test_factory_prefixes_remain_canonical() -> None:
    """反向核对：真实工厂前缀（ses_/thr_）仍被接受，豁免不误伤 canonical 面。"""
    from app.core.identifier import create_prefixed_id

    validate_session_id(create_prefixed_id("ses"))
    validate_thread_id(create_prefixed_id("thr"))
