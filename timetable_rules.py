# =============================================================================
# timetable_rules.py — Kiểm tra hợp lệ cho chỉnh tay TKB (kéo-thả, gợi ý ô,
# tìm lỗi toàn trường). Dùng chung bởi 3 nơi: xác nhận một lần thả (drop),
# tô màu ô hợp lệ khi nhấc tiết lên, và bộ tìm lỗi toàn trường.
#
# Không phụ thuộc Streamlit — chỉ nhận/trả pandas DataFrame và dict thuần,
# để có thể gọi độc lập (script, test) ngoài app.py.
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass, field

import re

import pandas as pd

import database
import scheduler  # tái dùng hằng số hình học lưới: DAYS_ORDER, N_SANG, N_CHIEU, AFTERNOON_ONLY_SUBJECTS

DAY_KEY = {d: i for i, d in enumerate(scheduler.DAYS_ORDER)}


@dataclass(frozen=True)
class Issue:
    kind: str          # "teacher_double_booking" | "class_double_booking" |
                        # "teacher_unavailable" | "curriculum_mismatch"
    severity: str       # "error" | "warning"
    thu: str | None
    tiet: int | None
    buoi: str | None
    lop: str | None
    ten_gv: str | None
    mon: str | None
    message: str
    row_ids: tuple[int, ...] = field(default_factory=tuple)


def expand_team_teaching(timetable_df: pd.DataFrame) -> pd.DataFrame:
    """Tách các dòng team-teaching 'GV1 / GV2' thành nhiều dòng, mỗi dòng một
    GV, giữ nguyên các cột khác (kể cả id). Dùng chung để không copy-paste
    logic này ở nhiều nơi (trước đây lặp lại trong app.check_conflicts)."""
    if timetable_df.empty:
        return timetable_df
    out = timetable_df.copy()
    out["ten_gv"] = out["ten_gv"].astype(str).str.split(" / ")
    out = out.explode("ten_gv")
    out["ten_gv"] = out["ten_gv"].str.strip()
    return out[out["ten_gv"] != ""]


def all_slots(timetable_df: pd.DataFrame) -> list[tuple[str, int, str]]:
    """Danh sách toàn bộ (thu, tiet, buoi) trong tuần. Ngày lấy từ dữ liệu
    thật đang có (không hard-code) — nếu Thứ 7 từng xuất hiện trong DB, vẫn
    được liệt kê ở đây; lưới kéo-thả v1 chỉ hiển thị Thứ 2–Thứ 6 để nhất
    quán với phần còn lại của app, nhưng việc kiểm tra hợp lệ vẫn đúng cho
    mọi ngày có trong dữ liệu."""
    if timetable_df.empty or "thu" not in timetable_df.columns:
        days = list(scheduler.DAYS_ORDER)
    else:
        seen = set(timetable_df["thu"].astype(str))
        days = sorted(seen, key=lambda d: DAY_KEY.get(d, 999))
    slots: list[tuple[str, int, str]] = []
    for d in days:
        for p in range(1, scheduler.N_SANG + 1):
            slots.append((d, p, "Sáng"))
        for p in range(1, scheduler.N_CHIEU + 1):
            slots.append((d, p, "Chiều"))
    return slots


class TimetableIndex:
    """Chỉ mục tra cứu nhanh, dựng lại từ live DataFrame mỗi lần render
    (rẻ: toàn trường ~2000 dòng)."""

    def __init__(self, timetable_df: pd.DataFrame, teacher_prefs: list[dict] | None = None,
                 groups: pd.DataFrame | None = None):
        self.rows_by_id: dict[int, dict] = {}
        # Nhóm tự chọn (database.get_group_periods): ô lớp có HS đang học nhóm / GV đang dạy nhóm
        self.class_groups: dict[tuple[str, str, int, str], set[str]] = {}
        self.teacher_groups: dict[tuple[str, str, int, str], set[str]] = {}
        if groups is not None and not groups.empty:
            for g in groups.itertuples():
                self.class_groups.setdefault((g.lop, g.thu, int(g.tiet), g.buoi), set()).add(g.ma_nhom)
                self.teacher_groups.setdefault((g.gv, g.thu, int(g.tiet), g.buoi), set()).add(g.ma_nhom)
        self.by_class_slot: dict[tuple[str, str, int, str], dict] = {}
        self.by_teacher_slot: dict[tuple[str, str, int, str], list[dict]] = {}
        self.teacher_unavailable: dict[str, set[tuple[str, int, str]]] = {}

        if not timetable_df.empty:
            for _, r in timetable_df.iterrows():
                row = r.to_dict()
                if "id" in row and pd.notna(row["id"]):
                    self.rows_by_id[int(row["id"])] = row
                self.by_class_slot[(row["lop"], row["thu"], row["tiet"], row["buoi"])] = row
            expanded = expand_team_teaching(timetable_df)
            for _, r in expanded.iterrows():
                row = r.to_dict()
                key = (row["ten_gv"], row["thu"], row["tiet"], row["buoi"])
                self.by_teacher_slot.setdefault(key, []).append(row)

        for ps in (teacher_prefs or []):
            full = database.get_teacher_pref(ps["ma_so"])
            if not full:
                continue
            busy = {(s["day"], s["period"], s["buoi"]) for s in full.get("unavailable_slots", [])}
            if busy:
                self.teacher_unavailable[full["ten"]] = busy


