/* Dashboard interactivity.
 *
 * No framework and no bundler: the whole thing is progressive enhancement over
 * pages that already work without JavaScript. Every action is a real form POST
 * with a CSRF token; what this file adds is live output, live counters and
 * confirmation prompts.
 *
 * The one connection it opens is an EventSource to /events. If that fails
 * (a proxy that buffers, a browser with it disabled) the page falls back to
 * polling /api/state, and nothing becomes unusable.
 */
(function () {
  "use strict";

  var consoleEl = document.getElementById("console");
  var runsEl = document.getElementById("runs");
  var pendingPill = document.getElementById("pending-count");
  var liveDot = document.querySelector(".brand .dot");
  var activeRunId = consoleEl ? consoleEl.dataset.runId || null : null;

  /* -- helpers ---------------------------------------------------------- */

  function toast(message, action) {
    var host = document.getElementById("toast");
    if (!host) return;
    var box = document.createElement("div");
    box.className = "t";
    var text = document.createElement("span");
    text.textContent = message;
    box.appendChild(text);
    if (action) {
      var button = document.createElement("button");
      button.textContent = action.label;
      button.addEventListener("click", action.run);
      box.appendChild(button);
    }
    var close = document.createElement("button");
    close.className = "ghost";
    close.textContent = "×";
    close.setAttribute("aria-label", "dismiss");
    close.addEventListener("click", function () { box.remove(); });
    box.appendChild(close);
    host.appendChild(box);
    setTimeout(function () { box.remove(); }, 12000);
  }

  function appendLine(text, isError) {
    if (!consoleEl) return;
    var line = document.createElement("span");
    line.className = "line" + (isError ? " err" : "");
    line.textContent = text;
    consoleEl.appendChild(line);
    consoleEl.scrollTop = consoleEl.scrollHeight;
  }

  /* -- relative timestamps ---------------------------------------------- */

  function relative(iso) {
    var seconds = (new Date(iso).getTime() - Date.now()) / 1000;
    var past = seconds < 0;
    var s = Math.abs(seconds);
    var text;
    // Floor throughout: rounding the minutes of "11h 59m 40s" produces the
    // nonsense "11h 60m".
    if (s < 60) text = Math.floor(s) + "s";
    else if (s < 3600) text = Math.floor(s / 60) + "m";
    else if (s < 86400) text = Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
    else text = Math.floor(s / 86400) + "d " + Math.floor((s % 86400) / 3600) + "h";
    return past ? text + " ago" : "in " + text;
  }

  function refreshTimes() {
    document.querySelectorAll("[data-ts]").forEach(function (node) {
      var stamp = node.dataset.ts;
      // A blank or unparseable timestamp keeps whatever the server rendered:
      // "NaNd NaNh" is worse than a slightly stale string.
      if (!stamp) return;
      var when = new Date(stamp).getTime();
      if (isNaN(when)) return;
      node.textContent = relative(stamp);
    });
  }
  refreshTimes();
  setInterval(refreshTimes, 15000);

  /* -- confirmations ----------------------------------------------------- */

  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.dataset.confirm)) {
        event.preventDefault();
        return;
      }
      var button = form.querySelector("button[type=submit], button:not([type])");
      if (button) {
        button.disabled = true;
        button.dataset.label = button.textContent;
        button.textContent = "Working…";
        // A disabled button submits no value, so re-enable if the navigation
        // is cancelled or the page is restored from the back/forward cache.
        setTimeout(function () {
          button.disabled = false;
          button.textContent = button.dataset.label;
        }, 20000);
      }
    });
  });

  /* -- run rendering ----------------------------------------------------- */

  function runRow(run) {
    var row = document.createElement("div");
    row.className = "run";
    row.id = "run-" + run.id;
    var badge = document.createElement("span");
    badge.className = "pill " + ({
      succeeded: "good", failed: "bad", running: "pending", queued: "muted"
    }[run.status] || "muted");
    badge.textContent = run.status;
    row.appendChild(badge);
    var cmd = document.createElement("span");
    cmd.className = "cmd";
    cmd.textContent = run.command;
    row.appendChild(cmd);
    if (run.duration_seconds !== null && run.duration_seconds !== undefined) {
      var dur = document.createElement("span");
      dur.className = "muted small";
      dur.textContent = run.duration_seconds + "s";
      row.appendChild(dur);
    }
    return row;
  }

  function upsertRun(run) {
    if (!runsEl) return;
    var existing = document.getElementById("run-" + run.id);
    var row = runRow(run);
    if (existing) existing.replaceWith(row);
    else runsEl.prepend(row);
  }

  function loadRun(runId) {
    fetch("/api/runs/" + runId, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (run) {
        if (!run || !consoleEl) return;
        consoleEl.textContent = "";
        (run.lines || []).forEach(function (line) { appendLine(line, false); });
      })
      .catch(function () { /* the page still works without it */ });
  }

  /* -- state refresh ----------------------------------------------------- */

  var reloadOffered = false;

  function refreshState() {
    return fetch("/api/state", { headers: { Accept: "application/json" } })
      .then(function (r) {
        // The session expired or the server restarted: reloading lands on the
        // login page rather than leaving a page that silently stops updating.
        if (r.status === 401) { window.location.reload(); return null; }
        return r.ok ? r.json() : null;
      })
      .then(function (payload) {
        if (!payload) return;
        var status = payload.status;
        if (pendingPill) {
          var previous = parseInt(pendingPill.dataset.count || "0", 10);
          pendingPill.dataset.count = status.pending_count;
          pendingPill.textContent = status.pending_count;
          if (status.pending_count > previous && !reloadOffered) {
            reloadOffered = true;
            toast("New recommendation awaiting your decision.", {
              label: "Show it",
              run: function () { window.location.href = "/"; }
            });
          }
        }
        (payload.runs || []).forEach(upsertRun);
      })
      .catch(function () { /* offline is not fatal */ });
  }

  /* -- live stream ------------------------------------------------------- */

  var pollTimer = null;

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(refreshState, 5000);
  }

  function connect() {
    if (!window.EventSource) { startPolling(); return; }
    var source = new EventSource("/events");

    source.addEventListener("open", function () {
      if (liveDot) liveDot.classList.remove("offline");
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    });

    source.addEventListener("job.log", function (event) {
      var data = JSON.parse(event.data);
      if (!activeRunId || data.run_id === activeRunId) appendLine(data.line, false);
    });

    source.addEventListener("job", function (event) {
      var run = JSON.parse(event.data);
      upsertRun(run);
      if (run.status === "running") {
        activeRunId = run.id;
        if (consoleEl) {
          consoleEl.dataset.runId = run.id;
          consoleEl.textContent = "";
          appendLine("$ " + run.command, false);
        }
      }
      if (run.status === "failed" && run.error) appendLine(run.error, true);
      if (run.status === "succeeded" || run.status === "failed") {
        toast(run.label + ": " + run.status);
      }
    });

    source.addEventListener("state.changed", function () {
      refreshState();
    });

    source.addEventListener("error", function () {
      if (liveDot) liveDot.classList.add("offline");
      // EventSource retries on its own; polling covers the gap meanwhile.
      startPolling();
    });
  }

  if (activeRunId) loadRun(activeRunId);
  connect();
  refreshState();

  /* -- action forms ------------------------------------------------------ */

  document.querySelectorAll("form.action-form").forEach(function (form) {
    form.addEventListener("submit", function () {
      var button = form.querySelector("button");
      if (button) button.textContent = "Queued…";
    });
  });
})();
