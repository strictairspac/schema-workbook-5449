from fastapi import APIRouter,Depends
from src.config.database import db_session
from src.utils.security import current,writer
from src.schemas.reference import ReferenceInput,ReferenceMappingInput
from src.services import reference_store as service
router=APIRouter()

@router.get('/references')
def listing(db=Depends(db_session),user=Depends(current)):
    return service.reference_listing(db)

@router.post('/references',status_code=201)
def create(data:ReferenceInput,db=Depends(db_session),user=Depends(writer)):
    # 成环、合并属性、未知属性均在引擎内拒绝（422）
    return service.create_reference(db,data,user)

@router.put('/references/{ident}/mapping')
def update_mapping(ident:int,data:ReferenceMappingInput,db=Depends(db_session),user=Depends(writer)):
    return service.update_reference_mapping(db,ident,data,user)

@router.post('/references/{ident}/retry')
def retry(ident:int,db=Depends(db_session),user=Depends(writer)):
    # 重试幂等：已应用的版本不会重复产生规则
    return service.retry_reference(db,ident,user)

@router.post('/relations/{ident}/propagate')
def propagate(ident:int,db=Depends(db_session),user=Depends(writer)):
    return service.propagate(db,ident,user,action_label='手动触发引用链传播')

@router.get('/relations/{ident}/effective')
def effective(ident:int,db=Depends(db_session),user=Depends(current)):
    # 有效依赖视图：自有 + 已生效派生，含待检查/冲突状态与推导来源
    return service.effective_view(db,ident)
