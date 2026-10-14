# =============================================================================
# csv_loader.py — Nạp dữ liệu từ file CSV vào ứng dụng
# =============================================================================
# Mỗi hàm load_*_csv() nhận UploadedFile từ Streamlit và trả về:
#   (DataFrame hoặc None, danh sách lỗi)
#
# Cột bắt buộc của từng file:
#   teachers.csv       : ma_so, ten, ten_tat, mon_day
#   classes.csv        : lop, buoi, khoi
#   subjects.csv       : grade, subject, periods_per_week, type
#   class_subjects.csv : lop, subject, periods_per_week
#   assignments.csv    : lop, subject, ma_so
#   homeroom.csv       : lop, ma_so, ten_gv
# =============================================================================

from __future__ import annotations

import io
import pandas as pd

# --------------------------------------------------------------------------- #
# Định nghĩa cột bắt buộc cho từng file CSV                                   #
# --------------------------------------------------------------------------- #

REQUIRED_COLUMNS: dict[str, list[str]] = {
    "timetable":      ["thu", "tiet", "buoi", "lop", "ten_gv", "mon"],
    "teachers":       ["ma_so", "ten", "ten_tat", "mon_day"],
    "classes":        ["lop", "buoi", "khoi"],
    "subjects":       ["grade", "subject", "periods_per_week", "type"],
    "class_subjects": ["lop", "subject", "periods_per_week"],
    "assignments":    ["lop", "subject", "ma_so"],
    "homeroom":       ["lop", "ma_so", "ten_gv"],
}

# Mô tả từng cột để hiển thị hướng dẫn thân thiện
COLUMN_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "timetable": {
        "thu":    "Thứ trong tuần (Thứ 2, Thứ 3, ... Thứ 7)",
        "tiet":   "Tiết học (1–5 sáng, 1–4 chiều)",
        "buoi":   "Buổi học: Sáng hoặc Chiều",
        "lop":    "Tên lớp (VD: 10A1, 11A2, 12A10)",
        "ten_gv": "Họ tên đầy đủ giáo viên",
        "mon":    "Tên môn học",
    },
    "teachers": {
        "ma_so":   "Mã số giáo viên (VD: V1, T8, A4)",
        "ten":     "Họ tên đầy đủ",
        "ten_tat": "Tên viết tắt hiển thị trên TKB",
        "mon_day": "Môn giảng dạy",
    },
    "classes": {
        "lop":  "Tên lớp (VD: 10A1, 11A2, 12A10)",
        "buoi": "Buổi học: S (sáng) hoặc C (chiều)",
        "khoi": "Khối lớp (10, 11, hoặc 12)",
    },
    "subjects": {
        "grade":            "Khối (10, 11, hoặc 12)",
        "subject":          "Tên môn học",
        "periods_per_week": "Số tiết mỗi tuần (số nguyên)",
        "type":             "Loại môn: Bắt buộc hoặc Tự chọn",
    },
    "class_subjects": {
        "lop":              "Tên lớp",
        "subject":          "Tên môn học",
        "periods_per_week": "Số tiết mỗi tuần cho lớp này",
    },
    "assignments": {
        "lop":     "Tên lớp",
        "subject": "Tên môn học",
        "ma_so":   "Mã số giáo viên phụ trách",
    },
    "homeroom": {
        "lop":    "Tên lớp (VD: 10A1, 11A2, 12A10)",
        "ma_so":  "Mã số giáo viên chủ nhiệm",
        "ten_gv": "Họ tên đầy đủ giáo viên chủ nhiệm",
    },
}


# --------------------------------------------------------------------------- #
# Hàm tiện ích nội bộ                                                          #
# --------------------------------------------------------------------------- #

def _validate(df: pd.DataFrame, csv_name: str) -> list[str]:
    """Kiểm tra các cột bắt buộc. Trả về danh sách thông báo lỗi."""
    required = REQUIRED_COLUMNS.get(csv_name, [])
    missing  = [c for c in required if c not in df.columns]
    errors: list[str] = []
    if missing:
        errors.append(f"Thiếu cột: **{', '.join(missing)}**")
    if df.empty:
        errors.append("File không có dữ liệu (chỉ có tiêu đề hoặc rỗng).")
    return errors


def _read_uploaded(uploaded_file) -> tuple[pd.DataFrame | None, list[str]]:
    """Đọc file CSV từ UploadedFile của Streamlit. Hỗ trợ UTF-8 và UTF-8-BOM."""
    try:
        content = uploaded_file.read()
        for enc in ("utf-8-sig", "utf-8", "cp1258", "latin-1"):
            try:
                df = pd.read_csv(io.BytesIO(content), encoding=enc)
                df.columns = [c.strip() for c in df.columns]
                return df, []
            except UnicodeDecodeError:
                continue
        return None, ["Không đọc được file — hãy lưu file CSV theo mã UTF-8."]
    except Exception as exc:
        return None, [f"Lỗi khi đọc file: {exc}"]


