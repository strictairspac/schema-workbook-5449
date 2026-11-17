"""引用引擎与 MySQL 的桥接层。

引擎是内存权威（纯函数、可精确测试）；本层负责：
* 进程启动/需要时从 relations/dependencies/references 重建引擎；
* 每次写操作把引擎的决定落库，传播按单事务提交，失败整链回滚；
* 报告受理时通过引擎拿到“全旧或全新”的一致有效视图并冻结。

约定：本层的 *_uncommitted 系列只 flush 不 commit，由调用方在最外层
（route/service）统一提交，保证“自有规则更正 + 整链传播”是一个事务。
"""
from __future__ import annotations

import threading
from fastapi import HTTPException
from sqlalchemy import select

from src.models.entities import (
    Relation, Dependency, Reference as ReferenceModel, Setting, Audit,
)
from src.services.references import ReferenceEngine, ReferenceError, FD, Rule, Reference

_engine: ReferenceEngine | None = None
_engine_lock = threading.RLock()

SETTING_GLOBAL_REV = "reference_global_revision"
SETTING_REF_SEQ = "reference_seq"
SETTING_RULE_SEQ = "engine_rule_seq"
SETTING_REPORT_SEQ = "engine_report_seq"


def _get_setting(db, key: str, default=0) -> int:
    obj = db.get(Setting, key)
    return int(obj.value) if obj else default


def _set_setting(db, key: str, value: int) -> None:
    obj = db.get(Setting, key)
    if obj:
        obj.value = value
    else:
        db.add(Setting(key=key, value=value))


def hydrate(db) -> ReferenceEngine:
    """从数据库完整重建引擎（含已提交派生规则与全部版本/序列号）。"""
    engine = ReferenceEngine()
    relations = list(db.scalars(select(Relation).order_by(Relation.id)))
    for rel in relations:
        engine.register_relation(rel.id)
        engine.revisions[rel.id] = rel.revision or 0
    for dep in db.scalars(select(Dependency).order_by(Dependency.id)):
        for rhs in dep.rhs:
            engine._rule_seq += 1
            engine.own_rules.setdefault(dep.relation_id, []).append(Rule(
                id=dep.id, relation_id=dep.relation_id,
                fd=FD.make(dep.lhs, rhs), kind="own",
                origin={"type": "own", "note": dep.note or "", "dependency_id": dep.id},
            ))
    max_ref_id = 0
    for ref in db.scalars(select(ReferenceModel).order_by(ReferenceModel.id)):
        max_ref_id = max(max_ref_id, ref.id)
        engine.references[ref.id] = Reference(
            id=ref.id,
            source_relation_id=ref.source_relation_id,
            target_relation_id=ref.target_relation_id,
            mapping=dict(ref.mapping),
            status=ref.status,
            conflict_reason=ref.conflict_reason,
            applied_source_revision=ref.applied_source_revision,
        )
        engine.committed_derived.setdefault(ref.target_relation_id, {})[ref.id] = [
            (FD.make(item["fd"]["lhs"], item["fd"]["rhs"]), item["origin"])
            for item in (ref.committed_derived or [])
        ]
        if ref.applied_source_revision is not None:
            engine.applied_intents.add((ref.id, ref.applied_source_revision))
    engine.global_revision = _get_setting(db, SETTING_GLOBAL_REV)
    engine._ref_seq = max(max_ref_id, _get_setting(db, SETTING_REF_SEQ))
    engine._rule_seq = max(engine._rule_seq, _get_setting(db, SETTING_RULE_SEQ))
    engine._report_seq = _get_setting(db, SETTING_REPORT_SEQ)
    return engine


def get_engine(db) -> ReferenceEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = hydrate(db)
        return _engine


def reset_engine() -> None:
    """测试或显式重建时清空内存引擎。"""
    global _engine
    with _engine_lock:
        _engine = None


def _persist_counters(db, engine: ReferenceEngine) -> None:
    _set_setting(db, SETTING_GLOBAL_REV, engine.global_revision)
    _set_setting(db, SETTING_REF_SEQ, engine._ref_seq)
    _set_setting(db, SETTING_RULE_SEQ, engine._rule_seq)
    _set_setting(db, SETTING_REPORT_SEQ, engine._report_seq)


def _persist_reference(db, engine: ReferenceEngine, ref_id: int) -> None:
    ref = engine.references[ref_id]
    entries = engine.committed_derived.get(ref.target_relation_id, {}).get(ref_id, [])
    model = db.get(ReferenceModel, ref_id)
    payload = {
        "source_relation_id": ref.source_relation_id,
        "target_relation_id": ref.target_relation_id,
        "mapping": ref.mapping,
        "status": ref.status,
        "conflict_reason": ref.conflict_reason,
        "applied_source_revision": ref.applied_source_revision,
        "committed_derived": [
            {"fd": fd.as_dict(), "origin": origin} for fd, origin in entries
        ],
    }
    if model:
        for key, value in payload.items():
            setattr(model, key, value)
    else:
        db.add(ReferenceModel(id=ref_id, **payload))


