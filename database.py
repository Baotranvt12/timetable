"""
database.py — SQLite persistence layer for curriculum data.

Schema:
  curriculum(id, grade, subject, periods_per_week, type)
  teacher_prefs(id, ma_so, ten, mon_day, max_per_week, max_per_day, preferred_classes, notes)
  unavailable_slots(id, teacher_id, day, period, buoi)

Usage:
  import database
  database.init_db()          # run once at startup
  df = database.get_curriculum("12")
  database.upsert_curriculum("12", df)
"""

import contextlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import pandas as pd

DB_PATH = Path("data/timetable.db")

# ── Turso (SQLite trên mạng) ──────────────────────────────────────────────────
# Có cấu hình Turso (st.secrets["turso"] hoặc biến môi trường TURSO_DATABASE_URL /
# TURSO_AUTH_TOKEN) → dùng bản sao cục bộ (embedded replica) đồng bộ với Turso:
# đọc từ file cục bộ (nhanh), ghi gửi thẳng lên Turso. Không có → file DB_PATH.
REPLICA_PATH = Path("data/turso_replica.db")
SYNC_EVERY_S = 20   # đồng bộ lại để thấy thay đổi từ máy/người dùng khác


def _turso_config() -> tuple[str, str] | None:
    url = os.environ.get("TURSO_DATABASE_URL", "")
    token = os.environ.get("TURSO_AUTH_TOKEN", "")
    if not url:
        try:
            import streamlit as st
            cfg = st.secrets.get("turso", {})
            url, token = cfg.get("url", ""), cfg.get("auth_token", "")
        except Exception:
            pass
    return (url, token) if url else None


class _Row(tuple):
    """Giống sqlite3.Row: truy cập theo chỉ số hoặc tên cột, dùng được với dict(row)."""
    def __new__(cls, values, index):
        r = super().__new__(cls, values)
        r._index = index
        return r

    def __getitem__(self, k):
        return tuple.__getitem__(self, self._index[k] if isinstance(k, str) else k)

    def keys(self):
        return list(self._index)


