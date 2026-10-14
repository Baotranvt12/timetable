# =============================================================================
# manual_edit.py — Module thống nhất "Xếp TKB thủ công".
#
# Gồm 5 phần con trong CÙNG một tab (không rải rác):
#   1. 🗓️ Kéo-thả theo Lớp        — di chuyển 1 tiết học (v1: UI chọn-ô,
#      sẽ nối thêm component kéo-thả HTML5 thật ở bước sau; luồng ghi dữ
#      liệu/validate/log dùng chung nên không phải viết lại khi thêm kéo-thả).
#   2. 🧑‍🏫 GV — Trợ lý & TKB       — xem TKB 1 GV + bảng tải/gợi ý + hỏi AI.
#   3. ⚠️ Tìm lỗi toàn trường       — thay thế tab "Xung đột" cũ.
#   4. 🕒 Lịch sử thay đổi          — mọi thay đổi tay đều được ghi lại.
#   5. ➕ Môn tự chọn (nhóm)        — gán tay môn tự chọn xuyên lớp (AVBN,
#      STEM, THQT, thể thao, ôn TN...) cho từng lớp cụ thể — các môn này có
#      quá ít GV để mọi lớp trong khối cùng học (1 GV không thể dạy 21 lớp
#      × 2 tiết/tuần = 42 tiết trong khi 1 tuần chỉ có 40 tiết), nên người
#      dùng tự chọn lớp nào được học môn nào theo đúng sĩ số nhóm thực tế,
#      thay vì hệ thống tự gán hết cho mọi lớp (sẽ vỡ ràng buộc GV).
# =============================================================================

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pandas as pd
import streamlit as st

import ai_assistant
import database
import export_utils
import scheduler
import timetable_rules
from components.tkb_grid import tkb_grid


def render_manual_edit_tab(
    teachers: pd.DataFrame, classes: pd.DataFrame,
    timetable: pd.DataFrame, all_issues: list[timetable_rules.Issue],
) -> None:
    st.subheader("✏️ Xếp TKB thủ công")
    st.caption(
        "Kéo-thả tiết học, hệ thống tự kiểm tra hợp lệ, gợi ý ô có thể xếp, "
        "và ghi lại mọi thay đổi."
    )

    sub1, sub2, sub3, sub4, sub5 = st.tabs([
        "🗓️ Kéo-thả theo Lớp",
        "🧑‍🏫 GV — Trợ lý & TKB",
        "⚠️ Tìm lỗi toàn trường",
        "🕒 Lịch sử thay đổi",
        "➕ Môn tự chọn (nhóm)",
    ])
    with sub1:
        _render_drag_drop_section(classes, timetable)
    with sub2:
        _render_teacher_view_and_assistant(teachers, timetable, all_issues)
    with sub3:
        _render_error_finder_section(all_issues)
    with sub5:
        _render_elective_assignment_section(teachers, classes, timetable)
    with sub4:
        _render_change_log_section(timetable)


# --------------------------------------------------------------------------- #
# 1) Kéo-thả theo Lớp                                                        #
# --------------------------------------------------------------------------- #

def _slot_label(thu: str, tiet: int, buoi: str) -> str:
    return f"{thu} — Tiết {tiet} ({buoi})"


def _rules_index(timetable: pd.DataFrame) -> "timetable_rules.TimetableIndex":
    """Chỉ mục kiểm tra hợp lệ: TKB + lịch bận GV + lịch nhóm tự chọn (né các ô đã có nhóm)."""
    return timetable_rules.build_index(timetable, database.get_all_teacher_prefs(),
                                       database.get_group_periods())


def _get_unplaced_lessons(lop: str, timetable: pd.DataFrame) -> pd.DataFrame:
    """Trả về DataFrame [subject, ten_gv, ma_so, ma_nhom, missing] — các môn
    của lớp (từ class_subjects, gồm cả môn tự chọn/nhóm vừa gán) còn THIẾU
    tiết trong TKB hiện tại (so periods_per_week với số tiết đã thực sự
    xếp). Đây chính là "khay tiết chưa xếp" để kéo vào lưới. `ma_nhom` (nếu
    môn này được gán qua công cụ mã nhóm) chỉ để hiển thị."""
    cls_subj = database.get_class_subjects()
    needed = cls_subj[cls_subj["lop"] == lop]
    if needed.empty:
        return pd.DataFrame(columns=["subject", "ten_gv", "ma_so", "ma_nhom", "missing"])

    asgn = database.get_assignments()
    asgn_lop = asgn[asgn["lop"] == lop]
    teachers = database.get_teachers()
    maso_to_ten = dict(zip(teachers["ma_so"], teachers["ten"]))
    group_map = database.get_elective_group_map()
    group_map_lop = group_map[group_map["lop"] == lop]

    placed = (timetable[timetable["lop"] == lop].groupby("mon").size()
              if not timetable.empty else pd.Series(dtype=int))

    rows = []
    for _, r in needed.iterrows():
        subj = r["subject"]
        if subj in scheduler.CROSS_CLASS_SUBJECTS:  # đã xếp theo lịch nhóm tự chọn
            continue
        want = int(r["periods_per_week"])
        have = int(placed.get(subj, 0))
        missing = want - have
        if missing <= 0:
            continue
        a = asgn_lop[asgn_lop["subject"] == subj]
        ma_so = a["ma_so"].iloc[0] if not a.empty else ""
        ten_gv = maso_to_ten.get(ma_so, ma_so)
        g = group_map_lop[group_map_lop["subject"] == subj]
        ma_nhom = g["ma_nhom"].iloc[0] if not g.empty else ""
        rows.append({"subject": subj, "ten_gv": ten_gv, "ma_so": ma_so,
                     "ma_nhom": ma_nhom, "missing": missing})
    return pd.DataFrame(rows)


def _subject_label(subject: str, ma_nhom: str) -> str:
    return f"{subject} ({ma_nhom})" if ma_nhom else subject


def _get_group_map(lop: str) -> dict[str, str]:
    """{subject: ma_nhom} cho 1 lớp — dùng để gắn nhãn mã nhóm khi hiển thị
    môn tự chọn ở bất kỳ đâu (lưới, khay chưa xếp, chọn-ô)."""
    gm = database.get_elective_group_map()
    gm_lop = gm[gm["lop"] == lop]
    return dict(zip(gm_lop["subject"], gm_lop["ma_nhom"]))


def _get_group_siblings(lop: str, subject: str) -> frozenset[str]:
    """Các lớp KHÁC (không tính `lop`) cùng chung 1 mã nhóm với `lop` cho
    `subject` — đây là 1 buổi học chung nhiều lớp (cùng GV, cùng giờ), nên
    khi xếp cho `lop` phải tự động xếp CÙNG lúc cho các lớp này, và GV
    trùng giữa các lớp anh em này không bị coi là xung đột."""
    gm = database.get_elective_group_map()
    my = gm[(gm["lop"] == lop) & (gm["subject"] == subject)]
    if my.empty:
        return frozenset()
    ma_nhom = my.iloc[0]["ma_nhom"]
    if not ma_nhom:
        return frozenset()
    siblings = gm[(gm["ma_nhom"] == ma_nhom) & (gm["lop"] != lop)]
    return frozenset(siblings["lop"].unique())


