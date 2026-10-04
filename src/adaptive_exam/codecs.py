"""领域对象的 JSON 编解码。

结果快照与证据链均完整落盘：进程重启后，尚未结束的会话可以继续，
已结束会话的逐题依据与贡献也可以原样还原。
"""
from __future__ import annotations

from .models import (
    Difficulty,
    Evidence,
    EvidenceStatus,
    Item,
    ItemBank,
    ItemBankVersion,
    ScoringRuleSet,
    Session,
    SessionResult,
    SessionState,
    SkillAssessment,
    SkillGraph,
    SkillGraphVersion,
    SkillNode,
)


def _enum_name(obj) -> str:
    return obj.name


def _difficulty_from(name: str) -> Difficulty:
    return Difficulty[name]


# ---------------------------------------------------------------------------
# 能力图谱
# ---------------------------------------------------------------------------

def graph_version_to_dict(v: SkillGraphVersion) -> dict:
    return {"entity_id": v.entity_id, "display_name": v.display_name,
            "revision": v.revision}


def graph_version_from_dict(d: dict) -> SkillGraphVersion:
    return SkillGraphVersion(d["entity_id"], d["display_name"], d["revision"])


def graph_to_dict(g: SkillGraph) -> dict:
    return {
        "version": graph_version_to_dict(g.version),
        "nodes": [
            {"skill_id": n.skill_id, "name": n.name,
             "prerequisites": list(n.prerequisites)}
            for n in (g.node(sid) for sid in g.skill_ids)
        ],
    }


def graph_from_dict(d: dict) -> SkillGraph:
    nodes = [
        SkillNode(n["skill_id"], n["name"], tuple(n.get("prerequisites", ())))
        for n in d["nodes"]
    ]
    return SkillGraph(graph_version_from_dict(d["version"]), nodes)


# ---------------------------------------------------------------------------
# 题库
# ---------------------------------------------------------------------------

def bank_version_to_dict(v: ItemBankVersion) -> dict:
    return {"entity_id": v.entity_id, "display_name": v.display_name,
            "revision": v.revision}


def bank_version_from_dict(d: dict) -> ItemBankVersion:
    return ItemBankVersion(d["entity_id"], d["display_name"], d["revision"])


def item_to_dict(it: Item) -> dict:
    return {
        "item_id": it.item_id,
        "skill_id": it.skill_id,
        "difficulty": _enum_name(it.difficulty),
        "prompt": it.prompt,
        "answer": it.answer,
        "max_exposure": it.max_exposure,
        "active": it.active,
    }


def item_from_dict(d: dict) -> Item:
    return Item(
        item_id=d["item_id"],
        skill_id=d["skill_id"],
        difficulty=_difficulty_from(d["difficulty"]),
        prompt=d["prompt"],
        answer=d["answer"],
        max_exposure=d["max_exposure"],
        active=d.get("active", True),
    )


def bank_to_dict(b: ItemBank) -> dict:
    return {
        "version": bank_version_to_dict(b.version),
        "graph_version_key": b.graph_version_key,
        "items": [item_to_dict(it) for it in b.items],
    }


def bank_from_dict(d: dict) -> ItemBank:
    return ItemBank(
        bank_version_from_dict(d["version"]),
        [item_from_dict(x) for x in d["items"]],
        d["graph_version_key"],
    )


# ---------------------------------------------------------------------------
# 评分规则
# ---------------------------------------------------------------------------

def rules_to_dict(r: ScoringRuleSet) -> dict:
    return {
        "rule_set_id": r.rule_set_id,
        "revision": r.revision,
        "levels": list(r.levels),
        "thresholds": list(r.thresholds),
        "confident_successes": r.confident_successes,
        "confident_failures": r.confident_failures,
        "theta_prior": r.theta_prior,
        "convergence_std": r.convergence_std,
        "max_items": r.max_items,
        "min_items": r.min_items,
    }


def rules_from_dict(d: dict) -> ScoringRuleSet:
    return ScoringRuleSet(
        rule_set_id=d["rule_set_id"],
        revision=d["revision"],
        levels=tuple(d["levels"]),
        thresholds=tuple(d["thresholds"]),
        confident_successes=d.get("confident_successes", 2),
        confident_failures=d.get("confident_failures", 2),
        theta_prior=d.get("theta_prior", 3.0),
        convergence_std=d.get("convergence_std", 0.6),
        max_items=d.get("max_items", 30),
        min_items=d.get("min_items", 5),
    )


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------