class _Cursor:
    def __init__(self, cur):
        self._cur = cur
        desc = cur.description or ()
        self._index = {d[0]: i for i, d in enumerate(desc)}
        self.rowcount = getattr(cur, "rowcount", -1)
        self.lastrowid = getattr(cur, "lastrowid", None)
        self.description = cur.description

    def _wrap(self, r):
        return None if r is None else _Row(r, self._index)

    def fetchone(self):
        return self._wrap(self._cur.fetchone())

    def fetchall(self):
        return [self._wrap(r) for r in self._cur.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class _LibsqlConn:
    """Bọc kết nối libsql cho giống sqlite3 ở những gì module này dùng."""
    def __init__(self, raw):
        self._raw = raw
        self.wrote = False

    def execute(self, sql, params=()):
        if not sql.lstrip().upper().startswith(("SELECT", "PRAGMA", "WITH")):
            self.wrote = True
        try:
            return _Cursor(self._raw.execute(sql, tuple(params)))
        except ValueError as e:
            raise _as_integrity(e)

    def executemany(self, sql, seq):
        self.wrote = True
        try:
            return _Cursor(self._raw.executemany(sql, [tuple(p) for p in seq]))
        except ValueError as e:
            raise _as_integrity(e)

    def executescript(self, script):
        self.wrote = True
        return self._raw.executescript(script)

    @property
    def total_changes(self):
        return self._raw.execute("SELECT total_changes()").fetchone()[0]

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()


def _as_integrity(e: Exception) -> Exception:
    msg = str(e)
    if "UNIQUE" in msg or "constraint" in msg.lower():
        return sqlite3.IntegrityError(msg)
    return e


_TURSO_LOCK = threading.RLock()
_turso_conn = None
_turso_last_sync = 0.0


def _turso_connection(url: str, token: str):
    """1 kết nối dùng chung cho cả tiến trình (libsql không an toàn đa luồng → khoá)."""
    global _turso_conn, _turso_last_sync
    import libsql
    if _turso_conn is None:
        REPLICA_PATH.parent.mkdir(exist_ok=True)
        _turso_conn = libsql.connect(str(REPLICA_PATH), sync_url=url, auth_token=token)
        _turso_conn.sync()
        _turso_last_sync = time.time()
    elif time.time() - _turso_last_sync > SYNC_EVERY_S:
        _turso_conn.sync()
        _turso_last_sync = time.time()
    return _turso_conn


def using_turso() -> bool:
    return _turso_config() is not None

# ── Seed data (từ yêu cầu của người dùng) ────────────────────────────────────
# Dữ liệu này áp dụng cho cả 3 khối (10, 11, 12) làm giá trị mặc định.
# Khối 10 và 11 có thể có một số môn khác nhau — điều chỉnh sau khi seed.
SEED_SUBJECTS: list[dict] = [
    {"subject": "ANH",      "periods_per_week": 3, "type": "Bắt buộc"},
    {"subject": "AVBN",     "periods_per_week": 2, "type": "Tự chọn"},
    {"subject": "C.NGHỆ",   "periods_per_week": 2, "type": "Tự chọn"},
    {"subject": "KT&PL",    "periods_per_week": 2, "type": "Bắt buộc"},
    {"subject": "GDQP",     "periods_per_week": 1, "type": "Bắt buộc"},
    {"subject": "GDĐP",     "periods_per_week": 1, "type": "Bắt buộc"},
    {"subject": "HÓA",      "periods_per_week": 2, "type": "Tự chọn"},
    {"subject": "KN SỐNG",  "periods_per_week": 1, "type": "Tự chọn"},
    {"subject": "LÝ",       "periods_per_week": 2, "type": "Tự chọn"},
    {"subject": "SINH",     "periods_per_week": 2, "type": "Tự chọn"},
    {"subject": "SỬ",       "periods_per_week": 1, "type": "Tự chọn"},
    {"subject": "THQT",     "periods_per_week": 2, "type": "Tự chọn"},
    {"subject": "THỂ DỤC",  "periods_per_week": 2, "type": "Bắt buộc"},
    {"subject": "TIN HỌC",  "periods_per_week": 2, "type": "Tự chọn"},
    {"subject": "TNHN",     "periods_per_week": 2, "type": "Tự chọn"},
    {"subject": "TOÁN",     "periods_per_week": 3, "type": "Bắt buộc"},
    {"subject": "VĂN",      "periods_per_week": 3, "type": "Bắt buộc"},
    {"subject": "ĐỊA",      "periods_per_week": 2, "type": "Tự chọn"},
]


# ── Connection ────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def _connect():
    """`with _connect() as conn:` — mở kết nối, commit/rollback rồi ĐÓNG hẳn
    khi thoát khối `with`. Trước đây `_connect()` trả thẳng về
    `sqlite3.Connection` và `with conn:` của sqlite3 chỉ bọc transaction
    (commit/rollback) — KHÔNG đóng connection — nên mỗi lần gọi một hàm
    database.* là rò 1 file descriptor; sau nhiều giờ chạy, 1 process
    Streamlit có thể tích luỹ hàng trăm FD trên cùng 1 file và cuối cùng
    sqlite3.connect() tự thất bại với "disk I/O error" dù đĩa còn trống và
    DB không hỏng (xác nhận bằng PRAGMA integrity_check). Luôn đóng bằng
    try/finally để không rò dù có exception.
    """
    cfg = _turso_config()
    if cfg:
        global _turso_last_sync
        with _TURSO_LOCK:
            conn = _LibsqlConn(_turso_connection(*cfg))
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            if conn.wrote:   # kéo bản ghi vừa ghi về bản sao cục bộ
                conn._raw.sync()
                _turso_last_sync = time.time()
        return

    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create tables and seed defaults on first run."""
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS curriculum (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                grade            TEXT    NOT NULL,
                subject          TEXT    NOT NULL,
                periods_per_week INTEGER NOT NULL DEFAULT 0,
                type             TEXT    NOT NULL DEFAULT 'Tự chọn',
                UNIQUE(grade, subject)
            );

            CREATE TABLE IF NOT EXISTS teacher_prefs (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                ma_so            TEXT    NOT NULL UNIQUE,
                ten              TEXT    NOT NULL DEFAULT '',
                mon_day          TEXT    NOT NULL DEFAULT '',
                max_per_week     INTEGER NOT NULL DEFAULT 18,
                max_per_day      INTEGER NOT NULL DEFAULT 4,
                preferred_classes TEXT   NOT NULL DEFAULT '[]',
                notes            TEXT    NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS unavailable_slots (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                teacher_id INTEGER NOT NULL REFERENCES teacher_prefs(id) ON DELETE CASCADE,
                day        TEXT    NOT NULL,
                period     INTEGER NOT NULL,
                buoi       TEXT    NOT NULL,
                UNIQUE(teacher_id, day, period, buoi)
            );

            -- Dữ liệu CSV upload: lưu để tái sử dụng sau khi refresh/restart
            CREATE TABLE IF NOT EXISTS teachers (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ma_so    TEXT    NOT NULL UNIQUE,
                ten      TEXT    NOT NULL DEFAULT '',
                ten_tat  TEXT    NOT NULL DEFAULT '',
                mon_day  TEXT    NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS classes (
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                lop   TEXT    NOT NULL UNIQUE,
                buoi  TEXT    NOT NULL DEFAULT 'S',
                khoi  TEXT    NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS assignments (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                lop     TEXT    NOT NULL,
                subject TEXT    NOT NULL,
                ma_so   TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS class_subjects (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                lop              TEXT    NOT NULL,
                subject          TEXT    NOT NULL,
                periods_per_week INTEGER NOT NULL DEFAULT 0,
                UNIQUE(lop, subject)
            );

            CREATE TABLE IF NOT EXISTS homeroom (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                lop    TEXT    NOT NULL UNIQUE,
                ma_so  TEXT    NOT NULL DEFAULT '',
                ten_gv TEXT    NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS timetable (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                thu    TEXT    NOT NULL,
                tiet   INTEGER NOT NULL,
                buoi   TEXT    NOT NULL,
                lop    TEXT    NOT NULL,
                ten_gv TEXT    NOT NULL DEFAULT '',
                mon    TEXT    NOT NULL DEFAULT '',
                UNIQUE(thu, tiet, buoi, lop)
            );

            CREATE TABLE IF NOT EXISTS change_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           TEXT    NOT NULL,
                batch_id     TEXT    NOT NULL,
                action       TEXT    NOT NULL,
                timetable_id INTEGER,
                lop          TEXT    NOT NULL DEFAULT '',
                ten_gv       TEXT    NOT NULL DEFAULT '',
                before_json  TEXT,
                after_json   TEXT,
                note         TEXT    NOT NULL DEFAULT '',
                author       TEXT    NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_change_log_lop    ON change_log(lop);
            CREATE INDEX IF NOT EXISTS idx_change_log_ten_gv ON change_log(ten_gv);
            CREATE INDEX IF NOT EXISTS idx_change_log_ts     ON change_log(ts);

            -- Ghi nhớ lớp này được gán vào ĐÚNG mã nhóm nào cho 1 môn tự
            -- chọn (vd "CT10-AVBN-05") — chỉ để HIỂN THỊ, không ảnh hưởng
            -- logic xếp TKB (scheduler chỉ cần môn + GV như thường).
            CREATE TABLE IF NOT EXISTS elective_group_map (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                lop     TEXT    NOT NULL,
                subject TEXT    NOT NULL,
                ma_nhom TEXT    NOT NULL DEFAULT '',
                UNIQUE(lop, subject)
            );

            -- Thông tin học sinh + nhóm môn tự chọn từng em đã đăng ký,
            -- đọc từ các file TKB_Danh_Sach_HS_Khoi_*.xlsx. mahs là khoá
            -- định danh tuyệt đối (không đổi khi HS chuyển lớp).
            CREATE TABLE IF NOT EXISTS students (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ma_hs        TEXT    NOT NULL UNIQUE,
                ten          TEXT    NOT NULL DEFAULT '',
                lop          TEXT    NOT NULL DEFAULT '',
                khoi         TEXT    NOT NULL DEFAULT '',
                gdtc_nhom    TEXT    NOT NULL DEFAULT '',
                gdtc_mon     TEXT    NOT NULL DEFAULT '',
                thqt_nhom    TEXT    NOT NULL DEFAULT '',
                avbn_nhom    TEXT    NOT NULL DEFAULT '',
                stem_nhom    TEXT    NOT NULL DEFAULT '',
                on_tn_1_nhom TEXT    NOT NULL DEFAULT '',
                on_tn_1_mon  TEXT    NOT NULL DEFAULT '',
                on_tn_2_nhom TEXT    NOT NULL DEFAULT '',
                on_tn_2_mon  TEXT    NOT NULL DEFAULT '',
                ghi_chu      TEXT    NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_students_lop  ON students(lop);
            CREATE INDEX IF NOT EXISTS idx_students_khoi ON students(khoi);

            -- Lịch học cụ thể (Thứ/tiết/GV/phòng) của từng buổi nhóm tự
            -- chọn/ôn TN mà 1 HS đã đăng ký — đọc thẳng từ cell Excel, do
            -- trường đã ấn định sẵn (không phải do app xếp). loai là 1
            -- trong 'GDTC'|'THQT'|'AVBN'|'STEM'|'ON_TN_1'|'ON_TN_2'.
            -- tiet_dau/tiet_cuoi đánh số 1-8 cho cả ngày (1-4 sáng, 5-8
            -- chiều), khác quy ước tiet 1-4/buổi của bảng timetable.
            CREATE TABLE IF NOT EXISTS student_electives (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ma_hs     TEXT    NOT NULL,
                loai      TEXT    NOT NULL,
                ma_nhom   TEXT    NOT NULL DEFAULT '',
                mon       TEXT    NOT NULL DEFAULT '',
                thu       TEXT    NOT NULL DEFAULT '',
                tiet_dau  INTEGER,
                tiet_cuoi INTEGER,
                gv        TEXT    NOT NULL DEFAULT '',
                phong     TEXT    NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_student_electives_mahs ON student_electives(ma_hs);

            -- GV được miễn chặn buổi sáng sinh hoạt chuyên môn của tổ
            CREATE TABLE IF NOT EXISTS shcm_exempt (
                ma_so   TEXT PRIMARY KEY,
                ghi_chu TEXT NOT NULL DEFAULT ''
            );
        """)
    _seed_if_empty()


