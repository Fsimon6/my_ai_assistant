"""
提示词模板模块
包含系统提示词、用户提示词和各种模板
"""

from .template_loader import load_template, get_prompt_template
from .system_prompts import SYSTEM_PROMPTS, ROLES_CONFIG
from .chat_templates import CHAT_TEMPLATES

__all__ = [
    'load_template',
    'get_prompt_template',
    'SYSTEM_PROMPTS',
    'ROLES_CONFIG',
    'CHAT_TEMPLATES'
]
