"""Gateway 与 Workspace 共用的配置事务恢复；调用方持有原数据库写锁。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

import psutil

from app.services.infrastructure.config.state import ConfigConflictError


def recover_expired_applies(connection: sqlite3.Connection, domain: str) -> tuple[str, ...]:
    """在线只回收已过期执行权，不把未完成的副作用当作已恢复。"""
    now = datetime.now(UTC).isoformat()
    ids = tuple(str(row[0]) for row in connection.execute(
        "SELECT candidate_id FROM config_pending_candidate WHERE config_domain=? AND state='applying' "
        "AND candidate_id NOT IN (SELECT candidate_id FROM config_apply_claim "
        "WHERE config_domain=? AND lease_expires_at>?)", (domain, domain, now),
    ))
    reason = ("Gateway " if domain == "gateway" else "") + "启动恢复发现 apply lease 已过期"
    for candidate_id in ids:
        connection.execute(
            "UPDATE config_pending_candidate SET state='recovery_required',last_error=? "
            "WHERE config_domain=? AND candidate_id=? AND state='applying'", (reason, domain, candidate_id),
        )
        connection.execute(
            "UPDATE config_apply_journal SET state='recovery_required',last_error=?,updated_at=? "
            "WHERE config_domain=? AND candidate_id=? AND state='applying'", (reason, now, domain, candidate_id),
        )
        if domain == "gateway":
            connection.execute(
                "UPDATE gateway_restart_intent SET state='recovery_required',last_error=?,updated_at=? "
                "WHERE candidate_id=? AND state='applying'", (reason, now, candidate_id),
            )
    # 提升已提交而 finally 未运行时，也必须释放过期 claim。
    connection.execute("DELETE FROM config_apply_claim WHERE config_domain=? AND lease_expires_at<=?", (domain, now))
    return ids


@dataclass(frozen=True)
class ConfigRecoveryPlan:
    actions: tuple[dict[str, str], ...]
    blockers: tuple[dict[str, str], ...]


def _stopped_effect(connection: sqlite3.Connection, effect: dict[str, object], domain: str, matches_active: bool) -> bool:
    """只识别已有账本能证明结束的副作用，未知插件副作用必须交还原 owner。"""
    if effect.get("resource") == "runtime-logging":
        states = [cast(dict[str, object], effect[key]) for key in ("before", "current") if isinstance(effect.get(key), dict)]
        return bool(states) and all(
            type(state.get("pid")) is int and not psutil.pid_exists(cast(int, state["pid"]))
            for state in states
        )
    if domain == "gateway" and effect.get("action") == "generation_handoff":
        # TODO：仅承接本次用户授权恢复的 2026-11-12 dev 换代回执；存量回执结算且不再支持该来源后移除。
        # 历史换代回执只有在内容已被当前 active 接纳、旧代已关闭时才能结算；
        # 停止 Gateway 本身不能证明独立的工作区进程已停止。
        if not matches_active or effect.get("status") != "succeeded":
            return False
        if effect.get("resource") not in {"registry-batch", "ssh-tunnel-proxy", "workspace-process", "remote-projection"}:
            return False
        row = connection.execute(
            "SELECT process_id,state,listener_state FROM gateway_runtime_generation WHERE generation_id=?",
            (effect.get("generation"),),
        ).fetchone()
        return bool(row and row[1] in {"closed", "failed"} and row[2] == "closed"
                    and (row[0] is None or not psutil.pid_exists(row[0])))
    return effect.get("phase") == "compensation" and effect.get("status") == "succeeded"


def plan_stopped_config_recovery(connection: sqlite3.Connection, domain: str) -> ConfigRecoveryPlan:
    """只读计划；离线写入还必须取得数据库 ownership 与 BEGIN IMMEDIATE。"""
    actions: list[dict[str, str]] = []
    blockers: list[dict[str, str]] = []
    now = datetime.now(UTC).isoformat()
    active = connection.execute(
        "SELECT active_revision,promoted_apply_id,state,candidate_id,effective_digest "
        "FROM config_active_snapshot WHERE config_domain=?", (domain,),
    ).fetchone()
    if active is not None and active[2] != "active":
        blockers.append({"kind": "active", "id": domain, "state": str(active[2])})
    if domain == "gateway":
        for generation, pid in connection.execute(
            "SELECT generation_id,process_id FROM gateway_runtime_generation "
            "WHERE state NOT IN ('closed','failed') AND process_id IS NOT NULL",
        ):
            if psutil.pid_exists(pid):
                blockers.append({"kind": "runtime", "id": generation, "state": "running"})
    for apply_id, expires in connection.execute(
        "SELECT apply_id,lease_expires_at FROM config_apply_claim WHERE config_domain=?", (domain,),
    ):
        if expires > now:
            blockers.append({"kind": "claim", "id": apply_id, "state": "claimed"})
        else:
            actions.append({"kind": "claim", "id": apply_id, "state": "released"})
    blocked_candidates: set[str] = set()
    for apply_id, candidate_id, effects in connection.execute(
        "SELECT apply_id,candidate_id,side_effects_json FROM config_apply_journal "
        "WHERE config_domain=? AND state IN ('applying','recovery_required')", (domain,),
    ):
        candidate = connection.execute(
            "SELECT effective_digest FROM config_pending_candidate WHERE config_domain=? AND candidate_id=?", (domain, candidate_id),
        ).fetchone()
        matches_active = bool(active and candidate and active[4] == candidate[0])
        if active is not None and active[1] == apply_id:
            state = "committed"
        elif all(_stopped_effect(connection, effect, domain, matches_active) for effect in json.loads(effects)):
            state = "failed"
        else:
            blocked_candidates.add(candidate_id)
            blockers.append({"kind": "apply", "id": apply_id, "state": "recovery_required"})
            continue
        actions.append({"kind": "apply", "id": apply_id, "state": state})
    for candidate_id, revision, state in connection.execute(
        "SELECT candidate_id,pending_revision,state FROM config_pending_candidate "
        "WHERE config_domain=? AND state NOT IN ('active','rejected','discarded')", (domain,),
    ):
        if candidate_id in blocked_candidates or state == "conflict":
            blockers.append({"kind": "candidate", "id": candidate_id, "state": state})
            continue
        # 新 active 已经提交过更高 revision，旧候选不得再次覆盖它。
        if active is not None and candidate_id == active[3]:
            target = "active"
        elif active is not None and revision < active[0]:
            target = "discarded"
        elif state in {"applying", "recovery_required"}:
            restart = (connection.execute("SELECT 1 FROM gateway_restart_intent WHERE candidate_id=?", (candidate_id,)).fetchone()
                       if domain == "gateway" else connection.execute(
                           "SELECT candidate_ref FROM config_pending_candidate WHERE config_domain=? AND candidate_id=?",
                           (domain, candidate_id),
                       ).fetchone())
            target = "pending_restart" if restart and restart[0] else "candidate_validated"
        else:
            continue
        actions.append({"kind": "candidate", "id": candidate_id, "state": target})
    return ConfigRecoveryPlan(tuple(actions), tuple(blockers))


def recover_stopped_config_applies(connection: sqlite3.Connection, domain: str) -> ConfigRecoveryPlan:
    """在原离线备份／发布事务内结算；候选正文保留，绝不伪造运行时健康证明。"""
    plan = plan_stopped_config_recovery(connection, domain)
    if plan.blockers:
        raise ConfigConflictError(f"配置恢复尚有存活执行者或未核实副作用: {plan.blockers}")
    now = datetime.now(UTC).isoformat()
    reason = "启动恢复：旧执行已结束；保留来源配置，待新运行时重新验证"
    for action in plan.actions:
        kind, identity, state = action["kind"], action["id"], action["state"]
        if kind == "claim":
            connection.execute("DELETE FROM config_apply_claim WHERE config_domain=? AND apply_id=?", (domain, identity))
        elif kind == "apply":
            connection.execute(
                "UPDATE config_apply_journal SET state=?,last_error=?,updated_at=? WHERE config_domain=? AND apply_id=?",
                (state, None if state == "committed" else reason, now, domain, identity),
            )
        else:
            connection.execute(
                "UPDATE config_pending_candidate SET state=?,last_error=? WHERE config_domain=? AND candidate_id=?",
                (state, None if state == "active" else reason, domain, identity),
            )
            if domain == "gateway":
                connection.execute(
                    "UPDATE gateway_restart_intent SET state=?,last_error=?,updated_at=? WHERE candidate_id=? "
                    "AND state NOT IN ('active','discarded')",
                    (state if state in {"active", "discarded"} else "failed", None if state == "active" else reason, now, identity),
                )
    return plan
