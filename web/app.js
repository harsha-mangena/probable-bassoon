"use strict";
const $ = (id) => document.getElementById(id);
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const state = {
  config: null,
  doctors: [],
  doctor: null,
  slot: null,
  hold: null,
  staff: null,
  call: null,
  lookup: null,
  move: null,
  busy: false,
  slotsRequest: 0,
};
const key = () => crypto.randomUUID();
let noticeTimer;
function notify(message, error = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
  $("notice").hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => ($("notice").hidden = true), 8000);
}
async function api(path, body) {
  const r = await fetch("/api" + path, {
    method: body === undefined ? "GET" : "POST",
    headers: {
      "Content-Type": "application/json",
      ...(state.staff ? { "X-CSRF-Token": state.staff.csrf } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await r.json();
  if (!r.ok) {
    if (r.status === 401 && state.staff) {
      state.staff = null;
      showStaffLogin();
    }
    throw new Error(data.error || "Request failed");
  }
  return data;
}
function bind(id, event, fn) {
  $(id).addEventListener(event, async (e) => {
    try {
      await fn(e);
    } catch (error) {
      notify(error.message, true);
    }
  });
}
async function task(button, fn) {
  if (button.disabled) return;
  button.disabled = true;
  try {
    return await fn();
  } finally {
    button.disabled = false;
  }
}
function showView(name) {
  document
    .querySelectorAll("[data-view]")
    .forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  for (const n of ["book", "call", "manage"])
    $("view-" + n).hidden = n !== name;
}
function showStaffTab(name) {
  if (name === "admin" && state.staff?.role !== "admin") return;
  document
    .querySelectorAll("[data-staff]")
    .forEach((b) => b.classList.toggle("active", b.dataset.staff === name));
  for (const n of ["appointments", "availability", "activity", "admin"])
    $("staff-" + n).hidden = n !== name;
}
function doctorsView() {
  const initials = (d) =>
    d.name
      .replace(/^Dr\.\s*/, "")
      .split(" ")
      .map((x) => x[0])
      .slice(0, 2)
      .join("");
  $("doctor-cards").innerHTML =
    state.doctors
      .map(
        (d) =>
          `<button class="doctor-card ${state.doctor?.id === d.id ? "selected" : ""}" data-doctor="${esc(d.id)}" aria-pressed="${state.doctor?.id === d.id}"><span class="avatar">${esc(initials(d))}</span><div><strong>${esc(d.name)}</strong><span class="meta">${esc(d.specialty)}</span><span class="meta">${d.duration} minute visit</span></div></button>`,
      )
      .join("") ||
    "<p>No doctors are currently available. Contact the clinic.</p>";
}
function summary() {
  const d = state.doctor,
    a = state.slot;
  $("selection-summary").innerHTML =
    d && a
      ? `<strong>${esc(d.name)}</strong><p>${esc(a.label)}</p><p class="muted">${d.duration} minutes · ${esc(a.timezone)}</p><p class="muted">${$("book-count").value === "1" ? "One visit" : `${$("book-count").value} visits, every ${$("book-interval").value} week(s)`}</p>`
      : "Choose a doctor and a time to get started.";
}
async function fetchSlots() {
  state.slot = null;
  summary();
  if (!state.doctor) return;
  const req = ++state.slotsRequest;
  $("slots").innerHTML = '<p class="muted">Checking live availability…</p>';
  const data = await api(
    `/slots?doctor_id=${encodeURIComponent(state.doctor.id)}&day=${$("book-day").value}`,
  );
  if (req !== state.slotsRequest) return;
  state.slots = data.slots;
  $("slot-count").textContent = data.slots.length + " open";
  $("slots").innerHTML =
    data.slots
      .map(
        (a, i) =>
          `<button data-slot="${i}" aria-pressed="false">${esc(new Date(a.start).toLocaleTimeString("en-US", { timeZone: state.config.clinic.timezone, hour: "numeric", minute: "2-digit" }))}</button>`,
      )
      .join("") ||
    '<p class="muted">No available times on this date. Try another day.</p>';
}
function freezeBooking(value) {
  for (const id of ["book-day", "book-count", "book-interval"])
    $(id).disabled = value;
  document
    .querySelectorAll("[data-doctor],[data-slot]")
    .forEach((b) => (b.disabled = value));
  $("booking-form").hidden = value;
  $("hold-review").hidden = !value;
}
async function releaseHold() {
  if (state.hold)
    await api("/holds/release", {
      hold_id: state.hold.hold_id,
      idempotency_key: state.holdKey,
    });
  state.hold = null;
  freezeBooking(false);
  await fetchSlots();
}
function resultHTML(result) {
  const confirmed = result.appointments.filter((a) => a.status === "confirmed");
  return `<h3>${confirmed.length ? "You’re booked. We’ll see you soon." : "Booking reference"}</h3><p class="hint">Save both numbers securely. The private code allows you to view, cancel, or move your appointments.</p><div class="credential">Booking reference<strong>${esc(result.reference)}</strong></div><div class="credential">Private management code<strong>${esc(result.manage_code)}</strong></div>${result.appointments.map((a) => `<p>${esc(a.doctor_name)} · ${esc(a.label)} · ${esc(a.status)}</p>`).join("")}<button class="quiet" data-save-reference data-reference="${esc(result.reference)}" data-code="${esc(result.manage_code)}">Manage these appointments →</button>`;
}
async function reviewBooking(e) {
  e.preventDefault();
  await task($("review-booking"), async () => {
    if (!state.slot) throw new Error("Choose an available time first.");
    state.patient = {
      name: $("patient-name").value.trim(),
      phone: $("patient-phone").value.trim(),
    };
    state.holdKey = key();
    state.commitKey = key();
    const hold = await api(state.staff ? "/admin/holds" : "/holds", {
      doctor_id: state.doctor.id,
      start: state.slot.start,
      count: Number($("book-count").value),
      interval_weeks: Number($("book-interval").value),
      idempotency_key: state.holdKey,
    });
    state.hold = hold;
    $("hold-summary").innerHTML =
      "<h4>Review your visits</h4>" +
      hold.appointments
        .map((a) => `<p class="hint">${esc(a.label)}</p>`)
        .join("") +
      `<p>${esc(state.patient.name)}<br>${esc(state.patient.phone)}</p>`;
    $("hold-expiry").textContent =
      "Held until " +
      new Date(hold.expires).toLocaleTimeString("en-US", {
        timeZone: state.config.clinic.timezone,
      }) +
      " (" +
      state.config.clinic.timezone +
      ").";
    freezeBooking(true);
  });
}
async function confirmBooking() {
  await task($("confirm-booking"), async () => {
    const result = await api(state.staff ? "/admin/bookings" : "/bookings", {
      hold_id: state.hold.hold_id,
      ...state.patient,
      idempotency_key: state.commitKey,
    });
    state.hold = null;
    freezeBooking(false);
    $("booking-result").hidden = false;
    $("booking-result").innerHTML = resultHTML(result);
    $("booking-result").scrollIntoView({ behavior: "smooth", block: "center" });
    notify("Appointment confirmed. Save your reference and private code.");
    await fetchSlots();
  });
}
function rowHTML(a, staff = false) {
  return `<article class="appointment-row"><div><h3>${esc(staff ? a.patient_name : a.doctor_name)}</h3><p>${esc(a.label)} · ${esc(a.timezone)}</p><p>${staff ? `${esc(a.doctor_name)} · ${esc(a.phone)} · Reference ${esc(a.group_id)}` : esc(a.specialty)}</p></div><div class="row-actions"><span class="status ${esc(a.status)}">${esc(a.status)}</span>${a.status === "confirmed" && a.future ? `<button class="quiet" data-move="${esc(a.id)}" data-for="${staff ? "staff" : "patient"}">Move</button><button class="danger" data-cancel="${esc(a.id)}" data-for="${staff ? "staff" : "patient"}">Cancel</button>` : ""}</div></article>`;
}
async function lookup(e) {
  if (e) e.preventDefault();
  const result = await api("/patient/bookings", {
    reference: $("manage-reference").value.trim(),
    manage_code: $("manage-code").value.trim(),
  });
  state.lookup = { ...result, manage_code: $("manage-code").value.trim() };
  for (const button of document.querySelectorAll("[data-save-reference]")) {
    if (button.dataset.reference === result.reference) {
      button.closest(".result").innerHTML = resultHTML(state.lookup);
    }
  }
  $("patient-appointments").innerHTML = result.appointments
    .map((a) => rowHTML(a))
    .join("");
  $("cancel-series").hidden = !result.appointments.some(
    (a) => a.status === "confirmed" && a.future,
  );
}
let confirmResolve;
function confirmAction(message) {
  $("confirm-message").textContent = message;
  $("confirm-dialog").showModal();
  return new Promise((resolve) => (confirmResolve = resolve));
}
function resolveConfirm(value) {
  $("confirm-dialog").close();
  if (confirmResolve) {
    confirmResolve(value);
    confirmResolve = null;
  }
}
async function cancelAppointment(id, staff = false) {
  const rows = staff ? state.overview.appointments : state.lookup.appointments;
  const a = rows.find((x) => x.id === id);
  if (
    !(await confirmAction(
      `Cancel the appointment on ${a.label} with ${a.doctor_name}?`,
    ))
  )
    return;
  await api(staff ? "/admin/cancel" : "/patient/cancel", {
    reference: a.group_id,
    appointment_id: id,
    ...(!staff ? { manage_code: state.lookup.manage_code } : {}),
  });
  notify("Appointment cancelled.");
  if (staff) await loadStaff();
  else await lookup();
}
async function openMove(id, staff = false) {
  const rows = staff ? state.overview.appointments : state.lookup.appointments;
  const a = rows.find((x) => x.id === id);
  state.move = { a, staff };
  $("move-current").textContent = a.doctor_name + " · " + a.label;
  $("move-day").value = a.start.slice(0, 10);
  $("move-day").min = state.config.today;
  $("move-dialog").showModal();
  await moveSlots();
}
async function moveSlots() {
  const a = state.move.a;
  const data = await api(
    `/slots?doctor_id=${encodeURIComponent(a.doctor_id)}&day=${$("move-day").value}`,
  );
  $("move-slot").innerHTML = data.slots
    .map((x) => `<option value="${esc(x.start)}">${esc(x.label)}</option>`)
    .join("");
  if (!data.slots.length)
    $("move-slot").innerHTML =
      '<option value="">No available times. Choose another date.</option>';
}
async function moveAppointment(e) {
  e.preventDefault();
  if (!$("move-slot").value) throw new Error("Choose an available time.");
  const { a, staff } = state.move;
  await api(staff ? "/admin/move" : "/patient/move", {
    appointment_id: a.id,
    start: $("move-slot").value,
    ...(!staff ? { manage_code: state.lookup.manage_code } : {}),
  });
  $("move-dialog").close();
  notify("Appointment rescheduled.");
  if (staff) await loadStaff();
  else await lookup();
}
function bubble(text, caller = false) {
  const el = document.createElement("div");
  el.className = "bubble" + (caller ? " caller" : "");
  el.textContent = text;
  $("transcript").appendChild(el);
  $("transcript").scrollTop = $("transcript").scrollHeight;
}
function speak(text) {
  if (!$("voice-consent").checked || !window.speechSynthesis) return;
  window.speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.lang = "en-US";
  u.rate = 0.96;
  window.speechSynthesis.speak(u);
}
function renderCall(result) {
  state.call = result;
  const done = result.stage === "done";
  $("call-status").textContent = done
    ? "Call complete"
    : "Connected · " + result.stage.replaceAll("-", " ");
  $("call-text").disabled = done;
  $("call-send").disabled = done;
  $("mic").disabled = done || !$("voice-consent").checked;
  $("end-call").hidden = done;
  $("start-call").disabled = !done;
  $("call-choices").innerHTML = result.choices
    .map(
      (c) =>
        `<button data-reply="${esc(/^\d+\./.test(c) ? c.split(".")[0] : c)}">${esc(c)}</button>`,
    )
    .join("");
  bubble(result.message);
  speak(result.message);
  if (result.result?.reference && result.result?.manage_code) {
    $("call-result").hidden = false;
    $("call-result").className = "result";
    $("call-result").innerHTML = resultHTML(result.result);
  }
}
async function startCall() {
  if (state.call && state.call.stage !== "done") await sendCall("stop");
  const result = await api("/call/start", {});
  $("transcript").innerHTML = "";
  $("call-result").hidden = true;
  renderCall(result);
  $("call-text").focus();
}
async function sendCall(text) {
  if (state.busy || !state.call || state.call.stage === "done") return;
  state.busy = true;
  $("call-send").disabled = true;
  const turn = {
    call_id: state.call.call_id,
    seq: state.call.seq,
    text,
    request_id: key(),
  };
  bubble(text, true);
  try {
    let result;
    try {
      result = await api("/call/turn", turn);
    } catch (error) {
      if (error instanceof TypeError) result = await api("/call/turn", turn);
      else throw error;
    }
    renderCall(result);
    $("call-text").value = "";
    $("call-text").focus();
  } finally {
    state.busy = false;
    $("call-send").disabled = state.call?.stage === "done";
  }
}
let recognition;
function mic() {
  if (!$("voice-consent").checked) throw new Error("Enable voice input first.");
  const Speech = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Speech)
    throw new Error(
      "Speech recognition is unavailable in this browser. Please type your response.",
    );
  if (recognition) {
    recognition.abort();
    recognition = null;
    return;
  }
  window.speechSynthesis?.cancel();
  recognition = new Speech();
  recognition.lang = "en-US";
  recognition.interimResults = false;
  recognition.onstart = () => {
    $("mic").textContent = "● Listening…";
  };
  recognition.onend = () => {
    $("mic").textContent = "🎙 Speak";
    recognition = null;
  };
  recognition.onerror = (e) =>
    notify("Microphone: " + e.error + ". You can type your response.", true);
  recognition.onresult = (e) =>
    sendCall(e.results[0][0].transcript).catch((error) =>
      notify(error.message, true),
    );
  recognition.start();
}
function showStaffLogin() {
  $("staff-login").hidden = false;
  $("staff-control").hidden = true;
  $("login-password").value = "";
}
async function enterStaff() {
  const entering = $("staff-app").hidden;
  $("staff-app").hidden = !entering;
  $("patient-app").hidden = entering;
  $("staff-switch").textContent = entering
    ? "Patient portal ↗"
    : "Staff portal ↗";
  if (entering) {
    try {
      state.staff = await api("/auth/me");
      await loadStaff();
    } catch (error) {
      showStaffLogin();
    }
  }
}
async function loadStaff() {
  if (!state.staff) return;
  $("staff-login").hidden = true;
  $("staff-control").hidden = false;
  $("staff-identity").textContent =
    state.staff.username + " · " + state.staff.role;
  $("admin-tab").hidden = state.staff.role !== "admin";
  if (state.staff.role !== "admin") $("staff-admin").hidden = true;
  const params = new URLSearchParams();
  if ($("staff-day").value) params.set("day", $("staff-day").value);
  if ($("staff-status").value) params.set("status", $("staff-status").value);
  if ($("staff-doctor").value) params.set("doctor_id", $("staff-doctor").value);
  const data = await api("/admin/overview?" + params);
  state.overview = data;
  $("stats").innerHTML = [
    [data.today, "Appointments today"],
    [data.stats.confirmed || 0, "Confirmed visits"],
    [data.stats.held || 0, "Active holds"],
    [data.calls, "Recent calls"],
  ]
    .map(
      ([n, label]) =>
        `<div class="stat"><b>${n}</b><span>${label}</span></div>`,
    )
    .join("");
  $("staff-appointment-list").innerHTML =
    data.appointments.map((a) => rowHTML(a, true)).join("") ||
    '<section class="panel"><p class="muted">No appointments match these filters.</p></section>';
  $("audit-list").innerHTML = data.audit
    .map(
      (a) =>
        `<div class="audit-row"><span>${esc(a.action)}<br><span class="muted">${esc(a.actor)} · ${esc(a.target)}</span></span><span class="muted">${esc(new Date(a.at).toLocaleString())}</span></div>`,
    )
    .join("");
  const selected = $("staff-doctor").value;
  $("staff-doctor").innerHTML =
    '<option value="">All doctors</option>' +
    data.doctors
      .map((d) => `<option value="${esc(d.id)}">${esc(d.name)}</option>`)
      .join("");
  $("staff-doctor").value = selected;
  const selectedAvail = $("availability-doctor").value;
  $("availability-doctor").innerHTML = data.doctors
    .filter((d) => d.active)
    .map((d) => `<option value="${esc(d.id)}">${esc(d.name)}</option>`)
    .join("");
  if (selectedAvail) $("availability-doctor").value = selectedAvail;
  await loadSchedule();
  if (state.staff.role === "admin") {
    $("doctor-admin-list").innerHTML = data.doctors
      .map(
        (d) =>
          `<p class="hint"><button class="quiet" data-edit-doctor="${esc(d.id)}">Edit</button> ${esc(d.name)} · ${d.active ? "Active" : "Inactive"}</p>`,
      )
      .join("");
    const users = await api("/admin/users");
    $("user-list").innerHTML = users.users
      .map((u) => `<p class="hint">${esc(u.username)} · ${esc(u.role)}</p>`)
      .join("");
  }
}
const weekdays = [
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
  "Sunday",
];
async function loadSchedule() {
  if (!$("availability-doctor").value) {
    $("schedule-view").innerHTML = "No active doctors.";
    return;
  }
  state.schedule = await api(
    "/admin/availability?doctor_id=" +
      encodeURIComponent($("availability-doctor").value),
  );
  $("schedule-view").innerHTML =
    "<h4>Weekly hours</h4>" +
    Object.entries(state.schedule.weekly)
      .map(
        ([i, w]) =>
          `<div class="${w.length ? "" : "closed"}">${weekdays[i]} · ${esc(w.length ? w.map((x) => x.join("–")).join(", ") : "Closed")}</div>`,
      )
      .join("") +
    "<h4>Date overrides</h4>" +
    Object.entries(state.schedule.overrides)
      .map(
        ([day, w]) =>
          `<div>${esc(day)} · ${esc(w.length ? w.map((x) => x.join("–")).join(", ") : "Closed")} <button class="quiet" data-override="${esc(day)}">Edit</button></div>`,
      )
      .join("");
  scheduleFields();
}
function scheduleFields() {
  const override = $("availability-type").value === "override";
  $("weekday-field").hidden = override;
  $("override-field").hidden = !override;
  $("remove-override").hidden = !override;
  const w = override
    ? state.schedule?.overrides[$("availability-day").value]
    : state.schedule?.weekly[$("availability-weekday").value];
  $("availability-windows").value = (w || [])
    .map((x) => x.join("-"))
    .join("\n");
}
async function saveAvailability(e, remove = false) {
  if (e) e.preventDefault();
  const override = $("availability-type").value === "override";
  if (override && !$("availability-day").value)
    throw new Error("Choose an override date.");
  const windows = $("availability-windows")
    .value.trim()
    .split("\n")
    .filter((x) => x.trim())
    .map((x) =>
      x
        .trim()
        .split("-")
        .map((v) => v.trim()),
    );
  await api("/admin/availability", {
    doctor_id: $("availability-doctor").value,
    windows,
    ...(override
      ? { day: $("availability-day").value, remove }
      : { weekday: Number($("availability-weekday").value) }),
  });
  notify("Availability saved.");
  await loadSchedule();
  state.doctors = (await api("/doctors")).doctors;
  doctorsView();
  await fetchSlots();
}
async function saveDoctor(e) {
  e.preventDefault();
  await api("/admin/doctors", {
    id: $("doctor-id").value,
    name: $("doctor-name").value,
    specialty: $("doctor-specialty").value,
    duration: Number($("doctor-duration").value),
    buffer: Number($("doctor-buffer").value),
    active: $("doctor-active").value === "true",
  });
  notify("Doctor saved. Set their availability in the schedule tab.");
  state.doctors = (await api("/doctors")).doctors;
  state.doctor =
    state.doctors.find((d) => d.id === state.doctor?.id) || state.doctors[0];
  doctorsView();
  await loadStaff();
  await fetchSlots();
}
function editDoctor(id) {
  const d = state.overview.doctors.find((x) => x.id === id);
  for (const [field, v] of Object.entries({
    id: d.id,
    name: d.name,
    specialty: d.specialty,
    duration: d.duration,
    buffer: d.buffer,
    active: !!d.active,
  }))
    $("doctor-" + field).value = String(v);
  $("doctor-id").focus();
}
async function init() {
  state.config = await api("/config");
  state.doctors = (await api("/doctors")).doctors;
  state.doctor = state.doctors[0] || null;
  document.title = state.config.clinic.name + " · Scheduling";
  $("footer-name").textContent = state.config.clinic.name;
  $("clinic-timezone").textContent = state.config.clinic.timezone;
  $("book-day").value = state.config.today;
  $("book-day").min = state.config.today;
  const max = new Date(state.config.today + "T12:00:00Z");
  max.setUTCDate(max.getUTCDate() + state.config.policy.max_advance_days);
  $("book-day").max = max.toISOString().slice(0, 10);
  $("cancel-policy").textContent =
    `Keep your private management code secure. Patient changes require at least ${state.config.policy.cancellation_notice_minutes / 60} hours’ notice. Contact clinic staff for later changes.`;
  $("phone-channel").textContent = state.config.voice.phone_enabled
    ? "The clinic’s phone adapter is enabled. Ask clinic staff for the configured phone number."
    : "Browser calling is ready. A clinic phone number can be connected using the optional phone adapter.";
  $("voice-consent").checked = false;
  doctorsView();
  await fetchSlots();
}
bind("staff-switch", "click", enterStaff);
bind("book-day", "change", fetchSlots);
bind("book-count", "change", summary);
bind("book-interval", "change", summary);
bind("booking-form", "submit", reviewBooking);
bind("confirm-booking", "click", confirmBooking);
bind("release-booking", "click", releaseHold);
bind("lookup-form", "submit", lookup);
bind("cancel-series", "click", async () => {
  if (
    !(await confirmAction("Cancel every remaining appointment in this series?"))
  )
    return;
  await api("/patient/cancel", {
    reference: state.lookup.reference,
    manage_code: state.lookup.manage_code,
  });
  notify("Series cancelled.");
  await lookup();
});
bind("move-day", "change", moveSlots);
bind("move-form", "submit", moveAppointment);
bind("close-move", "click", () => $("move-dialog").close());
bind("confirm-yes", "click", () => resolveConfirm(true));
bind("confirm-no", "click", () => resolveConfirm(false));
bind("confirm-dialog", "cancel", () => resolveConfirm(false));
bind("start-call", "click", () => task($("start-call"), startCall));
bind("end-call", "click", () => sendCall("stop"));
bind("call-form", "submit", (e) => {
  e.preventDefault();
  return sendCall($("call-text").value);
});
bind("mic", "click", mic);
bind("voice-consent", "change", () => {
  $("mic").disabled =
    !$("voice-consent").checked || !state.call || state.call.stage === "done";
  if (!$("voice-consent").checked) {
    recognition?.abort();
    window.speechSynthesis?.cancel();
  }
});
bind("login-form", "submit", async (e) => {
  e.preventDefault();
  state.staff = await api("/auth/login", {
    username: $("login-username").value,
    password: $("login-password").value,
  });
  $("login-password").value = "";
  showStaffTab("appointments");
  await loadStaff();
});
bind("logout", "click", async () => {
  await api("/auth/logout", {});
  state.staff = null;
  state.overview = null;
  for (const id of [
    "staff-appointment-list",
    "audit-list",
    "user-list",
    "doctor-admin-list",
  ])
    $(id).innerHTML = "";
  showStaffLogin();
});
bind("refresh-staff", "click", loadStaff);
for (const id of ["staff-day", "staff-doctor", "staff-status"])
  bind(id, "change", loadStaff);
bind("clear-filters", "click", () => {
  for (const id of ["staff-day", "staff-doctor", "staff-status"])
    $(id).value = "";
  return loadStaff();
});
bind("availability-doctor", "change", loadSchedule);
for (const id of [
  "availability-type",
  "availability-weekday",
  "availability-day",
])
  bind(id, "change", scheduleFields);
bind("availability-form", "submit", saveAvailability);
bind("remove-override", "click", () => saveAvailability(null, true));
bind("doctor-form", "submit", saveDoctor);
bind("user-form", "submit", async (e) => {
  e.preventDefault();
  await api("/admin/users", {
    username: $("user-username").value,
    password: $("user-password").value,
    role: $("user-role").value,
  });
  $("user-password").value = "";
  notify("Staff account created.");
  await loadStaff();
});
document.addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  try {
    if (b.dataset.saveReference !== undefined) {
      $("manage-reference").value = b.dataset.reference;
      $("manage-code").value = b.dataset.code;
      showView("manage");
      await lookup();
    }
    if (b.dataset.view) showView(b.dataset.view);
    if (b.dataset.staff) showStaffTab(b.dataset.staff);
    if (b.dataset.doctor) {
      state.doctor = state.doctors.find((d) => d.id === b.dataset.doctor);
      doctorsView();
      await fetchSlots();
    }
    if (b.dataset.slot !== undefined) {
      state.slot = state.slots[Number(b.dataset.slot)];
      document.querySelectorAll("[data-slot]").forEach((x) => {
        x.classList.toggle("selected", x === b);
        x.setAttribute("aria-pressed", String(x === b));
      });
      summary();
    }
    if (b.dataset.reply) await sendCall(b.dataset.reply);
    if (b.dataset.cancel)
      await cancelAppointment(b.dataset.cancel, b.dataset.for === "staff");
    if (b.dataset.move)
      await openMove(b.dataset.move, b.dataset.for === "staff");
    if (b.dataset.editDoctor) editDoctor(b.dataset.editDoctor);
    if (b.dataset.override) {
      $("availability-type").value = "override";
      $("availability-day").value = b.dataset.override;
      scheduleFields();
    }
  } catch (error) {
    notify(error.message, true);
  }
});
window.addEventListener("pagehide", () => {
  window.speechSynthesis?.cancel();
  recognition?.abort();
});
init().catch((error) => notify(error.message, true));

bind("staff-book", "click", () => {
  $("staff-app").hidden = true;
  $("patient-app").hidden = false;
  $("staff-switch").textContent = "Staff portal ↗";
  showView("book");
  $("view-book").scrollIntoView({ behavior: "smooth" });
});