_GROUP_CODE_RE = re.compile(r"^(CT|TC|TN)\d\d-[A-Z]+-\d+( / (CT|TC|TN)\d\d-[A-Z]+-\d+)*$")


def is_shared_subject(mon) -> bool:
    """Môn học chung nhiều lớp cùng giờ (ô ghi mã nhóm tự chọn, hoặc GDTC theo nhóm):
    1 GV dạy nhiều lớp cùng tiết là đúng, không phải trùng giờ."""
    m = str(mon or "")
    return m == "GDTC" or bool(_GROUP_CODE_RE.match(m))


def build_index(timetable_df: pd.DataFrame, teacher_prefs: list[dict] | None = None,
                groups: pd.DataFrame | None = None) -> TimetableIndex:
    return TimetableIndex(timetable_df, teacher_prefs, groups)


def is_move_legal(
    idx: TimetableIndex, *, moving_id: int | None, lop: str, ten_gv: str, mon: str,
    target_thu: str, target_tiet: int, target_buoi: str,
    sibling_classes: frozenset[str] = frozenset(),
) -> tuple[bool, list[str]]:
    """Kiểm: đặt (lop, ten_gv, mon) vào ô đích có hợp lệ ngay bây giờ không,
    bỏ qua chính ô mà moving_id đang chiếm (để một tiết thả lại đúng ô cũ
    không tự chặn chính nó).

    `sibling_classes`: các lớp khác CÙNG chung 1 mã nhóm với `lop` (nhóm
    tự chọn liên lớp — 1 GV dạy chung nhiều lớp trong 1 buổi). GV đã dạy
    1 trong các lớp này ở đúng ô đích thì KHÔNG bị coi là xung đột (đó
    chính là buổi học chung, không phải GV dạy 2 nơi cùng lúc)."""
    reasons: list[str] = []

    occ = idx.by_class_slot.get((lop, target_thu, target_tiet, target_buoi))
    if occ is not None and int(occ.get("id", -1)) != moving_id:
        reasons.append(
            f"Lớp {lop} đã có tiết ({occ['mon']} — {occ['ten_gv']}) vào "
            f"{target_thu} tiết {target_tiet} {target_buoi}."
        )

    grp = idx.class_groups.get((lop, target_thu, target_tiet, target_buoi))
    if grp:
        reasons.append(f"Lớp {lop} có HS học nhóm {', '.join(sorted(grp))} vào "
                       f"{target_thu} tiết {target_tiet} {target_buoi}.")

    reasons += teacher_slot_reasons(
        idx, moving_id=moving_id, ten_gv=ten_gv, mon=mon, target_thu=target_thu,
        target_tiet=target_tiet, target_buoi=target_buoi, sibling_classes=sibling_classes,
    )
    return (len(reasons) == 0, reasons)


def teacher_slot_reasons(
    idx: TimetableIndex, *, moving_id: int | None, ten_gv: str, mon: str,
    target_thu: str, target_tiet: int, target_buoi: str,
    sibling_classes: frozenset[str] = frozenset(),
) -> list[str]:
    """Lý do GV/môn KHÔNG thể dạy ở ô đích (không xét lớp đang trống hay không)."""
    reasons: list[str] = []
    for tn in [t.strip() for t in str(ten_gv).split(" / ") if t.strip()]:
        for other in idx.by_teacher_slot.get((tn, target_thu, target_tiet, target_buoi), []):
            if int(other.get("id", -1)) == moving_id:
                continue
            if other.get("lop") in sibling_classes:
                continue
            if other.get("mon") == mon and is_shared_subject(mon):
                continue
            reasons.append(f"GV {tn} đã dạy lớp {other['lop']} cùng tiết đó.")
        if (target_thu, target_tiet, target_buoi) in idx.teacher_unavailable.get(tn, set()):
            reasons.append(f"GV {tn} đã báo bận tiết {target_thu} — tiết {target_tiet} — {target_buoi}.")
        tg = idx.teacher_groups.get((tn, target_thu, target_tiet, target_buoi))
        if tg:
            reasons.append(f"GV {tn} dạy nhóm {', '.join(sorted(tg))} cùng tiết đó.")

    if mon in scheduler.AFTERNOON_ONLY_SUBJECTS and target_buoi != "Chiều":
        reasons.append(f"Môn {mon} chỉ được xếp buổi Chiều.")
    return reasons


