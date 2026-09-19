"""
FastAPI API测试脚本
"""
import requests
import json
import sys

BASE_URL = 'http://127.0.0.1:8000'


class APITester:
    """API测试器"""

    def __init__(self, base_url):
        self.base_url = base_url
        self.token = None
        self.user_id = None

    def print_response(self, response, description=""):
        """打印响应"""
        print(f'\n{"=" * 60}')
        if description:
            print(f'{description}')
        print(f'状态码：{response.status_code}')
        try:
            data = response.json()
            print(f'响应内容：')
            print(json.dumps(data, indent=2, ensure_ascii=False))
        except:
            print(f'响应内容：{response.text[:200]}')
        print(f'{"=" * 60}')

        return response

    def test_basic(self):
        """测试基础端点"""
        print('🚀 测试基础端点...')

        endpoints = ['/', '/health', '/docs', '/redoc']
        for endpoint in endpoints:
            response = requests.get(f'{self.base_url}{endpoint}')
            status = '✅️' if response.status_code == 200 else '❌️'
            print(f'{status}{endpoint}: {response.status_code}')

    def test_register(self):
        """测试用户注册"""
        print('\n 测试用户注册...')

        user_data = {
            'username': 'test_user',
            'email': 'testuser@example.com',
            'password': 'test123456',
            'full_name': '测试用户',
        }

        response = requests.post(f'{self.base_url}/api/v1/auth/register', json=user_data)
        self.print_response(response, '用户注册')

        if response.status_code == 200:
            print('✅️ 用户注册成功')
        return response

    def test_login(self):
        """测试用户登录"""
        print('\n 测试用户登录...')

        login_data = {
            'username': 'test',
            'password': 'test123',
        }

        response = requests.post(f'{self.base_url}/api/v1/auth/login', json=login_data)
        result = self.print_response(response, '用户登录')

        if response.status_code == 200:
            data = response.json()
            if data.get('success'):
                self.token = data['data']['access_token']
                self.user_id = data['data']['user']['id']
                print(f'✅️ 登录成功，获取到token：{self.token[:20]}...')
                print(f'    用户ID：{self.user_id}')

        return response

    def test_auth_endpoints(self):
        """测试需要认证的端点"""
        if not self.token:
            print('❌️ 未获取到token，跳过认证测试')
            return

        print('\n 测试认证端点...')

        headers = {'Authorization': f'Bearer {self.token}'}

        # 1.测试获取用户信息
        print('\n1. 获取用户信息：')
        response = requests.get(f'{self.base_url}/api/v1/auth/me', headers=headers)

        self.print_response(response, '获取用户信息')

        # 2.创建角色
        print('\n2. 创建角色：')
        character_data = {
            'name': '我的AI助手',
            'system_prompt': '你是一个乐于助人的助手',
            'model': 'gpt-3.5-turbo'
        }
        response = requests.post(
            f'{self.base_url}/api/v1/characters/',
            json=character_data,
            headers=headers
        )
        result = self.print_response(response, '创建AI角色')

        if response.status_code == 200:
            data = response.json()
            if data.get('success'):
                character_id = data['data']['id']

            # 3.获取角色列表
            print('\n3. 获取角色列表：')
            response = requests.get(f'{self.base_url}/api/v1/characters/', headers=headers)
            self.print_response(response, '角色列表')

            # 4.与角色对话
            print('\n4. 与角色对话：')
            speak_data = {'text': '你好，请介绍一下自己'}
            response = requests.post(
                f'{self.base_url}/api/v1/characters/{character_id}/speak',
                json=speak_data,
                headers=headers
            )
            self.print_response(response, '对话')

            # 5.获取对话历史
            # print('\n5. 获取对话历史：')
            # response = requests.get(f'{self.base_url}/api/v1/characters/{character_id}/conversations')
            # self.print_response(response, '对话历史')
            #
            # # 6.获取统计信息
            # print('\n6. 获取统计信息：')
            # response = requests.get(f'{self.base_url}/api/v1/characters/{character_id}/stats')
            # self.print_response(response, '统计信息')
            #
            # # 7.测试健康检查
            # print('\n7. 健康检查：')
            # response = requests.get(f'http://localhost:8000/health')
            # self.print_response(response, '健康检查')
            #
            # # 8.测试批量对话（需要先多几次对话）
            # print('\n8. 批量对话：')
            # # 先多几次对话
            # for i in range(3):
            #     speak_data = {
            #         'text': f'测试消息{i + 1}',
            #         'character_id': character_id,
            #     }
            #     requests.post(f'{self.base_url}/api/v1/characters/{character_id}/speak/', json=speak_data)
            #
            # batch_data = {
            #     'text': ['你好', '今天天气怎样', '谢谢'],
            #     'character_id': character_id,
            # }
            # response = requests.post(f'{BASE_URL}/api/v1/characters/{character_id}/batch-speak', json=batch_data)
            # self.print_response(response, '批量对话')
            #
            # # 10.删除角色
            # print('\n10. 删除角色：')
            # response = requests.get(f'{BASE_URL}/characters/{character_id}')
            # self.print_response(response, '删除角色')

    def run_all_tests(self):
        """运行所有测试"""
        print('🚀 开始完整API测试')
        print(f' 测试地址：{self.base_url}')

        try:
            self.test_basic()
            self.test_register()
            self.test_login()
            self.test_auth_endpoints()
            print('\n✅️ 所有测试完成！')
        except requests.exceptions.ConnectionError:
            print('❌️ 无法连接到服务器，请确保服务已启动')
            print('    运行命令：python backend/main.py')
        except Exception as e:
            print(f'测试失败：{type(e).__name__}: {e}')
            import traceback
            traceback.print_exc()


def main():
    """主函数"""
    tester = APITester(BASE_URL)
    tester.run_all_tests()


if __name__ == '__main__':
    main()
