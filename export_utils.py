"""
export_utils.py — Build and export TKB tables for individual teachers or classes.

Table shape (8 data rows):
  SÁNG  | Tiết 1 | 7h00-7h45   | T.Hai | T.Ba | T.Tư | T.Năm | T.Sáu | (T.Bảy)
         | Tiết 2 | 8h15-9h00   | ...
         | Tiết 3 | 9h00-9h45   | ...
         | Tiết 4 | 9h55-10h40  | ...
  CHIỀU | Tiết 1 | 13h15-14h00  | ...
         | Tiết 2 | 14h00-14h45 | ...
         | Tiết 3 | 15h00-15h45 | ...
         | Tiết 4 | 15h45-16h30 | ...
"""

from io import BytesIO

import pandas as pd

SANG_TIMES  = {1: "7h30-8h15", 2: "8h45-9h30",
               3: "9h30-10h15", 4: "10h25-11h10"}   # Tiết 1–4, có giờ chơi 8h15-8h45 và 10h15-10h25
CHIEU_TIMES = {1: "13h15-14h00", 2: "14h00-14h45",
               3: "15h00-15h45",  4: "15h45-16h30"}

DAYS_DISPLAY = ["T.Hai", "T.Ba", "T.Tư", "T.Năm", "T.Sáu"]
DAYS_DATA    = ["Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6"]
SAT_DISPLAY, SAT_DATA = "T.Bảy", "Thứ 7"   # chỉ hiện khi bảng có tiết Thứ 7 (vd GDTC sáng T7)


def days_of(rows: list[dict]) -> list[str]:
    return DAYS_DISPLAY + ([SAT_DISPLAY] if any(r.get(SAT_DISPLAY) for r in rows) else [])


def short_group(code: str) -> str:
    """CT10-AVBN-08 → AVBN-08, TC10-CL-08 → GDTC-CL-08, TN12-ANH-01 → TN-ANH-01."""
    parts = str(code).split("-")
    rest = "-".join(parts[1:])
    return f"GDTC-{rest}" if code.startswith("TC") else f"TN-{rest}" if code.startswith("TN") else rest


def _lookup(sub: pd.DataFrame, day: str, tiet: int, buoi: str, value_col: str) -> str:
    mask = (sub["thu"] == day) & (sub["tiet"] == tiet) & (sub["buoi"] == buoi)
    rows = sub[mask]
    if rows.empty:
        return ""
    vals = rows[value_col].dropna().tolist()
    return " / ".join(str(v) for v in vals) if vals else ""


# Thứ 2 Tiết 1 Sáng: cố định Chào cờ. Tiết 2 KHÔNG còn cố định "TNHN" —
# đây là ô lịch bình thường, môn thật sự được xếp phải hiển thị đúng.
FIXED_SANG = {1: "Chào cờ"}


def _build_rows(sub: pd.DataFrame, value_col: str,
                is_class: bool = False) -> list[dict]:
    rows = []
    for tiet, tg in SANG_TIMES.items():
        row = {"buoi": "SÁNG", "tiet": tiet, "tg": tg}
        for day_data, day_disp in zip(DAYS_DATA + [SAT_DATA], DAYS_DISPLAY + [SAT_DISPLAY]):
            # Chào cờ: chỉ hiển thị ở Thứ 2, Tiết 1 khi xem theo lớp
            if is_class and day_data == "Thứ 2" and tiet in FIXED_SANG:
                row[day_disp] = FIXED_SANG[tiet]
            else:
                row[day_disp] = _lookup(sub, day_data, tiet, "Sáng", value_col)
        rows.append(row)
    for tiet, tg in CHIEU_TIMES.items():
        row = {"buoi": "CHIỀU", "tiet": tiet, "tg": tg}
        for day_data, day_disp in zip(DAYS_DATA + [SAT_DATA], DAYS_DISPLAY + [SAT_DISPLAY]):
            row[day_disp] = _lookup(sub, day_data, tiet, "Chiều", value_col)
        rows.append(row)
    return rows