# --------------------------------------------------------------------------- #
# Hàm tải từng loại CSV                                                        #
# --------------------------------------------------------------------------- #

def load_timetable_csv(uploaded_file) -> tuple[pd.DataFrame | None, list[str]]:
    """
    Tải timetable.csv — dữ liệu TKB chính.
    Cột bắt buộc: thu, tiet, buoi, lop, ten_gv, mon
    File này có thể xuất từ tab '🤖 Sinh TKB tự động' → nút Tải CSV.
    """
    df, errors = _read_uploaded(uploaded_file)
    if errors:
        return None, errors
    errors = _validate(df, "timetable")
    if errors:
        return None, errors
    df["thu"]    = df["thu"].astype(str).str.strip()
    df["buoi"]   = df["buoi"].astype(str).str.strip()
    df["lop"]    = df["lop"].astype(str).str.strip()
    df["ten_gv"] = df["ten_gv"].astype(str).str.strip()
    df["mon"]    = df["mon"].astype(str).str.strip()
    try:
        df["tiet"] = pd.to_numeric(df["tiet"], errors="coerce").fillna(1).astype(int)
    except Exception:
        return None, ["Cột **tiet** phải là số nguyên (1–5)."]
    return df, []


def load_teachers_csv(uploaded_file) -> tuple[pd.DataFrame | None, list[str]]:
    """
    Tải teachers.csv.
    Cột bắt buộc: ma_so, ten, ten_tat, mon_day
    """
    df, errors = _read_uploaded(uploaded_file)
    if errors:
        return None, errors
    errors = _validate(df, "teachers")
    if errors:
        return None, errors
    df["ma_so"]   = df["ma_so"].astype(str).str.strip()
    df["ten"]     = df["ten"].astype(str).str.strip()
    df["ten_tat"] = df["ten_tat"].astype(str).str.strip()
    df["mon_day"] = df["mon_day"].astype(str).str.strip()
    return df, []


def load_classes_csv(uploaded_file) -> tuple[pd.DataFrame | None, list[str]]:
    """
    Tải classes.csv.
    Cột bắt buộc: lop, buoi, khoi
    """
    df, errors = _read_uploaded(uploaded_file)
    if errors:
        return None, errors
    errors = _validate(df, "classes")
    if errors:
        return None, errors
    df["lop"]  = df["lop"].astype(str).str.strip()
    df["buoi"] = df["buoi"].astype(str).str.strip().str.upper()
    df["khoi"] = df["khoi"].astype(str).str.strip()
    return df, []


def load_subjects_csv(uploaded_file) -> tuple[pd.DataFrame | None, list[str]]:
    """
    Tải subjects.csv.
    Cột bắt buộc: grade, subject, periods_per_week, type
    """
    df, errors = _read_uploaded(uploaded_file)
    if errors:
        return None, errors
    errors = _validate(df, "subjects")
    if errors:
        return None, errors
    df["grade"]   = df["grade"].astype(str).str.strip()
    df["subject"] = df["subject"].astype(str).str.strip()
    df["type"]    = df["type"].astype(str).str.strip()
    try:
        df["periods_per_week"] = pd.to_numeric(df["periods_per_week"], errors="coerce").fillna(0).astype(int)
    except Exception:
        return None, ["Cột **periods_per_week** phải là số nguyên."]
    return df, []


def load_class_subjects_csv(uploaded_file) -> tuple[pd.DataFrame | None, list[str]]:
    """
    Tải class_subjects.csv.
    Cột bắt buộc: lop, subject, periods_per_week
    """
    df, errors = _read_uploaded(uploaded_file)
    if errors:
        return None, errors
    errors = _validate(df, "class_subjects")
    if errors:
        return None, errors
    df["lop"]     = df["lop"].astype(str).str.strip()
    df["subject"] = df["subject"].astype(str).str.strip()
    try:
        df["periods_per_week"] = pd.to_numeric(df["periods_per_week"], errors="coerce").fillna(0).astype(int)
    except Exception:
        return None, ["Cột **periods_per_week** phải là số nguyên."]
    return df, []


