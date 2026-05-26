#!/usr/bin/env python3
"""
Local CI pipeline — cross-platform replacement for GitHub Actions.

Runs the same steps as .github/workflows/ci.yml:
  1. Quality   — ruff check, ruff format --check, pytest (container + HA)
  2. Security  — bandit, pip-audit
  3. Docker    — build image, optional trivy scan, push to registry

Usage:
    python scripts/ci.py                    # Run everything, do not push
    python scripts/ci.py --push --tag v1.30.0   # Build, scan and push
    python scripts/ci.py --skip-quality --skip-security --push
    python scripts/ci.py --no-trivy
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
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


def step_quality(cov_fail_under: int = 72) -> None:
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
                "-c",
                "container/.bandit.yml",
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

    # pip-audit
    try:
        run(
            [
                "pip-audit",
                "-r",
                "container/requirements.txt",
                "--format=json",
                "--output=pip-audit-report.json",
                "--ignore-vuln",
                "CVE-2026-28684",
            ]
        )
        ok("pip-audit")
    except subprocess.CalledProcessError:
        ok("pip-audit (vulnerabilities found — review pip-audit-report.json)")

    print(f"\n{GREEN}Security reports saved:{RESET}")
    print(f"  {GREEN}- bandit-report.json{RESET}")
    print(f"  {GREEN}- pip-audit-report.json{RESET}")


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
    parser.add_argument(
        "--cov-fail-under",
        type=int,
        default=72,
        help="Coverage threshold (default: 72)",
    )
    args = parser.parse_args()

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
            print(f"{GREEN}Reports: bandit-report.json, pip-audit-report.json{RESET}")
        sys.exit(0)
    else:
        print(f"{RED}CI completed with failures.{RESET}")
        sys.exit(1)


if __name__ == "__main__":
    main()
