"""
AI角色类模块
模拟一个具有个性和记忆的对话角色
"""
from datetime import datetime
from typing import List, Dict, Optional, Union, Tuple

MAX_NAME_LENGTH = 50
MAX_PROMPT_LENGTH = 1000
MIN_API_KEY_LENGTH = 20
DEFAULT_MODEL = 'gpt-3.5-turbo'


class AICharacter:
    """
    一个AI角色

    属性：
    name(str):角色名字
    system_prompt(str):定义角色行为的系统提示词
    conversation_history(list):对话历史，每个元素是(speaker, message)元组
    """

    # ========装饰器定义========
    @staticmethod
    def validate_input(func):
        """装饰器：验证speak方法的输入"""

        def wrapper(self, text, *args, **kwargs):
            if not isinstance(text, str):
                raise TypeError(f'输入必须是字符串，而不是{type(text).__name__}')
            if not text.strip():
                raise TypeError('输入不能位空')
            if len(text) > 1000:
                raise TypeError('输入过长，请控制在1000字符以内')
            return func(self, text, *args, **kwargs)

        return wrapper

    def __init__(self, name: str, system_prompt: str, model: str = 'gpt-3.5-turbo'):
        # 使用带下划线的私有属性
        self._name = name
        self.system_prompt = system_prompt
        self.model = model
        # 创建一个空列表来储存历史对话
        self.conversation_history = []
        self.created_at = datetime.now()

        # 完全私有的属性
        self.__api_key = None
        self.__rate_limit = 1000

    # ========属性装饰器示例========

    @property
    def name(self):
        """获取角色名"""
        return self._name

    @name.setter
    def name(self, value):
        """设置角色名，带有验证"""
        if not value or not value.strip():
            raise ValueError('角色名不能为空')
        if len(value) > 50:
            raise ValueError('角色名不能超过50个字符')
        self._name = value.strip()

    @property
    def system_prompt(self):
        """获取系统提示"""
        return self._system_prompt

    @system_prompt.setter
    def system_prompt(self, value):
        """设置系统提示，带有验证"""
        if not value:
            raise ValueError('系统提示词不能为空')
        if len(value) > 1000:
            raise ValueError('系统提示词过长，请精简到1000字符以内')
        self._system_prompt = value

    @property
    def api_key(self):
        """
        获取API key(安全显示)
        外部访问时隐藏真实值
        """
        if self.__api_key:
            # 只显示前3位和后4位
            visible_part = self.__api_key[:3]
            hidden_part = '*' * (len(self.__api_key) - 7)
            last_part = self.__api_key[-4:] if len(self.__api_key) > 7 else ''
            return f'{visible_part}{hidden_part}{last_part}'
        return None

    @api_key.setter
    def api_key(self, value):
        """设置API key，带有严格验证"""
        if not value.startswith('sk-'):
            raise ValueError('API Key应以"sk-"开头')
        if len(value) < 20:
            raise ValueError('API Key太短，可能无效')
        if " " in value:
            raise ValueError('API Key不能包含空格')

        self.__api_key = value
        print(f'API Key已安全设置({len(value)}字符)')

    @api_key.deleter
    def api_key(self):
        """删除API Key时的清理操作"""
        print('正在清除API Key...')
        self.__api_key = None

    @property
    def rate_limit(self):
        """获取速率限制（只读属性）"""
        return self.__rate_limit

    @property
    def info_summary(self):
        """计算属性：信息摘要"""
        total_words = sum(
            len(record['user'].split()) +
            len(record['assistant'].split()) for record in self.conversation_history
        )

        return {
            'name': self.name,
            'model': self.model,
            'conversation_count': len(self),
            'total_words': total_words,
            'active_days': (datetime.now() - self.created_at).days + 1
        }

    @validate_input
    def speak(self, text: str) -> str:
        """
        处理用户输入并生成AI回复

        Args：
            text: 用户输入的文本

        Returns：
            str：AI生成的回复

        Raises：
            ValueError：如果输入为空或过长
            TypeError：如果输入不是字符串

        Examples：
            >>> ai = AICharacter('助手', '提示')
            >>> ai.speak('你好')
            '助手：你好呀’
        """
        rule_response = {
            '名字': [f'我叫{self.name}，我是一个AI助手', f'我是{self.name}，请问有什么可以帮您'],
            '你好': ['你好呀', '嗨！很高兴见到你', '你好，请问有什么可以帮您'],
            '天气': ['今天下雨，记得带雨伞哦！', '今天大晴天，可以出去散散步，记得防晒哦', '今天天气不错呢'],
            '秋天': ['秋天是丰收的季节，我喜欢秋天', '我喜欢秋天的凉爽'],
            '谢谢': ['不客气。', '很高兴帮到您！', '这是我应该做的。']
        }
        ai_response = None
        # 遍历规则，检查用户消息中是否包含关键词
        for keyword, response in rule_response.items():
            if keyword in text:
                import random
                # 从该关键词的回复列表中随机选择一个
                template = random.choice(response)
                ai_response = template.format(name=self.name)
                break

        # 如果没有匹配到关键词，使用默认回复
        if ai_response is None:
            ai_response = f'{self.name}:我是你爹，不太明白{text}的意思，你可以问我天气，名字，或者打招呼哦。'

        # 创建一个对话记录字典
        conversation_record = {
            'timestamp': datetime.now().isoformat(),
            'user': text,  # 用户消息
            'assistant': ai_response,
            'model': self.model,
        }

        # 将字典添加到历史
        self.conversation_history.append(conversation_record)

        return ai_response

    def show_conversation_history(self):
        """打印出当前所有的对话历史"""
        if not self.conversation_history:
            print('对话历史为空')
            return

        print(f'\n===与{self.name}的对话历史===')
        for record in self.conversation_history:
            print(f"[{record['timestamp']}]")
            print(f'用户：{record["user"]}')
            print(f'{self.name}:{record["assistant"]}')
            print('-' * 30)

    def save_conversation_to_file(self, filename: str = None):
        """安全保存对话历史到文件"""
        if filename is None:
            filename = f'conversation_{self.name}_{datetime.now().strftime("%Y%m%d")}.json'

        try:
            import json
            # 准备数据
            data = {
                'character_name': self.name,
                'system_prompt': self.system_prompt,
                'total_conversations': len(self),
                'saved_at': datetime.now().isoformat(),
                'conversations': self.conversation_history,
            }

            # 写入文件
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            print(f'√ 对话历史已保存到{filename}')
            return True

        except PermissionError:
            print(f'× 权限不足，无法写入文件：{filename}')
            return False
        except IOError as e:
            print(f'× 文件操作错误：{e}')
            return False
        except Exception as e:
            print(f'× 未知错误：{e}')
            return False

    def get_system_prompt(self):
        return self.system_prompt

    def set_system_prompt(self, new_prompt: str):
        self.system_prompt = new_prompt
        print(f'[系统]{self.name}的提示词已更新')

    # ========魔法方法实现========
    def __str__(self):
        """
        用户友好的字符串表示
        使用print(character)或str(character)时调用
        """
        return f'AI角色：{self.name}（使用{self.model}模型）'

    def __repr__(self):
        """
        开发者友好的字符串表示
        在调试器、交互式Python中显示
        """
        return (f'AICharacter(name={self.name!r},'
                f'model={self.model!r}, '
                f'history_items={len(self)})')

    def __len__(self):
        """
        返回对话历史长度
        使用len(character)时调用
        """
        return len(self.conversation_history)

    def __getitem__(self, index):
        """
        支持索引访问对话历史
        使用character[0]或character[-1]时调用
        """
        if isinstance(index, slice):
            # 处理切片，如 assistant[1:3]
            start, stop, step = index.indices(len(self.conversation_history))
            return [self.conversation_history[i] for i in range(start, stop, step)]
        elif isinstance(index, int):
            # 如果是整数索引，确保返回字典
            # 处理负数索引
            if index < 0:
                index = len(self.conversation_history) + index

            # 检查索引是否有效
            if 0 <= index < len(self.conversation_history):
                return self.conversation_history[index]
            else:
                raise IndexError(f'索引{index}超出范围。有效范围：0-{len(self.conversation_history) - 1}')
        else:
            raise TypeError(f'索引必须是整数或切片，而不是{type(index).__name__}')

    def __contains__(self, keyword):
        """
        检查对话历史中是否包含关键词
        使用'Python' in character时调用
        """
        for record in self.conversation_history:
            if keyword in record['user'] or keyword in record['assistant']:
                return True
        return False

    def __add__(self, other):
        """
        合并两个AI角色的对话历史
        使用character1 + character2时调用
        """
        if not isinstance(other, AICharacter):
            raise TypeError('只能合并AICharacter对象')

        merged = AICharacter(
            name=f'{self.name}&{other.name}',
            system_prompt='merged Character'
        )
        merged.conversation_history = self.conversation_history + other.conversation_history
        return merged

    def __call__(self, prompt):
        """
        使实例可调用
        使用character('hello')等价于character.speak('hello')
        """
        return self.speak(prompt)


