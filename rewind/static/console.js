(function () {
  var band = document.querySelector("[data-status-band]");
  var appId = band && band.getAttribute("data-app");
  if (!appId) return;

  var releasesPanel = document.querySelector("[data-releases]");
  var diagnosisPanel = document.querySelector("[data-diagnosis]");
  var fixPanel = document.querySelector("[data-fix-panel]");
  var pickStatus = releasesPanel
    ? releasesPanel.querySelector("[data-pick-status]")
    : null;

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
    attempts = attempts || 40;
    return fetch("/api/apps/" + appId, { cache: "no-store" })
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
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
    if (force) {
      var ok = window.confirm(
        "Version " +
          version +
          " is quarantined. Restore it anyway?"
      );
      if (!ok) return;
    }
    setPickButtonsDisabled(true);
    setPickStatus("Restoring " + version + "…", true);
    fetch("/api/apps/" + appId + "/pick", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version: version, force: force }),
      cache: "no-store"
    })
      .then(function (response) {
        return response.json().then(function (body) {
          return { status: response.status, body: body };
        });
      })
      .then(function (result) {
        if (result.status === 409) {
          setPickStatus(result.body.message || "Quarantined", true);
          setPickButtonsDisabled(false);
          return;
        }
        if (result.status === 200 && result.body.status === "already_live") {
          setPickStatus("Already live: " + version, true);
          setPickButtonsDisabled(false);
          return;
        }
        if (result.status !== 202) {
          setPickStatus(
            (result.body && result.body.message) || "Restore failed",
            true
          );
          setPickButtonsDisabled(false);
          return;
        }
        setPickStatus("Swap accepted — waiting for " + version + "…", true);
        return waitForVersion(version).then(function () {
          setPickStatus("Live on " + version + " — reloading", true);
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

  var fixBtn = document.querySelector("[data-ai-fix]");
  if (fixBtn) {
    fixBtn.addEventListener("click", function () {
      fixBtn.disabled = true;
      fetch("/api/apps/" + appId + "/fix", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
        cache: "no-store"
      })
        .then(function (r) {
          return r.json().then(function (b) {
            return { status: r.status, body: b };
          });
        })
        .then(function (res) {
          if (res.status >= 400 && res.body.status === "error") {
            window.alert(res.body.message || "AI fix failed");
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
  }

  function pollFix() {
    if (!fixPanel) return;
    var statusEl = fixPanel.querySelector("[data-fix-status]");
    if (!statusEl) return;
    if (statusEl.textContent.trim() !== "running") return;
    fetch("/api/apps/" + appId + "/fix", { cache: "no-store" })
      .then(function (r) {
        return r.json();
      })
      .then(function (job) {
        if (!job || job.status === "empty") return;
        if (job.status && job.status !== "running") {
          window.location.reload();
        }
      })
      .catch(function () {});
  }
  setInterval(pollFix, 3000);

  var shipBtn = document.querySelector("[data-ship]");
  if (shipBtn) {
    shipBtn.addEventListener("click", function () {
      var input = document.querySelector("[data-ship-version]");
      var version = (input && input.value) || "3.0.0";
      shipBtn.disabled = true;
      fetch("/api/apps/" + appId + "/fix/ship", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ version: version }),
        cache: "no-store"
      })
        .then(function (r) {
          return r.json().then(function (b) {
            return { status: r.status, body: b };
          });
        })
        .then(function (res) {
          if (res.status >= 400) {
            window.alert(res.body.message || "Ship failed");
            shipBtn.disabled = false;
            return;
          }
          setPickStatus("Shipped " + version + " — verifying…", true);
          return waitForVersion(version).then(function () {
            window.location.reload();
          });
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
      fetch("/api/apps/" + appId + "/fix/discard", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
        cache: "no-store"
      }).then(function () {
        window.location.reload();
      });
    });
  }

  function paintSpark(svg, points, thresh, maxHint) {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    if (!points || !points.length) return;
    var w = 600;
    var h = 72;
    var vals = points.map(function (p) {
      return Number(p.v) || 0;
    });
    var maxV = Math.max(maxHint || 0, thresh * 1.5, Math.max.apply(null, vals.concat([1])));
    var ns = "http://www.w3.org/2000/svg";
    var threshY = h - (thresh / maxV) * (h - 8) - 4;
    var line = document.createElementNS(ns, "line");
    line.setAttribute("x1", "0");
    line.setAttribute("x2", String(w));
    line.setAttribute("y1", String(threshY));
    line.setAttribute("y2", String(threshY));
    line.setAttribute("stroke", "#ff2a00");
    line.setAttribute("stroke-width", "2");
    line.setAttribute("stroke-dasharray", "4 3");
    svg.appendChild(line);
    var d = [];
    points.forEach(function (p, i) {
      var x = points.length === 1 ? 0 : (i / (points.length - 1)) * w;
      var y = h - ((Number(p.v) || 0) / maxV) * (h - 8) - 4;
      d.push((i === 0 ? "M" : "L") + x.toFixed(1) + " " + y.toFixed(1));
    });
    var path = document.createElementNS(ns, "path");
    path.setAttribute("d", d.join(" "));
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "#0b0b0b");
    path.setAttribute("stroke-width", "3");
    svg.appendChild(path);
  }

  function fetchSeries() {
    var wrap = document.querySelector("[data-charts]");
    if (!wrap) return;
    fetch("/api/apps/" + appId + "/series?minutes=15", { cache: "no-store" })
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        if (!data || !data.ok) return;
        var map = {
          cpu: data.cpu,
          memory: data.memory,
          p95_ms: data.p95_ms,
          error_pct: data.error_pct
        };
        wrap.querySelectorAll("[data-chart]").forEach(function (row) {
          var key = row.getAttribute("data-chart");
          var thresh = Number(row.getAttribute("data-thresh") || 0);
          var svg = row.querySelector("svg");
          var live = row.querySelector("[data-live-val]");
          var series = map[key] || [];
          if (live && series.length) {
            live.textContent =
              Math.round(series[series.length - 1].v) +
              (row.getAttribute("data-unit") || "");
          }
          if (svg) paintSpark(svg, series, thresh, thresh * 1.5);
        });
        if (data.probes) {
          var health = data.probes.health;
          var healthEl = document.getElementById("probe-health");
          if (healthEl && health) {
            healthEl.querySelector("[data-status]").textContent = health.status;
            healthEl.querySelector("[data-ms]").textContent =
              Math.round(health.ms) + " ms";
          }
          (data.probes.probes || []).forEach(function (probe) {
            var el = document.querySelector(
              '[data-probe-path="' + probe.path + '"]'
            );
            if (!el) return;
            el.querySelector("[data-status]").textContent = probe.status;
            el.querySelector("[data-ms]").textContent =
              Math.round(probe.ms) + " ms";
          });
        }
      })
      .catch(function () {});
  }

  fetchSeries();
  setInterval(fetchSeries, 5000);
})();