def _apply_insert(subject: str, ten_gv: str, lop: str,
                  target_thu: str, target_tiet: int, target_buoi: str) -> None:
    """Xếp 1 tiết CHƯA TỪNG có trong TKB (vd môn tự chọn/nhóm vừa gán) vào 1
    ô cụ thể — tạo dòng mới thay vì di chuyển dòng có sẵn.

    Nếu môn này là môn NHÓM (được gán qua mã nhóm và có các lớp anh em chung
    mã nhóm — xem `_get_group_siblings`), đây là 1 buổi học chung nhiều lớp
    (cùng GV, cùng giờ), nên tự động xếp CÙNG lúc cho mọi lớp anh em còn
    thiếu tiết này vào ĐÚNG ô vừa chọn, không bắt người dùng kéo từng lớp
    một — và không coi GV trùng giữa các lớp anh em là xung đột."""
    tt = st.session_state["timetable"]
    siblings = _get_group_siblings(lop, subject)
    idx = _rules_index(tt)

    ok, reasons = timetable_rules.is_move_legal(
        idx, moving_id=-1, lop=lop, ten_gv=ten_gv, mon=subject,
        target_thu=target_thu, target_tiet=target_tiet, target_buoi=target_buoi,
        sibling_classes=siblings,
    )
    if not ok:
        st.toast("❌ Không thể xếp: " + "; ".join(reasons))
        return

    # Chỉ tự động xếp cho các lớp anh em CÒN THIẾU đúng môn này (đã xếp đủ
    # rồi thì bỏ qua — không tạo dư tiết).
    todo_siblings = []
    for sib in siblings:
        unplaced_sib = _get_unplaced_lessons(sib, tt)
        if not unplaced_sib.empty and (unplaced_sib["subject"] == subject).any():
            ok_sib, reasons_sib = timetable_rules.is_move_legal(
                idx, moving_id=-1, lop=sib, ten_gv=ten_gv, mon=subject,
                target_thu=target_thu, target_tiet=target_tiet, target_buoi=target_buoi,
                sibling_classes=siblings | {lop},
            )
            if not ok_sib:
                st.toast(f"❌ Không thể xếp cho lớp anh em {sib}: " + "; ".join(reasons_sib))
                return
            todo_siblings.append(sib)

    batch_id = None
    placed_classes = [lop] + todo_siblings
    for cls in placed_classes:
        new_id = database.insert_timetable_row(target_thu, target_tiet, target_buoi, cls, ten_gv, subject)
        after = {"id": new_id, "thu": target_thu, "tiet": target_tiet, "buoi": target_buoi,
                 "lop": cls, "ten_gv": ten_gv, "mon": subject}
        note = ("Xếp tiết chưa xếp (nhóm tự chọn) thủ công" if cls == lop
                else f"Tự động xếp cùng nhóm với lớp {lop}")
        batch_id = database.log_change("create", new_id, None, after, note=note, batch_id=batch_id)

    st.session_state["timetable"] = database.get_timetable()
    if todo_siblings:
        st.toast(
            f"✅ Đã xếp {subject} — {ten_gv} cho {len(placed_classes)} lớp cùng nhóm "
            f"({', '.join(placed_classes)}) vào {_slot_label(target_thu, target_tiet, target_buoi)}."
        )
    else:
        st.toast(f"✅ Đã xếp {subject} — {ten_gv} vào {_slot_label(target_thu, target_tiet, target_buoi)}.")
    st.rerun()


def _apply_move(row_id: int, target_thu: str, target_tiet: int, target_buoi: str) -> None:
    """Xác nhận hợp lệ rồi ghi DB + change_log + refetch session_state.
    Dùng chung cho cả UI chọn-ô hiện tại và component kéo-thả sau này."""
    tt = st.session_state["timetable"]
    row = tt[tt["id"] == row_id]
    if row.empty:
        st.toast("❌ Tiết học không còn tồn tại (đã bị thay đổi ở nơi khác).")
        return
    r = row.iloc[0]

    idx = _rules_index(tt)
    ok, reasons = timetable_rules.is_move_legal(
        idx, moving_id=int(r["id"]), lop=r["lop"], ten_gv=r["ten_gv"], mon=r["mon"],
        target_thu=target_thu, target_tiet=target_tiet, target_buoi=target_buoi,
    )
    if not ok:
        st.toast("❌ Không thể xếp: " + "; ".join(reasons))
        return

    before = {k: r[k] for k in ["id", "thu", "tiet", "buoi", "lop", "ten_gv", "mon"]}
    database.move_timetable_row(int(r["id"]), target_thu, target_tiet, target_buoi, r["lop"])
    after = {**before, "thu": target_thu, "tiet": target_tiet, "buoi": target_buoi}
    database.log_change("move", int(r["id"]), before, after,
                         note="Xếp TKB thủ công")
    st.session_state["timetable"] = database.get_timetable()
    st.toast(
        f"✅ Đã chuyển {r['mon']} — {r['ten_gv']} sang "
        f"{_slot_label(target_thu, target_tiet, target_buoi)}."
    )
    st.rerun()


def _build_grid_data(sel_lop: str, sub: pd.DataFrame, idx: "timetable_rules.TimetableIndex",
                      slots: list[tuple[str, int, str]], unplaced: pd.DataFrame,
                      group_map: dict[str, str]) -> dict:
    days = sorted({s[0] for s in slots}, key=lambda d: timetable_rules.DAY_KEY.get(d, 999))
    periods = {"Sáng": list(range(1, scheduler.N_SANG + 1)),
               "Chiều": list(range(1, scheduler.N_CHIEU + 1))}
    cells = []
    legal_targets: dict[str, list] = {}
    for _, r in sub.iterrows():
        rid, tiet = int(r["id"]), int(r["tiet"])
        cells.append({
            "thu": r["thu"], "tiet": tiet, "buoi": r["buoi"],
            "lesson": {"id": rid, "mon": _subject_label(r["mon"], group_map.get(r["mon"], "")),
                      "ten_gv": r["ten_gv"]},
        })
        legal = timetable_rules.legal_targets_for_lesson(
            idx, moving_id=rid, lop=r["lop"], ten_gv=r["ten_gv"], mon=r["mon"], slots=slots,
        )
        legal = [s for s in legal if s != (r["thu"], tiet, r["buoi"])]
        legal_targets[str(rid)] = [list(s) for s in legal]

    # Ô có HS của lớp đang học nhóm tự chọn: hiện mã nhóm, không thả tiết vào được
    by_slot = {(c["thu"], c["tiet"], c["buoi"]): c for c in cells}
    for (lop, thu, tiet, buoi), codes in idx.class_groups.items():
        if lop != sel_lop:
            continue
        text = ", ".join(sorted(export_utils.short_group(c) for c in codes))
        if (thu, tiet, buoi) in by_slot:
            by_slot[(thu, tiet, buoi)]["group"] = text
        else:
            cells.append({"thu": thu, "tiet": tiet, "buoi": buoi, "group": text})

    # Khay tiết CHƯA XẾP (vd môn tự chọn/nhóm vừa gán, chưa có ngày/tiết) —
    # mỗi môn còn thiếu N tiết thì tạo N thẻ "new_<subject>_<i>" riêng để kéo
    # từng cái một, id giả (không phải id thật trong bảng timetable).
    unplaced_cards = []
    for _, r in unplaced.iterrows():
        siblings = _get_group_siblings(sel_lop, r["subject"])
        for i in range(int(r["missing"])):
            uid = f"new_{r['subject']}_{i}"
            legal = timetable_rules.legal_targets_for_lesson(
                idx, moving_id=-1, lop=sel_lop, ten_gv=r["ten_gv"], mon=r["subject"], slots=slots,
                sibling_classes=siblings,
            )
            legal_targets[uid] = [list(s) for s in legal]
            unplaced_cards.append({
                "id": uid, "mon": _subject_label(r["subject"], r.get("ma_nhom", "")),
                "ten_gv": r["ten_gv"],
            })

    return {
        "lop": sel_lop, "days": days, "periods": periods,
        "cells": cells, "legal_targets": legal_targets,
        "unplaced": unplaced_cards,
        "free_teachers": _free_teachers_by_slot(sel_lop, idx, slots, unplaced),
        "nonce": int(time.time() * 1000),
    }


