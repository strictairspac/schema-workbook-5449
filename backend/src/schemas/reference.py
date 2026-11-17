from pydantic import BaseModel,Field,field_validator

class ReferenceInput(BaseModel):
    source_relation_id:int=Field(gt=0)
    target_relation_id:int=Field(gt=0)
    # 源属性 -> 目标属性；只包含被选取参与映射的属性
    mapping:dict[str,str]=Field(min_length=1)
    @field_validator('mapping')
    @classmethod
    def mapping_valid(cls,value):
        if any(not str(src).strip() or not str(dst).strip() for src,dst in value.items()):
            raise ValueError('映射属性不能为空')
        if len(set(value.values()))!=len(value.values()):
            raise ValueError('多个源属性不能映射到同一目标属性（合并属性）')
        return value

class ReferenceMappingInput(BaseModel):
    mapping:dict[str,str]=Field(min_length=1)
    @field_validator('mapping')
    @classmethod
    def mapping_valid(cls,value):
        if any(not str(src).strip() or not str(dst).strip() for src,dst in value.items()):
            raise ValueError('映射属性不能为空')
        if len(set(value.values()))!=len(value.values()):
            raise ValueError('多个源属性不能映射到同一目标属性（合并属性）')
        return value
