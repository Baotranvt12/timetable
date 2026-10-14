# =============================================================================
# Thời Khoá Biểu — THPT Nguyễn An Ninh  (MVP v2)
# =============================================================================
# Section 1: Hằng số & cấu hình
# Section 2: load_teachers()  — đọc DanhSach → mã GV, họ tên, môn
# Section 3: load_classes()   — đọc DSLop   → danh sách lớp & buổi học
# Section 4: _parse_block2()  — hàm nội bộ đọc Block 2 (tên tắt GV) của TKBS/TKBC
#            load_timetable() — gộp TKBS + TKBC, ánh xạ tên tắt → họ tên đầy đủ
#            [AI HOOK] Thay thế/bổ sung logic ở đây để AI tự xếp hoặc tối ưu TKB
# Section 5: check_conflicts() — phát hiện GV dạy 2 lớp cùng tiết
#            [AI HOOK] AI có thể tự đề xuất hoán đổi tiết để sửa xung đột
# Section 6: render_ui() — giao diện Streamlit
# =============================================================================

import json
import re
import threading
import time as _time
import unicodedata
from pathlib import Path

import streamlit as st
import pandas as pd

import database
import export_utils
import scheduler
import csv_loader
import timetable_rules
import manual_edit

# ── Background CP-SAT tasks (module-level, tồn tại giữa các lần rerun) ────────
_BG_TASKS: dict[str, dict] = {}


def _start_bg_cpsat(task_id: str, bg_state: dict, time_limit: float) -> None:
    """Chạy CP-SAT cải tiến trong background thread."""
    def _worker() -> None:
        try:
            improved = scheduler.cpsat_improve(
                lessons    = bg_state["lessons"],
                hint       = bg_state["assignment"],
                unavailable= bg_state["unavail"],
                max_per_day= bg_state["max_pd"],
                time_limit_s=max(5.0, time_limit),
            )
            base = scheduler.score_assignment(
                bg_state["lessons"], bg_state["assignment"], bg_state["max_pd"]
            )["total_score"]
            metrics = improved and scheduler.score_assignment(
                bg_state["lessons"], improved, bg_state["max_pd"]
            )
            # CP-SAT nền không tính ô sáng trống → chỉ đề xuất khi tổng điểm thật sự tốt hơn
            if metrics and metrics["total_score"] < base:
                result_df = scheduler.assignment_to_df(
                    bg_state["lessons"], improved, bg_state.get("homeroom_map")
                )
                locked_df = bg_state.get("locked_df")
                if locked_df is not None:
                    result_df = pd.concat(
                        [locked_df[["thu", "tiet", "buoi", "lop", "ten_gv", "mon"]], result_df],
                        ignore_index=True,
                    )
                _BG_TASKS[task_id] = {
                    "status":  "done",
                    "df":      result_df,
                    "metrics": metrics,
                }
            else:
                _BG_TASKS[task_id] = {
                    "status": "no_improve",
                    "df":     None,
                    "msg":    "Greedy đã tối ưu — CP-SAT không cải tiến thêm.",
                }
        except Exception as exc:
            _BG_TASKS[task_id] = {"status": "error", "df": None, "msg": str(exc)}

    _BG_TASKS[task_id] = {"status": "running"}
    threading.Thread(target=_worker, daemon=True).start()

DEFAULT_XLS = "/Users/baotran/Downloads/TKB AP DUNG.xls"


def _nfc(s) -> str:
    """Chuẩn hoá Unicode về NFC trước khi so khớp chuỗi tìm kiếm — dữ liệu
    nhập từ vài file Excel gốc có tên GV ở dạng NFD (dấu tách rời), trông
    giống hệt trên màn hình nhưng so khớp chuỗi ('abc' in 'abc') sẽ luôn
    trả về False nếu 2 vế lệch dạng chuẩn hoá. Luôn gọi hàm này cho cả
    chuỗi cần tìm VÀ chuỗi nguồn trước khi so khớp."""
    return unicodedata.normalize("NFC", str(s)) if s is not None else ""

DAYS_ORDER  = ["Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6"]
DAYS_SHORT  = ["T.2", "T.3", "T.4", "T.5", "T.6"]
DAY_KEY     = {d: i for i, d in enumerate(DAYS_ORDER)}

# Chỉ giữ lớp có tên dạng NNAnn (10A1, 11A2, 12A10...)
_CLASS_RE   = re.compile(r"^(10|11|12)A\d+$")

DATA_DIR   = Path("data")
PREFS_FILE = DATA_DIR / "teacher_prefs.json"   # vẫn dùng JSON cho prefs (chuyển DB sau)


# =============================================================================
# Section 2: Danh sách giáo viên
# =============================================================================

def load_teachers(path: str) -> pd.DataFrame:
    """
    Đọc sheet DanhSach.
    Cột: 2=Mã số, 3=Tên đầy đủ, 5=Tên tắt, 7=Môn dạy.
    Trả về DataFrame [ma_so, ten, ten_tat, mon_day].
    """
    df = pd.read_excel(path, sheet_name="DanhSach", header=None, engine="xlrd")
    rows = []
    for _, r in df.iloc[3:].iterrows():
        ma  = r.iloc[2]
        ten = r.iloc[3]
        tat = r.iloc[5]
        mon = r.iloc[7]
        if pd.notna(ma) and pd.notna(ten) and str(ma).strip():
            rows.append({
                "ma_so":   str(ma).strip(),
                "ten":     str(ten).strip(),
                "ten_tat": str(tat).strip() if pd.notna(tat) else "",
                "mon_day": str(mon).strip() if pd.notna(mon) else "",
            })
    return pd.DataFrame(rows)


# =============================================================================
# Section 3: Danh sách lớp
# =============================================================================

def load_classes(path: str) -> pd.DataFrame:
    """
    Đọc sheet DSLop.
    Cột: 2=Tên lớp, 4=Buổi (S/C).
    Trả về DataFrame [lop, buoi, khoi].
    """
    df = pd.read_excel(path, sheet_name="DSLop", header=None, engine="xlrd")
    rows = []
    for _, r in df.iloc[3:].iterrows():
        lop  = r.iloc[2]
        buoi = r.iloc[4]
        if pd.notna(lop) and _CLASS_RE.match(str(lop).strip()):
            rows.append({
                "lop":  str(lop).strip(),
                "buoi": str(buoi).strip() if pd.notna(buoi) else "S",
                "khoi": str(lop).strip()[:2],
            })
    return pd.DataFrame(rows)


# =============================================================================
# Section 4: Đọc TKB từ file Excel
# =============================================================================

def _parse_block2(path: str, sheet_name: str, session_label: str) -> pd.DataFrame:
    """
    Đọc Block 2 của sheet TKBS hoặc TKBC.
    Block 2: header tại row 36, dữ liệu tại rows 37–66 (0-indexed).
    Mỗi 5 rows liên tiếp = 1 ngày (Thứ 2 → Thứ 7).
    Giá trị ô = tên tắt GV (vd: HẠNH (T), M. THANH, HÙNG (AV)...).
    """
    df      = pd.read_excel(path, sheet_name=sheet_name, header=None, engine="xlrd")
    headers = df.iloc[36]           # hàng tiêu đề Block 2
    data    = df.iloc[37:67].reset_index(drop=True)   # 30 hàng dữ liệu

    # Tìm tất cả cột có tên lớp hợp lệ; nếu lớp bị lặp lấy cột đầu tiên
    seen_classes: set[str] = set()
    class_cols: list[tuple[int, str]] = []
    for col_idx, val in enumerate(headers):
        if pd.notna(val) and _CLASS_RE.match(str(val).strip()):
            name = str(val).strip()
            if name not in seen_classes:
                seen_classes.add(name)
                class_cols.append((col_idx, name))

    records = []
    for row_idx in range(len(data)):
        day_idx = row_idx // 5
        tiet    = (row_idx % 5) + 1
        if day_idx >= len(DAYS_ORDER):
            continue
        day = DAYS_ORDER[day_idx]

        row = data.iloc[row_idx]
        for col_idx, cls_name in class_cols:
            val = row.iloc[col_idx]
            if pd.notna(val):
                s = str(val).strip()
                if s and s not in ("CHÀO CỜ", "nan", "0"):
                    records.append({
                        "thu":    day,
                        "tiet":   tiet,
                        "lop":    cls_name,
                        "ten_tat_raw": s,
                        "buoi":   session_label,
                    })
    return pd.DataFrame(records)


def load_timetable(path: str, teachers: pd.DataFrame) -> pd.DataFrame:
    """
    Gộp TKB buổi sáng (TKBS) và chiều (TKBC).
    Ánh xạ tên tắt → họ tên đầy đủ qua bảng DanhSach (khớp không phân biệt hoa/thường).
    Nếu không khớp, giữ nguyên tên tắt.
    """
    # Bảng tra cứu tên tắt → họ tên đầy đủ & môn dạy
    ten_tat_map: dict[str, str] = {}
    mon_map:     dict[str, str] = {}
    for _, r in teachers.iterrows():
        key = r["ten_tat"].strip().upper().replace(" ", "")
        if key:
            ten_tat_map[key] = r["ten"]
            mon_map[key]     = r["mon_day"]

    def resolve(raw: str) -> tuple[str, str]:
        key = raw.strip().upper().replace(" ", "")
        return ten_tat_map.get(key, raw), mon_map.get(key, "")

    tkbs = _parse_block2(path, "TKBS", "Sáng")
    tkbc = _parse_block2(path, "TKBC", "Chiều")
    df   = pd.concat([tkbs, tkbc], ignore_index=True)

    if df.empty:
        return df

    # Loại bỏ trùng lặp trong cùng buổi (sáng/chiều dùng số tiết 1-5 / 1-4 riêng biệt)
    df = df.drop_duplicates(subset=["thu", "tiet", "lop", "buoi"], keep="first")

    df[["ten_gv", "mon"]] = df["ten_tat_raw"].apply(
        lambda v: pd.Series(resolve(v))
    )
    return df.drop(columns=["ten_tat_raw"])


# =============================================================================
# Section 5: Phát hiện xung đột
# =============================================================================

def check_conflicts(timetable: pd.DataFrame) -> pd.DataFrame:
    """
    Phát hiện GV dạy 2+ lớp khác nhau trong cùng một tiết (cùng ngày).
    Hỗ trợ team-teaching: ten_gv = 'T1 / T2' → tách ra kiểm tra từng GV.

    Giữ lại như wrapper mỏng tương thích ngược — logic thật đã chuyển vào
    timetable_rules.find_school_errors() (superset: còn phát hiện thêm lớp
    trùng tiết, GV báo bận vẫn bị xếp dạy, lệch chương trình). Tab "Xung đột"
    cũ đã được thay bằng tab "✏️ Xếp TKB thủ công" → "⚠️ Tìm lỗi toàn trường".
    """
    if timetable.empty:
        return pd.DataFrame()
    issues = [i for i in timetable_rules.find_school_errors(timetable)
              if i.kind == "teacher_double_booking"]
    rows = [{
        "Thứ": i.thu, "Tiết": i.tiet, "Buổi": i.buoi,
        "Giáo viên": i.ten_gv, "Môn": i.mon,
        "Lớp bị trùng": i.message.rsplit(": ", 1)[-1].rstrip("."),
    } for i in issues]
    return pd.DataFrame(rows)