def _free_teachers_by_slot(sel_lop: str, idx: "timetable_rules.TimetableIndex",
                           slots: list[tuple[str, int, str]],
                           unplaced: pd.DataFrame) -> dict[str, list[dict]]:
    """Gợi ý khi kéo-thả: với mỗi ô, các GV của lớp đang rảnh và được phép dạy
    môn của họ ở ô đó. GV có môn còn thiếu tiết xếp trước."""
    asgn = database.get_assignments()
    asgn_lop = asgn[asgn["lop"] == sel_lop]
    if asgn_lop.empty:
        return {}
    teachers = database.get_teachers()
    ten_of = dict(zip(teachers["ma_so"], teachers["ten"]))
    tat_of = dict(zip(teachers["ma_so"], teachers["ten_tat"]))
    missing_of = dict(zip(unplaced["subject"], unplaced["missing"])) if not unplaced.empty else {}

    options = []
    for subj, grp in asgn_lop.groupby("subject"):
        codes = [c for c in grp["ma_so"] if c in ten_of]
        if not codes:
            continue
        options.append({
            "ten_gv": " / ".join(ten_of[c] for c in codes),
            "tat": " / ".join(str(tat_of.get(c) or ten_of[c]) for c in codes),
            "mon": subj,
            "missing": int(missing_of.get(subj, 0)),
            "siblings": _get_group_siblings(sel_lop, subj) | {sel_lop},
        })

    result: dict[str, list[dict]] = {}
    for thu, tiet, buoi in slots:
        free = [
            {k: o[k] for k in ("ten_gv", "tat", "mon", "missing")}
            for o in options
            if not timetable_rules.teacher_slot_reasons(
                idx, moving_id=None, ten_gv=o["ten_gv"], mon=o["mon"],
                target_thu=thu, target_tiet=tiet, target_buoi=buoi,
                sibling_classes=o["siblings"],
            )
        ]
        free.sort(key=lambda f: (-f["missing"], f["mon"]))
        result[f"{thu}|{tiet}|{buoi}"] = free
    return result


def _render_click_fallback(sel_lop: str, sub: pd.DataFrame, timetable: pd.DataFrame,
                           unplaced: pd.DataFrame) -> None:
    """Cách chọn-ô bằng danh sách — dùng cùng luồng _apply_move/_apply_insert
    như kéo-thả, hữu ích khi không thể thao tác chuột hoặc muốn thao tác
    nhanh bằng bàn phím. Gồm cả tiết đã có (di chuyển) VÀ tiết chưa xếp
    (xếp mới — vd môn tự chọn/nhóm vừa gán)."""
    group_map = _get_group_map(sel_lop)
    existing_options = {
        int(r["id"]): f"{_slot_label(r['thu'], r['tiet'], r['buoi'])} — "
                      f"{_subject_label(r['mon'], group_map.get(r['mon'], ''))} ({r['ten_gv']})"
        for _, r in sub.iterrows()
    }
    new_options: dict[str, str] = {}
    for _, r in unplaced.iterrows():
        for i in range(int(r["missing"])):
            label = _subject_label(r["subject"], r.get("ma_nhom", ""))
            new_options[f"new_{r['subject']}_{i}"] = f"🆕 CHƯA XẾP — {label} ({r['ten_gv']})"

    all_options = {**new_options, **existing_options}
    if not all_options:
        st.info(f"Lớp {sel_lop} chưa có tiết học hoặc môn tự chọn nào cần xếp.")
        return

    sel_key = st.selectbox(
        "Tiết muốn xếp/di chuyển:", list(all_options.keys()),
        format_func=lambda i: all_options[i], key=f"me_dd_lesson_{sel_lop}",
    )

    is_new = isinstance(sel_key, str) and sel_key.startswith("new_")
    idx = _rules_index(timetable)
    slots = timetable_rules.all_slots(timetable)

    if is_new:
        row = None
        # Tìm đúng dòng unplaced tương ứng (subject nằm giữa "new_" và "_<i>")
        for _, r in unplaced.iterrows():
            if sel_key.startswith(f"new_{r['subject']}_"):
                row = r
                break
        if row is None:
            st.error("Không tìm thấy môn tương ứng.")
            return
        mon, ten_gv = row["subject"], row["ten_gv"]
        legal = timetable_rules.legal_targets_for_lesson(
            idx, moving_id=-1, lop=sel_lop, ten_gv=ten_gv, mon=mon, slots=slots,
            sibling_classes=_get_group_siblings(sel_lop, mon),
        )
    else:
        r = sub[sub["id"] == sel_key].iloc[0]
        mon, ten_gv = r["mon"], r["ten_gv"]
        legal = timetable_rules.legal_targets_for_lesson(
            idx, moving_id=int(sel_key), lop=r["lop"], ten_gv=ten_gv, mon=mon, slots=slots,
        )
        legal = [s for s in legal if s != (r["thu"], int(r["tiet"]), r["buoi"])]

    st.caption(f"🟢 {len(legal)} ô hợp lệ có thể xếp cho tiết này.")
    if not legal:
        st.warning("Không còn ô hợp lệ nào.")
        return
    target_labels = {s: _slot_label(*s) for s in legal}
    sel_target = st.selectbox(
        "Ô đích (chỉ hiện ô hợp lệ):", legal,
        format_func=lambda s: target_labels[s], key=f"me_dd_target_{sel_lop}",
    )
    btn_label = "🆕 Xếp tiết này" if is_new else "🔀 Di chuyển tiết này"
    if st.button(btn_label, type="primary", key=f"me_dd_move_{sel_lop}"):
        if is_new:
            _apply_insert(mon, ten_gv, sel_lop, sel_target[0], sel_target[1], sel_target[2])
        else:
            _apply_move(int(sel_key), sel_target[0], sel_target[1], sel_target[2])


