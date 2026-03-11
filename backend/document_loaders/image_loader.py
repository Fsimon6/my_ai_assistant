"""
图片文档加载器（基础版，支持OCR）
"""
from typing import Optional, List, Dict, Any
import base64
from io import BytesIO
from PIL import Image
import pytesseract
from .base_loader import DocumentLoader
from .models import Document, ProcessingConfig


class ImageLoader(DocumentLoader):
    """图片文档加载器"""

    def __init__(self, config: Optional[ProcessingConfig] = None):
        super().__init__(config)
        self.supported_extensions = (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tiff")
        self.use_ocr = config.extract_text if config else False

    def load(self, file_path: str) -> Document:
        """加载图片文件"""
        with open(file_path, "rb") as f:
            file_bytes = f.read()

        return self.load_from_bytes(file_bytes, file_path)

    def load_from_bytes(self, file_bytes: bytes, filename: str) -> Document:
        """从字节加载图片"""
        metadata = self.extract_metadata(filename, file_bytes)

        try:
            # 获取图片信息
            image = Image.open(BytesIO(file_bytes))

            # 提取图片元数据
            image_metadata = self._extract_image_metadata(image, file_bytes)
            metadata.update(image_metadata)

            # 如果启用OCR，提取文字
            extracted_text = ''
            if self.use_ocr:
                extracted_text = self._extract_text_from_image(image)

            # 构建文档内容
            content_parts = []

            if extracted_text:
                content_parts.append('=== 图片中的文字 ===')
                content_parts.append(extracted_text)
                content_parts.append('')

            content_parts.append('=== 图片信息 ===')
            content_parts.append(f'格式：{metadata.get("format", "Unknown")}')
            content_parts.append(f'尺寸：{metadata.get("width", 0)} * {metadata.get("height", 0)}')
            content_parts.append(f'模式：{metadata.get("mode", "Unknown")}')

            if metadata.get('dpi'):
                content_parts.append(f'DPI: {metadata.get("dpi")}')

            full_content = '\n'.join(content_parts)

            return Document(
                content=full_content,
                metadata=metadata,
                source_type='image'
            )

        except Exception as e:
            raise ValueError(f'图片处理失败：{str(e)}')

    def _extract_image_metadata(self, image: Image.Image, file_bytes: bytes) -> Dict[str, Any]:
        """提取图片元数据"""
        metadata = {
            'format': image.format,
            'width': image.width,
            'height': image.height,
            'mode': image.mode,
            'size_bytes': len(file_bytes),
            'has_alpha': image.mode in ['RGBA', 'LA', 'P'],
            'is_animated': getattr(image, 'is_animated', False),
        }

        # 尝试获取DPI
        try:
            dpi = image.info.get('dpi')
            if dpi:
                metadata['dpi'] = dpi
        except:
            pass

        # 尝试获取EXIF数据
        try:
            exif_data = image._getexif()
            if exif_data:
                metadata['exif'] = self._parse_exif_data(exif_data)
        except:
            pass

        return metadata

    def _parse_exif_data(self, exif_data: Dict) -> Dict[str, Any]:
        """解析EXIF数据"""
        parsed = {}

        # EXIF标签定义（部分）
        exif_tags = {
            0x0100: 'ImageWidth',
            0x0101: 'ImageHeight',
            0x010E: 'ImageDescription',
            0x010F: 'Make',
            0x0110: 'Model',
            0x0112: 'Orientation',
            0x011A: 'XResolution',
            0x011B: 'YResolution',
            0x0128: 'ResolutionUnit',
            0x0131: 'Software',
            0x0132: 'DataTime',
            0x013B: 'Artist',
            0x013E: 'WhitePoint',
            0x013F: 'PrimaryChromaticities',
            0x0211: 'YCbCrCoefficients',
            0x0213: 'YCbCrPositioning',
            0x0214: 'ReferenceClackWhite',
            0x8298: 'Copyright',
            0x8769: 'ExitOffset',
            0x8825: 'GPSInfo'
        }

        for tag_id, value in exif_data.items():
            tag_name = exif_tags.get(tag_id, f'未知标签：_{tag_id}')

            # 简单处理值
            if isinstance(value, bytes):
                try:
                    value.decode('utf-8', errors='ignore')
                except:
                    value = str(value)

            parsed[tag_name] = value

        return parsed

    def _extract_text_from_image(self, image: Image.Image) -> str:
        """使用OCR提取图片中的文字"""
        try:
            # 检查是否安装了Tesseract
            pytesseract.get_tesseract_version()

            # 预处理图片以提高OCR准确率
            processed_image = self._preprocess_image(image)

            # 设置语言（支持中文和英文）
            lang = 'chi_sim+eng'  # 简体中文 + 英文

            # 提取文字
            text = pytesseract.image_to_string(
                processed_image,
                lang=lang,
                config='--psm 3 --oem 3'
            )

            return text.strip()

        except pytesseract.TesseractNotFoundError:
            print('警告：Tesseract OCR未安装，无法提取图片文字')
            return ''
        except Exception as e:
            print(f'OCR提取失败：{e}')
            return ''

    def _preprocess_image(self, image: Image.Image) -> Image.Image:
        """预处理图片以提高ORC准确率"""
        # 转换为灰度图
        if image.mode != 'L':
            image = image.convert('L')

        # 图片增强步骤（根据需要开启）
        if True:  # 设置True以启动增强
            import numpy as np
            # 转换为numpy数组
            img_array = np.array(image)

            # 增加对比度
            from PIL import ImageEnhance
            enhancer = ImageEnhance.Contrast(image)
            image = enhancer.enhance(2.0)

            # 二值化处理
            threshold = 128
            image = image.point(lambda p: p > threshold and 255)

        return image

    def get_image_preview(self, file_bytes: bytes, max_size: tuple = (300, 300)) -> str:
        """获取图片的base64预览"""
        try:
            image = Image.open(BytesIO(file_bytes))

            # 调整大小
            image.thumbnail(max_size, Image.Resampling.LANCZOS)

            # 转换为base64
            buffered = BytesIO()
            image.save(buffered, format='PNG')
            img_str = base64.b64encode(buffered.getvalue()).decode()

            return f'data:image/png;base64,{img_str}'

        except Exception as e:
            print(f'生成图片预览失败：{e}')
            return ''

    def detect_text_regions(self, file_bytes: bytes) -> List[Dict[str, Any]]:
        """检测图片中的文字区域"""
        try:
            image = Image.open(BytesIO(file_bytes))

            # 使用PCR获取文字位置信息
            data = pytesseract.image_to_data(
                image,
                lang='chi_sim+eng',
                output_type=pytesseract.Output.DICT,
            )

            regions = []

            for i in range(len(data['text'])):
                text = data['text'][i].strip()
                if text:    # 只保留有文字的区域
                    region = {
                        'text': text,
                        'x': data['left'][i],
                        'y': data['top'][i],
                        'width': data['width'][i],
                        'height': data['height'][i],
                        'confidence': data['conf'][i],
                    }

                    regions.append(region)

            return regions

        except Exception as e:
            print(f'文字区域检测失败：{e}')
            return []