# =============================================================================
# Section 6: Lưu / Tải JSON (chỉ còn dùng cho teacher_prefs)
# =============================================================================

def _save_json(obj: dict, path: Path) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def _load_json(path: Path, default: dict) -> dict:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


# =============================================================================
# Section 7: Phân phối chương trình — dùng SQLite qua database.py
# =============================================================================

def _infer_curriculum(timetable: pd.DataFrame, grade: str) -> list[dict]:
    """Suy số tiết trung bình mỗi môn từ TKB cũ → list để upsert vào DB."""
    sub = timetable[
        timetable["lop"].str.startswith(grade) & (timetable["buoi"] == "Sáng")
    ]
    n_classes = sub["lop"].nunique()
    if n_classes == 0:
        return []
    counts = sub.groupby("mon").size()
    return [{"Môn dạy": mon, "Tiết/tuần": max(1, round(cnt / n_classes)), "Loại": "Tự chọn"}
            for mon, cnt in counts.items()]


def render_curriculum_tab(teachers: pd.DataFrame, timetable: pd.DataFrame) -> None:
    st.subheader("📚 Phân phối chương trình theo môn học")
    st.caption("Dữ liệu lưu trong SQLite (`data/timetable.db`). Chỉnh sửa rồi nhấn **Lưu**.")

    grade_tabs = st.tabs(["Khối 10", "Khối 11", "Khối 12"])

    for grade, gtab in zip(["10", "11", "12"], grade_tabs):
        with gtab:
            df_curr = database.get_curriculum(grade)
            n_class = (
                timetable[timetable["lop"].str.startswith(grade)]["lop"].nunique()
                if not timetable.empty else 0
            )

            col_form, col_info = st.columns([3, 1], gap="large")

            with col_form:
                edited = st.data_editor(
                    df_curr,
                    column_config={
                        "Môn dạy": st.column_config.TextColumn(
                            disabled=True, width="medium"),
                        "Tiết/tuần": st.column_config.NumberColumn(
                            min_value=0, max_value=10, step=1, width="small",
                            help="0 = môn này không dạy ở khối này",
                        ),
                        "Loại": st.column_config.SelectboxColumn(
                            options=["Bắt buộc", "Tự chọn"], width="small",
                        ),
                    },
                    hide_index=True,
                    use_container_width=True,
                    key=f"curr_editor_{grade}",
                )

            with col_info:
                total    = int(edited["Tiết/tuần"].sum())
                bat_buoc = int(edited[edited["Loại"] == "Bắt buộc"]["Tiết/tuần"].sum())
                tu_chon  = int(edited[edited["Loại"] == "Tự chọn"]["Tiết/tuần"].sum())

                st.metric("Tổng tiết/tuần", total)
                if total < 25:
                    st.warning(f"Thiếu **{25 - total}** tiết")
                elif total == 25:
                    st.success("Đúng 25 tiết ✓")
                else:
                    st.error(f"Vượt **{total - 25}** tiết")

                st.write(f"Bắt buộc: **{bat_buoc}**")
                st.write(f"Tự chọn: **{tu_chon}**")
                if n_class:
                    st.info(f"**{n_class} lớp** Khối {grade}")

            # Thêm môn mới
            with st.expander("➕ Thêm môn học mới"):
                ca, cb, cc, cd = st.columns([2, 1, 1, 1])
                new_mon  = ca.text_input("Tên môn:", key=f"new_mon_{grade}")
                new_tiet = cb.number_input("Tiết/tuần:", 0, 10, 1, key=f"new_tiet_{grade}")
                new_type = cc.selectbox("Loại:", ["Bắt buộc", "Tự chọn"], key=f"new_type_{grade}")
                cd.write("")
                cd.write("")
                if cd.button("Thêm", key=f"add_mon_{grade}") and new_mon.strip():
                    ok = database.add_subject(grade, new_mon, new_tiet, new_type)
                    if ok:
                        st.success(f"Đã thêm **{new_mon}** vào Khối {grade}.")
                        st.rerun()
                    else:
                        st.error("Môn này đã tồn tại trong khối.")

            st.divider()
            c1, c2, c3 = st.columns(3)
            with c1:
                if st.button(f"💾 Lưu Khối {grade}", type="primary",
                             key=f"save_curr_{grade}"):
                    database.upsert_curriculum(grade, edited)
                    st.success(f"Đã lưu {len(edited)} môn cho Khối {grade}.")
            with c2:
                if st.button(f"🔄 Về mặc định", key=f"reset_curr_{grade}"):
                    database.reset_curriculum(grade)
                    st.success("Đã reset về dữ liệu mặc định.")
                    st.rerun()
            with c3:
                if st.button(f"📊 Suy từ TKB cũ", key=f"infer_curr_{grade}"):
                    rows = _infer_curriculum(timetable, grade)
                    if rows:
                        database.upsert_curriculum(grade, pd.DataFrame(rows))
                        st.success(f"Đã cập nhật {len(rows)} môn từ TKB.")
                        st.rerun()
                    else:
                        st.warning("Không có dữ liệu TKB cho khối này.")

    # Xuất toàn bộ DB ra JSON
    st.divider()
    col_dl, col_ul = st.columns(2)
    with col_dl:
        export = database.get_all_curriculum()
        st.download_button(
            "⬇️ Xuất curriculum.json",
            data=json.dumps(export, ensure_ascii=False, indent=2),
            file_name="curriculum.json",
            mime="application/json",
        )
    with col_ul:
        uploaded = st.file_uploader("⬆️ Nhập curriculum.json", type="json",
                                    key="upload_curr")
        if uploaded:
            data: dict[str, list] = json.load(uploaded)
            for g, rows in data.items():
                df_imp = pd.DataFrame(rows).rename(columns={
                    "subject": "Môn dạy",
                    "periods_per_week": "Tiết/tuần",
                    "type": "Loại",
                })
                database.upsert_curriculum(g, df_imp)
            st.success(f"Đã nhập {sum(len(v) for v in data.values())} bản ghi.")
            st.rerun()


# =============================================================================
# Section 8: Nguyện vọng giáo viên — lưu vào SQLite qua database.py
# =============================================================================

def _default_avail_df(n_periods: int) -> pd.DataFrame:
    return pd.DataFrame(
        True,
        index=[f"Tiết {i}" for i in range(1, n_periods + 1)],
        columns=DAYS_SHORT,
    )

def _slots_to_avail_df(slots: list[dict], session: str, n_periods: int) -> pd.DataFrame:
    df = _default_avail_df(n_periods)
    for s in slots:
        if s.get("buoi") != session:
            continue
        if s["day"] not in DAYS_ORDER:
            continue
        col = DAYS_ORDER.index(s["day"])
        row = s["period"] - 1
        if 0 <= row < n_periods:
            df.iloc[row, col] = False
    return df

def _avail_df_to_slots(df: pd.DataFrame, session: str) -> list[dict]:
    return [
        {"day": DAYS_ORDER[ci], "period": ri + 1, "buoi": session}
        for ci in range(5)
        for ri in range(len(df))
        if not df.iloc[ri, ci]
    ]

def render_preferences_tab(teachers: pd.DataFrame, timetable: pd.DataFrame) -> None:
    st.subheader("🙋 Nguyện vọng & lịch rảnh giáo viên")
    st.caption("Bỏ tick ở tiết GV **bận**. Dữ liệu lưu vào SQLite (`data/timetable.db`).")

    with st.expander("🏫 Lịch sinh hoạt chuyên môn (SHCM) theo tổ"):
        st.markdown(
            "- **Sáng Thứ 3**: Toán, Lịch sử, Địa lí, GDQP&AN\n"
            "- **Sáng Thứ 4**: Vật lí, Công nghệ, Ngữ văn, Hóa học\n"
            "- **Sáng Thứ 5**: GD KT&PL\n"
            "- **Sáng Thứ 6**: Tiếng Anh, Sinh học\n\n"
            "Áp dụng cho **mọi tiết** GV dạy (kể cả HĐTN, GDĐP, chuyên đề). "
            "Bộ sinh TKB luôn tự chặn các buổi này; nút dưới ghi thêm vào lịch bận "
            "của từng GV để hiển thị ở đây và để kiểm tra xung đột TKB đã có."
        )
        if st.button("💾 Lưu lịch SHCM vào lịch bận tất cả GV", key="sync_shcm"):
            res = database.sync_dept_meeting_slots()
            st.success(
                f"Đã áp dụng cho {res['teachers']} GV — thêm mới {res['new_slots']} tiết bận."
            )

        st.markdown("**GV được miễn SHCM** (vẫn xếp tiết vào sáng ngày họp tổ):")
        gv_opts = {f"{r.ten} ({r.mon_day})": r.ma_so for r in teachers.itertuples()}
        exempt = database.get_shcm_exempt()
        sel_exempt = st.multiselect(
            "GV miễn SHCM:", sorted(gv_opts),
            default=sorted(k for k, v in gv_opts.items() if v in exempt),
            key="shcm_exempt_sel", label_visibility="collapsed",
        )
        if st.button("💾 Lưu danh sách miễn SHCM", key="save_shcm_exempt"):
            database.set_shcm_exempt([gv_opts[k] for k in sel_exempt])
            st.success(f"Đã lưu {len(sel_exempt)} GV miễn SHCM.")

    col_sel, col_stat = st.columns([2, 1])
    with col_sel:
        sel_gv = st.selectbox(
            "Chọn giáo viên:", sorted(teachers["ten"].unique()), key="pref_sel_gv"
        )

    gv_row  = teachers[teachers["ten"] == sel_gv].iloc[0]
    ma_so   = gv_row["ma_so"]
    saved   = database.get_teacher_pref(ma_so)

    with col_stat:
        st.metric("Tiết bận đã lưu", len(saved.get("unavailable_slots", [])))
        st.metric("Max tiết/tuần",   saved.get("max_periods_per_week", "—"))

    st.markdown(
        f"**{gv_row['ten']}** &nbsp;|&nbsp; Môn: **{gv_row['mon_day']}** "
        f"&nbsp;|&nbsp; Mã: `{ma_so}`"
    )
    st.divider()

    sang_tab, chieu_tab = st.tabs(["🌅 Buổi Sáng (Tiết 1–4)", "🌇 Buổi Chiều (Tiết 1–4)"])
    with sang_tab:
        st.caption("✅ = rảnh &nbsp; ☐ = bận")
        df_sang = _slots_to_avail_df(saved.get("unavailable_slots", []), "Sáng", 4)
        edited_sang = st.data_editor(
            df_sang,
            column_config={d: st.column_config.CheckboxColumn(d) for d in DAYS_SHORT},
            use_container_width=True, key=f"avail_sang_{ma_so}",
        )
    with chieu_tab:
        st.caption("✅ = rảnh &nbsp; ☐ = bận")
        df_chieu = _slots_to_avail_df(saved.get("unavailable_slots", []), "Chiều", 4)
        edited_chieu = st.data_editor(
            df_chieu,
            column_config={d: st.column_config.CheckboxColumn(d) for d in DAYS_SHORT},
            use_container_width=True, key=f"avail_chieu_{ma_so}",
        )

    st.divider()
    col_a, col_b = st.columns(2)
    with col_a:
        max_tiet = st.slider(
            "Số tiết tối đa/tuần:", 0, 30,
            value=saved.get("max_periods_per_week", 18), key=f"max_tiet_{ma_so}",
        )
        max_per_day = st.slider(
            "Số tiết tối đa/ngày:", 0, 9,
            value=saved.get("max_periods_per_day", 4), key=f"max_day_{ma_so}",
        )
    with col_b:
        all_classes = sorted(timetable["lop"].unique()) if not timetable.empty else []
        pref_classes = st.multiselect(
            "Lớp ưu tiên:", options=all_classes,
            default=[c for c in saved.get("preferred_classes", []) if c in all_classes],
            key=f"pref_cls_{ma_so}",
        )
        notes = st.text_area(
            "Ghi chú:", value=saved.get("notes", ""),
            placeholder="VD: Thứ 4 đón con, không dạy sau 16h...",
            key=f"notes_{ma_so}", height=90,
        )

    if st.button(f"💾 Lưu — {sel_gv}", type="primary", key=f"save_pref_{ma_so}"):
        unavail = (
            _avail_df_to_slots(edited_sang,  "Sáng") +
            _avail_df_to_slots(edited_chieu, "Chiều")
        )
        database.upsert_teacher_pref(
            ma_so, gv_row["ten"], gv_row["mon_day"],
            max_tiet, max_per_day, pref_classes, notes, unavail,
        )
        st.success(f"Đã lưu nguyện vọng **{sel_gv}** ({len(unavail)} tiết bận).")

    # ── Tổng quan ────────────────────────────────────────────
    st.divider()
    st.markdown("#### 📋 Tổng quan tất cả giáo viên đã lưu")
    all_prefs = database.get_all_teacher_prefs()
    if all_prefs:
        st.dataframe(
            pd.DataFrame(all_prefs).rename(columns={
                "ma_so": "Mã", "ten": "Họ tên", "mon_day": "Môn",
                "max_per_week": "Max tiết/tuần", "n_busy": "Tiết bận",
                "notes": "Ghi chú",
            }),
            use_container_width=True, hide_index=True,
        )
    else:
        st.info("Chưa có GV nào lưu nguyện vọng.")


