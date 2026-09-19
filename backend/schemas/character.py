"""
AI角色相关的Pydantic模式
"""
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, validator, model_validator, Field
from datetime import datetime


class CharacterBase(BaseModel):
    """角色基础模式"""
    name: str = Field(..., min_length=1, max_length=50, description='角色名称')
    system_prompt: str = Field(..., min_length=1, max_length=1000, description='系统提示词')
    model: Optional[str] = Field(None, description='使用的大模型（留空则使用系统默认模型）')


class CharacterCreate(CharacterBase):
    """创建角色请求模式"""
    api_key: Optional[str] = Field(None, description='API密钥')

    @validator('api_key')
    def validate_api_key(cls, v):
        if v and len(v) < 10:
            raise ValueError('API密钥太短')
        return v

    @model_validator(mode='after')
    def check_model_api_key_pair(self):
        """模型与 API Key 必须同时填写或同时留空（方案 A）"""
        model = (self.model or '').strip()
        api_key = (self.api_key or '').strip()
        if bool(model) != bool(api_key):
            raise ValueError('模型和 API Key 必须同时填写或同时留空')
        return self


class CharacterUpdate(BaseModel):
    """更新角色请求模式"""
    name: Optional[str] = Field(None, min_length=1, max_length=50, description='角色名称')
    system_prompt: Optional[str] = Field(None, min_length=1, max_length=1000, description='系统提示词')
    model: Optional[str] = Field(None, description='使用的大模型')
    api_key: Optional[str] = Field(None, description='API密钥')

    @validator('api_key')
    def validate_api_key(cls, v):
        if v and len(v) < 10:
            raise ValueError('API密钥太短')
        return v

    @model_validator(mode='after')
    def check_model_api_key_pair(self):
        """模型与 API Key 必须同时填写或同时留空（编辑同样适用）

        编辑为部分更新：若请求体仅携带 name（model/api_key 均为 None），视为不修改
        模型配置，校验通过；若 model 或 api_key 任一带值而另一个为 None/空，则最终存储态
        会破坏“同填同空”约束，直接拒绝。
        """
        model = (self.model or '').strip()
        api_key = (self.api_key or '').strip()
        if bool(model) != bool(api_key):
            raise ValueError('模型和 API Key 必须同时填写或同时留空')
        return self


class ConversationRecord(BaseModel):
    """对话记录模式"""
    timestamp: str = Field(..., description='时间戳')
    user: str = Field(..., description='用户消息')
    assistant: str = Field(..., description='AI回复')
    model: str = Field(..., description='使用的模型')


class CharacterResponse(CharacterBase):
    """角色响应模式"""
    id: str = Field(..., description='角色ID')
    conversation_count: int = Field(..., description='对话数量')
    created_at: str = Field(..., description='创建时间')
    update_at: Optional[str] = Field(..., description='更新时间')

    class Config:
        from_attributes = True


class CharacterDetailResponse(CharacterResponse):
    """角色详情响应模式"""
    conversations_history: List[ConversationRecord] = Field(default_factory=list, description='已使用的token数量')

    class Config:
        from_attributes = True


class SpeakRequest(BaseModel):
    """对话请求模式"""
    text: str = Field(..., min_length=1, max_length=1000, description='用户消息')
    character_id: str = Field(..., description='角色ID')


class SpeakResponse(BaseModel):
    """对话响应模式"""
    success: bool = Field(..., description='是否成功')
    text: str = Field(..., description='响应消息')
    response: str = Field(..., description='AI回复')
    character_id: str = Field(..., description='角色ID')
    timestamp: str = Field(..., description='时间戳')


class BatchSpeakRequest(BaseModel):
    """批量对话请求模式"""
    text: List[str] = Field(..., min_length=1, max_length=10, description='用户信息列表')
    character_id: str = Field(..., description='角色ID')


class BatchSpeakResponse(BaseModel):
    """批量对话响应模式"""
    success: bool = Field(..., description='是否成功')
    response: List[SpeakResponse] = Field(..., description='响应列表')
    total: int = Field(..., description='总数量')
    success_count: int = Field(..., description='成功数量')


class CharacterStatsResponse(BaseModel):
    """角色统计响应模式"""
    character_id: str = Field(..., description='角色ID')
    total_conversations: int = Field(..., description='总对话数')
    today_conversations: int = Field(..., description='今日对话数')
    avg_user_words: int = Field(..., description='平均用户词数')
    avg_assistant_words: int = Field(..., description='平均AI回复词数')
    first_conversation: Optional[str] = Field(..., description='首次对话时间')
    last_conversation: Optional[str] = Field(..., description='最后对话时间')
    token_used: Optional[str] = Field(..., description='已使用token数')
