"""
文档加载器单元测试
"""
import pytest
import tempfile
import os
from pathlib import Path
from unittest.mock import patch, MagicMock
from backend.document_loaders.factory import DocumentLoaderFactory
from backend.document_loaders.models import ProcessingConfig, Document
from backend.document_loaders.pdf_loader import PDFLoader
from backend.document_loaders.txt_loader import TXTLoader
from backend.document_loaders.docx_loader import DOCXLoader
from backend.document_loaders.excel_loader import ExcelLoader
from backend.document_loaders.image_loader import ImageLoader
from document_loaders import DocumentLoader


class TestDocumentLoader:
    """文档加载器测试类"""

    def test_factory_supported_extensions(self):
        """测试工厂类支持的文件扩展名"""

        extensions = DocumentLoader.supported_extensions()
        assert '.pdf' in extensions
        assert '.txt' in extensions
        assert '.docx' in extensions
        assert '.xlsx' in extensions
        assert '.jpg' in extensions

    def test_factory_get_loader(self):
        """测试工厂获取加载器"""
        # PDF加载器
        pdf_loader = DocumentLoaderFactory.get_loader('test(空).pdf')
        assert isinstance(pdf_loader, PDFLoader)

        # TXT加载器
        txt_loader = DocumentLoaderFactory.get_loader('test（空）.txt')
        assert isinstance(txt_loader, TXTLoader)

        # DOCX加载器
        docx_loader = DocumentLoaderFactory.get_loader('test.docx')
        assert isinstance(docx_loader, DOCXLoader)

        # 不支持的文件格式
        with pytest.raises(ValueError):
            DocumentLoaderFactory.get_loader('test.unsupported')

    def test_processing_config(self):
        """测试处理配置"""
        config = ProcessingConfig(
            chunk_size=500,
            chunk_overlap=100,
            min_chunk_size=50,
            split_by_sentences=True
        )

        assert config.chunk_size == 500
        assert config.chunk_overlap == 100
        assert config.min_chunk_size == 50
        assert config.split_by_sentences == True

    def test_txt_loader_with_config(self, temp_test_file):
        config = ProcessingConfig(
            chunk_size=100,
            chunk_overlap=20,
        )

        loader = TXTLoader(config)
        document = loader.load(temp_test_file)
        assert isinstance(document, Document)
        assert document.content is not None
        assert 'test document' in document.content.lower()
        assert document.source_type == 'text'
        assert 'filename' in document.metadata

    def test_txt_loader_chunking(self, temp_test_file):
        """测试TXT文档分块"""
        config = ProcessingConfig(
            chunk_size=50,
            chunk_overlap=10,
        )
        loader = TXTLoader(config)
        document = loader.load(temp_test_file)
        chunks = loader.chunk_document(document)
        assert len(chunks) > 0
        for chunk in chunks:
            assert chunk.content is not None
            assert len(chunk.content) <= config.chunk_size + 10     # 允许一些余量
            assert chunk.chunk_index >= 0

    def test_pdf_loader_mock(self):
        """测试PDF加载器（使用mock）"""
        with patch('fitz.open') as mock_fitz:
            # 配置mock
            mock_doc = MagicMock()
            mock_page = MagicMock()
            mock_page.get_text.return_value = 'PDF page content'
            mock_doc.__len__.return_value = 1
            mock_doc.__getitem__.return_value = mock_page
            mock_doc.metadata = {'author': 'Test Author'}
            mock_doc.close = MagicMock()
            mock_fitz.return_value = mock_doc

            # 创建临时PDF文件
            with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
                f.write(b'PDF dummy content')
                pdf_path = f.name

            try:
                loader = PDFLoader()
                document = loader.load(pdf_path)
                assert document.content is not None
                assert 'PDF page content' in document.content
                assert document.source_type == 'pdf'
                assert document.metadata['page_count'] == 1
            finally:
                if os.path.exists(pdf_path):
                    os.unlink(pdf_path)

    def test_docx_loader_mock(self):
        """测试DOCX加载器（使用mock）"""
        with patch('docx.Document') as mock_docx:
            # 配置mock
            mock_document = MagicMock()
            mock_paragraph = MagicMock()
            mock_paragraph.text = "DOCX paragraph content"
            mock_document.paragraphs = [mock_paragraph]
            mock_document.tables = []
            mock_document.core_properties.author = 'Test Author'
            mock_docx.return_value = mock_document

            # 创建临时DOCX文件
            with tempfile.NamedTemporaryFile(suffix='.docx', delete=False) as f:
                f.write(b'DOCX dummy content')
                docx_path = f.name

            try:
                loader = DOCXLoader()
                document = loader.load(docx_path)
                assert document is not None
                assert 'DOCX paragraph content' in document.content
                assert document.source_type == 'docx'
            finally:
                if os.path.exists(docx_path):
                    os.unlink(docx_path)

    def test_excel_loader_mock(self):
        """测试Excel加载器（使用mock）"""
        import pandas as pd
        from io import BytesIO

        # 创建测试DataFrame
        test_data = {
            'Name': ['Alice', 'Bob', 'Charlie'],
            'Age': [25, 30, 35],
            'City': ['New York', 'London', 'Tokyo']
        }
        df = pd.DataFrame(test_data)

        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as f:
            # 保存为Excel
            with pd.ExcelWriter(f.name, engine='openpyxl') as writer:
                df.to_excel(writer, sheet_name='Sheet1', index=False)
                excel_path = f.name

        try:
            loader = ExcelLoader()
            document = loader.load(excel_path)
            assert document is not None
            assert 'Sheet1' in document.content
            assert 'Alice' in document.content
            assert 'New York' in document.content
            assert document.source_type == 'excel'
            assert document.metadata['sheet_count'] == 1
        finally:
            if os.path.exists(excel_path):
                os.unlink(excel_path)

    def test_image_loader_without_ocr(self):
        """测试图片加载器（无OCR）"""
        # 创建测试图片
        from PIL import Image
        import numpy as np

        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
            # 创建简单的测试图片
            img_array = np.random.rand(100, 100, 3) * 255
            img = Image.fromarray(img_array.astype('uint8'))
            img.save(f.name, 'PNG')
            img_path = f.name

        try:
            config = ProcessingConfig(extract_text=False)
            loader = ImageLoader(config)
            document = loader.load(img_path)
            assert document is not None
            assert '图片信息' in document.content
            assert document.source_type == 'image'
            assert document.metadata['width'] == 100
            assert document.metadata['height'] == 100
        finally:
            if os.path.exists(img_path):
                os.unlink(img_path)

    def test_document_model(self):
        """测试文档模型"""
        metadata = {
            'filename': 'test（空）.txt',
            'source': 'test'
        }

        document = Document(
            content="Test content",
            metadata=metadata,
            source_type='text'
        )
        assert document.content == "Test content"
        assert document.metadata['filename'] == 'test（空）.txt'
        assert document.source_type == 'text'

        # 测试转换为字典
        doc_dict = document.to_dict()
        assert doc_dict['content'] == "Test content"
        assert doc_dict['source_type'] == 'text'

        # 测试从字典创建
        new_document = Document.from_dict(doc_dict)
        assert new_document.content == document.content

    def test_chunk_model(self):
        """测试文档快模型"""
        from backend.document_loaders.models import Chunk
        metadata = {
            'chunk_index': 0,
            'start_pos': 0,
            'end_pos': 100,
        }
        chunk = Chunk(
            content='Chunk content',
            metadata=metadata,
            chunk_index=0,
            start_pos=0,
            end_pos=100,
        )

        assert chunk.content == 'Chunk content'
        assert chunk.chunk_index == 0
        assert chunk.strat_pos == 0
        assert chunk.end_pos == 100

        # 测试转换为字典
        chunk_dict = chunk.to_dict()
        assert chunk_dict['content'] == 'Chunk content'
        assert chunk_dict['chunk_index'] == 0

        # 测试从字典创建
        new_chunk = Chunk.from_dict(chunk_dict)
        assert new_chunk.content == chunk.content

    def test_loader_can_load(self):
        """测试加载器支持检查"""
        pdf_loader = PDFLoader()
        assert pdf_loader.can_load('test(空).pdf') is True
        assert pdf_loader.can_load('test（空）.txt') is False
        txt_loader = TXTLoader()
        assert txt_loader.can_load('test（空）.txt') is True
        assert txt_loader.can_load('test.md') is True
        assert txt_loader.can_load('test(空).pdf') is False

    def test_encoding_detection(self):
        """测试编码检测"""
        from backend.document_loaders.txt_loader import TXTLoader
        # 测试UTF-8编码
        with tempfile.NamedTemporaryFile(mode='wb', suffix='.txt', delete=False) as f:
            f.write('UTF-8 测试文本'.encode('utf-8'))
            utf8_path = f.name

        # 测试GBK编码
        with tempfile.NamedTemporaryFile(mode='wb', suffix='.txt', delete=False) as f:
            f.write('GBK 测试文本'.encode('gbk'))
            gbk_path = f.name

        try:
            loader = TXTLoader()

            # UTF-8文件
            utf8_doc = loader.load(utf8_path)
            assert 'UTF-8 测试文本' in utf8_doc.content
            assert utf8_doc.metadata['encoding'] == 'utf-8'

            # GBK文件
            gbk_doc = loader.load(gbk_path)
            assert 'GBK 测试文本' in gbk_doc.content
            assert gbk_doc.metadata['encoding'] in ['gbk', 'gb2312', 'GB2312']
        finally:
            for path in [utf8_path, gbk_path]:
                if os.path.exists(path):
                    os.unlink(path)

    def test_large_file_handling(self):
        """测试大文件处理"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            # 生成包含多行的文本
            for i in range(1000):
                f.write(f'这是第{i}行测试文本， 用于测试大文件处理能力。\n')
            large_file_path = f.name

        try:
            config = ProcessingConfig(chunk_size=500, chunk_overlap=50)
            loader = TXTLoader(config)

            document = loader.load(large_file_path)
            chunks = loader.chunk_document(document)
            assert len(chunks) > 1  # 应该被分成多个块
            total_content = ''.join(chunk.content for chunk in chunks)
            assert len(total_content) >= len(document.content) - 100    # 允许一些差异

            # 检查块重叠
            for i in range(len(chunks) - 1):
                current_chunk = chunks[i].content
                next_chunk = chunks[i + 1].content
                # 检查是否有重叠（简化检查）
                assert len(current_chunk) <= config.chunk_size + 100
        finally:
            if os.path.exists(large_file_path):
                os.unlink(large_file_path)
