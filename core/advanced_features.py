"""
高级AI功能模块

包含AI角色的高级功能扩展
1. 对话情感分析
2. 对话统计功能
3. 批量对话处理
"""

from typing import List, Dict, Any
from datetime import datetime, timedelta
from character import AICharacter, AdvancedAICharacterq
from core import character


class AIConversationAnalyzer:
    """AI对话分析器"""

    def __init__(self, character: AICharacter):
        self.character = character

    def get_conversation_stats(self) -> Dict[str, Any]:
        """获取对话统计信息"""
        if not self.character.conversation_history:
            return {'total': 0, 'message': '暂无对话记录'}

        total = len(self.character.conversation_history)
        today = datetime.now().date()

        # 计算今日对话
        today_conversation = []
        for conv in self.character.conversation_history:
            try:
                # 确保时间戳格式正确
                conv_time = datetime.fromisoformat(conv['timestamp'].replace('Z', '+00:00'))
                if conv_time.date() == today:
                    today_conversation.append(conv)
            except (ValueError, KeyError):
                # 如果时间戳格式有问题，跳过
                continue

        # 计算对话长度
        total_user_words = 0
        total_assistant_words = 0

        for conv in self.character.conversation_history:
            total_user_words += len(conv.get('users', '').split())
            total_assistant_words += len(conv.get('assistant', '').split())

        # 获取第一个和最后一个对话的时间戳
        first_conv = None
        last_conv = None
        if self.character.conversation_history:
            first_conv = self.character.conversation_history[0].get('timestamp')
            last_conv = self.character.conversation_history[-1].get('timestamp')

        return {
            'total_conversation': total,
            'today_conversation': len(today_conversation),
            'avg_user_words': round(total_user_words / total, 2) if total > 0 else 0,
            'avg_assistant_words': round(total_assistant_words / total, 2) if total > 0 else 0,
            'first_conversation': first_conv,
            'last_conversation': last_conv,
        }

    def find_conversation_by_keyword(self, keyword: str) -> List[Dict]:
        """根据关键词查找对话"""
        result = []
        for conv in self.character.conversation_history:
            if keyword in conv['user'] or keyword in conv['assistant']:
                result.append(conv)
        return result

    def export_conversations(self, start_date: str = None, end_date: str = None) -> List[Dict]:
        """导出指定时间段的对话"""
        if not self.character.conversation_history:
            return []
        filtered = []
        for conv in self.character.conversation_history:
            conv_time = datetime.fromisoformat(conv('timestamp'))

            # 时间筛选
            if start_date and conv_time < datetime.fromisoformat(start_date):
                continue
            if end_date and conv_time > datetime.fromisoformat(end_date):
                continue

            filtered.append(conv)

        return filtered


class AIConversationBatchProcessor:
    """AI对话批量处理器"""

    @staticmethod
    def batch_create_characters(configs: List[Dict]) -> List[AICharacter]:
        """批量创建AI角色"""
        characters = []

        # 添加调试信息
        print(f'    接收到{len(configs)}个配置')

        for i, config in enumerate(configs):
            try:
                print(f'    处理第{i + 1}个配置：{config}')

                # 检查必要字段
                if 'name' not in config:
                    print(f'    ⚠️配置{i + 1}缺少name字段，跳过')
                    continue

                name = config['name'],
                system_prompt = config.get('prompt', '默认提示词'),
                model = config.get('model', 'gpt-3.5-turbo')

                # 创建角色
                if config.get('advanced', False):
                    role = config.get('role', 'assistant')
                    print(f'    创建高级角色：{name}，角色：{role}')

                    char = AdvancedAICharacter(
                        name=name,
                        system_prompt=system_prompt,
                        model=model,
                        role=role,
                    )
                else:
                    print(f'    创建普通角色：{name}')
                    char = AICharacter(
                        name=name,
                        system_prompt=system_prompt,
                        model=model,
                    )

                # 如果有API Key就设置
                if 'api_key' in config:
                    try:
                        char.api_key = config['api_key']
                    except ValueError as e:
                        print(f'    ⚠️设置API Key失败：{e}')

                characters.append(char)
                print(f'    ✅️成功创建角色：{name}')

            except Exception as e:
                print(f'    ❌️创建角色失败：{e}')
                continue

        print(f'    总共创建了{len(characters)}个角色')
        return characters

    @staticmethod
    def batch_converse(characters: List[AICharacter], message: str) -> Dict[str, Any]:
        """批量对话"""
        results = {}
        for char in characters:
            try:
                response = char.speak(message)
                results[char.name] = {
                    'response': response,
                    'success': True,
                }
            except Exception as e:
                results[char.name] = {
                    'response': response,
                    'success': False,
                }
        return results


class AICharacterManager:
    """AI角色管理器"""

    def __init__(self):
        self.characters: Dict[str, AICharacter] = {}

    def add_character(self, character: AICharacter) -> str:
        """添加角色"""
        if character.name in self.characters:
            raise ValueError(f'角色名{character.name}已存在')
        self.characters[character.name] = character
        return f'角色{character.name}添加成功'

    def remove_character(self, name: str) -> str:
        """移除角色"""
        if name not in self.characters:
            raise ValueError(f'角色"{name}"不存在')

        del self.characters[name]
        return f'角色{name}已移除'

    def get_characters(self, name: str) -> AICharacter:
        """获取角色"""
        return self.characters.get(name)

    def list_characters(self) -> List[Dict]:
        """列出所有角色"""
        return [
            {
                'name': char.name,
                'type': char.__class__.__name__,
                'conversations': len(char),
                'model': char.model
            }
            for char in self.characters.values()
        ]


# 测试代码
if __name__ == '__main__':
    print('=== 测试高级AI功能 ===\n')

    # 测试对话分析器
    print('1. 测试对话分析器：')
    ai = AICharacter('测试助手', '测试提示')
    ai.speak('你好')
    ai.speak('你叫什么名字？')

    analyzer = AIConversationAnalyzer(ai)
    stats = analyzer.get_conversation_stats()
    print(' 对话统计：')
    for key, value in stats.items():
        if key in ['avg_user_words', 'avg_assistant_words']:
            print(f'    {key}: {value:.2f}')
        else:
            print(f'    {key}: {value}')

    # 测试批量处理器
    print('\n2. 批量创建角色测试：')
    configs = [
        {'name': '助手1', 'prompt': '帮助用户1', 'advanced': False},
        {'name': '导师1', 'prompt': '教学导师', 'advanced': True, 'role': 'tutor'}
    ]

    characters = AIConversationBatchProcessor.batch_create_characters(configs)
    print(f'    创建了{len(characters)}个角色')

    # 测试批量对话
    if characters:
        print('\n3. 测试批量对话：')
        result = AIConversationBatchProcessor.batch_converse(characters, '你好')
        print('    批量对话结果：')
        for name, result in result.items():
            status = '✅️' if result['success'] else '❌️'
            response = result['response']
            preview = response[:60] + '...' if len(response) > 60 else response
            print(f'    {status} {name}: {preview}')
    else:
        print('\n3. 跳过批量对话测试（没有成功创建任何角色）')

    # 测试角色管理器
    print('\n4. 测试角色管理器：')
    manager = AICharacterManager()

    # 添加角色
    for char in characters:
        try:
            result = manager.add_character(char)
            print(f'    {result}')
        except ValueError as e:
            print(f'    ⚠️{e}')

    # 列出角色
    char_list = manager.list_characters()
    print(f'    管理器中有{len(char_list)}个角色：')
    for char_info in char_list:
        print(f'    {char_info["name"]}({char_info["type"]})')

        print('\n=== 测试完成===')
