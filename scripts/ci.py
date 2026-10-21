#!/usr/bin/env python3
"""
Local CI pipeline — cross-platform replacement for GitHub Actions.

Runs the same steps as .github/workflows/ci.yml:
  1. Quality   — ruff check, ruff format --check, self-docs sync,
                 dependency lock sync, pytest (container + HA)
  2. Security  — bandit, pip-audit (on the hashed lock + pinned torch)
  3. Docker    — build image, optional trivy scan, push to registry

Usage:
    python scripts/ci.py                    # Run everything, do not push
    python scripts/ci.py --push --tag v1.30.0   # Build, scan and push
    python scripts/ci.py --skip-quality --skip-security --push
    python scripts/ci.py --no-trivy
    python scripts/ci.py --check-lock       # Only verify container/requirements.lock
    python scripts/ci.py --update-lock      # Regenerate container/requirements.lock
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Colors for terminal output
if platform.system() == "Windows":
    # Windows CMD/PowerShell may not support ANSI by default
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass

RESET = "\033[0m"
CYAN = "\033[36m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[90m"

CI_FAIL = 0

# Bandit checks skipped everywhere (CI, pre-commit, local-ci.ps1): B101 assert,
# B104 bind-all-interfaces, B105 hardcoded-password-string false positives,
# B311 non-crypto random, B608 SQL string building (parameterized elsewhere).
BANDIT_SKIPS = "B101,B104,B105,B311,B608"

# Dependency lock for the container image. torch is installed separately from
# the PyTorch CPU index (requirements-torch.txt) and only constrains the lock,
# so the lock carries torch's CPU runtime dependencies but not torch itself.
REQUIREMENTS = "container/requirements.txt"
TORCH_REQUIREMENTS = "container/requirements-torch.txt"
LOCK_FILE = "container/requirements.lock"
LOCK_PYTHON_VERSION = "3.12"  # keep in sync with the Dockerfile base image
# Image platforms (Debian bookworm, glibc 2.36). The lock is resolved for the
# first one; every platform must resolve to the identical lock. Hashes cover
# all published files of each pinned version, so one lock serves both. (uv's
# --universal mode is not used: it trusts one wheel's metadata, and
# sherpa-onnx's linux_armv7l wheel omits its sherpa-onnx-core dependency.)
LOCK_PLATFORMS = ("x86_64-manylinux_2_36", "aarch64-manylinux_2_36")
LOCK_COMPILE_COMMAND = "python scripts/ci.py --update-lock"
# CUDA-only dependencies of the PyPI torch wheel; the CPU wheel does not need them.
TORCH_GPU_PACKAGES = (
    "cuda-bindings",
    "cuda-toolkit",
    "nvidia-cudnn-cu13",
    "nvidia-cusparselt-cu13",
    "nvidia-nccl-cu13",
    "nvidia-nvshmem-cu13",
    "triton",
)
GPU_PACKAGE_PREFIXES = ("nvidia-", "cuda-", "triton")


def header(msg: str) -> None:
    print(f"\n{CYAN}{'=' * 50}{RESET}")
    print(f"{CYAN}{msg}{RESET}")
    print(f"{CYAN}{'=' * 50}{RESET}")


def ok(msg: str) -> None:
    print(f"{GREEN}OK: {msg}{RESET}")


def fail(msg: str) -> None:
    global CI_FAIL
    CI_FAIL = 1
    print(f"{RED}FAIL: {msg}{RESET}")


def warn(msg: str) -> None:
    print(f"{YELLOW}WARN: {msg}{RESET}")


def dim(msg: str) -> None:
    print(f"{DIM}{msg}{RESET}")


def run(
    cmd: list[str],
    *,
    cwd: str | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command and return the result."""
    cmd_str = " ".join(cmd)
    print(f"\n{YELLOW}>>> {cmd_str}{RESET}")
    merged_env = {**os.environ, **env} if env else None
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            check=check,
            capture_output=True,
            text=True,
            env=merged_env,
        )
        if result.stdout:
            print(result.stdout, end="")
        return result
    except subprocess.CalledProcessError as e:
        if e.stdout:
            print(e.stdout, end="")
        if e.stderr:
            print(e.stderr, end="", file=sys.stderr)
        raise


