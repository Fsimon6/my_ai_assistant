"""
文档加载器数据模型
"""
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
from datetime import datetime
from pydantic import BaseModel


@dataclass
class Document:
    """文档类"""
    content: str
    metadata: Dict[str, Any]
    source_type: str
    chunks: List['Chunk'] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'content': self.content,
            'metadata': self.metadata,
            'source_type': self.source_type,
            'chunks': [chunk.to_dict() for chunk in self.chunks] if self.chunks else []
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Document':
        """从字典创建"""
        doc = cls(
            content=data['content'],
            metadata=data['metadata'],
            source_type=data['source_type'],
        )

        if 'chunks' in data:
            doc.chunks = [Chunk.from_dict(chunk) for chunk in data['chunks']]
            return doc


@dataclass
class Chunk:
    """文档块类"""
    content: str
    metadata: Dict[str, Any]
    chunk_index: int
    strat_pos: int
    end_pos: int

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'content': self.content,
            'metadata': self.metadata,
            'chunk_index': self.chunk_index,
            'strat_pos': self.strat_pos,
            'end_pos': self.end_pos,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Chunk':
        """从字典创建"""
        return cls(
            content=data['content'],
            metadata=data['metadata'],
            chunk_index=data['chunk_index'],
            strat_pos=data['strat_pos'],
            end_pos=data['end_pos'],
        )


class ProcessingConfig(BaseModel):
    """处理配置"""
    chunk_size: int = 1000
    chunk_overlap: int = 200
    min_chunk_size: int = 50
    max_chunk_size: int = 2000
    split_by_sentences: bool = True
    preserve_formatting: bool = False
    extract_tables: bool = True
    extract_images: bool = False
    languages: str = 'zh'

    class Config:
        json_schema_extra = {
            'example': {
                'chunk_size': 1000,
                'chunk_overlap': 200,
                'min_chunk_size': 50,
                'max_chunk_size': 2000,
                'split_by_sentences': True,
                'preserve_formatting': False,
                'extract_tables': True,
                'extract_images': False,
                'languages': 'zh'
            }
        }