# =============================================================================
# Section 9: Giao diện Streamlit
# =============================================================================

def _sort_pivot(pivot: pd.DataFrame, has_buoi: bool = False) -> pd.DataFrame:
    """Sắp xếp pivot theo thứ tự ngày → tiết (→ buổi nếu có)."""
    if pivot.empty:
        return pivot
    if has_buoi:
        # index = (thu, tiet, buoi)
        order = sorted(
            range(len(pivot)),
            key=lambda i: (DAY_KEY.get(pivot.index[i][0], 99), pivot.index[i][1],
                           0 if pivot.index[i][2] == "Sáng" else 1),
        )
        result = pivot.iloc[order]
        result.index = [f"{d} – T{p} ({b[0]})" for d, p, b in result.index]
    else:
        # index = (thu, tiet)
        order = sorted(
            range(len(pivot)),
            key=lambda i: (DAY_KEY.get(pivot.index[i][0], 99), pivot.index[i][1]),
        )
        result = pivot.iloc[order]
        result.index = [f"{d} – Tiết {p}" for d, p in result.index]
    return result


def render_ui() -> None:
    st.set_page_config(page_title="TKB Nguyễn An Ninh", layout="wide", initial_sidebar_state="expanded")
    database.init_db()   # tạo bảng + seed nếu DB chưa tồn tại
    st.title("📅 Thời Khoá Biểu — THPT Nguyễn An Ninh")

    # ── Khởi tạo session state — tải từ DB nếu chưa có ──────
    if "teachers" not in st.session_state:
        db_teachers = database.get_teachers()
        st.session_state["teachers"] = db_teachers if not db_teachers.empty \
            else pd.DataFrame(columns=["ma_so", "ten", "ten_tat", "mon_day"])

    if "classes" not in st.session_state:
        db_classes = database.get_classes()
        st.session_state["classes"] = db_classes if not db_classes.empty \
            else pd.DataFrame(columns=["lop", "buoi", "khoi"])

    # TKB sửa trực tiếp trong DB (ngoài phiên này) → nạp lại thay vì dùng bản cũ trong session
    tkb_version = database.timetable_version()
    if st.session_state.get("timetable_version") != tkb_version:
        st.session_state.pop("timetable", None)
        st.session_state["timetable_version"] = tkb_version

    if "timetable" not in st.session_state:
        db_tkb = database.get_timetable()
        st.session_state["timetable"] = db_tkb if not db_tkb.empty \
            else pd.DataFrame(columns=["id", "thu", "tiet", "buoi", "lop", "ten_gv", "mon"])

    if "csv_assignments" not in st.session_state:
        db_asgn = database.get_assignments()
        if not db_asgn.empty:
            st.session_state["csv_assignments"] = db_asgn

    if "csv_class_subjects" not in st.session_state:
        db_cs = database.get_class_subjects()
        if not db_cs.empty:
            st.session_state["csv_class_subjects"] = db_cs

    if "csv_homeroom" not in st.session_state:
        db_hr = database.get_homeroom()
        if not db_hr.empty:
            st.session_state["csv_homeroom"] = db_hr

    teachers  = st.session_state["teachers"]
    classes   = st.session_state["classes"]
    timetable = st.session_state["timetable"]
    all_issues = timetable_rules.find_school_errors(
        timetable, database.get_all_teacher_prefs(),
        scheduler.load_curriculum_map(), st.session_state.get("csv_class_subjects"),
    )

    # ── Metrics ───────────────────────────────────────────────
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Giáo viên (DS)", len(teachers))
    c2.metric("Lớp (DS)",       len(classes))
    c3.metric("Lớp có TKB",     timetable["lop"].nunique())
    c4.metric("Tổng tiết",      len(timetable))
    c5.metric("Lỗi/Cảnh báo",   len(all_issues), delta_color="inverse")
    st.divider()

    # ── Sidebar: upload TKB + upload GV ──────────────────────
    with st.sidebar:
        st.header("📂 Tải dữ liệu")

        st.markdown("**TKB (timetable.csv)**")
        st.download_button(
            "⬇️ Tải file mẫu TKB",
            data=csv_loader.get_sample_csv("timetable").encode("utf-8-sig"),
            file_name="timetable_sample.csv",
            mime="text/csv",
            key="sb_dl_tkb",
            use_container_width=True,
        )
        up_tkb = st.file_uploader(
            "Upload timetable.csv", type=["csv"], key="sb_tkb",
            help="Cột: thu, tiet, buoi, lop, ten_gv, mon",
            label_visibility="collapsed",
        )
        if up_tkb:
            df_tkb, errs = csv_loader.load_timetable_csv(up_tkb)
            if errs:
                st.error("TKB: " + "; ".join(errs))
            else:
                database.save_timetable(df_tkb)
                timetable = database.get_timetable()
                st.session_state["timetable"] = timetable
                st.success(f"TKB: {len(df_tkb)} tiết ✅ Đã lưu DB")

        st.markdown("**Giáo viên (teachers.csv)**")
        st.download_button(
            "⬇️ Tải file mẫu GV",
            data=csv_loader.get_sample_csv("teachers").encode("utf-8-sig"),
            file_name="teachers_sample.csv",
            mime="text/csv",
            key="sb_dl_gv",
            use_container_width=True,
        )
        up_gv = st.file_uploader(
            "Upload teachers.csv", type=["csv"], key="sb_gv",
            help="Cột: ma_so, ten, ten_tat, mon_day",
            label_visibility="collapsed",
        )
        if up_gv:
            df_gv, errs = csv_loader.load_teachers_csv(up_gv)
            if errs:
                st.error("GV: " + "; ".join(errs))
            else:
                st.session_state["teachers"] = df_gv
                teachers = df_gv
                database.save_teachers(df_gv)
                st.success(f"GV: {len(df_gv)} người ✅ Đã lưu DB")

        st.divider()
        st.subheader("🔍 Bộ lọc")
        avail_days  = [d for d in DAYS_ORDER if d in timetable["thu"].values]
        all_classes = sorted(timetable["lop"].unique())
        # Tách team-teaching "T1 / T2" thành tên GV riêng lẻ
        all_teachers = sorted(set(
            tn.strip()
            for gv in timetable["ten_gv"].unique()
            for tn in str(gv).split(" / ")
            if tn.strip()
        ) | set(database.get_group_periods()["gv"].dropna()))

        sel_class   = st.selectbox("Lớp:",        ["Tất cả"] + all_classes)

        # Lọc GV theo tên hoặc tên tắt
        search_gv = st.text_input("🔍 Tìm GV (tên / tên tắt):", key="search_gv")
        if search_gv.strip() and not teachers.empty:
            q = _nfc(search_gv.strip()).lower()
            matched = teachers[
                teachers["ten"].apply(_nfc).str.lower().str.contains(q, na=False)
                | teachers["ten_tat"].apply(_nfc).str.lower().str.contains(q, na=False)
            ]["ten"].tolist()
            matched_set = {_nfc(t) for t in matched}
            display_teachers = [t for t in all_teachers if _nfc(t) in matched_set]
        else:
            display_teachers = all_teachers

        sel_teacher = st.selectbox("Giáo viên:",  ["Tất cả"] + display_teachers)
        sel_day     = st.selectbox("Thứ:",        ["Tất cả"] + avail_days)
        sel_buoi    = st.selectbox("Buổi:",       ["Tất cả", "Sáng", "Chiều"])

    filtered = timetable.copy()
    if sel_class   != "Tất cả": filtered = filtered[filtered["lop"]    == sel_class]
    if sel_teacher != "Tất cả":
        filtered = filtered[filtered["ten_gv"].apply(
            lambda x: sel_teacher in [t.strip() for t in str(x).split(" / ")])]
    if sel_day     != "Tất cả": filtered = filtered[filtered["thu"]    == sel_day]
    if sel_buoi    != "Tất cả": filtered = filtered[filtered["buoi"]   == sel_buoi]

    # ── Tabs ──────────────────────────────────────────────────
    tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8, tab9, tab10 = st.tabs([
        "🗓️ TKB theo Lớp",
        "👨‍🏫 TKB theo GV",
        "📋 Danh sách tiết",
        "👥 Danh sách GV",
        "✏️ Xếp TKB thủ công",
        "🙋 Nguyện vọng GV",
        "📤 Xuất TKB",
        "🤖 Sinh TKB tự động",
        "📁 Tải dữ liệu CSV",
        "👨‍🎓 Học sinh",
    ])

    group_periods = database.get_group_periods()

    # ── Tab 1: Lịch theo lớp ──────────────────────────────────
    with tab1:
        if timetable.empty:
            st.info("Chưa có dữ liệu TKB. Tải file **timetable.csv** trong sidebar để bắt đầu.")
        elif sel_class != "Tất cả":
            st.subheader(f"Lịch lớp {sel_class}")
            rows_c, n_c = export_utils.build_class_table(timetable, sel_class, group_periods)
            st.markdown(export_utils.render_html_table(rows_c, f"Lớp {sel_class}", f"Số tiết: {n_c}",
                                                       ""), unsafe_allow_html=True)
            with st.expander("Bảng chi tiết GV từng tiết"):
                pivot = filtered.pivot_table(
                    index=["thu", "tiet", "buoi"], columns="lop",
                    values="ten_gv", aggfunc="first"
                )
                st.dataframe(_sort_pivot(pivot, has_buoi=True), use_container_width=True)
            _render_group_list(group_periods[group_periods["lop"] == sel_class],
                               f"🧩 Nhóm tự chọn có HS lớp {sel_class}")
        else:
            st.caption("Chọn một lớp trong sidebar để xem TKB chi tiết. Dưới đây là tổng thể (tối đa 15 lớp).")
            sample = sorted(timetable["lop"].unique())[:15]
            sub    = timetable[timetable["lop"].isin(sample)]
            pivot  = sub.pivot_table(
                index=["thu", "tiet", "buoi"], columns="lop",
                values="ten_gv", aggfunc="first"
            )
            st.dataframe(_sort_pivot(pivot, has_buoi=True), use_container_width=True)

    # ── Tab 2: Lịch theo GV ───────────────────────────────────
    with tab2:
        if timetable.empty:
            st.info("Chưa có dữ liệu TKB. Tải file **timetable.csv** trong sidebar để bắt đầu.")
        elif sel_teacher != "Tất cả":
            gv_row = teachers[teachers["ten"] == sel_teacher]
            if not gv_row.empty:
                info = gv_row.iloc[0]
                st.info(f"**{info['ten']}** | Môn: **{info['mon_day']}** | Mã: {info['ma_so']}")
            rows_t, subj_t, n_t = export_utils.build_teacher_table(timetable, sel_teacher, group_periods)
            st.metric("Số tiết dạy trong tuần (gồm tiết nhóm)", n_t)
            st.markdown(export_utils.render_html_table(rows_t, sel_teacher, f"({subj_t})   Số tiết dạy: {n_t}",
                                                       ""), unsafe_allow_html=True)
            if not filtered.empty:
                with st.expander("Bảng chi tiết môn từng tiết"):
                    pivot = filtered.pivot_table(
                        index=["thu", "tiet", "buoi"], columns="lop",
                        values="mon", aggfunc="first"
                    )
                    st.dataframe(_sort_pivot(pivot, has_buoi=True), use_container_width=True)
            _render_group_list(group_periods[group_periods["gv"] == sel_teacher],
                               f"🧩 Nhóm tự chọn {sel_teacher} dạy")
        else:
            # Bảng tóm tắt số tiết mỗi GV (loại bỏ hàng ten_gv rỗng)
            summary = (
                timetable[timetable["ten_gv"].str.strip() != ""].groupby("ten_gv")
                .agg(so_tiet=("lop", "count"), mon=("mon", "first"))
                .sort_values("so_tiet", ascending=False)
                .reset_index()
                .rename(columns={"ten_gv": "Giáo viên", "so_tiet": "Số tiết/tuần", "mon": "Môn"})
            )
            st.subheader("Số tiết dạy mỗi giáo viên")
            st.dataframe(summary, use_container_width=True, hide_index=True)

    # ── Tab 3: Danh sách tiết ─────────────────────────────────
    with tab3:
        if timetable.empty:
            st.info("Chưa có dữ liệu TKB. Tải file **timetable.csv** trong sidebar để bắt đầu.")
        else:
            display = (
                filtered[["thu", "tiet", "buoi", "lop", "ten_gv", "mon"]]
                .rename(columns={
                    "thu": "Thứ", "tiet": "Tiết", "buoi": "Buổi",
                    "lop": "Lớp", "ten_gv": "Giáo viên", "mon": "Môn",
                })
                .sort_values(
                    ["Thứ", "Tiết", "Lớp"],
                    key=lambda col: col.map(DAY_KEY) if col.name == "Thứ" else col,
                )
            )
            st.dataframe(display, use_container_width=True, hide_index=True, height=500)
            st.caption(f"Tổng: **{len(display)}** tiết")

    # ── Tab 4: Danh sách GV ───────────────────────────────────
    with tab4:
        if teachers.empty:
            st.info("Chưa có danh sách giáo viên. Tải file **teachers.csv** trong sidebar.")
        else:
            search_gv_tab = st.text_input(
                "🔍 Tìm kiếm (tên, tên tắt, mã số, môn dạy):",
                key="search_gv_tab4",
            )
            df_gv_show = teachers
            if search_gv_tab.strip():
                q = _nfc(search_gv_tab.strip()).lower()
                df_gv_show = teachers[
                    teachers["ten"].apply(_nfc).str.lower().str.contains(q, na=False)
                    | teachers["ten_tat"].apply(_nfc).str.lower().str.contains(q, na=False)
                    | teachers["ma_so"].apply(_nfc).str.lower().str.contains(q, na=False)
                    | teachers["mon_day"].apply(_nfc).str.lower().str.contains(q, na=False)
                ]
            st.caption(f"Hiển thị {len(df_gv_show)}/{len(teachers)} giáo viên")
            st.dataframe(
                df_gv_show.rename(columns={
                    "ma_so": "Mã", "ten": "Họ tên",
                    "ten_tat": "Tên tắt", "mon_day": "Môn dạy",
                }),
                use_container_width=True, hide_index=True,
            )

    # ── Tab 5: Xếp TKB thủ công (module thống nhất) ──────────────
    with tab5:
        manual_edit.render_manual_edit_tab(teachers, classes, timetable, all_issues)

    # ── Tab 6: Nguyện vọng GV ────────────────────────────────
    with tab6:
        if teachers.empty:
            st.info("Chưa có danh sách giáo viên. Tải file **teachers.csv** trong sidebar trước.")
        else:
            render_preferences_tab(teachers, timetable)

    # ── Tab 7: Xuất TKB ──────────────────────────────────────
    with tab7:
        st.subheader("📤 Xuất thời khoá biểu")
        if timetable.empty:
            st.info("Chưa có dữ liệu TKB. Tải file **timetable.csv** trong sidebar trước.")
        else:
            col_mode, col_date = st.columns([2, 1])
            with col_mode:
                mode = st.radio("Xuất theo:", ["Theo Giáo viên", "Theo Lớp"], horizontal=True)
            with col_date:
                apply_date = st.text_input("Ngày áp dụng:", value="05-09-2024",
                                           help="VD: 05-09-2024", key="export_date")

            st.divider()

            if mode == "Theo Giáo viên":
                # Tách team-teaching "T1 / T2" để GV chọn riêng lẻ
                all_gv = sorted(set(
                    _nfc(tn.strip())
                    for gv in timetable["ten_gv"].unique()
                    for tn in str(gv).split(" / ")
                    if tn.strip()
                ) | set(group_periods["gv"].dropna().map(_nfc)))

                # Label hiển thị: "Họ tên — TÊN TẮT (Môn dạy)"
                _gv_label: dict[str, str] = {}   # label → tên thật
                for _name in all_gv:
                    _row = teachers[teachers["ten"].apply(_nfc) == _name]
                    if not _row.empty:
                        _r = _row.iloc[0]
                        _gv_label[f"{_name} — {_r['ten_tat']} ({_r['mon_day']})"] = _name
                    else:
                        _gv_label[_name] = _name

                # Tìm kiếm nhanh
                _q_export = st.text_input(
                    "🔍 Tìm GV (tên / tên tắt / môn):", key="search_export_gv",
                )
                if _q_export.strip():
                    _q = _nfc(_q_export.strip()).lower()
                    _labels = [l for l in sorted(_gv_label) if _q in _nfc(l).lower()]
                else:
                    _labels = sorted(_gv_label)

                sel_label = st.selectbox(
                    "Chọn giáo viên:", _labels, key="export_sel_gv",
                ) if _labels else None
                sel = _gv_label.get(sel_label) if sel_label else None
                if not sel:
                    st.info("Không tìm thấy giáo viên phù hợp.")
                    st.stop()

                rows, subject, n_tiet = export_utils.build_teacher_table(timetable, sel, group_periods)
                title    = sel
                subtitle = f"Số tiết dạy: {n_tiet}"

                st.markdown(
                    export_utils.render_html_table(rows, title, subtitle, apply_date),
                    unsafe_allow_html=True,
                )

                st.divider()
                col_a, col_b = st.columns(2)
                with col_a:
                    xlsx_bytes = export_utils.export_to_excel(
                        rows, sel, f"Số tiết dạy: {n_tiet}",
                        sheet_name=sel[:31], apply_date=apply_date,
                    )
                    st.download_button(
                        f"⬇️ Tải Excel — {sel}",
                        data=xlsx_bytes,
                        file_name=f"TKB_{sel}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                with col_b:
                    with st.expander("⬇️ Xuất tất cả giáo viên (1 sheet, 2 cột)"):
                        if st.button("Tạo file Excel tất cả GV", key="export_all_gv"):
                            with st.spinner("Đang tạo file..."):
                                all_bytes = export_utils.export_teachers_one_sheet(
                                    timetable, all_gv, apply_date=apply_date, groups=group_periods,
                                )
                            st.download_button(
                                "⬇️ Tải xuống",
                                data=all_bytes,
                                file_name="TKB_TatCaGV.xlsx",
                                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                key="dl_all_gv",
                            )

            else:  # Theo Lớp
                all_cls = sorted(timetable["lop"].unique())
                sel     = st.selectbox("Chọn lớp:", all_cls, key="export_sel_cls")

                rows, n_tiet = export_utils.build_class_table(timetable, sel, group_periods)
                title    = f"Lớp {sel}"
                subtitle = f"Số tiết: {n_tiet}"

                st.markdown(
                    export_utils.render_html_table(rows, title, subtitle, apply_date),
                    unsafe_allow_html=True,
                )

                st.divider()
                col_a, col_b = st.columns(2)
                with col_a:
                    xlsx_bytes = export_utils.export_to_excel(
                        rows, title, subtitle,
                        sheet_name=sel[:31], apply_date=apply_date,
                    )
                    st.download_button(
                        f"⬇️ Tải Excel — Lớp {sel}",
                        data=xlsx_bytes,
                        file_name=f"TKB_Lop_{sel}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                with col_b:
                    with st.expander("⬇️ Xuất tất cả lớp theo khối (3 sheets)"):
                        if st.button("Tạo file Excel tất cả lớp", key="export_all_cls"):
                            with st.spinner("Đang tạo file..."):
                                all_bytes = export_utils.export_classes_by_grade_to_excel(
                                    timetable, apply_date=apply_date, groups=group_periods,
                                )
                            st.download_button(
                                "⬇️ Tải xuống",
                                data=all_bytes,
                                file_name="TKB_TatCaLop_TheoKhoi.xlsx",
                                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                key="dl_all_cls",
                            )


    # ── Tab 8: Sinh TKB tự động ──────────────────────────────────────────
    with tab8:
        _render_scheduler_tab(DEFAULT_XLS)

    # ── Tab 9: Tải dữ liệu CSV ──────────────────────────────────────────
    with tab9:
        _render_csv_tab()

    # ── Tab 10: Học sinh ────────────────────────────────────────────────
    with tab10:
        _render_students_tab()


def _render_group_list(gp: pd.DataFrame, title: str) -> None:
    """Bảng nhóm tự chọn (mã nhóm, lịch, GV, lớp/sĩ số) từ database.get_group_periods()."""
    if gp.empty:
        return
    rows = []
    for g, d in gp.groupby("ma_nhom"):
        ts = sorted(d["tiet"].unique()); buoi = d["buoi"].iloc[0]
        base = 4 if buoi == "Chiều" else 0
        rows.append({
            "Mã nhóm": g, "Lịch": f"{buoi} {d['thu'].iloc[0].lower()} tiết {base + ts[0]}-{base + ts[-1]}",
            "GV": d["gv"].iloc[0],
            "Lớp (số HS)": ", ".join(f"{l} ({n})" for l, n in
                                     d.drop_duplicates("lop")[["lop", "so_hs"]].itertuples(index=False)),
        })
    st.markdown(f"**{title}**")
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _student_group_summary(row: dict) -> str:
    parts = []
    for key, label in (("thqt_nhom", "THQT"), ("stem_nhom", "STEM"), ("avbn_nhom", "AVBN")):
        if str(row.get(key, "")).strip():
            parts.append(label)
    gdtc_mon = str(row.get("gdtc_mon", "")).strip()
    if gdtc_mon:
        parts.append(f"GDTC:{gdtc_mon}")
    return ", ".join(parts)


def _student_tn_summary(row: dict) -> str:
    mons = [str(row.get("on_tn_1_mon", "")).strip(), str(row.get("on_tn_2_mon", "")).strip()]
    mons = [m for m in mons if m]
    return ", ".join(f"TN {m}" for m in mons)


def _render_students_tab() -> None:
    st.subheader("👨‍🎓 Thông tin học sinh & nhóm môn tự chọn")
    df = database.get_students()
    if df.empty:
        st.info("Chưa có dữ liệu học sinh. Vui lòng nhập dữ liệu vào bảng `students`.")
        return

    mode = st.radio(
        "Chế độ xem", ["📋 Danh sách", "🗓️ TKB cá nhân"],
        key="students_mode", horizontal=True,
    )

    khoi_opts = ["Tất cả"] + sorted(df["khoi"].unique().tolist())
    lop_opts_all = sorted(df["lop"].unique().tolist())

    c1, c2, c3 = st.columns([1, 1, 2])
    with c1:
        sel_khoi = st.selectbox("Khối", khoi_opts, key="students_khoi")
    with c2:
        lop_opts = ["Tất cả"] + (
            lop_opts_all if sel_khoi == "Tất cả"
            else sorted(df.loc[df["khoi"] == sel_khoi, "lop"].unique().tolist())
        )
        sel_lop = st.selectbox("Lớp", lop_opts, key="students_lop")
    with c3:
        search = st.text_input("Tìm theo Mã HS / Họ tên", key="students_search")

    view = df.copy()
    if sel_khoi != "Tất cả":
        view = view[view["khoi"] == sel_khoi]
    if sel_lop != "Tất cả":
        view = view[view["lop"] == sel_lop]
    if search.strip():
        s = _nfc(search.strip()).lower()
        view = view[
            view["ma_hs"].apply(_nfc).str.lower().str.contains(s)
            | view["ten"].apply(_nfc).str.lower().str.contains(s)
        ]

    if mode == "📋 Danh sách":
        view = view.assign(
            **{"Nhóm môn chọn": view.apply(lambda r: _student_group_summary(r.to_dict()), axis=1)}
        )
        display_cols = ["ma_hs", "ten", "lop", "Nhóm môn chọn"]
        col_labels = {"ma_hs": "Mã HS", "ten": "Họ tên", "lop": "Lớp"}

        if sel_khoi == "12" or (sel_khoi == "Tất cả" and (view["khoi"] == "12").any()):
            view = view.assign(
                **{"2 môn thi tốt nghiệp": view.apply(lambda r: _student_tn_summary(r.to_dict()), axis=1)}
            )
            display_cols.append("2 môn thi tốt nghiệp")

        st.caption(f"Tổng số học sinh: {len(view)} / {len(df)}")
        st.dataframe(
            view[display_cols].rename(columns=col_labels),
            use_container_width=True,
            hide_index=True,
        )
    else:
        _render_student_personal_timetable(view)


_ELECTIVE_LABEL = {
    "GDTC": "GDTC", "THQT": "THQT", "AVBN": "AVBN", "STEM": "STEM",
    "ON_TN_1": "Ôn TN", "ON_TN_2": "Ôn TN",
}


def _tiet_global_to_buoi_period(tiet: int) -> tuple[str, int]:
    """tiết 1-8 cả ngày (quy ước file nguồn) -> (buổi, tiết 1-4/buổi, quy ước app)."""
    if tiet <= scheduler.N_SANG:
        return "Sáng", tiet
    return "Chiều", tiet - scheduler.N_SANG


def _render_student_personal_timetable(students_view: pd.DataFrame) -> None:
    if students_view.empty:
        st.info("Không có học sinh nào khớp bộ lọc.")
        return

    opts = {
        f"{r.ten} — {r.ma_hs} ({r.lop})": r.ma_hs
        for r in students_view.itertuples()
    }
    sel_label = st.selectbox("Chọn học sinh", sorted(opts.keys()), key="students_personal_pick")
    ma_hs = opts[sel_label]
    row = students_view[students_view["ma_hs"] == ma_hs].iloc[0]
    lop = row["lop"]

    st.caption(f"Mã HS **{ma_hs}** · Lớp **{lop}** · Nhóm môn chọn: {_student_group_summary(row.to_dict()) or '(chưa chọn)'}")
    if row["khoi"] == "12":
        tn = _student_tn_summary(row.to_dict())
        if tn:
            st.caption(f"2 môn thi tốt nghiệp: {tn}")

    core = database.get_timetable()
    core = core[core["lop"] == lop] if not core.empty else core
    electives = database.get_student_electives(ma_hs)

    grid: dict[tuple[str, str, int], str] = {}
    for r in core.itertuples():
        grid[(r.thu, r.buoi, r.tiet)] = f"{r.mon} · {r.ten_gv}"

    extra_rows = []
    for r in electives.itertuples():
        label = _ELECTIVE_LABEL.get(r.loai, r.loai)
        text = f"🔸{label}: {r.mon} · {r.gv}" + (f" · {r.phong}" if r.phong else "")
        if r.thu not in DAYS_ORDER:
            extra_rows.append({
                "Loại": label, "Thứ": r.thu, "Tiết": f"{r.tiet_dau}-{r.tiet_cuoi}",
                "Môn": r.mon, "GV": r.gv, "Phòng": r.phong,
            })
            continue
        for t in range(int(r.tiet_dau), int(r.tiet_cuoi) + 1):
            buoi, tiet = _tiet_global_to_buoi_period(t)
            key = (r.thu, buoi, tiet)
            if key in grid and grid[key] != text:
                grid[key] = f"{grid[key]}  ⚠️ TRÙNG: {text}"
            else:
                grid[key] = text

    if not grid and not extra_rows:
        st.info("Học sinh này chưa có dữ liệu TKB (lớp chưa được sinh TKB và chưa đăng ký nhóm tự chọn nào).")
        return

    long_df = pd.DataFrame(
        [{"thu": k[0], "buoi": k[1], "tiet": k[2], "content": v} for k, v in grid.items()]
    )
    for buoi_name, n_periods in (("Sáng", scheduler.N_SANG), ("Chiều", scheduler.N_CHIEU)):
        sub = long_df[long_df["buoi"] == buoi_name]
        st.markdown(f"**Buổi {buoi_name}**")
        if sub.empty:
            st.caption("(không có tiết nào)")
            continue
        pivot = sub.pivot_table(index="tiet", columns="thu", values="content", aggfunc="first")
        pivot = pivot.reindex(index=range(1, n_periods + 1), columns=DAYS_ORDER)
        pivot.index = [f"Tiết {p}" for p in pivot.index]
        st.dataframe(pivot.fillna(""), use_container_width=True)

    if extra_rows:
        st.markdown("**Lịch ngoài Thứ 2–6 (vd. Thứ 7)**")
        st.dataframe(pd.DataFrame(extra_rows), hide_index=True, use_container_width=True)


def _render_csv_tab() -> None:
    # ── Cấu hình từng file ────────────────────────────────────────────────
    # save_db: hàm database.save_*() tương ứng (None = không tự động lưu riêng)
    FILE_CONFIGS = [
        {
            "key":        "teachers",
            "ss_key":     "teachers",
            "icon":       "👨‍🏫",
            "title":      "Danh sách giáo viên",
            "filename":   "teachers.csv",
            "desc":       "Thông tin giáo viên: mã số, họ tên, tên viết tắt, môn dạy.",
            "loader":     csv_loader.load_teachers_csv,
            "save_db":    database.save_teachers,
            "import_db":  None,
            "count_label": "giáo viên",
        },
        {
            "key":        "classes",
            "ss_key":     "classes",
            "icon":       "🏫",
            "title":      "Danh sách lớp",
            "filename":   "classes.csv",
            "desc":       "Danh sách lớp học: tên lớp, buổi học (S/C), khối.",
            "loader":     csv_loader.load_classes_csv,
            "save_db":    database.save_classes,
            "import_db":  None,
            "count_label": "lớp",
        },
        {
            "key":        "subjects",
            "ss_key":     "csv_subjects",
            "icon":       "📚",
            "title":      "Phân phối chương trình",
            "filename":   "subjects.csv",
            "desc":       "Số tiết mỗi môn theo khối (10, 11, 12).",
            "loader":     csv_loader.load_subjects_csv,
            "save_db":    None,
            "import_db":  csv_loader.import_subjects_to_db,
            "count_label": "môn học",
        },
        {
            "key":        "class_subjects",
            "ss_key":     "csv_class_subjects",
            "icon":       "📋",
            "title":      "Số tiết theo lớp",
            "filename":   "class_subjects.csv",
            "desc":       "Số tiết từng môn cho từng lớp cụ thể.",
            "loader":     csv_loader.load_class_subjects_csv,
            "save_db":    database.save_class_subjects,
            "import_db":  None,
            "count_label": "bản ghi",
        },
        {
            "key":        "assignments",
            "ss_key":     "csv_assignments",
            "icon":       "📝",
            "title":      "Phân công giảng dạy",
            "filename":   "assignments.csv",
            "desc":       "Phân công GV dạy môn cho từng lớp (dùng để sinh TKB tự động).",
            "loader":     csv_loader.load_assignments_csv,
            "save_db":    database.save_assignments,
            "import_db":  None,
            "count_label": "phân công",
        },
        {
            "key":        "homeroom",
            "ss_key":     "csv_homeroom",
            "icon":       "🏠",
            "title":      "GV chủ nhiệm",
            "filename":   "homeroom.csv",
            "desc":       "Phân công giáo viên chủ nhiệm cho từng lớp (1 lớp — 1 GVCN).",
            "loader":     csv_loader.load_homeroom_csv,
            "save_db":    database.save_homeroom,
            "import_db":  None,
            "count_label": "lớp",
        },
    ]

    st.subheader("📁 Tải dữ liệu")

    # ── Bảng trạng thái ───────────────────────────────────────────────────
    st.markdown("#### Trạng thái")
    status_cols = st.columns(len(FILE_CONFIGS))
    for col, cfg in zip(status_cols, FILE_CONFIGS):
        loaded = cfg["ss_key"] in st.session_state and not st.session_state[cfg["ss_key"]].empty
        if loaded:
            n = len(st.session_state[cfg["ss_key"]])
            col.success(f"{cfg['icon']} **{cfg['title']}**\n\n{n} {cfg['count_label']}")
        else:
            col.warning(f"{cfg['icon']} **{cfg['title']}**\n\nChưa tải")

    st.divider()

    # ── Tải tất cả file mẫu ──────────────────────────────────────────────
    st.markdown("#### Tải file mẫu")
    st.caption("Tải về để xem định dạng chuẩn, sau đó điền dữ liệu thực và upload lại.")
    sample_cols = st.columns(len(FILE_CONFIGS))
    for col, cfg in zip(sample_cols, FILE_CONFIGS):
        sample = csv_loader.get_sample_csv(cfg["key"])
        col.download_button(
            f"⬇️ {cfg['filename']}",
            data=sample.encode("utf-8-sig"),
            file_name=cfg["filename"],
            mime="text/csv",
            key=f"dl_top_{cfg['key']}",
            use_container_width=True,
        )

    st.divider()

    # ── Card từng file ────────────────────────────────────────────────────
    st.markdown("#### Upload dữ liệu")

    for cfg in FILE_CONFIGS:
        loaded    = cfg["ss_key"] in st.session_state and not st.session_state[cfg["ss_key"]].empty
        badge     = "✅ Đã tải" if loaded else "⬜ Chưa tải"
        cols_req  = csv_loader.REQUIRED_COLUMNS.get(cfg["key"], [])

        with st.expander(
            f"{cfg['icon']} **{cfg['title']}** — `{cfg['filename']}` &nbsp; {badge}",
            expanded=not loaded,
        ):
            left, right = st.columns([1, 1], gap="large")

            with left:
                st.markdown(f"**Mô tả:** {cfg['desc']}")
                st.markdown(
                    "**Cột bắt buộc:** " +
                    " · ".join(f"`{c}`" for c in cols_req)
                )

                # Mô tả chi tiết từng cột
                descs = csv_loader.COLUMN_DESCRIPTIONS.get(cfg["key"], {})
                if descs:
                    rows_desc = [{"Cột": c, "Ý nghĩa": descs.get(c, "")} for c in cols_req]
                    st.dataframe(
                        pd.DataFrame(rows_desc),
                        hide_index=True,
                        use_container_width=True,
                        height=min(35 + len(cols_req) * 35, 250),
                    )

                st.download_button(
                    f"⬇️ Tải file mẫu",
                    data=csv_loader.get_sample_csv(cfg["key"]).encode("utf-8-sig"),
                    file_name=cfg["filename"],
                    mime="text/csv",
                    key=f"dl_card_{cfg['key']}",
                )

            with right:
                uploaded = st.file_uploader(
                    f"Chọn file {cfg['filename']}",
                    type=["csv"],
                    key=f"up_{cfg['key']}",
                    label_visibility="collapsed",
                )

                if uploaded:
                    df, messages = cfg["loader"](uploaded)

                    if df is None:
                        for msg in messages:
                            st.error(f"❌ {msg}")
                    else:
                        if messages:
                            st.warning(
                                "**Phát hiện vấn đề — vẫn có thể tải nhưng cần kiểm tra:**\n\n"
                                + "\n\n".join(messages)
                            )
                        st.session_state[cfg["ss_key"]] = df
                        db_msg = ""
                        if cfg["save_db"]:
                            try:
                                cfg["save_db"](df)
                                db_msg = " · 💾 Đã lưu vào database"
                            except Exception as exc:
                                st.warning(f"⚠️ Không lưu được DB: {exc}")
                        if cfg["import_db"]:
                            try:
                                cfg["import_db"](df)
                                db_msg += " · 📥 Đã nhập curriculum"
                            except Exception as exc:
                                st.warning(f"⚠️ Import curriculum: {exc}")
                        st.success(
                            f"✅ **{len(df)}** {cfg['count_label']}{db_msg}"
                        )

                elif not loaded:
                    st.info("Kéo thả hoặc nhấn để chọn file CSV.")

            # ── Bảng chỉnh sửa trực tiếp (full-width, dưới 2 cột) ──────
            cur_loaded = (
                cfg["ss_key"] in st.session_state
                and not st.session_state[cfg["ss_key"]].empty
            )
            if cur_loaded:
                df_cur = st.session_state[cfg["ss_key"]]
                st.divider()
                st.caption(
                    f"✏️ **Chỉnh sửa trực tiếp** — {len(df_cur)} {cfg['count_label']} "
                    "· Thêm / sửa / xoá hàng rồi nhấn 💾 **Lưu thay đổi**"
                )

                edited_df = st.data_editor(
                    df_cur,
                    num_rows="dynamic",
                    use_container_width=True,
                    hide_index=True,
                    key=f"edit_{cfg['key']}",
                )

                c_save, c_del, _ = st.columns([1, 1, 3])
                with c_save:
                    if st.button(
                        "💾 Lưu thay đổi",
                        key=f"save_{cfg['key']}",
                        type="primary",
                        use_container_width=True,
                    ):
                        clean = edited_df.dropna(how="all").reset_index(drop=True)
                        st.session_state[cfg["ss_key"]] = clean
                        ok = True
                        if cfg["save_db"]:
                            try:
                                cfg["save_db"](clean)
                            except Exception as exc:
                                st.error(f"❌ Lỗi lưu DB: {exc}")
                                ok = False
                        if cfg.get("import_db"):
                            try:
                                cfg["import_db"](clean)
                            except Exception as exc:
                                st.error(f"❌ Lỗi import: {exc}")
                                ok = False
                        if ok:
                            st.toast(
                                f"💾 Đã lưu {len(clean)} {cfg['count_label']} vào database"
                            )
                        st.rerun()
                with c_del:
                    if st.button(
                        "🗑️ Xoá tất cả",
                        key=f"clear_{cfg['key']}",
                        use_container_width=True,
                    ):
                        del st.session_state[cfg["ss_key"]]
                        st.rerun()

    # ── Phần xem class_subjects ───────────────────────────────────────────
    st.divider()
    _render_class_subjects_view()

    # ── Phần xem homeroom ─────────────────────────────────────────────────
    st.divider()
    _render_homeroom_view()

    # ── Reset toàn bộ dữ liệu CSV ─────────────────────────────────────────
    st.divider()
    with st.expander("⚠️ Reset dữ liệu"):
        st.warning(
            "Xoá **tất cả** dữ liệu CSV đã lưu (GV, lớp, phân công, TKB...) "
            "khỏi database và session. Không ảnh hưởng đến curriculum và nguyện vọng GV."
        )
        if st.button("🗑️ Xoá tất cả dữ liệu CSV", key="reset_all_csv", type="secondary"):
            database.clear_all_csv_data()
            for k in ["teachers", "classes", "csv_assignments", "csv_class_subjects",
                      "csv_homeroom", "csv_subjects", "timetable", "gen_timetable"]:
                st.session_state.pop(k, None)
            st.success("Đã xoá toàn bộ dữ liệu CSV. Trang sẽ reload.")
            st.rerun()


def _render_class_subjects_view() -> None:
    """Hiển thị bảng môn học của từng lớp từ dữ liệu class_subjects.csv đã tải."""
    st.markdown("### 📋 Môn học theo từng lớp")

    df = st.session_state.get("csv_class_subjects")
    classes_df = st.session_state.get("classes")

    if df is None or df.empty:
        st.info(
            "Chưa có dữ liệu. Tải lên **class_subjects.csv** ở mục trên để xem "
            "từng lớp học những môn gì và số tiết mỗi tuần."
        )
        return

    # Bổ sung cột khối nếu có classes.csv, nếu không tự suy từ tên lớp
    if classes_df is not None and not classes_df.empty:
        khoi_map = dict(zip(classes_df["lop"], classes_df["khoi"]))
        df = df.copy()
        df["khoi"] = df["lop"].map(khoi_map).fillna(df["lop"].str[:2])
    else:
        df = df.copy()
        df["khoi"] = df["lop"].str[:2]

    all_grades = sorted(df["khoi"].unique())

    # ── Tổng quan ─────────────────────────────────────────────────────────
    c1, c2, c3 = st.columns(3)
    c1.metric("Tổng số lớp", df["lop"].nunique())
    c2.metric("Tổng số môn", df["subject"].nunique())
    c3.metric("Tổng lượt phân công", len(df))

    st.divider()

    # ── Chọn khối để xem ──────────────────────────────────────────────────
    grade_tabs = st.tabs([f"Khối {g}" for g in all_grades] + ["Tất cả lớp"])

    for i, grade in enumerate(all_grades):
        with grade_tabs[i]:
            sub = df[df["khoi"] == grade].copy()
            classes_in_grade = sorted(sub["lop"].unique())
            subjects_in_grade = sorted(sub["subject"].unique())

            st.caption(
                f"**{len(classes_in_grade)} lớp** · **{len(subjects_in_grade)} môn** "
                f"· {len(sub)} lượt phân công"
            )

            # Ma trận lớp × môn (pivot)
            pivot = sub.pivot_table(
                index="lop", columns="subject",
                values="periods_per_week", aggfunc="sum", fill_value=0,
            )
            # Thêm cột tổng
            pivot["Tổng tiết"] = pivot.sum(axis=1)
            pivot = pivot.sort_index()

            st.dataframe(
                pivot.style.map(
                    lambda v: "background-color: #f0f0f0; color: #aaa;" if v == 0 else "",
                    subset=[c for c in pivot.columns if c != "Tổng tiết"],
                ).map(
                    lambda v: "font-weight: bold; color: #1a6f3c;" if v > 0 else "",
                    subset=["Tổng tiết"],
                ),
                use_container_width=True,
                height=min(60 + len(classes_in_grade) * 35, 550),
            )

            st.caption("Số trong ô = tiết/tuần · ô xám = môn không học")

            # Chi tiết từng lớp
            st.markdown("**Xem chi tiết từng lớp:**")
            sel_lop = st.selectbox(
                "Chọn lớp:", classes_in_grade,
                key=f"cs_sel_{grade}",
                label_visibility="collapsed",
            )
            lop_data = sub[sub["lop"] == sel_lop][["subject", "periods_per_week"]].sort_values("subject")
            lop_data.columns = ["Môn học", "Tiết/tuần"]
            total_tiet = int(lop_data["Tiết/tuần"].sum())

            col_tbl, col_stat = st.columns([2, 1])
            with col_tbl:
                st.dataframe(lop_data, hide_index=True, use_container_width=True)
            with col_stat:
                st.metric(f"Tổng tiết — {sel_lop}", total_tiet)
                if total_tiet < 25:
                    st.warning(f"Thiếu {25 - total_tiet} tiết")
                elif total_tiet == 25:
                    st.success("Đúng 25 tiết ✓")
                else:
                    st.error(f"Vượt {total_tiet - 25} tiết")

    # Tab "Tất cả lớp"
    with grade_tabs[-1]:
        pivot_all = df.pivot_table(
            index="lop", columns="subject",
            values="periods_per_week", aggfunc="sum", fill_value=0,
        )
        pivot_all["Tổng tiết"] = pivot_all.sum(axis=1)
        pivot_all = pivot_all.sort_index()
        st.dataframe(pivot_all, use_container_width=True, height=500)
        csv_bytes = pivot_all.reset_index().to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "⬇️ Xuất bảng này ra CSV",
            data=csv_bytes,
            file_name="class_subjects_matrix.csv",
            mime="text/csv",
        )


def _render_homeroom_view() -> None:
    """Hiển thị danh sách GVCN từ homeroom.csv đã tải."""
    st.markdown("### 🏠 Giáo viên chủ nhiệm")

    df = st.session_state.get("csv_homeroom")
    classes_df = st.session_state.get("classes")

    if df is None or df.empty:
        st.info(
            "Chưa có dữ liệu. Tải lên **homeroom.csv** ở mục trên để xem "
            "danh sách giáo viên chủ nhiệm theo từng lớp."
        )
        return

    # Bổ sung cột khối
    if classes_df is not None and not classes_df.empty:
        khoi_map = dict(zip(classes_df["lop"], classes_df["khoi"]))
        df = df.copy()
        df["khoi"] = df["lop"].map(khoi_map).fillna(df["lop"].str[:2])
    else:
        df = df.copy()
        df["khoi"] = df["lop"].str[:2]

    all_grades = sorted(df["khoi"].unique())

    # ── Tổng quan ─────────────────────────────────────────────────────────
    c1, c2, c3 = st.columns(3)
    c1.metric("Tổng số lớp có GVCN", df["lop"].nunique())
    c2.metric("Số GVCN khác nhau", df["ten_gv"].nunique())

    # Phát hiện GV kiêm nhiệm (chủ nhiệm nhiều lớp)
    multi = df.groupby("ten_gv")["lop"].count()
    n_multi = int((multi > 1).sum())
    c3.metric("GV kiêm nhiệm ≥2 lớp", n_multi,
              delta="Cần kiểm tra" if n_multi > 0 else None,
              delta_color="inverse" if n_multi > 0 else "off")

    if n_multi > 0:
        st.warning(
            "Các GV chủ nhiệm nhiều lớp: " +
            ", ".join(f"**{gv}** ({cnt} lớp)"
                      for gv, cnt in multi[multi > 1].items())
        )

    st.divider()

    # ── Tabs theo khối ────────────────────────────────────────────────────
    grade_tabs = st.tabs([f"Khối {g}" for g in all_grades] + ["Tất cả"])

    for i, grade in enumerate(all_grades):
        with grade_tabs[i]:
            sub = df[df["khoi"] == grade][["lop", "ma_so", "ten_gv"]].sort_values("lop")
            sub.columns = ["Lớp", "Mã GV", "Họ tên GVCN"]

            # Kiểm tra lớp chưa có GVCN (so với classes.csv nếu có)
            if classes_df is not None and not classes_df.empty:
                grade_classes = set(
                    classes_df[classes_df["khoi"] == grade]["lop"]
                )
                assigned = set(df[df["khoi"] == grade]["lop"])
                missing  = sorted(grade_classes - assigned)
                if missing:
                    st.error(
                        f"Chưa có GVCN: **{', '.join(missing)}**"
                    )
                else:
                    st.success(f"✅ Tất cả {len(grade_classes)} lớp Khối {grade} đã có GVCN.")

            st.dataframe(sub, hide_index=True, use_container_width=True)

            # Xuất danh sách khối
            csv_bytes = sub.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                f"⬇️ Xuất danh sách GVCN Khối {grade}",
                data=csv_bytes,
                file_name=f"GVCN_khoi{grade}.csv",
                mime="text/csv",
                key=f"dl_hr_{grade}",
            )

    # Tab "Tất cả"
    with grade_tabs[-1]:
        all_df = df[["lop", "khoi", "ma_so", "ten_gv"]].sort_values(["khoi", "lop"])
        all_df.columns = ["Lớp", "Khối", "Mã GV", "Họ tên GVCN"]
        st.dataframe(all_df, hide_index=True, use_container_width=True)

        csv_all = all_df.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "⬇️ Xuất toàn trường",
            data=csv_all,
            file_name="GVCN_toan_truong.csv",
            mime="text/csv",
            key="dl_hr_all",
        )