def _seed_if_empty() -> None:
    with _connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM curriculum").fetchone()[0]
        if count > 0:
            return
        for grade in ("10", "11", "12"):
            conn.executemany(
                "INSERT OR IGNORE INTO curriculum (grade, subject, periods_per_week, type) "
                "VALUES (?, ?, ?, ?)",
                [(grade, r["subject"], r["periods_per_week"], r["type"])
                 for r in SEED_SUBJECTS],
            )


# ── Curriculum CRUD ───────────────────────────────────────────────────────────

def get_curriculum(grade: str) -> pd.DataFrame:
    """Return curriculum for one grade as a DataFrame [Môn dạy, Tiết/tuần, Loại]."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT subject, periods_per_week, type "
            "FROM curriculum WHERE grade = ? ORDER BY subject",
            (grade,),
        ).fetchall()
    return pd.DataFrame(
        [{"Môn dạy": r["subject"],
          "Tiết/tuần": r["periods_per_week"],
          "Loại": r["type"]} for r in rows]
    )


def upsert_curriculum(grade: str, df: pd.DataFrame) -> None:
    """Save a full curriculum DataFrame for one grade (insert or update)."""
    with _connect() as conn:
        conn.executemany(
            "INSERT INTO curriculum (grade, subject, periods_per_week, type) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(grade, subject) DO UPDATE SET "
            "  periods_per_week = excluded.periods_per_week, "
            "  type = excluded.type",
            [(grade, row["Môn dạy"], int(row["Tiết/tuần"]), row["Loại"])
             for _, row in df.iterrows()],
        )


def add_subject(grade: str, subject: str, periods: int, stype: str) -> bool:
    """Add a new subject row. Returns False if already exists."""
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO curriculum (grade, subject, periods_per_week, type) "
                "VALUES (?, ?, ?, ?)",
                (grade, subject.strip(), periods, stype),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def delete_subject(grade: str, subject: str) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM curriculum WHERE grade = ? AND subject = ?",
            (grade, subject),
        )


def reset_curriculum(grade: str) -> None:
    """Delete and re-seed defaults for one grade."""
    with _connect() as conn:
        conn.execute("DELETE FROM curriculum WHERE grade = ?", (grade,))
        conn.executemany(
            "INSERT INTO curriculum (grade, subject, periods_per_week, type) "
            "VALUES (?, ?, ?, ?)",
            [(grade, r["subject"], r["periods_per_week"], r["type"])
             for r in SEED_SUBJECTS],
        )


def clear_curriculum(grade: str | None = None) -> int:
    """Xoá toàn bộ curriculum của một khối hoặc tất cả khối.
    Trả về số hàng đã xoá."""
    with _connect() as conn:
        if grade:
            n = conn.execute(
                "DELETE FROM curriculum WHERE grade = ?", (grade,)
            ).rowcount
        else:
            n = conn.execute("DELETE FROM curriculum").rowcount
    return n


def get_all_curriculum() -> dict[str, list[dict]]:
    """Return full curriculum dict {grade: [{subject, periods_per_week, type}]}."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT grade, subject, periods_per_week, type FROM curriculum ORDER BY grade, subject"
        ).fetchall()
    result: dict[str, list] = {}
    for r in rows:
        result.setdefault(r["grade"], []).append({
            "subject": r["subject"],
            "periods_per_week": r["periods_per_week"],
            "type": r["type"],
        })
    return result