def load_assignments_csv(uploaded_file) -> tuple[pd.DataFrame | None, list[str]]:
    """
    Tải assignments.csv.
    Cột bắt buộc: lop, subject, ma_so
    Cảnh báo nếu có nhiều GV cùng dạy một môn cho một lớp.
    """
    df, errors = _read_uploaded(uploaded_file)
    if errors:
        return None, errors
    errors = _validate(df, "assignments")
    if errors:
        return None, errors
    df["lop"]     = df["lop"].astype(str).str.strip()
    df["subject"] = df["subject"].astype(str).str.strip()
    df["ma_so"]   = df["ma_so"].astype(str).str.strip()

    # Phát hiện nhiều GV cùng dạy 1 môn cho 1 lớp
    dup = (
        df.groupby(["lop", "subject"])["ma_so"]
        .apply(list)
        .reset_index()
    )
    dup = dup[dup["ma_so"].apply(len) > 1]
    warnings: list[str] = []
    for _, row in dup.iterrows():
        gvs = ", ".join(f"`{g}`" for g in row["ma_so"])
        warnings.append(
            f"⚠️ **{row['lop']}** — môn **{row['subject']}** "
            f"có {len(row['ma_so'])} GV: {gvs}"
        )

    return df, warnings


def load_homeroom_csv(uploaded_file) -> tuple[pd.DataFrame | None, list[str]]:
    """
    Tải homeroom.csv — phân công giáo viên chủ nhiệm.
    Cột bắt buộc: lop, ma_so, ten_gv
    """
    df, errors = _read_uploaded(uploaded_file)
    if errors:
        return None, errors
    errors = _validate(df, "homeroom")
    if errors:
        return None, errors
    df["lop"]    = df["lop"].astype(str).str.strip()
    df["ma_so"]  = df["ma_so"].astype(str).str.strip()
    df["ten_gv"] = df["ten_gv"].astype(str).str.strip()
    # Kiểm tra trùng lớp
    dup = df[df.duplicated("lop", keep=False)]["lop"].unique()
    if len(dup):
        return None, [f"Lớp bị trùng GVCN: **{', '.join(dup)}** — mỗi lớp chỉ được 1 GVCN."]
    return df, []


# --------------------------------------------------------------------------- #
# Import vào SQLite (qua database.py)                                          #
# --------------------------------------------------------------------------- #

def import_subjects_to_db(df: pd.DataFrame) -> int:
    """
    Nhập dữ liệu từ subjects.csv vào bảng curriculum trong SQLite.
    Trả về số hàng đã upsert.
    """
    import database
    count = 0
    for grade, group in df.groupby("grade"):
        db_df = group.rename(columns={
            "subject":          "Môn dạy",
            "periods_per_week": "Tiết/tuần",
            "type":             "Loại",
        })[["Môn dạy", "Tiết/tuần", "Loại"]]
        database.upsert_curriculum(str(grade), db_df)
        count += len(db_df)
    return count


def get_sample_csv(csv_name: str) -> str:
    """Tạo nội dung CSV mẫu để người dùng tải về tham khảo."""
    samples = {
        "timetable": (
            "thu,tiet,buoi,lop,ten_gv,mon\n"
            "Thứ 2,1,Sáng,10A1,Nguyễn Văn An,Ngữ Văn\n"
            "Thứ 2,2,Sáng,10A1,Trần Thị Bình,Toán\n"
            "Thứ 2,1,Sáng,10A2,Lê Minh Châu,Tiếng Anh\n"
        ),
        "teachers": (
            "ma_so,ten,ten_tat,mon_day\n"
            "V1,Nguyễn Văn An,AN,Ngữ Văn\n"
            "T2,Trần Thị Bình,BÌNH,Toán\n"
            "A3,Lê Minh Châu,CHÂU,Tiếng Anh\n"
        ),
        "classes": (
            "lop,buoi,khoi\n"
            "10A1,S,10\n"
            "10A2,C,10\n"
            "11A1,S,11\n"
        ),
        "subjects": (
            "grade,subject,periods_per_week,type\n"
            "10,Ngữ Văn,4,Bắt buộc\n"
            "10,Toán,4,Bắt buộc\n"
            "10,Tiếng Anh,3,Bắt buộc\n"
        ),
        "class_subjects": (
            "lop,subject,periods_per_week\n"
            "10A1,Ngữ Văn,4\n"
            "10A1,Toán,4\n"
            "10A2,Ngữ Văn,3\n"
        ),
        "assignments": (
            "lop,subject,ma_so\n"
            "10A1,Ngữ Văn,V1\n"
            "10A1,Toán,T2\n"
            "10A2,Tiếng Anh,A3\n"
        ),
        "homeroom": (
            "lop,ma_so,ten_gv\n"
            "10A1,V1,Nguyễn Văn An\n"
            "10A2,T2,Trần Thị Bình\n"
            "11A1,A3,Lê Minh Châu\n"
            "11A2,H4,Phạm Thị Dung\n"
            "12A1,S5,Hoàng Văn Em\n"
        ),
    }
    return samples.get(csv_name, "")
