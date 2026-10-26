# -*- coding: utf-8 -*-
"""零列工作表的底层查询保护（合成 representation，不使用真实 Excel）。"""
import pytest

from backend.excel import engine as query_engine
from backend.excel import query as excel_query
from backend.excel.representation import (
    HEADER_NONE,
    SheetRepresentation,
    WorkbookRepresentation,
)


def _zero_column_representation() -> WorkbookRepresentation:
    sheet = SheetRepresentation(
        sheet_name='Blank',
        sheet_index=0,
        header_mode=HEADER_NONE,
        columns=[],
        rows=[],
        row_excel_numbers=[],
        row_count=0,
        column_count=0,
        warnings=['Sheet 内无有效数据'],
    )
    return WorkbookRepresentation(
        schema_version='1.0',
        document_id='zero-column-doc',
        user_id=1,
        filename='blank.xlsx',
        file_type='xlsx',
        parser='test',
        sheet_count=1,
        sheets=[sheet],
    )


@pytest.mark.parametrize('engine_name', ['duckdb', 'python'])
def test_zero_column_sheet_returns_classified_query_error(engine_name):
    """两个引擎都必须返回 ExcelQueryError，不能生成非法 DDL 或伪装成功。"""
    rep = _zero_column_representation()

    with pytest.raises(excel_query.ExcelQueryError) as exc_info:
        query_engine.run_structured_query(
            rep,
            {'sheet_index': 0},
            engine=engine_name,
        )

    err = exc_info.value
    assert err.code == excel_query.ERR_INVALID_PARAM
    assert err.status == 400
    assert '没有任何列' in err.message


def test_auto_engine_zero_column_does_not_leak_parser_exception():
    """auto 默认优先 DuckDB，但零列必须在 DDL 前转为可分类错误。"""
    rep = _zero_column_representation()

    with pytest.raises(excel_query.ExcelQueryError) as exc_info:
        query_engine.run_structured_query(
            rep,
            {'sheet_index': 0},
            engine='auto',
        )

    assert exc_info.value.code == excel_query.ERR_INVALID_PARAM
    assert 'ParserException' not in str(exc_info.value)