# ── Teacher Preferences CRUD ──────────────────────────────────────────────────

def upsert_teacher_pref(
    ma_so: str, ten: str, mon_day: str,
    max_per_week: int, max_per_day: int,
    preferred_classes: list[str], notes: str,
    unavailable: list[dict],          # [{"day": ..., "period": ..., "buoi": ...}]
) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO teacher_prefs "
            "  (ma_so, ten, mon_day, max_per_week, max_per_day, preferred_classes, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(ma_so) DO UPDATE SET "
            "  ten = excluded.ten, mon_day = excluded.mon_day, "
            "  max_per_week = excluded.max_per_week, "
            "  max_per_day  = excluded.max_per_day, "
            "  preferred_classes = excluded.preferred_classes, "
            "  notes = excluded.notes",
            (ma_so, ten, mon_day, max_per_week, max_per_day,
             json.dumps(preferred_classes, ensure_ascii=False), notes),
        )
        teacher_id = conn.execute(
            "SELECT id FROM teacher_prefs WHERE ma_so = ?", (ma_so,)
        ).fetchone()["id"]

        conn.execute("DELETE FROM unavailable_slots WHERE teacher_id = ?", (teacher_id,))
        if unavailable:
            conn.executemany(
                "INSERT OR IGNORE INTO unavailable_slots (teacher_id, day, period, buoi) "
                "VALUES (?, ?, ?, ?)",
                [(teacher_id, s["day"], s["period"], s["buoi"]) for s in unavailable],
            )


