(function () {
  var band = document.querySelector("[data-status-band]");
  var appId = band && band.getAttribute("data-app");
  if (!appId) return;
  var api = "/api/apps/" + encodeURIComponent(appId);

  var releasesPanel = document.querySelector("[data-releases]");
  var diagnosisPanel = document.querySelector("[data-diagnosis]");
  var fixPanel = document.querySelector("[data-fix-panel]");
  var pickStatus = releasesPanel ? releasesPanel.querySelector("[data-pick-status]") : null;

  function getJSON(url) {
    return fetch(url, { cache: "no-store" }).then(function (r) {
      return r.json();
    });
  }

  function sendJSON(url, body, method) {
    return fetch(url, {
      method: method || "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
      cache: "no-store"
    }).then(function (r) {
      return r.json().then(function (b) {
        return { status: r.status, body: b };
      });
    });
  }

  function num(value, digits) {
    var n = Number(value) || 0;
    return n.toLocaleString("en-US", {
      minimumFractionDigits: digits || 0,
      maximumFractionDigits: digits || 0
    });
  }

  // —— pick / revert ——

  function setPickStatus(text, show) {
    if (!pickStatus) return;
    pickStatus.textContent = text || "";
    if (show) pickStatus.removeAttribute("hidden");
    else pickStatus.setAttribute("hidden", "");
  }

  function setPickButtonsDisabled(disabled) {
    document.querySelectorAll("[data-pick]").forEach(function (btn) {
      btn.disabled = !!disabled;
    });
  }

  function waitForVersion(version, attempts) {
    attempts = attempts || 60;
    return getJSON(api).then(function (data) {
      var current = (data.app && data.app.live_version) || "";
      if (current === version) return data;
      if (attempts <= 1) throw new Error("Timed out waiting for " + version);
      return new Promise(function (resolve) {
        setTimeout(function () {
          resolve(waitForVersion(version, attempts - 1));
        }, 1500);
      });
    });
  }

  function onPickClick(evt) {
    var btn = evt.target.closest("[data-pick]");
    if (!btn) return;
    var version = btn.getAttribute("data-pick");
    var force = btn.getAttribute("data-force") === "1";
    if (force && !window.confirm("Version " + version + " is quarantined. Restore it anyway?")) return;
    setPickButtonsDisabled(true);
    setPickStatus("Restoring " + version + "…", true);
    sendJSON(api + "/pick", { version: version, force: force })
      .then(function (result) {
        if (result.status === 200 && result.body.status === "already_live") {
          setPickStatus("Already live: " + version, true);
          setPickButtonsDisabled(false);
          return;
        }
        if (result.status !== 202) {
          setPickStatus((result.body && result.body.message) || "Restore failed", true);
          setPickButtonsDisabled(false);
          return;
        }
        setPickStatus("Swap accepted, waiting for " + version + "…", true);
        return waitForVersion(version).then(function () {
          setPickStatus("Live on " + version + ", reloading", true);
          window.location.reload();
        });
      })
      .catch(function (err) {
        setPickStatus(String(err.message || err), true);
        setPickButtonsDisabled(false);
      });
  }

  if (releasesPanel) releasesPanel.addEventListener("click", onPickClick);
  if (diagnosisPanel) diagnosisPanel.addEventListener("click", onPickClick);

  // —— AI fix ——

  document.querySelectorAll("[data-ai-fix]").forEach(function (fixBtn) {
    fixBtn.addEventListener("click", function () {
      fixBtn.disabled = true;
      sendJSON(api + "/fix", {})
        .then(function (res) {
          if (res.status >= 400) {
            window.alert(res.body.message || res.body.error || "AI fix failed");
            fixBtn.disabled = false;
            return;
          }
          window.location.reload();
        })
        .catch(function (err) {
          window.alert(String(err.message || err));
          fixBtn.disabled = false;
        });
    });
  });

  function pollFix() {
    if (!fixPanel) return;
    var statusEl = fixPanel.querySelector("[data-fix-status]");
    if (!statusEl || statusEl.textContent.trim() !== "running") return;
    getJSON(api + "/fix")
      .then(function (job) {
        if (job && job.status && job.status !== "running") window.location.reload();
      })
      .catch(function () {});
  }
  setInterval(pollFix, 3000);

  var shipBtn = document.querySelector("[data-ship]");
  if (shipBtn) {
    shipBtn.addEventListener("click", function () {
      var input = document.querySelector("[data-ship-version]");
      var version = (input && input.value.trim()) || "";
      shipBtn.disabled = true;
      setPickStatus("Building " + version + " from the AI fix…", true);
      sendJSON(api + "/fix/ship", { version: version })
        .then(function (res) {
          if (res.status >= 400) {
            window.alert(res.body.message || "Ship failed");
            shipBtn.disabled = false;
            return;
          }
          window.location.reload();
        })
        .catch(function (err) {
          window.alert(String(err.message || err));
          shipBtn.disabled = false;
        });
    });
  }

  var discardBtn = document.querySelector("[data-discard]");
  if (discardBtn) {
    discardBtn.addEventListener("click", function () {
      sendJSON(api + "/fix/discard", {}).then(function () {
        window.location.reload();
      });
    });
  }

  // —— GitHub source ——

  var sourceStatus = document.querySelector("[data-source-status]");
  function setSourceStatus(text) {
    if (!sourceStatus) return;
    sourceStatus.textContent = text;
    sourceStatus.removeAttribute("hidden");
  }

  var connectForm = document.querySelector("[data-github-connect]");
  if (connectForm) {
    connectForm.addEventListener("submit", function (evt) {
      evt.preventDefault();
      var data = Object.fromEntries(new FormData(connectForm).entries());
      setSourceStatus("Connecting " + data.repo + "…");
      sendJSON(api + "/github", data).then(function (res) {
        if (res.status >= 400) {
          setSourceStatus(res.body.message || "Connect failed");
          return;
        }
        window.location.reload();
      });
    });
  }

  var disconnectBtn = document.querySelector("[data-github-disconnect]");
  if (disconnectBtn) {
    disconnectBtn.addEventListener("click", function () {
      sendJSON(api + "/github", {}, "DELETE").then(function () {
        window.location.reload();
      });
    });
  }

  // —— charts ——

  var NS = "http://www.w3.org/2000/svg";
  var W = 600;
  var H = 72;
  var wrap = document.querySelector("[data-charts]");
  var legend = document.querySelector("[data-legend]");
  var readout = document.querySelector("[data-readout]");
  var rows = wrap ? Array.prototype.slice.call(wrap.querySelectorAll("[data-chart]")) : [];
  var STYLES = [
    { stroke: "#0b0b0b", dash: "", swatch: "" },
    { stroke: "#0b0b0b", dash: "7 5", swatch: "dashed" },
    { stroke: "#888888", dash: "", swatch: "grey" },
    { stroke: "#888888", dash: "7 5", swatch: "grey dashed" }
  ];
  var BAD_STYLE = { stroke: "#ff2a00", dash: "", swatch: "alarm" };
  var BAD_EVENTS = { first_bad_vote: 1, majority: 1, rollback_failed: 1, no_target: 1, waiting: 1 };

  var state = {
    mode: "live",
    series: null,
    recordings: [],
    verifying: null,
    hidden: {},
    selected: null,
    playhead: 0,
    playing: false,
    following: false,
    wasVerifying: band.getAttribute("data-verifying") || ""
  };

  function svgEl(name, attrs) {
    var node = document.createElementNS(NS, name);
    Object.keys(attrs).forEach(function (key) {
      node.setAttribute(key, attrs[key]);
    });
    return node;
  }

  function clearSvg(svg) {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
  }

  function yFor(value, maxV) {
    return H - ((Number(value) || 0) / maxV) * (H - 8) - 4;
  }

  function drawLimit(svg, thresh, maxV) {
    var y = yFor(thresh, maxV).toFixed(1);
    svg.appendChild(
      svgEl("line", {
        x1: "0",
        x2: String(W),
        y1: y,
        y2: y,
        stroke: "#ff2a00",
        "stroke-width": "2",
        "stroke-dasharray": "4 3"
      })
    );
  }

  function drawLine(svg, points, style, width, opacity) {
    if (!points.length) return;
    var d = points
      .map(function (p, i) {
        return (i === 0 ? "M" : "L") + p[0].toFixed(1) + " " + p[1].toFixed(1);
      })
      .join(" ");
    var attrs = {
      d: d,
      fill: "none",
      stroke: style.stroke,
      "stroke-width": String(width || 3),
      "stroke-linejoin": "round",
      opacity: String(opacity == null ? 1 : opacity)
    };
    if (style.dash) attrs["stroke-dasharray"] = style.dash;
    svg.appendChild(svgEl("path", attrs));
  }

  function paintLive() {
    var data = state.series;
    if (!data) return;
    var map = { cpu: data.cpu, memory: data.memory, p95_ms: data.p95_ms, error_pct: data.error_pct };
    var start = data.window ? data.window.start : 0;
    var end = data.window ? data.window.end : 1;
    rows.forEach(function (row) {
      var svg = row.querySelector("svg");
      var thresh = Number(row.getAttribute("data-thresh") || 0);
      var series = map[row.getAttribute("data-chart")] || [];
      var live = row.querySelector("[data-live-val]");
      if (live) {
        live.textContent = series.length
          ? num(series[series.length - 1].v) + (row.getAttribute("data-unit") || "")
          : "—";
      }
      clearSvg(svg);
      var maxV = Math.max(thresh * 1.5, Math.max.apply(null, series.map(function (p) { return Number(p.v) || 0; }).concat([1])));
      drawLimit(svg, thresh, maxV);
      drawLine(
        svg,
        series.map(function (p) {
          return [((p.t - start) / Math.max(end - start, 1)) * W, yFor(p.v, maxV)];
        }),
        STYLES[0]
      );
    });
    if (readout) {
      var cpu = data.cpu || [];
      readout.textContent = cpu.length
        ? "Live · " + cpu.length + " samples in the last 15 minutes · red dashes are the verify limits"
        : "Waiting for the first samples…";
    }
  }

  function styleFor(rec, index) {
    if (rec.decision === "anomaly") return BAD_STYLE;
    var healthyIndex = 0;
    for (var i = 0; i < index; i += 1) {
      if (state.recordings[i].decision !== "anomaly") healthyIndex += 1;
    }
    return STYLES[healthyIndex % STYLES.length];
  }

  function recEnd(rec) {
    var end = 0;
    (rec.samples || []).forEach(function (s) { end = Math.max(end, s.t); });
    (rec.events || []).forEach(function (e) { end = Math.max(end, e.t); });
    return end;
  }

  function compareSpan() {
    var span = 10;
    state.recordings.forEach(function (rec) {
      if (state.hidden[rec.version]) return;
      span = Math.max(span, Number(rec.window_s) || 0, recEnd(rec));
    });
    return span;
  }

  function sampleAt(rec, t) {
    var found = null;
    (rec.samples || []).forEach(function (s) {
      if (s.t <= t + 0.001) found = s;
    });
    return found;
  }

  function selectedRec() {
    for (var i = 0; i < state.recordings.length; i += 1) {
      if (state.recordings[i].version === state.selected) return state.recordings[i];
    }
    return null;
  }

  function paintCompare() {
    var span = compareSpan();
    var chosen = selectedRec();
    rows.forEach(function (row) {
      var svg = row.querySelector("svg");
      var key = row.getAttribute("data-rec");
      var thresh = Number(row.getAttribute("data-thresh") || 0);
      var unit = row.getAttribute("data-unit") || "";
      clearSvg(svg);
      var maxV = thresh * 1.5;
      state.recordings.forEach(function (rec) {
        if (state.hidden[rec.version]) return;
        (rec.samples || []).forEach(function (s) { maxV = Math.max(maxV, Number(s[key]) || 0); });
      });
      drawLimit(svg, thresh, maxV);
      state.recordings.forEach(function (rec, index) {
        if (state.hidden[rec.version]) return;
        var points = (rec.samples || []).map(function (s) {
          return [(s.t / span) * W, yFor(s[key], maxV)];
        });
        var dim = chosen && state.playing && rec.version !== chosen.version;
        drawLine(svg, points, styleFor(rec, index), 3, dim ? 0.3 : 1);
      });
      if (chosen && !state.hidden[chosen.version]) {
        (chosen.events || []).forEach(function (evt) {
          if (evt.kind !== "majority" && evt.kind !== "restored" && evt.kind !== "healthy") return;
          var x = ((evt.t / span) * W).toFixed(1);
          svg.appendChild(svgEl("line", { x1: x, x2: x, y1: "0", y2: "7", stroke: evt.kind === "majority" ? "#ff2a00" : "#0b0b0b", "stroke-width": "4" }));
        });
        var x = ((state.playhead / span) * W).toFixed(1);
        svg.appendChild(svgEl("line", { x1: x, x2: x, y1: "0", y2: String(H), stroke: "#0b0b0b", "stroke-width": "2" }));
        var s = sampleAt(chosen, state.playhead);
        if (s) {
          svg.appendChild(
            svgEl("circle", {
              cx: ((s.t / span) * W).toFixed(1),
              cy: yFor(s[key], maxV).toFixed(1),
              r: "5",
              fill: s.anomaly ? "#ff2a00" : "#ffe500",
              stroke: "#0b0b0b",
              "stroke-width": "2"
            })
          );
        }
        var live = row.querySelector("[data-live-val]");
        if (live) live.textContent = s ? chosen.version + " · " + num(s[key], key === "error_rate" ? 1 : 0) + unit : chosen.version;
      }
    });
    if (readout) {
      var sample = chosen ? sampleAt(chosen, state.playhead) : null;
      if (!state.recordings.length) {
        readout.textContent = "No recordings yet. Each verify window is recorded and overlaid here.";
      } else if (sample) {
        readout.textContent =
          chosen.version + " at +" + num(state.playhead, 1) + " s · cpu " + num(sample.cpu) + "% · memory " + num(sample.memory) +
          "% · p95 " + num(sample.latency_p95) + " ms · errors " + num(sample.error_rate, 1) + "% · votes " + sample.votes + "/" + chosen.majority +
          (sample.anomaly ? " · ANOMALOUS" : "");
      } else {
        readout.textContent = "Seconds since deploy on the x axis. Red dashes are the verify limits.";
      }
    }
  }

  function paintCharts() {
    if (!rows.length) return;
    if (state.mode === "compare") paintCompare();
    else paintLive();
  }

  function renderLegend() {
    if (!legend) return;
    legend.innerHTML = "";
    if (state.mode !== "compare") {
      legend.setAttribute("hidden", "");
      return;
    }
    legend.removeAttribute("hidden");
    state.recordings.forEach(function (rec, index) {
      var style = styleFor(rec, index);
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "legend-item" + (state.hidden[rec.version] ? " off" : "");
      btn.innerHTML = '<span class="swatch ' + style.swatch + '"></span>';
      btn.appendChild(document.createTextNode(rec.version + " · " + (rec.decision || "verifying")));
      btn.addEventListener("click", function () {
        state.hidden[rec.version] = !state.hidden[rec.version];
        renderLegend();
        paintCharts();
      });
      legend.appendChild(btn);
    });
  }

  function setMode(mode) {
    state.mode = mode;
    if (wrap) {
      wrap.querySelectorAll("[data-mode]").forEach(function (btn) {
        btn.classList.toggle("active", btn.getAttribute("data-mode") === mode);
      });
    }
    renderLegend();
    paintCharts();
  }

  if (wrap) {
    wrap.querySelectorAll("[data-mode]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        setMode(btn.getAttribute("data-mode"));
      });
    });
  }

  // —— replay ——

  var replay = document.querySelector("[data-replay]");
  var replaySelect = replay ? replay.querySelector("[data-replay-version]") : null;
  var replaySteps = replay ? replay.querySelector("[data-replay-steps]") : null;
  var replayEmpty = replay ? replay.querySelector("[data-replay-empty]") : null;
  var replayScrub = replay ? replay.querySelector("[data-replay-scrub]") : null;
  var replayClock = replay ? replay.querySelector("[data-replay-clock]") : null;
  var playBtn = replay ? replay.querySelector("[data-replay-play]") : null;
  var stepBtn = replay ? replay.querySelector("[data-replay-step]") : null;
  var lastFrame = 0;

  function renderReplayOptions() {
    if (!replaySelect) return;
    var current = state.selected;
    replaySelect.innerHTML = "";
    state.recordings
      .slice()
      .reverse()
      .forEach(function (rec) {
        var opt = document.createElement("option");
        opt.value = rec.version;
        opt.textContent = rec.version + " · " + (rec.decision || "verifying");
        if (rec.version === current) opt.selected = true;
        replaySelect.appendChild(opt);
      });
    var has = state.recordings.length > 0;
    if (replayEmpty) replayEmpty.hidden = has;
    [replaySelect, playBtn, stepBtn, replayScrub].forEach(function (node) {
      if (node) node.disabled = !has;
    });
  }

  function renderSteps() {
    if (!replaySteps) return;
    var rec = selectedRec();
    replaySteps.innerHTML = "";
    if (!rec) return;
    var events = rec.events || [];
    var currentIndex = -1;
    events.forEach(function (evt, i) {
      if (evt.t <= state.playhead + 0.001) currentIndex = i;
    });
    events.forEach(function (evt, i) {
      var li = document.createElement("li");
      li.className =
        (BAD_EVENTS[evt.kind] ? "bad " : "") +
        (i <= currentIndex ? "done" : "") +
        (i === currentIndex ? " current" : "");
      li.innerHTML = '<span class="when"></span><span class="what"></span>';
      li.querySelector(".when").textContent = "+" + num(evt.t, 1) + " s";
      li.querySelector(".what").textContent = evt.label;
      li.addEventListener("click", function () {
        stopPlaying();
        state.following = false;
        seek(evt.t);
      });
      replaySteps.appendChild(li);
    });
  }

  function seek(t) {
    var rec = selectedRec();
    var end = rec ? recEnd(rec) : 0;
    state.playhead = Math.max(0, Math.min(t, end));
    if (replayScrub) {
      replayScrub.max = String(Math.max(end, 1));
      replayScrub.value = String(state.playhead);
    }
    if (replayClock) replayClock.textContent = "+" + num(state.playhead, 1) + " s";
    renderSteps();
    if (state.mode === "compare") paintCharts();
  }

  function stopPlaying() {
    state.playing = false;
    if (playBtn) playBtn.textContent = "Play";
  }

  function frame(now) {
    if (!state.playing) return;
    var rec = selectedRec();
    if (!rec) return stopPlaying();
    var dt = lastFrame ? (now - lastFrame) / 1000 : 0;
    lastFrame = now;
    var next = state.playhead + dt * 4;
    if (next >= recEnd(rec)) {
      seek(recEnd(rec));
      stopPlaying();
      paintCharts();
      return;
    }
    seek(next);
    window.requestAnimationFrame(frame);
  }

  function play() {
    var rec = selectedRec();
    if (!rec) return;
    if (state.mode !== "compare") setMode("compare");
    if (state.playhead >= recEnd(rec) - 0.05) seek(0);
    state.playing = true;
    state.following = false;
    lastFrame = 0;
    if (playBtn) playBtn.textContent = "Pause";
    window.requestAnimationFrame(frame);
  }

  if (playBtn) {
    playBtn.addEventListener("click", function () {
      if (state.playing) {
        stopPlaying();
        paintCharts();
      } else {
        play();
      }
    });
  }

  if (stepBtn) {
    stepBtn.addEventListener("click", function () {
      var rec = selectedRec();
      if (!rec) return;
      stopPlaying();
      state.following = false;
      if (state.mode !== "compare") setMode("compare");
      var next = null;
      (rec.events || []).forEach(function (evt) {
        if (next === null && evt.t > state.playhead + 0.05) next = evt.t;
      });
      seek(next === null ? 0 : next);
    });
  }

  if (replayScrub) {
    replayScrub.addEventListener("input", function () {
      stopPlaying();
      state.following = false;
      if (state.mode !== "compare") setMode("compare");
      seek(Number(replayScrub.value) || 0);
    });
  }

  if (replaySelect) {
    replaySelect.addEventListener("change", function () {
      stopPlaying();
      state.following = false;
      state.selected = replaySelect.value;
      seek(0);
      paintCharts();
    });
  }

  // —— live verify banner ——

  var banner = document.querySelector("[data-verify-banner]");
  function renderBanner() {
    if (!banner) return;
    var p = state.verifying;
    if (!p) {
      banner.setAttribute("hidden", "");
      return;
    }
    banner.removeAttribute("hidden");
    var last = p.last || {};
    var text = "Verifying " + p.version + " · " + num(p.elapsed_s) + " / " + p.window_s + " s · votes " + p.votes + "/" + p.majority;
    if (p.last) {
      text += " · cpu " + num(last.cpu) + "% · p95 " + num(last.latency_p95) + " ms · errors " + num(last.error_rate, 1) + "%";
    }
    banner.querySelector("[data-verify-text]").textContent = text;
    banner.querySelector("[data-verify-meter]").style.width =
      Math.min(100, (100 * (Number(p.elapsed_s) || 0)) / Math.max(Number(p.window_s) || 1, 1)) + "%";
    banner.classList.toggle("bad", !!last.anomaly);
  }

  // —— data loops ——

  function applyRecordings(data) {
    state.recordings = data.recordings || [];
    state.verifying = data.verifying || null;
    var versions = state.recordings.map(function (r) { return r.version; });
    if (state.verifying && versions.indexOf(state.verifying.version) >= 0 && !state.playing) {
      if (state.selected !== state.verifying.version) state.following = true;
      state.selected = state.verifying.version;
    }
    if (!state.selected || versions.indexOf(state.selected) < 0) {
      var incident = band.getAttribute("data-incident");
      state.selected = versions.indexOf(incident) >= 0 ? incident : versions[versions.length - 1] || null;
    }
    renderReplayOptions();
    renderLegend();
    if (state.following && !state.playing) {
      var rec = selectedRec();
      seek(rec ? recEnd(rec) : 0);
    } else {
      seek(state.playhead);
    }
    renderBanner();
    paintCharts();

    var nowVerifying = state.verifying ? state.verifying.version : "";
    if (nowVerifying !== state.wasVerifying) {
      setTimeout(function () { window.location.reload(); }, nowVerifying ? 300 : 1200);
    }
    state.wasVerifying = nowVerifying;
  }

  function refreshRecordings() {
    getJSON(api + "/recordings")
      .then(applyRecordings)
      .catch(function () {})
      .then(function () {
        setTimeout(refreshRecordings, state.verifying ? 3000 : 5000);
      });
  }

  function paintProbes(probes) {
    if (!probes) return;
    function paint(el, probe) {
      if (!el || !probe) return;
      var ok = probe.ok !== false;
      el.classList.toggle("probe-fail", !ok);
      el.classList.toggle("probe-ok", ok);
      el.querySelector("[data-status]").textContent = probe.status;
      el.querySelector("[data-ms]").textContent = num(probe.ms) + " ms";
    }
    paint(document.getElementById("probe-health"), probes.health);
    (probes.probes || []).forEach(function (probe) {
      document.querySelectorAll("[data-probe-path]").forEach(function (el) {
        if (el.getAttribute("data-probe-path") === probe.path) paint(el, probe);
      });
    });
  }

  function fetchSeries() {
    getJSON(api + "/series?minutes=15")
      .then(function (data) {
        if (!data || !data.ok) return;
        state.series = data;
        if (state.mode === "live") paintCharts();
        paintProbes(data.probes);
      })
      .catch(function () {});
  }

  fetchSeries();
  setInterval(fetchSeries, 5000);
  refreshRecordings();
})();