def _render_drag_drop_section(classes: pd.DataFrame, timetable: pd.DataFrame) -> None:
    all_lop = sorted(classes["lop"].dropna().unique()) if not classes.empty else \
        sorted(timetable["lop"].dropna().unique())
    if not all_lop:
        st.info("Chưa có danh sách lớp. Tải file **classes.csv** trong sidebar để bắt đầu.")
        return
    sel_lop = st.selectbox("Chọn lớp:", all_lop, key="me_dd_lop")

    sub = timetable[timetable["lop"] == sel_lop].copy() if not timetable.empty else timetable
    sub = sub.sort_values(
        ["thu", "tiet", "buoi"],
        key=lambda col: col.map(timetable_rules.DAY_KEY) if col.name == "thu" else col,
    ) if not sub.empty else sub

    unplaced = _get_unplaced_lessons(sel_lop, timetable)
    if not unplaced.empty:
        st.info(
            f"🆕 Lớp {sel_lop} có **{int(unplaced['missing'].sum())} tiết chưa xếp** "
            f"(môn tự chọn/nhóm vừa gán chưa có ngày/tiết): "
            + ", ".join(f"{_subject_label(r['subject'], r['ma_nhom'])} ({r['missing']} tiết)"
                        for _, r in unplaced.iterrows())
            + " — kéo thẻ 🆕 bên dưới lưới vào ô trống, hoặc dùng cách chọn-ô ở mục dưới."
        )

    st.markdown(f"**Lưới tuần — lớp {sel_lop}** — kéo 1 tiết (kể cả thẻ 🆕 chưa xếp) rồi thả vào ô xanh.")

    idx = _rules_index(timetable)
    slots = timetable_rules.all_slots(timetable)
    group_map = _get_group_map(sel_lop)
    grid_data = _build_grid_data(sel_lop, sub, idx, slots, unplaced, group_map)

    result = tkb_grid(grid_data, key=f"tkb_grid_{sel_lop}")

    last_seq_key = f"me_grid_last_seq_{sel_lop}"
    if result and result.get("event") == "drop" and result.get("drop_seq") != st.session_state.get(last_seq_key):
        st.session_state[last_seq_key] = result["drop_seq"]
        tgt = result["target"]
        lesson_id = result["lesson_id"]
        if isinstance(lesson_id, str) and lesson_id.startswith("new_"):
            row = None
            for _, r in unplaced.iterrows():
                if lesson_id.startswith(f"new_{r['subject']}_"):
                    row = r
                    break
            if row is not None:
                _apply_insert(row["subject"], row["ten_gv"], sel_lop, tgt["thu"], tgt["tiet"], tgt["buoi"])
        else:
            _apply_move(lesson_id, tgt["thu"], tgt["tiet"], tgt["buoi"])

    _render_class_teachers_schedule(sel_lop, timetable, idx)

    with st.expander("🔧 Cách khác: chọn ô bằng danh sách (không cần kéo chuột)"):
        _render_click_fallback(sel_lop, sub, timetable, unplaced)


def _split_gv(x) -> list[str]:
    return [t.strip() for t in str(x).split(" / ") if t.strip()]