def get_teacher_pref(ma_so: str) -> dict:
    """Return saved prefs for one teacher, or empty dict."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM teacher_prefs WHERE ma_so = ?", (ma_so,)
        ).fetchone()
        if row is None:
            return {}
        slots = conn.execute(
            "SELECT day, period, buoi FROM unavailable_slots WHERE teacher_id = ?",
            (row["id"],),
        ).fetchall()
    return {
        "ma_so":             row["ma_so"],
        "ten":               row["ten"],
        "mon_day":           row["mon_day"],
        "max_periods_per_week": row["max_per_week"],
        "max_periods_per_day":  row["max_per_day"],
        "preferred_classes": json.loads(row["preferred_classes"]),
        "notes":             row["notes"],
        "unavailable_slots": [{"day": s["day"], "period": s["period"], "buoi": s["buoi"]}
                               for s in slots],
    }


# Sinh hoạt chuyên môn: GV thuộc tổ nào thì bận cả buổi sáng ngày họp tổ đó,
# bất kể đang dạy môn gì (kể cả HĐTN, GDĐP, chuyên đề). Khóa theo teachers.mon_day.
DEPT_MEETING_DAY_BY_DEPT: dict[str, str] = {
    "TOÁN": "Thứ 3", "LỊCH SỬ": "Thứ 3", "ĐỊA LÍ": "Thứ 3", "GDQP&AN": "Thứ 3",
    "SỬ": "Thứ 3", "ĐỊA": "Thứ 3", "GDQP": "Thứ 3", "QPAN": "Thứ 3",
    "VẬT LÍ": "Thứ 4", "CÔNG NGHỆ": "Thứ 4", "NGỮ VĂN": "Thứ 4", "HÓA HỌC": "Thứ 4",
    "HOÁ HỌC": "Thứ 4", "LÝ": "Thứ 4", "CN": "Thứ 4", "VĂN": "Thứ 4", "HÓA": "Thứ 4",
    "GD KT&PL": "Thứ 5", "KTPL": "Thứ 5",
    "TIẾNG ANH": "Thứ 6", "SINH HỌC": "Thứ 6", "ANH": "Thứ 6", "SINH": "Thứ 6",
}
DEPT_MEETING_PERIODS = (1, 2, 3, 4)


def dept_meeting_day(mon_day: str) -> str | None:
    import unicodedata
    key = unicodedata.normalize("NFC", str(mon_day or "")).strip().upper()
    return DEPT_MEETING_DAY_BY_DEPT.get(key)


def dept_meeting_slots(mon_day: str) -> list[dict]:
    day = dept_meeting_day(mon_day)
    if not day:
        return []
    return [{"day": day, "period": p, "buoi": "Sáng"} for p in DEPT_MEETING_PERIODS]


def get_shcm_exempt() -> set[str]:
    """Mã GV được miễn chặn sáng SHCM."""
    with _connect() as conn:
        return {r["ma_so"] for r in conn.execute("SELECT ma_so FROM shcm_exempt")}


def set_shcm_exempt(ma_so_list: list[str]) -> None:
    """Ghi đè danh sách GV miễn SHCM và gỡ các tiết bận SHCM đã lưu của họ."""
    with _connect() as conn:
        conn.execute("DELETE FROM shcm_exempt")
        conn.executemany("INSERT INTO shcm_exempt (ma_so) VALUES (?)",
                         [(m,) for m in ma_so_list])
        for m in ma_so_list:
            t = conn.execute("SELECT mon_day FROM teachers WHERE ma_so = ?", (m,)).fetchone()
            day = dept_meeting_day(t["mon_day"]) if t else None
            if day:
                conn.execute(
                    "DELETE FROM unavailable_slots WHERE buoi = 'Sáng' AND day = ? AND teacher_id = "
                    "(SELECT id FROM teacher_prefs WHERE ma_so = ?)", (day, m),
                )


def sync_dept_meeting_slots() -> dict:
    """Ghi lịch SHCM vào lịch bận của mọi GV trong bảng teachers (trừ GV miễn).
    Chỉ thêm, không xoá tiết bận GV đã tự khai. Trả về thống kê."""
    n_teachers = n_new = 0
    exempt = get_shcm_exempt()
    with _connect() as conn:
        teachers = conn.execute("SELECT ma_so, ten, mon_day FROM teachers").fetchall()
        for t in teachers:
            if t["ma_so"] in exempt:
                continue
            slots = dept_meeting_slots(t["mon_day"])
            if not slots:
                continue
            n_teachers += 1
            conn.execute(
                "INSERT INTO teacher_prefs (ma_so, ten, mon_day) VALUES (?, ?, ?) "
                "ON CONFLICT(ma_so) DO NOTHING",
                (t["ma_so"], t["ten"], t["mon_day"]),
            )
            teacher_id = conn.execute(
                "SELECT id FROM teacher_prefs WHERE ma_so = ?", (t["ma_so"],)
            ).fetchone()["id"]
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO unavailable_slots (teacher_id, day, period, buoi) "
                "VALUES (?, ?, ?, ?)",
                [(teacher_id, s["day"], s["period"], s["buoi"]) for s in slots],
            )
            n_new += conn.total_changes - before
    return {"teachers": n_teachers, "new_slots": n_new}


def get_all_teacher_prefs() -> list[dict]:
    """Summary row for each teacher that has saved prefs."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT tp.ma_so, tp.ten, tp.mon_day, tp.max_per_week, tp.notes, "
            "       COUNT(us.id) AS n_busy "
            "FROM teacher_prefs tp "
            "LEFT JOIN unavailable_slots us ON us.teacher_id = tp.id "
            "GROUP BY tp.id ORDER BY tp.ten"
        ).fetchall()
    return [dict(r) for r in rows]


# ── CSV Data Tables CRUD ──────────────────────────────────────────────────────
# Các hàm này lưu/tải 6 loại CSV để dữ liệu tồn tại sau khi refresh/restart.

# ── Teachers ──────────────────────────────────────────────────────────────────

def save_teachers(df: pd.DataFrame) -> int:
    """Lưu toàn bộ danh sách GV (xoá cũ, nhập mới). Trả về số hàng."""
    with _connect() as conn:
        conn.execute("DELETE FROM teachers")
        conn.executemany(
            "INSERT OR REPLACE INTO teachers (ma_so, ten, ten_tat, mon_day) VALUES (?, ?, ?, ?)",
            [(str(r["ma_so"]), str(r["ten"]), str(r["ten_tat"]), str(r["mon_day"]))
             for _, r in df.iterrows()],
        )
    return len(df)


def get_teachers() -> pd.DataFrame:
    """Tải danh sách GV. Trả về DataFrame rỗng nếu chưa có."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ma_so, ten, ten_tat, mon_day FROM teachers ORDER BY ten"
        ).fetchall()
    if not rows:
        return pd.DataFrame(columns=["ma_so", "ten", "ten_tat", "mon_day"])
    return pd.DataFrame([dict(r) for r in rows])


# ── Classes ───────────────────────────────────────────────────────────────────

def save_classes(df: pd.DataFrame) -> int:
    """Lưu toàn bộ danh sách lớp. Trả về số hàng."""
    with _connect() as conn:
        conn.execute("DELETE FROM classes")
        conn.executemany(
            "INSERT OR REPLACE INTO classes (lop, buoi, khoi) VALUES (?, ?, ?)",
            [(str(r["lop"]), str(r["buoi"]), str(r["khoi"]))
             for _, r in df.iterrows()],
        )
    return len(df)


def get_classes() -> pd.DataFrame:
    """Tải danh sách lớp. Trả về DataFrame rỗng nếu chưa có."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT lop, buoi, khoi FROM classes ORDER BY khoi, lop"
        ).fetchall()
    if not rows:
        return pd.DataFrame(columns=["lop", "buoi", "khoi"])
    return pd.DataFrame([dict(r) for r in rows])


