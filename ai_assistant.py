# =============================================================================
# ai_assistant.py — Chat AI hỏi-đáp tự do về 1 giáo viên, dùng Anthropic API.
#
# `import anthropic` được đặt TRONG hàm ask(), không ở đầu file, để toàn app
# vẫn chạy/import được trước khi cài `anthropic` (chỉ cần cài khi thực sự
# dùng chat). Panel thông tin/gợi ý (manual_edit._teacher_load_summary) không
# phụ thuộc file này — chat chỉ là lớp tuỳ chọn phía trên.
# =============================================================================

from __future__ import annotations

import os

import streamlit as st

DEFAULT_MODEL = "claude-haiku-4-5"  # Q&A tra cứu trong ngữ cảnh nhỏ, có giới hạn — không cần model lớn.


def _get_api_key() -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        return st.secrets.get("ANTHROPIC_API_KEY")
    except Exception:
        # st.secrets raises nếu chưa có .streamlit/secrets.toml — coi như chưa cấu hình.
        return None


def is_chat_enabled() -> bool:
    return bool(_get_api_key())


def build_context(ten_gv: str, load_summary: dict, teacher_pref: dict,
                   school_issues: list | None = None) -> str:
    """Dựng đoạn ngữ cảnh tiếng Việt từ dữ liệu cục bộ (không gọi mạng)."""
    lines = [
        f"Giáo viên: {ten_gv}",
        f"Số tiết/tuần hiện tại: {load_summary['so_tiet']} (tối đa: {load_summary.get('max_per_week', '?')})",
        f"Số tiết/ngày tối đa: {load_summary.get('max_per_day', '?')}",
        f"Ghi chú nguyện vọng: {teacher_pref.get('notes') or '(không có)'}",
        "Tiết trống trong tuần: " + (
            ", ".join(f"{d} — tiết {p} ({b})" for d, p, b in load_summary["free_slots"]) or "(không có)"
        ),
        "Tiết đã báo bận: " + (
            ", ".join(f"{d} — tiết {p} ({b})" for d, p, b in load_summary["busy_slots"]) or "(không có)"
        ),
    ]
    if school_issues:
        relevant = [i for i in school_issues if i.ten_gv == ten_gv]
        if relevant:
            lines.append("Lỗi/cảnh báo liên quan đến GV này:")
            lines += [f"- {i.message}" for i in relevant]
    return "\n".join(lines)


def ask(question: str, context: str, model: str = DEFAULT_MODEL) -> str:
    import anthropic  # deferred — xem ghi chú đầu file
    client = anthropic.Anthropic(api_key=_get_api_key())
    resp = client.messages.create(
        model=model, max_tokens=1024,
        system=(
            "Bạn là trợ lý xếp thời khoá biểu cho một trường THPT tại Việt Nam. "
            "Trả lời ngắn gọn, chính xác, CHỈ dựa trên dữ liệu ngữ cảnh được cung cấp. "
            "Nếu ngữ cảnh không đủ để trả lời, hãy nói rõ là không có dữ liệu, không suy đoán."
        ),
        messages=[{"role": "user", "content": f"NGỮ CẢNH:\n{context}\n\nCÂU HỎI: {question}"}],
    )
    return next((b.text for b in resp.content if b.type == "text"), "")
