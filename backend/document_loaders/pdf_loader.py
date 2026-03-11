"""
PDF文档加载器
"""
import fitz     # PyMuPDF
from typing import List, Optional
# from pathlib import Path
from .base_loader import DocumentLoader
from .models import Document, ProcessingConfig


class PDFLoader(DocumentLoader):
    """PDF文档加载器"""
    def __init__(self, config: Optional[ProcessingConfig] = None):
        super().__init__(config)
        self.supported_extensions = ['.pdf']

    def load(self, file_path: str) -> Document:
        """加载PDF文件"""
        with open(file_path, 'rb') as f:
            file_bytes = f.read()

        return self.load_from_bytes(file_bytes, file_path)

    def load_from_bytes(self, file_bytes: bytes, filename: str) -> Document:
        """从字节加载PDF"""
        metadata = self.extract_metadata(filename, file_bytes)

        try:
            # 使用PyMuPDF读取PDF
            doc = fitz.open(stream=file_bytes, filetype='pdf')

            # 提取文本
            text_parts = []
            for page_num in range(len(doc)):
                page = doc[page_num]
                page_text = page.get_text()

                # 清理文本
                page_text = self._clean_text(page_text)

                if page_text.strip():
                    text_parts.append(f'第{page_num + 1}页：\n{page_text}')

            full_text = '\n\n'.join(text_parts)

            # 提取额外元数据
            pdf_metadata = doc.metadata
            metadata.update({
                'page_count': len(doc),
                'author': pdf_metadata.get('author', ''),
                'title': pdf_metadata.get('title', ''),
                'subject': pdf_metadata.get('subject', ''),
                'creator': pdf_metadata.get('creator', ''),
                'producer': pdf_metadata.get('producer', ''),
                'creation_date': pdf_metadata.get('creationDate', ''),
                'modification_data': pdf_metadata.get('modDate', ''),
            })

            doc.close()

            return Document(
                content=full_text,
                metadata=metadata,
                source_type='pdf',
            )

        except Exception as e:
            raise ValueError(f'PDF解析失败：{str(e)}')

    def _clean_text(self, text: str) -> str:
        """清理文本"""
        # 移除多余的空格和换行
        lines = text.split('\n')
        cleaned_lines = []

        for line in lines:
            line = line.strip()
            if line:
                cleaned_lines.append(line)

        return '\n'.join(cleaned_lines)

    def extract_tables(self, file_path: str) -> List[dict]:
        """提取PDF中的表格"""
        try:
            doc = fitz.open(file_path)
            tables = []

            for page_num in range(len(doc)):
                page = doc[page_num]

                # 尝试提取表格（这是一个简化版本，实际可能需要更复杂的处理）
                text = page.get_text()

                # 这里可以添加表格检测和提取逻辑
                # 实际项目中可能需要使用专门的表格提取库如camelot或tabula-py
                # 简化的表格检测：寻找类似表格的结构
                lines = text.split('\n')
                table_candidates = []
                current_table = []

                for line in lines:
                    if self._looks_like_table_row(line):
                        current_table.append(line)
                    elif current_table:
                        if len(current_table) >= 2: # 至少有标题行和数据行
                            tables.append({
                                'page': page_num + 1,
                                'rows': current_table.copy(),
                            })
                        current_table = []

            doc.close()
            return tables

        except Exception as e:
            print(f'表格提取失败：{e}')
            return []

    def _looks_like_table_row(self, line: str) -> bool:
        """判断一行是否看起来像表格行"""
        # 简单的启发式规则：包含多个由空格或制表符分隔的字段
        parts = line.split()
        return len(parts) >= 3 and any('，' in line or '|' in line)
