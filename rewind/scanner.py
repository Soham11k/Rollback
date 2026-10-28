"""DevSecOps release scanner: Bandit SAST, pip-audit CVEs, Trivy image + SBOM, Dockerfile hardening."""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from registry import Registry

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
SEVERITY_DEDUCTION = {"critical": 30, "high": 15, "medium": 7, "low": 2}
_SECRET_NAME_RE = re.compile(r"(secret|password|passwd|token|api_key|apikey|private_key|auth_key)", re.IGNORECASE)
_REQ_LINE_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*(?:([=<>!~]+)\s*([A-Za-z0-9_.*+-]+))?")

# Offline fallback advisory database so CVE scanning works without internet access.
KNOWN_VULN_PACKAGES: list[dict[str, Any]] = [
    {
        "package": "pyyaml",
        "affected": lambda v: _ver_tuple(v) < (5, 4),
        "id": "CVE-2020-14343",
        "severity": "critical",
        "cwe": "CWE-20",
        "fix_version": "5.4",
        "summary": "Arbitrary code execution in PyYAML via full_load / load.",
    },
    {
        "package": "urllib3",
        "affected": lambda v: _ver_tuple(v) < (2, 0, 7),
        "id": "CVE-2023-45803",
        "severity": "medium",
        "cwe": "CWE-200",
        "fix_version": "2.0.7",
        "summary": "Request body not stripped after redirect from POST to GET.",
    },
    {
        "package": "requests",
        "affected": lambda v: _ver_tuple(v) < (2, 31, 0),
        "id": "CVE-2023-32681",
        "severity": "medium",
        "cwe": "CWE-200",
        "fix_version": "2.31.0",
        "summary": "Unintended leak of Proxy-Authorization header to destination server.",
    },
    {
        "package": "werkzeug",
        "affected": lambda v: _ver_tuple(v) < (3, 0, 1),
        "id": "CVE-2023-46136",
        "severity": "high",
        "cwe": "CWE-400",
        "fix_version": "3.0.1",
        "summary": "High resource consumption when parsing multipart form data.",
    },
    {
        "package": "jinja2",
        "affected": lambda v: _ver_tuple(v) < (3, 1, 3),
        "id": "CVE-2024-22195",
        "severity": "medium",
        "cwe": "CWE-79",
        "fix_version": "3.1.3",
        "summary": "HTML attribute injection via xmlattr filter.",
    },
]


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ver_tuple(version: str) -> tuple[int, ...]:
    nums = [int(x) for x in re.findall(r"\d+", version or "")]
    return tuple(nums) if nums else (0,)


def _fingerprint(tool: str, rule_id: str, file: str, symbol: str) -> str:
    return f"{tool}:{rule_id}:{file}:{symbol}"


def _enclosing_symbol(tree: ast.AST, lineno: int) -> str:
    best_name = "module"
    best_start = -1
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            end = getattr(node, "end_lineno", None) or node.lineno
            if node.lineno <= lineno <= end and node.lineno >= best_start:
                best_name = node.name
                best_start = node.lineno
    return best_name