def which(tool: str) -> str | None:
    """Check if a command exists in PATH."""
    from shutil import which as shutil_which

    return shutil_which(tool)


def ensure_installed(tool: str) -> None:
    """Install a Python tool if missing."""
    if not which(tool):
        dim(f"Installing {tool}...")
        run([sys.executable, "-m", "pip", "install", tool], check=False)


def uv_command() -> list[str]:
    """Return the command prefix for uv.

    Prefers the uv pinned in requirements-dev.txt (installed as a module in
    this interpreter) so lock output does not drift with a global uv version.
    """
    import importlib.util

    if importlib.util.find_spec("uv") is not None:
        return [sys.executable, "-m", "uv"]
    exe = which("uv")
    if exe:
        warn("uv from requirements-dev.txt not installed; using uv from PATH")
        return [exe]
    return [sys.executable, "-m", "uv"]


def compile_lock(output: str, python_platform: str = LOCK_PLATFORMS[0]) -> None:
    """Resolve requirements.txt for ``python_platform`` into a hashed lock.

    An existing file at ``output`` acts as the preference set, so unchanged
    requirements keep their pins and the result only moves when requirements
    change (pip-tools semantics).
    """
    with tempfile.TemporaryDirectory() as tmp:
        excludes = Path(tmp) / "torch-gpu-excludes.txt"
        excludes.write_text("\n".join(TORCH_GPU_PACKAGES) + "\n")
        run(
            [
                *uv_command(),
                "pip",
                "compile",
                REQUIREMENTS,
                "--constraints",
                TORCH_REQUIREMENTS,
                "--excludes",
                str(excludes),
                "--no-emit-package",
                "torch",
                "--python-platform",
                python_platform,
                "--python-version",
                LOCK_PYTHON_VERSION,
                "--generate-hashes",
                "--custom-compile-command",
                LOCK_COMPILE_COMMAND,
                "--quiet",
                "--output-file",
                output,
            ]
        )


def gpu_packages_in(lock_text: str) -> list[str]:
    """Return lock entries that belong to the CUDA stack."""
    names = []
    for line in lock_text.splitlines():
        if line and not line[0].isspace() and not line.startswith(("#", "-")):
            name = line.split("==", 1)[0].strip().lower()
            if name.startswith(GPU_PACKAGE_PREFIXES):
                names.append(name)
    return names


def step_lock(update: bool = False) -> None:
    """Verify (or regenerate) container/requirements.lock."""
    base_image = f"FROM python:{LOCK_PYTHON_VERSION}-"
    if base_image not in Path("container/Dockerfile").read_text():
        fail(f"Dockerfile base image is not {base_image}* (update LOCK_PYTHON_VERSION)")
        sys.exit(1)

    if update:
        try:
            compile_lock(LOCK_FILE)
        except subprocess.CalledProcessError:
            fail("lock resolution failed")
            sys.exit(1)
        ok(f"{LOCK_FILE} regenerated")
    elif not Path(LOCK_FILE).exists():
        fail(f"{LOCK_FILE} missing (run: {LOCK_COMPILE_COMMAND})")
        sys.exit(1)

    # Re-resolve from the committed pins for every image platform; any
    # difference means requirements changed or the platforms diverge.
    for python_platform in LOCK_PLATFORMS:
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp) / "requirements.lock"
            shutil.copyfile(LOCK_FILE, candidate)
            try:
                compile_lock(str(candidate), python_platform)
            except subprocess.CalledProcessError:
                fail(f"lock resolution failed for {python_platform}")
                sys.exit(1)
            if candidate.read_text() != Path(LOCK_FILE).read_text():
                fail(
                    f"{LOCK_FILE} does not match the {python_platform} resolution "
                    f"(run: {LOCK_COMPILE_COMMAND}; if it persists, the platforms "
                    "need different pins)"
                )
                sys.exit(1)
    ok(f"dependency lock in sync ({', '.join(LOCK_PLATFORMS)})")

    gpu = gpu_packages_in(Path(LOCK_FILE).read_text())
    if gpu:
        fail(
            f"CUDA packages in {LOCK_FILE}: {', '.join(gpu)} (extend TORCH_GPU_PACKAGES)"
        )
        sys.exit(1)


