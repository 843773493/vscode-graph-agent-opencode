"""HTTP 来源的唯一接受链路：条件请求、版本、代次和显式不可用。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

import httpx
import pytest

from app.core.lifecycle import LifetimeScope
from app.services.infrastructure.resource_platform.observation.resource_observation_channel import (
    ResourceObservationChannel,
)
from app.services.infrastructure.resource_platform.observation.source_update_policy import (
    SourceUpdatePolicy,
)
from app.services.infrastructure.resource_platform.sources import skill_source_config
from app.services.infrastructure.resource_platform.sources.configured_skill_sources import (
    ConfiguredSkillSources,
)
from app.services.infrastructure.resource_platform.sources.http_skill_fetch import (
    HttpSkillFetch,
)
from app.services.infrastructure.resource_platform.sources.observed_source import (
    MAX_STABLE_READ_BYTES,
)
from app.services.infrastructure.resource_platform.sources.skill_fetch_contracts import (
    SkillFetchCapability,
)
from app.services.infrastructure.resource_platform.sources.skill_source_config import (
    SkillSourceConfig,
    validate_skill_sources,
)


@dataclass
class HttpCase:
    owner: ConfiguredSkillSources
    requests: list[httpx.Request] = field(default_factory=list)
    status: int = 200
    body: bytes = b"initial body"
    headers: dict[str, str] = field(default_factory=lambda: {"ETag": '"one"', "Last-Modified": "Mon, 26 Oct 2026 10:00:00 GMT"})
    handler: Callable[[httpx.Request], Awaitable[httpx.Response]] | None = None

    async def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.handler is not None:
            return await self.handler(request)
        return httpx.Response(self.status, content=self.body, headers=self.headers)


@pytest.fixture
def source_config() -> SkillSourceConfig:
    return SkillSourceConfig.model_validate({
        "id": "remote-one", "name": "demo",
        "fetch": {"protocol": "http", "url": "https://skills.example.test/SKILL.md"},
        "updates": {"mode": "manual"},
    })


@pytest.fixture
async def http_case(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[HttpCase]:
    owner = ConfiguredSkillSources(scope="gateway", scope_id="gateway_0123456789abcdef0123456789abcdef", channel=ResourceObservationChannel())
    case = HttpCase(owner)
    capability = skill_source_config.skill_fetch_capability("http")
    monkeypatch.setattr(skill_source_config, "SKILL_FETCH_CAPABILITIES", {
        "http": SkillFetchCapability(config_type=capability.config_type,
            create=lambda: HttpSkillFetch(httpx.AsyncClient(transport=httpx.MockTransport(case.respond)))),
    })
    try:
        yield case
    finally:
        await owner.close()


async def _start(case: HttpCase, config: SkillSourceConfig) -> None:
    case.owner.activate(await case.owner.prepare((config,)))


async def test_conditional_fetch_uses_body_revision_and_stable_identity(http_case: HttpCase, source_config: SkillSourceConfig) -> None:
    case = http_case
    await _start(case, source_config)
    before = case.owner.entries()[0]
    case.headers["ETag"] = '"validator-only"'
    await case.owner.refresh()
    assert case.owner.entries()[0] == before
    assert case.requests[-1].headers["If-None-Match"] == '"one"'
    assert "If-Modified-Since" in case.requests[-1].headers
    case.status, case.body = 304, b""
    await case.owner.refresh()
    assert case.owner.entries()[0] == before
    assert case.requests[-1].headers["If-None-Match"] == '"validator-only"'
    case.status, case.body = 200, "new body Ω\r\n".encode()
    case.headers.clear()
    await case.owner.refresh()
    after = case.owner.entries()[0]
    assert after.revision != before.revision
    assert (after.entry_identity, after.source_id, after.display_uri) == (before.entry_identity, before.source_id, before.display_uri)
    assert after.byte_length == len(case.body)
    assert case.owner.read(after.display_uri, MAX_STABLE_READ_BYTES).data == case.body
    assert len(case.requests) == 4  # 读取 accepted 正文不得联网。
    assert "skills.example.test" not in after.model_dump_json()


@pytest.mark.parametrize("failure", ["status", "encoding", "size", "unsolicited-304", "body-304"])
async def test_failure_keeps_previous_bytes_but_never_reports_them_as_current(
    http_case: HttpCase, source_config: SkillSourceConfig, failure: str,
) -> None:
    case = http_case
    if failure == "unsolicited-304":
        case.headers.clear()
    await _start(case, source_config)
    before = case.owner.entries()[0]
    if failure == "status":
        case.status = 503
    elif failure == "encoding":
        case.body = b"\xff"
    elif failure == "size":
        case.body = b"x" * (MAX_STABLE_READ_BYTES + 1)
    else:
        case.status, case.body = 304, b"" if failure == "unsolicited-304" else b"illegal body"
    await case.owner.refresh()
    failed = case.owner.entries()[0]
    assert not failed.available and failed.error_code == "source-unavailable"
    assert (failed.content, failed.revision) == (before.content, before.revision)
    with pytest.raises(FileNotFoundError):
        case.owner.read(before.display_uri, MAX_STABLE_READ_BYTES)
    case.status, case.body = 200, before.content.encode()
    await case.owner.refresh()
    assert case.owner.entries()[0] == before


async def test_failed_candidate_keeps_original_generation_and_validators(http_case: HttpCase, source_config: SkillSourceConfig) -> None:
    case = http_case
    await _start(case, source_config)
    previous = case.owner.current
    before = case.owner.entries()
    case.status = 503
    with pytest.raises(ExceptionGroup, match="对账失败"):
        await case.owner.prepare((source_config.model_copy(update={"priority": 2}),))
    assert case.owner.current is previous and case.owner.entries() == before
    assert case.owner._generations == {previous}
    case.status, case.body = 304, b""
    await case.owner.refresh()
    assert case.requests[-1].headers["If-None-Match"] == '"one"'
    assert case.owner.entries() == before


@pytest.mark.parametrize("late_error", [False, True])
async def test_retired_generation_cannot_publish_late_result_or_error(
    http_case: HttpCase, source_config: SkillSourceConfig, late_error: bool,
) -> None:
    case = http_case
    await _start(case, source_config)
    previous = case.owner.current
    assert previous is not None
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        if late_error:
            raise httpx.ReadError("late old generation")
        return httpx.Response(200, content=b"late old bytes")

    case.handler = delayed
    pending = asyncio.create_task(case.owner.refresh())
    await entered.wait()
    case.handler, case.body = None, b"candidate bytes"
    candidate = await case.owner.prepare((source_config,))
    case.owner.activate(candidate)
    release.set()
    await pending
    assert case.owner.entries()[0].content == "candidate bytes"
    assert case.owner.entries()[0].available
    await case.owner.close_generation(previous)
    assert case.owner._generations == {candidate}


async def test_overlapping_refreshes_share_one_inflight_fetch(http_case: HttpCase, source_config: SkillSourceConfig) -> None:
    case = http_case
    await _start(case, source_config)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return httpx.Response(200, content=b"one accepted update")

    case.handler = delayed
    first = asyncio.create_task(case.owner.refresh())
    await entered.wait()
    second = asyncio.create_task(case.owner.refresh())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert len(case.requests) == 2
    assert case.owner.entries()[0].content == "one accepted update"


async def test_poll_and_manual_use_same_reconciler(http_case: HttpCase, source_config: SkillSourceConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    case = http_case
    monkeypatch.setattr(SourceUpdatePolicy, "poll_seconds", property(lambda self: 0.02 if self.poll_interval else None))
    poll = source_config.model_copy(update={"updates": SourceUpdatePolicy(mode="poll", poll_interval="1m")})
    await _start(case, poll)
    old = case.owner.current
    assert old is not None
    case.body = b"polled update"
    async with asyncio.timeout(1):
        while case.owner.entries()[0].content != "polled update":
            await asyncio.sleep(0.005)
    manual = await case.owner.prepare((source_config,))
    case.owner.activate(manual)
    await case.owner.close_generation(old)
    count = len(case.requests)
    case.body = b"manual update"
    await asyncio.sleep(0.06)
    assert len(case.requests) == count
    await case.owner.refresh()
    assert case.owner.entries()[0].content == "manual update"
    assert all(task.done() for task in old.tasks.values()) and not manual.tasks


@pytest.mark.parametrize("change", [
    {"fetch": {"protocol": "ftp", "url": "https://example.test/skill"}},
    {"fetch": {"protocol": "http", "url": "file:///etc/hosts"}},
    {"fetch": {"protocol": "http", "url": "https://example.test/#fragment"}},
    {"fetch": {"protocol": "http", "url": "https://example.test:bad/"}},
    {"id": "a" * 65},
    {"updates": {"mode": "event"}},
    {"updates": {"mode": "event_poll", "poll_interval": "1h"}},
    {"updates": {"mode": "poll"}},
    {"updates": {"mode": "manual", "poll_interval": "1h"}},
    {"priority": True},
])
def test_unsupported_fetch_policy_combinations_fail(source_config: SkillSourceConfig, change: dict) -> None:
    with pytest.raises(ValueError):
        validate_skill_sources([{**source_config.model_dump(), **change}])


def test_source_ids_are_unique_and_default_policy_is_explicit(source_config: SkillSourceConfig) -> None:
    with pytest.raises(ValueError, match="id 重复"):
        validate_skill_sources([source_config, source_config])
    default = SkillSourceConfig.model_validate(source_config.model_dump(exclude={"updates"}))
    assert default.updates == SourceUpdatePolicy(mode="poll", poll_interval="1h")


async def test_shadow_reuses_manual_generation_then_drains_after_actual_source_change(http_case: HttpCase, source_config: SkillSourceConfig) -> None:
    case = http_case
    first, second, third = (LifetimeScope(f"config-{index}") for index in range(3))
    (await case.owner.prepare_config((source_config,), first))()
    original = case.owner.current
    (await case.owner.prepare_config((source_config,), second))()
    await first.close()
    assert case.owner.current is original and case.owner._generations == {original}
    assert len(case.requests) == 1, "无关 shadow 切换不触发 manual 获取"
    activate = await case.owner.prepare_config((source_config.model_copy(update={"priority": 2}),), third)
    assert case.owner.current is original
    activate()
    await second.close()
    assert original not in case.owner._generations and len(case.owner._generations) == 1
    assert case.owner.entries()[0].priority == 2
    await third.close()
    assert case.owner.current is not None  # 当前代次由进程 owner 关闭。


async def test_unpublished_shadow_closes_only_candidate(http_case: HttpCase, source_config: SkillSourceConfig) -> None:
    case = http_case
    await _start(case, source_config)
    previous = case.owner.current
    scope = LifetimeScope("rejected-config")
    await case.owner.prepare_config((source_config.model_copy(update={"priority": 1}),), scope)
    assert len(case.owner._generations) == 2
    await scope.close()
    assert case.owner.current is previous and case.owner._generations == {previous}