def _active_symbols(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Return (active_functions, active_module_names) reachable from public functions or module top-level."""
    func_defs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    top_calls: set[str] = set()
    top_names: set[str] = set()

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_defs[node.name] = node
        else:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                    top_calls.add(sub.func.id)
                elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                    top_names.add(sub.id)

    active_funcs: set[str] = {
        name for name in func_defs if not name.startswith("_") or name in top_calls
    }
    queue = list(active_funcs)
    while queue:
        current = queue.pop()
        fn = func_defs.get(current)
        if not fn:
            continue
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                callee = sub.func.id
                if callee in func_defs and callee not in active_funcs:
                    active_funcs.add(callee)
                    queue.append(callee)

    private_refs: dict[str, set[str]] = {}
    active_refs: set[str] = set(top_names)
    for name, fn in func_defs.items():
        names = {
            sub.id
            for sub in ast.walk(fn)
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load)
        }
        if name in active_funcs:
            active_refs |= names
        else:
            for ref in names:
                private_refs.setdefault(ref, set()).add(name)

    return active_funcs, active_refs, private_refs


def _ast_sast_findings(src_dir: Path) -> list[dict[str, Any]]:
    """Deterministic Python AST SAST rules for secrets, weak crypto, and unbounded memory caches."""
    findings: list[dict[str, Any]] = []
    if not src_dir.is_dir():
        return findings

    for path in sorted(src_dir.rglob("*.py")):
        if path.name.startswith("._") or "__pycache__" in path.parts or "tests" in path.parts:
            continue
        rel = path.relative_to(src_dir).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(text, filename=rel)
        except (OSError, SyntaxError):
            continue
        lines = text.splitlines()
        active_funcs, active_refs, private_refs = _active_symbols(tree)

        # Track module-level list/dict collections to spot unbounded append in request paths.
        module_lists: dict[str, int] = {}
        for node in tree.body:
            target_name = None
            val = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                target_name = node.targets[0].id
                val = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target_name = node.target.id
                val = node.value
            if target_name and isinstance(val, ast.List) and len(val.elts) == 0:
                module_lists[target_name] = node.lineno

        # Check assignments for hardcoded secrets (strings or bytes).
        for node in ast.walk(tree):
            target_name = None
            val = None
            lineno = getattr(node, "lineno", 1)
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                target_name = node.targets[0].id
                val = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target_name = node.target.id
                val = node.value
            if not target_name or not _SECRET_NAME_RE.search(target_name):
                continue
            if isinstance(val, ast.Constant) and isinstance(val.value, (str, bytes)) and len(val.value) >= 4:
                enclosing = _enclosing_symbol(tree, lineno)
                if enclosing != "module" and enclosing not in active_funcs:
                    continue
                if enclosing == "module" and target_name not in active_refs and target_name in private_refs:
                    # Only referenced by dead private helper(s).
                    continue
                code_line = lines[lineno - 1].strip() if 1 <= lineno <= len(lines) else target_name
                findings.append(
                    {
                        "fingerprint": _fingerprint("bandit", "B105", rel, target_name),
                        "tool": "bandit",
                        "rule_id": "B105",
                        "severity": "high",
                        "confidence": "high",
                        "cwe": "CWE-798",
                        "file": rel,
                        "line": lineno,
                        "symbol": target_name,
                        "title": f"Hardcoded secret in {target_name}",
                        "message": f"Hardcoded credential or signing secret assigned to `{target_name}`.",
                        "code": code_line,
                        "remediation": "Read the secret from an environment variable (`os.environ.get`) or remove the unused secret.",
                    }
                )

        # Check calls for weak hashes (sha1/md5), shell=True, eval/exec, and unbounded global list growth.
        cleared_lists: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                if node.func.attr in {"pop", "clear", "remove"}:
                    cleared_lists.add(node.func.value.id)
            elif isinstance(node, ast.Delete):
                for target in node.targets:
                    if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
                        cleared_lists.add(target.value.id)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            lineno = getattr(node, "lineno", 1)
            enclosing = _enclosing_symbol(tree, lineno)
            if enclosing != "module" and enclosing not in active_funcs:
                continue
            code_line = lines[lineno - 1].strip() if 1 <= lineno <= len(lines) else ""

            # hashlib.sha1 / hashlib.md5
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "hashlib"
                and node.func.attr in {"sha1", "md5"}
            ):
                algo = node.func.attr.upper()
                findings.append(
                    {
                        "fingerprint": _fingerprint("bandit", "B324", rel, f"{enclosing}:{algo}"),
                        "tool": "bandit",
                        "rule_id": "B324",
                        "severity": "medium",
                        "confidence": "high",
                        "cwe": "CWE-327",
                        "file": rel,
                        "line": lineno,
                        "symbol": enclosing,
                        "title": f"Use of weak {algo} hash in {enclosing}()",
                        "message": f"`hashlib.{node.func.attr}` is a weak cryptographic hash.",
                        "code": code_line,
                        "remediation": "Use `hashlib.sha256` for collision resistance.",
                    }
                )

            # Unbounded module-level list .append() inside an active function
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.attr in {"append", "extend"}
                and node.func.value.id in module_lists
                and node.func.value.id not in cleared_lists
                and enclosing != "module"
            ):
                list_name = node.func.value.id
                findings.append(
                    {
                        "fingerprint": _fingerprint("bandit", "B400", rel, f"{enclosing}:{list_name}"),
                        "tool": "bandit",
                        "rule_id": "B400",
                        "severity": "medium",
                        "confidence": "high",
                        "cwe": "CWE-400",
                        "file": rel,
                        "line": lineno,
                        "symbol": list_name,
                        "title": f"Unbounded in-memory growth on {list_name}",
                        "message": f"`{list_name}.{node.func.attr}(...)` appends on every call in `{enclosing}()` without bounding or eviction.",
                        "code": code_line,
                        "remediation": f"Remove `{list_name}.{node.func.attr}(...)` or bound `{list_name}` with a `collections.deque(maxlen=...)`.",
                    }
                )

    return findings


def run_bandit_sast(src_dir: Path) -> tuple[list[dict[str, Any]], str]:
    """Run Bandit CLI (if available) plus AST SAST checks, deduplicating by (rule_id, file, line)."""
    findings = list(_ast_sast_findings(src_dir))
    seen = {(f["rule_id"], f["file"], f["line"]) for f in findings}
    seen_fp = {f["fingerprint"] for f in findings}
    status = "ast"

    if src_dir.is_dir():
        cmd = None
        if shutil.which("bandit"):
            cmd = ["bandit", "-r", str(src_dir), "-f", "json", "-q", "-x", f"{src_dir}/tests"]
        else:
            cmd = [sys.executable, "-m", "bandit", "-r", str(src_dir), "-f", "json", "-q", "-x", f"{src_dir}/tests"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
            if proc.stdout and proc.stdout.lstrip().startswith("{"):
                payload = json.loads(proc.stdout)
                status = "ok"
                for item in payload.get("results") or []:
                    rule_id = str(item.get("test_id") or "B000")
                    # Skip B104 (0.0.0.0 bind inside container is expected for container apps)
                    if rule_id == "B104":
                        continue
                    raw_file = str(item.get("filename") or "")
                    try:
                        rel = Path(raw_file).resolve().relative_to(src_dir.resolve()).as_posix()
                    except ValueError:
                        rel = Path(raw_file).name
                    lineno = int(item.get("line_number") or 1)
                    if (rule_id, rel, lineno) in seen:
                        continue
                    cwe_obj = item.get("issue_cwe") or {}
                    cwe = f"CWE-{cwe_obj.get('id')}" if cwe_obj.get("id") else "CWE-703"
                    sev = str(item.get("issue_severity") or "low").lower()
                    if sev not in SEVERITY_ORDER:
                        sev = "low"
                    conf = str(item.get("issue_confidence") or "medium").lower()
                    symbol = f"line_{lineno}"
                    try:
                        tree = ast.parse((src_dir / rel).read_text(encoding="utf-8", errors="replace"))
                        symbol = _enclosing_symbol(tree, lineno)
                    except Exception:  # noqa: BLE001
                        pass
                    fp = _fingerprint("bandit", rule_id, rel, symbol)
                    if fp in seen_fp:
                        continue
                    seen.add((rule_id, rel, lineno))
                    seen_fp.add(fp)
                    findings.append(
                        {
                            "fingerprint": fp,
                            "tool": "bandit",
                            "rule_id": rule_id,
                            "severity": sev,
                            "confidence": conf,
                            "cwe": cwe,
                            "file": rel,
                            "line": lineno,
                            "symbol": symbol,
                            "title": f"{rule_id}: {item.get('test_name') or 'SAST issue'}",
                            "message": str(item.get("issue_text") or "").strip(),
                            "code": str(item.get("code") or "").strip().splitlines()[0] if item.get("code") else "",
                            "remediation": "Review and remediate the flagged construct.",
                        }
                    )
        except Exception:  # noqa: BLE001
            pass

    return findings, status


def parse_requirements(src_dir: Path) -> list[dict[str, Any]]:
    req_path = src_dir / "requirements.txt"
    if not req_path.is_file():
        return []
    packages: list[dict[str, Any]] = []
    for idx, raw in enumerate(req_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _REQ_LINE_RE.match(line)
        if not match:
            continue
        name, op, ver = match.group(1).lower(), match.group(2) or "", match.group(3) or ""
        packages.append(
            {
                "name": name,
                "operator": op,
                "version": ver or "unpinned",
                "pinned": op == "==" and bool(ver),
                "line": idx,
                "raw": line,
                "type": "pypi",
            }
        )
    return packages


def run_pip_audit(src_dir: Path, packages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """Check requirements.txt against pip-audit CLI (if reachable) + built-in advisory database."""
    findings: list[dict[str, Any]] = []
    seen_fp: set[str] = set()
    req_path = src_dir / "requirements.txt"
    status = "ok" if req_path.is_file() else "no_requirements"
    if not req_path.is_file():
        return findings, status

    # 1. Built-in advisory database + pinning hygiene.
    for pkg in packages:
        name = pkg["name"]
        ver = pkg["version"]
        lineno = pkg["line"]
        if not pkg["pinned"]:
            fp = _fingerprint("pip_audit", "DEP-UNPINNED", "requirements.txt", name)
            seen_fp.add(fp)
            findings.append(
                {
                    "fingerprint": fp,
                    "tool": "pip_audit",
                    "rule_id": "DEP-UNPINNED",
                    "severity": "low",
                    "confidence": "high",
                    "cwe": "CWE-1104",
                    "file": "requirements.txt",
                    "line": lineno,
                    "symbol": name,
                    "title": f"Unpinned dependency `{name}`",
                    "message": f"`{pkg['raw']}` is not pinned with `==`, risking non-repeatable builds.",
                    "code": pkg["raw"],
                    "remediation": f"Pin `{name}==<version>` in requirements.txt.",
                }
            )
            continue
        for adv in KNOWN_VULN_PACKAGES:
            if adv["package"] == name and adv["affected"](ver):
                fp = _fingerprint("pip_audit", adv["id"], "requirements.txt", name)
                seen_fp.add(fp)
                findings.append(
                    {
                        "fingerprint": fp,
                        "tool": "pip_audit",
                        "rule_id": adv["id"],
                        "severity": adv["severity"],
                        "confidence": "high",
                        "cwe": adv["cwe"],
                        "file": "requirements.txt",
                        "line": lineno,
                        "symbol": f"{name}=={ver}",
                        "title": f"{adv['id']} in {name}=={ver}",
                        "message": adv["summary"],
                        "code": pkg["raw"],
                        "remediation": f"Upgrade `{name}` to `>={adv['fix_version']}` in requirements.txt.",
                    }
                )

    # 2. Optional live pip-audit network pass when REWIND_LIVE_PIP_AUDIT=1 is enabled.
    if os.environ.get("REWIND_LIVE_PIP_AUDIT") == "1" and shutil.which("pip-audit"):
        try:
            proc = subprocess.run(
                ["pip-audit", "-r", str(req_path), "-f", "json", "--progress-spinner", "off"],
                capture_output=True,
                text=True,
                timeout=12,
                check=False,
            )
            if proc.stdout and proc.stdout.lstrip().startswith("{"):
                data = json.loads(proc.stdout)
                for dep in data.get("dependencies") or []:
                    pkg_name = str(dep.get("name") or "").lower()
                    pkg_ver = str(dep.get("version") or "")
                    for vuln in (dep.get("vulns") or [])[:1]:
                        vuln_id = str(vuln.get("id") or "PYSEC")
                        fp = _fingerprint("pip_audit", vuln_id, "requirements.txt", pkg_name)
                        if fp in seen_fp:
                            continue
                        seen_fp.add(fp)
                        fix_versions = vuln.get("fix_versions") or []
                        fix_hint = f"Upgrade `{pkg_name}` to {fix_versions[0]}." if fix_versions else f"Upgrade `{pkg_name}`."
                        findings.append(
                            {
                                "fingerprint": fp,
                                "tool": "pip_audit",
                                "rule_id": vuln_id,
                                "severity": "low",
                                "confidence": "high",
                                "cwe": "CWE-1395",
                                "file": "requirements.txt",
                                "line": 1,
                                "symbol": f"{pkg_name}=={pkg_ver}",
                                "title": f"{vuln_id} in {pkg_name}=={pkg_ver}",
                                "message": str(vuln.get("description") or vuln_id)[:240],
                                "code": f"{pkg_name}=={pkg_ver}",
                                "remediation": fix_hint,
                            }
                        )
        except Exception:  # noqa: BLE001
            pass

    return findings, status


def audit_dockerfile(src_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Static hardening check on Dockerfile."""
    dockerfile = src_dir / "Dockerfile"
    meta = {"base_image": None, "user": "root", "has_healthcheck": False}
    findings: list[dict[str, Any]] = []
    if not dockerfile.is_file():
        return findings, meta

    lines = dockerfile.read_text(encoding="utf-8", errors="replace").splitlines()
    user_line = None
    for idx, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        upper = stripped.upper()
        if upper.startswith("FROM "):
            base = stripped.split()[1] if len(stripped.split()) > 1 else ""
            meta["base_image"] = base
            if ":" not in base or base.endswith(":latest"):
                findings.append(
                    {
                        "fingerprint": _fingerprint("dockerfile", "DKR-002", "Dockerfile", "FROM"),
                        "tool": "dockerfile",
                        "rule_id": "DKR-002",
                        "severity": "low",
                        "confidence": "high",
                        "cwe": "CWE-1104",
                        "file": "Dockerfile",
                        "line": idx,
                        "symbol": "FROM",
                        "title": "Unpinned base image tag in Dockerfile",
                        "message": f"Base image `{base}` uses `:latest` or omits a version tag.",
                        "code": stripped,
                        "remediation": "Pin the base image to an explicit version tag (e.g. `python:3.11-slim`).",
                    }
                )
        elif upper.startswith("USER "):
            user_val = stripped.split(None, 1)[1].strip() if len(stripped.split()) > 1 else "root"
            meta["user"] = user_val
            user_line = idx
        elif upper.startswith("HEALTHCHECK "):
            meta["has_healthcheck"] = True
        elif upper.startswith(("ENV ", "ARG ")) and _SECRET_NAME_RE.search(stripped):
            findings.append(
                {
                    "fingerprint": _fingerprint("dockerfile", "DKR-004", "Dockerfile", f"ENV:{idx}"),
                    "tool": "dockerfile",
                    "rule_id": "DKR-004",
                    "severity": "high",
                    "confidence": "medium",
                    "cwe": "CWE-798",
                    "file": "Dockerfile",
                    "line": idx,
                    "symbol": "ENV",
                    "title": "Secret-like variable baked into Dockerfile layer",
                    "message": "Secrets in ENV/ARG persist in image history.",
                    "code": stripped,
                    "remediation": "Pass secrets at runtime via environment variables or secret mounts.",
                }
            )

    if meta["user"] in {"root", "0", ""}:
        findings.append(
            {
                "fingerprint": _fingerprint("dockerfile", "DKR-001", "Dockerfile", "USER"),
                "tool": "dockerfile",
                "rule_id": "DKR-001",
                "severity": "low",
                "confidence": "high",
                "cwe": "CWE-250",
                "file": "Dockerfile",
                "line": user_line or 1,
                "symbol": "USER",
                "title": "Container runs as root user",
                "message": "Dockerfile does not switch to a non-root `USER` before `CMD`.",
                "code": lines[(user_line or 1) - 1].strip() if lines else "FROM ...",
                "remediation": "Add a non-root user (`RUN useradd -r app && USER app`) before `CMD`.",
            }
        )

    if not meta["has_healthcheck"]:
        findings.append(
            {
                "fingerprint": _fingerprint("dockerfile", "DKR-003", "Dockerfile", "HEALTHCHECK"),
                "tool": "dockerfile",
                "rule_id": "DKR-003",
                "severity": "low",
                "confidence": "high",
                "cwe": "CWE-693",
                "file": "Dockerfile",
                "line": len(lines) or 1,
                "symbol": "HEALTHCHECK",
                "title": "Missing container HEALTHCHECK instruction",
                "message": "Dockerfile does not declare a `HEALTHCHECK`.",
                "code": lines[-1].strip() if lines else "",
                "remediation": "Add a `HEALTHCHECK` instruction so orchestrators detect hung workers.",
            }
        )

    return findings, meta


def inspect_image_and_trivy(
    image_name: str | None,
    dockerfile_meta: dict[str, Any],
    packages: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], str]:
    """Inspect Docker image metadata, run Trivy image scan if available, and build CycloneDX SBOM."""
    image_info = {
        "name": image_name or "none",
        "size_bytes": 0,
        "size_mb": 0.0,
        "layers": 0,
        "base_image": dockerfile_meta.get("base_image"),
        "user": dockerfile_meta.get("user") or "root",
        "has_healthcheck": bool(dockerfile_meta.get("has_healthcheck")),
    }
    findings: list[dict[str, Any]] = []
    trivy_status = "skipped"

    if image_name and shutil.which("docker"):
        try:
            proc = subprocess.run(
                ["docker", "image", "inspect", image_name],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                arr = json.loads(proc.stdout)
                if arr:
                    info = arr[0]
                    size_bytes = int(info.get("Size") or 0)
                    layers = len((info.get("RootFS") or {}).get("Layers") or [])
                    cfg = info.get("Config") or {}
                    image_info.update(
                        {
                            "size_bytes": size_bytes,
                            "size_mb": round(size_bytes / (1024 * 1024), 1),
                            "layers": layers,
                            "user": cfg.get("User") or image_info["user"],
                            "has_healthcheck": bool(cfg.get("Healthcheck")) or image_info["has_healthcheck"],
                        }
                    )
                    trivy_status = "inspected"
        except Exception:  # noqa: BLE001
            pass

    if image_name and shutil.which("trivy") and image_info["size_bytes"] > 0:
        try:
            proc = subprocess.run(
                [
                    "trivy",
                    "image",
                    "--scanners",
                    "vuln",
                    "--severity",
                    "HIGH,CRITICAL",
                    "--ignore-unfixed",
                    "--format",
                    "json",
                    "--quiet",
                    "--timeout",
                    "15s",
                    image_name,
                ],
                capture_output=True,
                text=True,
                timeout=18,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout and proc.stdout.lstrip().startswith("{"):
                trivy_status = "ok"
                data = json.loads(proc.stdout)
                for res in data.get("Results") or []:
                    target = str(res.get("Target") or image_name)
                    # Focus on application-level / python packages so base Debian noise doesn't drown app findings
                    if res.get("Class") == "os-pkgs":
                        continue
                    for vuln in (res.get("Vulnerabilities") or [])[:10]:
                        vid = str(vuln.get("VulnerabilityID") or "CVE")
                        pkg = str(vuln.get("PkgName") or "pkg")
                        installed = str(vuln.get("InstalledVersion") or "")
                        fixed = str(vuln.get("FixedVersion") or "")
                        sev = str(vuln.get("Severity") or "high").lower()
                        if sev not in SEVERITY_ORDER:
                            sev = "high"
                        fp = _fingerprint("trivy", vid, target, pkg)
                        findings.append(
                            {
                                "fingerprint": fp,
                                "tool": "trivy",
                                "rule_id": vid,
                                "severity": sev,
                                "confidence": "high",
                                "cwe": "CWE-1395",
                                "file": target,
                                "line": 1,
                                "symbol": f"{pkg}@{installed}",
                                "title": f"{vid} in {pkg} ({installed})",
                                "message": str(vuln.get("Title") or vuln.get("Description") or vid)[:220],
                                "code": f"{pkg}=={installed}",
                                "remediation": f"Upgrade `{pkg}` to `{fixed}`." if fixed else f"Upgrade `{pkg}`.",
                            }
                        )
        except Exception:  # noqa: BLE001
            pass

    sbom_components = [
        {
            "type": "library",
            "name": pkg["name"],
            "version": pkg["version"],
            "purl": f"pkg:pypi/{pkg['name']}@{pkg['version']}",
        }
        for pkg in packages
    ]
    if image_info.get("base_image"):
        sbom_components.insert(
            0,
            {
                "type": "container",
                "name": image_info["base_image"],
                "version": image_info["base_image"].split(":")[1] if ":" in str(image_info["base_image"]) else "latest",
                "purl": f"pkg:docker/{image_info['base_image']}",
            },
        )
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "timestamp": _ts(),
            "component": {"type": "application", "name": image_name or "app"},
        },
        "components": sbom_components,
    }
    return image_info, findings, sbom, trivy_status