def _render_scheduler_tab(_xls_path: str = "") -> None:
    st.subheader("🤖 Sinh thời khoá biểu tự động")

    # ── Kiểm tra dữ liệu đầu vào ─────────────────────────────────────────
    asgn_df    = st.session_state.get("csv_assignments")
    teachers   = st.session_state.get("teachers")
    cls_sub_df = st.session_state.get("csv_class_subjects")

    has_asgn     = asgn_df    is not None and not asgn_df.empty
    has_teachers = teachers   is not None and not teachers.empty
    has_cls_sub  = cls_sub_df is not None and not cls_sub_df.empty
    ready        = has_asgn and has_teachers

    # ── Trạng thái nguồn dữ liệu ─────────────────────────────────────────
    c1, c2, c3 = st.columns(3)
    if has_asgn:
        c1.success(f"✅ assignments.csv — {len(asgn_df)} phân công")
    else:
        c1.error("❌ Chưa có assignments.csv")
    if has_teachers:
        c2.success(f"✅ teachers.csv — {len(teachers)} GV")
    else:
        c2.error("❌ Chưa có teachers.csv")
    if has_cls_sub:
        c3.success(f"✅ class_subjects.csv — {len(cls_sub_df)} bản ghi")
    else:
        c3.warning("⚠️ class_subjects.csv — dùng curriculum DB")

    if not ready:
        st.info(
            "Vui lòng tải lên **assignments.csv** và **teachers.csv** "
            "ở tab **📁 Tải dữ liệu CSV** trước."
        )
        return

    st.divider()

    # ── Xem trước phân công ───────────────────────────────────────────────
    with st.expander("👁️ Xem trước dữ liệu đầu vào"):
        tab_asgn, tab_cls = st.tabs(["Phân công GV", "Số tiết theo lớp"])
        with tab_asgn:
            summary = (
                asgn_df.assign(grade=asgn_df["lop"].str[:2])
                .groupby(["grade", "subject"])["lop"].count()
                .rename("Số lớp").reset_index()
                .rename(columns={"grade": "Khối", "subject": "Môn"})
            )
            st.dataframe(summary, hide_index=True, use_container_width=True)
        with tab_cls:
            if has_cls_sub:
                st.dataframe(cls_sub_df.head(30), hide_index=True, use_container_width=True)
            else:
                curr = database.get_all_curriculum()
                rows = [
                    {"Khối": g, "Môn": s["subject"], "Tiết/tuần": s["periods_per_week"]}
                    for g, ss in curr.items() for s in ss
                ]
                st.caption("Dùng curriculum từ database (chưa có class_subjects.csv):")
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    # ── Cấu hình solver ───────────────────────────────────────────────────
    st.markdown("#### ⚙️ Cấu hình")
    col_a, col_b, col_c, col_d = st.columns(4)
    with col_a:
        all_grades = sorted(asgn_df["lop"].str[:2].unique())
        saved_tkb = database.get_timetable()
        saved_grades = set(saved_tkb["lop"].astype(str).str[:2]) if not saved_tkb.empty else set()
        grade_choices = st.multiselect(
            "Khối cần xếp:", all_grades,
            default=[g for g in all_grades if not (g == "12" and "12" in saved_grades)],
            key="sched_grades",
            help="Khối KHÔNG chọn sẽ được giữ nguyên theo TKB đã lưu; "
                 "giờ dạy của GV ở các khối đó được coi là giờ bận cố định.",
        )
        kept_grades = sorted(saved_grades - set(grade_choices)) if grade_choices else []
        if kept_grades:
            st.caption("🔐 Giữ nguyên TKB đã lưu: " + ", ".join(f"Khối {g}" for g in kept_grades))
    with col_b:
        use_cpsat = st.toggle(
            "CP-SAT cải tiến", value=False, key="sched_cpsat",
            help="Greedy đã đủ nhanh (<1s). Bật nếu muốn tối ưu thêm "
                 "(thực nghiệm: thường không cải thiện hơn Tabu Search).",
        )
    with col_c:
        use_tabu = st.toggle(
            "Tabu Search (dồn gọn thêm)", value=True, key="sched_tabu",
            help="Chạy sau Greedy để dồn gọn tiết GV/lớp hơn (không đụng "
                 "tiết đôi). Tốn thêm thời gian — khoảng vài chục giây đến "
                 "vài phút tuỳ số lớp chọn.",
        )
    with col_d:
        time_limit = st.slider("Thời gian tối đa (giây):", 10, 300, 30, step=10, key="sched_time")

    tabu_iterations = 0
    if use_tabu:
        tabu_iterations = st.slider(
            "Số vòng Tabu Search:", 20, 400, 100, step=20, key="sched_tabu_iters",
            help="Nhiều vòng hơn = dồn gọn tốt hơn nhưng chạy lâu hơn.",
        )
    kempe_time = st.slider(
        "Thời gian lấp sáng & dồn gọn GV (giây):", 0, 300, 60, step=15, key="sched_kempe",
        help="Hoán đổi chuỗi Kempe: kéo tiết chiều lên ô sáng trống, giảm tiết trống "
             "và số buổi lên trường của GV. Càng lâu càng tốt; 0 = tắt.",
    )

    st.caption("💡 Greedy GRASP xếp toàn trường trong < 1 giây. "
               "Tabu Search chạy thêm để dồn gọn — có thể tắt nếu muốn xem kết quả ngay.")

    # ── Nút chạy ──────────────────────────────────────────────────────────
    run_btn = st.button("▶ Sinh TKB tự động", type="primary", key="sched_run")

    # Auto-run khi vừa upload đủ dữ liệu lần đầu.
    # LƯU Ý: trước đây thiếu dấu ngoặc nên `and`/`or` tính sai thứ tự — biểu
    # thức thực chất là (ready and "..." not in state) or (hash khác), nghĩa
    # là khi "ready" là False, vế OR vẫn có thể tự thành True chỉ vì hash
    # không khớp (vd session mới, hash rỗng khác hash cũ) — khiến tự động
    # sinh TKB toàn trường chạy ngầm mà không ai bấm nút, ghi đè dữ liệu
    # đang có. Thêm ngoặc để "ready" luôn là điều kiện bắt buộc trước.
    just_ready = ready and (
        "sched_last_run_hash" not in st.session_state
        or st.session_state.get("sched_last_run_hash") != _data_hash(asgn_df, teachers)
    )

    if run_btn or (just_ready and st.session_state.get("sched_auto", False)):
        st.session_state["sched_last_run_hash"] = _data_hash(asgn_df, teachers)
        _run_scheduler(asgn_df, teachers, cls_sub_df, grade_choices, time_limit, use_cpsat,
                       database.get_homeroom(), use_tabu=use_tabu, tabu_iterations=tabu_iterations,
                       kempe_time=kempe_time)

    # Checkbox auto-run
    st.checkbox(
        "Tự động sinh lại TKB khi dữ liệu thay đổi",
        key="sched_auto",
        help="Khi bật, TKB sẽ được sinh lại mỗi khi bạn upload file mới.",
    )

    # ── Trạng thái CP-SAT nền ─────────────────────────────────────────────
    _render_bg_cpsat_status()

    # ── Hiển thị TKB đã sinh ─────────────────────────────────────────────
    if "gen_timetable" in st.session_state:
        _render_gen_timetable()