def build_teacher_table(
    timetable: pd.DataFrame, teacher_name: str, groups: pd.DataFrame | None = None,
) -> tuple[list[dict], str, int]:
    """Returns (rows, subject, n_periods). Cells contain class names (và mã nhóm tự chọn
    nếu truyền `groups` — xem database.get_group_periods). Hỗ trợ team-teaching."""
    sub = timetable[timetable["ten_gv"].apply(
        lambda x: teacher_name in [t.strip() for t in str(x).split(" / ")]
    )].copy()
    is_cc = sub["mon"].isin(FIXED_SANG.values())
    teaching = sub[~is_cc]
    # Môn chính = môn dạy nhiều tiết nhất (không tính Chào cờ, ưu tiên môn thường hơn "CĐ ...")
    counts = teaching["mon"].value_counts()
    subject = max(counts.index, key=lambda m: (counts[m], not str(m).startswith("CĐ "))) \
        if not counts.empty else ""
    sub["cell"] = sub["lop"].where(~is_cc, "Chào cờ " + sub["lop"].astype(str))
    n = len(teaching)
    if groups is not None and not groups.empty:
        g = groups[groups["gv"] == teacher_name].drop_duplicates(["ma_nhom", "thu", "buoi", "tiet"])
        if not g.empty:
            sub = pd.concat([sub, g.assign(cell=g["ma_nhom"])[["thu", "tiet", "buoi", "cell"]]])
            n += len(g)
            if not subject:
                kinds = {"GDTC" if c.startswith("TC") else c.split("-")[1] for c in g["ma_nhom"]}
                subject = "/".join(sorted(kinds))
    return _build_rows(sub, "cell"), subject, n


def build_class_table(
    timetable: pd.DataFrame, class_name: str, groups: pd.DataFrame | None = None,
) -> tuple[list[dict], int]:
    """Returns (rows, n_periods). Cells contain subject names; ô có nhóm tự chọn
    (HS của lớp học nhóm đó) ghi mã nhóm rút gọn, vd "AVBN-08, STEM-02"."""
    sub = timetable[timetable["lop"] == class_name].copy()
    # Loại Chào cờ cố định khỏi sub (hiển thị riêng qua is_class=True) —
    # KHÔNG loại TNHN nữa vì giờ nó là môn xếp bình thường, có thể ở bất kỳ tiết nào.
    sub = sub[~sub["mon"].isin(FIXED_SANG.values())]
    n = len(sub)
    if groups is not None and not groups.empty:
        g = groups[groups["lop"] == class_name]
        if not g.empty:
            g = (g.assign(mon=g["ma_nhom"].map(short_group))
                  .groupby(["thu", "tiet", "buoi"])["mon"]
                  .agg(lambda x: ", ".join(sorted(set(x)))).reset_index())
            sub = pd.concat([sub, g])
    return _build_rows(sub, "mon", is_class=True), n