def _sync_relation_revisions(db, engine: ReferenceEngine) -> None:
    for rel_id, rev in engine.revisions.items():
        rel = db.get(Relation, rel_id)
        if rel and rel.revision != rev:
            rel.revision = rev


# --------------------------------------------------------------------------- #
# 服务操作
# --------------------------------------------------------------------------- #
def create_reference(db, data, user) -> dict:
    engine = get_engine(db)
    source = db.get(Relation, data.source_relation_id)
    target = db.get(Relation, data.target_relation_id)
    if not source or not target:
        raise HTTPException(404, "源或目标草案不存在")
    try:
        with engine._lock:
            ref = engine.add_reference(
                data.source_relation_id, data.target_relation_id,
                dict(data.mapping), list(source.attributes), list(target.attributes),
            )
            _persist_reference(db, engine, ref.id)
            _persist_counters(db, engine)
            _sync_relation_revisions(db, engine)
            db.add(Audit(actor=user.email, action=f"建立跨草案引用 #{ref.id}（待检查）"))
            db.commit()
    except ReferenceError as exc:
        db.rollback()
        raise HTTPException(422, str(exc))
    return engine._ref_dict(ref)


def update_reference_mapping(db, ref_id: int, data, user) -> dict:
    engine = get_engine(db)
    ref_model = db.get(ReferenceModel, ref_id)
    if not ref_model:
        raise HTTPException(404, "引用不存在")
    source = db.get(Relation, ref_model.source_relation_id)
    target = db.get(Relation, ref_model.target_relation_id)
    try:
        with engine._lock:
            ref = engine.update_mapping(
                ref_id, dict(data.mapping),
                list(source.attributes), list(target.attributes),
            )
            _persist_reference(db, engine, ref_id)
            _persist_counters(db, engine)
            _sync_relation_revisions(db, engine)
            db.add(Audit(actor=user.email, action=f"调整引用 #{ref_id} 映射，转为待检查"))
            db.commit()
    except ReferenceError as exc:
        db.rollback()
        raise HTTPException(422, str(exc))
    return engine._ref_dict(ref)


def propagate_uncommitted(db, start_relation_id: int) -> dict:
    """沿引用链传播，只 flush 不 commit；失败恢复引擎旧版并上抛，由外层回滚。"""
    engine = get_engine(db)
    if not db.get(Relation, start_relation_id):
        raise HTTPException(404, "草案不存在")
    with engine._lock:
        backup = engine._snapshot_state()
        try:
            result = engine.propagate(start_relation_id)
        except Exception:
            engine._restore_state(backup)
            raise
        for ref_id in engine.references:
            _persist_reference(db, engine, ref_id)
        _sync_relation_revisions(db, engine)
        _persist_counters(db, engine)
        db.flush()
        return result


def propagate(db, start_relation_id: int, user, action_label: str = "传播源规则更正") -> dict:
    """独立传播入口（如手动重试）：自带事务与审计。"""
    try:
        result = propagate_uncommitted(db, start_relation_id)
    except ReferenceError as exc:
        db.rollback()
        raise HTTPException(422, str(exc))
    db.add(Audit(actor=user.email,
                 action=f"{action_label}：草案 {start_relation_id}，全局版本 {result['global_revision']}"))
    db.commit()
    return result


def retry_reference(db, ref_id: int, user) -> dict:
    engine = get_engine(db)
    if ref_id not in engine.references:
        raise HTTPException(404, "引用不存在")
    source_id = engine.references[ref_id].source_relation_id
    return propagate(db, source_id, user, action_label=f"重试引用 #{ref_id}")


def reference_listing(db) -> list[dict]:
    return get_engine(db).reference_listing()


def effective_view(db, relation_id: int) -> dict:
    if not db.get(Relation, relation_id):
        raise HTTPException(404, "草案不存在")
    return get_engine(db).effective_view(relation_id)


def freeze_snapshot(db, relation_id: int) -> dict:
    """报告受理：冻结一个一致版本的有效依赖（全旧或全新，绝不混用）。"""
    engine = get_engine(db)
    if not db.get(Relation, relation_id):
        raise HTTPException(404, "草案不存在")
    with engine._lock:
        view = engine.effective_view(relation_id)
        return {
            "relation_id": relation_id,
            "global_revision": engine.global_revision,
            "relation_revision": engine.revisions.get(relation_id, 0),
            "dependencies": [
                {"lhs": item["fd"]["lhs"], "rhs": [item["fd"]["rhs"]],
                 "kind": item["kind"], "origin": item["origin"]}
                for item in view["rules"]
            ],
            "conflicts": view["conflicts"],
        }
