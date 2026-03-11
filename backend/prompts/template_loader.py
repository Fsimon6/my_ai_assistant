"""
模块加载器
处理提示词模板的加载和渲染
"""
import os
import json
from typing import Dict, Any, Optional
from jinja2 import Template, Environment, FileSystemLoader
from pathlib import Path

# 模板目录
TEMPLATE_DIR = Path(__file__).parent / 'templates'

# 创建模板目录
TEMPLATE_DIR.mkdir(exist_ok=True)

# Jinja2环境
env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True
)


class PromptTemplate:
    """提示词模板类"""

    def __init__(self, name: str, template_str: str):
        self.name = name
        self.template = Template(template_str)

    def render(self, **kwargs) -> str:
        """渲染模板"""
        return self.template.render(**kwargs)

    @classmethod
    def from_file(cls, template_name: str) -> 'PromptTemplate':
        """从文件加载模板"""
        template_path = TEMPLATE_DIR / f'{template_name}.j2'
        if not template_path.exists():
            raise FileNotFoundError(f'Template {template_name} not found')

        with open(template_path, 'r', encoding='utf-8') as f:
            template_str = f.read()

        return cls(template_name, template_str)


def load_template(template_name: str) -> Optional[PromptTemplate]:
    """加载模板"""
    try:
        return PromptTemplate.from_file(template_name)
    except FileNotFoundError:
        return None


def get_prompt_template(template_name: str, context: Dict[str, Any]) -> str:
    """获取渲染后的提示词"""
    template = load_template(template_name)
    if template is None:
        raise ValueError(f'Template {template_name} not found')

    return template.render(**context)


# 预定义模板
DEFAULT_TEMPLATES = {
    'rag': """基于以下上下文回答问题。如果你不知道答案，就说不知道，不要编造。
    
    上下文：
    {% for doc in context %}
    [文档{{ loop.index }}]: {{ doc }}
    {% endfor %}
    
    问题：{{ question }}
    
    答案：""",

    'summary': """请总结以下文档内容
    
    文档：
    {{ document }}
    
    总结要求：
    1. 提取核心观点
    2. 保持原意不变
    3. 控制长度在200字以内
    
    总结：""",

    'chat': """
    历史对话：
    {% for msg in history %}
    {{ msg.role }}: {{ msg.context }}
    {% endfor %}
    
    当前问题：{{ question }}
    
    请根据历史对话回答当前问题："""
}


# 创建默认模板文件
def create_default_templates():
    """创建默认模板文件"""
    for name, content in DEFAULT_TEMPLATES.items():
        template_path = TEMPLATE_DIR / f'{name}.j2'
        if not template_path.exists():
            with open(template_path, 'w', encoding='utf-8') as f:
                f.write(content)


# 初始化时创建模板
create_default_templates()