class AdvancedAICharacter(AICharacter):
    """
    高级AI角色类，继承自AICharacter
    添加了角色类型，token统计等额外功能
    """

    def __init__(self, name, system_prompt, model='gpt-4', role='assistant'):
        # 调用父类的__init__方法
        super().__init__(name, system_prompt)
        # 子类特有的属性
        self.role = role  # 角色类型：tutor，reviewer等
        self.model = model  # 运用的大模型
        self.token_used = 0  # token使用统计
        self.skills = []  # 技能列表
        self._performance_score = 100  # 私有属性：性能评分

    # ========方法重写（多态示例）========
    def speak(self, text):
        """
        重写speak方法，添加token统计和角色前缀
        """
        # 添加角色前缀到用户输入
        if self.role == 'tutor':
            prefixed_text = f'【学生提问】{text}'
        elif self.role == 'reviewer':
            prefixed_text = f'【代码审查】{text}'
        else:
            prefixed_text = text

        # 调用父类的speak方法 - 它会返回ai_response并添加记录到conversation_history
        response = super().speak(prefixed_text)

        # 添加token统计（简化的模拟）
        self.update_token_usage(prefixed_text, response)

        # 根据角色调整回复格式
        formatted_response = self._format_response_by_role(response)

        # 更新最后一条记录中的assistant字段
        if self.conversation_history:
            # 获取最后一条记录
            last_record = self.conversation_history[-1]
            # 更新回复内容
            formatted_response = self._format_response_by_role(response)
            last_record['assistant'] = formatted_response

        return formatted_response

    # ========子类新增方法========
    def add_skill(self, skill, level=1):
        """添加技能"""
        self.skills.append({'skill': skill, 'level': level})

    def get_stats(self) -> dict:
        """获取使用信息统计"""
        return {
            'name': self.name,
            'skills': self.skills,
            'token_used': self.token_used,
            'role': self.role,
            'total_conversations': len(self),
            'performance_score': self._performance_score,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M:%S')
        }

    def improve_performance(self, point):
        """提高性能评分"""
        if point <= 0:
            raise ValueError('Improvment points must be positive')
        self._performance_score += point
        print(f'{self.name}性能提升{point}分,当前评分：{self._performance_score}')

    # ========私有辅助方法========
    def update_token_usage(self, input_message, output_message):
        """更新token使用量（模拟）"""
        # 简单模拟：每个词记为一个token
        input_tokens = len(input_message.split())
        output_tokens = len(output_message.split())
        self.token_used += input_tokens + output_tokens

    def _format_response_by_role(self, response):
        """根据角色格式化回复"""
        if self.role == 'reviewer':
            return f'审查意见：{response}'
        elif self.role == 'tutor':
            return f'导师回答{response}'
        else:
            return response

    # ========魔法方法重写/新增========
    def __str__(self):
        """重写字符串表示"""
        return f'AdvancedAICharacter(name={self.name}, role={self.role}, skills={len(self.skills)})'

    def __repr__(self):
        """重写开发表示"""
        return f'AdvancedAICharacter(name={self.name!r}, role={self.role!r})'


