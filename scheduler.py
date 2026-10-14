"""
scheduler.py — Timetable generator for THPT Nguyễn An Ninh.

Strategy  (3-phase "Beautiful TKB"):
  Phase 1 — Greedy:   Fast slot assignment with teacher-compaction scoring.
  Phase 2 — GRASP:    Repeat Phase 1 N times, keep the BEST by quality score.
  Phase 3 — CP-SAT:   Exact optimisation from the GRASP hint (optional).

Quality scoring (lower = better):
  • teacher_sessions : total (day, buổi) half-days used across all teachers
  • teacher_gaps     : empty slots between first/last lesson in the same half-day
  • teacher_overload : lessons/day exceeding max_per_day
  • same_day_violations : same subject twice on the same day for a class

Hard constraints (all phases):
  • No class double-booking
  • No teacher double-booking
  • Teacher unavailability
  • Same subject on different days (relaxed only when necessary)
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd
from ortools.sat.python import cp_model

import database

logger = logging.getLogger(__name__)

# ── Time structure ─────────────────────────────────────────────────────────────

DAYS_ORDER = ["Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6"]
N_DAYS  = 5
N_SANG  = 4   # Tiết 1–4 sáng (không có tiết 5)
N_CHIEU = 4   # Tiết 1–4 chiều
N_SLOTS = N_DAYS * (N_SANG + N_CHIEU)  # 40
_SANG_END = N_DAYS * N_SANG

SLOT_INFO: list[tuple[str, str, int]] = []
SLOT_DAY:  list[int]                  = []
for _s in range(N_SLOTS):
    if _s < _SANG_END:
        _d, _p = divmod(_s, N_SANG)
        SLOT_INFO.append((DAYS_ORDER[_d], "Sáng", _p + 1))
    else:
        _d, _p = divmod(_s - _SANG_END, N_CHIEU)
        SLOT_INFO.append((DAYS_ORDER[_d], "Chiều", _p + 1))
    SLOT_DAY.append(_d)

def day_slots(day_idx: int) -> list[int]:
    sang  = [day_idx * N_SANG + p for p in range(N_SANG)]
    chieu = [_SANG_END + day_idx * N_CHIEU + p for p in range(N_CHIEU)]
    return sang + chieu

def info_to_slot(day: str, buoi: str, period: int) -> int | None:
    if day not in DAYS_ORDER:
        return None
    d = DAYS_ORDER.index(day)
    if buoi == "Sáng" and 1 <= period <= N_SANG:
        return d * N_SANG + (period - 1)
    if buoi == "Chiều" and 1 <= period <= N_CHIEU:
        return _SANG_END + d * N_CHIEU + (period - 1)
    return None


# ── Subject mapping ───────────────────────────────────────────────────────────

SUBJ_COL_TO_KEY: dict[str, str] = {
    "VĂN":        "VĂN",
    "TOÁN":       "TOÁN",
    "ANH":        "ANH",
    "SỬ":         "SỬ",
    "THỂ DỤC":    "THỂ DỤC",
    "QUỐC PHÒNG": "GDQP",
    "TN-HN":      "TNHN",
    "GDĐP":       "GDĐP",
    "LÝ":         "LÝ",
    "HOÁ":        "HÓA",
    "SINH":       "SINH",
    "ĐỊA":        "ĐỊA",
    "KTPL":       "KT&PL",
    "TIN":        "TIN HỌC",
    "CÔNG NGHỆ":  "C.NGHỆ",
}
_SKIP_SUBJECTS = {"THỂ DỤC", "GDĐP"}


# ── Parsing & loading ─────────────────────────────────────────────────────────

def parse_assignments(path: str) -> pd.DataFrame:
    rows = []
    for sheet in ["K10", "K11", "K12"]:
        df = pd.read_excel(path, sheet_name=sheet, header=None)
        headers = [str(h).strip() if pd.notna(h) else "" for h in df.iloc[0]]
        grade = sheet[1:]
        for _, row in df.iloc[1:].iterrows():
            cls = str(row.iloc[0]).strip()
            if not cls or cls in ("nan", ""):
                continue
            for col_idx, col_name in enumerate(headers[1:16], start=1):
                subj_key = SUBJ_COL_TO_KEY.get(col_name)
                if subj_key is None or subj_key in _SKIP_SUBJECTS:
                    continue
                val = row.iloc[col_idx]
                if pd.notna(val) and str(val).strip() not in ("", "nan"):
                    rows.append({
                        "grade":        grade,
                        "cls":          cls,
                        "subject_col":  col_name,
                        "subject_key":  subj_key,
                        "teacher_code": str(val).strip(),
                    })
    return pd.DataFrame(rows)


def load_teacher_map(xls_path: str) -> dict[str, str]:
    df = pd.read_excel(xls_path, sheet_name="DanhSach", header=None, engine="xlrd")
    mapping: dict[str, str] = {}
    for _, r in df.iloc[3:].iterrows():
        ma  = r.iloc[2]
        ten = r.iloc[3]
        if pd.notna(ma) and str(ma).strip():
            mapping[str(ma).strip()] = str(ten).strip() if pd.notna(ten) else str(ma).strip()
    return mapping


@dataclass(frozen=True)
class Lesson:
    id:            int
    cls:           str
    grade:         str
    subject:       str
    teacher_codes: tuple[str, ...]   # (code,) thường; (code1, code2) team-teaching AVBN
    teacher_names: tuple[str, ...]   # (name,) thường; (name1, name2) team-teaching
    occurrence:    int

    @property
    def teacher_code(self) -> str:
        """GV chính (backward compat với XLSX pipeline)."""
        return self.teacher_codes[0] if self.teacher_codes else ""

    @property
    def teacher_name(self) -> str:
        """Tên GV chính (backward compat)."""
        return self.teacher_names[0] if self.teacher_names else ""


def build_lessons(
    assignments: pd.DataFrame,
    teacher_map: dict[str, str],
    curriculum:  dict[tuple[str, str], int],
) -> list[Lesson]:
    lessons: list[Lesson] = []
    lid = 0
    for _, row in assignments.iterrows():
        ppw = curriculum.get((row["grade"], row["subject_key"]), 0)
        ppw -= _FIXED_PERIODS.get(row["subject_key"], 0)   # trừ tiết cố định (TNHN)
        if ppw <= 0:
            continue
        code = row["teacher_code"]
        for occ in range(int(ppw)):
            lessons.append(Lesson(
                id=lid, cls=row["cls"], grade=row["grade"],
                subject=row["subject_key"],
                teacher_codes=(code,),
                teacher_names=(teacher_map.get(code, code),),
                occurrence=occ,
            ))
            lid += 1
    return lessons


def load_curriculum_map() -> dict[tuple[str, str], int]:
    result = database.get_all_curriculum()
    return {
        (grade, subj["subject"]): subj["periods_per_week"]
        for grade, subjects in result.items()
        for subj in subjects
    }

def load_unavailable_map(teacher_map: dict[str, str]) -> dict[str, set[int]]:
    unavail: dict[str, set[int]] = {}
    for ps in database.get_all_teacher_prefs():
        full = database.get_teacher_pref(ps["ma_so"])
        name = teacher_map.get(ps["ma_so"], ps["ma_so"])
        busy: set[int] = set()
        for s in full.get("unavailable_slots", []):
            slot = info_to_slot(s["day"], s["buoi"], s["period"])
            if slot is not None:
                busy.add(slot)
        if busy:
            unavail[name] = busy
    return unavail

def merge_dept_meeting_unavailable(
    unavail: dict[str, set[int]], teachers_df: pd.DataFrame | None,
) -> int:
    """Thêm sáng ngày SHCM của tổ (theo teachers.mon_day) vào lịch bận từng GV,
    trừ GV trong bảng shcm_exempt (đồng thời nạp SHCM_EXEMPT_TEACHERS)."""
    if teachers_df is None or "mon_day" not in teachers_df.columns:
        return 0
    exempt_codes = database.get_shcm_exempt()
    SHCM_EXEMPT_TEACHERS.clear()
    SHCM_EXEMPT_TEACHERS.update(
        str(t["ten"]) for _, t in teachers_df.iterrows() if str(t["ma_so"]) in exempt_codes
    )
    n = 0
    for _, t in teachers_df.iterrows():
        if str(t["ma_so"]) in exempt_codes:
            continue
        slots = {
            info_to_slot(s["day"], s["buoi"], s["period"])
            for s in database.dept_meeting_slots(t["mon_day"])
        }
        slots.discard(None)
        if slots:
            unavail.setdefault(str(t["ten"]), set()).update(slots)
            n += 1
    return n


def load_max_per_day_map(teacher_map: dict[str, str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for ps in database.get_all_teacher_prefs():
        full = database.get_teacher_pref(ps["ma_so"])
        name = teacher_map.get(ps["ma_so"], ps["ma_so"])
        result[name] = full.get("max_periods_per_day", 4)
    return result


# Thứ 2 Tiết 1 Sáng dành riêng cho Chào cờ — không xếp tiết học.
# Tiết 2 KHÔNG còn cố định là TNHN — xếp môn khác vào bình thường như mọi tiết khác.
FIXED_CLASS_SLOTS: dict[int, str] = {
    0: "Chào cờ",  # Thứ 2, Tiết 1, Sáng
}

# Số tiết cố định mỗi môn (trừ Chào cờ) → trừ khi tính periods_per_week
_FIXED_PERIODS: dict[str, int] = {}
for _fs, _fl in FIXED_CLASS_SLOTS.items():
    if _fl != "Chào cờ":
        _FIXED_PERIODS[_fl] = _FIXED_PERIODS.get(_fl, 0) + 1
# → {"TNHN": 1}  — TNHN có 1 tiết cố định, chỉ xếp thêm (ppw - 1) tiết

# Tất cả cặp slot liên tiếp hợp lệ (cùng ngày, cùng buổi, kề nhau)
CONSECUTIVE_PAIRS: list[tuple[int, int]] = [
    (s, s + 1)
    for s in range(N_SLOTS - 1)
    if (SLOT_INFO[s][0] == SLOT_INFO[s + 1][0]      # cùng ngày
        and SLOT_INFO[s][1] == SLOT_INFO[s + 1][1])  # cùng buổi
]

# Set để tra cứu O(1) trong CP-SAT
_CONSECUTIVE_SET: set[tuple[int, int]] = set(CONSECUTIVE_PAIRS)

# Môn buộc phải có ≥1 tiết đôi mỗi tuần dù có 3 tiết (các môn khác vẫn xếp
# tiết lẻ bình thường)
FORCED_DOUBLE_SUBJECTS: set[str] = {"VĂN", "TOÁN", "ANH", "TIN"}

# Môn chỉ được xếp buổi CHIỀU (không xếp sáng) — gồm các nhóm tự chọn
# (CT: AVBN/STEM/THQT), thể thao tự chọn (TC), và ôn thi tốt nghiệp khối 12
# (TN — dùng tên đầy đủ để không trùng với môn chính khoá viết tắt, vd
# "TIẾNG ANH" khác "ANH"). So sánh dùng .upper() nên khai báo sẵn viết hoa.
AFTERNOON_ONLY_SUBJECTS: set[str] = {
    "THQT", "AVBN", "STEM", "GDTC",
    "BÓNG CHUYỀN", "BÓNG ĐÁ", "BÓNG RỔ", "CẦU LÔNG",
    "TIẾNG ANH", "ĐỊA LÍ", "HÓA HỌC", "GD KT&PL", "VẬT LÍ",
    "SINH HỌC", "LỊCH SỬ", "TIN HỌC",
}

# Môn liên lớp (1 GV dạy chung HS của rất nhiều lớp cùng lúc — GDTC/THQT/
# AVBN/STEM) — bộ sinh TKB tự động (Greedy/CP-SAT) coi mỗi (lớp, môn) là
# 1 tiết cần đúng 1 GV rảnh riêng cho lớp đó, nên nếu đưa các môn này vào,
# 1 GV sẽ bị hiểu nhầm là phải "dạy" hàng chục lớp khác nhau cùng lúc
# (vượt xa 40 tiết/tuần khả dụng) → solver luôn thất bại. Các môn này phải
# được xếp trực tiếp/thủ công vào từng lớp (xem tab "Học sinh"/"Xếp TKB thủ
# công"), không đưa vào generate_timetable_from_dfs.
CROSS_CLASS_SUBJECTS: set[str] = {"GDTC", "THQT", "AVBN", "STEM"}

# Môn được PHÉP phá tiết đôi (đã xác nhận với người dùng) như phương án
# cuối cùng để lấp ô sáng còn trống — chỉ dùng ở fill_morning_gaps_swap
# sau khi cách xếp an toàn (không phá tiết đôi) không lấp được hết.
# TIN: phá đôi nghĩa là nửa còn lại (đang ở chiều, vì movable_ids chỉ xét
# tiết đang ở chiều) bị bỏ lại MỘT MÌNH ở chiều — người dùng đã xác nhận
# đánh đổi này là CHẤP NHẬN ĐƯỢC, đổi lại GV Tin được dạy đủ 4 tiết sáng.
BREAKABLE_DOUBLE_LAST_RESORT: frozenset[str] = frozenset({"ANH", "TIN"})

# Môn không được xếp vào sáng Thứ 2 (GDQP-AN và Thể dục — HS cần trạng thái
# sẵn sàng thể lực, sáng Thứ 2 đã có Chào cờ nên không phù hợp xếp thêm)
MONDAY_MORNING_BLOCKED_SUBJECTS: set[str] = {"QPAN", "THỂ DỤC", "GDTC"}
MONDAY_MORNING_SLOTS: set[int] = set(range(N_SANG))  # Thứ 2 = ngày đầu tiên trong layout theo ngày

# "Ngày sinh hoạt chuyên môn" — mỗi tổ bộ môn họp vào 1 buổi sáng cố định
# trong tuần, nên GV thuộc tổ đó không được xếp tiết dạy buổi sáng đúng
# ngày đó (áp dụng cho mọi lớp, kể cả tiết "CĐ ..." cùng tổ). Khai báo theo
# đúng tên môn viết tắt dùng trong class_subjects/assignments.
DEPT_MEETING_DAY: dict[str, str] = {
    "QPAN": "Thứ 3", "TOÁN": "Thứ 3", "SỬ": "Thứ 3", "ĐỊA": "Thứ 3",
    "CĐ TOÁN": "Thứ 3", "CĐ SỬ": "Thứ 3", "CĐ ĐỊA": "Thứ 3",
    "VĂN": "Thứ 4", "LÝ": "Thứ 4", "CN": "Thứ 4", "HÓA": "Thứ 4",
    "CĐ VĂN": "Thứ 4", "CĐ LÝ": "Thứ 4", "CĐ HÓA": "Thứ 4",
    "KTPL": "Thứ 5", "CĐ KTPL": "Thứ 5",
    "ANH": "Thứ 6", "SINH": "Thứ 6", "CĐ SINH": "Thứ 6",
}


def _day_morning_slots(day: str) -> frozenset[int]:
    d = DAYS_ORDER.index(day)
    return frozenset(range(d * N_SANG, d * N_SANG + N_SANG))


# subject (viết hoa) -> tập slot buổi sáng ngày họp tổ của môn đó, bị chặn.
DEPT_MEETING_BLOCKED_SLOTS: dict[str, frozenset[int]] = {
    subj: _day_morning_slots(day) for subj, day in DEPT_MEETING_DAY.items()
}

# Tên GV được miễn chặn sáng SHCM (bảng shcm_exempt) — nạp lại mỗi lần sinh TKB.
SHCM_EXEMPT_TEACHERS: set[str] = set()


def _dept_blocked(l: "Lesson") -> frozenset[int]:
    if l.teacher_names and all(tn in SHCM_EXEMPT_TEACHERS for tn in l.teacher_names):
        return frozenset()
    return DEPT_MEETING_BLOCKED_SLOTS.get(l.subject.upper(), frozenset())

# Tiết đôi (VĂN/TOÁN, hoặc môn có đúng 2 tiết/tuần) nên ưu tiên xếp vào
# Tiết 2–3 buổi sáng khi có thể chọn (đây chỉ là tiêu chí ưu tiên/tie-break,
# không phá vỡ các ràng buộc dồn gọn/tránh buổi 2 tiết đã có).
PREFERRED_DOUBLE_PERIOD = 2  # tiết bắt đầu của cặp tiết đôi ưu tiên (2 → chiếm tiết 2 và 3)

# Riêng môn TIN: ưu tiên tiết đôi buổi sáng ở Tiết 1-2 HOẶC Tiết 3-4 (không
# phải 2-3 như các môn khác) — theo yêu cầu người dùng, để tiết đôi Tin nằm
# sát đầu hoặc cuối buổi sáng, dễ ghép thêm tiết lẻ phía còn lại.
TIN_PREFERRED_START_PERIODS: frozenset[int] = frozenset({1, 3})


# ── Phase 1: Greedy Scheduler ─────────────────────────────────────────────────
#
# Slot-selection priority:
#   1. Filter: hard constraints (class/teacher no-clash, unavailability)
#   2. Prefer: full constraints (day-spread, max_per_day)
#   3. Rank: teacher-compaction score (fewer sessions, fewer gaps, adjacent slots)
#   4. Tiebreak: balance morning slots across days for the class
#
# The key insight for "beautiful" teacher TKBs: instead of random choice among
# valid slots, we SCORE each candidate slot by how much it improves the teacher's
# compactness. This makes every single assignment locally optimal for teachers.

def _slot_session(s: int) -> tuple[int, str]:
    """Trả về (day_idx, buoi) cho slot s."""
    return (SLOT_DAY[s], "Sáng" if s < _SANG_END else "Chiều")


def _slot_compaction_score(
    s: int,
    teacher_names: tuple[str, ...],
    teacher_used: dict[str, set[int]],
    teacher_session_used: dict[str, set[tuple[int, str]]],
) -> tuple[int, int, int]:
    """
    Score a candidate slot for teacher compaction (lower = better).

    Returns (new_session, gap_cost, isolation_cost):
      new_session:    0 if GV already teaches in this (day,buoi), else 1
      gap_cost:       number of empty slots between existing lessons + this one
                      within the same (day,buoi)
      isolation_cost: 0 if GV already teaches on this day (either buổi), else 1
    """
    sess = _slot_session(s)
    day = SLOT_DAY[s]

    new_session = 0
    gap_cost = 0
    isolation_cost = 1

    for tn in teacher_names:
        sessions = teacher_session_used.get(tn, set())
        used = teacher_used.get(tn, set())

        # Đã dạy ngày này chưa?
        if any(SLOT_DAY[u] == day for u in used):
            isolation_cost = 0

        # Đã dạy buổi này chưa?
        if sess not in sessions:
            new_session = max(new_session, 1)
        else:
            # Tính gap: lấy tất cả slot GV đã dùng trong cùng buổi + slot mới
            same_session = [u for u in used if _slot_session(u) == sess]
            all_slots = sorted(same_session + [s])
            if len(all_slots) >= 2:
                gap_cost = max(gap_cost, (all_slots[-1] - all_slots[0]) - (len(all_slots) - 1))

    return (new_session, gap_cost, isolation_cost)


def _cls_afternoon_gap_cost(s: int, cls: str, cls_used: dict[str, set[int]]) -> int:
    """
    Phạt khi xếp tiết chiều của LỚP (không phải GV) tạo lỗ hổng đầu buổi —
    vd lớp đã có tiết 7-8 nhưng tiết 5-6 còn trống thì HS phải chờ không
    việc gì làm. Tính từ Tiết 1 CHIỀU (mốc neo cố định, không phải từ tiết
    sớm nhất lớp đã có) để chính slot `s` cũng bị phạt nếu nó tự tạo lỗ so
    với đầu buổi, không chỉ so với các tiết đã xếp trước đó. 0 nếu `s` ở
    buổi sáng (hàm này chỉ áp dụng cho buổi chiều).
    """
    if s < _SANG_END:
        return 0
    day = SLOT_DAY[s]
    chieu_start = _SANG_END + day * N_CHIEU
    used = cls_used.get(cls, set())
    same_day_chieu = [u for u in used if u >= _SANG_END and SLOT_DAY[u] == day]
    all_slots = sorted(same_day_chieu + [s])
    span = (all_slots[-1] - chieu_start) + 1
    return span - len(all_slots)


def _score_and_pick(
    pool: list[int],
    teacher_names: tuple[str, ...],
    cls: str,
    cls_used: dict[str, set[int]],
    teacher_used: dict[str, set[int]],
    teacher_session_used: dict[str, set[tuple[int, str]]],
    rng: random.Random,
) -> int:
    """
    Chọn slot tốt nhất từ pool cho teacher compaction.
    Ưu tiên (theo thứ tự): không phá session mới của GV → không tạo lỗ
    hổng đầu buổi chiều cho LỚP → các tiêu chí compaction còn lại của GV →
    tiebreak phân bổ đều tiết sáng theo ngày cho lớp.
    """
    if len(pool) == 1:
        return pool[0]

    # Score mỗi slot
    scored: list[tuple[tuple, int, int]] = []
    used = cls_used.get(cls, set())
    day_cnt: dict[int, int] = {}
    for u in used:
        if u < _SANG_END:
            day_cnt[SLOT_DAY[u]] = day_cnt.get(SLOT_DAY[u], 0) + 1

    for s in pool:
        new_session, gap_cost, isolation_cost = _slot_compaction_score(
            s, teacher_names, teacher_used, teacher_session_used
        )
        cls_gap = _cls_afternoon_gap_cost(s, cls, cls_used)
        compact = (new_session, cls_gap, gap_cost, isolation_cost)
        cls_day_load = day_cnt.get(SLOT_DAY[s], 0)
        scored.append((compact, cls_day_load, s))

    scored.sort(key=lambda x: (x[0], x[1]))
    best_key = (scored[0][0], scored[0][1])
    ties = [s for (k, c, s) in scored if (k, c) == best_key]
    return rng.choice(ties)


def _assign_single(
    l: "Lesson",
    assignment: dict[int, int],
    cls_used: dict[str, set[int]],
    teacher_used: dict[str, set[int]],
    cls_subj_days: dict[tuple[str, str], set[int]],
    teacher_day_cnt: dict[tuple[str, int], int],
    teacher_session_used: dict[str, set[tuple[int, str]]],
    unavailable: dict[str, set[int]],
    max_per_day: dict[str, int],
    rng: random.Random,
) -> bool:
    """
    Xếp 1 tiết đơn.
    Mặc định: SÁNG full → CHIỀU full → SÁNG relaxed → CHIỀU relaxed.
    Môn AFTERNOON_ONLY (THQT, AVBN): CHIỀU full → CHIỀU relaxed (không sáng).
    Môn MONDAY_MORNING_BLOCKED (QPAN, THỂ DỤC): loại hẳn slot sáng Thứ 2 khỏi pool.
    Chọn slot bằng compaction scoring thay vì random.
    """
    busy        = set().union(*(unavailable.get(tn, set()) for tn in l.teacher_names))
    mpd         = min(max_per_day.get(tn, 4) for tn in l.teacher_names)
    days_spread = cls_subj_days[(l.cls, l.subject)]
    is_afternoon    = l.subject.upper() in AFTERNOON_ONLY_SUBJECTS
    blocked_monday  = l.subject.upper() in MONDAY_MORNING_BLOCKED_SUBJECTS
    dept_blocked    = _dept_blocked(l)

    # Full constraints (day-spread + mpd)
    full = [
        s for s in range(N_SLOTS)
        if s not in cls_used[l.cls]
        and all(s not in teacher_used[tn] for tn in l.teacher_names)
        and s not in busy
        and SLOT_DAY[s] not in days_spread
        and all(teacher_day_cnt.get((tn, SLOT_DAY[s]), 0) < mpd for tn in l.teacher_names)
    ]
    # Mid: bỏ day-spread, vẫn giữ mpd (không ép GV dạy vượt số tiết/ngày tối đa
    # chỉ để giữ tiết ở buổi sáng — mpd bảo vệ GV nên ưu tiên giữ hơn day-spread)
    mid = [
        s for s in range(N_SLOTS)
        if s not in cls_used[l.cls]
        and all(s not in teacher_used[tn] for tn in l.teacher_names)
        and s not in busy
        and all(teacher_day_cnt.get((tn, SLOT_DAY[s]), 0) < mpd for tn in l.teacher_names)
    ]
    # Relaxed (chỉ giữ hard: class/teacher no-clash) — phương án cuối cùng
    relaxed = [
        s for s in range(N_SLOTS)
        if s not in cls_used[l.cls]
        and all(s not in teacher_used[tn] for tn in l.teacher_names)
        and s not in busy
    ]

    full_m    = [s for s in full    if s < _SANG_END]
    full_c    = [s for s in full    if s >= _SANG_END]
    mid_m     = [s for s in mid     if s < _SANG_END]
    mid_c     = [s for s in mid     if s >= _SANG_END]
    relaxed_m = [s for s in relaxed if s < _SANG_END]
    relaxed_c = [s for s in relaxed if s >= _SANG_END]

    if blocked_monday:
        full_m    = [s for s in full_m    if s not in MONDAY_MORNING_SLOTS]
        mid_m     = [s for s in mid_m     if s not in MONDAY_MORNING_SLOTS]
        relaxed_m = [s for s in relaxed_m if s not in MONDAY_MORNING_SLOTS]
    if dept_blocked:
        full_m    = [s for s in full_m    if s not in dept_blocked]
        mid_m     = [s for s in mid_m     if s not in dept_blocked]
        relaxed_m = [s for s in relaxed_m if s not in dept_blocked]

    if is_afternoon:
        pool = full_c or mid_c or relaxed_c
    else:
        # Buổi sáng là CHÍNH KHOÁ — HS phải học đủ cả 4 tiết sáng mọi ngày,
        # nên lấp sáng được ưu tiên TUYỆT ĐỐI: thử hết mọi cách để giữ tiết
        # ở sáng (kể cả nới day-spread lẫn mpd) TRƯỚC KHI chuyển sang chiều.
        # Chỉ dùng chiều khi lớp thật sự không còn ô sáng nào — dù đã nới
        # hết các ràng buộc mềm — do xung đột cứng thật sự (GV đã dạy lớp
        # khác/bận đúng những tiết sáng còn lại).
        pool = full_m or mid_m or relaxed_m or full_c or mid_c or relaxed_c
    if not pool:
        return False

    s = _score_and_pick(pool, l.teacher_names, l.cls, cls_used,
                        teacher_used, teacher_session_used, rng)
    assignment[l.id] = s
    cls_used[l.cls].add(s)
    for tn in l.teacher_names:
        teacher_used[tn].add(s)
        teacher_session_used.setdefault(tn, set()).add(_slot_session(s))
    cls_subj_days[(l.cls, l.subject)].add(SLOT_DAY[s])
    for tn in l.teacher_names:
        teacher_day_cnt[(tn, SLOT_DAY[s])] = teacher_day_cnt.get((tn, SLOT_DAY[s]), 0) + 1
    return True


def _score_pair(
    s1: int, s2: int,
    teacher_names: tuple[str, ...],
    teacher_used: dict[str, set[int]],
    teacher_session_used: dict[str, set[tuple[int, str]]],
    cls: str | None = None,
    cls_used: dict[str, set[int]] | None = None,
    preferred_start_periods: frozenset[int] = frozenset({PREFERRED_DOUBLE_PERIOD}),
) -> tuple[int, int, int, int, int]:
    """Score a double-pair candidate: (new_session, cls_gap, gap_cost, isolation,
    position_pref). cls_gap (như _cls_afternoon_gap_cost, xem đó) chỉ được tính
    khi `cls`/`cls_used` được truyền vào — tránh lỗ hổng đầu buổi chiều cho LỚP
    khi tiết đôi (vd Anh/Tin) được xếp vào Tiết 3-4 chiều trong khi Tiết 1-2
    chiều còn trống. position_pref: 0 nếu tiết bắt đầu của cặp nằm trong
    `preferred_start_periods` buổi sáng (mặc định chỉ Tiết 2 — chiếm 2-3; môn
    TIN truyền riêng {1, 3} để ưu tiên 1-2 hoặc 3-4 theo yêu cầu người dùng),
    1 nếu không — chỉ dùng làm tiêu chí tie-break SAU compaction,
    không ghi đè dồn gọn."""
    sess = _slot_session(s1)
    day = SLOT_DAY[s1]
    new_session = 0
    isolation_cost = 1

    for tn in teacher_names:
        sessions = teacher_session_used.get(tn, set())
        used = teacher_used.get(tn, set())
        if any(SLOT_DAY[u] == day for u in used):
            isolation_cost = 0
        if sess not in sessions:
            new_session = max(new_session, 1)

    # Gap cost: tiết đôi liền → gap = 0 luôn nếu không có tiết cũ
    gap_cost = 0
    for tn in teacher_names:
        used = teacher_used.get(tn, set())
        same_session = [u for u in used if _slot_session(u) == sess]
        all_slots = sorted(same_session + [s1, s2])
        if len(all_slots) >= 2:
            gap_cost = max(gap_cost, (all_slots[-1] - all_slots[0]) - (len(all_slots) - 1))

    cls_gap = 0
    if cls is not None and cls_used is not None and s1 >= _SANG_END:
        chieu_start = _SANG_END + day * N_CHIEU
        used_cls = cls_used.get(cls, set())
        same_day_chieu = [u for u in used_cls if u >= _SANG_END and SLOT_DAY[u] == day]
        all_cls_slots = sorted(same_day_chieu + [s1, s2])
        cls_gap = (all_cls_slots[-1] - chieu_start) + 1 - len(all_cls_slots)

    period1 = SLOT_INFO[s1][2]
    position_pref = 0 if (sess[1] == "Sáng" and period1 in preferred_start_periods) else 1

    return (new_session, cls_gap, gap_cost, isolation_cost, position_pref)


def _greedy_once(
    lessons:     list[Lesson],
    unavailable: dict[str, set[int]],
    max_per_day: dict[str, int],
    rng:         random.Random,
    forced_double_subjects: set[str] = FORCED_DOUBLE_SUBJECTS,
    fixed_teacher_slots: dict[str, set[int]] | None = None,
) -> dict[int, int] | None:
    """Single greedy pass — scored slot selection for teacher compaction.
    `forced_double_subjects` cho phép 1 lần gọi cụ thể ghi đè tập môn bắt
    buộc tiết đôi (mặc định dùng FORCED_DOUBLE_SUBJECTS toàn cục) — dùng khi
    người dùng muốn "xé lẻ" thêm vài môn (vd Tin) cho riêng 1 lần xếp mà
    không đổi hẳn chính sách chung của trường."""
    cls_used:             dict[str, set[int]]                = {}
    teacher_used:         dict[str, set[int]]                = {}
    cls_subj_days:        dict[tuple[str, str], set[int]]    = {}
    teacher_day_cnt:      dict[tuple[str, int], int]         = {}
    teacher_session_used: dict[str, set[tuple[int, str]]]    = {}

    fixed_slots = set(FIXED_CLASS_SLOTS.keys())
    for l in lessons:
        cls_used.setdefault(l.cls, set(fixed_slots))
        for tn in l.teacher_names:
            teacher_used.setdefault(tn, set())
        cls_subj_days.setdefault((l.cls, l.subject), set())
    for tn, slots in (fixed_teacher_slots or {}).items():
        if tn not in teacher_used:
            continue
        teacher_used[tn] |= slots
        for fs in slots:
            teacher_session_used.setdefault(tn, set()).add(_slot_session(fs))
            teacher_day_cnt[(tn, SLOT_DAY[fs])] = teacher_day_cnt.get((tn, SLOT_DAY[fs]), 0) + 1

    # Phân loại: cặp đôi vs đơn
    subj_groups: dict[tuple[str, str], list[Lesson]] = {}
    for l in lessons:
        subj_groups.setdefault((l.cls, l.subject), []).append(l)

    # Chỉ ghép tiết đôi cho môn thuộc FORCED_DOUBLE_SUBJECTS (Toán/Văn/Anh/Tin)
    # — môn khác dù đúng 2 tiết/tuần (Sử, Địa, KTPL, Sinh, ...) vẫn xếp 2 tiết
    # lẻ độc lập, không ghép liền nhau.
    double_pairs: list[tuple[Lesson, Lesson]] = []
    single_lessons: list[Lesson] = []
    for group in subj_groups.values():
        subj = group[0].subject
        if len(group) >= 2 and subj in forced_double_subjects:
            double_pairs.append((group[0], group[1]))
            single_lessons.extend(group[2:])
        else:
            single_lessons.extend(group)

    # Sort: GV chỉ dạy được 1 buổi (nghỉ hết buổi kia) → xếp TRƯỚC TIÊN, vì
    # slot của họ khan hiếm và không có phương án dự phòng (không thể đẩy
    # sang buổi còn lại như các tiết khác) — nếu để tới lượt sau, buổi sáng
    # của đúng lớp đó có thể đã bị các môn khác (không bị giới hạn buổi)
    # chiếm hết, gây fail oan trong khi tổng số tiết của GV đó vẫn còn dư
    # chỗ. Sau đó mới đến AFTERNOON_ONLY, rồi các môn còn lại.
    def _confined_buoi(teacher_names: tuple[str, ...]) -> bool:
        busy = set().union(*(unavailable.get(tn, set()) for tn in teacher_names))
        return busy.issuperset(range(_SANG_END, N_SLOTS)) or busy.issuperset(range(_SANG_END))

    def _afternoon_first(pair_or_lesson):
        lesson0 = pair_or_lesson[0] if isinstance(pair_or_lesson, tuple) else pair_or_lesson
        subj = lesson0.subject
        if _confined_buoi(lesson0.teacher_names):
            tier = 0
        elif subj.upper() in AFTERNOON_ONLY_SUBJECTS:
            tier = 1
        else:
            tier = 2
        return (tier, rng.random())

    double_pairs.sort(key=_afternoon_first)
    single_lessons.sort(key=_afternoon_first)

    assignment: dict[int, int] = {}

    # ── Tiết đôi ─────────────────────────────────────────────────────────
    for l1, l2 in double_pairs:
        busy = set().union(*(unavailable.get(tn, set()) for tn in l1.teacher_names))
        mpd  = min(max_per_day.get(tn, 4) for tn in l1.teacher_names)
        blocked_monday = l1.subject.upper() in MONDAY_MORNING_BLOCKED_SUBJECTS
        dept_blocked   = _dept_blocked(l1)

        def _pair_ok(s1, s2, need_day_spread, need_mpd):
            if s1 in cls_used[l1.cls] or s2 in cls_used[l1.cls]:
                return False
            if any(s1 in teacher_used[tn] or s2 in teacher_used[tn] for tn in l1.teacher_names):
                return False
            if s1 in busy or s2 in busy:
                return False
            if blocked_monday and s1 in MONDAY_MORNING_SLOTS:
                return False
            if dept_blocked and (s1 in dept_blocked or s2 in dept_blocked):
                return False
            if need_day_spread and SLOT_DAY[s1] in cls_subj_days[(l1.cls, l1.subject)]:
                return False
            if need_mpd and any(teacher_day_cnt.get((tn, SLOT_DAY[s1]), 0) + 2 > mpd
                                 for tn in l1.teacher_names):
                return False
            return True

        full_candidates    = [(s1, s2) for s1, s2 in CONSECUTIVE_PAIRS if _pair_ok(s1, s2, True, True)]
        # Mid: bỏ day-spread, vẫn giữ mpd (ưu tiên giữ giới hạn tiết/ngày của GV hơn)
        mid_candidates     = [(s1, s2) for s1, s2 in CONSECUTIVE_PAIRS if _pair_ok(s1, s2, False, True)]
        relaxed_candidates = [(s1, s2) for s1, s2 in CONSECUTIVE_PAIRS if _pair_ok(s1, s2, False, False)]
        if not relaxed_candidates:
            single_lessons.extend([l1, l2])
            continue

        is_afternoon = l1.subject.upper() in AFTERNOON_ONLY_SUBJECTS
        if is_afternoon:
            full_c    = [(s1, s2) for s1, s2 in full_candidates    if s1 >= _SANG_END]
            mid_c     = [(s1, s2) for s1, s2 in mid_candidates     if s1 >= _SANG_END]
            relaxed_c = [(s1, s2) for s1, s2 in relaxed_candidates if s1 >= _SANG_END]
            pool = full_c or mid_c or relaxed_c or full_candidates or mid_candidates or relaxed_candidates
        else:
            # Buổi sáng là CHÍNH KHOÁ — ưu tiên TUYỆT ĐỐI lấp đầy sáng trước
            # khi dùng chiều (giống _assign_single): thử hết mọi cách để giữ
            # cặp tiết đôi ở sáng (kể cả nới day-spread lẫn mpd) trước khi
            # chuyển sang chiều.
            full_m    = [(s1, s2) for s1, s2 in full_candidates    if s1 < _SANG_END]
            mid_m     = [(s1, s2) for s1, s2 in mid_candidates     if s1 < _SANG_END]
            relaxed_m = [(s1, s2) for s1, s2 in relaxed_candidates if s1 < _SANG_END]
            full_c    = [(s1, s2) for s1, s2 in full_candidates    if s1 >= _SANG_END]
            mid_c     = [(s1, s2) for s1, s2 in mid_candidates     if s1 >= _SANG_END]
            relaxed_c = [(s1, s2) for s1, s2 in relaxed_candidates if s1 >= _SANG_END]
            pool = full_m or mid_m or relaxed_m or full_c or mid_c or relaxed_c

        # Score each pair for compaction
        pref_periods = (
            TIN_PREFERRED_START_PERIODS if l1.subject.upper() == "TIN"
            else frozenset({PREFERRED_DOUBLE_PERIOD})
        )
        scored = []
        for s1, s2 in pool:
            sc = _score_pair(s1, s2, l1.teacher_names,
                             teacher_used, teacher_session_used,
                             cls=l1.cls, cls_used=cls_used,
                             preferred_start_periods=pref_periods)
            scored.append((sc, s1, s2))
        scored.sort(key=lambda x: x[0])
        best_key = scored[0][0]
        ties = [(s1, s2) for (k, s1, s2) in scored if k == best_key]
        s1, s2 = rng.choice(ties)

        assignment[l1.id] = s1
        assignment[l2.id] = s2
        for s in (s1, s2):
            cls_used[l1.cls].add(s)
            for tn in l1.teacher_names:
                teacher_used[tn].add(s)
                teacher_session_used.setdefault(tn, set()).add(_slot_session(s))
        cls_subj_days[(l1.cls, l1.subject)].add(SLOT_DAY[s1])
        for tn in l1.teacher_names:
            teacher_day_cnt[(tn, SLOT_DAY[s1])] = teacher_day_cnt.get((tn, SLOT_DAY[s1]), 0) + 2

    # ── Tiết đơn ─────────────────────────────────────────────────────────
    for l in single_lessons:
        if not _assign_single(l, assignment, cls_used, teacher_used,
                              cls_subj_days, teacher_day_cnt, teacher_session_used,
                              unavailable, max_per_day, rng):
            return None

    return assignment


# ── Phase 2: GRASP — keep best of N feasible solutions ───────────────────────

def greedy_solve(
    lessons:     list[Lesson],
    unavailable: dict[str, set[int]],
    max_per_day: dict[str, int],
    max_retries: int = 300,
    seed:        int = 42,
    progress_cb: Callable[[str], None] | None = None,
    forced_double_subjects: set[str] = FORCED_DOUBLE_SUBJECTS,
    fixed_teacher_slots: dict[str, set[int]] | None = None,
) -> dict[int, int] | None:
    """
    GRASP: retry greedy N times, return the BEST solution by quality score.
    Không dừng ở lời giải feasible đầu tiên — so sánh tất cả và chọn tốt nhất.
    """
    from collections import Counter
    teacher_count = Counter(tn for l in lessons for tn in l.teacher_names)

    best_result: dict[int, int] | None = None
    best_score: float = float("inf")
    feasible_count = 0
    KEEP_SEARCHING = 50  # tìm thêm 50 lời giải sau feasible đầu tiên

    for attempt in range(max_retries):
        rng = random.Random(seed + attempt)
        sorted_lessons = sorted(
            lessons,
            key=lambda l: (-(teacher_count[l.teacher_name]), rng.random()),
        )
        result = _greedy_once(sorted_lessons, unavailable, max_per_day, rng,
                              forced_double_subjects, fixed_teacher_slots)
        if result is not None:
            feasible_count += 1
            metrics = score_assignment(lessons, result, max_per_day, fixed_teacher_slots)
            sc = metrics["total_score"]
            if sc < best_score:
                best_score = sc
                best_result = result
                if progress_cb and feasible_count <= 3:
                    progress_cb(
                        f"  GRASP #{feasible_count} (attempt {attempt+1}): "
                        f"score={sc:.0f} sessions={metrics['teacher_sessions']} "
                        f"gaps={metrics['teacher_gaps']} ovl={metrics['overload']} "
                        f"2tiet={metrics['two_period_sessions']} "
                        f"lechcap={metrics['misplaced_pairs']} "
                        f"trongsang={metrics['morning_gaps']}"
                    )
            # Đã tìm đủ số lời giải → dừng sớm
            if feasible_count >= KEEP_SEARCHING:
                break

    if best_result is not None and progress_cb:
        final_m = score_assignment(lessons, best_result, max_per_day, fixed_teacher_slots)
        progress_cb(
            f"  GRASP chọn tốt nhất từ {feasible_count}/{max_retries} "
            f"feasible: score={final_m['total_score']:.0f} "
            f"(sessions={final_m['teacher_sessions']}, "
            f"gaps={final_m['teacher_gaps']}, "
            f"ovl={final_m['overload']}, "
            f"2tiet={final_m['two_period_sessions']}, "
            f"lechcap={final_m['misplaced_pairs']}, "
            f"trongsang={final_m['morning_gaps']})"
        )
    elif best_result is None and progress_cb:
        progress_cb(f"  GRASP thất bại sau {max_retries} lần thử")

    return best_result


def fill_morning_gaps(
    lessons: list[Lesson], assignment: dict[int, int],
    unavailable: dict[str, set[int]] | None = None,
) -> dict[int, int]:
    """
    Bước vá sau GRASP: buổi sáng là chính khoá nên cần lấp đầy trước khi
    dùng chiều. GRASP (nhiều lần thử ngẫu nhiên, chọn tốt nhất) đôi khi vẫn
    để lại vài ô sáng trống dù có cách xếp khác không xung đột — vì bài
    toán toàn trường quá lớn, số lời giải hợp lệ mà GRASP tìm được để chọn
    thường rất ít (thực nghiệm: 2–8/300 lần thử với 1300+ tiết), nên "GRASP
    chọn tốt nhất trong số ít lựa chọn" có thể vẫn chưa lấp hết sáng.

    Hàm này quét từng ô sáng còn trống của mỗi lớp, tìm 1 tiết ĐƠN (không
    phải tiết đôi) của ĐÚNG lớp đó đang ở chiều mà GV không bận đúng ô đó
    (kiểm tra lại từ đầu trên toàn bộ assignment cuối cùng — không chỉ dựa
    vào trạng thái tạm trong quá trình xếp), rồi chuyển tiết đó lên sáng.
    Không đụng tới tiết đôi (để không phá cấu trúc 2 tiết liên tiếp) và
    không đụng môn buộc chỉ xếp chiều (AFTERNOON_ONLY_SUBJECTS).
    """
    assignment = dict(assignment)
    lesson_by_id = {l.id: l for l in lessons}

    # Tiết nào đang thuộc 1 cặp tiết đôi thật (2 slot liên tiếp, cùng buổi,
    # cùng lớp+môn) — không di chuyển riêng lẻ để giữ nguyên cặp.
    cls_subj_ids: dict[tuple[str, str], list[int]] = {}
    for l in lessons:
        cls_subj_ids.setdefault((l.cls, l.subject), []).append(l.id)
    paired_ids: set[int] = set()
    for (cls, subj), ids in cls_subj_ids.items():
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                s1, s2 = assignment[ids[i]], assignment[ids[j]]
                pair = (min(s1, s2), max(s1, s2))
                if pair in _CONSECUTIVE_SET and _slot_session(s1) == _slot_session(s2):
                    paired_ids.add(ids[i])
                    paired_ids.add(ids[j])

    unavailable = unavailable or {}
    cls_used:     dict[str, set[int]] = {}
    teacher_used: dict[str, set[int]] = {}
    for l in lessons:
        s = assignment[l.id]
        cls_used.setdefault(l.cls, set()).add(s)
        for tn in l.teacher_names:
            teacher_used.setdefault(tn, set()).add(s)

    fixed = set(FIXED_CLASS_SLOTS.keys())
    n_filled = 0

    for cls in list(cls_used.keys()):
        movable_ids = [
            l.id for l in lessons
            if l.cls == cls and l.id not in paired_ids
            and assignment[l.id] >= _SANG_END
            and l.subject.upper() not in AFTERNOON_ONLY_SUBJECTS
        ]
        empty_morning = [
            s for s in range(_SANG_END)
            if s not in cls_used[cls] and s not in fixed
        ]
        for target in empty_morning:
            chosen = None
            for lid in movable_ids:
                l = lesson_by_id[lid]
                if l.subject.upper() in MONDAY_MORNING_BLOCKED_SUBJECTS and target in MONDAY_MORNING_SLOTS:
                    continue
                if target in _dept_blocked(l):
                    continue
                if any(target in teacher_used.get(tn, set()) for tn in l.teacher_names):
                    continue
                if any(target in unavailable.get(tn, set()) for tn in l.teacher_names):
                    continue
                chosen = lid
                break
            if chosen is None:
                continue
            l = lesson_by_id[chosen]
            old = assignment[chosen]
            cls_used[cls].discard(old)
            cls_used[cls].add(target)
            for tn in l.teacher_names:
                teacher_used[tn].discard(old)
                teacher_used[tn].add(target)
            assignment[chosen] = target
            movable_ids.remove(chosen)
            n_filled += 1

    if n_filled:
        logger.info(f"🩹 fill_morning_gaps: đã chuyển {n_filled} tiết đơn từ chiều sang sáng")
    return assignment


def fill_morning_gaps_swap(
    lessons: list[Lesson], assignment: dict[int, int],
    unavailable: dict[str, set[int]] | None = None,
    breakable_subjects: frozenset[str] = frozenset(),
) -> tuple[dict[int, int], int]:
    """
    Bước vá NÂNG CAO, chạy sau fill_morning_gaps(): với ô sáng còn trống mà
    fill_morning_gaps() bó tay (vì tiết chiều có thể chuyển lên thì GV lại
    đang dạy LỚP KHÁC đúng ô đó, không phải do GV thật sự báo bận), thử
    "đổi chỗ" — dời tiết đang chiếm ô đó (của lớp khác) sang 1 ô trống khác
    còn hợp lệ cho GV/lớp đó, rồi mới chuyển tiết chiều của lớp đang thiếu
    lên lấp ô sáng. Mặc định KHÔNG đụng tiết đôi (cả 2 phía) và không dời
    tiết vào đúng ô GV đã báo bận thật — chỉ "nhường chỗ" giữa các tiết
    đang xếp. `breakable_subjects` (nếu có) cho phép PHÁ tiết đôi của đúng
    những môn trong tập này — dùng khi người dùng đã đồng ý đánh đổi tiết
    đôi để lấp sáng cho vài lớp còn thiếu, chỉ áp dụng ở đúng nơi cần (số
    tiết đôi bị phá phụ thuộc số ô còn trống, không phá tràn lan).
    """
    assignment = dict(assignment)
    lesson_by_id = {l.id: l for l in lessons}

    cls_subj_ids: dict[tuple[str, str], list[int]] = {}
    for l in lessons:
        cls_subj_ids.setdefault((l.cls, l.subject), []).append(l.id)
    paired_ids: set[int] = set()
    for (cls, subj), ids in cls_subj_ids.items():
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                s1, s2 = assignment[ids[i]], assignment[ids[j]]
                pair = (min(s1, s2), max(s1, s2))
                if pair in _CONSECUTIVE_SET and _slot_session(s1) == _slot_session(s2):
                    paired_ids.add(ids[i])
                    paired_ids.add(ids[j])

    def _breakable(lid: int) -> bool:
        return lesson_by_id[lid].subject.upper() in breakable_subjects

    unavailable = unavailable or {}
    cls_used: dict[str, set[int]] = {}
    teacher_used: dict[str, set[int]] = {}
    by_teacher_slot: dict[tuple[str, int], int] = {}
    for l in lessons:
        s = assignment[l.id]
        cls_used.setdefault(l.cls, set()).add(s)
        for tn in l.teacher_names:
            teacher_used.setdefault(tn, set()).add(s)
            by_teacher_slot[(tn, s)] = l.id

    fixed = set(FIXED_CLASS_SLOTS.keys())
    n_filled = 0

    for cls in list(cls_used.keys()):
        movable_ids = [
            l.id for l in lessons
            if l.cls == cls and (l.id not in paired_ids or _breakable(l.id))
            and assignment[l.id] >= _SANG_END
            and l.subject.upper() not in AFTERNOON_ONLY_SUBJECTS
        ]
        empty_morning = [s for s in range(_SANG_END) if s not in cls_used[cls] and s not in fixed]

        for target in empty_morning:
            for lid in list(movable_ids):
                l = lesson_by_id[lid]
                if l.subject.upper() in MONDAY_MORNING_BLOCKED_SUBJECTS and target in MONDAY_MORNING_SLOTS:
                    continue
                if target in _dept_blocked(l):
                    continue
                if any(target in unavailable.get(tn, set()) for tn in l.teacher_names):
                    continue

                blockers: set[int] = set()
                ok = True
                for tn in l.teacher_names:
                    if target in teacher_used.get(tn, set()):
                        blk = by_teacher_slot.get((tn, target))
                        if blk is None or blk == lid or (blk in paired_ids and not _breakable(blk)):
                            ok = False
                            break
                        blockers.add(blk)
                if not ok:
                    continue

                moves: list[tuple[int, int]] = []
                temp_cls_used     = {c: set(v) for c, v in cls_used.items()}
                temp_teacher_used = {t: set(v) for t, v in teacher_used.items()}
                success = True
                for blk in blockers:
                    bl = lesson_by_id[blk]
                    old_slot = assignment[blk]
                    bl_afternoon = bl.subject.upper() in AFTERNOON_ONLY_SUBJECTS
                    candidates = range(_SANG_END, N_SLOTS) if bl_afternoon else range(N_SLOTS)
                    new_slot = None
                    for s2 in candidates:
                        if s2 in (old_slot, target) or s2 in fixed:
                            continue
                        if bl.subject.upper() in MONDAY_MORNING_BLOCKED_SUBJECTS and s2 in MONDAY_MORNING_SLOTS:
                            continue
                        if s2 in _dept_blocked(bl):
                            continue
                        if s2 in temp_cls_used.get(bl.cls, set()):
                            continue
                        if any(s2 in temp_teacher_used.get(tn, set()) for tn in bl.teacher_names):
                            continue
                        if any(s2 in unavailable.get(tn, set()) for tn in bl.teacher_names):
                            continue
                        new_slot = s2
                        break
                    if new_slot is None:
                        success = False
                        break
                    moves.append((blk, new_slot))
                    temp_cls_used.setdefault(bl.cls, set()).discard(old_slot)
                    temp_cls_used.setdefault(bl.cls, set()).add(new_slot)
                    for tn in bl.teacher_names:
                        temp_teacher_used.setdefault(tn, set()).discard(old_slot)
                        temp_teacher_used.setdefault(tn, set()).add(new_slot)

                if not success:
                    continue

                # Áp dụng thật: dời từng blocker rồi mới dời l vào target.
                for blk, new_slot in moves:
                    bl = lesson_by_id[blk]
                    old_slot = assignment[blk]
                    cls_used[bl.cls].discard(old_slot)
                    cls_used[bl.cls].add(new_slot)
                    for tn in bl.teacher_names:
                        teacher_used[tn].discard(old_slot)
                        teacher_used[tn].add(new_slot)
                        del by_teacher_slot[(tn, old_slot)]
                        by_teacher_slot[(tn, new_slot)] = blk
                    assignment[blk] = new_slot

                old_l_slot = assignment[lid]
                cls_used[cls].discard(old_l_slot)
                cls_used[cls].add(target)
                for tn in l.teacher_names:
                    teacher_used[tn].discard(old_l_slot)
                    teacher_used[tn].add(target)
                    del by_teacher_slot[(tn, old_l_slot)]
                    by_teacher_slot[(tn, target)] = lid
                assignment[lid] = target
                movable_ids.remove(lid)
                n_filled += 1
                break

    if n_filled:
        logger.info(f"🩹 fill_morning_gaps_swap: đã đổi chỗ để lấp thêm {n_filled} ô sáng")
    return assignment, n_filled


def concentrate_teacher_mornings(
    lessons: list[Lesson], assignment: dict[int, int],
    unavailable: dict[str, set[int]] | None = None,
    subjects: frozenset[str] = frozenset({"TIN"}),
) -> tuple[dict[int, int], int]:
    """
    Bước vá CUỐI CÙNG (chạy sau Tabu Search), chỉ áp dụng cho các môn trong
    `subjects` (mặc định TIN): fill_morning_gaps_swap dồn theo Ô TRỐNG CỦA
    LỚP (mỗi lớp lấp đầy sáng), nhưng mục tiêu ở đây khác — dồn các tiết của
    CÙNG 1 GV (qua nhiều lớp khác nhau) vào CÙNG 1 buổi sáng của 1 ngày, để
    GV đó dạy được 4 tiết liên tục 1 buổi sáng, thay vì rải mỗi buổi sáng
    1-2 tiết trên nhiều ngày khác nhau. Chỉ dời tiết vào Ô TRỐNG thật (không
    đổi chỗ/đẩy tiết khác), nên có thể không dồn hết mọi GV — đây là bước
    "cố gắng thêm", không đảm bảo mọi GV đủ 4 tiết/buổi.
    """
    assignment = dict(assignment)
    lesson_by_id = {l.id: l for l in lessons}
    unavailable = unavailable or {}
    fixed = set(FIXED_CLASS_SLOTS.keys())

    cls_used:     dict[str, set[int]] = {}
    teacher_used: dict[str, set[int]] = {}
    for l in lessons:
        s = assignment[l.id]
        cls_used.setdefault(l.cls, set()).add(s)
        for tn in l.teacher_names:
            teacher_used.setdefault(tn, set()).add(s)

    def _cls_subj_day_count(cls: str, subj: str, day_idx: int, exclude_id: int) -> int:
        return sum(
            1 for l in lessons
            if l.id != exclude_id and l.cls == cls and l.subject == subj
            and SLOT_DAY[assignment[l.id]] == day_idx
        )

    by_teacher: dict[str, list[int]] = {}
    for l in lessons:
        if l.subject.upper() not in subjects:
            continue
        for tn in l.teacher_names:
            by_teacher.setdefault(tn, []).append(l.id)

    n_moved = 0
    for tn, ids in by_teacher.items():
        for day_idx, day in enumerate(DAYS_ORDER):
            day_slots = _day_morning_slots(day)
            free = sorted(
                s for s in day_slots
                if s not in teacher_used.get(tn, set())
                and s not in unavailable.get(tn, set())
                and s not in fixed
            )
            for target in free:
                chosen = None
                for lid in ids:
                    if SLOT_DAY[assignment[lid]] == day_idx:
                        continue  # đã ở đúng ngày này rồi, không cần dời
                    l = lesson_by_id[lid]
                    if target in cls_used.get(l.cls, set()):
                        continue  # lớp đó đã có tiết khác đúng ô này
                    if any(target in teacher_used.get(t2, set()) for t2 in l.teacher_names if t2 != tn):
                        continue  # GV dạy chung (team-teaching) bận ô này
                    if target in _dept_blocked(l):
                        continue
                    if _cls_subj_day_count(l.cls, l.subject, day_idx, exclude_id=lid) >= 2:
                        continue  # tránh 3 tiết cùng môn/lớp/ngày
                    chosen = lid
                    break
                if chosen is None:
                    continue
                old = assignment[chosen]
                l = lesson_by_id[chosen]
                cls_used[l.cls].discard(old)
                cls_used[l.cls].add(target)
                for t2 in l.teacher_names:
                    teacher_used[t2].discard(old)
                    teacher_used[t2].add(target)
                assignment[chosen] = target
                ids.remove(chosen)
                n_moved += 1

    if n_moved:
        logger.info(f"🎯 concentrate_teacher_mornings: đã dồn {n_moved} tiết để GV dạy gọn buổi sáng")
    return assignment, n_moved


# ── Phase 3 (optional): CP-SAT Optimisation ──────────────────────────────────
#
# Multi-objective (weighted sum):
#   1. teacher_sessions : minimise total (day,buoi) half-days used by all teachers
#   2. teacher_gaps     : minimise empty slots between first/last lesson per session
#   3. overload         : minimise max_per_day violations
#
# All hard constraints preserved. Hints from GRASP best solution.

def cpsat_improve(
    lessons:      list[Lesson],
    hint:         dict[int, int],
    unavailable:  dict[str, set[int]],
    max_per_day:  dict[str, int],
    time_limit_s: float = 30.0,
    n_workers:    int   = 1,  # >1 đã gây treo vô hạn (không tôn trọng time_limit_s)
                              # trong môi trường chạy hiện tại — xem ghi chú ở
                              # solver.parameters.num_workers bên dưới.
    progress_cb:  Callable[[str], None] | None = None,
) -> dict[int, int] | None:
    """
    CP-SAT improvement from a GRASP hint.
    Minimises: W_SESSION * sessions + W_GAP * gaps + W_OVERLOAD * overload
             + W_TWO_PERIOD * two_period_sessions + W_PAIR_POS * misplaced_pairs
             + W_SAMEDAY * same_day_excess.
    """
    W_SESSION    = 3     # mỗi buổi thêm = 3 penalty
    W_GAP        = 2     # mỗi tiết trống giữa = 2 penalty
    W_OVERLOAD   = 10    # mỗi tiết vượt max_per_day = 10 penalty
    W_TWO_PERIOD = 8     # mỗi buổi GV chỉ có đúng 2 tiết = 8 penalty (nên dồn gọn)
    W_PAIR_POS   = 4     # tiết đôi không bắt đầu ở Tiết 2 sáng = 4 penalty (ưu tiên, không bắt buộc)
    W_SAMEDAY    = 5     # mỗi tiết cùng môn/lớp vượt quá 2 tiết/ngày = 5 penalty (khớp score_assignment)

    model  = cp_model.CpModel()
    solver = cp_model.CpSolver()

    # ── Variables ────────────────────────────────────────────────────────
    fixed_slots = set(FIXED_CLASS_SLOTS.keys())  # vd: Thứ 2 Tiết 1 = Chào cờ, không xếp môn khác
    slot_var: dict[int, cp_model.IntVar] = {}
    for l in lessons:
        busy      = set().union(*(unavailable.get(tn, set()) for tn in l.teacher_names))
        available = [s for s in range(N_SLOTS) if s not in busy and s not in fixed_slots]
        if not available:
            return None
        slot_var[l.id] = model.new_int_var_from_domain(
            cp_model.Domain.from_values(available), f"s{l.id}"
        )
        model.add_hint(slot_var[l.id], hint[l.id])

    # ── Hard: class all-different ────────────────────────────────────────
    cls_ids: dict[str, list[int]] = {}
    for l in lessons:
        cls_ids.setdefault(l.cls, []).append(l.id)
    for ids in cls_ids.values():
        if len(ids) > 1:
            model.add_all_different([slot_var[lid] for lid in ids])

    # ── Hard: teacher all-different (per teacher_code) ───────────────────
    teacher_ids: dict[str, list[int]] = {}
    for l in lessons:
        for tc in l.teacher_codes:
            teacher_ids.setdefault(tc, []).append(l.id)
    for ids in teacher_ids.values():
        if len(ids) > 1:
            model.add_all_different([slot_var[lid] for lid in ids])

    code_to_name: dict[str, str] = {}
    for l in lessons:
        for tc, tn in zip(l.teacher_codes, l.teacher_names):
            code_to_name[tc] = tn

    # ── Hard: AFTERNOON_ONLY subjects ────────────────────────────────────
    for l in lessons:
        if l.subject.upper() in AFTERNOON_ONLY_SUBJECTS:
            afternoon_slots = [s for s in range(N_SLOTS) if s >= _SANG_END]
            model.add_allowed_assignments(
                [slot_var[l.id]], [[s] for s in afternoon_slots]
            )

    # ── Hard: MONDAY_MORNING_BLOCKED subjects (QPAN, THỂ DỤC) ─────────────
    for l in lessons:
        if l.subject.upper() in MONDAY_MORNING_BLOCKED_SUBJECTS:
            non_monday_morning = [s for s in range(N_SLOTS) if s not in MONDAY_MORNING_SLOTS]
            model.add_allowed_assignments(
                [slot_var[l.id]], [[s] for s in non_monday_morning]
            )

    # ── Hard: DEPT_MEETING_DAY ("ngày sinh hoạt chuyên môn" theo tổ) ──────
    for l in lessons:
        blocked = _dept_blocked(l)
        if blocked:
            allowed = [s for s in range(N_SLOTS) if s not in blocked]
            model.add_allowed_assignments(
                [slot_var[l.id]], [[s] for s in allowed]
            )

    # ── Soft objectives (khai báo trước — khối "double-pair" bên dưới cần
    #    dùng pair_pos_terms/PREFERRED_PAIR_START_SLOTS ngay) ───────────────
    DEFAULT_MAX = 4
    session_terms:    list[cp_model.IntVar] = []
    gap_terms:        list[cp_model.IntVar] = []
    overload_terms:   list[cp_model.IntVar] = []
    two_period_terms: list[cp_model.IntVar] = []
    pair_pos_terms:   list[cp_model.IntVar] = []  # phạt khi tiết đôi KHÔNG bắt đầu ở Tiết 2 sáng
    same_day_terms:   list[cp_model.IntVar] = []  # phạt khi ≥3 tiết cùng môn/lớp rơi cùng 1 ngày

    def _pref_pair_start_slots(periods: frozenset[int]) -> list[int]:
        return [
            s for s in range(N_SLOTS)
            if SLOT_INFO[s][1] == "Sáng" and SLOT_INFO[s][2] in periods
        ]

    PREFERRED_PAIR_START_SLOTS = _pref_pair_start_slots(frozenset({PREFERRED_DOUBLE_PERIOD}))
    # Riêng TIN: ưu tiên Tiết 1-2 hoặc 3-4 (xem TIN_PREFERRED_START_PERIODS).
    TIN_PREFERRED_PAIR_START_SLOTS = _pref_pair_start_slots(TIN_PREFERRED_START_PERIODS)

    # ── Same-subject different-day ───────────────────────────────────────
    cls_subj: dict[tuple[str, str], list[int]] = {}
    for l in lessons:
        cls_subj.setdefault((l.cls, l.subject), []).append(l.id)

    # Preserve double-pair structure from hint.
    #
    # QUAN TRỌNG: chỉ khoá ĐÚNG 1 cặp mà Greedy thực sự ghép làm tiết đôi
    # (occurrence 0 và 1 — xem `_greedy_once`: double_pairs.append((group[0],
    # group[1]))), KHÔNG được quét mọi cặp (i,j) trong nhóm rồi khoá bất kỳ
    # cặp nào tình cờ liền kề nhau trong hint. Bug cũ: với môn có ≥3 tiết/tuần
    # thuộc FORCED_DOUBLE_SUBJECTS (vd TOÁN ppw=3), nếu Greedy xếp tiết đơn
    # thứ 3 ngay sát tiết đôi để dồn gọn (rất thường xảy ra), vòng lặp cũ sẽ
    # khoá CẢ 2 cặp chồng lấn (id0–id1 và id1–id2) → ép cả 3 tiết vào cùng 1
    # ngày, mâu thuẫn trực tiếp với ràng buộc "same-subject different-day"
    # ngay bên dưới → model.solve() trả về INFEASIBLE gần như ngay lập tức.
    occ_id: dict[tuple[str, str, int], int] = {}
    for l in lessons:
        occ_id[(l.cls, l.subject, l.occurrence)] = l.id

    for (cls, subj), ids in cls_subj.items():
        n = len(ids)
        # Chỉ khoá cặp tiết đôi cho môn thuộc FORCED_DOUBLE_SUBJECTS — khớp
        # với quy tắc ghép tiết đôi trong _greedy_once.
        if n < 2 or subj not in FORCED_DOUBLE_SUBJECTS:
            continue
        id0 = occ_id.get((cls, subj, 0))
        id1 = occ_id.get((cls, subj, 1))
        if id0 is None or id1 is None:
            continue
        hi, hj = hint.get(id0), hint.get(id1)
        if hi is None or hj is None:
            continue
        pair = (min(hi, hj), max(hi, hj))
        if pair not in _CONSECUTIVE_SET:
            continue

        first_id = id0 if hi < hj else id1
        if hi < hj:
            model.add(slot_var[id1] == slot_var[id0] + 1)
        else:
            model.add(slot_var[id0] == slot_var[id1] + 1)

        # Ưu tiên (soft): cặp tiết đôi nên bắt đầu ở Tiết 2 sáng (riêng TIN:
        # Tiết 1 hoặc Tiết 3 sáng — xem TIN_PREFERRED_PAIR_START_SLOTS).
        pref_slots = (
            TIN_PREFERRED_PAIR_START_SLOTS if subj.upper() == "TIN"
            else PREFERRED_PAIR_START_SLOTS
        )
        in_pref = []
        for s in pref_slots:
            b = model.new_bool_var(f"prefslot_{first_id}_{s}")
            model.add(slot_var[first_id] == s).only_enforce_if(b)
            model.add(slot_var[first_id] != s).only_enforce_if(b.negated())
            in_pref.append(b)
        is_pref = model.new_bool_var(f"ispref_{first_id}")
        model.add_max_equality(is_pref, in_pref)
        pair_pos_terms.append(is_pref.negated())

    # Same-subject different-day: SOFT, không phải hard. Trước đây dùng
    # model.add_all_different(day_var) — tuyệt đối cấm 2 tiết cùng môn/lớp
    # rơi cùng ngày. Với bộ dữ liệu thật của trường (nhiều lớp × nhiều GV
    # dùng chung), ràng buộc cứng này gây INFEASIBLE ngay khi gộp đủ nhiều
    # lớp (xác nhận bằng thực nghiệm: 10 lớp OK, 11 lớp trở lên INFEASIBLE,
    # và feasible trở lại ngay khi đổi ràng buộc này thành soft). Greedy đã
    # xử lý mềm dẻo việc này từ đầu (cho phép trùng ngày khi cần, chỉ phạt
    # qua same_day_violations trong score_assignment) — đổi CP-SAT theo đúng
    # logic đó để nhất quán: tiết đôi (2 tiết cùng ngày) là cố ý, không phạt;
    # chỉ phạt phần VƯỢT quá 2 tiết cùng môn/lớp trong 1 ngày.
    day_var: dict[int, cp_model.IntVar] = {}
    for (cls, subj), ids in cls_subj.items():
        if len(ids) < 2:
            continue
        for lid in ids:
            if lid not in day_var:
                day_var[lid] = model.new_int_var(0, N_DAYS - 1, f"d{lid}")
                model.add_element(slot_var[lid], SLOT_DAY, day_var[lid])

        for d in range(N_DAYS):
            day_match = []
            for lid in ids:
                b = model.new_bool_var(f"sameday_{cls}_{subj}_{lid}_{d}")
                model.add(day_var[lid] == d).only_enforce_if(b)
                model.add(day_var[lid] != d).only_enforce_if(b.negated())
                day_match.append(b)
            count_d = model.new_int_var(0, len(ids), f"samedaycnt_{cls}_{subj}_{d}")
            model.add(count_d == sum(day_match))
            excess = model.new_int_var(0, len(ids), f"samedayexcess_{cls}_{subj}_{d}")
            model.add(excess >= count_d - 2)
            same_day_terms.append(excess)

    # Bảng buổi: slot → session_index (0..9: 5 ngày × 2 buổi)
    SLOT_SESSION_IDX = []
    for s in range(N_SLOTS):
        d = SLOT_DAY[s]
        b = 0 if s < _SANG_END else 1
        SLOT_SESSION_IDX.append(d * 2 + b)
    N_SESSIONS = N_DAYS * 2  # 10

    for tc, ids in teacher_ids.items():
        if len(ids) <= 1:
            continue
        name   = code_to_name.get(tc, tc)
        max_pd = max_per_day.get(name, DEFAULT_MAX)

        for lid in ids:
            if lid not in day_var:
                day_var[lid] = model.new_int_var(0, N_DAYS - 1, f"d{lid}")
                model.add_element(slot_var[lid], SLOT_DAY, day_var[lid])

        # Session vars per lesson
        sess_var: dict[int, cp_model.IntVar] = {}
        for lid in ids:
            sess_var[lid] = model.new_int_var(0, N_SESSIONS - 1, f"sess_{tc}_{lid}")
            model.add_element(slot_var[lid], SLOT_SESSION_IDX, sess_var[lid])

        # ── Objective 1: minimise sessions used ──────────────────────────
        for si in range(N_SESSIONS):
            has_lesson = []
            for lid in ids:
                b = model.new_bool_var(f"hs_{tc}_{lid}_{si}")
                model.add(sess_var[lid] == si).only_enforce_if(b)
                model.add(sess_var[lid] != si).only_enforce_if(b.negated())
                has_lesson.append(b)
            used_si = model.new_bool_var(f"used_{tc}_{si}")
            model.add_max_equality(used_si, has_lesson)
            session_terms.append(used_si)

        # ── Objective 2: minimise gaps within each session ───────────────
        for si in range(N_SESSIONS):
            d_idx = si // 2
            buoi_morning = (si % 2 == 0)
            if buoi_morning:
                base = d_idx * N_SANG
                n_per = N_SANG
            else:
                base = _SANG_END + d_idx * N_CHIEU
                n_per = N_CHIEU

            in_session = []
            for lid in ids:
                b = model.new_bool_var(f"is_{tc}_{lid}_{si}")
                model.add(sess_var[lid] == si).only_enforce_if(b)
                model.add(sess_var[lid] != si).only_enforce_if(b.negated())
                in_session.append((lid, b))

            if len(in_session) < 2:
                continue

            # first_slot, last_slot in this session
            first_s = model.new_int_var(base, base + n_per - 1, f"first_{tc}_{si}")
            last_s  = model.new_int_var(base, base + n_per - 1, f"last_{tc}_{si}")
            count_s = model.new_int_var(0, len(ids), f"cnt_{tc}_{si}")

            for lid, b in in_session:
                model.add(slot_var[lid] >= first_s).only_enforce_if(b)
                model.add(slot_var[lid] <= last_s).only_enforce_if(b)

            model.add(count_s == sum(b for _, b in in_session))

            # gap = (last - first) - (count - 1) = span - (count-1)
            span = model.new_int_var(0, n_per - 1, f"span_{tc}_{si}")
            model.add(span == last_s - first_s)

            gap = model.new_int_var(0, n_per, f"gap_{tc}_{si}")
            model.add(gap >= span - count_s + 1)
            gap_terms.append(gap)

            # ── Objective 4: hạn chế buổi chỉ ĐÚNG 2 tiết/GV ──────────────
            # Lãng phí 1 buổi lên trường chỉ để dạy 2 tiết — ưu tiên dồn gọn
            # (nhiều tiết hơn trong ít buổi hơn) thay vì rải mỏng kiểu này.
            is_two = model.new_bool_var(f"two_{tc}_{si}")
            model.add(count_s == 2).only_enforce_if(is_two)
            model.add(count_s != 2).only_enforce_if(is_two.negated())
            two_period_terms.append(is_two)

        # ── Objective 3: minimise overload ───────────────────────────────
        for d in range(N_DAYS):
            day_match = []
            for lid in ids:
                b = model.new_bool_var(f"bd_{tc}_{lid}_{d}")
                model.add(day_var[lid] == d).only_enforce_if(b)
                model.add(day_var[lid] != d).only_enforce_if(b.negated())
                day_match.append(b)
            count_d  = model.new_int_var(0, len(ids), f"dcnt_{tc}_{d}")
            model.add(count_d == sum(day_match))
            ov = model.new_int_var(0, len(ids), f"ov_{tc}_{d}")
            model.add(ov >= count_d - max_pd)
            overload_terms.append(ov)

    # ── Combined objective ───────────────────────────────────────────────
    total_pen = model.new_int_var(0, 100000, "total_pen")
    all_terms = (
        [W_SESSION * t for t in session_terms]
        + [W_GAP * t for t in gap_terms]
        + [W_OVERLOAD * t for t in overload_terms]
        + [W_TWO_PERIOD * t for t in two_period_terms]
        + [W_PAIR_POS * t for t in pair_pos_terms]
        + [W_SAMEDAY * t for t in same_day_terms]
    )
    if all_terms:
        model.add(total_pen == sum(all_terms))
        model.minimize(total_pen)

    solver.parameters.max_time_in_seconds = time_limit_s
    # LƯU Ý: num_workers > 1 (tìm kiếm đa luồng) đã được xác minh bằng thực
    # nghiệm là TREO VÔ HẠN trong môi trường chạy hiện tại — không tôn trọng
    # max_time_in_seconds dù bài toán rất nhỏ (26 tiết, 1 lớp). num_workers=1
    # (đơn luồng) tôn trọng đúng time_limit_s. Nếu triển khai sang môi trường
    # khác và muốn thử đa luồng trở lại, kiểm tra kỹ bằng 1 lớp nhỏ trước.
    solver.parameters.num_workers         = n_workers
    solver.parameters.log_search_progress = False

    if progress_cb:
        progress_cb(f"  CP-SAT tối ưu (max {time_limit_s:.0f}s, "
                    f"W={W_SESSION}/{W_GAP}/{W_OVERLOAD}/{W_TWO_PERIOD}/{W_PAIR_POS})...")

    status = solver.solve(model)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        result = {l.id: solver.value(slot_var[l.id]) for l in lessons}
        pen = int(solver.objective_value) if all_terms else 0
        if progress_cb:
            progress_cb(f"  CP-SAT {solver.status_name(status)} "
                        f"in {solver.wall_time:.1f}s (penalty={pen})")
        return result
    if progress_cb:
        progress_cb(f"  CP-SAT không tìm được lời giải ({solver.status_name(status)})")
    return None


# ── Solution → DataFrame ──────────────────────────────────────────────────────

def assignment_to_df(
    lessons: list[Lesson], assignment: dict[int, int],
    homeroom_map: dict[str, str] | None = None,
) -> pd.DataFrame:
    """homeroom_map: {lop: ten_gvcn} — gán GVCN vào các dòng cố định (Chào cờ)
    thay vì để trống. Không truyền thì giữ nguyên hành vi cũ (ten_gv rỗng)."""
    records = []
    for l in lessons:
        s = assignment[l.id]
        day, buoi, period = SLOT_INFO[s]
        records.append({
            "thu": day, "tiet": period, "buoi": buoi,
            "lop": l.cls,
            # Team-teaching: "GV1 / GV2"; thường: "GV1"
            "ten_gv": " / ".join(l.teacher_names) if l.teacher_names else "",
            "mon": l.subject,
        })
    df = pd.DataFrame(records)

    # Thêm Chào cờ cho tất cả lớp — gán đúng GVCN nếu có homeroom_map
    all_classes = df["lop"].unique() if not df.empty else []
    fixed_rows = []
    for slot, label in FIXED_CLASS_SLOTS.items():
        day, buoi, period = SLOT_INFO[slot]
        for cls in all_classes:
            fixed_rows.append({
                "thu": day, "tiet": period, "buoi": buoi,
                "lop": cls, "ten_gv": (homeroom_map or {}).get(cls, ""), "mon": label,
            })
    if fixed_rows:
        df = pd.concat([pd.DataFrame(fixed_rows), df], ignore_index=True)
    return df


def score_assignment(lessons: list[Lesson], assignment: dict[int, int],
                     max_per_day: dict[str, int],
                     fixed_teacher_slots: dict[str, set[int]] | None = None) -> dict:
    """
    Comprehensive quality metrics for a solution.

    Returns dict with:
      teacher_sessions      : total half-days used by all teachers
      teacher_gaps          : total empty slots between lessons within a half-day
      overload              : total lessons exceeding max_per_day
      same_day_violations   : same subject 3+ times on same day for a class
                              (double pairs are intentional → count only excess)
      two_period_sessions   : số buổi (sáng/chiều) mà 1 GV chỉ có ĐÚNG 2 tiết —
                              lãng phí 1 buổi lên trường chỉ để dạy 2 tiết, nên
                              dồn gọn thay vì để rải rác kiểu này.
      misplaced_pairs       : số cặp tiết đôi (VĂN/TOÁN, hoặc môn đúng 2 tiết/tuần)
                              không bắt đầu ở Tiết 2 sáng (chỉ là ưu tiên, không
                              phải vi phạm cứng).
      morning_gaps          : số ô sáng lẽ ra lấp được (theo tổng số tiết của lớp)
                              nhưng đang bị bỏ trống — buổi sáng là chính khoá,
                              nên đây là chỉ số ưu tiên cao nhất sau overload.
      total_score           : weighted total (lower = better)
    """
    from collections import defaultdict

    teacher_day:     dict[tuple[str, int], int]              = defaultdict(int)
    teacher_session: dict[str, set[tuple[int, str]]]         = defaultdict(set)
    teacher_slots:   dict[tuple[str, int, str], list[int]]   = defaultdict(list)
    cls_subj_day_cnt: dict[tuple[str, str, int], int]        = defaultdict(int)
    cls_subj_slots:  dict[tuple[str, str], list[int]]        = defaultdict(list)

    code_to_name: dict[str, str] = {}
    for l in lessons:
        s   = assignment[l.id]
        day = SLOT_DAY[s]
        sess = _slot_session(s)
        for tc, tn in zip(l.teacher_codes, l.teacher_names):
            code_to_name[tc] = tn
            teacher_day[(tc, day)] += 1
            teacher_session[tc].add(sess)
            teacher_slots[(tc, sess[0], sess[1])].append(s)
        cls_subj_day_cnt[(l.cls, l.subject, day)] += 1
        cls_subj_slots[(l.cls, l.subject)].append(s)

    # Tiết cố định (vd khối đã khoá) của GV: tính vào buổi/tiết trống/quá tải
    if fixed_teacher_slots:
        name_to_code = {tn: tc for tc, tn in code_to_name.items()}
        for tn, slots in fixed_teacher_slots.items():
            tc = name_to_code.get(tn)
            if tc is None:
                continue
            for s in slots:
                sess = _slot_session(s)
                teacher_day[(tc, SLOT_DAY[s])] += 1
                teacher_session[tc].add(sess)
                teacher_slots[(tc, sess[0], sess[1])].append(s)

    # Tiết đôi (2 tiết/ngày) là cố ý → chỉ tính vi phạm khi ≥3 tiết cùng môn/ngày
    same_day_violations = sum(
        cnt - 2 for cnt in cls_subj_day_cnt.values() if cnt > 2
    )

    # Đếm thêm: môn chỉ có 1 tiết/tuần mà bị xếp trùng ngày với chính nó (không nên)
    # → môn ≤1 tiết nhóm mà 2+ cùng ngày là vi phạm thực sự
    subj_groups_size: dict[tuple[str, str], int] = defaultdict(int)
    for l in lessons:
        subj_groups_size[(l.cls, l.subject)] += 1
    for (cls, subj, day), cnt in cls_subj_day_cnt.items():
        grp = subj_groups_size[(cls, subj)]
        # Môn 1-tiết không có double pair → 2 cùng ngày = vi phạm
        if grp == 1 and cnt >= 2:
            same_day_violations += cnt - 1

    mpd_default = 4
    overload = sum(
        max(0, cnt - max_per_day.get(code_to_name.get(tc, tc), mpd_default))
        for (tc, _), cnt in teacher_day.items()
    )

    total_sessions = sum(len(sessions) for sessions in teacher_session.values())

    total_gaps = 0
    two_period_sessions = 0
    one_period_sessions = sum(1 for sl in teacher_slots.values() if len(sl) == 1)
    for key, slots_list in teacher_slots.items():
        if len(slots_list) < 2:
            continue
        if len(slots_list) == 2:
            two_period_sessions += 1
        sorted_s = sorted(slots_list)
        span = sorted_s[-1] - sorted_s[0]
        total_gaps += span - (len(sorted_s) - 1)

    # Cặp tiết đôi (chỉ các môn thuộc FORCED_DOUBLE_SUBJECTS) không bắt đầu ở
    # Tiết 2 sáng — chỉ là tiêu chí ưu tiên (soft), không phải vi phạm cứng.
    misplaced_pairs = 0
    for (cls, subj), slots_list in cls_subj_slots.items():
        if len(slots_list) < 2 or subj not in FORCED_DOUBLE_SUBJECTS:
            continue
        found_pair = None
        for a in range(len(slots_list)):
            for b in range(a + 1, len(slots_list)):
                s1, s2 = sorted((slots_list[a], slots_list[b]))
                if (s1, s2) in _CONSECUTIVE_SET:
                    found_pair = (s1, s2)
                    break
            if found_pair:
                break
        if found_pair:
            s1, _ = found_pair
            pref_periods = (
                TIN_PREFERRED_START_PERIODS if subj.upper() == "TIN"
                else frozenset({PREFERRED_DOUBLE_PERIOD})
            )
            if not (SLOT_INFO[s1][1] == "Sáng" and SLOT_INFO[s1][2] in pref_periods):
                misplaced_pairs += 1

    # Buổi sáng là CHÍNH KHOÁ — mọi lớp phải lấp đầy 19 ô sáng còn lại (sau
    # khi trừ 1 ô Chào cờ cố định) TRƯỚC KHI dùng buổi chiều, miễn là lớp có
    # đủ tổng số tiết/tuần để làm điều đó. morning_gaps = số ô sáng lẽ ra lấp
    # được (theo tổng số tiết của lớp) nhưng lại đang bỏ trống.
    _MORNING_CAPACITY = N_SANG * N_DAYS - len(FIXED_CLASS_SLOTS)  # 20 - 1 = 19
    cls_total:   dict[str, int] = defaultdict(int)
    cls_morning: dict[str, int] = defaultdict(int)
    for l in lessons:
        s = assignment[l.id]
        cls_total[l.cls] += 1
        if s < _SANG_END:
            cls_morning[l.cls] += 1
    morning_gaps = sum(
        max(0, min(_MORNING_CAPACITY, cls_total[cls]) - cls_morning[cls])
        for cls in cls_total
    )

    # Lớp học chiều phải liền từ Tiết 1 (HS không phải chờ giữa giờ)
    cls_pm: dict[tuple[str, int], list[int]] = defaultdict(list)
    for l in lessons:
        s = assignment[l.id]
        if s >= _SANG_END:
            cls_pm[(l.cls, SLOT_DAY[s])].append(SLOT_INFO[s][2])
    class_pm_gaps = sum(max(ps) - len(ps) for ps in cls_pm.values())

    # Ngày GV lên trường chỉ dạy 1–2 tiết (nên dồn ≥3 tiết/ngày)
    short_days = sum(1 for cnt in teacher_day.values() if 0 < cnt < 3)

    W_S, W_G, W_O, W_V, W_T, W_P, W_M, W_C, W_D, W_1 = 3, 6, 10, 5, 8, 4, 40, 2, 12, 10
    total_score = (W_S * total_sessions + W_G * total_gaps + W_1 * one_period_sessions
                   + W_O * overload + W_V * same_day_violations
                   + W_T * two_period_sessions + W_P * misplaced_pairs
                   + W_M * morning_gaps + W_C * class_pm_gaps + W_D * short_days)

    return {
        "teacher_sessions":    total_sessions,
        "teacher_gaps":        total_gaps,
        "morning_gaps":        morning_gaps,
        "class_pm_gaps":       class_pm_gaps,
        "short_days":          short_days,
        "one_period_sessions": one_period_sessions,
        "overload":            overload,
        "same_day_violations": same_day_violations,
        "two_period_sessions": two_period_sessions,
        "misplaced_pairs":     misplaced_pairs,
        "total_score":         total_score,
    }


def _tabu_slot_hard_ok(l: "Lesson", s: int, unavailable: dict[str, set[int]]) -> bool:
    """Kiểm tra các ràng buộc cứng theo MÔN (không liên quan lớp/GV đang
    chiếm slot — những cái đó kiểm riêng bằng cls_used/teacher_used) cho 1
    tiết `l` nếu đặt vào slot `s`."""
    subj = l.subject.upper()
    if subj in AFTERNOON_ONLY_SUBJECTS and s < _SANG_END:
        return False
    if subj in MONDAY_MORNING_BLOCKED_SUBJECTS and s in MONDAY_MORNING_SLOTS:
        return False
    if s in _dept_blocked(l):
        return False
    if any(s in unavailable.get(tn, set()) for tn in l.teacher_names):
        return False
    return True


def _paired_lesson_ids(lessons: list["Lesson"], assignment: dict[int, int]) -> set[int]:
    """Phát hiện id các tiết đang thuộc 1 cặp tiết đôi THẬT (2 slot liên
    tiếp, cùng buổi, cùng lớp+môn) trong 1 assignment cụ thể."""
    cls_subj_ids: dict[tuple[str, str], list[int]] = {}
    for l in lessons:
        cls_subj_ids.setdefault((l.cls, l.subject), []).append(l.id)
    paired: set[int] = set()
    for (cls, subj), ids in cls_subj_ids.items():
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                s1, s2 = assignment[ids[i]], assignment[ids[j]]
                pair = (min(s1, s2), max(s1, s2))
                if pair in _CONSECUTIVE_SET and _slot_session(s1) == _slot_session(s2):
                    paired.add(ids[i])
                    paired.add(ids[j])
    return paired


def kempe_improve(
    lessons:      list[Lesson],
    assignment:   dict[int, int],
    unavailable:  dict[str, set[int]],
    max_per_day:  dict[str, int],
    time_limit_s: float = 30.0,
    seed:         int   = 0,
    progress_cb:  Callable[[str], None] | None = None,
    fixed_teacher_slots: dict[str, set[int]] | None = None,
    no_worse: tuple[str, ...] = (),
) -> tuple[dict[int, int], dict]:
    """Local search bằng chuỗi Kempe: đổi chỗ đồng loạt mọi tiết liên thông
    (cùng lớp hoặc cùng GV) giữa 2 slot a ↔ b — luôn giữ không trùng lớp/GV.
    Ưu tiên kéo tiết chiều của lớp còn thiếu sáng lên ô sáng trống; còn lại
    thử ngẫu nhiên để dồn gọn lịch GV. Chỉ nhận nước đi làm điểm tốt hơn
    (hoặc bằng, để thoát vùng phẳng). Không đụng tiết đôi đang ghép."""
    import time as _t
    from collections import defaultdict

    rng = random.Random(seed)
    cur = dict(assignment)
    by_id = {l.id: l for l in lessons}
    at_cls: dict[tuple[str, int], int] = {}
    at_t:   dict[tuple[str, int], int] = {}
    for l in lessons:
        at_cls[l.cls, cur[l.id]] = l.id
        for tn in l.teacher_names:
            at_t[tn, cur[l.id]] = l.id
    cls_lessons: dict[str, list[int]] = defaultdict(list)
    grp_ids: dict[tuple[str, str], list[int]] = defaultdict(list)
    for l in lessons:
        cls_lessons[l.cls].append(l.id)
        grp_ids[l.cls, l.subject].append(l.id)

    def find_pairs() -> dict[int, int]:
        part: dict[int, int] = {}
        for ids in grp_ids.values():
            if len(ids) < 2:
                continue
            by_slot = {cur[i]: i for i in ids}
            for s, i in sorted(by_slot.items()):
                j = by_slot.get(s + 1)
                if j is not None and i not in part and (s, s + 1) in _CONSECUTIVE_SET:
                    part[i], part[j] = j, i
        return part

    partner = find_pairs()
    paired = set(partner)
    morning_cap = N_SANG * N_DAYS - len(FIXED_CLASS_SLOTS)
    morning_free = [s for s in range(_SANG_END) if s not in FIXED_CLASS_SLOTS]
    movable = [l.id for l in lessons]

    def chain(start: list[int], swap: dict[int, int]) -> list[int] | None:
        """Thành phần liên thông khi đổi slot theo `swap` (song ánh, tự nghịch đảo).
        Tiết đôi chỉ được kéo theo nếu tiết còn lại của cặp cũng nằm trong chuỗi."""
        comp, queue = set(start), list(start)
        while queue:
            x = by_id[queue.pop()]
            tgt = swap[cur[x.id]]
            others = [at_cls.get((x.cls, tgt))] + [at_t.get((tn, tgt)) for tn in x.teacher_names]
            for o in others:
                if o is not None and o not in comp:
                    comp.add(o)
                    queue.append(o)
        for x_id in comp:
            if x_id in paired and partner.get(x_id) not in comp:
                return None
            if not _tabu_slot_hard_ok(by_id[x_id], swap[cur[x_id]], unavailable):
                return None
        return list(comp)

    def apply(comp: list[int], swap: dict[int, int]) -> None:
        moves = [(x, cur[x], swap[cur[x]]) for x in comp]
        for x, old, _new in moves:
            l = by_id[x]
            at_cls.pop((l.cls, old), None)
            for tn in l.teacher_names:
                at_t.pop((tn, old), None)
        for x, _old, new in moves:
            l = by_id[x]
            cur[x] = new
            at_cls[l.cls, new] = x
            for tn in l.teacher_names:
                at_t[tn, new] = x

    best_metrics = score_assignment(lessons, cur, max_per_day, fixed_teacher_slots)
    cur_score = best_metrics["total_score"]
    start_score = cur_score
    t0 = _t.time()
    n_try = n_ok = 0
    while _t.time() - t0 < time_limit_s:
        n_try += 1
        short = None
        if rng.random() < 0.5:
            cands = [c for c, ids in cls_lessons.items()
                     if sum(cur[i] < _SANG_END for i in ids) < min(morning_cap, len(ids))]
            if cands:
                short = rng.choice(cands)
        if short:
            pm = [i for i in cls_lessons[short] if cur[i] >= _SANG_END]
            empty = [s for s in morning_free if (short, s) not in at_cls]
            if not pm or not empty:
                continue
            lid, b = rng.choice(pm), rng.choice(empty)
        else:
            lid = rng.choice(movable)
            b = rng.randrange(N_SLOTS)
            tn = by_id[lid].teacher_names[0] if by_id[lid].teacher_names else None
            if tn and rng.random() < 0.5:
                # Dồn ngày dạy ít (1–2 tiết) của GV sang sát các tiết khác của GV ở ngày khác
                t_slots = [s for s in range(N_SLOTS) if (tn, s) in at_t] + \
                    sorted((fixed_teacher_slots or {}).get(tn, ()))
                day_cnt = sum(1 for s in t_slots if SLOT_DAY[s] == SLOT_DAY[cur[lid]])
                near = [s + dlt for s in t_slots for dlt in (-1, 1)
                        if SLOT_DAY[s] != SLOT_DAY[cur[lid]] and 0 <= s + dlt < N_SLOTS
                        and SLOT_DAY[s + dlt] == SLOT_DAY[s] and _slot_session(s + dlt) == _slot_session(s)]
                if day_cnt < 3 and near:
                    b = rng.choice(near)
            if b in FIXED_CLASS_SLOTS or b == cur[lid]:
                continue
        a = cur[lid]
        if lid in paired:
            # Dời nguyên cặp tiết đôi (a1,a2) → (b1,b2) liền kề cùng buổi
            j = partner[lid]
            a1, a2 = sorted((a, cur[j]))
            b1 = b if (b, b + 1) in _CONSECUTIVE_SET else b - 1
            b2 = b1 + 1
            if (b1, b2) not in _CONSECUTIVE_SET or {a1, a2} & {b1, b2} or b1 in FIXED_CLASS_SLOTS:
                continue
            swap = {a1: b1, a2: b2, b1: a1, b2: a2}
            comp = chain([lid, j], swap)
        else:
            swap = {a: b, b: a}
            comp = chain([lid], swap)
        if comp is None:
            continue
        apply(comp, swap)
        m = score_assignment(lessons, cur, max_per_day, fixed_teacher_slots)
        worse = any(m[k] > best_metrics[k] for k in no_worse)
        if not worse and (m["total_score"] < cur_score
                          or (m["total_score"] == cur_score and rng.random() < 0.2)):
            if m["total_score"] < cur_score:
                n_ok += 1
            cur_score, best_metrics = m["total_score"], m
            partner = find_pairs()
            paired = set(partner)
        else:
            apply(comp, swap)  # đổi ngược lại đúng chuỗi cũ để hoàn tác
    if progress_cb:
        progress_cb(f"  🔗 Kempe: {n_try} lần thử, {n_ok} cải thiện, "
                    f"score {start_score:.0f} → {cur_score:.0f} "
                    f"(sáng trống={best_metrics['morning_gaps']}, GV trống={best_metrics['teacher_gaps']})")
    return cur, best_metrics


def tabu_search(
    lessons:      list["Lesson"],
    assignment:   dict[int, int],
    unavailable:  dict[str, set[int]],
    max_per_day:  dict[str, int],
    iterations:   int = 400,
    tabu_tenure:  int = 15,
    seed:         int = 0,
    progress_cb:  "Callable[[str], None] | None" = None,
    homeroom_teachers: "set[str] | None" = None,
    fixed_teacher_slots: "dict[str, set[int]] | None" = None,
) -> tuple[dict[int, int], dict]:
    """
    Local search / Tabu Search cải thiện 1 lời giải Greedy đã khả thi.

    CHỈ di chuyển các tiết ĐƠN (không đụng tiết đã ghép đôi — giữ đúng
    chính sách tiết đôi Toán/Văn/Anh/Tin đã thống nhất với nhà trường).
    2 loại move, mỗi vòng thử ngẫu nhiên một số ứng viên (không duyệt hết
    toàn bộ không gian — quá lớn với ~1300 tiết):
      (a) dời 1 tiết đơn sang 1 slot trống hợp lệ khác cho đúng lớp/GV đó.
      (b) đổi chỗ 2 tiết đơn (khác lớp) cho nhau, hợp lệ cho CẢ HAI chiều.
    Mọi move được áp dụng theo kiểu "tạm bỏ khỏi trạng thái → kiểm tra hợp
    lệ → áp dụng thật hoặc khôi phục nguyên trạng" — tránh lỗi tính toán
    thủ công qua phép trừ tập hợp (đã từng gây bug thật trong dự án này).
    Tabu list ghi nhớ move vừa làm để không lặp lại ngay (chống dao động),
    có "aspiration": vẫn nhận move tabu nếu nó tạo ra lời giải TỐT NHẤT từ
    trước đến giờ.

    `homeroom_teachers` (nếu có): tên các GVCN — Tiết 1 Thứ 2 (Chào cờ) của
    LỚP HỌ CHỦ NHIỆM đã bị chiếm, nhưng Chào cờ không nằm trong `lessons`
    (được ghép riêng ở tầng xuất bảng) nên Tabu Search không tự biết GV đó
    bận slot 0 — phải truyền tay để không dời nhầm 1 tiết khác của chính GV
    đó vào đúng slot 0 (gây GV dạy 2 nơi cùng lúc khi ghép lại kết quả).

    Trả về (assignment tốt nhất tìm được, metrics của nó) — LUÔN so sánh
    với assignment đầu vào, không bao giờ trả về kết quả tệ hơn.
    """
    rng = random.Random(seed)

    def _log(msg: str):
        if progress_cb:
            progress_cb(msg)

    lesson_by_id = {l.id: l for l in lessons}
    paired_ids = _paired_lesson_ids(lessons, assignment)
    movable_ids = [l.id for l in lessons if l.id not in paired_ids]

    cur = dict(assignment)
    fixed_slots = set(FIXED_CLASS_SLOTS.keys())
    cls_used: dict[str, set[int]] = {}
    teacher_used: dict[str, set[int]] = {}
    for tn in (homeroom_teachers or ()):
        teacher_used.setdefault(tn, set()).update(fixed_slots)
    for l in lessons:
        s = cur[l.id]
        cls_used.setdefault(l.cls, set(fixed_slots)).add(s)
        for tn in l.teacher_names:
            teacher_used.setdefault(tn, set()).add(s)

    def try_move(lid: int, new_slot: int) -> bool:
        l = lesson_by_id[lid]
        old_slot = cur[lid]
        if new_slot == old_slot:
            return False
        if new_slot in cls_used.get(l.cls, set()):
            return False
        if any(new_slot in teacher_used.get(tn, set()) for tn in l.teacher_names):
            return False
        if not _tabu_slot_hard_ok(l, new_slot, unavailable):
            return False
        cls_used[l.cls].discard(old_slot)
        cls_used[l.cls].add(new_slot)
        for tn in l.teacher_names:
            teacher_used[tn].discard(old_slot)
            teacher_used[tn].add(new_slot)
        cur[lid] = new_slot
        return True

    def undo_move(lid: int, old_slot: int) -> None:
        l = lesson_by_id[lid]
        new_slot = cur[lid]
        cls_used[l.cls].discard(new_slot)
        cls_used[l.cls].add(old_slot)
        for tn in l.teacher_names:
            teacher_used[tn].discard(new_slot)
            teacher_used[tn].add(old_slot)
        cur[lid] = old_slot

    def try_swap(lid1: int, lid2: int) -> bool:
        l1, l2 = lesson_by_id[lid1], lesson_by_id[lid2]
        s1, s2 = cur[lid1], cur[lid2]
        if l1.cls == l2.cls:
            return False
        # Tạm bỏ cả 2 khỏi trạng thái rồi kiểm tra lại từ đầu.
        cls_used[l1.cls].discard(s1)
        for tn in l1.teacher_names:
            teacher_used[tn].discard(s1)
        cls_used[l2.cls].discard(s2)
        for tn in l2.teacher_names:
            teacher_used[tn].discard(s2)

        ok = (
            s2 not in cls_used.get(l1.cls, set())
            and all(s2 not in teacher_used.get(tn, set()) for tn in l1.teacher_names)
            and _tabu_slot_hard_ok(l1, s2, unavailable)
            and s1 not in cls_used.get(l2.cls, set())
            and all(s1 not in teacher_used.get(tn, set()) for tn in l2.teacher_names)
            and _tabu_slot_hard_ok(l2, s1, unavailable)
        )
        if ok:
            cls_used[l1.cls].add(s2)
            for tn in l1.teacher_names:
                teacher_used[tn].add(s2)
            cls_used[l2.cls].add(s1)
            for tn in l2.teacher_names:
                teacher_used[tn].add(s1)
            cur[lid1], cur[lid2] = s2, s1
        else:
            cls_used[l1.cls].add(s1)
            for tn in l1.teacher_names:
                teacher_used[tn].add(s1)
            cls_used[l2.cls].add(s2)
            for tn in l2.teacher_names:
                teacher_used[tn].add(s2)
        return ok

    cur_score = score_assignment(lessons, cur, max_per_day, fixed_teacher_slots)["total_score"]
    best_assignment = dict(cur)
    best_score = cur_score
    tabu: dict[tuple, int] = {}

    for it in range(iterations):
        sample_ids = rng.sample(movable_ids, k=min(10, len(movable_ids)))
        best_candidate = None  # (score, kind, args, sig)
        for lid in sample_ids:
            l = lesson_by_id[lid]
            old_slot = cur[lid]

            # Move loại (a): dời sang 1 slot trống hợp lệ khác.
            empty_slots = [
                s for s in range(N_SLOTS)
                if s not in cls_used.get(l.cls, set())
                and all(s not in teacher_used.get(tn, set()) for tn in l.teacher_names)
                and _tabu_slot_hard_ok(l, s, unavailable)
            ]
            rng.shuffle(empty_slots)
            for s in empty_slots[:5]:
                sig = ("move", lid, old_slot, s)
                if try_move(lid, s):
                    sc = score_assignment(lessons, cur, max_per_day, fixed_teacher_slots)["total_score"]
                    undo_move(lid, old_slot)
                    is_tabu = tabu.get(sig, -1) > it
                    if not is_tabu or sc < best_score:
                        if best_candidate is None or sc < best_candidate[0]:
                            best_candidate = (sc, "move", (lid, s), sig)

            # Move loại (b): đổi chỗ với 1 tiết đơn khác lớp.
            other_ids = rng.sample(movable_ids, k=min(8, len(movable_ids)))
            for lid2 in other_ids:
                if lid2 == lid:
                    continue
                s2 = cur[lid2]
                sig = ("swap", min(lid, lid2), max(lid, lid2))
                if try_swap(lid, lid2):
                    sc = score_assignment(lessons, cur, max_per_day, fixed_teacher_slots)["total_score"]
                    try_swap(lid, lid2)  # swap lại 2 lần = khôi phục nguyên trạng
                    is_tabu = tabu.get(sig, -1) > it
                    if not is_tabu or sc < best_score:
                        if best_candidate is None or sc < best_candidate[0]:
                            best_candidate = (sc, "swap", (lid, lid2), sig)

        if best_candidate is None:
            continue
        sc, kind, args, sig = best_candidate
        if kind == "move":
            lid, s = args
            old_slot = cur[lid]
            try_move(lid, s)
            tabu[("move", lid, s, old_slot)] = it + tabu_tenure
        else:
            lid1, lid2 = args
            try_swap(lid1, lid2)
            tabu[sig] = it + tabu_tenure

        cur_score = sc
        if cur_score < best_score:
            best_score = cur_score
            best_assignment = dict(cur)
            _log(f"  Tabu it={it}: score={cur_score:.0f} (tốt nhất mới)")

    best_metrics = score_assignment(lessons, best_assignment, max_per_day, fixed_teacher_slots)
    return best_assignment, best_metrics


# ── All-in-one ────────────────────────────────────────────────────────────────

def solve(
    lessons:      list[Lesson],
    unavailable:  dict[str, set[int]],
    max_per_day:  dict[str, int],
    time_limit_s: float = 60.0,
    n_workers:    int   = 1,  # xem ghi chú ở cpsat_improve — đa luồng bị treo trong môi trường hiện tại
    use_cpsat:    bool  = True,
    progress_cb:  Callable[[str], None] | None = None,
) -> tuple[pd.DataFrame | None, str]:
    """
    Main solve entry point (3-phase).
    Phase 1+2: Greedy + GRASP (fast, always runs — keeps best of N).
    Phase 3:   CP-SAT improvement (optional, uses remaining time).
    """
    def _log(msg: str):
        logger.info(msg)
        if progress_cb:
            progress_cb(msg)

    import time

    _log(f"  {len(lessons)} tiết | "
         f"{len({l.cls for l in lessons})} lớp | "
         f"{len({l.teacher_code for l in lessons})} GV")

    # ── Phase 1+2: Greedy + GRASP ────────────────────────────────────────
    t0 = time.time()
    _log("  Phase 1+2: Greedy + GRASP (best-of-N)...")
    assignment = greedy_solve(lessons, unavailable, max_per_day,
                              max_retries=300, progress_cb=_log)
    greedy_time = time.time() - t0

    if assignment is None:
        return None, "❌ Greedy thất bại — kiểm tra lại dữ liệu phân công."

    assignment = fill_morning_gaps(lessons, assignment, unavailable)
    assignment, n_swapped = fill_morning_gaps_swap(lessons, assignment, unavailable)
    assignment, n_swapped2 = fill_morning_gaps_swap(
        lessons, assignment, unavailable, breakable_subjects=BREAKABLE_DOUBLE_LAST_RESORT
    )
    n_swapped += n_swapped2
    if n_swapped:
        _log(f"🩹 Đổi chỗ lấp thêm {n_swapped} ô sáng còn trống.")
    metrics = score_assignment(lessons, assignment, max_per_day)
    _log(f"  GRASP xong: {greedy_time:.2f}s | "
         f"score={metrics['total_score']:.0f} "
         f"sessions={metrics['teacher_sessions']} "
         f"gaps={metrics['teacher_gaps']} "
         f"ovl={metrics['overload']}")

    # ── Phase 3: CP-SAT improvement ──────────────────────────────────────
    remaining = time_limit_s - greedy_time
    if use_cpsat and remaining > 5:
        _log("  Phase 3: CP-SAT tối ưu...")
        improved = cpsat_improve(
            lessons, assignment, unavailable, max_per_day,
            time_limit_s=min(remaining, 60),
            n_workers=n_workers,
            progress_cb=_log,
        )
        if improved is not None:
            new_metrics = score_assignment(lessons, improved, max_per_day)
            _log(f"  CP-SAT cải tiến: score {metrics['total_score']:.0f}"
                 f"→{new_metrics['total_score']:.0f} "
                 f"(sessions {metrics['teacher_sessions']}"
                 f"→{new_metrics['teacher_sessions']}, "
                 f"gaps {metrics['teacher_gaps']}"
                 f"→{new_metrics['teacher_gaps']})")
            assignment = improved
            metrics    = new_metrics
    else:
        _log("  Phase 3: bỏ qua (đủ tốt hoặc hết thời gian)")

    df = assignment_to_df(lessons, assignment)
    msg = (
        f"✅ Xếp {len(df)} tiết thành công | "
        f"sessions={metrics['teacher_sessions']} "
        f"gaps={metrics['teacher_gaps']} "
        f"ovl={metrics['overload']}"
    )
    return df, msg


def generate_timetable(
    assign_path:  str,
    teacher_path: str,
    time_limit_s: float = 60.0,
    progress_cb:  Callable[[str], None] | None = None,
    grades:       list[str] | None = None,
    use_cpsat:    bool = True,
) -> tuple[pd.DataFrame | None, str, pd.DataFrame]:
    """Full pipeline (XLSX). Returns (timetable_df, status_msg, assignments_df)."""
    def _log(msg: str):
        logger.info(msg)
        if progress_cb:
            progress_cb(msg)

    _log("📂 Đọc file phân công...")
    assignments = parse_assignments(assign_path)
    if grades:
        assignments = assignments[assignments["grade"].isin(grades)]

    _log("📚 Đọc curriculum từ DB...")
    curriculum = load_curriculum_map()

    _log("👥 Đọc danh sách GV...")
    teacher_map = load_teacher_map(teacher_path)

    _log("📋 Tạo danh sách tiết học...")
    lessons = build_lessons(assignments, teacher_map, curriculum)
    _log(f"   → {len(lessons)} tiết học")

    _log("🔒 Đọc lịch bận & max-per-day GV từ DB...")
    unavail    = load_unavailable_map(teacher_map)
    max_pd_map = load_max_per_day_map(teacher_map)

    _log("🧠 Xếp TKB (Greedy → GRASP → CP-SAT)...")
    df, status = solve(lessons, unavail, max_pd_map,
                       time_limit_s=time_limit_s,
                       use_cpsat=use_cpsat,
                       progress_cb=_log)

    return df, status, assignments


def generate_timetable_from_dfs(
    assignments_df:    pd.DataFrame,
    teachers_df:       pd.DataFrame,
    class_subjects_df: pd.DataFrame | None = None,
    time_limit_s:      float = 60.0,
    progress_cb:       Callable[[str], None] | None = None,
    grades:            list[str] | None = None,
    use_cpsat:         bool = True,
    homeroom_df:       pd.DataFrame | None = None,
    forced_double_subjects: set[str] = FORCED_DOUBLE_SUBJECTS,
    use_tabu:          bool = True,
    tabu_iterations:   int = 100,
    kempe_time_s:      float = 60.0,
    locked_timetable:  pd.DataFrame | None = None,
) -> tuple[pd.DataFrame | None, str, dict | None]:
    """
    CSV-based pipeline — Greedy realtime + trả về bg_state cho CP-SAT chạy nền.

    assignments_df    : lop, subject, ma_so
    teachers_df       : ma_so, ten, ...
    class_subjects_df : lop, subject, periods_per_week  (optional, falls về DB)
    homeroom_df       : lop, ma_so, ten_gv  (optional — gán GVCN vào dòng Chào cờ)
    locked_timetable  : các dòng TKB đã xếp cần GIỮ NGUYÊN (vd cả khối 12) — lớp
                        trong đó không xếp lại, giờ dạy của GV ở đó thành giờ bận
                        cố định, và các dòng này được ghép lại vào kết quả.

    Returns (greedy_df, greedy_msg, bg_state).
    bg_state chứa lessons+assignment+unavail+max_pd để CP-SAT tối ưu nền.
    bg_state = None nếu Greedy đã hoàn hảo hoặc use_cpsat=False.
    """
    import time as _time
    from collections import defaultdict

    def _log(msg: str):
        logger.info(msg)
        if progress_cb:
            progress_cb(msg)

    # ── Loại các môn liên lớp khỏi bộ sinh tự động (xem CROSS_CLASS_SUBJECTS) ──
    n_excluded = int(assignments_df["subject"].isin(CROSS_CLASS_SUBJECTS).sum())
    if n_excluded:
        _log(f"⏭️ Bỏ qua {n_excluded} dòng phân công các môn liên lớp "
             f"({', '.join(sorted(CROSS_CLASS_SUBJECTS))}) — xếp thủ công riêng.")
    assignments_df = assignments_df[~assignments_df["subject"].isin(CROSS_CLASS_SUBJECTS)]
    if class_subjects_df is not None and not class_subjects_df.empty:
        class_subjects_df = class_subjects_df[
            ~class_subjects_df["subject"].isin(CROSS_CLASS_SUBJECTS)
        ]

    # ── Teacher map: ma_so → ten ──────────────────────────────────────────
    _log("👥 Xây dựng bảng GV từ CSV...")
    teacher_map: dict[str, str] = dict(
        zip(teachers_df["ma_so"].astype(str), teachers_df["ten"].astype(str))
    )

    # ── Curriculum map ────────────────────────────────────────────────────
    _log("📚 Xây dựng curriculum...")
    curriculum_cls: dict[tuple[str, str], int] = {}
    if class_subjects_df is not None and not class_subjects_df.empty:
        for _, r in class_subjects_df.iterrows():
            curriculum_cls[(str(r["lop"]), str(r["subject"]))] = int(r["periods_per_week"])

    curriculum_grade = load_curriculum_map()

    # ── Build lessons (hỗ trợ team-teaching) ─────────────────────────────
    _log("📋 Tạo danh sách tiết học...")
    df_asgn = assignments_df.copy()
    df_asgn["grade"] = df_asgn["lop"].astype(str).str[:2]
    if grades:
        df_asgn = df_asgn[df_asgn["grade"].isin(grades)]
    locked_classes: set[str] = set()
    if locked_timetable is not None and not locked_timetable.empty:
        locked_classes = set(locked_timetable["lop"].astype(str))
        df_asgn = df_asgn[~df_asgn["lop"].astype(str).isin(locked_classes)]
        _log(f"🔐 Giữ nguyên {len(locked_classes)} lớp đã xếp "
             f"({len(locked_timetable)} dòng TKB) — không xếp lại.")

    # Nhóm theo (lop, subject) → phát hiện team-teaching (2+ GV cùng môn/lớp)
    asgn_grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    for _, row in df_asgn.iterrows():
        asgn_grouped[(str(row["lop"]), str(row["subject"]))].append(str(row["ma_so"]))

    lessons: list[Lesson] = []
    skipped_subjects: set[str] = set()
    n_team = 0
    lid = 0

    for (cls, subject), codes in asgn_grouped.items():
        grade = cls[:2]

        ppw = curriculum_cls.get((cls, subject), 0)
        if ppw == 0:
            ppw = curriculum_grade.get((grade, subject), 0)
        ppw -= _FIXED_PERIODS.get(subject, 0)   # trừ tiết cố định (TNHN Thứ 2)
        if ppw <= 0:
            skipped_subjects.add(subject)
            continue

        codes_tuple = tuple(codes)
        names_tuple = tuple(teacher_map.get(c, c) for c in codes)
        if len(codes) > 1:
            n_team += 1

        for occ in range(int(ppw)):
            lessons.append(Lesson(
                id=lid, cls=cls, grade=grade,
                subject=subject,
                teacher_codes=codes_tuple,
                teacher_names=names_tuple,
                occurrence=occ,
            ))
            lid += 1

    if skipped_subjects:
        _log(f"   ⚠️ Bỏ qua {len(skipped_subjects)} môn không có tiết/tuần: "
             f"{', '.join(sorted(skipped_subjects)[:5])}"
             f"{'...' if len(skipped_subjects) > 5 else ''}")
    if n_team:
        _log(f"   🤝 {n_team} lớp/môn team-teaching (2 GV dạy cùng — AVBN...)")
    _log(f"   → {len(lessons)} tiết học từ {df_asgn['lop'].nunique()} lớp")

    if not lessons:
        return None, "❌ Không có tiết học nào — kiểm tra class_subjects.csv hoặc curriculum DB.", None

    # ── Unavailability & max-per-day từ DB ───────────────────────────────
    _log("🔒 Đọc lịch bận GV từ DB...")
    unavail    = load_unavailable_map(teacher_map)
    max_pd_map = load_max_per_day_map(teacher_map)
    n_shcm = merge_dept_meeting_unavailable(unavail, teachers_df)
    _log(f"   🏫 Chặn sáng sinh hoạt chuyên môn cho {n_shcm} GV theo tổ.")

    fixed_slots: dict[str, set[int]] = defaultdict(set)
    if locked_classes:
        for _, r in locked_timetable.iterrows():
            if str(r["mon"]) == "Chào cờ":
                continue
            slot = info_to_slot(str(r["thu"]), str(r["buoi"]), int(r["tiet"]))
            if slot is None:
                continue
            for tn in str(r["ten_gv"]).split(" / "):
                if tn.strip():
                    fixed_slots[tn.strip()].add(slot)
        for tn, slots in fixed_slots.items():
            unavail.setdefault(tn, set()).update(slots)
        _log(f"   🔐 {len(fixed_slots)} GV có tiết ở lớp đã khoá → chặn trùng giờ.")

    # ── Phase 1+2: Greedy + GRASP (best-of-N, trả kết quả ngay) ────────
    _log("🏃 Phase 1+2: Greedy + GRASP (best-of-N)...")
    t0 = _time.time()
    assignment = greedy_solve(lessons, unavail, max_pd_map,
                              max_retries=300, progress_cb=_log,
                              forced_double_subjects=forced_double_subjects,
                              fixed_teacher_slots=fixed_slots)
    greedy_time = _time.time() - t0

    if assignment is None:
        return None, "❌ Greedy thất bại — kiểm tra lại dữ liệu phân công.", None

    assignment = fill_morning_gaps(lessons, assignment, unavail)
    assignment, n_swapped = fill_morning_gaps_swap(lessons, assignment, unavail)
    assignment, n_swapped2 = fill_morning_gaps_swap(
        lessons, assignment, unavail, breakable_subjects=BREAKABLE_DOUBLE_LAST_RESORT
    )
    n_swapped += n_swapped2
    if n_swapped:
        _log(f"🩹 Đổi chỗ lấp thêm {n_swapped} ô sáng còn trống.")
    metrics = score_assignment(lessons, assignment, max_pd_map, fixed_slots)
    _log(f"  ✅ GRASP xong: {greedy_time:.2f}s | "
         f"score={metrics['total_score']:.0f} "
         f"sessions={metrics['teacher_sessions']} "
         f"gaps={metrics['teacher_gaps']} "
         f"ovl={metrics['overload']}")

    homeroom_map: dict[str, str] = {}
    if homeroom_df is not None and not homeroom_df.empty:
        homeroom_map = dict(zip(homeroom_df["lop"], homeroom_df["ten_gv"]))

    # ── Tabu Search: dồn gọn thêm sau Greedy (không đụng tiết đôi) ───────
    if use_tabu and len(lessons) > 0:
        t1 = _time.time()
        assignment, tabu_metrics = tabu_search(
            lessons, assignment, unavail, max_pd_map,
            iterations=tabu_iterations, tabu_tenure=15, seed=42,
            progress_cb=None, homeroom_teachers=set(homeroom_map.values()),
            fixed_teacher_slots=fixed_slots,
        )
        tabu_time = _time.time() - t1
        if tabu_metrics["total_score"] < metrics["total_score"]:
            metrics = tabu_metrics
            _log(f"  🔁 Tabu Search: {tabu_time:.1f}s | "
                 f"score={metrics['total_score']:.0f} "
                 f"sessions={metrics['teacher_sessions']} "
                 f"gaps={metrics['teacher_gaps']} "
                 f"ovl={metrics['overload']} (đã cải thiện)")
        else:
            _log(f"  🔁 Tabu Search: {tabu_time:.1f}s | không cải thiện thêm, giữ kết quả Greedy")

    # ── Dồn buổi sáng cho GV Tin (chạy cuối, sau Tabu Search) ────────────
    # Lặp tới khi không còn dời được gì thêm — mỗi lần dời 1 tiết có thể mở
    # ra cơ hội dời tiếp 1 tiết khác (lớp vừa nhường chỗ lại tạo ô trống mới).
    total_concentrated = 0
    for _ in range(5):
        assignment, n_concentrated = concentrate_teacher_mornings(lessons, assignment, unavail)
        total_concentrated += n_concentrated
        if n_concentrated == 0:
            break
    if total_concentrated:
        metrics = score_assignment(lessons, assignment, max_pd_map, fixed_slots)
        _log(f"  🎯 Dồn buổi sáng GV Tin: đã dời {total_concentrated} tiết | "
             f"score={metrics['total_score']:.0f}")

    # ── Kempe: lấp ô sáng trống + dồn gọn lịch GV ────────────────────────
    if kempe_time_s > 0:
        _log(f"🔗 Hoán đổi chuỗi Kempe ({kempe_time_s:.0f}s): lấp sáng + dồn gọn GV...")
        assignment, metrics = kempe_improve(
            lessons, assignment, unavail, max_pd_map,
            time_limit_s=kempe_time_s, progress_cb=_log,
            fixed_teacher_slots=fixed_slots,
        )

    df = assignment_to_df(lessons, assignment, homeroom_map)
    if locked_classes:
        df = pd.concat([locked_timetable[["thu", "tiet", "buoi", "lop", "ten_gv", "mon"]], df],
                       ignore_index=True)
    msg = (f"🏃 GRASP+Tabu+Kempe xếp {len(df)} tiết | "
           f"sáng trống={metrics['morning_gaps']} "
           f"GV trống={metrics['teacher_gaps']} "
           f"buổi={metrics['teacher_sessions']} "
           f"ovl={metrics['overload']} "
           f"({greedy_time:.1f}s)")

    # ── Chuẩn bị bg_state cho CP-SAT Phase 3 chạy nền ───────────────────
    bg_state: dict | None = None
    if use_cpsat:
        bg_state = {
            "lessons":      lessons,
            "assignment":   assignment,
            "unavail":      unavail,
            "max_pd":       max_pd_map,
            "homeroom_map": homeroom_map,
            "locked_df":    locked_timetable if locked_classes else None,
        }

    return df, msg, bg_state


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys, time
    logging.basicConfig(level=logging.INFO)
    database.init_db()

    assign_path  = (sys.argv[1] if len(sys.argv) > 1
                    else "/Users/baotran/Downloads/20-08 Hoàn tất phân công K10-K11.xlsx")
    teacher_path = (sys.argv[2] if len(sys.argv) > 2
                    else "/Users/baotran/Downloads/TKB AP DUNG.xls")
    grades       = sys.argv[3].split(",") if len(sys.argv) > 3 else None
    use_cpsat    = "--no-cpsat" not in sys.argv

    t0 = time.time()
    df, msg, _ = generate_timetable(
        assign_path, teacher_path,
        time_limit_s=90,
        progress_cb=print,
        grades=grades,
        use_cpsat=use_cpsat,
    )
    print(f"\n{msg}")
    print(f"Total time: {time.time()-t0:.2f}s")
    if df is not None:
        print(f"\nSample (first 10 rows):")
        print(df.head(10).to_string())
        from collections import Counter
        print(f"\nTop teacher loads:")
        for gv, cnt in Counter(df["ten_gv"]).most_common(5):
            print(f"  {gv}: {cnt} tiết")
