/* Collis Family Planner — app JS */

// ── Toast notifications ────────────────────────────────────────────────────── //
function showToast(msg, type = "success") {
  const container = document.getElementById("toast-container") || (() => {
    const c = document.createElement("div");
    c.id = "toast-container";
    c.className = "toast-container";
    document.body.appendChild(c);
    return c;
  })();
  const t = document.createElement("div");
  t.className = `toast toast-${type}`;
  t.textContent = msg;
  container.appendChild(t);
  setTimeout(() => t.remove(), 3000);
}

// ── Generic POST helper ────────────────────────────────────────────────────── //
async function postAction(url, formData = null, successMsg = null, errorMsg = "Something went wrong") {
  try {
    const opts = { method: "POST" };
    if (formData) opts.body = formData;
    const resp = await fetch(url, opts);
    const json = await resp.json().catch(() => ({}));
    if (resp.ok && json.ok !== false) {
      if (successMsg) showToast(successMsg);
      return json;
    } else {
      showToast(json.error || errorMsg, "error");
      return null;
    }
  } catch (e) {
    showToast(errorMsg, "error");
    return null;
  }
}

// ── Task check/uncheck ─────────────────────────────────────────────────────── //
document.addEventListener("click", async (e) => {
  const checkBtn = e.target.closest(".task-check");
  if (!checkBtn) return;
  const taskItem = checkBtn.closest(".task-item");
  const taskId   = taskItem?.dataset.taskId;
  if (!taskId) return;

  const isChecked = checkBtn.classList.contains("checked");
  const url = isChecked ? `/tasks/${taskId}/uncomplete` : `/tasks/${taskId}/complete`;
  const result = await postAction(url, new FormData());
  if (!result) return;

  checkBtn.classList.toggle("checked", !isChecked);
  checkBtn.textContent = isChecked ? "" : "✓";

  const style = document.body.dataset.completedStyle || "fade";
  taskItem.classList.toggle("completed-item", !isChecked);
  taskItem.classList.toggle(`style-${style}`, !isChecked);

  updateCompletedCount();
});

// ── Completed count (collapse mode) ───────────────────────────────────────── //
function updateCompletedCount() {
  const countEl = document.getElementById("completed-count");
  if (!countEl) return;
  const done = document.querySelectorAll(".task-item.completed-item").length;
  countEl.textContent = done > 0 ? `${done} completed — tap to show` : "";
  countEl.style.display = done > 0 ? "block" : "none";
}

document.addEventListener("DOMContentLoaded", () => {
  updateCompletedCount();

  // Collapse style: hide completed initially, show on count click
  const countEl = document.getElementById("completed-count");
  if (countEl) {
    countEl.addEventListener("click", () => {
      document.querySelectorAll(".task-item.completed-item.style-collapse")
        .forEach(el => el.style.display = "flex");
      countEl.style.display = "none";
    });
  }
});

// ── Task defer ────────────────────────────────────────────────────────────── //
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".task-defer-btn");
  if (!btn) return;
  const taskId  = btn.dataset.taskId;
  const tomorrow = new Date();
  tomorrow.setDate(tomorrow.getDate() + 1);
  const deferTo = tomorrow.toISOString().split("T")[0];
  const fd = new FormData();
  fd.append("defer_to", deferTo);
  postAction(`/tasks/${taskId}/defer`, fd, "Task deferred to tomorrow");
  btn.closest(".task-item")?.remove();
});

// ── Exec-function transfer ─────────────────────────────────────────────────── //
document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".exec-transfer-btn");
  if (!btn) return;
  const taskId = btn.dataset.taskId;
  const result = await postAction(`/tasks/${taskId}/transfer`, new FormData(), "Transferred! They'll get a reminder.");
  if (result) {
    btn.closest(".task-item")?.classList.add("transferred-away");
    btn.textContent = `✓ Transferred to ${result.transferred_to}`;
    btn.disabled = true;
  }
});

// ── Task delete ───────────────────────────────────────────────────────────── //
document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".task-delete-btn");
  if (!btn) return;
  if (!confirm("Delete this task?")) return;
  const taskId = btn.dataset.taskId;
  const result = await postAction(`/tasks/${taskId}/delete`, new FormData());
  if (result) btn.closest(".task-item")?.remove();
});

