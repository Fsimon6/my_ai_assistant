"""
AI角色API路由
"""
from typing import List
from fastapi import APIRouter, HTTPException, Depends, Query
from requests import Session

from backend.database.base import get_db
from backend.utils.auth import get_current_active_user
from backend.services.character_service import character_service
from backend.services.conversation_service import conversation_service
from backend.schemas.character import (
    CharacterCreate,
    CharacterUpdate,
    CharacterResponse,
    CharacterStatsResponse,
    BatchSpeakResponse,
    SpeakRequest,
    BatchSpeakRequest,
    SpeakResponse,
)
from backend.utils.response import api_response
from backend.models.user import User

router = APIRouter(prefix="/characters", tags=["AI角色管理"])


@router.post("/", response_model=CharacterResponse)
async def create_character(
        character_data: CharacterCreate,
        current_user: User = Depends(get_current_active_user),
        db: Session = Depends(get_db)
):
    """
    创建新的AI角色

    - **name**: 角色名称
    - **system_prompt**: 系统提示词
    - **model**: 使用的大模型
    - **api_key**: API密钥
    """
    result = character_service.create_character(
        name=character_data.name,
        system_prompt=character_data.system_prompt,
        model=character_data.model,
        api_key=character_data.api_key,
        user_id=current_user.id,
    )

    if not result['success']:
        raise HTTPException(status_code=400, detail=result.get('error', '创建失败'))

    character = result['character']
    return api_response.success(
        data={
            'id': result['character_id'],
            'name': character.name,
            'system_prompt': character.system_prompt,
            'model': character.model,
            'conversation_count': character.total_conversations or 0,
            'created_at': character.created_at.isoformat() if character.created_at else None,
            'update_at': character.updated_at.isoformat() if character.updated_at else None,
        },
        text='用户创建成功'
    )


@router.get("/", response_model=List[CharacterResponse])
async def list_characters(
        current_user: User = Depends(get_current_active_user),
        db=Depends(get_db)
):
    """获取当前用户的所有AI角色列表（需要登录）"""
    characters = character_service.list_characters(user_id=current_user.id)

    return api_response.success(
        data=characters,
        text=f'获取到{len(characters)}个角色'
    )

    #
    # # 分页
    # start = (page - 1) * page_size
    # end = start + page_size
    # paginated_characters = characters[start:end]
    #
    # return api_response.success(
    #     data=paginated_characters,
    #     text=f'获取到{len(paginated_characters)}个角色',
    #     meta={
    #         'total': len(characters),
    #         'page': page,
    #         'page_size': page_size,
    #         'total_page': (len(characters) + page_size - 1) // page_size,
    #     }
    # )


@router.get("/{character_id}", response_model=CharacterResponse)
async def get_character(
        character_id: str,
        current_user: User = Depends(get_current_active_user),
        db: Session = Depends(get_db)
):
    """获取特定AI角色的信息（需要登录，仅限本人）"""
    character = character_service.get_character(character_id, user_id=current_user.id)
    if character is None:
        raise HTTPException(status_code=404, detail='角色不存在')

    return api_response.success(
        data={
            'id': character_id,
            'name': character.name,
            'system_prompt': character.system_prompt,
            'model': character.model,
            'conversation_count': character.total_conversations or 0,
            'conversation_history': [],
            'created_at': character.created_at.isoformat() if character.created_at else None,
            'update_at': character.updated_at.isoformat() if character.updated_at else None,
        },
        text='角色信息获取成功'
    )


@router.put("/{character_id}", response_model=CharacterResponse)
async def update_character(
        character_id: str,
        character_data: CharacterUpdate,
        current_user: User = Depends(get_current_active_user),
        db: Session = Depends(get_db)
):
    """更新AI角色（需要登录，仅限本人）"""
    result = character_service.update_character(
        character_id,
        name=character_data.name,
        system_prompt=character_data.system_prompt,
        model=character_data.model,
        api_key=character_data.api_key,
        user_id=current_user.id,
    )
    if not result['success']:
        raise HTTPException(status_code=404, detail=result.get('error', '角色不存在'))

    character = result['character']
    return api_response.success(
        data={
            'id': character_id,
            'name': character.name,
            'system_prompt': character.system_prompt,
            'model': character.model,
            'conversation_count': character.total_conversations or 0,
            'created_at': character.created_at.isoformat() if character.created_at else None,
            'update_at': character.updated_at.isoformat() if character.updated_at else None,
        },
        text='角色更新成功'
    )


