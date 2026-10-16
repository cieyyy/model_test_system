"""Sophnet 测试结果的统一 CSV/XLSX 导出。"""

import json
from pathlib import Path


def export_xlsx(out_dir, rows, metadata):
    """将逐请求 rows 导出为带样式的 results.xlsx；不保存密钥。"""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        return None
    out_dir = Path(out_dir)
    wb = Workbook()
    ws = wb.active
    ws.title = "测试结果"
    navy, light = "1F4E78", "F3F6F9"
    thin = Side(style="thin", color="B7C9D6")
    ws.merge_cells("A1:L1")
    ws["A1"] = "Kimi K3 Sophnet 测试结果"
    ws["A1"].font = Font(size=14, bold=True, color="FFFFFF")
    ws["A1"].fill = PatternFill("solid", fgColor=navy)
    ws["A1"].alignment = Alignment(horizontal="center")
    ws.merge_cells("A2:L2")
    ws["A2"] = "；".join(f"{k}：{v}" for k, v in metadata.items() if k not in ("key", "api_key"))
    ws["A2"].alignment = Alignment(wrap_text=True)
    headers = ["编号", "测试项目", "接口", "总耗时（秒）", "Input Tokens", "Output Tokens", "关键响应", "状态", "HTTP状态", "结果原值", "判定说明", "证据文件"]
    for col, value in enumerate(headers, 1):
        cell = ws.cell(4, col, value)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=navy)
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
    for index, row in enumerate(rows, 1):
        def get(*names):
            for name in names:
                if name in row and row[name] not in (None, ""):
                    return row[name]
            return ""
        values = [index, get("case", "kind", "mode"), get("endpoint"), get("elapsed_ms", "total_ms"),
                  get("input_tokens", "prompt_tokens"), get("output_tokens", "completion_tokens"),
                  get("content", "finish_reason", "response_model"), get("result", "status"),
                  get("http_status"), get("finish_reason"), get("note", "error"), get("raw_file", "evidence_file")]
        for col, value in enumerate(values, 1):
            cell = ws.cell(index + 4, col, str(value)[:2000])
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = Border(bottom=thin)
            if index % 2 == 0:
                cell.fill = PatternFill("solid", fgColor="F8FBFD")
    widths = [9, 28, 38, 17, 16, 16, 62, 16, 12, 16, 42, 48]
    for col, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(col)].width = width
    for row in range(5, 5 + len(rows)):
        ws.row_dimensions[row].height = 48
    ws.freeze_panes = "A5"
    ws.sheet_view.showGridLines = False
    summary = wb.create_sheet("测试概览")
    summary["A1"] = "Kimi K3 Sophnet 测试概览"
    summary["A1"].font = Font(size=14, bold=True, color="FFFFFF")
    summary["A1"].fill = PatternFill("solid", fgColor=navy)
    summary.merge_cells("A1:D1")
    for r, (key, value) in enumerate(metadata.items(), 3):
        if key in ("key", "api_key"): continue
        summary.cell(r, 1, key).font = Font(bold=True, color=navy)
        summary.cell(r, 2, str(value))
        summary.cell(r, 1).fill = PatternFill("solid", fgColor=light)
        summary.cell(r, 2).alignment = Alignment(wrap_text=True)
    summary.column_dimensions["A"].width = 24
    summary.column_dimensions["B"].width = 80
    summary.sheet_view.showGridLines = False
    path = out_dir / "results.xlsx"
    wb.save(path)
    return path