def detect_image_name() -> str:
    """Detect image name from git remote origin."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
        remote = result.stdout.strip()
        if remote:
            # git@github.com:user/repo.git -> user/repo
            import re

            m = re.search(r"github\.com[/:]([^/]+/[^/]+?)(?:\.git)?$", remote)
            if m:
                return m.group(1).lower()
            m = re.search(r"[:/]([^/]+/[^/]+?)\.git$", remote)
            if m:
                return m.group(1).lower()
    except Exception:
        pass
    return "ha-agenthub"


def step_quality(cov_fail_under: int = 80) -> None:
    """Run quality checks."""
    header("STEP 1/4 — Quality")

    # ruff check
    try:
        run(["ruff", "check", "container/", "custom_components/"])
        ok("ruff check")
    except subprocess.CalledProcessError:
        fail("ruff check")
        sys.exit(1)

    # ruff format --check
    try:
        run(["ruff", "format", "--check", "container/", "custom_components/"])
        ok("ruff format")
    except subprocess.CalledProcessError:
        fail("ruff format")
        sys.exit(1)

    # bundled self-docs (container/app/knowledge/) match their docs/ sources
    try:
        run([sys.executable, "scripts/sync_self_docs.py", "--check"])
        ok("self-docs sync")
    except subprocess.CalledProcessError:
        fail("self-docs sync (run: python scripts/sync_self_docs.py)")
        sys.exit(1)

    # generated Lovelace card (custom_components/.../agenthub-activity-orb-card.js) matches its sources
    try:
        run([sys.executable, "scripts/sync_orb_card.py", "--check"])
        ok("orb card sync")
    except subprocess.CalledProcessError:
        fail("orb card sync (run: python scripts/sync_orb_card.py)")
        sys.exit(1)

    # container/requirements.lock matches requirements.txt + requirements-torch.txt
    step_lock()

    # mypy (non-blocking: report-only, never fails the build)
    try:
        result = run([sys.executable, "-m", "mypy", "container/app"], check=False)
        if result.returncode == 0:
            ok("mypy")
        else:
            warn("mypy (type issues found, non-blocking)")
    except Exception:
        warn("mypy not available or crashed")

    # pytest container
    try:
        run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/",
                "-n",
                "auto",
                "-q",
                "--tb=short",
                "--cov=app",
                "--cov-report=term-missing",
                "--cov-report=xml:coverage.xml",
                f"--cov-fail-under={cov_fail_under}",
            ],
            cwd="container",
        )
        ok("container pytest")
    except subprocess.CalledProcessError:
        fail("container pytest")
        sys.exit(1)

    # pytest HA
    try:
        run(
            [
                sys.executable,
                "-m",
                "pytest",
                "custom_components/tests/",
                "-v",
                "--tb=short",
                "-n",
                "auto",
            ]
        )
        ok("HA pytest")
    except subprocess.CalledProcessError:
        fail("HA pytest")
        sys.exit(1)


def step_security() -> None:
    """Run security checks."""
    header("STEP 2/4 — Security")

    ensure_installed("bandit")
    ensure_installed("pip-audit")

    # bandit
    try:
        run(
            [
                "bandit",
                "-r",
                "container/app",
                "-f",
                "json",
                "-o",
                "bandit-report.json",
                "-s",
                BANDIT_SKIPS,
            ]
        )
        ok("bandit")
    except subprocess.CalledProcessError:
        # bandit exits non-zero when issues found — check severity
        if Path("bandit-report.json").exists():
            try:
                with open("bandit-report.json") as f:
                    report = json.load(f)
                metrics = report.get("metrics", {})
                # metrics is a dict of file paths -> metric dicts
                high = sum(
                    m.get("SEVERITY.HIGH", 0)
                    for m in metrics.values()
                    if isinstance(m, dict)
                )
                if high > 0:
                    fail(f"bandit (HIGH severity issues found: {high})")
                    sys.exit(2)
            except Exception:
                pass
        ok("bandit (low/medium issues only)")

    # pip-audit on exactly what the image installs, without resolving or
    # installing anything: the hashed lock, then the separately pinned torch
    # (unhashed, so it cannot share a run with the hash-checked lock).
    audits = (
        (LOCK_FILE, ["--require-hashes"], "pip-audit-report.json"),
        (TORCH_REQUIREMENTS, ["--no-deps"], "pip-audit-torch-report.json"),
    )
    for requirements, mode, report in audits:
        try:
            run(
                [
                    "pip-audit",
                    "-r",
                    requirements,
                    *mode,
                    "--disable-pip",
                    "--format=json",
                    f"--output={report}",
                ]
            )
            ok(f"pip-audit ({requirements})")
        except subprocess.CalledProcessError:
            # pip-audit exits non-zero on known vulnerabilities (or audit
            # errors); either way the set is not proven clean: fail the gate.
            fail(f"pip-audit ({requirements}: vulnerabilities found — review {report})")
            sys.exit(2)

    print(f"\n{GREEN}Security reports saved:{RESET}")
    print(f"  {GREEN}- bandit-report.json{RESET}")
    print(f"  {GREEN}- pip-audit-report.json{RESET}")
    print(f"  {GREEN}- pip-audit-torch-report.json{RESET}")


def step_docker(
    image_name: str, tag: str, registry: str, no_trivy: bool, push: bool, no_cache: bool
) -> None:
    """Build, scan and optionally push Docker image."""
    header("STEP 3/4 — Docker Build")

    if not which("docker"):
        fail("docker is not installed or not in PATH")
        sys.exit(3)

    full_name = f"{registry}/{image_name}:{tag}"
    dim(f"Image: {full_name}")

    # Build
    build_cmd = [
        "docker",
        "compose",
        "-f",
        "container/docker-compose_local.yml",
        "build",
    ]
    if no_cache:
        build_cmd.append("--no-cache")
    try:
        run(build_cmd)
        ok("docker build")
    except subprocess.CalledProcessError:
        fail("docker build")
        sys.exit(3)

    # Tag if different from compose default
    if full_name != "ha-agenthub:latest":
        try:
            run(["docker", "tag", "ha-agenthub:latest", full_name])
        except subprocess.CalledProcessError:
            fail("docker tag")
            sys.exit(3)

    # Smoke test
    try:
        run(
            [
                "docker",
                "run",
                "--rm",
                "ha-agenthub:latest",
                "python",
                "-c",
                "from app.main import app; print('OK')",
            ]
        )
        ok("docker smoke test")
    except subprocess.CalledProcessError:
        fail("docker smoke test")
        sys.exit(3)

    # Trivy scan
    if not no_trivy:
        header("STEP 4/4 — Trivy Scan")
        if not which("trivy"):
            warn("trivy not found. Install from https://aquasecurity.github.io/trivy/")
            dim("Skipping Trivy scan.")
        else:
            try:
                run(
                    [
                        "trivy",
                        "image",
                        "--exit-code",
                        "1",
                        "--ignore-unfixed",
                        "--severity",
                        "HIGH,CRITICAL",
                        full_name,
                    ]
                )
                ok("trivy scan")
            except subprocess.CalledProcessError:
                fail("trivy scan (HIGH/CRITICAL vulnerabilities found)")
                print(f"{YELLOW}Review with: trivy image {full_name}{RESET}")
                sys.exit(3)
    else:
        dim("Skipping Trivy scan.")

    # Push
    if push:
        header(f"PUSH — {full_name}")
        try:
            run(["docker", "push", full_name])
            ok("docker push")
        except subprocess.CalledProcessError:
            fail("docker push")
            sys.exit(3)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Local CI pipeline — cross-platform replacement for GitHub Actions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/ci.py                          # Run everything, do not push
  python scripts/ci.py --push --tag v1.30.0     # Build, scan and push
  python scripts/ci.py --skip-quality --push    # Skip quality, build + push
  python scripts/ci.py --no-trivy               # Skip Trivy scan
  python scripts/ci.py --check-lock             # Verify container/requirements.lock
  python scripts/ci.py --update-lock            # Regenerate container/requirements.lock
        """,
    )
    parser.add_argument(
        "--skip-quality", action="store_true", help="Skip quality checks"
    )
    parser.add_argument(
        "--skip-security", action="store_true", help="Skip security checks"
    )
    parser.add_argument("--skip-docker", action="store_true", help="Skip docker build")
    parser.add_argument("--push", action="store_true", help="Push the built image")
    parser.add_argument(
        "--registry", default="ghcr.io", help="Container registry (default: ghcr.io)"
    )
    parser.add_argument(
        "--image-name",
        default="",
        help="Image name (default: auto-detected from git remote)",
    )
    parser.add_argument("--tag", default="latest", help="Image tag (default: latest)")
    parser.add_argument("--no-trivy", action="store_true", help="Skip Trivy image scan")
    parser.add_argument(
        "--no-cache", action="store_true", help="Build docker image without cache"
    )
    lock_group = parser.add_mutually_exclusive_group()
    lock_group.add_argument(
        "--check-lock",
        action="store_true",
        help=f"Only verify that {LOCK_FILE} is in sync, then exit",
    )
    lock_group.add_argument(
        "--update-lock",
        action="store_true",
        help=f"Only regenerate {LOCK_FILE} (needs uv), then exit",
    )
    parser.add_argument(
        "--cov-fail-under",
        type=int,
        default=80,
        help="Coverage threshold (default: 80)",
    )
    args = parser.parse_args()

    if args.check_lock or args.update_lock:
        step_lock(update=args.update_lock)
        sys.exit(CI_FAIL)

    image_name = args.image_name or detect_image_name()
    dim(f"Detected image name: {image_name}")

    start = time.time()

    if not args.skip_quality:
        step_quality(cov_fail_under=args.cov_fail_under)
    else:
        dim("Skipping quality checks.")

    if not args.skip_security:
        step_security()
    else:
        dim("Skipping security checks.")

    if not args.skip_docker:
        step_docker(
            image_name=image_name,
            tag=args.tag,
            registry=args.registry,
            no_trivy=args.no_trivy,
            push=args.push,
            no_cache=args.no_cache,
        )
    else:
        dim("Skipping docker build.")

    elapsed = time.time() - start
    header("CI SUMMARY")
    if CI_FAIL == 0:
        print(f"{GREEN}All requested steps completed successfully.{RESET}")
        print(f"{GREEN}Elapsed: {elapsed:.1f}s{RESET}")
        if not args.skip_docker:
            print(f"{GREEN}Image: {args.registry}/{image_name}:{args.tag}{RESET}")
        if not args.skip_security:
            print(
                f"{GREEN}Reports: bandit-report.json, pip-audit-report.json, "
                f"pip-audit-torch-report.json{RESET}"
            )
        sys.exit(0)
    else:
        print(f"{RED}CI completed with failures.{RESET}")
        sys.exit(1)


if __name__ == "__main__":
    main()
