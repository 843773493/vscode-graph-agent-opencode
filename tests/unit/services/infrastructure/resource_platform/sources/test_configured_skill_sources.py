"""登记的非 HTTP 能力直接进入同一接受链路，不靠 URL、目录或手动 observe。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

import pytest

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
from app.services.infrastructure.resource_platform.sources.skill_fetch_contracts import (
    SkillChangeSubscription,
    SkillFetchCapability,
    SkillFetchConfig,
    SkillFetchResult,
    SkillFetchRuntime,
)
from app.services.infrastructure.resource_platform.sources.skill_source_config import (
    SkillSourceConfig,
)


class FixtureFetchConfig(SkillFetchConfig, frozen=True):
    protocol: Literal["fixture"]
    key: str


@dataclass
class PulseSubscription:
    signal: asyncio.Event = field(default_factory=asyncio.Event)
    closed: bool = False
    error: OSError | None = None
    waits: int = 0
    cancellations: int = 0

    async def wait(self) -> None:
        self.waits += 1
        try:
            await self.signal.wait()
        except asyncio.CancelledError:
            self.cancellations += 1
            raise
        self.signal.clear()
        if self.error is not None:
            raise self.error

    async def close(self) -> None:
        self.closed = True


@dataclass
class FetchCase:
    owner: ConfiguredSkillSources
    channel: ResourceObservationChannel
    body: bytes = b"first accepted body"
    validators: dict[str, str] = field(default_factory=lambda: {"fixture_token": "first"})
    reads: int = 0
    received: list[Mapping[str, str]] = field(default_factory=list)
    subscriptions: list[PulseSubscription] = field(default_factory=list)
    runtime_closed: bool = False
    expected_close_failure: bool = False

    def config(self, mode: str = "event", interval: str | None = None) -> SkillSourceConfig:
        return SkillSourceConfig.model_validate({
            "id": "registered-one", "name": "demo", "fetch": {"protocol": "fixture", "key": "private-owner-key"},
            "updates": {"mode": mode, "poll_interval": interval},
        })


@pytest.fixture
async def fetch_case(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FetchCase]:
    channel = ResourceObservationChannel()
    owner = ConfiguredSkillSources(scope="workspace", scope_id="fixture-workspace", channel=channel)
    case = FetchCase(owner, channel)

    class FixtureFetch:
        async def fetch(self, config: SkillFetchConfig, *, validators: Mapping[str, str], has_snapshot: bool, max_bytes: int) -> SkillFetchResult:
            assert isinstance(config, FixtureFetchConfig) and config.key == "private-owner-key"
            case.reads += 1
            case.received.append(validators)
            return SkillFetchResult(case.body, dict(case.validators))

        async def close(self) -> None:
            case.runtime_closed = True

    async def subscribe(_runtime: SkillFetchRuntime, _config: SkillFetchConfig) -> SkillChangeSubscription:
        subscription = PulseSubscription()
        case.subscriptions.append(subscription)
        return subscription

    monkeypatch.setattr(skill_source_config, "SKILL_FETCH_CAPABILITIES", {
        **skill_source_config.SKILL_FETCH_CAPABILITIES,
        "fixture": SkillFetchCapability(FixtureFetchConfig, FixtureFetch, subscribe),
    })
    try:
        yield case
    finally:
        if case.expected_close_failure:
            with pytest.raises(BaseExceptionGroup):
                await owner.close()
        else:
            await owner.close()
        assert case.runtime_closed or case.reads == 0
        assert all(subscription.closed for subscription in case.subscriptions)


async def _until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0.001)


async def test_non_http_event_source_accepts_initial_window_and_shared_validation(fetch_case: FetchCase) -> None:
    case = fetch_case
    config = SkillSourceConfig.model_validate_json(case.config().model_dump_json())
    assert config == case.config() and isinstance(config.fetch, FixtureFetchConfig)
    candidate = await case.owner.prepare((config,))
    assert len(case.subscriptions) == 1 and case.reads == 1
    # 初始获取与配置激活之间的变化必须由已经建立的订阅取得。
    case.body = b"changed before activation"
    case.subscriptions[0].signal.set()
    case.owner.activate(candidate)
    await _until(lambda: case.owner.entries()[0].content == "changed before activation")
    assert case.reads == 2
    accepted = case.owner.entries()[0]
    assert case.owner.read(accepted.display_uri, 1024).data == case.body
    assert case.reads == 2, "普通导航只消费 accepted，不触发获取能力"
    case.body, case.validators["fixture_token"] = b"\xff", "must-not-commit"
    case.subscriptions[0].signal.set()
    await _until(lambda: not case.owner.entries()[0].available)
    assert case.owner.entries()[0].content == accepted.content
    case.body = b"recovered from same capability"
    case.subscriptions[0].signal.set()
    await _until(lambda: case.owner.entries()[0].available)
    assert case.received[-1] == {"fixture_token": "first"}, "正文校验失败不得推进条件缓存"
    assert case.owner.entries()[0].source_id == accepted.source_id
    assert "private-owner-key" not in case.owner.entries()[0].model_dump_json()


@pytest.mark.parametrize("mode", ["poll", "event_poll"])
async def test_registered_capability_composes_poll_event_and_manual_without_second_state(
    fetch_case: FetchCase, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    case = fetch_case
    monkeypatch.setattr(SourceUpdatePolicy, "poll_seconds", property(lambda self: 0.03 if self.poll_interval else None))
    previous = await case.owner.prepare((case.config(mode, "1m"),))
    case.owner.activate(previous)
    assert len(case.subscriptions) == int(mode == "event_poll")
    case.body = b"periodic reconciliation"
    await _until(lambda: case.owner.entries()[0].content == "periodic reconciliation")
    if mode == "event_poll":
        case.body = b"event reconciliation"
        case.subscriptions[0].signal.set()
        await _until(lambda: case.owner.entries()[0].content == "event reconciliation")
    manual = await case.owner.prepare((case.config("manual"),))
    case.owner.activate(manual)
    await case.owner.close_generation(previous)
    assert all(task.done() for task in previous.tasks.values())
    assert all(subscription.closed for subscription in case.subscriptions)
    count = case.reads
    case.body = b"explicit manual reconciliation"
    await asyncio.sleep(0.06)
    assert case.reads == count and not manual.tasks
    await case.owner.refresh()
    assert case.owner.entries()[0].content == "explicit manual reconciliation"


async def test_event_poll_keeps_pending_event_wait_across_poll_timeouts(
    fetch_case: FetchCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = fetch_case
    monkeypatch.setattr(SourceUpdatePolicy, "poll_seconds", property(lambda self: 0.02 if self.poll_interval else None))
    generation = await case.owner.prepare((case.config("event_poll", "1m"),))
    case.owner.activate(generation)
    await _until(lambda: case.reads >= 4)
    subscription = case.subscriptions[0]
    assert subscription.waits == 1 and subscription.cancellations == 0
    case.body = b"event after several polling cycles"
    subscription.signal.set()
    await _until(lambda: case.owner.entries()[0].content == case.body.decode())
    await _until(lambda: subscription.waits == 2)
    await case.owner.close_generation(generation)
    assert subscription.closed and subscription.cancellations == 1


@pytest.mark.parametrize("mode", ["event", "poll"])
async def test_configuration_rollback_resumes_previous_observer_without_losing_pending_trigger(
    fetch_case: FetchCase, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    case = fetch_case
    monkeypatch.setattr(SourceUpdatePolicy, "poll_seconds", property(lambda self: 0.02 if self.poll_interval else None))
    previous = await case.owner.prepare((case.config(mode, "1m" if mode == "poll" else None),))
    case.owner.activate(previous)
    if mode == "event":
        await _until(lambda: case.subscriptions[0].waits == 1)
    else:
        await asyncio.sleep(0)
    candidate = await case.owner.prepare((case.config("manual"),))
    case.owner.activate(candidate)
    count = case.reads
    case.body = b"change while candidate awaits settlement"
    if mode == "event":
        case.subscriptions[0].signal.set()
    await asyncio.sleep(0.06)
    assert case.reads == count, "休眠代次不得获取或发布"
    # 配置结算失败复用原代次；已收到的触发必须在恢复后继续对账。
    case.owner.activate(previous)
    await case.owner.close_generation(candidate)
    await _until(lambda: case.owner.entries()[0].available and case.owner.entries()[0].content == case.body.decode())
    assert not previous.tasks["registered-one"].done()


async def test_missing_event_capability_and_unknown_protocol_fail_before_runtime_creation(
    fetch_case: FetchCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = fetch_case
    original = skill_source_config.skill_fetch_capability("fixture")
    monkeypatch.setattr(skill_source_config, "SKILL_FETCH_CAPABILITIES", {
        "fixture": SkillFetchCapability(original.config_type, original.create),
    })
    with pytest.raises(ValueError, match="未登记事件"):
        case.config()
    with pytest.raises(ValueError, match="未登记"):
        SkillSourceConfig.model_validate({"id": "unknown", "name": "demo", "fetch": {"protocol": "unknown"}})
    assert case.reads == 0 and not case.subscriptions and not case.owner._runtimes


async def test_event_failure_invalidates_publication_without_fallback_polling(fetch_case: FetchCase, caplog: pytest.LogCaptureFixture) -> None:
    case = fetch_case
    notices = case.channel.subscribe(label="source-failure-proof")
    case.owner.activate(await case.owner.prepare((case.config(),)))
    notices.pending()
    before = case.owner.entries()[0]
    case.expected_close_failure = True
    case.subscriptions[0].error = OSError("registered subscription disconnected")
    case.subscriptions[0].signal.set()
    await _until(lambda: not case.owner.entries()[0].available)
    async with asyncio.timeout(1):
        notice = await notices.next()
    assert notice.directory_id == case.owner.directory_id and not notice.available
    assert case.owner.entries()[0].content == before.content and case.reads == 1
    assert "registered subscription disconnected" in caplog.text
    case.channel.unsubscribe(notices)


def test_poll_interval_must_be_finite_before_any_owner_starts() -> None:
    with pytest.raises(ValueError, match="有限秒数"):
        SourceUpdatePolicy(mode="poll", poll_interval="9" * 400 + "h")