# ── Assignments ───────────────────────────────────────────────────────────────

def save_assignments(df: pd.DataFrame) -> int:
    """Lưu phân công giảng dạy. Trả về số hàng.
    Tự động migrate bảng cũ (có UNIQUE constraint) sang bảng mới nếu cần."""
    with _connect() as conn:
        # Migrate: nếu bảng cũ vẫn có UNIQUE(lop, subject) thì drop & recreate
        try:
            conn.execute("DROP INDEX IF EXISTS sqlite_autoindex_assignments_1")
        except Exception:
            pass
        # Recreate không UNIQUE nếu bảng cũ còn ràng buộc
        idx_info = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='assignments'"
        ).fetchone()
        if idx_info and "UNIQUE" in str(idx_info[0]):
            conn.executescript("""
                DROP TABLE IF EXISTS assignments_old;
                ALTER TABLE assignments RENAME TO assignments_old;
                CREATE TABLE assignments (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    lop     TEXT    NOT NULL,
                    subject TEXT    NOT NULL,
                    ma_so   TEXT    NOT NULL
                );
                DROP TABLE assignments_old;
            """)

        conn.execute("DELETE FROM assignments")
        conn.executemany(
            "INSERT INTO assignments (lop, subject, ma_so) VALUES (?, ?, ?)",
            [(str(r["lop"]), str(r["subject"]), str(r["ma_so"]))
             for _, r in df.iterrows()],
        )
    return len(df)


def get_assignments() -> pd.DataFrame:
    """Tải phân công giảng dạy. Trả về DataFrame rỗng nếu chưa có."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT lop, subject, ma_so FROM assignments ORDER BY lop, subject"
        ).fetchall()
    if not rows:
        return pd.DataFrame(columns=["lop", "subject", "ma_so"])
    return pd.DataFrame([dict(r) for r in rows])


# ── Class Subjects ────────────────────────────────────────────────────────────

def save_class_subjects(df: pd.DataFrame) -> int:
    """Lưu số tiết từng môn theo lớp. Trả về số hàng."""
    with _connect() as conn:
        conn.execute("DELETE FROM class_subjects")
        conn.executemany(
            "INSERT OR REPLACE INTO class_subjects (lop, subject, periods_per_week) VALUES (?, ?, ?)",
            [(str(r["lop"]), str(r["subject"]), int(r["periods_per_week"]))
             for _, r in df.iterrows()],
        )
    return len(df)


def get_class_subjects() -> pd.DataFrame:
    """Tải số tiết theo lớp. Trả về DataFrame rỗng nếu chưa có."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT lop, subject, periods_per_week FROM class_subjects ORDER BY lop, subject"
        ).fetchall()
    if not rows:
        return pd.DataFrame(columns=["lop", "subject", "periods_per_week"])
    return pd.DataFrame([dict(r) for r in rows])


# ── Homeroom ──────────────────────────────────────────────────────────────────

def save_homeroom(df: pd.DataFrame) -> int:
    """Lưu danh sách GVCN. Trả về số hàng."""
    with _connect() as conn:
        conn.execute("DELETE FROM homeroom")
        conn.executemany(
            "INSERT OR REPLACE INTO homeroom (lop, ma_so, ten_gv) VALUES (?, ?, ?)",
            [(str(r["lop"]), str(r["ma_so"]), str(r["ten_gv"]))
             for _, r in df.iterrows()],
        )
    return len(df)


def get_homeroom() -> pd.DataFrame:
    """Tải danh sách GVCN. Trả về DataFrame rỗng nếu chưa có."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT lop, ma_so, ten_gv FROM homeroom ORDER BY lop"
        ).fetchall()
    if not rows:
        return pd.DataFrame(columns=["lop", "ma_so", "ten_gv"])
    return pd.DataFrame([dict(r) for r in rows])


# ── Timetable ─────────────────────────────────────────────────────────────────

def save_timetable(df: pd.DataFrame) -> int:
    """Lưu TKB (xoá cũ, nhập mới). Trả về số hàng."""
    with _connect() as conn:
        conn.execute("DELETE FROM timetable")
        conn.executemany(
            "INSERT OR REPLACE INTO timetable (thu, tiet, buoi, lop, ten_gv, mon) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(str(r["thu"]), int(r["tiet"]), str(r["buoi"]),
              str(r["lop"]), str(r["ten_gv"]), str(r["mon"]))
             for _, r in df.iterrows()],
        )
    return len(df)


def timetable_version() -> tuple:
    """Dấu hiệu thay đổi của TKB trong DB (đổi khi lưu lại TKB hoặc có thay đổi được ghi log)."""
    with _connect() as conn:
        return tuple(conn.execute(
            "SELECT (SELECT COUNT(*) FROM timetable), (SELECT MAX(id) FROM timetable), "
            "(SELECT MAX(id) FROM change_log)"
        ).fetchone())


def get_timetable() -> pd.DataFrame:
    """Tải TKB đã lưu. Trả về DataFrame rỗng nếu chưa có."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, thu, tiet, buoi, lop, ten_gv, mon FROM timetable"
        ).fetchall()
    if not rows:
        return pd.DataFrame(columns=["id", "thu", "tiet", "buoi", "lop", "ten_gv", "mon"])
    df = pd.DataFrame([dict(r) for r in rows])
    df["id"]   = df["id"].astype(int)
    df["tiet"] = df["tiet"].astype(int)
    return df


