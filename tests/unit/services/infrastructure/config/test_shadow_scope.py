"""配置 shadow generation 生命周期合同测试。"""

from __future__ import annotations

import pytest

from app.services.infrastructure.config.contracts import ConfigPublication
from app.services.infrastructure.config.shadow_scope import (
    ConfigShadowGeneration,
    ConfigShadowLifecycleError,
    ConfigShadowLifecycleOwner,
)


def _owner(
    *,
    reconcile_calls: list[str] | None = None,
    published: list[ConfigShadowGeneration] | None = None,
    publish_error: Exception | None = None,
    guard_keys: tuple[str, ...] = ("context",),
    validator_error: Exception | None = None,
) -> ConfigShadowLifecycleOwner:
    reconcile = reconcile_calls if reconcile_calls is not None else []
    published = published if published is not None else []
    scope_releases: list[str] = []

    async def reconcile_candidate(config, scope) -> None:
        reconcile.append(f"generation-candidate:{config['name']}")
        scope.register(lambda: scope_releases.append(config["name"]), label="candidate")

    async def publish_candidate(generation: ConfigShadowGeneration, publish: ConfigPublication) -> None:
        if publish_error is not None:
            raise publish_error
        published.append({"generation": generation.generation, "name": generation.config["name"]})
        publish()

    def validate(config) -> None:
        if validator_error is not None:
            raise validator_error
        if config.get("name") != "valid":
            raise ValueError("invalid name")

    return ConfigShadowLifecycleOwner(
        validator=validate,
        reconcile=reconcile_candidate,
        publish=publish_candidate,
        bootstrap_guard_keys=guard_keys,
    )


@pytest.mark.asyncio
async def test_bootstrap_publishes_generation_and_ready_gate_opens() -> None:
    reconcile: list[str] = []
    owner = _owner(reconcile_calls=reconcile)
    active = await owner.start({"name": "valid", "context": {}})
    assert owner.readiness == "ready"
    assert owner.active is active
    assert active.generation == 1
    assert reconcile == ["generation-candidate:valid"]


@pytest.mark.asyncio
async def test_invalid_bootstrap_fails_startup_without_active_generation() -> None:
    owner = _owner(
        validator_error=ValueError("bad bootstrap"),
        guard_keys=(),
    )
    with pytest.raises(ValueError, match="bad bootstrap"):
        await owner.start({"name": "valid"})
    assert owner.readiness == "failed"
    with pytest.raises(ConfigShadowLifecycleError, match="readiness_gate_closed"):
        owner.active  # noqa: B018


@pytest.mark.asyncio
async def test_bad_candidate_only_closes_candidate_and_keeps_old_generation() -> None:
    reconcile: list[str] = []
    owner = _owner(reconcile_calls=reconcile)
    active = await owner.start({"name": "valid", "context": {}})
    with pytest.raises(ConfigShadowLifecycleError, match="candidate_invalid"):
        await owner.apply_candidate(
            {"name": "invalid", "context": {}},
            expected_generation=active.generation,
        )
    assert owner.generation == 1
    assert owner.active is active
    assert reconcile == ["generation-candidate:valid"]


@pytest.mark.asyncio
async def test_candidate_cannot_remove_bootstrap_key() -> None:
    owner = _owner()
    await owner.start({"name": "valid", "context": {}})
    assert owner.generation == 1
    with pytest.raises(ConfigShadowLifecycleError, match="bootstrap_removed"):
        await owner.apply_candidate({"name": "valid"}, expected_generation=1)
    assert owner.generation == 1


@pytest.mark.asyncio
async def test_generation_fence_rejects_stale_candidate_before_reconcile() -> None:
    reconcile: list[str] = []
    owner = _owner(reconcile_calls=reconcile)
    await owner.start({"name": "valid", "context": {}})
    assert owner.generation == 1
    with pytest.raises(ConfigShadowLifecycleError, match="generation_fence_conflict"):
        await owner.apply_candidate(
            {"name": "valid", "context": {"changed": True}},
            expected_generation=0,
        )
    assert reconcile == ["generation-candidate:valid"]


@pytest.mark.asyncio
async def test_same_config_is_unchanged_and_does_not_double_publish() -> None:
    reconcile: list[str] = []
    published: list[ConfigShadowGeneration] = []
    owner = _owner(reconcile_calls=reconcile, published=published)
    active = await owner.start({"name": "valid", "context": {}})
    result = await owner.apply_candidate(
        {"name": "valid", "context": {}},
        expected_generation=active.generation,
    )
    assert result.status == "unchanged"
    assert result.generation == 1
    assert len(published) == 1