def _render_class_teachers_schedule(sel_lop: str, timetable: pd.DataFrame,
                                    idx: "timetable_rules.TimetableIndex") -> None:
    """Lịch tuần của mọi GV dạy lớp đang xếp (1 GV / dòng) — để xếp tiết sát các tiết
    sẵn có của GV, tránh xé lẻ buổi dạy."""
    import html
    from collections import defaultdict

    sub = timetable[timetable["lop"] == sel_lop]
    teacher_subj: dict[str, set[str]] = defaultdict(set)
    for r in sub.itertuples():
        for t in _split_gv(r.ten_gv):
            teacher_subj[t].add(r.mon)
    if not teacher_subj:
        return

    order = ["Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7"]
    days = [d for d in order if d in set(timetable["thu"])]
    sessions = [("Sáng", list(range(1, scheduler.N_SANG + 1))), ("Chiều", list(range(1, scheduler.N_CHIEU + 1)))]
    cols = [(d, b, p) for d in days for b, ps in sessions for p in ps]

    occ: dict[tuple[str, str, str, int], list[tuple[str, str]]] = defaultdict(list)
    tset = set(teacher_subj)
    for r in timetable.itertuples():
        for t in _split_gv(r.ten_gv):
            if t in tset:
                occ[(t, r.thu, r.buoi, int(r.tiet))].append((r.lop, r.mon))

    def order_key(t):
        main = sorted(teacher_subj[t] - {"Chào cờ"}) or ["Chào cờ"]
        return (main[0], t)

    css = """
    <style>
    .gvt-wrap{overflow-x:auto;max-width:100%;}
    .gvt{border-collapse:collapse;font-size:11px;font-family:Arial,sans-serif;}
    .gvt th,.gvt td{border:1px solid #d0d0d0;padding:2px 3px;text-align:center;white-space:nowrap;}
    .gvt th{background:#217346;color:#fff;font-weight:600;}
    .gvt th.s{background:#70AD47;} .gvt th.c{background:#2E75B6;}
    .gvt td.name{position:sticky;left:0;background:#fafafa;text-align:left;font-weight:600;min-width:170px;}
    .gvt td.me{background:#2e7d32;color:#fff;font-weight:700;}
    .gvt td.other{background:#e3e6ea;color:#333;}
    .gvt td.busy{background:#fde2e2;color:#b91c1c;}
    .gvt td.one{box-shadow:inset 0 0 0 2px #f0ad4e;}
    .gvt td.dsep{border-left:2px solid #555;}
    .gvt td.stat{background:#fffbe6;}
    </style>"""
    h = [css, '<div class="gvt-wrap"><table class="gvt"><tr><th rowspan="2">GV (môn dạy lớp)</th>']
    for d in days:
        h.append(f'<th colspan="{sum(len(ps) for _, ps in sessions)}">{d}</th>')
    h.append('<th rowspan="2">Buổi lên trường</th><th rowspan="2">Buổi 1 tiết</th><th rowspan="2">Tiết trống</th></tr><tr>')
    for d in days:
        for b, ps in sessions:
            for p in ps:
                h.append(f'<th class="{"s" if b == "Sáng" else "c"}">{b[0]}{p}</th>')
    h.append("</tr>")

    for t in sorted(teacher_subj, key=order_key):
        busy = idx.teacher_unavailable.get(t, set())
        n_ses = n_one = n_gap = 0
        single: set[tuple[str, str]] = set()
        for d in days:
            for b, ps in sessions:
                used = [p for p in ps if occ.get((t, d, b, p))]
                if used:
                    n_ses += 1
                    n_gap += used[-1] - used[0] + 1 - len(used)
                    if len(used) == 1:
                        n_one += 1; single.add((d, b))
        subj = ", ".join(sorted(teacher_subj[t]))
        h.append(f'<tr><td class="name">{html.escape(t)}<br><span style="font-weight:400;color:#666">{html.escape(subj)}</span></td>')
        for i, (d, b, p) in enumerate(cols):
            items = occ.get((t, d, b, p), [])
            cls = []
            if i and cols[i - 1][0] != d:
                cls.append("dsep")
            if items:
                mine = any(l == sel_lop for l, _ in items)
                cls.append("me" if mine else "other")
                if (d, b) in single:
                    cls.append("one")
                text = " / ".join(l for l, _ in items)
                tip = "; ".join(f"{l}: {m}" for l, m in items)
            elif (d, p, b) in busy:
                cls.append("busy"); text, tip = "bận", "GV báo bận / họp tổ"
            else:
                text, tip = "", ""
            h.append(f'<td class="{" ".join(cls)}" title="{html.escape(f"{d} {b} tiết {p}: {tip}")}">{html.escape(text)}</td>')
        h.append(f'<td class="stat">{n_ses}</td><td class="stat">{n_one}</td><td class="stat">{n_gap}</td></tr>')
    h.append("</table></div>")

    st.markdown(f"**👩‍🏫 Lịch toàn bộ GV dạy lớp {sel_lop}** — xếp tiết sát các tiết sẵn có để GV không bị xé lẻ buổi dạy.")
    st.caption("🟩 dạy lớp này · ⬜ dạy lớp khác · 🟥 báo bận/họp tổ · viền cam = buổi chỉ có 1 tiết. "
               "Rê chuột vào ô để xem môn.")
    st.markdown("".join(h), unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# 2) GV — Trợ lý & TKB                                                        #
# --------------------------------------------------------------------------- #

def _teacher_weekly_pivot(timetable_df: pd.DataFrame, ten_gv: str) -> pd.DataFrame:
    """Giống hệt pivot ở tab "TKB theo GV" (app.py, tab2) — tái dùng logic,
    không viết lại — để hai nơi luôn cho ra cùng kết quả cho cùng 1 GV."""
    filtered = timetable_df[timetable_df["ten_gv"] == ten_gv]
    return filtered.pivot_table(index=["thu", "tiet", "buoi"], columns="lop",
                                 values="mon", aggfunc="first")


def _teacher_load_summary(timetable_df: pd.DataFrame, ten_gv: str, teacher_pref: dict) -> dict:
    """Chỉ tính từ dữ liệu cục bộ sẵn có, không gọi mạng.
    `so_tiet` dùng đúng cách đếm như tab "TKB theo GV" cũ (so khớp chính xác
    ten_gv, không tách team-teaching) để số liệu nhất quán giữa 2 nơi."""
    so_tiet = int((timetable_df["ten_gv"] == ten_gv).sum())
    max_per_week = teacher_pref.get("max_periods_per_week", 18)
    max_per_day = teacher_pref.get("max_periods_per_day", 4)

    idx = timetable_rules.build_index(timetable_df)
    busy_from_schedule = {
        (thu, tiet, buoi) for (tn, thu, tiet, buoi) in idx.by_teacher_slot if tn == ten_gv
    }
    slots = timetable_rules.all_slots(timetable_df)
    free_slots = [s for s in slots if s not in busy_from_schedule]
    unavailable = {(s["day"], s["period"], s["buoi"]) for s in teacher_pref.get("unavailable_slots", [])}

    by_day: dict[str, int] = {}
    for (thu, _tiet, _buoi) in busy_from_schedule:
        by_day[thu] = by_day.get(thu, 0) + 1
    overload_days = [d for d, c in by_day.items() if c > max_per_day]

    return {
        "so_tiet": so_tiet, "max_per_week": max_per_week, "max_per_day": max_per_day,
        "by_day": by_day, "overload_days": overload_days,
        "free_slots": sorted(free_slots, key=lambda s: (timetable_rules.DAY_KEY.get(s[0], 99), s[2], s[1])),
        "busy_slots": sorted(unavailable, key=lambda s: (timetable_rules.DAY_KEY.get(s[0], 99), s[2], s[1])),
    }


def _render_teacher_view_and_assistant(
    teachers: pd.DataFrame, timetable: pd.DataFrame, all_issues: list[timetable_rules.Issue],
) -> None:
    if timetable.empty:
        st.info("Chưa có dữ liệu TKB. Tải file **timetable.csv** trong sidebar để bắt đầu.")
        return

    all_gv = sorted(timetable["ten_gv"].dropna().unique())
    all_gv = [g for g in all_gv if g.strip()]
    if not all_gv:
        st.info("Chưa có tên giáo viên nào trong TKB.")
        return
    sel_gv = st.selectbox("Chọn giáo viên:", all_gv, key="me_gv_sel")

    gv_row = teachers[teachers["ten"] == sel_gv]
    ma_so = gv_row.iloc[0]["ma_so"] if not gv_row.empty else ""
    mon_day = gv_row.iloc[0]["mon_day"] if not gv_row.empty else ""
    teacher_pref = database.get_teacher_pref(ma_so) if ma_so else {}

    if mon_day:
        st.caption(f"Môn dạy: **{mon_day}** · Mã GV: `{ma_so}`")

    load = _teacher_load_summary(timetable, sel_gv, teacher_pref)

    c1, c2, c3 = st.columns(3)
    c1.metric("Số tiết/tuần", load["so_tiet"], help=f"Tối đa: {load['max_per_week']}")
    c2.metric("Tối đa tiết/ngày", load["max_per_day"])
    c3.metric("Ngày quá tải", len(load["overload_days"]),
              delta_color="inverse" if load["overload_days"] else "normal")

    st.markdown(f"**TKB tuần — {sel_gv}**")
    pivot = _teacher_weekly_pivot(timetable, sel_gv)
    if pivot.empty:
        st.warning("Không tìm thấy tiết dạy.")
    else:
        st.dataframe(pivot, use_container_width=True)

    with st.expander(f"🟢 {len(load['free_slots'])} tiết trống trong tuần"):
        if load["free_slots"]:
            st.write(", ".join(_slot_label(*s) for s in load["free_slots"]))
        else:
            st.write("(không có)")
    with st.expander(f"🔴 {len(load['busy_slots'])} tiết đã báo bận"):
        if load["busy_slots"]:
            st.write(", ".join(_slot_label(*s) for s in load["busy_slots"]))
        else:
            st.write("(không có)")

    st.divider()
    if ai_assistant.is_chat_enabled():
        q = st.text_input("💬 Hỏi trợ lý AI về GV này:", key=f"me_ai_q_{ma_so or sel_gv}")
        if q:
            context = ai_assistant.build_context(sel_gv, load, teacher_pref, all_issues)
            try:
                with st.spinner("Đang hỏi Claude..."):
                    answer = ai_assistant.ask(q, context)
                st.markdown(f"**Trả lời:** {answer}")
            except Exception as exc:
                st.error(f"⚠️ Lỗi gọi AI: {exc}")
    else:
        st.info(
            "💬 Trợ lý AI đang tắt — chưa cấu hình `ANTHROPIC_API_KEY` "
            "(đặt biến môi trường hoặc thêm vào `.streamlit/secrets.toml`)."
        )


# --------------------------------------------------------------------------- #
# 3) Tìm lỗi toàn trường                                                     #
# --------------------------------------------------------------------------- #

def _render_error_finder_section(all_issues: list[timetable_rules.Issue]) -> None:
    if not all_issues:
        st.success("✅ Không phát hiện lỗi hoặc cảnh báo nào trong TKB!")
        return

    n_error = sum(1 for i in all_issues if i.severity == "error")
    n_warn  = sum(1 for i in all_issues if i.severity == "warning")
    c1, c2 = st.columns(2)
    c1.metric("🔴 Lỗi", n_error)
    c2.metric("🟡 Cảnh báo", n_warn)

    kinds = sorted({i.kind for i in all_issues})
    kind_label = {
        "teacher_double_booking": "GV trùng tiết",
        "class_double_booking":   "Lớp trùng tiết",
        "teacher_unavailable":    "GV báo bận",
        "group_conflict":         "Trùng giờ nhóm tự chọn",
        "curriculum_mismatch":    "Lệch chương trình",
    }
    sel_kinds = st.multiselect(
        "Lọc theo loại lỗi:", kinds,
        default=kinds, format_func=lambda k: kind_label.get(k, k), key="me_err_kinds",
    )
    filtered = [i for i in all_issues if i.kind in sel_kinds]
    df = timetable_rules.issues_to_dataframe(filtered)
    st.dataframe(
        df.sort_values(["Mức độ", "Thứ"], key=lambda col: col.map(timetable_rules.DAY_KEY)
                        if col.name == "Thứ" else col),
        use_container_width=True, hide_index=True,
    )


# --------------------------------------------------------------------------- #
# 4) Lịch sử thay đổi                                                        #
# --------------------------------------------------------------------------- #

def _parse_change_json(value) -> dict | None:
    """SQLite NULL đọc qua pandas thành NaN (float), không phải None/"" —
    và NaN lại "truthy" trong Python nên kiểm tra `if value` không đủ, phải
    kiểm tra rõ là chuỗi thật trước khi json.loads()."""
    if not isinstance(value, str) or not value:
        return None
    return json.loads(value)


def _format_change(row: pd.Series) -> str:
    before = _parse_change_json(row["before_json"])
    after = _parse_change_json(row["after_json"])
    if row["action"] == "move" and before and after:
        return (f"{_slot_label(before['thu'], before['tiet'], before['buoi'])} → "
                f"{_slot_label(after['thu'], after['tiet'], after['buoi'])}")
    if row["action"] == "edit" and before and after:
        return f"{before.get('ten_gv')}/{before.get('mon')} → {after.get('ten_gv')}/{after.get('mon')}"
    if row["action"] == "create" and after:
        return f"Tạo mới: {after.get('mon')} — {after.get('ten_gv')} tại {_slot_label(after['thu'], after['tiet'], after['buoi'])}"
    if row["action"] == "delete" and before:
        return f"Xoá: {before.get('mon')} — {before.get('ten_gv')} tại {_slot_label(before['thu'], before['tiet'], before['buoi'])}"
    return row["action"]


def _render_change_log_section(timetable: pd.DataFrame) -> None:
    st.markdown("Lịch sử mọi thay đổi thực hiện qua module xếp TKB thủ công.")
    col1, col2, col3 = st.columns(3)
    with col1:
        lop_options = ["Tất cả"] + (sorted(timetable["lop"].dropna().unique())
                                     if not timetable.empty else [])
        f_lop = st.selectbox("Lớp:", lop_options, key="me_log_lop")
    with col2:
        f_gv = st.text_input("Giáo viên (chứa):", key="me_log_gv")
    with col3:
        f_n = st.slider("Số dòng gần nhất:", 20, 500, 100, key="me_log_n")

    log_df = database.get_change_log(limit=f_n, lop=None if f_lop == "Tất cả" else f_lop)
    if f_gv.strip():
        log_df = log_df[log_df["ten_gv"].str.contains(f_gv.strip(), case=False, na=False)]

    if log_df.empty:
        st.info("Chưa có lịch sử thay đổi nào.")
        return

    log_df = log_df.copy()
    log_df["Thay đổi"] = log_df.apply(_format_change, axis=1)
    display = log_df[["ts", "action", "lop", "ten_gv", "Thay đổi", "note"]].rename(columns={
        "ts": "Thời gian", "action": "Loại", "lop": "Lớp", "ten_gv": "GV", "note": "Ghi chú",
    })
    st.dataframe(display, use_container_width=True, hide_index=True)


# --------------------------------------------------------------------------- #
# 5) Môn tự chọn (nhóm xuyên lớp)                                            #
# --------------------------------------------------------------------------- #

# Đường dẫn mặc định tới file danh sách mã nhóm (đặt sẵn trong thư mục dự
# án). Người dùng vẫn có thể tải lên bản khác/mới hơn qua giao diện.
DEFAULT_MA_NHOM_XLSX = Path(__file__).parent / "Danh_sach_ma_nhom_HKI_2026_2027.xlsx"

# Tiền tố loại nhóm trong mã (vd "CT10-AVBN-05") → tên môn dùng trong hệ
# thống — khớp đúng tên đã dùng trong scheduler.AFTERNOON_ONLY_SUBJECTS.
MA_NHOM_TYPE_TO_SUBJECT: dict[str, str] = {
    "AVBN": "AVBN", "STEM": "STEM", "THQT": "THQT",
    "BC": "Bóng chuyền", "BD": "Bóng đá", "BR": "Bóng rổ", "CL": "Cầu lông",
    "ANH": "Tiếng Anh", "DIA": "Địa lí", "HOA": "Hóa học", "KTPL": "GD KT&PL",
    "LY": "Vật lí", "SINH": "Sinh học", "SU": "Lịch sử", "TIN": "Tin học",
}

_MA_NHOM_RE = re.compile(r"^(CT|TC|TN)(\d+)-([A-Z]+)-(\d+)$")


@st.cache_data(show_spinner=False)
def _load_ma_nhom_file(file_bytes: bytes) -> pd.DataFrame:
    """Đọc file Excel danh sách mã nhóm → DataFrame [ma_nhom, khoi, subject,
    ma_gv, ten_gv]. Mã GV được giải quyết theo TÊN GV trong bảng teachers khi
    khớp được (một số file gốc dùng lại cùng 1 mã GV — vd "THQT-04" — cho
    nhiều người khác nhau ở các khối khác nhau; tra theo tên tránh nhầm lẫn)."""
    import io

    import openpyxl

    teachers_df = database.get_teachers()
    name_to_maso = {str(n).strip().upper(): m
                     for n, m in zip(teachers_df["ten"], teachers_df["ma_so"])}

    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    ws = wb[wb.sheetnames[0]]

    # Tìm dòng tiêu đề (chứa "Mã nhóm") để không phụ thuộc số dòng cố định.
    header_row = None
    for r in range(1, min(ws.max_row, 20) + 1):
        vals = [str(ws.cell(row=r, column=c).value or "") for c in range(1, 5)]
        if any("mã nhóm" in v.lower() for v in vals):
            header_row = r
            break
    if header_row is None:
        return pd.DataFrame(columns=["ma_nhom", "khoi", "subject", "ma_gv", "ten_gv"])

    rows = []
    for r in range(header_row + 1, ws.max_row + 1):
        ma_nhom = ws.cell(row=r, column=2).value
        ma_gv = ws.cell(row=r, column=3).value
        ten_gv = ws.cell(row=r, column=4).value
        if not ma_nhom:
            continue
        m = _MA_NHOM_RE.match(str(ma_nhom).strip())
        if not m:
            continue
        _kind, khoi, typ, _num = m.groups()
        subject = MA_NHOM_TYPE_TO_SUBJECT.get(typ)
        if subject is None:
            continue
        resolved = name_to_maso.get(str(ten_gv).strip().upper(), ma_gv)
        rows.append({"ma_nhom": ma_nhom, "khoi": khoi, "subject": subject,
                     "ma_gv": resolved, "ten_gv": ten_gv})
    return pd.DataFrame(rows)


def _get_ma_nhom_df() -> pd.DataFrame:
    if "me_ma_nhom_df" in st.session_state:
        return st.session_state["me_ma_nhom_df"]
    if DEFAULT_MA_NHOM_XLSX.exists():
        df = _load_ma_nhom_file(DEFAULT_MA_NHOM_XLSX.read_bytes())
        st.session_state["me_ma_nhom_df"] = df
        return df
    return pd.DataFrame(columns=["ma_nhom", "khoi", "subject", "ma_gv", "ten_gv"])


def _render_khung_gio_table() -> None:
    """Bảng khung giờ trong ngày (sáng + chiều) — dùng đúng SANG_TIMES/
    CHIEU_TIMES trong export_utils.py để không bao giờ lệch với TKB xuất ra."""
    rows = []
    for tiet, tg in export_utils.SANG_TIMES.items():
        rows.append({"Buổi": "Sáng" if tiet == 1 else "", "Tiết": str(tiet),
                     "Thời gian": tg.replace("h", ":")})
        if tiet == 1:
            rows.append({"Buổi": "", "Tiết": "Giờ chơi", "Thời gian": "8:15-8:45"})
        if tiet == 3:
            rows.append({"Buổi": "", "Tiết": "Giờ chơi", "Thời gian": "10:15-10:25"})
    for tiet, tg in export_utils.CHIEU_TIMES.items():
        rows.append({"Buổi": "Chiều" if tiet == 1 else "", "Tiết": str(tiet),
                     "Thời gian": tg.replace("h", ":")})
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


def _render_chieu_grid(timetable: pd.DataFrame) -> None:
    """Lưới trống buổi chiều Thứ 2 – Thứ 6 (đúng khung giờ thật) để tham
    chiếu khi gắn mã nhóm lên TKB. Hiện thêm số lớp đã có tiết chính khoá
    (không phải môn tự chọn) ở từng ô, để biết ô nào rảnh/bận trên diện rộng."""
    days = export_utils.DAYS_DATA
    days_disp = export_utils.DAYS_DISPLAY
    periods = export_utils.CHIEU_TIMES

    grid = {}
    if not timetable.empty:
        chieu = timetable[timetable["buoi"] == "Chiều"]
        grid = chieu.groupby(["thu", "tiet"])["lop"].nunique().to_dict()

    header = ["Tiết", "Thời gian"] + days_disp
    rows = []
    for tiet, tg in periods.items():
        row = {"Tiết": tiet, "Thời gian": tg.replace("h", ":")}
        for d, d_disp in zip(days, days_disp):
            n = grid.get((d, tiet), 0)
            row[d_disp] = f"{n} lớp có tiết" if n else "—"
        rows.append(row)
    st.dataframe(pd.DataFrame(rows)[header], hide_index=True, use_container_width=True)
    st.caption(
        "Số trong mỗi ô = số lớp đã có tiết chính khoá ở khung giờ đó (tiết còn lại "
        "trong tuần của lớp là chỗ có thể gắn thêm nhóm tự chọn). Công cụ này chưa "
        "tự xếp ngày/tiết cụ thể cho từng mã nhóm — bạn xem lưới này để tự chọn "
        "khung giờ phù hợp, sau đó dùng tab 🗓️ Kéo-thả theo Lớp để đặt vào đúng ô."
    )


def _render_elective_assignment_section(teachers: pd.DataFrame, classes: pd.DataFrame,
                                        timetable: pd.DataFrame) -> None:
    st.markdown(
        "Các môn nhóm tự chọn (AVBN, STEM, THQT, thể thao, ôn thi tốt nghiệp...) "
        "thường có **rất ít GV** so với số lớp — 1 GV không thể dạy riêng cho "
        "cả khối. Chọn đúng **mã nhóm** rồi gộp lớp vào nhóm đó, theo đúng sĩ "
        "số nhóm thực tế, thay vì để hệ thống gán tự động cho mọi lớp."
    )

    with st.expander("🕐 Khung giờ trong ngày", expanded=False):
        _render_khung_gio_table()

    with st.expander("📅 Lưới buổi Chiều Thứ 2 – Thứ 6 (tham chiếu để gắn nhóm)", expanded=True):
        _render_chieu_grid(timetable)

    with st.expander("📂 File danh sách mã nhóm"):
        uploaded = st.file_uploader(
            "Tải lên file Excel danh sách mã nhóm (nếu có bản mới hơn):",
            type=["xlsx"], key="me_ma_nhom_upload",
        )
        if uploaded is not None:
            st.session_state["me_ma_nhom_df"] = _load_ma_nhom_file(uploaded.getvalue())
            st.success(f"Đã tải {len(st.session_state['me_ma_nhom_df'])} mã nhóm từ file vừa tải lên.")
        elif DEFAULT_MA_NHOM_XLSX.exists():
            st.caption(f"Đang dùng file mặc định: `{DEFAULT_MA_NHOM_XLSX.name}`")
        else:
            st.warning("Chưa có file mã nhóm nào — hãy tải lên ở trên.")

    ma_nhom_df = _get_ma_nhom_df()

    cls_subj = database.get_class_subjects()
    all_classes = sorted(classes["lop"].dropna().unique()) if not classes.empty else []

    # ── Hiện trạng ────────────────────────────────────────────────────────
    with st.expander("📊 Hiện trạng theo môn (lớp đã gán / tổng lớp trong khối)", expanded=True):
        if ma_nhom_df.empty:
            st.info("Chưa có dữ liệu mã nhóm để hiển thị hiện trạng.")
        else:
            subj_khoi = ma_nhom_df[["subject", "khoi"]].drop_duplicates().sort_values(["subject", "khoi"])
            rows = []
            for _, r in subj_khoi.iterrows():
                subj, g = r["subject"], r["khoi"]
                total = len(classes[classes["khoi"] == g]) if not classes.empty else 0
                done = len(cls_subj[(cls_subj["subject"] == subj)
                                     & (cls_subj["lop"].str.startswith(g))])
                n_nhom = len(ma_nhom_df[(ma_nhom_df["subject"] == subj) & (ma_nhom_df["khoi"] == g)])
                rows.append({"Môn": subj, "Khối": g, "Số mã nhóm": n_nhom,
                             "Lớp đã gán": done, "Tổng lớp": total,
                             "Còn thiếu": max(0, total - done)})
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    st.divider()
    st.markdown("#### ➕ Gán mã nhóm cho các lớp đã chọn")

    if ma_nhom_df.empty:
        st.info("Chưa có file mã nhóm nào được tải — tải lên ở mục trên để chọn theo mã nhóm.")
        return

    ma_nhom_sorted = ma_nhom_df.sort_values(["subject", "khoi", "ma_nhom"])
    code_labels = {
        row["ma_nhom"]: f"{row['ma_nhom']} — {row['subject']} (Khối {row['khoi']}) — GV: {row['ten_gv']}"
        for _, row in ma_nhom_sorted.iterrows()
    }
    sel_code = st.selectbox(
        "Chọn mã nhóm:", list(code_labels.keys()),
        format_func=lambda c: code_labels[c], key="me_elec_manhom_code",
    )
    sel_row = ma_nhom_df[ma_nhom_df["ma_nhom"] == sel_code].iloc[0]
    sel_subj, sel_ma_so, sel_khoi = sel_row["subject"], sel_row["ma_gv"], sel_row["khoi"]
    periods = st.number_input(
        "Số tiết/tuần (thường 2 — 1 buổi khối 2 tiết):",
        min_value=1, max_value=10, value=2, key="me_elec_periods",
    )
    st.caption(
        f"Môn: **{sel_subj}** · GV: **{sel_row['ten_gv']}** (`{sel_ma_so}`) · Khối **{sel_khoi}**"
    )

    filterable = [c for c in all_classes if c[:2] == sel_khoi]
    group_map = database.get_elective_group_map()
    already_df = cls_subj[(cls_subj["subject"] == sel_subj) & (cls_subj["lop"].isin(filterable))]
    if not already_df.empty:
        def _fmt_already(lop: str) -> str:
            m = group_map[(group_map["lop"] == lop) & (group_map["subject"] == sel_subj)]
            return f"{lop} ({m.iloc[0]['ma_nhom']})" if not m.empty else lop
        st.caption(f"⚠️ Các lớp sau đã có môn {sel_subj} (chọn lại sẽ THAY THẾ GV/mã nhóm cũ): "
                   f"{', '.join(_fmt_already(lop) for lop in already_df['lop'])}")
    sel_classes = st.multiselect(f"Các lớp gộp vào nhóm {sel_code}:", filterable, key="me_elec_classes")

    if st.button("➕ Gán cho các lớp đã chọn", type="primary", key="me_elec_add"):
        if not sel_classes:
            st.error("Chưa chọn lớp nào.")
        else:
            _apply_elective_assignment(sel_subj, int(periods), sel_ma_so, sel_classes, ma_nhom=sel_code)
            st.success(
                f"✅ Đã gán mã nhóm **{sel_code}** — môn **{sel_subj}** ({periods} tiết/tuần, "
                f"GV {sel_row['ten_gv']}) cho {len(sel_classes)} lớp: {', '.join(sel_classes)}."
            )
            st.rerun()

    st.divider()
    st.markdown("#### 🗑️ Gỡ môn khỏi lớp (nếu gán nhầm)")
    col4, col5 = st.columns(2)
    with col4:
        remove_subj = st.selectbox(
            "Môn:", sorted(cls_subj["subject"].unique()) if not cls_subj.empty else [],
            key="me_elec_remove_subj",
        )
    with col5:
        assigned_classes = sorted(cls_subj[cls_subj["subject"] == remove_subj]["lop"].unique()) \
            if remove_subj else []
        remove_classes = st.multiselect("Lớp cần gỡ:", assigned_classes, key="me_elec_remove_classes")
    if st.button("🗑️ Gỡ khỏi các lớp đã chọn", key="me_elec_remove_btn"):
        if remove_subj and remove_classes:
            _remove_elective_assignment(remove_subj, remove_classes)
            st.success(f"✅ Đã gỡ môn **{remove_subj}** khỏi {len(remove_classes)} lớp.")
            st.rerun()
        else:
            st.error("Chưa chọn môn hoặc lớp cần gỡ.")


def _apply_elective_assignment(subject: str, periods: int, ma_so: str, lop_list: list[str],
                               ma_nhom: str | None = None) -> None:
    """Gán 1 môn tự chọn (subject, periods, 1 GV) cho danh sách lớp cụ thể.
    Mỗi lớp chỉ có ĐÚNG 1 GV cho môn này tại một thời điểm — nếu lớp đã có
    GV khác cho môn này, sẽ bị THAY THẾ (không cộng dồn thành dạy chung, vì
    scheduler hiểu nhiều GV cùng 1 lớp+môn là "dạy chung 1 tiết", không phải
    "chọn 1 trong nhiều"). `ma_nhom` (nếu có) chỉ được ghi nhớ để HIỂN THỊ
    (khay chưa xếp, lưới kéo-thả...) — không ảnh hưởng logic xếp TKB."""
    grades = {lop[:2] for lop in lop_list}
    for g in grades:
        db_df = pd.DataFrame([{"Môn dạy": subject, "Tiết/tuần": periods, "Loại": "Tự chọn"}])
        database.upsert_curriculum(g, db_df)

    cls_subj = database.get_class_subjects()
    cls_subj = cls_subj[~((cls_subj["subject"] == subject) & (cls_subj["lop"].isin(lop_list)))]
    new_cs = pd.DataFrame([{"lop": lop, "subject": subject, "periods_per_week": periods}
                            for lop in lop_list])
    database.save_class_subjects(pd.concat([cls_subj, new_cs], ignore_index=True))

    asgn = database.get_assignments()
    asgn = asgn[~((asgn["subject"] == subject) & (asgn["lop"].isin(lop_list)))]
    new_asgn = pd.DataFrame([{"lop": lop, "subject": subject, "ma_so": ma_so} for lop in lop_list])
    database.save_assignments(pd.concat([asgn, new_asgn], ignore_index=True))

    if ma_nhom:
        for lop in lop_list:
            database.set_elective_group(lop, subject, ma_nhom)


def _remove_elective_assignment(subject: str, lop_list: list[str]) -> None:
    cls_subj = database.get_class_subjects()
    cls_subj = cls_subj[~((cls_subj["subject"] == subject) & (cls_subj["lop"].isin(lop_list)))]
    database.save_class_subjects(cls_subj)

    asgn = database.get_assignments()
    asgn = asgn[~((asgn["subject"] == subject) & (asgn["lop"].isin(lop_list)))]
    database.save_assignments(asgn)

    database.remove_elective_group(subject, lop_list)