# 测试代码
if __name__ == '__main__':
    # 创建角色
    assistant = AICharacter(
        name='范西蒙',
        system_prompt='我是一个乐于助人、知识渊博的AI助手'
    )
    print(f'{assistant.name}已上线，{assistant.system_prompt},输入退出或quit结束对话')
    # 对话循环
    while True:
        try:
            # 获取用户输入
            text = input(f'你：').strip()
            # 退出条件
            if text in ['quit', '退出']:
                print(f'{assistant.name}:再见！')
                break
            if not text:
                continue

            # AI回复
            reply = assistant.speak(text)
            print(f'{assistant.name}:{reply}')

            # 每3轮对话，可以选择查看历史记录
            if len(assistant.conversation_history) % 6 == 0:
                show = input('\n查看对话历史？(y/n).').lower()
                if show == 'y':
                    assistant.show_conversation_history()

        except KeyboardInterrupt:
            print(f'\n{assistant.name}：对话被中断，再见！')
            break
        except Exception as e:
            print(f'\n系统错误：{e}')
            continue


def test_advanced_features():
    """测试所有OOP进阶功能"""
    print('=== 测试OOP进阶功能===\n')

    # 1.测试继承
    print('1.测试继承:')
    tutor = AdvancedAICharacter(
        name='Python导师',
        system_prompt='你是一位专业的Python导师',
        role='tutor'
    )
    print(f'   创建:{tutor}')
    print(f'   是AICharacter子类吗？{isinstance(tutor, AICharacter)}')

    # 2.测试属性装饰器
    print('\n2. 测试属性装饰器：')
    assistant = AICharacter('助手', '帮助用户')

    # 测试setter验证
    try:
        assistant.name = ''  # 应该报错
    except ValueError as e:
        print(f'   验证失效：{e}')

    assistant.name = '代码助手'
    print(f'   角色名设置成功：{assistant.name}')

    # 测试API Key安全访问
    assistant.api_key = 'sk-1234567890abcdefghijklmn'
    print(f'   API Key安全显示{assistant.api_key}')

    # 3.测试魔法方法
    print('\n3. 测试魔法方法：')

    # __str__ 和 __repr__
    print(f'   __str__:{assistant}')
    print(f'   __repr__:{repr(assistant)}')

    # 添加对话
    assistant.speak('你好!')
    assistant.speak('python好学吗?')

    # __len__
    print(f'   对话数量(__len__):{len(assistant)}')

    # __getitem__
    if len(assistant) > 0:
        first_record = assistant[0]
        print(f'   第一条记录(__getitem__):{first_record["user"]}')
    else:
        print('   暂无对话记录')

    # __contains__
    print(f'   包含‘python吗(__contains__):{"python" in assistant}')

    # __call__
    print(f'   可调用测试(__call__):{assistant("测试调用")}')

    # 4.测试子类特有功能
    print('\n4. 测试子类特有功能：')
    tutor.add_skill('Python教学', 5)
    tutor.add_skill('算法讲解', 4)

    tutor.speak('如何学习列表推导式？')
    tutor.speak('解释一下装饰器')

    stats = tutor.get_stats()
    print(f'   数据统计：{stats}')

    # 5.测试合并功能（__add__）
    print('\n5.测试合并功能：')
    assistant1 = AICharacter('助手A', '帮助A')
    assistant2 = AICharacter('助手B', '帮助B')

    assistant1.speak('我是助手A')
    assistant2.speak('我是助手B')

    merged = assistant1 + assistant2
    print(f'   合并后对话数：{len(merged)}')
    print(f'   合并对象：{merged}')
    print('\n=== 所有测试完成 ===')


if __name__ == '__main__':
    test_advanced_features()
