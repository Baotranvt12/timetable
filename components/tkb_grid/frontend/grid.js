/*
 * grid.js — component tĩnh cho Streamlit (không dùng npm/React, không tải
 * script từ CDN). Cài đặt trực tiếp giao thức postMessage giữa iframe
 * component và cửa sổ Streamlit cha:
 *   ra:  streamlit:componentReady, streamlit:setComponentValue, streamlit:setFrameHeight
 *   vào: streamlit:render  (event.data.args chứa các kwargs đã truyền từ Python)
 */

(function () {
  "use strict";

  function postToStreamlit(payload) {
    window.parent.postMessage(Object.assign({ isStreamlitMessage: true }, payload), "*");
  }

  function setComponentValue(value) {
    postToStreamlit({ type: "streamlit:setComponentValue", value: value, dataType: "json" });
  }

  function setFrameHeight() {
    var root = document.getElementById("tkb-grid-root");
    var h = root ? root.scrollHeight + 12 : 400;
    postToStreamlit({ type: "streamlit:setFrameHeight", height: h });
  }

  function componentReady() {
    postToStreamlit({ type: "streamlit:componentReady", apiVersion: 1 });
  }

  // ── Trạng thái nội bộ ──────────────────────────────────────────────────
  var dropSeq = 0;
  var currentGridData = null;

  function slotKey(thu, tiet, buoi) {
    return thu + "|" + tiet + "|" + buoi;
  }

  function render(gridData) {
    currentGridData = gridData;
    var root = document.getElementById("tkb-grid-root");
    root.innerHTML = "";

    if (!gridData || !gridData.days || !gridData.days.length) {
      var empty = document.createElement("div");
      empty.className = "tkb-empty";
      empty.textContent = "Chưa có dữ liệu TKB cho lớp này.";
      root.appendChild(empty);
      setFrameHeight();
      return;
    }

    var days = gridData.days;
    var periods = gridData.periods || { "Sáng": [1, 2, 3, 4], "Chiều": [1, 2, 3, 4] };
    var buoiOrder = Object.keys(periods);

    // Chỉ mục ô theo (thu,tiet,buoi) để tra nhanh khi render.
    var cellByKey = {};
    (gridData.cells || []).forEach(function (c) {
      cellByKey[slotKey(c.thu, c.tiet, c.buoi)] = c;
    });
    var freeTeachers = gridData.free_teachers || {};

    var panel = document.createElement("div");
    panel.id = "suggest-panel";
    panel.className = "suggest-panel";
    root.appendChild(panel);
    showSuggestions(null);

    var table = document.createElement("table");
    table.className = "tkb-grid";

    // ── Header ──
    var thead = document.createElement("thead");
    var headRow = document.createElement("tr");
    ["Buổi", "Tiết"].concat(days).forEach(function (label) {
      var th = document.createElement("th");
      th.textContent = label;
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    // ── Body ──
    var tbody = document.createElement("tbody");
    buoiOrder.forEach(function (buoi) {
      var tietList = periods[buoi];
      tietList.forEach(function (tiet, i) {
        var tr = document.createElement("tr");
        if (i === 0) {
          var buoiTh = document.createElement("th");
          buoiTh.textContent = buoi;
          buoiTh.rowSpan = tietList.length;
          tr.appendChild(buoiTh);
        }
        var tietTh = document.createElement("th");
        tietTh.textContent = tiet;
        tr.appendChild(tietTh);

        days.forEach(function (thu) {
          var key = slotKey(thu, tiet, buoi);
          var cellInfo = cellByKey[key];
          var td = document.createElement("td");
          td.className = "cell " + (cellInfo && cellInfo.lesson ? "occupied" : "empty");
          td.dataset.thu = thu;
          td.dataset.tiet = String(tiet);
          td.dataset.buoi = buoi;

          td.addEventListener("dragover", onDragOver);
          td.addEventListener("dragenter", onDragEnter);
          td.addEventListener("dragleave", onDragLeave);
          td.addEventListener("drop", onDrop);
          td.addEventListener("mouseenter", function () { showSuggestions(key); });

          var free = freeTeachers[key] || [];
          var hint = document.createElement("div");
          hint.className = "free-hint";
          if (free.length) {
            var shown = free.slice(0, 3).map(function (f) {
              return (f.missing > 0 ? "★" : "") + f.tat;
            });
            hint.textContent = shown.join(", ") + (free.length > 3 ? " +" + (free.length - 3) : "");
            td.title = "GV rảnh: " + free.map(function (f) {
              return f.ten_gv + " (" + f.mon + ")";
            }).join(", ");
          } else {
            hint.textContent = "— không GV rảnh —";
            hint.classList.add("none");
          }

          if (cellInfo && cellInfo.lesson) {
            var card = document.createElement("div");
            card.className = "lesson-card";
            card.draggable = true;
            card.dataset.lessonId = String(cellInfo.lesson.id);
            card.dataset.sourceThu = thu;
            card.dataset.sourceTiet = String(tiet);
            card.dataset.sourceBuoi = buoi;

            var monEl = document.createElement("div");
            monEl.className = "mon";
            monEl.textContent = cellInfo.lesson.mon || "";
            var gvEl = document.createElement("div");
            gvEl.className = "gv";
            gvEl.textContent = cellInfo.lesson.ten_gv || "";

            card.appendChild(monEl);
            card.appendChild(gvEl);
            card.addEventListener("dragstart", onDragStart);
            card.addEventListener("dragend", onDragEnd);
            td.appendChild(card);
          }
          if (cellInfo && cellInfo.group) {
            // HS của lớp đang học nhóm tự chọn ở ô này → không thả tiết vào được
            var tag = document.createElement("div");
            tag.className = "group-tag";
            tag.textContent = "🧩 " + cellInfo.group;
            tag.title = "Nhóm tự chọn: " + cellInfo.group + " — né ô này";
            td.appendChild(tag);
            td.classList.add("has-group");
          }
          td.appendChild(hint);

          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
    });
    table.appendChild(tbody);
    root.appendChild(table);
    renderUnplacedTray(gridData.unplaced || []);
    setFrameHeight();
  }

  // Khung gợi ý: danh sách đầy đủ GV của lớp rảnh ở ô đang trỏ/kéo qua.
  function showSuggestions(key) {
    var panel = document.getElementById("suggest-panel");
    if (!panel) return;
    panel.innerHTML = "";
    if (!key) {
      panel.textContent = "💡 Kéo một tiết hoặc rê chuột vào ô để xem GV của lớp còn rảnh. "
        + "★ = môn còn thiếu tiết.";
      return;
    }
    var parts = key.split("|");
    var free = ((currentGridData && currentGridData.free_teachers) || {})[key] || [];
    var head = document.createElement("strong");
    head.textContent = parts[0] + " — Tiết " + parts[1] + " " + parts[2] + ": ";
    panel.appendChild(head);
    if (!free.length) {
      panel.appendChild(document.createTextNode("không còn GV nào của lớp rảnh."));
      return;
    }
    free.forEach(function (f) {
      var chip = document.createElement("span");
      chip.className = "free-chip" + (f.missing > 0 ? " need" : "");
      chip.textContent = (f.missing > 0 ? "★ " : "") + f.ten_gv + " · " + f.mon
        + (f.missing > 0 ? " (thiếu " + f.missing + ")" : "");
      panel.appendChild(chip);
    });
  }

  // Khay các tiết CHƯA XẾP (vd môn tự chọn/nhóm vừa gán) — kéo từ đây vào
  // ô trống hợp lệ trong lưới, giống hệt kéo 1 thẻ đã có sẵn trong lưới.
  function renderUnplacedTray(unplaced) {
    var root = document.getElementById("tkb-grid-root");
    if (!unplaced.length) return;

    var wrap = document.createElement("div");
    wrap.className = "unplaced-tray";
    var title = document.createElement("div");
    title.className = "unplaced-tray-title";
    title.textContent = "🆕 Tiết chưa xếp (kéo vào ô trống hợp lệ):";
    wrap.appendChild(title);

    var cardsWrap = document.createElement("div");
    cardsWrap.className = "unplaced-tray-cards";
    unplaced.forEach(function (item) {
      var card = document.createElement("div");
      card.className = "lesson-card unplaced";
      card.draggable = true;
      card.dataset.lessonId = item.id;
      card.dataset.isNew = "true";

      var monEl = document.createElement("div");
      monEl.className = "mon";
      monEl.textContent = item.mon || "";
      var gvEl = document.createElement("div");
      gvEl.className = "gv";
      gvEl.textContent = item.ten_gv || "";

      card.appendChild(monEl);
      card.appendChild(gvEl);
      card.addEventListener("dragstart", onDragStart);
      card.addEventListener("dragend", onDragEnd);
      cardsWrap.appendChild(card);
    });
    wrap.appendChild(cardsWrap);
    root.appendChild(wrap);
  }

  // ── Kéo-thả ─────────────────────────────────────────────────────────────

  function onDragStart(ev) {
    var card = ev.currentTarget;
    card.classList.add("dragging");
    ev.dataTransfer.effectAllowed = "move";
    ev.dataTransfer.setData("text/plain", card.dataset.lessonId);

    document.body.classList.add("dragging-active");
    var legal = (currentGridData.legal_targets || {})[card.dataset.lessonId] || [];
    var legalSet = {};
    legal.forEach(function (s) { legalSet[slotKey(s[0], s[1], s[2])] = true; });

    document.querySelectorAll("td.cell.empty").forEach(function (td) {
      var key = slotKey(td.dataset.thu, td.dataset.tiet, td.dataset.buoi);
      if (legalSet[key]) td.classList.add("legal-target");
    });
    var src = card.closest("td.cell");
    if (src) src.classList.add("drag-source");
  }

  function onDragEnd(ev) {
    ev.currentTarget.classList.remove("dragging");
    document.body.classList.remove("dragging-active");
    document.querySelectorAll("td.cell.legal-target, td.cell.drag-source").forEach(function (td) {
      td.classList.remove("legal-target", "drag-over", "drag-source");
    });
  }

  function onDragOver(ev) {
    if (ev.currentTarget.classList.contains("legal-target")) {
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "move";
    }
  }

  function onDragEnter(ev) {
    var td = ev.currentTarget;
    showSuggestions(slotKey(td.dataset.thu, td.dataset.tiet, td.dataset.buoi));
    if (td.classList.contains("legal-target")) {
      ev.preventDefault();
      ev.currentTarget.classList.add("drag-over");
    }
  }

  function onDragLeave(ev) {
    ev.currentTarget.classList.remove("drag-over");
  }

  function onDrop(ev) {
    var td = ev.currentTarget;
    if (!td.classList.contains("legal-target")) return; // ô không hợp lệ — không nhận thả
    ev.preventDefault();
    td.classList.remove("drag-over");

    var lessonId = ev.dataTransfer.getData("text/plain");
    var card = document.querySelector('.lesson-card[data-lesson-id="' + lessonId + '"]');
    if (!card) return;

    dropSeq += 1;
    setComponentValue({
      event: "drop",
      lesson_id: isNaN(Number(lessonId)) ? lessonId : Number(lessonId),
      source: card.dataset.isNew ? null : {
        thu: card.dataset.sourceThu,
        tiet: Number(card.dataset.sourceTiet),
        buoi: card.dataset.sourceBuoi,
      },
      target: {
        thu: td.dataset.thu,
        tiet: Number(td.dataset.tiet),
        buoi: td.dataset.buoi,
      },
      drop_seq: dropSeq,
    });
  }

  // ── Nhận sự kiện render từ Streamlit ─────────────────────────────────────
  function onMessageFromStreamlit(event) {
    var data = event.data;
    if (!data || data.type !== "streamlit:render") return;
    var gridData = (data.args && data.args.grid_data) || null;
    render(gridData);
  }

  window.addEventListener("message", onMessageFromStreamlit);
  componentReady();
  // Chiều cao ban đầu trước khi có dữ liệu thật.
  setFrameHeight();
})();
