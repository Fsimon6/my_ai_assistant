# -*- coding: utf-8 -*-
import asyncio
import sys
import os
import pytest

# 添加路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.asyncio
async def test_rag_system():
    """测试RAG系统完整流程"""
    print(' 开始测试RAG系统...')
    print('=' * 60)

    try:
        # 1. 测试服务导入
        print('1. 测试服务模块导入...')
        try:
            from backend.services.rag_service import get_rag_service
            from backend.services.document_service import DocumentProcessor
            print(' 服务模块导入成功')
        except Exception as e:
            print(f' 服务模块导入失败：{e}')
            return False

        # 2. 测试RAG查询
        print('2. 测试RAG查询...')
        try:
            rag_service = get_rag_service()
            test_query = '什么是人工智能？'
            print(f' 查询：{test_query}')
            response = ''
            async for chunk in rag_service.rag_query(test_query):
                response += chunk

            print(f' 响应：{response[:100]}...')
            print(' RAG查询成功')
        except Exception as e:
            print(f' RAG查询失败：{e}')

        # 3. 测试文档处理
        print('3. 测试文档处理...')
        try:
            processor = DocumentProcessor()
            # 创建测试文件
            test_file = 'test_document.txt'
            with open(test_file, 'w', encoding='utf-8') as f:
                f.write('这是一个测试文档内容。\n用于测试文档处理功能。\n 包含多行文本内容。')

            chunks = processor.process_file(test_file)
            print(f' 生成chunks数量：{len(chunks)}')
            print(f' 第一个chunk内容：{chunks[0]["content"][:50]}...')
            print(' 文档处理成功')

            # 清理测试文件
            os.remove(test_file)
        except Exception as e:
            print(f' 文档处理失败：{e}')

        print('=' * 60)
        print(' RAG系统测试完成！')
        return True

    except Exception as e:
        print(f'测试过程中出现错误：{e}')
        return False


async def test_api_endpoints():
    """测试API端点"""
    print('\n 测试API端点...')
    print('=' * 60)

    import httpx

    async with httpx.AsyncClient(base_url='http://localhost:8000') as client:
        # 测试健康检查
        try:
            response = await client.get('/health')
            print(f'1. 健康检查：{response.status_code} - {response.json()}')
        except Exception as e:
            print(f'1. 健康检查失败：{e}')

        # 测试RAG查询端点
        try:
            response = await client.post('/api/v1/rag/query', json={
                'query': '测试查询',
                'stream': False
            })
            print(f'2. RAG查询：{response.status_code}')
        except Exception as e:
            print(f'2. RAG查询失败：{e}')

    print('=' * 60)

if __name__ == '__main__':
    print(' 启动RAG系统测试')

    # 运行测试
    success = asyncio.run(test_rag_system())

    if success:
        # 测试API端点
        asyncio.run(test_api_endpoints())
    else:
        print('\n 由于基础测试失败，跳过API端点测试')

    print('\n 测试总结：')
    print(' 1. 确保backend/services/目录下所有必要文件')
    print(' 2. 运行 uvicorn backend.main:app --reload 启动服务')
    print(' 3. 访问 http://localhost:8000/docs 查看API文档')
    print(' 4. 前端访问 http://localhost:5173 测试完整功能')
