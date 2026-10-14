import os
from fastapi import HTTPException
from typing import List, Dict, Any, Optional
from abc import ABC, abstractmethod
import tempfile
import logging
import uuid
from pathlib import Path
from backend.services.types import SearchResult
from backend.config.settings import settings

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
                # 显式指定 UTF-8，避免 Windows 默认 GBK 编码读取中文 UTF-8 文本时报 UnicodeDecodeError
                loader = TextLoader(file_path, encoding='utf-8')
            elif file_ext == '.docx':
                loader = Docx2txtLoader(file_path)
            elif file_ext in ('.md', '.markdown'):
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


# 上传限制（生产加固，第二阶段）：扩展名白名单 + 大小上限
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10MB
# 普通文档（走 DocumentProcessor 文本链路）
DOC_EXT = {'.pdf', '.txt', '.docx', '.md'}
# 表格文件（Phase 1A：走 Unified Table Representation 链路）
EXCEL_EXT = {'.xlsx', '.xls', '.csv', '.tsv'}
ALLOWED_EXT = DOC_EXT | EXCEL_EXT
ALLOWED_MIME = {
    '.pdf': {'application/pdf'},
    '.txt': {'text/plain'},
    '.docx': {'application/vnd.openxmlformats-officedocument.wordprocessingml.document'},
    '.md': {'text/markdown', 'text/plain', 'text/x-markdown'},
    # 表格：浏览器 MIME 在各平台不一致，放宽为“常见值或 octet-stream”
    '.xlsx': {
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'application/vnd.ms-excel',
        'application/octet-stream',
    },
    '.xls': {'application/vnd.ms-excel', 'application/octet-stream'},
    '.csv': {'text/csv', 'application/csv', 'application/vnd.ms-excel', 'text/plain'},
    '.tsv': {'text/tab-separated-values', 'text/plain', 'text/csv'},
}


#: 上传落盘的块大小（1MB）：流式读取，避免整份文件常驻内存（6A-8 P4）。
UPLOAD_CHUNK_BYTES = 1024 * 1024


async def _read_upload_chunk(upload_file, size: int) -> bytes:
    """按块读取上传内容；保持改动前的分支顺序与 await 语义。

    - ``UploadFile``（异步 read）：``await upload_file.read(size)``；
    - 其它带 ``.file.read`` 的对象：同步 ``read(size)``；
    - 两者都没有：``ValueError``（与改动前一致）。

    读到 EOF 时返回空 ``bytes``，由调用方结束循环。
    """
    if hasattr(upload_file, 'read'):
        chunk = await upload_file.read(size)
        return chunk if chunk is not None else b''
    if hasattr(upload_file, 'file') and hasattr(upload_file.file, 'read'):
        chunk = upload_file.file.read(size)
        return chunk if chunk is not None else b''
    raise ValueError('upload_file 对象不支持读取')


async def save_uploaded_file(upload_file) -> str:
    """保存上传的文件到临时目录（含扩展名/MIME/大小校验）"""
    original_filename = getattr(upload_file, 'filename', 'uploaded_file')
    file_ext = Path(original_filename).suffix.lower()

    # 1) 扩展名白名单
    if file_ext not in ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f'不支持的文件类型：{file_ext or "无扩展名"}，'
                   f'仅支持 PDF / TXT / DOCX / MD / XLSX / XLS / CSV / TSV',
        )

    # 2) MIME 辅助校验（浏览器 MIME 不可靠，仅作辅助；放行 octet-stream 防误杀）
    content_type = getattr(upload_file, 'content_type', None)
    allowed_mimes = ALLOWED_MIME.get(file_ext, set())
    if content_type and content_type not in allowed_mimes and content_type != 'application/octet-stream':
        raise HTTPException(
            status_code=415,
            detail=f'文件内容与扩展名不符：Content-Type={content_type}',
        )

    try:
        # 上传临时目录（F2 统一配置）：
        #   UPLOAD_TEMP_DIR 留空 -> 保持原行为：<系统临时目录>/ai_assistant_uploads
        #   UPLOAD_TEMP_DIR 非空 -> 直接作为上传临时目录（测试/特殊部署可隔离，
        #                          不再需要运行时 patch tempfile / 写真实临时目录）
        _configured_upload_dir = (getattr(settings, 'UPLOAD_TEMP_DIR', '') or '').strip()
        upload_dir = (Path(_configured_upload_dir) if _configured_upload_dir
                      else Path(tempfile.gettempdir()) / 'ai_assistant_uploads')
        upload_dir.mkdir(parents=True, exist_ok=True)

        filename = f'{uuid.uuid4().hex} {file_ext}'
        file_path = upload_dir / filename

        # 读取内容（流式：逐块读入并直接落盘，避免整份文件常驻内存）
        # 6A-8 P4：原实现用一次性 read（不带 size）把整个文件读成 bytes——超限文件也要先
        # 吃满内存才判 413；现改为边读边写边累计，超限立刻中止并删除半成品。
        total_bytes = 0
        try:
            with open(file_path, 'wb') as f:
                while True:
                    chunk = await _read_upload_chunk(upload_file, UPLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    # 3) 大小限制（与原实现同一边界：> MAX_UPLOAD_BYTES 才算超限）
                    if total_bytes > MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f'文件过大，最大允许 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB',
                        )
                    f.write(chunk)
        except BaseException:
            # 超限 / 写入失败 / IO 异常 / 任务取消：一律删除已创建的半成品临时文件
            try:
                if file_path.exists():
                    file_path.unlink()
            except OSError as cleanup_err:
                logger.debug(f'清理半成品上传文件失败：{file_path}, {cleanup_err}')
            raise
        logger.info(f'文件被保存到：{file_path}')
        return str(file_path)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'保存文件失败：{e}')
        raise HTTPException(status_code=500, detail=f'保存上传文件失败：{e}')