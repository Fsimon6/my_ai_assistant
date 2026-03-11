import pytest
import json
import os
from datetime import datetime
from _pytest.tmpdir import tmp_path
from character import AICharacter, AdvancedAICharacter


class TestCharacterComprehensive:
    """综合测试AICharacter类"""

    def setup_method(self):
        """每个测试前的准备"""
        self.ai = AICharacter('测试助手，‘测试提示')

    def test_01_creation_and_properties(self):
        """测试创建和基本属性"""
        assert self.ai.name == '测试助手'
        assert self.ai.system_prompt == '测试提示'
        assert self.ai.model == 'gpt-3.5-turbo'
        assert len(self.ai) == 0

    def test_02_speak_functionality(self):
        """测试对话功能"""
        response = self.ai.speak('你好')
        assert '助手' in response
        assert len(self.ai) == 1

        # 测试对话历史结构
        record = self.ai[0]
        assert isinstance(record, dict)
        assert 'user' in record
        assert 'assistant' in record
        assert 'timestamp' in record

    def test_03_property_validation(self):
        """测试属性验证"""
        # 测试名称验证
        with pytest.raises(ValueError, match='不能为空'):
            self.ai.name = ' '

        with pytest.raises(ValueError, match='超过50个字符'):
            self.ai.name = 'a' * 51

        # 测试API Key验证
        with pytest.raises(ValueError, match='应以'):
            self.ai.api_key = 'invalid_key'

        with pytest.raises(ValueError, match='太短'):
            self.ai.api_key = 'sk-123'

    def test_04_magic_methods(self):
        """测试魔法方法"""
        self.ai.speak('消息1')
        self.ai.speak('消息2')

        # __len__
        assert len(self.ai) == 2

        # __getitem__
        assert isinstance(self.ai[0], dict)
        assert isinstance(self.ai[-1], dict)

        # __contains__
        self.ai.speak('包含Python关键字')
        assert 'Python' in self.ai

        # __str__ 和 __repr__
        assert 'AI角色' in str(self.ai)
        assert 'AICharacter' in repr(self.ai)

    def test_05_error_handling(self):
        """测试错误处理"""
        # 空输入
        with pytest.raises(ValueError, match='不能为空'):
            self.ai.speak('')

        # 无效索引
        with pytest.raises(IndexError):
            _ = self.ai[999]

        # 无效类型索引
        with pytest.raises(TypeError):
            _ = self.ai['invalid']

    def test_06_file_operations(self):
        """测试文件操作"""
        self.ai.speak('测试消息')

        # 保存到临时文件
        test_file = tmp_path / 'test.save.json'
        success = self.ai.save_conversation_to_file(str(test_file))

        assert success is True
        assert os.path.exists(test_file)

        # 验证文件内容
        with open(test_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            assert data['character_name'] == '测试助手'
            assert len(data['conversations']) == 1


class TestAdvancedAICharacter:
    """测试AdvancedAICharacter类"""

    def test_inheritance_and_override(self):
        """测试继承和方法重写"""
        advanced = AdvancedAICharacter(
            name='高级助手',
            system_prompt='高级提示',
            role='tutor'
        )

        # 测试继承
        assert isinstance(advanced, AICharacter)
        assert advanced.role == 'tutor'

        # 测试方法重写
        response = advanced.speak('问题')
        assert '导师回答' in response

        # 测试新增功能
        advanced.add_skill('Python', 5)
        assert len(advanced.skills) == 1
        assert advanced.skills[0]['skill'] == 'Python'
        assert advanced.skills[0]['level'] == 5

        stats = advanced.get_stats()
        assert 'skills' in stats
        assert 'token_used' in stats

    def test_merge_operation(self):
        """测试合并操作"""
        ai1 = AICharacter('A', '提示A')
        ai2 = AICharacter('B', '提示B')

        ai1.speak('消息A')
        ai2.speak('消息B')

        merged = ai1 + ai2
        assert merged.name == 'A&B'
        assert len(merged) == 2


# 运行测试的命令
if __name__ == '__main__':
    print('综合运行测试...')
    pytest.main([__file__, '-v'])
