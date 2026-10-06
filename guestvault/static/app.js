"use strict";
const $ = id => document.getElementById(id);
const token = location.hash.slice(1) || sessionStorage.getItem("guestvault-session");
if (token) sessionStorage.setItem("guestvault-session", token);
history.replaceState(null, "", location.pathname);
let state, initialized = false, pickerField, pickerKind, pickerParent;
const lines = value => value.split("\n").map(s => s.trim()).filter(Boolean);
const readableDate = value => value ? new Date(value).toLocaleString() : "No backups yet";
function notice(text) { $("notice").textContent = text; $("notice").classList.toggle("hidden", !text); }
async function api(path, data) {
  const options = { headers: { Authorization: `Bearer ${token || ""}` } };
  if (data !== undefined) { options.method = "POST"; options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(data); }
  const response = await fetch(path, options);
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || "Operation failed.");
  return value;
}
function modeChanged() {
  $("sources-field").classList.toggle("hidden", $("mode").value !== "folders");
  $("image-field").classList.toggle("hidden", $("mode").value !== "windows-image");
}
function protectionChanged() {
  const protectedBackup = $("protected").checked;
  $("protection-label").textContent = protectedBackup ? "Password protected" : "No password";
  $("password-fields").classList.toggle("hidden", !protectedBackup);
  $("remember").disabled = !protectedBackup;
  if (!protectedBackup) $("remember").checked = false;
  $("protection-note").textContent = protectedBackup ? "Protected backups need their password to restore." : "No password: anyone with your backup file can read its contents.";
}
function planData() {
  return {mode: $("mode").value, sources: lines($("sources").value), repository: $("repository").value,
    export_directory: $("exports").value || null, interval: Number($("hours").value), password: $("password").value,
    excludes: lines($("excludes").value), password_required: $("protected").checked, remember: $("remember").checked, scheduled: $("scheduled").checked,
    image_target: $("image-target").value || null};
}
async function action(callback) {
  notice("");
  try { await callback(); await refresh(); } catch (error) { notice(error.message); }
}
async function refresh() {
  try {
    state = await api("/api/status");
    $("os").textContent = `${state.doctor.os} · ${state.doctor.architecture}`;
    $("privilege").textContent = state.doctor.elevated ? "Administrator access" : "User access · folders only";
    $("scheduler-status").textContent = state.scheduler_running ? "Background scheduler running" : "Scheduling runs while this interface is open";
    $("setup").classList.toggle("hidden", Boolean(state.restic));
    $("warnings").replaceChildren(...state.doctor.limitations.map(text => { const p=document.createElement("p"); p.textContent=text; return p; }));
    if (!initialized) {
      initialized = true;
      const p = state.plan;
      $("repository").value = p?.repository || state.default_repository;
      $("mode").value = p?.mode || (state.doctor.elevated ? "system" : "folders");
      $("sources").value = p?.mode === "folders" ? p.sources.join("\n") : state.home;
      $("hours").value = p?.interval_hours || 24;
      $("exports").value = p?.export_directory || "";
      $("excludes").value = p?.excludes.join("\n") || "";
      $("scheduled").checked = Boolean(p?.scheduled);
      $("protected").checked = p ? p.password_required !== false : false;
      $("remember").checked = Boolean(p?.scheduled && $("protected").checked);
      protectionChanged();
      $("image-target").value = p?.image_target || "";
      modeChanged();
    }
    $("last-backup").textContent = readableDate(state.plan?.last_success);
    $("interval-label").textContent = state.plan?.scheduled ? `Every ${state.plan.interval_hours} hours` : "Manual backups";
    $("saved-plan-note").textContent = state.plan ? `Saved ${state.plan.mode} plan. Next scheduled run: ${state.plan.scheduled ? (state.plan.next_run ? readableDate(state.plan.next_run) : "when the scheduler checks") : "disabled"}.` : "Save a plan before your first backup.";
    const job = state.job;
    const running = job.state === "running";
    for (const id of ["save", "backup", "restore", "verify", "setup-button"]) $(id).disabled = running;
    $("progress").classList.toggle("hidden", !running);
    $("job-title").textContent = running ? "Operation in progress…" : job.state === "done" ? "Operation completed." : job.state === "failed" ? "Operation failed." : "Ready when you are.";
    const p = job.progress;
    $("job-message").textContent = job.error || p?.message || p?.error || (running ? "Reading and compressing your data. Large backups can take a while." : "Backups are compressed and verified. Password protection is optional.");
    $("progress-bar").style.width = `${Math.max(4, Math.min(100, 100 * (p?.percent_done || 0)))}%`;
    $("job-result").classList.toggle("hidden", !job.result);
    $("job-result").textContent = job.result ? JSON.stringify(job.result, null, 2) : "";
    if (state.activity?.success === false && job.state === "idle") notice(`Last scheduled backup failed: ${state.activity.error}`);
  } catch(error) { notice(error.message); }
}
document.querySelectorAll(".nav").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll(".nav").forEach(b => b.classList.toggle("active", b === button));
  $("backup-tab").classList.toggle("hidden", button.dataset.tab !== "backup");
  $("restore-tab").classList.toggle("hidden", button.dataset.tab !== "restore");
  $("page-title").textContent = button.dataset.tab === "backup" ? "A way back to your VM." : "Bring your saved data back.";
  $("page-subtitle").textContent = button.dataset.tab === "backup" ? "Save your files, apps’ data and system configuration in a compressed backup." : "Verify and import a backup from any saved location.";
}));
$("mode").addEventListener("change", modeChanged);
$("protected").addEventListener("change", protectionChanged);
$("scheduled").addEventListener("change", () => { if($("scheduled").checked && $("protected").checked) $("remember").checked = true; });
$("save").addEventListener("click", () => action(async () => { await api("/api/configure", planData()); notice("Backup plan saved."); }));
$("backup").addEventListener("click", () => action(async () => {
  if (!state.plan) throw new Error("Save your backup plan first.");
  const password = state.plan.password_required === false ? "" : $("password").value;
  await api("/api/backup", { password: password || null, saved: !password });
  $("password").value = "";
}));
$("setup-button").addEventListener("click", () => action(() => api("/api/setup", {})));
function importData() { return {bundle: $("import-file").value, password: $("import-password").value, target: $("target").value}; }
$("verify").addEventListener("click", () => action(() => api("/api/verify", importData())));
$("restore").addEventListener("click", () => action(async () => { await api("/api/restore", importData()); $("import-password").value = ""; }));
async function browse(path) {
  const data = await api(`/api/browse?path=${encodeURIComponent(path)}`);
  $("picker-path").value = data.path; pickerParent = data.parent;
  $("picker-items").replaceChildren(...data.entries.map(entry => {
    const button = document.createElement("button"); button.className = "file-item";
    button.textContent = `${entry.directory ? "▱" : "◈"}  ${entry.name}`;
    button.addEventListener("click", () => action(async () => {
      if(entry.directory) await browse(entry.path);
      else if(pickerKind === "file") { $(pickerField).value = entry.path; $("picker").close(); }
    }));
    return button;
  }));
  $("picker-select").classList.toggle("hidden", pickerKind !== "folder");
}
document.querySelectorAll(".browse").forEach(button => button.addEventListener("click", () => action(async () => {
  pickerField = button.dataset.for; pickerKind = button.dataset.kind;
  await browse(state.home); $("picker").showModal();
})));
$("picker-go").addEventListener("click", () => action(() => browse($("picker-path").value)));
$("picker-up").addEventListener("click", () => action(() => browse(pickerParent)));
$("picker-close").addEventListener("click", () => $("picker").close());
$("picker-select").addEventListener("click", () => { $(pickerField).value = $("picker-path").value; $("picker").close(); });
refresh(); setInterval(refresh, 2000);