# ── Timetable — CRUD theo từng dòng (dùng cho chỉnh tay / kéo-thả) ────────────
# Các hàm này KHÔNG tự kiểm tra hợp lệ (trùng GV/lớp, GV báo bận, v.v.) — nơi
# gọi phải kiểm bằng timetable_rules.is_move_legal() trước, nếu không sẽ có
# thể vi phạm UNIQUE(thu, tiet, buoi, lop) và raise sqlite3.IntegrityError.

def insert_timetable_row(thu: str, tiet: int, buoi: str, lop: str, ten_gv: str, mon: str) -> int:
    """Thêm một tiết học mới. Trả về id vừa tạo."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO timetable (thu, tiet, buoi, lop, ten_gv, mon) VALUES (?, ?, ?, ?, ?, ?)",
            (thu, int(tiet), buoi, lop, ten_gv, mon),
        )
        return cur.lastrowid


def get_timetable_row(row_id: int) -> dict | None:
    """Lấy một tiết học theo id (dùng để chụp snapshot trước khi ghi change_log)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, thu, tiet, buoi, lop, ten_gv, mon FROM timetable WHERE id = ?",
            (row_id,),
        ).fetchone()
    return dict(row) if row else None


def update_timetable_cell(row_id: int, ten_gv: str | None = None, mon: str | None = None) -> None:
    """Đổi GV/môn của một tiết (giữ nguyên thu/tiet/buoi/lop). None = giữ nguyên."""
    sets: list[str] = []
    params: list = []
    if ten_gv is not None:
        sets.append("ten_gv = ?"); params.append(ten_gv)
    if mon is not None:
        sets.append("mon = ?"); params.append(mon)
    if not sets:
        return
    params.append(row_id)
    with _connect() as conn:
        conn.execute(f"UPDATE timetable SET {', '.join(sets)} WHERE id = ?", params)


def move_timetable_row(row_id: int, thu: str, tiet: int, buoi: str, lop: str) -> None:
    """Chuyển một tiết học sang (thu, tiet, buoi, lop) mới."""
    with _connect() as conn:
        conn.execute(
            "UPDATE timetable SET thu=?, tiet=?, buoi=?, lop=? WHERE id=?",
            (thu, int(tiet), buoi, lop, row_id),
        )


def delete_timetable_row(row_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM timetable WHERE id = ?", (row_id,))


# ── Lịch sử thay đổi (change_log) ─────────────────────────────────────────────

def _json_default(o):
    """Xử lý các kiểu numpy/pandas (int64, v.v.) không tự json.dumps được —
    before/after dict thường được dựng trực tiếp từ một dòng DataFrame."""
    if hasattr(o, "item"):
        return o.item()
    return str(o)


def log_change(
    action: str, timetable_id: int | None,
    before: dict | None, after: dict | None,
    note: str = "", author: str = "", batch_id: str | None = None,
) -> str:
    """Ghi một dòng lịch sử thay đổi. Trả về batch_id đã dùng (tự sinh uuid4
    nếu không truyền vào — dùng để nhóm nhiều dòng cùng một hành động)."""
    import uuid
    import datetime
    bid = batch_id or uuid.uuid4().hex
    lop    = (after or before or {}).get("lop", "")
    ten_gv = (after or before or {}).get("ten_gv", "")
    with _connect() as conn:
        conn.execute(
            "INSERT INTO change_log (ts, batch_id, action, timetable_id, lop, ten_gv, "
            " before_json, after_json, note, author) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
             bid, action, timetable_id, lop, ten_gv,
             json.dumps(before, ensure_ascii=False, default=_json_default) if before else None,
             json.dumps(after, ensure_ascii=False, default=_json_default) if after else None,
             note, author),
        )
    return bid


def get_change_log(limit: int = 200, lop: str | None = None, ten_gv: str | None = None,
                    since: str | None = None) -> pd.DataFrame:
    """Tải lịch sử thay đổi, mới nhất trước, có thể lọc theo lớp/GV/thời điểm."""
    cols = ["ts", "action", "lop", "ten_gv", "before_json", "after_json", "note", "author", "batch_id"]
    q = f"SELECT {', '.join(cols)} FROM change_log WHERE 1=1"
    params: list = []
    if lop:
        q += " AND lop = ?"; params.append(lop)
    if ten_gv:
        q += " AND ten_gv = ?"; params.append(ten_gv)
    if since:
        q += " AND ts >= ?"; params.append(since)
    q += " ORDER BY ts DESC LIMIT ?"; params.append(limit)
    with _connect() as conn:
        rows = conn.execute(q, params).fetchall()
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame([dict(r) for r in rows])


# ── Mã nhóm tự chọn đã gán cho lớp (chỉ để hiển thị) ──────────────────────────

