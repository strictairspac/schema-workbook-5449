from sqlalchemy import select
from fastapi import HTTPException
from src.models.entities import Relation,Dependency
from src.services import reference_store
from src.utils.serialization import record
from .common import save
def listing(db): return [record(x) for x in db.scalars(select(Relation).order_by(Relation.id))]
def create(db,data,user):
    obj=Relation(**data.model_dump())
    result=save(db,obj,user,'创建关系草案 '+data.code)
    # 登记进引用引擎（新草案尚无规则，版本为 0）
    reference_store.get_engine(db).register_relation(obj.id)
    return result
def update(db,ident,data,user):
    obj=db.get(Relation,ident)
    if not obj:raise HTTPException(404,'关系不存在')
    deps=list(db.scalars(select(Dependency).where(Dependency.relation_id==ident)))
    if any(not set(x.lhs+x.rhs)<=set(data.attributes) for x in deps):raise HTTPException(422,'现有依赖引用被移除属性，请先修改依赖')
    for k,v in data.model_dump().items():setattr(obj,k,v)
    return save(db,obj,user,'更新关系草案 '+data.code)
