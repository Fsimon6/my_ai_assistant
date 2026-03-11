"""
文档加载器工厂
根据文件类型选择适合的加载器
"""
from typing import Optional, Dict, Type
from pathlib import Path
from .base_loader import BaseLoader
from .pdf_loader import PDFLoader
from .txt_loader import TXTLoader
from .docx_loader import DOCXLoader
# from .excel_loader import ExcelLoader
# from .image_loader import ImageLoader
from .models import ProcessingConfig


class DocumentLoaderFactory:
    """文档加载器工厂"""

    # 注册的加载器
    _loaders: Dict[str, Type[BaseLoader]] = {
        '.pdf': PDFLoader,
        '.txt': TXTLoader,
        '.md': TXTLoader,
        '.csv': TXTLoader,
        '.log': TXTLoader,
        '.json': TXTLoader,
        '.xml': TXTLoader,
        '.yaml': TXTLoader,
        '.yml': TXTLoader,
        '.docx': DOCXLoader,
        '.doc': DOCXLoader,
        # '.xlsx': ExcelLoader,
        # '.xls': ExcelLoader,
        # '.jpg': ImageLoader,
        # '.jpeg': ImageLoader,
        # '.png': ImageLoader,
        # '.gif': ImageLoader,
        # '.bmp': ImageLoader,
    }

    @classmethod
    def get_loader(cls, file_path: str, config: Optional[ProcessingConfig] = None) -> BaseLoader:
        """获取合适的文档加载器"""
        ext = Path(file_path).suffix.lower()
        loader_class = cls._loaders.get(ext)
        if loader_class is None:
            raise ValueError(f'不支持的文件格式：{ext}')

        return loader_class(config)

    @classmethod
    def register_loader(cls, extension: str, loader_class: Type[BaseLoader]):
        """注册新的加载器"""
        cls._loaders[extension.lower()] = loader_class

    @classmethod
    def get_supported_extensions(cls) -> list:
        """获取支持的文件拓展名"""
        return list(cls._loaders.keys())

    @classmethod
    def can_load_file(cls, file_path: str) -> bool:
        """检查是否支持该文件"""
        ext = Path(file_path).suffix.lower()
        return ext in cls._loaders

    @classmethod
    def load_document(cls, file_path: str, config: Optional[ProcessingConfig] = None):
        """加载文件（便捷方法）"""
        loader = cls.get_loader(file_path, config)
        return loader.load(file_path)

    @classmethod
    def load_document_from_bytes(cls, file_bytes: bytes, filename: str, config: Optional[ProcessingConfig] = None):
        """从字节加载文档（便捷方法）"""
        loader = cls.get_loader(filename, config)
        return loader.load_from_bytes(file_bytes, filename)