def legal_targets_for_lesson(
    idx: TimetableIndex, *, moving_id: int, lop: str, ten_gv: str, mon: str,
    slots: list[tuple[str, int, str]],
    sibling_classes: frozenset[str] = frozenset(),
) -> list[tuple[str, int, str]]:
    """Liệt kê mọi ô trong `slots` mà is_move_legal() chấp nhận cho tiết này."""
    return [
        (thu, tiet, buoi) for (thu, tiet, buoi) in slots
        if is_move_legal(idx, moving_id=moving_id, lop=lop, ten_gv=ten_gv, mon=mon,
                          target_thu=thu, target_tiet=tiet, target_buoi=buoi,
                          sibling_classes=sibling_classes)[0]
    ]


def find_school_errors(
    timetable_df: pd.DataFrame,
    teacher_prefs: list[dict] | None = None,
    curriculum: dict[tuple[str, str], int] | None = None,
    class_subjects_df: pd.DataFrame | None = None,
) -> list[Issue]:
    """Chạy toàn bộ kiểm tra trên các tiết ĐÃ XẾP hiện có (không phải giả
    định), trả về danh sách Issue. Dùng cho tab "Tìm lỗi toàn trường"."""
    issues: list[Issue] = []
    if timetable_df.empty:
        return issues

    # Nhóm tự chọn liên lớp (vd AVBN, STEM...) — nhiều lớp CHUNG 1 mã nhóm
    # học cùng 1 GV, cùng giờ là ĐÚNG (1 buổi học chung), không phải GV dạy
    # 2 nơi cùng lúc. Tra (lop, subject) -> mã nhóm để loại trừ đúng các
    # trường hợp này khỏi cảnh báo "GV trùng tiết".
    group_map = database.get_elective_group_map()
    group_of = {(r["lop"], r["subject"]): r["ma_nhom"]
                for _, r in group_map.iterrows()} if not group_map.empty else {}

    # 1) GV dạy trùng tiết ở nhiều lớp — thay thế app.check_conflicts() cũ.
    #    Nhóm học chung (cùng 1 mã nhóm, hoặc GDTC) ở nhiều lớp cùng giờ là đúng; chỉ báo
    #    khi GV phải dạy ≥2 việc khác nhau cùng lúc. Ô ghép "MÃ1 / MÃ2" có GV "GV1 / GV2"
    #    thì mã thứ i thuộc GV thứ i.
    slot_use: dict[tuple, dict[str, list]] = {}
    for _, r in timetable_df[timetable_df["mon"] != "Chào cờ"].iterrows():
        mon = str(r["mon"]); tns = [t.strip() for t in str(r["ten_gv"]).split(" / ") if t.strip()]
        codes = mon.split(" / ")
        for i, tn in enumerate(tns):
            if mon == "GDTC":
                key = "GDTC"
            elif is_shared_subject(mon):
                key = codes[i] if len(codes) == len(tns) else mon
            else:
                g = group_of.get((r["lop"], mon))
                key = f"nhóm {g}" if g else f"{r['lop']} {mon}"
            slot_use.setdefault((r["thu"], r["tiet"], r["buoi"], tn), {}).setdefault(key, []).append(r)
    for (thu, tiet, buoi, tn), uses in slot_use.items():
        if len(uses) <= 1:
            continue
        rows = [x for v in uses.values() for x in v]
        classes = sorted({x["lop"] for x in rows})
        issues.append(Issue(
            "teacher_double_booking", "error", thu, tiet, buoi, None, tn, rows[0]["mon"],
            f"GV {tn} dạy {len(uses)} việc cùng tiết {thu} — tiết {tiet} — {buoi}: "
            f"{'; '.join(sorted(uses))} (lớp {', '.join(classes)}).",
            tuple(int(x["id"]) for x in rows if "id" in x and pd.notna(x["id"])),
        ))

    # 2) Một lớp có nhiều tiết trùng giờ (scheduler.py kiểm khi sinh, nhưng
    #    chưa ai kiểm lại trên lưới đã chỉnh tay).
    for (thu, tiet, buoi, lop), grp in timetable_df.groupby(["thu", "tiet", "buoi", "lop"]):
        if len(grp) > 1:
            issues.append(Issue(
                "class_double_booking", "error", thu, tiet, buoi, lop, None, None,
                f"Lớp {lop} có {len(grp)} tiết trùng vào {thu} — tiết {tiet} — {buoi}.",
                tuple(int(i) for i in grp["id"]) if "id" in grp.columns else (),
            ))

    # 3) GV báo bận nhưng vẫn được xếp dạy.
    groups = database.get_group_periods()
    idx = build_index(timetable_df, teacher_prefs, groups)
    for tn, busy in idx.teacher_unavailable.items():
        for (t_ten, thu, tiet, buoi), rows in idx.by_teacher_slot.items():
            if t_ten != tn or (thu, tiet, buoi) not in busy:
                continue
            for r in rows:
                issues.append(Issue(
                    "teacher_unavailable", "warning", thu, tiet, buoi, r["lop"], tn, r["mon"],
                    f"GV {tn} báo bận tiết {thu} — tiết {tiet} — {buoi} nhưng vẫn được xếp dạy lớp {r['lop']}.",
                    (int(r["id"]),) if "id" in r and pd.notna(r["id"]) else (),
                ))

    # 3b) Tiết chính khóa trùng giờ nhóm tự chọn (HS của lớp hoặc GV đang ở nhóm).
    for _, r in timetable_df[timetable_df["mon"] != "Chào cờ"].iterrows():
        key = (r["thu"], int(r["tiet"]), r["buoi"])
        cg = idx.class_groups.get((r["lop"], *key))
        tg = set().union(*(idx.teacher_groups.get((t.strip(), *key), set())
                           for t in str(r["ten_gv"]).split(" / ")))
        if cg or tg:
            issues.append(Issue(
                "group_conflict", "error", r["thu"], int(r["tiet"]), r["buoi"], r["lop"], r["ten_gv"], r["mon"],
                f"Lớp {r['lop']} — {r['mon']} ({r['ten_gv']}) trùng giờ nhóm tự chọn "
                f"{', '.join(sorted((cg or set()) | tg))} vào {r['thu']} tiết {r['tiet']} {r['buoi']}.",
                (int(r["id"]),) if "id" in r and pd.notna(r["id"]) else (),
            ))

    # 4) Số tiết/tuần thực tế lệch so với quy định — ưu tiên số tiết riêng của
    #    từng lớp (class_subjects_df, vì một số lớp có chương trình khác nhau,
    #    ví dụ lớp song ngữ/tăng cường), chỉ dùng chương trình chung
    #    (curriculum) làm phương án dự phòng khi lớp đó không có số tiết riêng.
    per_class_expected: dict[tuple[str, str], int] = {}
    if class_subjects_df is not None and not class_subjects_df.empty:
        for _, cs in class_subjects_df.iterrows():
            per_class_expected[(cs["lop"], cs["subject"])] = int(cs["periods_per_week"])

    if curriculum or per_class_expected:
        actual = timetable_df.groupby(["lop", "mon"]).size().rename("actual").reset_index()
        for _, row in actual.iterrows():
            grade = str(row["lop"])[:2]
            expected = per_class_expected.get((row["lop"], row["mon"]))
            source = "lớp"
            if expected is None:
                expected = curriculum.get((grade, row["mon"])) if curriculum else None
                source = "khối"
            if expected is not None and int(row["actual"]) != int(expected):
                issues.append(Issue(
                    "curriculum_mismatch", "warning", None, None, None, row["lop"], None, row["mon"],
                    f"Lớp {row['lop']} — môn {row['mon']}: đang có {row['actual']} tiết/tuần, "
                    f"quy định theo {source} là {expected}.",
                    (),
                ))

    return issues


def issues_to_dataframe(issues: list[Issue]) -> pd.DataFrame:
    """Chuyển list[Issue] thành DataFrame để hiển thị trong st.dataframe."""
    if not issues:
        return pd.DataFrame(columns=["Mức độ", "Loại", "Thứ", "Tiết", "Buổi", "Lớp", "GV", "Môn", "Mô tả"])
    kind_label = {
        "teacher_double_booking": "GV trùng tiết",
        "class_double_booking":   "Lớp trùng tiết",
        "teacher_unavailable":    "GV báo bận",
        "group_conflict":         "Trùng giờ nhóm tự chọn",
        "curriculum_mismatch":    "Lệch chương trình",
    }
    return pd.DataFrame([{
        "Mức độ": "🔴 Lỗi" if i.severity == "error" else "🟡 Cảnh báo",
        "Loại":   kind_label.get(i.kind, i.kind),
        "Thứ":    i.thu, "Tiết": i.tiet, "Buổi": i.buoi,
        "Lớp":    i.lop, "GV":   i.ten_gv, "Môn": i.mon,
        "Mô tả":  i.message,
    } for i in issues])
