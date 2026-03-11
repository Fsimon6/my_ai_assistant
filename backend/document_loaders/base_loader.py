"""
基础文档加载器抽象类
"""
from abc import ABC, abstractmethod
from typing import List, Optional
from pathlib import Path
import hashlib
from datetime import datetime
from .models import Document, Chunk, ProcessingConfig


class BaseLoader(ABC):
    """基础加载器抽象类"""

    def __init__(self, config: Optional[ProcessingConfig] = None):
        self.config = config or ProcessingConfig()
        self.supported_extensions = []

    @abstractmethod
    def load(self, file_path: str) -> Document:
        """加载文档"""
        pass

    @abstractmethod
    def load_from_bytes(self, file_bytes: bytes, filename: str) -> Document:
        """从字节加载文档"""
        pass

    def can_load(self, file_path: str) -> bool:
        """检查是否支持该文件类型"""
        ext = Path(file_path).suffix.lower()
        return ext in self.supported_extensions

    def calculate_file_hash(self, file_bytes: bytes) -> str:
        """计算文件哈希值"""
        return hashlib.md5(file_bytes).hexdigest()


class DocumentLoader(BaseLoader):
    """文档加载器基类"""

    def __init__(self, config: Optional[ProcessingConfig] = None):
        super().__init__(config)
        self.chunk_size = config.chunk_size if config else 1000
        self.chunk_overlap = config.chunk_overlap if config else 200

    def chunk_document(self, document: Document) -> List[Chunk]:
        """将文档分块"""
        content = document.content
        chunks = []

        if len(content) <= self.chunk_size:
            chunks.append(Chunk(
                content=content,
                metadata=document.metadata.copy(),
                chunk_index=0,
                start_pos=0,
                end_pos=len(content),
            ))
            return chunks

        start = 0
        chunk_index = 0

        while start < len(content):
            end = min(start + self.chunk_size, len(content))

            # 尝试在句末分割
            if end < len(content):
                # 查找最近的句号、问号或感叹号
                for split_point in range(end, start, -1):
                    if content[split_point-1] in '.!?。！？':
                        end = split_point
                        break
                else:
                    # 查找最近的空格
                    for split_point in range(end, start, -1):
                        if content[split_point-1].isspace():
                            end = split_point
                            break

            chunk_content = content[start:end].strip()
            if chunk_content:   # 跳过空块
                chunk_metadata = document.metadata.copy()
                chunk_metadata.update({
                    'chunk_index': chunk_index,
                    'start_pos': start,
                    'end_pos': end,
                    'total_chunks': 0   # 稍后更新
                })

                chunks.append(Chunk(
                    content=chunk_content,
                    metadata=chunk_metadata,
                    chunk_index=chunk_index,
                    start_pos=start,
                    end_pos=end,
                ))
                chunk_index += 1

            start = end = self.chunk_overlap

        # 更新总块数
        for chunk in chunks:
            chunk.metadata['total_chunks'] = len(chunks)

            return chunks

    def extract_metadata(self, file_path: str, file_bytes: bytes) -> dict:
        """提取文档元数据"""
        path = Path(file_path)
        file_hash = self.calculate_file_hash(file_bytes)

        return {
            'filename': path.name,
            'file_extension': path.suffix.lower(),
            'file_size': len(file_bytes),
            'file_hash': file_hash,
            'last_modified': datetime.now().isoformat(),
            'processing_date': datetime.now().isoformat(),
            'chunk_size': self.chunk_size,
            'chunk_overlap': self.chunk_overlap,
        }