// ── Medicine take/untake ───────────────────────────────────────────────────── //
document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".take-btn");
  if (!btn) return;
  const medId      = btn.dataset.medId;
  const doseNumber = btn.dataset.doseNumber || "1";
  const isTaken    = btn.classList.contains("taken");
  const url = isTaken ? `/medicines/${medId}/untake` : `/medicines/${medId}/take`;
  const fd = new FormData();
  fd.append("dose_number", doseNumber);
  if (typeof _viewDate !== "undefined" && typeof _isToday !== "undefined" && !_isToday) {
    fd.append("dose_date", _viewDate);
  }
  const result = await postAction(url, fd);
  if (!result) return;
  btn.classList.toggle("taken", !isTaken);
  btn.textContent = isTaken ? "Take" : "✓";
});

// ── Shopping item check ────────────────────────────────────────────────────── //
document.addEventListener("change", async (e) => {
  const cb = e.target.closest(".shopping-checkbox");
  if (!cb) return;
  const itemId  = cb.dataset.itemId;
  const checked = cb.checked;
  const fd = new FormData();
  fd.append("checked", checked ? "1" : "0");
  await postAction(`/shopping/${itemId}/check`, fd);
  cb.closest(".shopping-item")?.classList.toggle("checked", checked);
});

// ── Shopping item delete ───────────────────────────────────────────────────── //
document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".shopping-delete-btn");
  if (!btn) return;
  const itemId = btn.dataset.itemId;
  const result = await postAction(`/shopping/${itemId}/delete`, new FormData());
  if (result) btn.closest(".shopping-item")?.remove();
});

// ── Modal open/close ───────────────────────────────────────────────────────── //
document.addEventListener("click", (e) => {
  // Open
  const openBtn = e.target.closest("[data-modal]");
  if (openBtn) {
    const id = openBtn.dataset.modal;
    document.getElementById(id)?.classList.add("open");
    return;
  }
  // Close
  if (e.target.classList.contains("modal-backdrop") || e.target.classList.contains("modal-close")) {
    e.target.closest(".modal-backdrop")?.classList.remove("open");
  }
});

// ── Scroll to highlighted item ─────────────────────────────────────────────── //
document.addEventListener("DOMContentLoaded", () => {
  const highlighted = document.querySelector(".highlight");
  if (highlighted) {
    highlighted.scrollIntoView({ behavior: "smooth", block: "center" });
    highlighted.style.animation = "pulse 1s ease 2";
  }
});

// ── Admin actions (session-authenticated — no PIN prompt needed) ──────────── //
async function adminPost(url, formData = null, successMsg = null) {
  return postAction(url, formData || new FormData(), successMsg);
}

// ── UniFi WLAN toggle ─────────────────────────────────────────────────────── //
document.addEventListener("change", async (e) => {
  const tog = e.target.closest(".wlan-toggle");
  if (!tog) return;
  const ssid    = tog.dataset.ssid;
  const enabled = tog.checked;
  const fd = new FormData();
  fd.append("enabled", enabled ? "true" : "false");
  const result = await adminPost(`/admin/wlan/${encodeURIComponent(ssid)}/toggle`, fd);
  if (!result) tog.checked = !enabled; // revert on failure
  else showToast(`${ssid} ${enabled ? "enabled" : "disabled"}`);
});

// ── Device block / unblock / kick ─────────────────────────────────────────── //
document.addEventListener("click", async (e) => {
  const btn    = e.target.closest("[data-device-action]");
  if (!btn) return;
  const devId  = btn.dataset.deviceId;
  const action = btn.dataset.deviceAction;
  const labels = { block: "Block", unblock: "Unblock", kick: "Kick" };
  if (!confirm(`${labels[action]} this device?`)) return;
  await adminPost(`/admin/device/${devId}/${action}`, new FormData());
});

// ── Clock (for work meetings view) ────────────────────────────────────────── //
function startClock() {
  const el = document.getElementById("live-clock");
  if (!el) return;
  const tick = () => {
    const now = new Date();
    el.textContent = now.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  };
  tick();
  setInterval(tick, 1000);
}
document.addEventListener("DOMContentLoaded", startClock);

// ── Device theme pin ──────────────────────────────────────────────────────── //
function toggleDeviceTheme() {
  const pinned = document.body.dataset.deviceTheme === "dark";
  if (pinned) {
    // Unpin — let person preference take over
    document.cookie = "device_theme=; path=/; max-age=0; SameSite=Lax";
  } else {
    // Pin dark on this device for 1 year
    document.cookie = "device_theme=dark; path=/; max-age=31536000; SameSite=Lax";
  }
  location.reload();
}

// ── NTFY test ─────────────────────────────────────────────────────────────── //
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("#ntfy-test-btn");
  if (!btn) return;
  const result = await postAction("/settings/ntfy_test", new FormData(), "Test notification sent!");
  if (!result) showToast("Make sure your NTFY channel is saved first.", "error");
});