def _data_hash(asgn_df: pd.DataFrame, teachers_df: pd.DataFrame) -> str:
    return f"{len(asgn_df)}-{len(teachers_df)}-{list(asgn_df.columns)}"


def _run_scheduler(asgn_df, teachers, cls_sub_df, grade_choices, time_limit, use_cpsat,
                   homeroom_df=None, use_tabu=True, tabu_iterations=100, kempe_time=60):
    # Huỷ task CP-SAT cũ nếu có
    old_id = st.session_state.get("bg_task_id")
    if old_id:
        _BG_TASKS.pop(old_id, None)
        st.session_state.pop("bg_task_id", None)

    log_box   = st.empty()
    log_lines: list[str] = []

    def _cb(msg: str):
        log_lines.append(msg)
        log_box.markdown("\n".join(f"- {l}" for l in log_lines[-15:]))

    locked_df = None
    if grade_choices:
        saved = database.get_timetable()
        if not saved.empty:
            locked_df = saved[~saved["lop"].astype(str).str[:2].isin(grade_choices)]

    spinner_msg = "🏃 Greedy GRASP đang xếp TKB..."
    if use_tabu:
        spinner_msg += " (sẽ chạy thêm Tabu Search để dồn gọn — có thể mất vài phút)"
    with st.spinner(spinner_msg):
        try:
            gen_df, greedy_msg, bg_state = scheduler.generate_timetable_from_dfs(
                assignments_df=asgn_df,
                teachers_df=teachers,
                class_subjects_df=cls_sub_df if (cls_sub_df is not None
                                                  and not cls_sub_df.empty) else None,
                time_limit_s=float(time_limit),
                progress_cb=_cb,
                grades=grade_choices or None,
                use_cpsat=use_cpsat,
                homeroom_df=homeroom_df,
                use_tabu=use_tabu,
                tabu_iterations=tabu_iterations,
                kempe_time_s=float(kempe_time),
                locked_timetable=locked_df,
            )
        except Exception as exc:
            st.error(f"❌ Lỗi: {exc}")
            return

    if gen_df is None or gen_df.empty:
        st.error(f"Solver không tìm được nghiệm: {greedy_msg}")
        return

    st.success(greedy_msg)
    st.session_state["gen_timetable"] = gen_df
    try:
        database.save_timetable(gen_df)
        st.session_state["timetable"] = database.get_timetable()
    except Exception:
        pass

    mc1, mc2, mc3 = st.columns(3)
    mc1.metric("Tổng tiết", len(gen_df))
    mc2.metric("Số lớp",    gen_df["lop"].nunique())
    mc3.metric("Số GV", len(set(
        tn.strip()
        for gv in gen_df["ten_gv"].unique()
        for tn in str(gv).split(" / ")
        if tn.strip()
    )))

    conflicts = check_conflicts(gen_df)
    if conflicts.empty:
        st.success("✅ Không có xung đột GV!")
    else:
        st.error(f"⚠️ {len(conflicts)} xung đột — xem tab Xung đột")

    # Khởi động CP-SAT tối ưu nền (nếu cần)
    if bg_state is not None:
        task_id = f"cpsat_{int(_time.time() * 1000)}"
        st.session_state["bg_task_id"] = task_id
        _start_bg_cpsat(task_id, bg_state, float(time_limit))

    # QUAN TRỌNG: bắt buộc rerun ngay — biến `timetable` dùng ở MỌI tab khác
    # (kể cả tab "Xuất TKB") được gán 1 LẦN ở đầu render_ui() TRƯỚC KHI tab
    # này kịp cập nhật session_state phía trên. Không rerun thì các tab
    # khác vẫn hiển thị TKB CŨ cho tới khi có một lượt tương tác khác —
    # đúng lỗi "bấm sinh TKB nhưng Xuất TKB vẫn hiện bản cũ" người dùng báo.
    st.rerun()