def set_elective_group(lop: str, subject: str, ma_nhom: str) -> None:
    """Ghi nhớ lớp `lop` được gán vào mã nhóm `ma_nhom` cho môn `subject`.
    Mỗi (lop, subject) chỉ giữ 1 mã nhóm gần nhất (khớp với việc gán lại sẽ
    thay thế GV cũ trong assignments)."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO elective_group_map (lop, subject, ma_nhom) VALUES (?, ?, ?) "
            "ON CONFLICT(lop, subject) DO UPDATE SET ma_nhom = excluded.ma_nhom",
            (lop, subject, ma_nhom),
        )


def get_elective_group_map() -> pd.DataFrame:
    with _connect() as conn:
        rows = conn.execute("SELECT lop, subject, ma_nhom FROM elective_group_map").fetchall()
    if not rows:
        return pd.DataFrame(columns=["lop", "subject", "ma_nhom"])
    return pd.DataFrame([dict(r) for r in rows])


def remove_elective_group(subject: str, lop_list: list[str]) -> None:
    with _connect() as conn:
        conn.executemany(
            "DELETE FROM elective_group_map WHERE subject = ? AND lop = ?",
            [(subject, lop) for lop in lop_list],
        )


# ── Học sinh ───────────────────────────────────────────────────────────────

_STUDENT_COLS = ["ma_hs", "ten", "lop", "khoi", "gdtc_nhom", "gdtc_mon",
                 "thqt_nhom", "avbn_nhom", "stem_nhom",
                 "on_tn_1_nhom", "on_tn_1_mon", "on_tn_2_nhom", "on_tn_2_mon", "ghi_chu"]


def save_students(df: pd.DataFrame) -> int:
    """Lưu toàn bộ danh sách học sinh (xoá cũ, nhập mới). Trả về số hàng."""
    with _connect() as conn:
        conn.execute("DELETE FROM students")
        conn.executemany(
            f"INSERT INTO students ({', '.join(_STUDENT_COLS)}) "
            f"VALUES ({', '.join('?' for _ in _STUDENT_COLS)})",
            [tuple(str(r.get(c, "")) for c in _STUDENT_COLS) for _, r in df.iterrows()],
        )
    return len(df)


def get_group_periods() -> pd.DataFrame:
    """Lịch nhóm tự chọn theo từng tiết: ma_nhom, gv, thu, buoi, tiet, lop, so_hs.
    student_electives đánh số tiết 1–8 cả ngày (5–8 = chiều); Thứ 7 dùng tiết 1–4 sáng."""
    cols = ["ma_nhom", "gv", "thu", "buoi", "tiet", "lop", "so_hs"]
    with _connect() as conn:
        rows = conn.execute(
            "SELECT e.ma_nhom, e.gv, e.thu, e.tiet_dau, e.tiet_cuoi, s.lop, COUNT(*) AS so_hs "
            "FROM student_electives e JOIN students s ON s.ma_hs = e.ma_hs "
            "WHERE e.thu <> '' AND e.tiet_dau IS NOT NULL "
            "GROUP BY e.ma_nhom, e.gv, e.thu, e.tiet_dau, e.tiet_cuoi, s.lop"
        ).fetchall()
    out = []
    for r in rows:
        for t in range(int(r["tiet_dau"]), int(r["tiet_cuoi"] or r["tiet_dau"]) + 1):
            buoi, tiet = ("Chiều", t - 4) if t > 4 else ("Sáng", t)
            out.append((r["ma_nhom"], r["gv"], r["thu"], buoi, tiet, r["lop"], r["so_hs"]))
    return pd.DataFrame(out, columns=cols)


def get_students() -> pd.DataFrame:
    """Tải danh sách học sinh. Trả về DataFrame rỗng nếu chưa có."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(_STUDENT_COLS)} FROM students ORDER BY lop, ten"
        ).fetchall()
    if not rows:
        return pd.DataFrame(columns=_STUDENT_COLS)
    return pd.DataFrame([dict(r) for r in rows])


_STUDENT_ELECTIVE_COLS = ["ma_hs", "loai", "ma_nhom", "mon", "thu", "tiet_dau", "tiet_cuoi", "gv", "phong"]


def save_student_electives(df: pd.DataFrame) -> int:
    """Lưu toàn bộ lịch học nhóm tự chọn/ôn TN của HS (xoá cũ, nhập mới)."""
    with _connect() as conn:
        conn.execute("DELETE FROM student_electives")
        conn.executemany(
            f"INSERT INTO student_electives ({', '.join(_STUDENT_ELECTIVE_COLS)}) "
            f"VALUES ({', '.join('?' for _ in _STUDENT_ELECTIVE_COLS)})",
            [tuple(r.get(c) for c in _STUDENT_ELECTIVE_COLS) for _, r in df.iterrows()],
        )
    return len(df)


def get_student_electives(ma_hs: str | None = None) -> pd.DataFrame:
    """Tải lịch học nhóm tự chọn/ôn TN. Lọc theo ma_hs nếu có."""
    with _connect() as conn:
        if ma_hs:
            rows = conn.execute(
                f"SELECT {', '.join(_STUDENT_ELECTIVE_COLS)} FROM student_electives "
                f"WHERE ma_hs = ? ORDER BY loai",
                (ma_hs,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {', '.join(_STUDENT_ELECTIVE_COLS)} FROM student_electives ORDER BY ma_hs, loai"
            ).fetchall()
    if not rows:
        return pd.DataFrame(columns=_STUDENT_ELECTIVE_COLS)
    return pd.DataFrame([dict(r) for r in rows])


def clear_all_csv_data() -> None:
    """Xoá toàn bộ dữ liệu CSV đã lưu (dùng khi muốn reset)."""
    with _connect() as conn:
        conn.executescript("""
            DELETE FROM teachers;
            DELETE FROM classes;
            DELETE FROM assignments;
            DELETE FROM class_subjects;
            DELETE FROM homeroom;
            DELETE FROM timetable;
        """)
