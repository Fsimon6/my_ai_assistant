"""
TXT文档加载器
"""
import chardet
from typing import Optional
from pathlib import Path
from .base_loader import DocumentLoader
from .models import Document, ProcessingConfig


class TXTLoader(DocumentLoader):
    """TXT文档加载器"""
    def __init__(self, config: Optional[ProcessingConfig] = None):
        super().__init__(config)
        self.supported_extensions = ['.txt', '.md', '.csv', '.log', '.json', '.xml', '.yaml', '.yml']

    def load(self, file_path: str) -> Document:
        """加载TXT文件"""
        with open(file_path, 'rb') as f:
            file_bytes = f.read()

        return self.load_from_bytes(file_bytes, file_path)

    def load_from_bytes(self, file_bytes: bytes, filename: str) -> Document:
        """从字节加载TXT"""
        metadata = self.extract_metadata(filename, file_bytes)

        # 检测编码
        encoding = self._detect_encoding(file_bytes)

        try:
            # 解码文本
            text = file_bytes.decode(encoding, errors='replace')

            # 清理文本
            text = self._clean_text(text)
            metadata.update({
                'encoding': encoding,
                'line_count': len(text.splitlines()),
                'character_count': len(text),
            })

            return Document(
                content=text,
                metadata=metadata,
                source_type='text',
            )

        except UnicodeDecodeError:
            # 尝试其它常见编码
            for enc in ['utf-8', 'gbk', 'gb2312', 'latin1']:
                try:
                    text = file_bytes.decode(enc, errors='replace')
                    text = self._clean_text(text)
                    metadata.update({
                        'encoding': enc,
                        'line_count': len(text.splitlines()),
                        'character_count': len(text),
                    })

                    return Document(
                        content=text,
                        metadata=metadata,
                        source_type='text',
                    )
                except UnicodeDecodeError:
                    continue

            raise ValueError('无法解码文本文件，请检查文件编码')

    def _detect_encoding(self, file_bytes: bytes) -> str:
        """检测文件编码"""
        result = chardet.detect(file_bytes)
        return result['encoding'] or 'utf-8'

    def _clean_text(self, text: str) -> str:
        """清理文本"""
        # 移除BOM（字节顺序标记）
        if text.startswith('\ufeff'):
            text = text[1:]

        # 标准化换行符
        text = text.replace('\r\n', '\n').replace('\r', '\n')

        # 移除多余的空行
        lines = text.split('\n')
        cleaned_lines = []
        empty_line_count = 0

        for line in lines:
            stripped = line.strip()
            if stripped:
                cleaned_lines.append(line)
                empty_line_count = 0
            else:
                empty_line_count += 1
                if empty_line_count <= 2:    # 最多保留两个空行
                    cleaned_lines.append('')

        return '\n'.join(cleaned_lines)