def evidence_to_dict(e: Evidence) -> dict:
    return {
        "item_id": e.item_id,
        "skill_id": e.skill_id,
        "difficulty": _enum_name(e.difficulty),
        "status": e.status.name,
        "presented_at": e.presented_at,
        "request_id": e.request_id,
        "selection_reason": e.selection_reason,
        "token": e.token,
        "answer_request_id": e.answer_request_id,
        "answer_given": e.answer_given,
        "is_correct": e.is_correct,
        "answered_at": e.answered_at,
        "void_reason": e.void_reason,
        "voided_by": e.voided_by,
        "voided_at": e.voided_at,
        "contribution": e.contribution,
    }


def evidence_from_dict(d: dict) -> Evidence:
    return Evidence(
        item_id=d["item_id"],
        skill_id=d["skill_id"],
        difficulty=_difficulty_from(d["difficulty"]),
        status=EvidenceStatus[d["status"]],
        presented_at=d["presented_at"],
        request_id=d["request_id"],
        selection_reason=d["selection_reason"],
        token=d["token"],
        answer_request_id=d.get("answer_request_id"),
        answer_given=d.get("answer_given"),
        is_correct=d.get("is_correct"),
        answered_at=d.get("answered_at"),
        void_reason=d.get("void_reason"),
        voided_by=d.get("voided_by"),
        voided_at=d.get("voided_at"),
        contribution=d.get("contribution"),
    )


def assessment_to_dict(a: SkillAssessment) -> dict:
    return {
        "skill_id": a.skill_id,
        "theta": a.theta,
        "sigma": a.sigma,
        "mastered": a.mastered,
        "gap": a.gap,
        "support": a.support,
        "successes": a.successes,
        "failures": a.failures,
        "gap_propagated_from": list(a.gap_propagated_from),
    }


def assessment_from_dict(d: dict) -> SkillAssessment:
    return SkillAssessment(
        skill_id=d["skill_id"],
        theta=d["theta"],
        sigma=d["sigma"],
        mastered=d["mastered"],
        gap=d["gap"],
        support=d["support"],
        successes=d["successes"],
        failures=d["failures"],
        gap_propagated_from=tuple(d.get("gap_propagated_from", ())),
    )


def result_to_dict(r: SessionResult) -> dict:
    return {
        "overall_theta": r.overall_theta,
        "overall_sigma": r.overall_sigma,
        "level": r.level,
        "rule_set_key": r.rule_set_key,
        "graph_version_key": r.graph_version_key,
        "bank_version_key": r.bank_version_key,
        "finished_at": r.finished_at,
        "item_count": r.item_count,
        "skill_assessments": [assessment_to_dict(a)
                              for a in r.skill_assessments],
        "regrade_of": r.regrade_of,
        "level_basis": r.level_basis,
    }


def result_from_dict(d: dict) -> SessionResult:
    return SessionResult(
        overall_theta=d["overall_theta"],
        overall_sigma=d["overall_sigma"],
        level=d["level"],
        rule_set_key=d["rule_set_key"],
        graph_version_key=d["graph_version_key"],
        bank_version_key=d["bank_version_key"],
        finished_at=d["finished_at"],
        item_count=d["item_count"],
        skill_assessments=tuple(
            assessment_from_dict(a) for a in d["skill_assessments"]
        ),
        regrade_of=d.get("regrade_of"),
        level_basis=d.get("level_basis"),
    )


def session_to_dict(s: Session) -> dict:
    return {
        "session_id": s.session_id,
        "learner_id": s.learner_id,
        "graph_version_key": s.graph_version_key,
        "bank_version_key": s.bank_version_key,
        "rule_set_key": s.rule_set_key,
        "created_at": s.created_at,
        "state": s.state.name,
        "evidence": [evidence_to_dict(e) for e in s.evidence],
        "result": result_to_dict(s.result) if s.result else None,
        "result_history": [result_to_dict(r) for r in s.result_history],
        "regrade_count": s.regrade_count,
        "audit_log": list(s.audit_log),
    }


def session_from_dict(d: dict) -> Session:
    return Session(
        session_id=d["session_id"],
        learner_id=d["learner_id"],
        graph_version_key=d["graph_version_key"],
        bank_version_key=d["bank_version_key"],
        rule_set_key=d["rule_set_key"],
        created_at=d["created_at"],
        state=SessionState[d.get("state", "IN_PROGRESS")],
        evidence=[evidence_from_dict(e) for e in d.get("evidence", [])],
        result=result_from_dict(d["result"]) if d.get("result") else None,
        result_history=[result_from_dict(r)
                        for r in d.get("result_history", [])],
        regrade_count=d.get("regrade_count", 0),
        audit_log=list(d.get("audit_log", [])),
    )
