from sqlalchemy import Column,Integer,String,JSON,DateTime,ForeignKey,Text
from datetime import datetime,timezone
from src.config.database import Base
class User(Base):
    __tablename__='users'
    id=Column(Integer,primary_key=True);email=Column(String(120),unique=True,nullable=False);password=Column(String(300),nullable=False);role=Column(String(20),nullable=False)
class Relation(Base):
    __tablename__='relations'
    id=Column(Integer,primary_key=True);code=Column(String(80),unique=True,nullable=False);title=Column(String(160),nullable=False);attributes=Column(JSON,nullable=False);note=Column(String(300),default='');revision=Column(Integer,nullable=False,default=0)
class Dependency(Base):
    __tablename__='dependencies'
    id=Column(Integer,primary_key=True);relation_id=Column(Integer,ForeignKey('relations.id'),nullable=False);lhs=Column(JSON,nullable=False);rhs=Column(JSON,nullable=False);note=Column(String(300),default='')
class Reference(Base):
    """跨草案引用：source 草案属性经 mapping 映射到 target 草案。"""
    __tablename__='references'
    id=Column(Integer,primary_key=True)
    source_relation_id=Column(Integer,ForeignKey('relations.id'),nullable=False)
    target_relation_id=Column(Integer,ForeignKey('relations.id'),nullable=False)
    # {"源属性": "目标属性"}，只含被选取的属性
    mapping=Column(JSON,nullable=False)
    # pending（待检查）/ effective（已生效）/ conflict（冲突）
    status=Column(String(20),nullable=False,default='pending')
    conflict_reason=Column(Text,nullable=True)
    applied_source_revision=Column(Integer,nullable=True)
    # 已提交的派生规则版本：[[{lhs,rhs}, origin], ...]，传播成功才整体换版
    committed_derived=Column(JSON,nullable=False,default=list)
    created_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
class Report(Base):
    __tablename__='reports'
    id=Column(Integer,primary_key=True);name=Column(String(120),nullable=False);relation_id=Column(Integer,ForeignKey('relations.id'),nullable=False)
    status=Column(String(20),nullable=False,default='queued');input_snapshot=Column(JSON,nullable=False);result=Column(JSON,nullable=True);error=Column(Text,nullable=True)
    created_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
class Setting(Base):
    """进程级单调计数器（全局版本、引擎序列号），保证重启动后状态可重建。"""
    __tablename__='settings'
    key=Column(String(80),primary_key=True);value=Column(JSON,nullable=False)
class Audit(Base):
    __tablename__='audit_logs'
    id=Column(Integer,primary_key=True);actor=Column(String(120),nullable=False);action=Column(String(160),nullable=False);created_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
