from sqlalchemy import select,func
from fastapi import HTTPException
from src.models.entities import Relation,Dependency,Audit
from src.services.references import FD,ReferenceError
from src.services import reference_store
from src.utils.serialization import record

def listing(db):return [record(x) for x in db.scalars(select(Dependency).order_by(Dependency.id))]

def validate(db,data,exclude=None):
    relation=db.get(Relation,data.relation_id)
    if not relation:raise HTTPException(404,'关系不存在')
    if not set(data.lhs+data.rhs)<=set(relation.attributes):raise HTTPException(422,'依赖属性必须属于选定关系')
    count=db.scalar(select(func.count()).select_from(Dependency).where(Dependency.relation_id==data.relation_id))
    if not exclude and count>=64:raise HTTPException(422,'最多64条函数依赖，请合并后重试')

def _sync_engine(db,obj):
    """把一条自有依赖的多 RHS 规则登记进引擎并提升草案版本。"""
    engine=reference_store.get_engine(db)
    engine.register_relation(obj.relation_id)
    existing={r.fd.signature for r in engine.own_rules.get(obj.relation_id,[])}
    for rhs in obj.rhs:
        fd=FD.make(obj.lhs,rhs)
        if fd.signature not in existing:
            engine.add_own_rule(obj.relation_id,fd,note=obj.note or '')
            existing.add(fd.signature)

def _commit_with_propagation(db,obj,user,action,engine_backup=None):
    """自有规则变更后的统一单事务处理。

    自有规则版本提升与沿引用链传播在同一事务里 flush；传播途中失败时，
    引擎恢复到变更前备份、DB 随异常整体回滚——整链保留旧版，绝不半新半旧。
    """
    engine=reference_store.get_engine(db)
    relation=db.get(Relation,obj.relation_id)
    relation.revision=engine.revisions[obj.relation_id]
    reference_store._persist_counters(db,engine)
    db.add(Audit(actor=user.email,
                 action=f'{action}（草案 {obj.relation_id} 版本 {relation.revision}）'))
    try:
        propagation=reference_store.propagate_uncommitted(db,obj.relation_id)
    except ReferenceError as exc:
        if engine_backup is not None:engine._restore_state(engine_backup)
        db.rollback();raise HTTPException(422,str(exc))
    db.commit()
    result=record(obj);result['reference_propagation']=propagation
    return result

def create(db,data,user):
    validate(db,data)
    obj=Dependency(**data.model_dump())
    db.add(obj)
    try: db.flush()
    except Exception: db.rollback();raise HTTPException(409,'依赖保存失败，请检查引用对象')
    engine=reference_store.get_engine(db)
    backup=engine._snapshot_state()
    _sync_engine(db,obj)
    return _commit_with_propagation(db,obj,user,'创建函数依赖',engine_backup=backup)

def update(db,ident,data,user):
    obj=db.get(Dependency,ident)
    if not obj:raise HTTPException(404,'依赖不存在')
    validate(db,data,ident)
    engine=reference_store.get_engine(db)
    # 在任何引擎改动前留备份，确保传播失败时连本次更正也一并回滚
    backup=engine._snapshot_state()
    old_signatures={(tuple(sorted(obj.lhs)),rhs) for rhs in obj.rhs}
    for k,v in data.model_dump().items():setattr(obj,k,v)
    db.flush()
    # 移除该依赖对应的旧自有规则，再登记更正后的规则；目标自有规则语义由其自身保持
    rules=engine.own_rules.setdefault(obj.relation_id,[])
    engine.own_rules[obj.relation_id]=[r for r in rules if r.fd.signature not in old_signatures]
    _sync_engine(db,obj)
    return _commit_with_propagation(db,obj,user,'更正函数依赖',engine_backup=backup)
