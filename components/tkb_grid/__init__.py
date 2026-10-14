# =============================================================================
# components/tkb_grid — Streamlit custom component: lưới TKB kéo-thả HTML5.
#
# Dùng phương pháp "static component" chính thức của Streamlit (không cần
# Node/React build tool — repo này chỉ có streamlit+pandas):
# declare_component(name, path=<thư mục static>) phục vụ nguyên thư mục
# frontend/ làm file tĩnh; index.html tự include grid.css/grid.js bằng
# đường dẫn tương đối.
# =============================================================================

import os

import streamlit.components.v1 as components

_COMPONENT_DIR = os.path.join(os.path.dirname(__file__), "frontend")
_tkb_grid = components.declare_component("tkb_grid", path=_COMPONENT_DIR)


def tkb_grid(grid_data: dict, key: str | None = None, height: int = 520) -> dict | None:
    """Vẽ lưới tuần của 1 lớp với kéo-thả HTML5 thật.

    grid_data: xem cấu trúc trong manual_edit._build_grid_data().
    Trả về payload sự kiện thả gần nhất dạng
    {"event": "drop", "lesson_id": ..., "source": {...}, "target": {...}, "drop_seq": ...}
    hoặc None nếu chưa có lần thả nào.
    """
    return _tkb_grid(grid_data=grid_data, key=key, height=height, default=None)
