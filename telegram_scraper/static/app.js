"use strict";

(() => {
  // A file preview has no archive server. Give it a working launch path
  // before binding controls or making any API requests.
  if (window.location.protocol === "file:") {
    document.title = "Open Channel Archive";
    const screen = document.createElement("main");
    screen.className = "file-launch";
    const eyebrow = document.createElement("p");
    eyebrow.className = "eyebrow";
    eyebrow.textContent = "CHANNEL ARCHIVE";
    const heading = document.createElement("h1");
    heading.textContent = "Your archive opens in the app.";
    const explanation = document.createElement("p");
    explanation.textContent = "This is the interface file. Open Channel Archive to browse your saved posts and connect to Telegram.";
    const launch = document.createElement("a");
    launch.className = "button primary";
    launch.href = "channel-archive://open";
    launch.textContent = "Open Channel Archive";
    const help = document.createElement("p");
    help.className = "file-launch-help";
    help.textContent = "If the button doesn’t open the app, open Channel Archive.app from your Applications folder. To run from source, use Launch.command in the project folder.";
    screen.append(eyebrow, heading, explanation, launch, help);
    document.body.replaceChildren(screen);
    return;
  }
  const $ = (id) => document.getElementById(id);
  const number = new Intl.NumberFormat();
  const dateFormat = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" });
  const timeFormat = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
  const monthFormat = new Intl.DateTimeFormat(undefined, { month: "short", year: "numeric" });
  const kindNames = { all: "All posts", video: "Videos", photo: "Photos", text: "Text posts", file: "Other files" };
  const singularNames = { video: "Video", photo: "Photo", text: "Text post", file: "File" };
  const pending = new Set();
  let state = null;
  let online = true;
  let stateFailures = 0;
  let pollTimer = null;
  let stateRequest = null;
  let postsRequest = null;
  let postsGeneration = 0;
  let detailGeneration = 0;
  let totalResults = 0;
  let postsLoaded = false;
  let settingsDirty = false;
  let lastRunning = false;
  let lastLibrarySignature = "";
  let selectedPost = null;
  let toastTimer;
  let searchTimer;
  let loginStep = "initial";
  const filters = { q: "", kind: "all", start: "", end: "", offset: 0, limit: 40 };

  function text(id, value) {
    const node = $(id);
    const next = String(value ?? "");
    if (node.textContent !== next) node.textContent = next;
  }

  function element(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = content;
    return node;
  }

  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#i-${name}`);
    svg.append(use);
    return svg;
  }

  function date(value) {
    if (!value) return null;
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }

  function dateLabel(value) {
    const parsed = date(value);
    return parsed ? dateFormat.format(parsed) : "Date unavailable";
  }

  function count(value) {
    return number.format(Number(value) || 0);
  }

  function postKind(post) {
    const value = String(post.media_kind || post.type || "text").toLowerCase();
    if (value.includes("video") || value === "animation" || value === "gif") return "video";
    if (value.includes("photo") || value.includes("image")) return "photo";
    if (value === "text" || value === "message" || value === "none" || value === "") return "text";
    return "file";
  }

  function iconForKind(kind) {
    return kind === "text" ? "post" : kind;
  }

  function feedback(id, message, type = "success") {
    const node = $(id);
    node.textContent = message || "";
    node.className = `form-feedback${type === "error" ? " error" : type === "neutral" ? " neutral" : ""}`;
  }

  function toast(message, error = false) {
    clearTimeout(toastTimer);
    text("toast", message);
    $("toast").className = `toast${error ? " error" : ""}`;
    $("toast").hidden = false;
    toastTimer = setTimeout(() => { $("toast").hidden = true; }, error ? 7500 : 4500);
  }

  function clearToast() {
    clearTimeout(toastTimer);
    $("toast").hidden = true;
  }

  async function api(path, body, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), options.timeout || 20000);
    const externalSignal = options.signal;
    const abort = () => controller.abort();
    if (externalSignal) {
      if (externalSignal.aborted) controller.abort();
      externalSignal.addEventListener("abort", abort, { once: true });
    }
    try {
      const headers = { Accept: "application/json" };
      const init = { credentials: "same-origin", cache: "no-store", signal: controller.signal, headers };
      if (body !== undefined) {
        if (!state?.csrf_token) throw new Error("The app is still connecting to its local server. Try again in a moment.");
        init.method = "POST";
        headers["Content-Type"] = "application/json";
        headers["X-CSRF-Token"] = state.csrf_token;
        init.body = JSON.stringify(body);
      }
      const response = await fetch(path, init);
      // Preserve Telegram's 64-bit identifiers exactly in source previews.
      // Parsing and re-serializing through JavaScript Number would round them.
      if (options.rawText && response.ok) return await response.text();
      let result;
      try { result = await response.json(); } catch { throw new Error("The local server returned an unexpected response. Try again or restart the app."); }
      if (!response.ok || result.ok === false) throw new Error(typeof result.error === "string" ? result.error : "That action couldn’t be completed. Please try again.");
      return result;
    } catch (error) {
      if (externalSignal?.aborted) throw error;
      if (error.name === "AbortError") throw new Error("The local server is taking longer than expected. Check the current status before trying again.");
      if (error instanceof TypeError) throw new Error("Can’t reach the local archive server. Make sure the app is running, then try again.");
      throw error;
    } finally {
      clearTimeout(timeout);
      externalSignal?.removeEventListener("abort", abort);
    }
  }

  async function task(name, action) {
    if (pending.has(name)) return;
    pending.add(name);
    renderControls();
    try { return await action(); } finally { pending.delete(name); renderControls(); }
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    const delay = !online ? Math.min(30000, 2000 * (2 ** Math.min(stateFailures - 1, 4))) : state?.job?.running ? 1500 : document.hidden ? 30000 : 15000;
    pollTimer = setTimeout(() => loadState(), delay);
  }

  async function loadState() {
    if (stateRequest) return stateRequest;
    stateRequest = (async () => {
      try {
        const next = await api("/api/state", undefined, { timeout: 12000 });
        const recovered = !online;
        state = next;
        document.documentElement.dataset.appReady = "true";
        online = true;
        stateFailures = 0;
        $("network-notice").hidden = true;
        if (lastRunning && !state.job?.running) clearToast();
        renderState();
        const librarySignature = [state.library?.total, state.library?.last_date, state.job?.added, state.job?.updated].join(":");
        if (!postsLoaded || recovered || lastLibrarySignature !== librarySignature || (lastRunning && !state.job?.running)) await loadPosts({ preserve: postsLoaded });
        lastLibrarySignature = librarySignature;
        lastRunning = Boolean(state.job?.running);
        return next;
      } catch (error) {
        online = false;
        stateFailures += 1;
        text("network-message", state ? "The local server is unavailable. Your saved files are safe; reconnecting automatically." : error.message);
        $("network-notice").hidden = false;
        if (!postsLoaded) showEmpty("error", error.message);
        renderControls();
        return null;
      } finally {
        stateRequest = null;
        schedulePoll();
      }
    })();
    return stateRequest;
  }

  async function refreshState() {
    if (stateRequest) await stateRequest;
    return loadState();
  }

  function renderState() {
    const library = state.library || {};
    const settings = state.settings || {};
    text("stat-total", count(library.total));
    text("stat-videos", count(library.videos));
    text("stat-media", count(library.media));
    text("nav-all-count", count(library.total));
    text("nav-video-count", count(library.videos));
    const first = date(library.first_date);
    const last = date(library.last_date);
    text("stat-range", first && last ? (first.getFullYear() === last.getFullYear() ? String(first.getFullYear()) : `${first.getFullYear()} – ${last.getFullYear()}`) : "—");
    text("stat-range-caption", first && last ? `${monthFormat.format(first)} — ${monthFormat.format(last)}` : "Your collection starts here");
    text("channel-label", library.channel_title || settings.channel || "YOUR TELEGRAM LIBRARY");
    text("page-subtitle", last ? `Latest archived post · ${dateLabel(library.last_date)}` : "A lasting home for your channel’s ideas.");
    const connected = Boolean(state.connection?.authorized);
    $("connection-dot").classList.toggle("connected", connected);
    text("connection-label", connected ? "Telegram connected" : settings.channel ? "Available offline" : "Ready when you are");
    text("connection-description", connected ? "Ready to save new posts from your channel." : settings.channel ? "Browse your saved posts. Connect Telegram to sync new ones." : "Connect Telegram to start your personal channel archive.");
    text("connection-action", connected ? "Manage connection →" : "Connect Telegram →");
    $("signin-badge").classList.toggle("connected", connected);
    text("signin-badge", connected ? "Connected" : "Not connected");
    if (connected) {
      loginStep = "initial";
      $("signin-actions").hidden = true;
      $("phone-form").hidden = true;
      $("verify-form").hidden = true;
      text("signin-help", "You’re connected. You can sync posts or watch the channel for new ones.");
      $("login-code").value = "";
      $("login-password").value = "";
    } else {
      if (loginStep === "initial") {
        const savedStep = String(state.connection?.step || "").toLowerCase();
        if (savedStep === "code" || savedStep === "password") loginStep = savedStep;
      }
      text("signin-help", settingsDirty ? "Save your settings before connecting to Telegram." : !settings.api_id || !settings.api_hash_set ? "Save your API ID and API hash above to enable sign-in." : loginStep === "password" ? "Enter the two-step verification password you set in Telegram." : loginStep === "code" ? "Enter the latest login code Telegram sent you." : "Connect a saved session, or sign in with your phone number.");
      renderLoginStep();
    }
    renderJob();
    renderHealth();
    renderSetup();
    renderControls();
    if (postsLoaded && totalResults === 0) showEmpty(hasFilters() ? "filtered" : "new");
  }

  function renderControls() {
    const running = Boolean(state?.job?.running);
    const unavailable = !state || !online;
    const starting = pending.has("job");
    const authPending = pending.has("auth");
    const needsCredentials = !state?.settings?.api_id || !state?.settings?.api_hash_set;
    $("setup-action").disabled = unavailable || running || starting || authPending || pending.has("settings");
    ["sync-button", "watch-button", "range-open", "verify-button"].forEach((id) => { $(id).disabled = unavailable || running || starting || pending.has("settings") || authPending; });
    $("watch-button").setAttribute("aria-pressed", String(running && (state?.job?.mode === "watch" || state?.job?.phase === "watching")));
    $("stop-button").disabled = unavailable || pending.has("stop") || !running || state?.job?.status === "stopping" || state?.job?.phase === "stopping";
    text("stop-button", pending.has("stop") || state?.job?.status === "stopping" || state?.job?.phase === "stopping" ? "Stopping…" : "Stop safely");
    $("settings-fields").disabled = unavailable || running || pending.has("settings") || authPending;
    $("save-settings").disabled = unavailable || running || pending.has("settings") || authPending;
    text("save-settings", pending.has("settings") ? "Saving…" : "Save settings");
    ["connect-button", "phone-open", "send-code", "verify-code", "restart-login"].forEach((id) => { $(id).disabled = unavailable || running || authPending || pending.has("settings") || settingsDirty || needsCredentials; });
    ["phone", "login-code", "login-password"].forEach((id) => { $(id).disabled = unavailable || running || authPending || settingsDirty; });
    text("connect-button", authPending ? "Connecting…" : "Connect saved session");
    text("send-code", authPending ? "Sending…" : "Send login code");
    text("verify-code", authPending ? "Signing in…" : "Finish sign-in");
    $("range-submit").disabled = unavailable || running || starting;
    text("range-submit", starting ? "Starting…" : "Fetch posts →");
    if (running && $("settings-dialog").open) feedback("settings-feedback", "Settings are paused while an archive operation is running.", "neutral");
    $("retry-button").disabled = pending.has("retry");
    $("export-button").setAttribute("aria-disabled", String(unavailable || pending.has("export") || !state?.library?.total));
    $("empty-action").disabled = $("empty-action").dataset.action === "new" && Boolean(running || starting || unavailable);
  }

  function renderSetup() {
    const settings = state?.settings || {};
    const credentials = Boolean(settings.api_id && settings.api_hash_set);
    const channel = Boolean(settings.channel);
    const connected = Boolean(state?.connection?.authorized);
    $("setup-panel").hidden = credentials && channel && connected;
    [["setup-credentials", credentials], ["setup-channel", channel], ["setup-login", connected]].forEach(([id, done]) => $(id).classList.toggle("done", done));
    text("setup-title", !credentials ? "Set up your Telegram archive" : !channel ? "Choose the channel to save" : "Sign in to start saving posts");
    text("setup-description", !credentials ? "Use your API ID and API hash from my.telegram.org. Then choose a channel and sign in with your Telegram account." : !channel ? "Your API details are saved. Add the channel’s username or link in Settings." : "Your API details and channel are already saved. Connect your Telegram account to start scraping; your existing archive is available offline.");
    text("setup-action", !credentials ? "Add API details" : !channel ? "Choose channel" : "Connect Telegram");
  }

  async function openSetup() {
    openSettings();
    if (!state?.settings?.api_id || !state?.settings?.api_hash_set) {
      $("api-id").focus();
    } else if (!state.settings.channel) {
      $("channel").focus();
    } else if (!state.connection?.authorized) {
      document.querySelector(".signin-section").scrollIntoView({ block: "start" });
      if (["code", "password"].includes(loginStep)) {
        $(loginStep === "code" ? "login-code" : "login-password").focus();
      } else {
        await connect();
        if (loginStep === "phone") $("phone").focus();
      }
    }
  }

  function renderHealth() {
    const health = state?.health;
    $("health-panel").hidden = !health;
    if (!health) return;
    const runs = health.runs || [];
    const raw = Number(health.posts_with_raw_metadata) || 0;
    const legacy = Number(health.legacy_posts) || 0;
    text("health-summary", health.error ? "Source store needs attention" : legacy ? `${count(legacy)} ${legacy === 1 ? "post awaits" : "posts await"} source capture` : runs.length ? `${count(raw)} ${raw === 1 ? "post" : "posts"} with source details` : "No Telegram scan recorded yet");
    text("health-source", `${count(raw)} with raw metadata · ${count(legacy)} legacy`);
    text("health-files", `${count(health.media_available)} available · ${count(health.media_missing)} missing`);
    const completed = runs.find(run => run.coverage?.history_scan_complete || run.coverage?.pagination_complete || run.coverage?.history_pagination_complete);
    text("health-coverage", completed ? `Scan recorded ${dateLabel(completed.ended_at || completed.started_at)}` : "No completed scan recorded");
    const legacyNote = legacy ? `${count(legacy)} older ${legacy === 1 ? "post contains" : "posts contain"} simplified data. A new sync can add source details that Telegram still exposes. ` : "";
    text("health-note", legacyNote + (health.coverage_note || ""));
    text("health-checksum-note", `${count(health.media_with_recorded_checksums)} primary files have a saved checksum. Use Check local files to compare their current contents.`);
    $("health-feedback").hidden = !health.error;
    text("health-feedback", health.error || "");
    const evidenceAvailable = Number(health.evidence?.observations) > 0 || runs.length > 0;
    $("download-evidence").setAttribute("aria-disabled", String(!evidenceAvailable || Boolean(health.error)));
    $("download-evidence").tabIndex = evidenceAvailable && !health.error ? 0 : -1;
    const signature = JSON.stringify(runs);
    if ($("health-runs").dataset.signature === signature) return;
    const expanded = new Set([...$("health-runs").querySelectorAll("details[open]")].map(node => node.dataset.runId));
    const cards = runs.map(run => {
      const id = String(run.run_id || run.id || "");
      const card = element("details", "capture-run");
      card.dataset.runId = id;
      card.open = expanded.has(id);
      const status = run.display_status || run.status || run.state || "unknown";
      const labels = { completed: "Finished", warning: "Finished with limits", running: "Running", cancelled: "Stopped", interrupted: "Interrupted", error: "Failed", failed: "Failed" };
      const mode = { sync: "Full history", range: "Date range", watch: "Live capture" }[run.metadata?.mode] || "Capture";
      card.append(element("summary", "", `${dateLabel(run.started_at)} · ${mode} · ${labels[status] || status}`));
      if (status === "interrupted") card.append(element("p", "", "The app closed before this run finished. Its saved observations remain available; run sync again to complete the requested scan."));
      const counts = run.counts || {};
      card.append(element("p", "", `${count(counts.processed)} posts processed · ${count(counts.media_downloaded)} main attachments saved${Number(counts.media_failed) ? ` · ${count(counts.media_failed)} attachments need attention` : ""}`));
      card.append(element("p", "", run.coverage?.history_scan_complete ? "The requested history scan finished. Capture limits below describe any related information that could not be saved." : "Complete history coverage has not been confirmed for this run."));
      if (run.metadata?.start) card.append(element("p", "", `Requested dates: ${run.metadata.start} to ${run.metadata.end || run.metadata.start} (UTC).`));
      if (Array.isArray(run.issues) && run.issues.length) {
        const issues = element("ul", "capture-issues");
        run.issues.slice(0, 5).forEach(issue => issues.append(element("li", "", issue.message || `${String(issue.stage || "Source detail").replaceAll("_", " ")}: see the full scrape report.`)));
        if (run.issues.length > 5) issues.append(element("li", "", `${count(run.issues.length - 5)} further items are included in the report.`));
        card.append(issues);
      }
      const report = { scope: run.metadata, counts: run.counts, coverage: run.coverage, checkpoint: run.checkpoint, issues: run.issues };
      const technical = element("details", "capture-technical");
      technical.append(element("summary", "", "Technical receipt"), element("pre", "", JSON.stringify(report, null, 2)));
      card.append(technical);
      const download = element("a", "button secondary compact", "Download full scrape report");
      download.href = `/api/evidence/export?run=${encodeURIComponent(id)}`;
      download.download = `scrape-${id}.json`;
      card.append(download);
      return card;
    });
    $("health-runs").replaceChildren(...cards);
    $("health-runs").dataset.signature = signature;
  }

  function renderJob() {
    const job = state.job || {};
    const status = String(job.status || "");
    const meaningful = job.running || job.message || ["completed", "failed", "error", "stopped", "cancelled"].includes(status);
    $("job-panel").hidden = !meaningful || status === "idle";
    if ($("job-panel").hidden) return;
    const failed = ["failed", "error"].includes(status);
    const warning = status === "warning";
    const stopped = ["stopped", "cancelled"].includes(status);
    const watching = job.mode === "watch" || job.phase === "watching";
    const checking = job.mode === "verify" || job.phase === "verifying";
    const title = job.running ? (status === "stopping" || job.phase === "stopping" ? "Finishing the current step" : watching ? "Watching your channel" : checking ? "Checking your local files" : "Saving posts to your archive") : failed ? "This operation needs attention" : stopped ? "Operation stopped safely" : warning ? "Finished with items to review" : checking ? "Local file check complete" : job.mode === "range" ? "Date range saved" : "Sync complete";
    text("job-title", title);
    text("job-message", job.message || (watching ? "New posts will be saved while the archive server is running." : "Preparing your archive…"));
    $("job-panel").className = `job-panel${job.running ? "" : failed ? " failed" : warning ? " warning" : " completed"}`;
    $("job-symbol").classList.toggle("running", Boolean(job.running && !watching));
    const symbol = watching && job.running ? "live" : job.running ? "sync" : failed || warning ? "info" : "check";
    $("job-symbol").replaceChildren(icon(symbol));
    $("stop-button").hidden = !job.running;
    $("job-progress").hidden = !job.running;
    const details = [];
    [["processed", "processed"], ["added", "new posts"], ["updated", "updated"], ["media_downloaded", "files saved"], ["media_failed", "files need attention"], ["missing", "missing files"]].forEach(([key, label]) => {
      if (Number(job[key]) > 0 || (key === "processed" && job.running)) details.push(`${count(job[key])} ${label}`);
    });
    const signature = details.join(" · ");
    if ($("job-details").dataset.signature !== signature) {
      $("job-details").replaceChildren(...details.map((detail) => element("span", "", detail)));
      $("job-details").dataset.signature = signature;
    }
    const result = job.result;
    $("job-report").hidden = !result || job.running;
    if (result && !job.running) {
      const reportSignature = JSON.stringify(result);
      if ($("job-report").dataset.signature !== reportSignature) {
        const report = $("job-report-body");
        const issues = Array.isArray(result.issues) ? result.issues : [];
        const changes = Array.isArray(result.changes) ? result.changes : [];
        report.replaceChildren(element("p", "", `${count(result.checked_messages)} posts · ${count(result.checked_media)} primary files · ${count(result.checked_variant_files)} additional files · ${count(result.checked_evidence_observations)} source observations · ${count(result.checked_archives)} backups checked`));
        text("job-report-summary", issues.length ? `Review ${count(issues.length)} ${issues.length === 1 ? "issue" : "issues"}${changes.length ? ` and ${count(changes.length)} changes` : ""}` : changes.length ? `View ${count(changes.length)} recorded changes` : "View check results");
        [["Items needing attention", issues], ["Changes since earlier backups", changes], ["Capture limits", result.limitations || []]].forEach(([label, items]) => {
          if (!items.length) return;
          report.append(element("strong", "", label));
          const list = element("ul");
          items.slice(0, 200).forEach((item) => list.append(element("li", "", String(item))));
          if (items.length > 200) list.append(element("li", "", `${count(items.length - 200)} additional items are not displayed.`));
          report.append(list);
        });
        $("job-report").dataset.signature = reportSignature;
      }
    }
  }

  function hasFilters() {
    return Boolean(filters.q || filters.start || filters.end || filters.kind !== "all");
  }

  function skeleton() {
    const rows = [];
    for (let i = 0; i < 5; i += 1) {
      const row = element("div", "skeleton-row");
      row.setAttribute("aria-hidden", "true");
      const copy = element("div", "skeleton-copy");
      for (let j = 0; j < 3; j += 1) copy.append(element("div", "skeleton-line"));
      row.append(element("div", "skeleton-square"), copy);
      rows.push(row);
    }
    $("post-list").replaceChildren(...rows);
  }

  async function loadPosts({ preserve = false, focus = false } = {}) {
    const generation = ++postsGeneration;
    postsRequest?.abort();
    const controller = new AbortController();
    postsRequest = controller;
    $("post-list").setAttribute("aria-busy", "true");
    if (!preserve) {
      $("empty-state").hidden = true;
      $("post-list").hidden = false;
      $("pagination").hidden = true;
      skeleton();
      text("results-label", "Loading your posts…");
    }
    try {
      const query = new URLSearchParams();
      Object.entries(filters).forEach(([key, value]) => { if (value !== "") query.set(key, value); });
      const result = await api(`/api/posts?${query}`, undefined, { signal: controller.signal });
      if (generation !== postsGeneration) return;
      const posts = Array.isArray(result.posts) ? result.posts : [];
      totalResults = Number(result.total) || 0;
      postsLoaded = true;
      if (filters.offset >= totalResults && filters.offset > 0) {
        filters.offset = Math.max(0, Math.floor((totalResults - 1) / filters.limit) * filters.limit);
        await loadPosts({ focus });
        return;
      }
      const focusedId = $("post-list").contains(document.activeElement) ? document.activeElement.dataset.postId : null;
      $("post-list").replaceChildren(...posts.map(postRow));
      if (focusedId && !focus) Array.from($("post-list").children).find((row) => row.dataset.postId === focusedId)?.focus({ preventScroll: true });
      $("post-list").hidden = posts.length === 0;
      $("empty-state").hidden = posts.length !== 0;
      if (posts.length === 0) showEmpty(hasFilters() ? "filtered" : "new");
      text("results-label", `${count(totalResults)} ${totalResults === 1 ? "post" : "posts"}${hasFilters() ? " found" : " in your archive"}`);
      $("pagination").hidden = totalResults <= filters.limit;
      $("previous-page").disabled = filters.offset === 0;
      $("next-page").disabled = filters.offset + posts.length >= totalResults;
      text("page-description", `${count(filters.offset + 1)}–${count(filters.offset + posts.length)} of ${count(totalResults)}`);
      if (focus) {
        $("post-list").querySelector("button")?.focus({ preventScroll: true });
        $("results-label").scrollIntoView({ block: "start", behavior: "instant" });
      }
    } catch (error) {
      if (controller.signal.aborted || generation !== postsGeneration) return;
      if (!preserve || !postsLoaded) showEmpty("error", error.message);
      else toast(error.message, true);
      text("results-label", "Posts couldn’t be refreshed");
    } finally {
      if (generation === postsGeneration) {
        $("post-list").setAttribute("aria-busy", "false");
        postsRequest = null;
      }
    }
  }

  function postMeta(post) {
    const meta = element("div", "post-meta");
    const timestamp = date(post.date);
    const time = element("time", "", dateLabel(post.date));
    if (timestamp) {
      time.dateTime = timestamp.toISOString();
      time.title = `${dateFormat.format(timestamp)}, ${timeFormat.format(timestamp)} · your local time`;
    }
    meta.append(time);
    if (timestamp) meta.append(element("span", "meta-dot", "·"), element("span", "", timeFormat.format(timestamp)));
    meta.append(element("span", "meta-dot", "·"), element("span", "type-label", singularNames[postKind(post)]));
    if (Number(post.revision_count) > 0) meta.append(element("span", "revision-badge", `${count(post.revision_count)} earlier ${Number(post.revision_count) === 1 ? "version" : "versions"}`));
    if (post.media_missing) meta.append(element("span", "missing-badge", "Media not saved"));
    return meta;
  }

  function postRow(post) {
    const kind = postKind(post);
    const row = element("button", "post-row");
    row.type = "button";
    row.dataset.postId = post.id;
    const typeIcon = element("span", `post-type-icon ${kind}`);
    typeIcon.append(icon(iconForKind(kind)));
    const summary = element("div", "post-summary");
    summary.append(postMeta(post));
    const caption = String(post.text || "").trim();
    const fallback = post.action_title || (kind === "text" ? "Post without text" : `${singularNames[kind]} without a caption`);
    summary.append(element("p", `post-excerpt${caption ? "" : " no-caption"}`, caption || fallback));
    if (post.media_name) summary.append(element("div", "post-filename", post.media_name));
    const arrow = element("span", "post-arrow");
    arrow.append(icon("chevron"));
    row.append(typeIcon, summary, arrow);
    row.addEventListener("click", () => openPost(post.id, row));
    return row;
  }

  function showEmpty(mode, message = "") {
    $("post-list").hidden = true;
    $("empty-state").hidden = false;
    $("pagination").hidden = true;
    $("empty-action").dataset.action = mode;
    if (mode === "error") {
      text("empty-eyebrow", "LET’S TRY THAT AGAIN");
      text("empty-title", "Your posts couldn’t be loaded");
      text("empty-description", message || "Make sure the local archive server is running, then try again.");
      text("empty-action", "Try again");
    } else if (mode === "filtered") {
      text("empty-eyebrow", "NOTHING HERE JUST YET");
      text("empty-title", "No posts match these filters");
      text("empty-description", "Try a different search, widen the date range, or return to all posts.");
      text("empty-action", "Clear filters");
    } else {
      const connected = Boolean(state?.connection?.authorized);
      const running = Boolean(state?.job?.running);
      text("empty-eyebrow", "A SPACE FOR WHAT MATTERS");
      text("empty-title", running ? "Your archive is taking shape" : "Your archive starts here");
      text("empty-description", running ? "Saved posts will appear here as your archive grows. You can follow the progress above." : connected ? "Your channel is ready. Sync its posts and videos to start building your local archive." : "Connect your Telegram account and choose a channel. Save its posts and videos, then revisit them anytime.");
      text("empty-action", running ? "Saving your posts…" : connected ? "Sync your first posts →" : "Set up your archive →");
    }
    renderControls();
  }

  function setView(kind) {
    filters.kind = kind;
    filters.offset = 0;
    $("kind-filter").value = kind;
    document.querySelectorAll("[data-view]").forEach((button) => {
      const active = button.dataset.view === kind;
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
    });
    $("page-title").replaceChildren(document.createTextNode(kindNames[kind]), element("span", "heading-dot", "."));
    loadPosts();
  }

  function clearFilters() {
    clearTimeout(searchTimer);
    filters.q = "";
    filters.start = "";
    filters.end = "";
    $("search-input").value = "";
    $("filter-start").value = "";
    $("filter-end").value = "";
    feedback("date-feedback", "");
    $("date-filter-toggle").setAttribute("aria-pressed", "false");
    setView("all");
  }

  function showDialog(id) {
    const dialog = $(id);
    if (!dialog.open) dialog.showModal();
  }

  function openSettings() {
    if (!state) { toast("The app is connecting to its local server. Try again in a moment.", true); return; }
    fillSettings();
    feedback("settings-feedback", state.job?.running ? "Settings are paused while an archive operation is running." : "", "neutral");
    feedback("signin-feedback", "");
    showDialog("settings-dialog");
    renderControls();
  }

  function fillSettings() {
    const settings = state.settings || {};
    $("api-id").value = settings.api_id || "";
    $("api-hash").value = "";
    $("api-hash").placeholder = settings.api_hash_set ? "Already saved · leave blank to keep" : "Your API hash";
    text("hash-help", settings.api_hash_set ? "A hash is saved. Enter a new one only to replace it." : "Provided with your API ID.");
    $("channel").value = settings.channel || "";
    $("download-media").checked = settings.download_media !== false;
    $("archive-before-sync").checked = settings.archive_before_sync !== false;
    $("capture-context").checked = settings.capture_context !== false;
    settingsDirty = false;
    renderLoginStep();
  }

  async function saveSettings(event) {
    event.preventDefault();
    if (state?.job?.running) return;
    const id = $("api-id").value.trim();
    if (id && (!/^\d+$/.test(id) || Number(id) <= 0)) {
      feedback("settings-feedback", "Enter the numeric API ID from your Telegram API settings.", "error");
      $("api-id").focus();
      return;
    }
    await task("settings", async () => {
      try {
        await api("/api/settings", { api_id: id ? Number(id) : "", api_hash: $("api-hash").value.trim(), channel: $("channel").value.trim(), download_media: $("download-media").checked, archive_before_sync: $("archive-before-sync").checked, capture_context: $("capture-context").checked });
        settingsDirty = false;
        await refreshState();
        if (online) fillSettings();
        feedback("settings-feedback", "Settings saved on this computer.");
      } catch (error) { feedback("settings-feedback", error.message, "error"); }
    });
  }

  function renderLoginStep() {
    const connected = Boolean(state?.connection?.authorized);
    $("signin-actions").hidden = connected || loginStep !== "initial";
    $("phone-form").hidden = connected || loginStep !== "phone";
    $("verify-form").hidden = connected || !["code", "password"].includes(loginStep);
    $("code-field").hidden = loginStep === "password";
    $("password-field").hidden = loginStep !== "password";
    $("login-code").required = loginStep === "code";
    $("login-password").required = loginStep === "password";
  }

  function inferLoginStep(result, fallback) {
    const step = String(result.step || result.connection?.step || state?.connection?.step || "").toLowerCase();
    if (result.password_required || result.requires_password || step.includes("password") || step.includes("2fa")) return "password";
    if (step.includes("code")) return "code";
    if (step.includes("phone")) return "phone";
    return fallback;
  }

  async function connect() {
    await task("auth", async () => {
      feedback("signin-feedback", "Connecting to Telegram…", "neutral");
      try {
        const result = await api("/api/connect", {}, { timeout: 45000 });
        await refreshState();
        if (state?.connection?.authorized || result.authorized) {
          feedback("signin-feedback", "Connected. Your archive is ready to sync.");
        } else {
          loginStep = inferLoginStep(result, "phone");
          renderLoginStep();
          feedback("signin-feedback", "Sign in to connect this computer to Telegram.", "neutral");
        }
      } catch (error) { feedback("signin-feedback", error.message, "error"); }
    });
  }

  async function sendCode(event) {
    event.preventDefault();
    const phone = $("phone").value.trim();
    if (!/^\+[\d\s().-]{6,24}$/.test(phone)) {
      feedback("signin-feedback", "Include the country code, starting with +, followed by your phone number.", "error");
      $("phone").focus();
      return;
    }
    await task("auth", async () => {
      try {
        feedback("signin-feedback", "Requesting your Telegram login code…", "neutral");
        const result = await api("/api/login/code", { phone: phone.replace(/[\s().-]/g, "") }, { timeout: 45000 });
        loginStep = inferLoginStep(result, "code");
        renderLoginStep();
        feedback("signin-feedback", "Code sent. Check Telegram on a device where you’re already signed in, or check your SMS.");
      } catch (error) { feedback("signin-feedback", error.message, "error"); }
    });
    if (loginStep === "code") $("login-code").focus();
  }

  async function verifyCode(event) {
    event.preventDefault();
    await task("auth", async () => {
      try {
        feedback("signin-feedback", "Signing in…", "neutral");
        const result = await api("/api/login/verify", { code: $("login-code").value.replace(/\s/g, ""), password: $("login-password").value }, { timeout: 45000 });
        await refreshState();
        if (state?.connection?.authorized || result.authorized) {
          feedback("signin-feedback", "You’re connected. Close settings and sync your first posts.");
          $("phone").value = "";
          $("login-code").value = "";
          $("login-password").value = "";
        } else {
          loginStep = inferLoginStep(result, "password");
          renderLoginStep();
          feedback("signin-feedback", loginStep === "password" ? "Your account uses two-step verification. Enter your Telegram password to continue." : "Enter the login code sent by Telegram.", "neutral");
        }
      } catch (error) {
        const message = error.message;
        if (/two.step|2fa|password.*required/i.test(message)) { loginStep = "password"; renderLoginStep(); }
        feedback("signin-feedback", message, "error");
        $("login-password").value = "";
      }
    });
    if (loginStep === "password") $("login-password").focus();
  }

  function readyToSync() {
    if (!state?.settings?.channel || !state?.connection?.authorized) {
      openSettings();
      feedback("signin-feedback", !state?.settings?.channel ? "Choose your channel and save settings, then connect Telegram to continue." : "Connect Telegram to save new posts. Your existing archive is available offline.", "neutral");
      return false;
    }
    return true;
  }

  async function startJob(mode, extras = {}) {
    if (state?.job?.running || pending.has("job")) return false;
    if (mode !== "verify" && !readyToSync()) return false;
    return task("job", async () => {
      try {
        clearToast();
        await api("/api/jobs", { mode, ...extras });
        await refreshState();
        if (mode === "watch") toast("Watching for new posts. Keep the archive server running to continue.");
        return true;
      } catch (error) {
        if (mode === "range") feedback("range-feedback", error.message, "error");
        else toast(error.message, true);
        await refreshState();
        return false;
      }
    });
  }

  function addTextWithLinks(container, content) {
    const pattern = /https?:\/\/[^\s<>]+/gi;
    let start = 0;
    for (const match of content.matchAll(pattern)) {
      container.append(document.createTextNode(content.slice(start, match.index)));
      const raw = match[0];
      const trimmed = raw.replace(/[.,;!?\])}]+$/, "");
      try {
        const url = new URL(trimmed);
        if (!["http:", "https:"].includes(url.protocol)) throw new Error();
        const link = element("a", "", trimmed);
        link.href = url.href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        container.append(link, document.createTextNode(raw.slice(trimmed.length)));
      } catch { container.append(document.createTextNode(raw)); }
      start = match.index + raw.length;
    }
    container.append(document.createTextNode(content.slice(start)));
  }

  async function openPost(id) {
    const generation = ++detailGeneration;
    selectedPost = null;
    text("post-detail-title", "Post details");
    text("detail-kind", "ARCHIVED POST");
    text("detail-id", "");
    $("copy-post").disabled = true;
    $("post-detail-body").replaceChildren(element("p", "detail-loading", "Opening your post…"));
    showDialog("post-dialog");
    try {
      const result = await api(`/api/posts/${encodeURIComponent(id)}`);
      if (generation !== detailGeneration || !$("post-dialog").open) return;
      const post = result.post || result;
      selectedPost = post;
      renderPostDetail(post);
    } catch (error) {
      if (generation !== detailGeneration || !$("post-dialog").open) return;
      const errorBox = element("div", "detail-error");
      const retry = element("button", "button secondary", "Try again");
      retry.addEventListener("click", () => openPost(id));
      errorBox.append(element("strong", "", "This post couldn’t be opened"), element("p", "", error.message), retry);
      $("post-detail-body").replaceChildren(errorBox);
    }
  }

  function renderPostDetail(post) {
    const kind = postKind(post);
    text("post-detail-title", kind === "text" ? "Archived post" : `Archived ${singularNames[kind].toLowerCase()}`);
    text("detail-kind", state?.library?.channel_title || "FROM YOUR ARCHIVE");
    text("detail-id", `Post ${post.id} · Saved locally`);
    $("copy-post").disabled = !post.text;
    const body = $("post-detail-body");
    body.replaceChildren(postMeta(post));
    if (kind !== "text") {
      if (post.media_url && !post.media_missing) {
        const source = `/api/media/${encodeURIComponent(post.id)}`;
        if (kind === "video" || kind === "photo") {
          const media = element(kind === "video" ? "video" : "img", `media-preview${kind === "photo" ? " photo" : ""}`);
          if (kind === "video") { media.controls = true; media.preload = "metadata"; media.playsInline = true; }
          else { media.alt = post.text ? String(post.text).slice(0, 250) : `Photo from post ${post.id}`; media.loading = "lazy"; }
          media.src = source;
          media.addEventListener("error", () => {
            if (media.nextElementSibling?.classList.contains("media-playback-error")) return;
            const note = element("p", "form-feedback neutral media-playback-error", kind === "video" ? "This browser couldn’t play the file. Download it to open in your preferred video player." : "This image couldn’t be displayed. Try downloading the original file.");
            media.after(note);
          }, { once: true });
          body.append(media);
        }
        const download = element("div", "media-download");
        const link = element("a", "button secondary compact", "Download file");
        link.prepend(icon("download"));
        link.href = `${source}?download=1`;
        link.download = post.media_name || `post-${post.id}`;
        download.append(element("span", "media-filename", post.media_name || `${singularNames[kind]} · original file`), link);
        body.append(download);
      } else {
        const notice = element("div", "media-unavailable");
        const copy = element("div");
        copy.append(element("strong", "", "This media isn’t saved locally"), element("p", "", "Enable media downloads in Settings, then sync this post’s date range to save its file. The original must still be available on Telegram."));
        notice.append(icon("info"), copy);
        body.append(notice);
      }
    }
    const caption = element("p", "post-text");
    addTextWithLinks(caption, String(post.text || post.action_title || (kind === "text" ? "This post has no text." : "No caption was included with this post.")));
    body.append(caption);
    renderStructuredContent(post, body);
    if (Array.isArray(post.revisions) && post.revisions.length) {
      const history = element("details", "post-history");
      history.append(element("summary", "", `Earlier saved versions (${count(post.revisions.length)})`));
      history.append(element("p", "history-help", "These are earlier versions captured by your archive. Edits between syncs may not have been saved."));
      [...post.revisions].reverse().forEach((revision) => {
        const version = element("div", "post-version");
        version.append(element("span", "version-date", revision.archived_at ? `Saved ${dateLabel(revision.archived_at)}` : revision.edit_date ? `Edited ${dateLabel(revision.edit_date)}` : "Earlier version"));
        const versionText = element("p", "post-text");
        addTextWithLinks(versionText, String(revision.text || "This version has no text."));
        version.append(versionText);
        history.append(version);
      });
      body.append(history);
    }
    renderSourceDetails(post, body);
  }

  function renderStructuredContent(post, body) {
    const content = post.structured_content || {};
    const section = element("section", "structured-content");
    const metadata = element("div", "source-metadata");
    if (content.author_signature) metadata.append(element("span", "", `Author: ${content.author_signature}`));
    if (content.views !== null && content.views !== undefined) metadata.append(element("span", "", `${count(content.views)} views at capture`));
    if (content.forwards !== null && content.forwards !== undefined) metadata.append(element("span", "", `${count(content.forwards)} forwards at capture`));
    if (content.album_id) metadata.append(element("span", "", `Album ${content.album_id}`));
    if (metadata.childNodes.length) section.append(metadata);
    if (content.poll) {
      const question = content.poll.question;
      section.append(element("h3", "", typeof question === "object" ? question?.text || "Poll" : question || "Poll"));
      const answers = element("ul", "poll-answers");
      (content.poll.answers || []).forEach(answer => {
        const label = typeof answer.text === "object" ? answer.text?.text : answer.text;
        answers.append(element("li", "", label || "Untitled answer"));
      });
      section.append(answers, element("p", "source-info", "Archived poll information. Opening this record does not cast a vote. Returned results and accessible voter details are included in the source observations."));
    }
    if (section.childNodes.length) body.append(section);
  }

  function renderSourceDetails(post, body) {
    const source = post.source || {};
    const section = element("section", "source-section");
    section.append(element("h3", "", "Source & preservation"));
    section.append(element("p", "", source.has_raw ? `Full message fields were saved${source.last_observed_at ? ` when checked on ${dateLabel(source.last_observed_at)}` : ""}. Earlier observations and linked details are kept separately from this reading view.` : "This is a legacy post with simplified fields. A new sync can capture additional source details that Telegram still exposes."));
    const actions = element("div", "source-actions");
    const view = element("button", "button secondary compact", "Inspect saved source");
    view.type = "button";
    const download = element("a", "button secondary compact", "Download source preview");
    download.href = `/api/posts/${encodeURIComponent(post.id)}/source`;
    download.download = `post-${post.id}-source-preview.json`;
    const allSources = element("a", "button secondary compact", "Export all source data");
    allSources.href = "/api/evidence/export";
    allSources.download = "source-observations.json";
    actions.append(view, download, allSources);
    section.append(actions);
    const capture = source.context_capture;
    if (capture) {
      const status = element("p", "source-info", capture.status === "complete" ? "Related details requested for this post were captured." : "Some related details are unavailable or need another attempt. The source report records each limitation.");
      section.append(status);
      const variants = element("details", "post-history");
      const saved = (capture.media_variants || []).map((value, index) => ({ ...value, index })).filter(value => value.media_file && ["downloaded", "reused"].includes(value.state));
      if (saved.length) {
        variants.append(element("summary", "", `Additional saved media (${count(saved.length)})`));
        const links = element("div", "source-actions");
        saved.forEach(value => {
          const label = `${String(value.role || value.constructor || "Media variant").replaceAll("_", " ")}${value.size_type ? ` · ${value.size_type}` : ""}`;
          const link = element("a", "button secondary compact", label);
          link.href = `/api/posts/${encodeURIComponent(post.id)}/variants/${value.index}`;
          link.download = "";
          links.append(link);
        });
        variants.append(links);
        section.append(variants);
      }
    }
    view.addEventListener("click", async () => {
      if (view.disabled) return;
      view.disabled = true;
      view.textContent = "Loading source…";
      try {
        const json = await api(`/api/posts/${encodeURIComponent(post.id)}/source`, undefined, { rawText: true });
        section.querySelector(".source-preview")?.remove();
        section.append(element("pre", "source-preview", json.length > 40000 ? `${json.slice(0, 40000)}\n\nPreview shortened. Export all source data for every saved observation and occurrence.` : json));
        view.textContent = "Refresh source";
      } catch (error) {
        section.append(element("p", "form-feedback error", error.message));
        view.textContent = "Try source again";
      } finally { view.disabled = false; }
    });
    body.append(section);
  }

  document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
  $("kind-filter").addEventListener("change", (event) => setView(event.target.value));
  $("search-input").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { filters.q = $("search-input").value.trim(); filters.offset = 0; loadPosts(); }, 280);
  });
  $("search-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { clearTimeout(searchTimer); filters.q = $("search-input").value.trim(); filters.offset = 0; loadPosts(); }
  });
  $("date-filter-toggle").addEventListener("click", () => {
    const open = $("date-filters").hidden;
    $("date-filters").hidden = !open;
    $("date-filter-toggle").setAttribute("aria-expanded", String(open));
    if (open) $("filter-start").focus();
  });
  $("date-filters").addEventListener("submit", (event) => {
    event.preventDefault();
    const start = $("filter-start").value;
    const end = $("filter-end").value;
    if (start && end && start > end) { feedback("date-feedback", "The end date must be on or after the start date.", "error"); return; }
    filters.start = start;
    filters.end = end;
    filters.offset = 0;
    feedback("date-feedback", "");
    $("date-filter-toggle").setAttribute("aria-pressed", String(Boolean(start || end)));
    loadPosts();
  });
  $("clear-dates").addEventListener("click", () => {
    $("filter-start").value = "";
    $("filter-end").value = "";
    filters.start = "";
    filters.end = "";
    filters.offset = 0;
    feedback("date-feedback", "");
    $("date-filter-toggle").setAttribute("aria-pressed", "false");
    loadPosts();
  });
  $("previous-page").addEventListener("click", () => { filters.offset = Math.max(0, filters.offset - filters.limit); loadPosts({ focus: true }); });
  $("next-page").addEventListener("click", () => { if (filters.offset + filters.limit < totalResults) { filters.offset += filters.limit; loadPosts({ focus: true }); } });
  $("settings-open").addEventListener("click", openSettings);
  $("setup-action").addEventListener("click", openSetup);
  $("connection-action").addEventListener("click", openSettings);
  $("settings-form").addEventListener("submit", saveSettings);
  $("settings-fields").addEventListener("input", () => {
    settingsDirty = true;
    feedback("settings-feedback", "You have unsaved changes.", "neutral");
    text("signin-help", "Save your settings before connecting to Telegram.");
    renderControls();
  });
  $("connect-button").addEventListener("click", connect);
  $("phone-open").addEventListener("click", () => { loginStep = "phone"; renderLoginStep(); feedback("signin-feedback", ""); $("phone").focus(); });
  $("restart-login").addEventListener("click", () => { loginStep = "phone"; $("login-code").value = ""; $("login-password").value = ""; renderLoginStep(); feedback("signin-feedback", ""); $("phone").focus(); });
  $("phone-form").addEventListener("submit", sendCode);
  $("verify-form").addEventListener("submit", verifyCode);
  $("sync-button").addEventListener("click", () => startJob("sync"));
  $("watch-button").addEventListener("click", () => startJob("watch"));
  $("verify-button").addEventListener("click", () => startJob("verify"));
  $("export-button").addEventListener("click", (event) => {
    event.preventDefault();
    if ($("export-button").getAttribute("aria-disabled") === "true") return;
    task("export", async () => {
      try {
        const response = await fetch("/api/export", { credentials: "same-origin", cache: "no-store" });
        if (!response.ok) {
          let message = "Your posts couldn’t be exported. Try again in a moment.";
          try { const result = await response.json(); if (typeof result.error === "string") message = result.error; } catch { /* Keep the readable fallback. */ }
          throw new Error(message);
        }
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = "messages_all.json";
        document.body.append(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 60000);
        toast("Your posts are ready to download. Media files stay in your archive folder.");
      } catch (error) { toast(error instanceof TypeError ? "Can’t reach the local archive server. Start the app and try again." : error.message, true); }
    });
  });
  $("stop-button").addEventListener("click", () => task("stop", async () => {
    try { await api("/api/jobs/stop", {}); toast("Stopping safely after the current step."); await refreshState(); }
    catch (error) { toast(error.message, true); }
  }));
  $("range-open").addEventListener("click", () => {
    if (!readyToSync()) return;
    feedback("range-feedback", "");
    showDialog("range-dialog");
  });
  $("range-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const start = $("range-start").value;
    const end = $("range-end").value || start;
    if (!start || start > end) { feedback("range-feedback", "Choose a start date. If you add an end date, it must be on or after the start.", "error"); return; }
    feedback("range-feedback", "");
    if (await startJob("range", { start, end })) $("range-dialog").close();
  });
  $("retry-button").addEventListener("click", () => task("retry", async () => { await loadState(); if (online) await loadPosts(); }));
  $("empty-action").addEventListener("click", () => {
    const action = $("empty-action").dataset.action;
    if (action === "error") { loadState(); loadPosts(); }
    else if (action === "filtered") clearFilters();
    else if (state?.connection?.authorized) startJob("sync");
    else openSettings();
  });
  $("copy-post").addEventListener("click", async () => {
    if (!selectedPost?.text) return;
    try {
      await navigator.clipboard.writeText(String(selectedPost.text));
      text("copy-post", "Copied");
      setTimeout(() => text("copy-post", "Copy text"), 1800);
    } catch { toast("Text couldn’t be copied. You can select and copy it directly from the post.", true); }
  });
  document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => $(button.dataset.close).close()));
  document.querySelectorAll("dialog").forEach((dialog) => {
    dialog.addEventListener("click", (event) => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
    });
  });
  $("post-dialog").addEventListener("close", () => {
    detailGeneration += 1;
    $("post-detail-body").querySelectorAll("video, audio").forEach((media) => { media.pause(); media.removeAttribute("src"); media.load(); });
    $("post-detail-body").replaceChildren();
    selectedPost = null;
    text("copy-post", "Copy text");
  });
  $("settings-dialog").addEventListener("close", () => {
    $("api-hash").value = "";
    $("login-password").value = "";
    if (settingsDirty) { settingsDirty = false; toast("Unsaved settings were discarded."); }
    renderControls();
  });
  document.addEventListener("keydown", (event) => {
    const editing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName) || document.activeElement?.isContentEditable;
    if (event.key === "/" && !editing && !event.ctrlKey && !event.metaKey && !event.altKey && !document.querySelector("dialog[open]")) { event.preventDefault(); $("search-input").focus(); }
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) loadState(); else schedulePoll(); });
  window.addEventListener("online", () => loadState());
  renderControls();
  skeleton();
  loadState();
})();