def _render_bg_cpsat_status() -> None:
    """Hiển thị trạng thái CP-SAT đang chạy nền và cho phép áp dụng kết quả."""
    task_id = st.session_state.get("bg_task_id")
    if not task_id:
        return

    task   = _BG_TASKS.get(task_id, {})
    status = task.get("status", "unknown")

    st.divider()
    if status == "running":
        col1, col2 = st.columns([4, 1])
        col1.info("⏳ **CP-SAT** đang tối ưu nền — kết quả sẽ hiển thị khi xong.")
        if col2.button("🔄 Kiểm tra", key="check_cpsat"):
            st.rerun()
        # Tự động poll mỗi 5 giây
        _time.sleep(5)
        st.rerun()

    elif status == "done":
        improved_df = task.get("df")
        if improved_df is not None:
            m = task.get("metrics", {})
            st.success(
                f"✨ **CP-SAT tối ưu xong!** "
                f"sáng trống: **{m.get('morning_gaps', '?')}** · "
                f"sessions: **{m.get('teacher_sessions', '?')}** · "
                f"gaps: **{m.get('teacher_gaps', '?')}** · "
                f"overload: **{m.get('overload', '?')}**"
            )
            col_a, col_b = st.columns(2)
            with col_a:
                if st.button("✅ Áp dụng kết quả tối ưu",
                             type="primary", key="apply_cpsat"):
                    st.session_state["gen_timetable"] = improved_df
                    try:
                        database.save_timetable(improved_df)
                        st.session_state["timetable"] = database.get_timetable()
                    except Exception:
                        pass
                    _BG_TASKS.pop(task_id, None)
                    st.session_state.pop("bg_task_id", None)
                    st.rerun()
            with col_b:
                if st.button("❌ Bỏ qua", key="dismiss_cpsat"):
                    _BG_TASKS.pop(task_id, None)
                    st.session_state.pop("bg_task_id", None)
                    st.rerun()

    elif status == "no_improve":
        st.info(f"✅ CP-SAT xác nhận Greedy đã tối ưu. {task.get('msg', '')}")
        _BG_TASKS.pop(task_id, None)
        st.session_state.pop("bg_task_id", None)

    elif status == "error":
        st.warning(f"⚠️ CP-SAT lỗi: {task.get('msg', '')} — đang dùng kết quả Greedy.")
        _BG_TASKS.pop(task_id, None)
        st.session_state.pop("bg_task_id", None)


