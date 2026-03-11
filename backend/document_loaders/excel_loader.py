"""
Excel文档加载器
"""
from typing import Optional, List, Dict, Any
import pandas as pd
from io import BytesIO
from .base_loader import DocumentLoader
from .models import Document, ProcessingConfig


class ExcelLoader(DocumentLoader):
    """Excel文档加载器"""

    def __init__(self, config: Optional[ProcessingConfig] = None):
        super().__init__(config)
        self.supported_extensions = ['.xlsx', '.xls', '.csv']

    def load(self, file_path: str) -> Document:
        """加载Excel文件"""
        with open(file_path, 'rb') as f:
            file_bytes = f.read()

        return self.load_from_bytes(file_bytes, file_path)

    def load_from_bytes(self, file_bytes: bytes, filename: str) -> Document:
        """从字节加载Excel"""
        metadata = self.extract_metadata(filename, file_bytes)
        try:
            file_ext = filename.lower().split('.')[-1]

            if file_ext == 'csv':
                # 处理CSV文件
                text = self._load_csv(file_bytes)
                sheet_count = 1
            else:
                # 处理Excel文件
                text, sheet_count = self._load_excel(file_bytes)
                metadata.update({
                    'sheet_count': sheet_count,
                    'file_type': 'excel' if file_ext == ['xls', 'xlsx'] else 'csv'
                })

            return Document(
                content=text,
                metadata=metadata,
                source_type='excel'
            )

        except Exception as e:
            raise ValueError(f'Excel解析失败：{str(e)}')

    def _load_excel(self, file_bytes: bytes) -> tuple:
        """加载Excel文件内容"""
        try:
            excel_file = BytesIO(file_bytes)
            # 读取所有sheet
            xls = pd.ExcelFile(excel_file)
            sheet_names = xls.sheet_names
            all_sheet_text = []

            for sheet_name in sheet_names:
                try:
                    # 读取sheet
                    df = pd.read_excel(excel_file, sheet_name=sheet_name)

                    # 读取sheet内容
                    sheet_text = self._dataframe_to_text(df, sheet_name)
                    all_sheet_text.append(sheet_text)
                except Exception as e:
                    print(f'读取sheet{sheet_name}失败：{e}')
                    continue
            return '\n\n'.join(all_sheet_text), len(sheet_names)

        except Exception as e:
            raise ValueError(f'Excel文件读取失败：{str(e)}')

    def _load_csv(self, file_bytes: bytes) -> str:
        """加载CSV文件内容"""
        try:
            # 尝试不同的编码
            encodings = ['utf-8', 'gbk', 'gb2312', 'latin1']
            for encoding in encodings:
                try:
                    # 解码文件内容
                    csv_content = file_bytes.decode(encoding)

                    # 使用pandas读取
                    df = pd.read_csv(BytesIO(file_bytes), encoding=encoding)

                    # 转换为文本
                    return self._dataframe_to_text(df, 'CSV数据')

                except UnicodeDecodeError:
                    continue
                except Exception:
                    continue

            raise ValueError('无法解码CSV文件，请检查文件编码')

        except Exception as e:
            raise ValueError(f'CSV文件读取失败：{str(e)}')

    def _dataframe_to_text(self, df: pd.DataFrame, sheet_name: str = '') -> str:
        """将DataFrame转换为文本"""
        if df.empty:
            return f'Sheet:{sheet_name}\n[空表]'

        # 获取基本信息
        rows, cols = df.shape
        text_parts = []

        # 添加sheet标题
        if sheet_name:
            text_parts.append(f'=== {sheet_name} ===')
            text_parts.append(f'行数：{rows}，列数：{cols}')
            text_parts.append('')

        # 添加列名
        columns = list(df.columns)
        text_parts.append(f'列名：{",".join(map(str, columns))}')
        text_parts.append('')

        # 对于大型数据集，只显示部分数据
        if rows > 100:
            # 显示前10行和后10行
            head_df = df.head(10)
            tail_df = df.tail(10)
            text_parts.append('前10行数据：')
            text_parts.append(self._format_dataframe(head_df))
            text_parts.append('')
            text_parts.append('后10行数据：')
            text_parts.append(self._format_dataframe(tail_df))
            text_parts.append(f'...省略中间{rows - 20}行')
        else:
            text_parts.append('全部数据：')
            text_parts.append(self._format_dataframe(df))

        # 添加统计信息
        text_parts.append('')
        text_parts.append('=== 数据统计 ===')

        try:
            # 数据列的统计
            numeric_cols = df.select_dtypes(include=['number']).columns
            if len(numeric_cols) > 0:
                stats = df[numeric_cols].describe()
                text_parts.append('数值列统计：')
                text_parts.append(str(stats))

        except Exception:
            pass

        return '\n'.join(text_parts)

    def _format_dataframe(self, df: pd.DataFrame) -> str:
        """格式化DataFrame为字符串"""
        # 限制列宽
        pd.set_option('display.max_colwidth', 50)
        pd.set_option('display.width', 1000)
        return str(df)

    def extract_sheet_names(self, file_bytes: bytes) -> List[str]:
        """提取sheet名称"""
        try:
            excel_file = BytesIO(file_bytes)
            xls = pd.ExcelFile(excel_file)
            return xls.sheet_names
        except Exception:
            return []

    def get_sheet_data(self, file_bytes: bytes, sheet_name: str) -> Dict[str, Any]:
        """获取指定sheet的数据"""
        try:
            excel_file = BytesIO(file_bytes)
            df = pd.read_excel(excel_file, sheet_name=sheet_name)

            return {
                'sheet_name': sheet_name,
                'shape': df.shape,
                'columns': list(df.columns),
                'data': df.head(100).to_dict(orient='records'), # 只返回前100行
                'dtype': df.dtypes.astype(str).to_dict()
            }

        except Exception as e:
            return {
                'sheet_name': sheet_name,
                'error': str(e)
            }
