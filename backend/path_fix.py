"""
Windows系统路径修复
"""
import os
import sys


def add_project_to_path():
    """将项目根目录添加到Python路径"""
    # 获取当前文件所在目录
    current_dir = os.path.dirname(os.path.abspath(__file__))
    # 获取项目根目录
    project_root = os.path.dirname(current_dir)

    # 添加到Python路径
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
        print(f'✅️ 已添加项目根目录到Python路径:{project_root}')

    # 添加backend目录
    if current_dir not in sys.path:
        sys.path.insert(0, current_dir)
        print(f'✅️ 已添加backend目录到Python路径：:{current_dir}')

    return current_dir, project_root

# 自动执行
if __name__ == '__main__':
    add_project_to_path()