@pytest.mark.asyncio
async def test_successful_candidate_drains_old_scope_after_publish() -> None:
    reconcile: list[str] = []
    published: list[ConfigShadowGeneration] = []
    owner = _owner(reconcile_calls=reconcile, published=published)
    old = await owner.start({"name": "valid", "context": {}})
    result = await owner.apply_candidate(
        {"name": "valid", "context": {"version": 2}},
        expected_generation=old.generation,
    )
    assert result.status == "published"
    assert result.generation == 2
    assert owner.active.generation == 2
    assert owner.active.scope.state == "open"
    assert old.scope.state == "closed"
    assert reconcile == [
        "generation-candidate:valid",
        "generation-candidate:valid",
    ]
    assert published == [
        {"generation": 1, "name": "valid"},
        {"generation": 2, "name": "valid"},
    ]


@pytest.mark.asyncio
async def test_publish_failure_keeps_old_active_and_closes_candidate() -> None:
    reconcile: list[str] = []
    candidate_error = RuntimeError("publish failed")
    owner = _owner(reconcile_calls=reconcile)
    # 只让 generation 2 的 candidate publish 失败，不影响 bootstrap。
    original_publish = owner._publish

    async def publish_second_failure(generation: ConfigShadowGeneration, publish: ConfigPublication) -> None:
        if generation.generation > 1:
            raise candidate_error
        await original_publish(generation, publish)

    owner._publish = publish_second_failure
    old = await owner.start({"name": "valid", "context": {}})
    with pytest.raises(RuntimeError, match="publish failed"):
        await owner.apply_candidate(
            {"name": "valid", "context": {"version": 2}},
            expected_generation=old.generation,
        )
    assert owner.active is old
    assert old.scope.state == "open"
    assert reconcile == ["generation-candidate:valid", "generation-candidate:valid"]


@pytest.mark.asyncio
async def test_publisher_must_explicitly_commit_before_returning() -> None:
    owner = _owner()
    old = await owner.start({"name": "valid", "context": {}})
    candidates = []

    async def forget_commit(generation: ConfigShadowGeneration, _publish: ConfigPublication) -> None:
        candidates.append(generation)

    owner._publish = forget_commit
    with pytest.raises(ConfigShadowLifecycleError, match="publication_not_committed"):
        await owner.apply_candidate({"name": "valid", "context": {"version": 2}}, expected_generation=1)
    assert owner.active is old and old.scope.state == "open"
    assert len(candidates) == 1 and candidates[0].scope.state == "closed"
    await owner.close()


@pytest.mark.asyncio
async def test_post_commit_error_retains_new_active_and_old_scope_until_close() -> None:
    owner = _owner()
    old = await owner.start({"name": "valid", "context": {}})

    async def fail_after_commit(_generation: ConfigShadowGeneration, publish: ConfigPublication) -> None:
        publish()
        raise RuntimeError("提交后的 claim 清理失败")

    owner._publish = fail_after_commit
    with pytest.raises(RuntimeError, match="提交后的 claim"):
        await owner.apply_candidate({"name": "valid", "context": {"version": 2}}, expected_generation=1)
    current = owner.active
    assert current.generation == 2 and current.scope.state == "open"
    assert old.scope.state == "open" and owner._retired == [old]
    await owner.close()
    assert current.scope.state == old.scope.state == "closed"
    assert owner.readiness == "closed" and not owner._retired


@pytest.mark.asyncio
async def test_failed_old_scope_remains_owned_and_repeated_close_reports_failure() -> None:
    owner = _owner()
    old = await owner.start({"name": "valid", "context": {}})
    attempts = []

    def fail_release() -> None:
        attempts.append("release")
        raise RuntimeError("旧 scope 无法释放")

    old.scope.register(fail_release, label="old-policy-source")
    with pytest.raises(ExceptionGroup):
        await owner.apply_candidate({"name": "valid", "context": {"version": 2}}, expected_generation=1)
    current = owner.active
    for _ in range(2):
        with pytest.raises(ExceptionGroup):
            await owner.close()
        assert owner.readiness == "close_failed"
        assert owner._retired == [old]
        assert current.scope.state == "closed" and old.scope.state == "close_failed"
    # 原 LifetimeScope 对失败的释放项重试；已成功关闭的新 scope 不再重复释放。
    assert attempts == ["release", "release", "release"]