def _render_gen_timetable():
    gen_df = st.session_state["gen_timetable"]
    st.divider()
    st.markdown("#### 📋 TKB đã sinh")

    view_mode = st.radio("Xem theo:", ["Lớp", "Giáo viên"], horizontal=True, key="sched_view")
    if view_mode == "Lớp":
        sel = st.selectbox("Lớp:", sorted(gen_df["lop"].unique()), key="sched_sel_cls")
        sub = gen_df[gen_df["lop"] == sel]
        pivot = sub.pivot_table(index=["thu", "tiet", "buoi"], columns="lop",
                                values="ten_gv", aggfunc="first")
        st.dataframe(_sort_pivot(pivot, has_buoi=True), use_container_width=True)
    else:
        # Tách tên GV từ team-teaching để có thể chọn riêng lẻ
        all_gv_sched = sorted(set(
            tn.strip()
            for gv in gen_df["ten_gv"].unique()
            for tn in str(gv).split(" / ")
            if tn.strip()
        ))
        sel = st.selectbox("GV:", all_gv_sched, key="sched_sel_gv")
        # Match cả "T1" và "T1 / T2"
        sub = gen_df[gen_df["ten_gv"].apply(
            lambda x: sel in [t.strip() for t in str(x).split(" / ")]
        )]
        pivot = sub.pivot_table(index=["thu", "tiet", "buoi"], columns="lop",
                                values="mon", aggfunc="first")
        st.dataframe(_sort_pivot(pivot, has_buoi=True), use_container_width=True)

    st.divider()
    col_m1, col_m2 = st.columns(2)
    with col_m1:
        if st.button("📥 Áp dụng TKB này cho toàn bộ app", key="sched_merge"):
            database.save_timetable(gen_df)
            st.session_state["timetable"] = database.get_timetable()
            st.success("✅ Đã cập nhật! Xem các tab TKB bên trên.")
            st.rerun()
    with col_m2:
        st.download_button(
            "⬇️ Tải CSV",
            data=gen_df.to_csv(index=False).encode("utf-8-sig"),
            file_name="TKB_generated.csv",
            mime="text/csv",
        )


# =============================================================================
if __name__ == "__main__":
    render_ui()
