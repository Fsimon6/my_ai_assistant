"""
聊天模板配置
包含不同场景的聊天模板
"""

CHAT_TEMPLATES = {
    "default": {
        "system": "你是一个有帮助的AI助手",
        "user": "{message}",
        "assistant": "{message}"
    },

    "code_helper": {
        "system": "你是一个编程专家，擅长多种编程语言和技术",
        "user": "编程问题：{question}\n相关代码：{code}",
        "assistant": "分析结果：{analysis}\n解决方案：{solution}\n示例代码：{example}"
    },

    "creative_writing": {
        "system": "你是一个创意写作助手，擅长故事创作和文案写作",
        "user": "写作要求：{requirement}\n主题：{theme}\n风格：{style}",
        "assistant": "大纲：{outline}\n正文：{content}\n修改建议：{suggestions}"
    },

    "analysis": {
        "system": "你是一个分析专家，擅长数据分析和逻辑推理",
        "user": "分析对象：{target}\n分析维度：{dimensions}\n数据：{data}",
        "assistant": "分析结果：{result}\n关键发现：{findings}\n建议：{recommendations}"
    }
}


def get_chat_template(template_name: str = 'default') -> dict:
    """获取聊天模板"""
    return CHAT_TEMPLATES.get(template_name, CHAT_TEMPLATES['default'])


def format_user_message(template_name: str, **kwargs) -> str:
    """格式化用户消息"""
    template = get_chat_template(template_name)
    user_template = template.get('user', '{message}')
    return user_template.format(**kwargs)


def format_assistant_message(template_name: str, **kwargs) -> str:
    """格式化助手消息"""
    template = get_chat_template(template_name)
    assistant_template = template.get('assistant', '{message}')
    return assistant_template.format(**kwargs)
