from sqlalchemy import select
from redis import Redis
from fastapi import HTTPException
from src.models.entities import Relation,Dependency,Report
from src.config.settings import REDIS_URL
from src.constants.enums import RUN_STATES
from src.services import reference_store
from src.utils.serialization import record
from .common import save
queue=Redis.from_url(REDIS_URL,decode_responses=True)
def listing(db):return [record(x) for x in db.scalars(select(Report).order_by(Report.id.desc()))]
def detail(db,ident):
    obj=db.get(Report,ident)
    if not obj:raise HTTPException(404,'报告不存在')
    return record(obj)
def create(db,data,user):
    relation=db.get(Relation,data.relation_id)
    if not relation:raise HTTPException(404,'关系不存在')
    # 受理瞬间通过引擎冻结一致版本：自有 + 已提交派生，全旧或全新，绝不混用。
    # 更正与传播在另一事务中原子换版，本快照读到的要么是换版前要么是换版后。
    frozen=reference_store.freeze_snapshot(db,relation.id)
    own_deps=list(db.scalars(select(Dependency).where(Dependency.relation_id==relation.id)))
    snapshot={
        'relation':record(relation),
        # 算法只消费 lhs/rhs；kind/origin 作为推导来源随快照留档
        'dependencies':[{'lhs':x['lhs'],'rhs':x['rhs']} for x in frozen['dependencies']],
        'dependency_origins':frozen['dependencies'],
        'reference_conflicts':frozen['conflicts'],
        'frozen_global_revision':frozen['global_revision'],
        'frozen_relation_revision':frozen['relation_revision'],
        'own_dependency_count':len(own_deps),
    }
    obj=Report(**data.model_dump(),input_snapshot=snapshot,status=RUN_STATES[0])
    result=save(db,obj,user,'范式分解入队 '+data.name+'（冻结版本 %s）'%frozen['global_revision'])
    try:queue.rpush('schemaworkbook:queue',str(obj.id))
    except Exception:
        obj.status='failed';obj.error='队列暂时不可用，请重新创建';db.commit();raise HTTPException(503,obj.error)
    return result
