from sqlalchemy import select,func
from src.config.database import Session,Base,engine
from src.models.entities import User,Relation,Dependency,Reference,Report
from src.services.references import FD
from src.services import reference_store
from src.utils.security import password_hash

def seed():
    Base.metadata.create_all(engine)
    with Session() as db:
        for role in ('admin','operator','viewer'):
            email=role+'@nanlu.test'
            if not db.scalar(select(User).where(User.email==email)):db.add(User(email=email,password=password_hash('Demo2026!'),role=role))
        cases=[('NL-1117-A','材料检测委托单',['委托号','客户','检测项','方法','结果'],[(['委托号'],['客户']),(['检测项'],['方法']),(['委托号','检测项'],['结果'])]),('NL-1117-B','循环编号与独立属性',['A','B','C'],[(['A'],['B']),(['B'],['A'])]),('NL-1117-C','尚未确认业务依赖',['批号','容器','记录人'],[])]
        for code,title,attrs,fds in cases:
            rel=db.scalar(select(Relation).where(Relation.code==code))
            if rel:continue
            rel=Relation(code=code,title=title,attributes=attrs,note='南麓材料实验室 · 09:15 委托表复核');db.add(rel);db.flush()
            for lhs,rhs in fds:db.add(Dependency(relation_id=rel.id,lhs=lhs,rhs=rhs,note='业务确认依赖'))
        db.commit()
        _seed_references(db)
        print({cls.__tablename__:db.scalar(select(func.count()).select_from(cls)) for cls in (User,Relation,Dependency,Reference,Report)})

def _seed_references(db):
    """跨草案复用演示：菱形引用 + 经未选属性传递 + 空决定项。"""
    reference_store.reset_engine()
    # 菱形顶端：共享客户主数据草案
    master=db.scalar(select(Relation).where(Relation.code=='NL-1117-M'))
    if master:return
    master=Relation(code='NL-1117-M',title='共享客户主数据',attributes=['客户编号','客户名称','信用等级'],note='多份委托草案复用的源规则',revision=0)
    db.add(master);db.flush()
    db.add(Dependency(relation_id=master.id,lhs=['客户编号'],rhs=['客户名称','信用等级'],note='主数据确认依赖'))
    # 空决定项（常量依赖）：实验室默认校准状态
    db.add(Dependency(relation_id=master.id,lhs=[],rhs=['信用等级'],note='隐含空决定项：默认信用等级'))
    # 两个下游草案，构成菱形的两条边
    left=Relation(code='NL-1117-L',title='化学检测委托（复用客户）',attributes=['客户ID','客户名','评级','样品'],note='',revision=0)
    right=Relation(code='NL-1117-R',title='物理检测委托（复用客户）',attributes=['客户键','客户','等级','设备'],note='',revision=0)
    db.add_all([left,right]);db.flush()
    db.commit()
    engine=reference_store.get_engine(db)
    # 源自有规则入引擎
    for dep in db.scalars(select(Dependency).where(Dependency.relation_id==master.id)):
        for rhs in dep.rhs:
            engine.add_own_rule(master.id,FD.make(dep.lhs,rhs),note=dep.note or '')
    class _U:email='admin@nanlu.test'
    class _In:
        def __init__(self,s,t,m):self.source_relation_id=s;self.target_relation_id=t;self.mapping=m
    # 两条引用选取不同目标属性名，演示属性映射
    reference_store.create_reference(db,_In(master.id,left.id,{'客户编号':'客户ID','客户名称':'客户名','信用等级':'评级'}),_U())
    reference_store.create_reference(db,_In(master.id,right.id,{'客户编号':'客户键','客户名称':'客户','信用等级':'等级'}),_U())
    reference_store.propagate(db,master.id,_U(),action_label='种子数据初始化引用链')
    db.commit()

if __name__=='__main__':seed()
