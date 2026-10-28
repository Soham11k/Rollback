"""Before-vs-After release comparison: metrics, per-route probes, AST symbols, side-by-side code diff, and DevSecOps scan deltas."""

from __future__ import annotations

import ast
import difflib
from pathlib import Path
from typing import Any

from diagnosis import FEATURE_NAMES, LIMITS, UNITS
from rank import _version_key, pick_best
from registry import Registry
from scanner import scan_release

SIGNAL_LABELS = {
    "cpu": "CPU usage",
    "memory": "Memory usage",
    "latency_p95": "p95 latency",
    "error_rate": "HTTP 5xx error rate",
}


def _peaks_for(registry: Registry, app_id: str, version: str) -> dict[str, float]:
    verify_doc = registry.read_verify(app_id, version) or {}
    peaks = dict(verify_doc.get("peaks") or {})
    if peaks:
        return {name: round(float(peaks.get(name) or 0.0), 2) for name in FEATURE_NAMES}
    rec = registry.read_recording(app_id, version) or {}
    samples = rec.get("samples") or []
    out = {name: 0.0 for name in FEATURE_NAMES}
    for row in samples:
        for name in FEATURE_NAMES:
            out[name] = max(out[name], float(row.get(name) or 0.0))
    return {name: round(val, 2) for name, val in out.items()}


def _path_stats_for(registry: Registry, app_id: str, version: str) -> dict[str, dict[str, float]]:
    verify_doc = registry.read_verify(app_id, version) or {}
    return dict(verify_doc.get("path_stats") or {})


def _ensure_scan(registry: Registry, app_id: str, version: str) -> dict[str, Any]:
    scan = registry.read_scan(app_id, version)
    if scan is not None:
        return scan
    try:
        return scan_release(registry, app_id, version)
    except Exception:  # noqa: BLE001
        return {
            "app_id": app_id,
            "version": version,
            "grade": "—",
            "score": 100,
            "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0, "total": 0},
            "by_tool": {},
            "image": {"name": "none", "size_mb": 0.0, "layers": 0},
            "sbom_summary": {"format": "CycloneDX 1.5", "package_count": 0, "packages": []},
            "findings": [],
        }


def comparison_presets(registry: Registry, app_id: str) -> list[dict[str, str]]:
    releases = sorted(
        registry.list_releases(app_id),
        key=lambda r: _version_key(str(r.get("version") or "")),
    )
    if len(releases) < 2:
        return []

    quarantined = [r for r in releases if r.get("quarantined") or r.get("decision") == "anomaly"]
    bad_ver = str(quarantined[-1].get("version")) if quarantined else None

    if bad_ver:
        prior_healthy = [
            r
            for r in releases
            if r.get("decision") == "healthy"
            and not r.get("quarantined")
            and _version_key(str(r.get("version") or "")) < _version_key(bad_ver)
        ]
    else:
        prior_healthy = []
    prior_best = pick_best(prior_healthy) if prior_healthy else pick_best(releases)
    best_ver = str(prior_best.get("version")) if prior_best else str(releases[0].get("version"))

    fixes = [r for r in releases if r.get("fixes_version")]
    if not fixes and bad_ver:
        fixes = [
            r
            for r in releases
            if r.get("decision") == "healthy"
            and not r.get("quarantined")
            and _version_key(str(r.get("version") or "")) > _version_key(bad_ver)
        ]
    fix_ver = str(fixes[-1].get("version")) if fixes else None

    presets: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, label: str, before: str | None, after: str | None) -> None:
        if not before or not after or before == after or (before, after) in seen:
            return
        seen.add((before, after))
        presets.append({"id": kind, "label": label, "before": before, "after": after})

    if bad_ver and best_ver and best_ver != bad_ver:
        add("best_to_bad", f"What broke ({best_ver} → {bad_ver})", best_ver, bad_ver)
    if bad_ver and fix_ver:
        add("bad_to_fix", f"AI fix delta ({bad_ver} → {fix_ver})", bad_ver, fix_ver)
    if best_ver and fix_ver and best_ver != fix_ver:
        add("best_to_fix", f"Baseline vs fixed ({best_ver} → {fix_ver})", best_ver, fix_ver)

    prev_ver = str(releases[-2].get("version"))
    latest_ver = str(releases[-1].get("version"))
    add("prev_to_latest", f"Latest release ({prev_ver} → {latest_ver})", prev_ver, latest_ver)
    return presets