def _row_height(row: dict, days: list[str], chars_per_line: int = 14) -> float:
    """Ô dài (nhiều mã nhóm) → tăng chiều cao dòng để chữ xuống dòng không bị che."""
    longest = max((len(str(row.get(d, "") or "")) for d in days), default=0)
    return 18 * max(1, -(-longest // chars_per_line))


# ── HTML ─────────────────────────────────────────────────────────────────────

def render_html_table(
    rows: list[dict],
    title: str,
    subtitle: str,
    apply_date: str = "05-09-2024",
) -> str:
    cell  = "border:1px solid #888; padding:4px 6px; text-align:center; font-size:13px; font-family:Arial,sans-serif;"
    th    = f"background:#217346; color:white; font-weight:bold; {cell}"
    sang_bg  = "background:#E2EFDA;"
    chieu_bg = "background:#DEEAF1;"
    sang_label  = "background:#70AD47; color:white; font-weight:bold; writing-mode:vertical-rl; text-align:center; border:1px solid #888; padding:4px; font-family:Arial,sans-serif;"
    chieu_label = "background:#2E75B6; color:white; font-weight:bold; writing-mode:vertical-rl; text-align:center; border:1px solid #888; padding:4px; font-family:Arial,sans-serif;"

    lines = ['<div style="font-family:Arial,sans-serif; max-width:960px; overflow-x:auto;">']
    heading = f"THỜI KHÓA BIỂU ÁP DỤNG TỪ {apply_date}" if apply_date else "THỜI KHÓA BIỂU"
    lines.append(f'<p style="text-align:center;font-weight:bold;margin:4px 0;">{heading}</p>')
    lines.append('<table style="border-collapse:collapse;width:100%;">')

    # Info row
    lines.append('<tr>')
    days = days_of(rows)
    lines.append(f'<td colspan="{3 + len(days)}" style="background:#70AD47;color:white;font-weight:bold;text-align:center;border:1px solid #888;padding:6px;font-size:14px;font-family:Arial,sans-serif;">')
    lines.append(f'{title} &nbsp;&nbsp; {subtitle}')
    lines.append('</td></tr>')

    # Header row
    lines.append('<tr>')
    for h in ["", "Tiết", "Thời gian"] + days:
        lines.append(f'<th style="{th}">{h}</th>')
    lines.append('</tr>')

    sang_rows  = [r for r in rows if r["buoi"] == "SÁNG"]
    chieu_rows = [r for r in rows if r["buoi"] == "CHIỀU"]

    for section, data_rows, bg, label_style in [
        ("SÁNG",  sang_rows,  sang_bg,  sang_label),
        ("CHIỀU", chieu_rows, chieu_bg, chieu_label),
    ]:
        for i, row in enumerate(data_rows):
            lines.append('<tr>')
            if i == 0:
                lines.append(f'<td rowspan="{len(data_rows)}" style="{label_style}">{section}</td>')
            lines.append(f'<td style="{cell}{bg}">Tiết {row["tiet"]}</td>')
            lines.append(f'<td style="{cell}{bg} white-space:nowrap;">{row["tg"]}</td>')
            for d in days:
                val = row.get(d, "")
                lines.append(f'<td style="{cell}">{val}</td>')
            lines.append('</tr>')

    lines.append('</table></div>')
    return "\n".join(lines)


# ── Excel (single sheet) ──────────────────────────────────────────────────────

def _apply_style(cell, font=None, fill=None, alignment=None, border=None):
    if font:      cell.font      = font
    if fill:      cell.fill      = fill
    if alignment: cell.alignment = alignment
    if border:    cell.border    = border


def export_to_excel(
    rows: list[dict],
    title: str,
    subtitle: str,
    sheet_name: str = "TKB",
    apply_date: str = "05-09-2024",
) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    def fill(hex_color: str) -> PatternFill:
        return PatternFill("solid", start_color=hex_color, end_color=hex_color)

    thin   = Side(style="thin", color="AAAAAA")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    vert   = Alignment(horizontal="center", vertical="center",
                       text_rotation=90, wrap_text=False)

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name[:31]

    from openpyxl.utils import get_column_letter
    days = days_of(rows)
    last = get_column_letter(3 + len(days))
    ws.column_dimensions["A"].width = 5
    ws.column_dimensions["B"].width = 8
    ws.column_dimensions["C"].width = 14
    for i in range(len(days)):
        ws.column_dimensions[get_column_letter(4 + i)].width = 12

    # Row 1 — date title
    ws.merge_cells(f"A1:{last}1")
    c = ws["A1"]
    c.value     = f"THỜI KHÓA BIỂU ÁP DỤNG TỪ {apply_date}"
    c.font      = Font(bold=True, name="Arial", size=11)
    c.alignment = center
    ws.row_dimensions[1].height = 20

    # Row 2 — entity info
    ws.merge_cells(f"A2:{last}2")
    c = ws["A2"]
    c.value     = f"{title}    {subtitle}"
    c.font      = Font(bold=True, color="FFFFFF", name="Arial", size=12)
    c.fill      = fill("70AD47")
    c.alignment = center
    c.border    = border
    ws.row_dimensions[2].height = 24

    # Row 3 — column headers
    for ci, h in enumerate(["", "Tiết", "Thời gian"] + days, start=1):
        c = ws.cell(row=3, column=ci, value=h)
        c.font      = Font(bold=True, color="FFFFFF", name="Arial", size=10)
        c.fill      = fill("217346")
        c.alignment = center
        c.border    = border
    ws.row_dimensions[3].height = 18

    sang_rows   = [r for r in rows if r["buoi"] == "SÁNG"]
    chieu_rows  = [r for r in rows if r["buoi"] == "CHIỀU"]
    sang_start  = 4
    sang_end    = sang_start + len(sang_rows) - 1
    chieu_start = sang_end + 1
    chieu_end   = chieu_start + len(chieu_rows) - 1

    for section, data_rows, row_start, sfill_hex, sbg_hex in [
        ("SÁNG",  sang_rows,  sang_start,  "70AD47", "E2EFDA"),
        ("CHIỀU", chieu_rows, chieu_start, "2E75B6", "DEEAF1"),
    ]:
        row_end = row_start + len(data_rows) - 1

        ws.merge_cells(start_row=row_start, start_column=1,
                       end_row=row_end,     end_column=1)
        c = ws.cell(row=row_start, column=1, value=section)
        c.font      = Font(bold=True, color="FFFFFF", name="Arial", size=10)
        c.fill      = fill(sfill_hex)
        c.alignment = vert
        c.border    = border

        for i, row in enumerate(data_rows):
            er = row_start + i
            ws.row_dimensions[er].height = _row_height(row, days)

            c = ws.cell(row=er, column=2, value=f"Tiết {row['tiet']}")
            c.font = Font(name="Arial", size=10)
            c.fill = fill(sbg_hex); c.alignment = center; c.border = border

            c = ws.cell(row=er, column=3, value=row["tg"])
            c.font = Font(name="Arial", size=10)
            c.fill = fill(sbg_hex); c.alignment = center; c.border = border

            for di, day in enumerate(days, start=4):
                val = row.get(day, "")
                c = ws.cell(row=er, column=di, value=val)
                c.font = Font(name="Arial", size=10)
                c.fill = fill("FFFFFF"); c.alignment = center; c.border = border

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── Excel (multi-sheet) ───────────────────────────────────────────────────────

def _write_table_to_ws(ws, rows: list[dict], title: str, subtitle: str,
                       apply_date: str, row_offset: int, col_offset: int = 0) -> None:
    """Write a single timetable block to ws at (row_offset, col_offset+1), 1-based rows."""
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    def fill(hex_color: str) -> PatternFill:
        return PatternFill("solid", start_color=hex_color, end_color=hex_color)

    thin   = Side(style="thin", color="AAAAAA")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    vert   = Alignment(horizontal="center", vertical="center",
                       text_rotation=90, wrap_text=False)

    days = days_of(rows)
    end_col = col_offset + 3 + len(days)
    r = row_offset

    ws.merge_cells(start_row=r, start_column=col_offset + 1, end_row=r, end_column=end_col)
    c = ws.cell(row=r, column=col_offset + 1, value=f"THỜI KHÓA BIỂU ÁP DỤNG TỪ {apply_date}")
    c.font = Font(bold=True, name="Arial", size=11)
    c.alignment = center
    ws.row_dimensions[r].height = 20
    r += 1

    ws.merge_cells(start_row=r, start_column=col_offset + 1, end_row=r, end_column=end_col)
    c = ws.cell(row=r, column=col_offset + 1, value=f"{title}    {subtitle}")
    c.font = Font(bold=True, color="FFFFFF", name="Arial", size=12)
    c.fill = fill("70AD47"); c.alignment = center; c.border = border
    ws.row_dimensions[r].height = 24
    r += 1

    for ci, h in enumerate(["", "Tiết", "Thời gian"] + days, start=col_offset + 1):
        c = ws.cell(row=r, column=ci, value=h)
        c.font = Font(bold=True, color="FFFFFF", name="Arial", size=10)
        c.fill = fill("217346"); c.alignment = center; c.border = border
    ws.row_dimensions[r].height = 18
    r += 1

    sang_rows  = [x for x in rows if x["buoi"] == "SÁNG"]
    chieu_rows = [x for x in rows if x["buoi"] == "CHIỀU"]

    for section, data_rows, sfill_hex, sbg_hex in [
        ("SÁNG",  sang_rows,  "70AD47", "E2EFDA"),
        ("CHIỀU", chieu_rows, "2E75B6", "DEEAF1"),
    ]:
        row_start = r
        row_end   = r + len(data_rows) - 1
        ws.merge_cells(start_row=row_start, start_column=col_offset + 1,
                       end_row=row_end,     end_column=col_offset + 1)
        c = ws.cell(row=row_start, column=col_offset + 1, value=section)
        c.font = Font(bold=True, color="FFFFFF", name="Arial", size=10)
        c.fill = fill(sfill_hex); c.alignment = vert; c.border = border

        for dr in data_rows:
            ws.row_dimensions[r].height = max(ws.row_dimensions[r].height or 0, _row_height(dr, days))
            c = ws.cell(row=r, column=col_offset + 2, value=f"Tiết {dr['tiet']}")
            c.font = Font(name="Arial", size=10)
            c.fill = fill(sbg_hex); c.alignment = center; c.border = border

            c = ws.cell(row=r, column=col_offset + 3, value=dr["tg"])
            c.font = Font(name="Arial", size=10)
            c.fill = fill(sbg_hex); c.alignment = center; c.border = border

            for di, day in enumerate(days, start=col_offset + 4):
                val = dr.get(day, "")
                c = ws.cell(row=r, column=di, value=val)
                c.font = Font(name="Arial", size=10)
                c.fill = fill("FFFFFF"); c.alignment = center; c.border = border
            r += 1


def export_teachers_one_sheet(
    timetable: pd.DataFrame,
    teacher_names: list[str],
    apply_date: str = "05-09-2024",
    rows_per_page: int = 2,
    groups: pd.DataFrame | None = None,
) -> bytes:
    """Xuất TKB mọi GV vào 1 sheet, xếp 2 bảng cạnh nhau mỗi hàng."""
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.pagebreak import Break

    BLOCK_ROWS, GAP_ROWS = 11, 2
    tables = [(name, *build_teacher_table(timetable, name, groups)) for name in teacher_names]
    ncol = max([3 + len(days_of(t[1])) for t in tables] or [8])
    RIGHT_COL = ncol + 1  # bảng phải cách bảng trái 1 cột trống

    wb = Workbook()
    ws = wb.active
    ws.title = "TKB GV"
    widths = [5, 8, 14] + [12] * (ncol - 3)
    for off in (0, RIGHT_COL):
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(off + i)].width = w
    ws.column_dimensions[get_column_letter(RIGHT_COL)].width = 3

    for i, (name, rows, subject, n) in enumerate(tables):
        pair = i // 2
        row_offset = 1 + pair * (BLOCK_ROWS + GAP_ROWS)
        _write_table_to_ws(
            ws, rows, title=name, subtitle=f"({subject})   Số tiết dạy: {n}",
            apply_date=apply_date, row_offset=row_offset,
            col_offset=0 if i % 2 == 0 else RIGHT_COL,
        )
        if i % 2 == 1 and (pair + 1) % rows_per_page == 0:
            ws.row_breaks.append(Break(id=row_offset + BLOCK_ROWS))

    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_options.horizontalCentered = True

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_classes_by_grade_to_excel(
    timetable: pd.DataFrame,
    apply_date: str = "05-09-2024",
    groups: pd.DataFrame | None = None,
) -> bytes:
    """Export all classes grouped by grade into a 3-sheet workbook (Khối 10/11/12)."""
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)

    col_widths = {"A": 5, "B": 8, "C": 14,
                  "D": 16, "E": 16, "F": 16, "G": 16, "H": 16, "I": 16}

    for grade in ["10", "11", "12"]:
        classes = sorted(
            timetable[timetable["lop"].str.startswith(grade)]["lop"].unique()
        )
        if not classes:
            continue

        ws = wb.create_sheet(title=f"Khối {grade}")
        for col, w in col_widths.items():
            ws.column_dimensions[col].width = w

        row_offset = 1
        for cls_name in classes:
            rows, n = build_class_table(timetable, cls_name, groups)
            _write_table_to_ws(
                ws, rows,
                title=f"Lớp {cls_name}",
                subtitle=f"Số tiết: {n}",
                apply_date=apply_date,
                row_offset=row_offset,
            )
            # Each block: 1 date row + 1 info row + 1 header + 4 sáng + 4 chiều = 11
            row_offset += 11 + 2  # +2 gap rows between tables

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()