def compute_grade(findings: list[dict[str, Any]]) -> tuple[str, int, dict[str, int]]:
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "total": 0}
    deduction = 0
    for item in findings:
        sev = str(item.get("severity") or "low").lower()
        if sev not in counts:
            sev = "low"
        counts[sev] += 1
        counts["total"] += 1
        deduction += SEVERITY_DEDUCTION.get(sev, 2)

    score = max(0, min(100, 100 - deduction))
    if counts["critical"] > 0 or score < 45:
        grade = "F" if counts["critical"] > 0 and score < 55 else "D"
    elif counts["high"] > 0:
        grade = "C" if score >= 60 else "D"
    elif score >= 90:
        grade = "A"
    elif score >= 78:
        grade = "B"
    elif score >= 60:
        grade = "C"
    else:
        grade = "D"
    return grade, score, counts


def scan_release(registry: Registry, app_id: str, version: str) -> dict[str, Any]:
    """Run Bandit SAST, pip-audit, Dockerfile checks, and Trivy/SBOM on a release and persist scan.json."""
    started = time.time()
    release = registry.get_release(app_id, version)
    if not release:
        raise KeyError(f"{app_id}/{version}")

    src_dir = registry.src_dir(app_id, version)
    image_name = release.get("image")

    bandit_findings, bandit_status = run_bandit_sast(src_dir)
    packages = parse_requirements(src_dir)
    pip_findings, pip_status = run_pip_audit(src_dir, packages)
    docker_findings, docker_meta = audit_dockerfile(src_dir)
    image_info, trivy_findings, sbom, trivy_status = inspect_image_and_trivy(
        image_name, docker_meta, packages
    )

    all_findings = bandit_findings + pip_findings + trivy_findings + docker_findings
    all_findings.sort(
        key=lambda f: (
            SEVERITY_ORDER.get(str(f.get("severity") or "low"), 9),
            str(f.get("file") or ""),
            int(f.get("line") or 0),
        )
    )

    grade, score, counts = compute_grade(all_findings)
    payload = {
        "app_id": app_id,
        "version": version,
        "scanned_at": _ts(),
        "duration_s": round(time.time() - started, 2),
        "grade": grade,
        "score": score,
        "counts": counts,
        "by_tool": {
            "bandit": {
                "label": "Bandit SAST",
                "status": bandit_status,
                "count": len(bandit_findings),
            },
            "pip_audit": {
                "label": "pip-audit CVEs",
                "status": pip_status,
                "count": len(pip_findings),
            },
            "trivy": {
                "label": "Trivy Image + SBOM",
                "status": trivy_status,
                "count": len(trivy_findings),
            },
            "dockerfile": {
                "label": "Dockerfile Hardening",
                "status": "ok" if (src_dir / "Dockerfile").is_file() else "no_dockerfile",
                "count": len(docker_findings),
            },
        },
        "image": image_info,
        "sbom_summary": {
            "format": "CycloneDX 1.5",
            "package_count": len(packages),
            "packages": [
                {"name": p["name"], "version": p["version"], "pinned": p["pinned"], "type": p["type"]}
                for p in packages
            ],
        },
        "findings": all_findings,
    }

    registry.write_scan(app_id, version, payload, sbom=sbom)
    registry.update_release(
        app_id,
        version,
        security_grade=grade,
        security_score=score,
        security_findings=counts["total"],
        security_counts=counts,
    )
    return payload