def default_pair(registry: Registry, app_id: str) -> tuple[str | None, str | None]:
    releases = sorted(
        registry.list_releases(app_id),
        key=lambda r: _version_key(str(r.get("version") or "")),
    )
    if not releases:
        return None, None
    if len(releases) == 1:
        only = str(releases[0].get("version"))
        return only, only

    presets = comparison_presets(registry, app_id)
    by_id = {p["id"]: p for p in presets}
    if "bad_to_fix" in by_id:
        p = by_id["bad_to_fix"]
        return p["before"], p["after"]
    if "best_to_bad" in by_id:
        p = by_id["best_to_bad"]
        return p["before"], p["after"]
    if presets:
        return presets[0]["before"], presets[0]["after"]
    return str(releases[-2].get("version")), str(releases[-1].get("version"))


def _extract_python_symbols(src_dir: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """Extract functions, classes, and module-level assignments across all .py files."""
    symbols: dict[tuple[str, str], dict[str, Any]] = {}
    if not src_dir.is_dir():
        return symbols

    for path in sorted(src_dir.rglob("*.py")):
        if path.name.startswith("._") or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(src_dir).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(text, filename=rel)
        except (OSError, SyntaxError):
            continue
        lines = text.splitlines()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                start = node.lineno
                end = getattr(node, "end_lineno", None) or start
                body_lines = lines[start - 1 : end]
                symbols[(rel, node.name)] = {
                    "file": rel,
                    "name": node.name,
                    "kind": kind,
                    "start_line": start,
                    "end_line": end,
                    "line_count": max(1, end - start + 1),
                    "body": "\n".join(line.rstrip() for line in body_lines),
                }
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                target_name = None
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    target_name = node.targets[0].id
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    target_name = node.target.id
                if target_name:
                    start = node.lineno
                    end = getattr(node, "end_lineno", None) or start
                    body_lines = lines[start - 1 : end]
                    symbols[(rel, target_name)] = {
                        "file": rel,
                        "name": target_name,
                        "kind": "constant",
                        "start_line": start,
                        "end_line": end,
                        "line_count": max(1, end - start + 1),
                        "body": "\n".join(line.rstrip() for line in body_lines),
                    }
    return symbols


def compute_ast_changes(before_src: Path, after_src: Path) -> dict[str, Any]:
    before_syms = _extract_python_symbols(before_src)
    after_syms = _extract_python_symbols(after_src)

    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    modified: list[dict[str, Any]] = []
    unchanged_count = 0

    for key in sorted(before_syms.keys() | after_syms.keys()):
        b = before_syms.get(key)
        a = after_syms.get(key)
        if b is None and a is not None:
            added.append(
                {
                    "file": a["file"],
                    "name": a["name"],
                    "kind": a["kind"],
                    "after_line": a["start_line"],
                    "line_count": a["line_count"],
                }
            )
        elif a is None and b is not None:
            removed.append(
                {
                    "file": b["file"],
                    "name": b["name"],
                    "kind": b["kind"],
                    "before_line": b["start_line"],
                    "line_count": b["line_count"],
                }
            )
        elif b is not None and a is not None:
            if b["body"] != a["body"]:
                b_lines = b["body"].splitlines()
                a_lines = a["body"].splitlines()
                matcher = difflib.SequenceMatcher(None, b_lines, a_lines)
                plus = sum(j2 - j1 for tag, _, _, j1, j2 in matcher.get_opcodes() if tag in {"insert", "replace"})
                minus = sum(i2 - i1 for tag, i1, i2, _, _ in matcher.get_opcodes() if tag in {"delete", "replace"})
                modified.append(
                    {
                        "file": a["file"],
                        "name": a["name"],
                        "kind": a["kind"],
                        "before_line": b["start_line"],
                        "after_line": a["start_line"],
                        "added_lines": plus,
                        "removed_lines": minus,
                        "delta_lines": a["line_count"] - b["line_count"],
                    }
                )
            else:
                unchanged_count += 1

    return {
        "added": added,
        "removed": removed,
        "modified": modified,
        "unchanged_count": unchanged_count,
        "total_changed": len(added) + len(removed) + len(modified),
    }


def _build_side_by_side(left_lines: list[str], right_lines: list[str], context: int = 3) -> list[dict[str, Any]]:
    """Build side-by-side aligned rows with context around changed opcodes."""
    matcher = difflib.SequenceMatcher(None, left_lines, right_lines, autojunk=False)
    rows: list[dict[str, Any]] = []
    for group in matcher.get_grouped_opcodes(n=context):
        if rows:
            rows.append(
                {
                    "type": "gap",
                    "left_line": None,
                    "left_text": "⋯",
                    "right_line": None,
                    "right_text": "⋯",
                }
            )
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for offset in range(i2 - i1):
                    rows.append(
                        {
                            "type": "equal",
                            "left_line": i1 + offset + 1,
                            "left_text": left_lines[i1 + offset],
                            "right_line": j1 + offset + 1,
                            "right_text": right_lines[j1 + offset],
                        }
                    )
            elif tag == "delete":
                for idx in range(i1, i2):
                    rows.append(
                        {
                            "type": "delete",
                            "left_line": idx + 1,
                            "left_text": left_lines[idx],
                            "right_line": None,
                            "right_text": "",
                        }
                    )
            elif tag == "insert":
                for jdx in range(j1, j2):
                    rows.append(
                        {
                            "type": "insert",
                            "left_line": None,
                            "left_text": "",
                            "right_line": jdx + 1,
                            "right_text": right_lines[jdx],
                        }
                    )
            elif tag == "replace":
                span = max(i2 - i1, j2 - j1)
                for offset in range(span):
                    l_idx = i1 + offset if offset < (i2 - i1) else None
                    r_idx = j1 + offset if offset < (j2 - j1) else None
                    rows.append(
                        {
                            "type": "replace",
                            "left_line": (l_idx + 1) if l_idx is not None else None,
                            "left_text": left_lines[l_idx] if l_idx is not None else "",
                            "right_line": (r_idx + 1) if r_idx is not None else None,
                            "right_text": right_lines[r_idx] if r_idx is not None else "",
                        }
                    )
    return rows


def compute_file_diffs(
    before_src: Path,
    after_src: Path,
    *,
    before_label: str = "before",
    after_label: str = "after",
) -> list[dict[str, Any]]:
    if not before_src.is_dir() and not after_src.is_dir():
        return []

    def list_files(root: Path) -> set[str]:
        if not root.is_dir():
            return set()
        return {
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if p.is_file() and not p.name.startswith("._") and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts
        }

    all_files = sorted(list_files(before_src) | list_files(after_src))
    diffs: list[dict[str, Any]] = []

    for rel in all_files:
        b_path = before_src / rel
        a_path = after_src / rel
        b_exists = b_path.is_file()
        a_exists = a_path.is_file()
        left_text = b_path.read_text(encoding="utf-8", errors="replace") if b_exists else ""
        right_text = a_path.read_text(encoding="utf-8", errors="replace") if a_exists else ""
        if left_text == right_text and b_exists == a_exists:
            continue

        left_lines = left_text.splitlines()
        right_lines = right_text.splitlines()
        unified = "".join(
            difflib.unified_diff(
                left_text.splitlines(True),
                right_text.splitlines(True),
                fromfile=f"{before_label}/{rel}",
                tofile=f"{after_label}/{rel}",
            )
        )
        side_by_side = _build_side_by_side(left_lines, right_lines)
        added = sum(1 for r in side_by_side if r["type"] in {"insert", "replace"} and r["right_line"] is not None)
        removed = sum(1 for r in side_by_side if r["type"] in {"delete", "replace"} and r["left_line"] is not None)
        status = "added" if not b_exists else ("removed" if not a_exists else "modified")
        diffs.append(
            {
                "file": rel,
                "status": status,
                "added_lines": added,
                "removed_lines": removed,
                "unified": unified,
                "side_by_side": side_by_side,
            }
        )
    return diffs


def compare_security(before_scan: dict[str, Any], after_scan: dict[str, Any]) -> dict[str, Any]:
    b_findings = {f["fingerprint"]: f for f in (before_scan.get("findings") or [])}
    a_findings = {f["fingerprint"]: f for f in (after_scan.get("findings") or [])}

    introduced = [a_findings[fp] for fp in a_findings if fp not in b_findings]
    fixed = [b_findings[fp] for fp in b_findings if fp not in a_findings]
    unchanged = [a_findings[fp] for fp in a_findings if fp in b_findings]

    b_pkgs = {p["name"]: p["version"] for p in (before_scan.get("sbom_summary") or {}).get("packages") or []}
    a_pkgs = {p["name"]: p["version"] for p in (after_scan.get("sbom_summary") or {}).get("packages") or []}
    pkg_added = [{"name": k, "version": a_pkgs[k]} for k in sorted(a_pkgs.keys() - b_pkgs.keys())]
    pkg_removed = [{"name": k, "version": b_pkgs[k]} for k in sorted(b_pkgs.keys() - a_pkgs.keys())]
    pkg_updated = [
        {"name": k, "before": b_pkgs[k], "after": a_pkgs[k]}
        for k in sorted(b_pkgs.keys() & a_pkgs.keys())
        if b_pkgs[k] != a_pkgs[k]
    ]

    b_img = before_scan.get("image") or {}
    a_img = after_scan.get("image") or {}
    b_mb = float(b_img.get("size_mb") or 0.0)
    a_mb = float(a_img.get("size_mb") or 0.0)

    b_total = int((before_scan.get("counts") or {}).get("total") or 0)
    a_total = int((after_scan.get("counts") or {}).get("total") or 0)

    return {
        "before_grade": before_scan.get("grade") or "—",
        "after_grade": after_scan.get("grade") or "—",
        "before_score": int(before_scan.get("score") or 0),
        "after_score": int(after_scan.get("score") or 0),
        "score_delta": int(after_scan.get("score") or 0) - int(before_scan.get("score") or 0),
        "before_counts": before_scan.get("counts") or {},
        "after_counts": after_scan.get("counts") or {},
        "total_delta": a_total - b_total,
        "introduced": introduced,
        "fixed": fixed,
        "unchanged": unchanged,
        "by_tool": after_scan.get("by_tool") or {},
        "packages": {
            "before_count": len(b_pkgs),
            "after_count": len(a_pkgs),
            "added": pkg_added,
            "removed": pkg_removed,
            "updated": pkg_updated,
        },
        "image": {
            "before_name": b_img.get("name"),
            "after_name": a_img.get("name"),
            "before_mb": round(b_mb, 1),
            "after_mb": round(a_mb, 1),
            "delta_mb": round(a_mb - b_mb, 1),
            "before_layers": int(b_img.get("layers") or 0),
            "after_layers": int(a_img.get("layers") or 0),
        },
    }


def compare_releases(
    registry: Registry,
    app_id: str,
    before_version: str | None = None,
    after_version: str | None = None,
) -> dict[str, Any]:
    app = registry.get_app(app_id)
    if not app:
        raise KeyError(app_id)

    def_before, def_after = default_pair(registry, app_id)
    before_ver = (before_version or def_before or "").strip()
    after_ver = (after_version or def_after or "").strip()
    if not before_ver or not after_ver:
        return {
            "app_id": app_id,
            "available": False,
            "presets": [],
            "message": "Publish at least two versions to compare Before vs After.",
        }

    before_rel = registry.get_release(app_id, before_ver)
    after_rel = registry.get_release(app_id, after_ver)
    if not before_rel:
        raise FileNotFoundError(f"Release not found: {before_ver}")
    if not after_rel:
        raise FileNotFoundError(f"Release not found: {after_ver}")

    before_scan = _ensure_scan(registry, app_id, before_ver)
    after_scan = _ensure_scan(registry, app_id, after_ver)
    before_rel = registry.get_release(app_id, before_ver) or before_rel
    after_rel = registry.get_release(app_id, after_ver) or after_rel

    before_peaks = _peaks_for(registry, app_id, before_ver)
    after_peaks = _peaks_for(registry, app_id, after_ver)

    signals: list[dict[str, Any]] = []
    for name in FEATURE_NAMES:
        b_val = float(before_peaks.get(name) or 0.0)
        a_val = float(after_peaks.get(name) or 0.0)
        delta = round(a_val - b_val, 2)
        limit = float(LIMITS[name])
        if a_val > limit and b_val <= limit:
            status = "crossed"
        elif a_val > limit and b_val > limit:
            status = "still_crossed"
        elif b_val > limit and a_val <= limit:
            status = "recovered"
        elif delta > 0.05:
            status = "regressed"
        elif delta < -0.05:
            status = "improved"
        else:
            status = "unchanged"
        signals.append(
            {
                "signal": name,
                "label": SIGNAL_LABELS.get(name, name),
                "unit": UNITS[name],
                "limit": limit,
                "before": b_val,
                "after": a_val,
                "delta": delta,
                "status": status,
            }
        )

    b_paths = _path_stats_for(registry, app_id, before_ver)
    a_paths = _path_stats_for(registry, app_id, after_ver)
    all_paths = list(dict.fromkeys(list(app.get("probe_paths") or []) + list(b_paths.keys()) + list(a_paths.keys())))
    routes: list[dict[str, Any]] = []
    for path in all_paths:
        b_row = b_paths.get(path) or {}
        a_row = a_paths.get(path) or {}
        if not b_row and not a_row:
            continue
        b_p95 = round(float(b_row.get("p95_ms") or 0.0), 1)
        a_p95 = round(float(a_row.get("p95_ms") or 0.0), 1)
        b_err = round(float(b_row.get("error_pct") or 0.0), 1)
        a_err = round(float(a_row.get("error_pct") or 0.0), 1)
        d_p95 = round(a_p95 - b_p95, 1)
        d_err = round(a_err - b_err, 1)

        b_bad = b_p95 > LIMITS["latency_p95"] or b_err > LIMITS["error_rate"]
        a_bad = a_p95 > LIMITS["latency_p95"] or a_err > LIMITS["error_rate"]
        if a_bad and not b_bad:
            r_status = "crossed"
        elif b_bad and not a_bad:
            r_status = "recovered"
        elif a_bad and b_bad:
            r_status = "still_crossed"
        elif d_err > 0.1 or d_p95 > 15.0:
            r_status = "regressed"
        elif d_err < -0.1 or d_p95 < -15.0:
            r_status = "improved"
        else:
            r_status = "unchanged"
        routes.append(
            {
                "path": path,
                "before_p95_ms": b_p95,
                "after_p95_ms": a_p95,
                "delta_p95_ms": d_p95,
                "before_error_pct": b_err,
                "after_error_pct": a_err,
                "delta_error_pct": d_err,
                "status": r_status,
            }
        )

    before_src = registry.src_dir(app_id, before_ver)
    after_src = registry.src_dir(app_id, after_ver)
    has_source = registry.has_source(app_id, before_ver) and registry.has_source(app_id, after_ver)
    ast_changes = (
        compute_ast_changes(before_src, after_src)
        if has_source
        else {"added": [], "removed": [], "modified": [], "unchanged_count": 0, "total_changed": 0}
    )
    files = (
        compute_file_diffs(before_src, after_src, before_label=before_ver, after_label=after_ver)
        if has_source
        else []
    )
    security = compare_security(before_scan, after_scan)

    b_score = before_rel.get("score")
    a_score = after_rel.get("score")
    score_delta = (
        round(float(a_score) - float(b_score), 1)
        if b_score is not None and a_score is not None
        else None
    )

    return {
        "app_id": app_id,
        "available": True,
        "has_source": has_source,
        "presets": comparison_presets(registry, app_id),
        "before": {
            "version": before_ver,
            "decision": before_rel.get("decision") or before_rel.get("status") or "unverified",
            "score": b_score,
            "quarantined": bool(before_rel.get("quarantined")),
            "security_grade": before_scan.get("grade") or "—",
            "security_score": before_scan.get("score"),
            "published_at": before_rel.get("published_at"),
            "commit": before_rel.get("commit"),
            "kind": before_rel.get("kind"),
            "fixes_version": before_rel.get("fixes_version"),
        },
        "after": {
            "version": after_ver,
            "decision": after_rel.get("decision") or after_rel.get("status") or "unverified",
            "score": a_score,
            "quarantined": bool(after_rel.get("quarantined")),
            "security_grade": after_scan.get("grade") or "—",
            "security_score": after_scan.get("score"),
            "published_at": after_rel.get("published_at"),
            "commit": after_rel.get("commit"),
            "kind": after_rel.get("kind"),
            "fixes_version": after_rel.get("fixes_version"),
        },
        "score_delta": score_delta,
        "signals": signals,
        "routes": routes,
        "ast_changes": ast_changes,
        "files": files,
        "security": security,
    }
