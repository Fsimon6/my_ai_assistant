import os
from typing import List, Dict, Any, Optional
from abc import ABC, abstractmethod
import tempfile
import logging
import uuid
from pathlib import Path
from services.types import SearchResult

from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import (
    PyPDFLoader,
    TextLoader,
    Docx2txtLoader,
    UnstructuredMarkdownLoader
)
from langchain_classic.schema import Document

logger = logging.getLogger(__name__)


class BaseDocumentLoader(ABC):
    """文档加载器基类"""

    @abstractmethod
    def load(self, file_path: str) -> List[Dict[str, Any]]:
        """加载文档"""
        pass


class DocumentProcessor:
    """文档处理器"""

    def __init__(
        self,
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
        separators: Optional[List[str]] = None,
    ):
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=separators or ['\n\n', '\n', '。', '！', '？', '；', '，', ' ', '']
        )

    def process_file(self, file_path: str) -> List[SearchResult]:
        """处理单个文件"""
        file_ext = Path(file_path).suffix.lower()

        try:
            # 根据文件类型选择加载器
            if file_ext == '.pdf':
                loader = PyPDFLoader(file_path)
            elif file_ext == '.txt':
                loader = TextLoader(file_path)
            elif file_ext == '.docx':
                loader = Docx2txtLoader(file_path)
            elif file_ext == ['.md', '.markdown']:
                loader = UnstructuredMarkdownLoader(file_path)
            else:
                raise ValueError(f'不支持的文件类型：{file_ext}')

            # 加载文档
            documents = loader.load()

            # 分割文档
            chunks = self.text_splitter.split_documents(documents)

            # 转换为字典格式
            results = []
            for i, chunk in enumerate(chunks):
                results.append({
                    'id': f'{Path(file_path).stem}_{i}',
                    'content': chunk.page_content,
                    'metadata': {
                        **chunk.metadata,
                        'source': Path(file_path).name,
                        'chunk_index': i
                    }
                })

            logger.info(f'处理文件{file_path}完成，生成{len(results)}个chunk')
            return results

        except Exception as e:
            logger.error(f'处理文件{file_path}失败：{e}')
            raise

    def process_text(self, text: str, metadata: Optional[Dict] = None) -> List[SearchResult]:
        """处理纯文本"""
        try:
            # 分割文本
            doc = Document(page_content=text, metadata=metadata or {})
            chunks = self.text_splitter.split_documents([doc])

            # 转换为字典格式
            results = []
            for i, chunk in enumerate(chunks):
                results.append({
                    'id': f'text_{i}',
                    'content': chunk.page_content,
                    'metadata': {
                        **chunk.metadata,
                        'source': metadata.get('source', 'text'),
                        'chunk_index': i
                    }
                })

            return results

        except Exception as e:
            logger.error(f'处理文本失败：{e}')
            raise


def save_uploaded_file(upload_file) -> str:
    """保存上传的文件到临时目录"""
    try:
        # 创建临时目录
        temp_dir = Path(tempfile.gettempdir())
        upload_dir = temp_dir / 'ai_assistant_uploads'
        upload_dir.mkdir(exist_ok=True)

        # 获取上传文件的原始文件名
        # 假设 upload_file 有 filename 和 file 属性
        original_filename = getattr(upload_file, 'filename', 'uploaded_file')
        file_ext = Path(original_filename).suffix
        filename = f'{uuid.uuid4().hex} {file_ext}'
        file_path = upload_dir / filename

        # 保存文件
        with open(file_path, 'wb') as f:
            # 假设 upload_file read() 方法
            if hasattr(upload_file, 'read'):
                content = upload_file.read()
            elif hasattr(upload_file, 'file') and hasattr(upload_file.file, 'read'):
                content = upload_file.file.read()
            else:
                raise ValueError('upload_file 对象不支持读取')

            f.write(content)
            logger.info(f'文件被保存到：{file_path}')
            return str(file_path)

    except Exception as e:
        logger.error(f'保存文件失败：{e}')