@router.delete("/{character_id}")
async def delete_character(
        character_id: str,
        current_user: User = Depends(get_current_active_user),
        db: Session = Depends(get_db)
):
    """删除AI角色（需要登录，仅限本人）"""
    result = character_service.delete_character(character_id, user_id=current_user.id)
    if not result['success']:
        raise HTTPException(status_code=404, detail=result.get('error', '角色不存在'))

    return api_response.success(
        data=None,
        text='角色删除成功'
    )


@router.post("/{chatacter_id}/speak", response_model=SpeakResponse)
async def speak_to_character(
        character_id: str,
        request: SpeakRequest,
        current_user: User = Depends(get_current_active_user),
        db = Depends(get_db)
):
    """与AI角色对话（需要登录，仅限本人）"""
    result = character_service.speak(character_id, request.text, user_id=current_user.id)

    if not result['success']:
        raise HTTPException(status_code=400, detail=result.get('error', '对话失败'))

    return api_response.success(
        data={
            'success': True,
            'character_id': character_id,
            'response': result['response'],
            'timestamp': result['timestamp']
        },
        text='对话完成'
    )


@router.post('/{character_id}/batch-speak/', response_model=BatchSpeakResponse)
async def batch_speak_to_character(
        character_id: str,
        batch_speak_request: BatchSpeakRequest,
        current_user: User = Depends(get_current_active_user)
):
    """
    与AI角色进行批量对话

    - **character_id**: 角色ID
    - **text**: 用户消息列表
    """
    result = character_service.batch_speak(
        character_id=character_id,
        text=batch_speak_request.text,
        user_id=current_user.id
    )

    if not result['success']:
        raise HTTPException(status_code=400, detail=result['error'])

    return api_response.success(
        data={
            'success': True,
            'responses': result['responses'],
            'total': result['total'],
            'success_count': result['success_count'],
        },
        message='批量对话完成'
    )


@router.get("/{character_id}/stats", response_model=CharacterStatsResponse)
async def get_character_stats(
        character_id: str,
        current_user: User = Depends(get_current_active_user)
):
    """
    获取AI角色的对话统计信息

    - **character_id**: 角色ID
    """
    result = character_service.get_character_stats(character_id, user_id=current_user.id)

    if not result['success']:
        raise HTTPException(status_code=400, detail=result['error'])

    # 移除success字段
    result_data = {k: v for k, v in result.items() if k != 'success'}

    return api_response.success(
        data=result_data,
        text='统计信息获取成功'
    )


@router.get("/{character_id}/conversations")
async def get_character_conversations(
        character_id: str,
        limit: int = Query(10, ge=1, le=100, description='限制返回数量'),
        offset: int = Query(0, ge=0, description='偏移量'),
        current_user: User = Depends(get_current_active_user),
        db: Session = Depends(get_db)
):
    """
    获取当前用户与某角色的对话历史（第二十阶段：从 conversations/texts 表读取）
    :param character_id: 角色ID
    :param limit: 限制返回数量
    :param offset: 偏移量
    """
    character = character_service.get_character(character_id, user_id=current_user.id)
    if character is None:
        raise HTTPException(status_code=404, detail='角色不存在')

    # 读取该 (用户, 角色) 的当前对话历史（conversations/texts 表）
    conv_id = conversation_service.get_existing_conversation_id(current_user.id, character.id)
    conversations = []
    total = 0
    if conv_id is not None:
        history = conversation_service.get_history(conv_id)
        total = len(history)
        conversations = history[offset:offset + limit]

    return api_response.success(
        data={
            'character_id': character_id,
            'character_name': character.name,
            'conversations': conversations,
            'total': total,
            'limit': limit,
            'offset': offset
        },
        text='对话历史获取成功'
    )


@router.delete("/{character_id}/conversations")
async def clear_character_conversation(
        character_id: str,
        current_user: User = Depends(get_current_active_user),
        db: Session = Depends(get_db)
):
    """清空当前用户与某角色的对话历史（删除 Conversation 及全部 Text，仅限本人，幂等）"""
    character = character_service.get_character(character_id, user_id=current_user.id)
    if character is None:
        raise HTTPException(status_code=404, detail='角色不存在')

    conversation_service.clear_conversation(current_user.id, character.id)

    return api_response.success(data=None, text='对话已清空')
