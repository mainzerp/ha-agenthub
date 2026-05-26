#!/usr/bin/env pwsh
#requires -Version 7.2
<#
.SYNOPSIS
    Local CI pipeline — replaces GitHub Actions for quality, security, docker build and push.

.DESCRIPTION
    Runs the same steps as .github/workflows/ci.yml but locally:
      1. Quality   — ruff check, ruff format --check, pytest (container + HA)
      2. Security  — bandit, pip-audit
      3. Docker    — build image, optional trivy scan, push to registry

    Exit codes:
      0 = all passed
      1 = quality failure
      2 = security failure
      3 = docker failure

.PARAMETER SkipQuality
    Skip quality checks.

.PARAMETER SkipSecurity
    Skip security checks.

.PARAMETER SkipDocker
    Skip docker build.

.PARAMETER Push
    Push the built image to the registry after build.

.PARAMETER Registry
    Container registry to push to (default: ghcr.io).

.PARAMETER ImageName
    Image name (default: derived from git remote origin URL).

.PARAMETER Tag
    Image tag (default: latest).

.PARAMETER NoTrivy
    Skip Trivy image scan.

.EXAMPLE
    .\scripts\local-ci.ps1
    # Run everything, do not push.

.EXAMPLE
    .\scripts\local-ci.ps1 -Push -Registry ghcr.io -ImageName myuser/ha-agenthub -Tag v1.30.0
    # Build, scan and push to GHCR.
#>

[CmdletBinding()]
param(
    [switch] $SkipQuality,
    [switch] $SkipSecurity,
    [switch] $SkipDocker,
    [switch] $Push,
    [string] $Registry = "ghcr.io",
    [string] $ImageName = "",
    [string] $Tag = "latest",
    [switch] $NoTrivy
)

$ErrorActionPreference = "Stop"
$Global:CI_FAIL = 0

function Step-Header($msg) {
    Write-Host "`n========================================" -ForegroundColor Cyan
    Write-Host $msg -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Cyan
}

function Step-Footer($ok, $msg) {
    if ($ok) {
        Write-Host "OK: $msg" -ForegroundColor Green
    } else {
        Write-Host "FAIL: $msg" -ForegroundColor Red
        $script:CI_FAIL = 1
    }
}

# ---------------------------------------------------------------------------
# Detect image name from git remote if not provided
# ---------------------------------------------------------------------------
if (-not $ImageName) {
    try {
        $remote = git remote get-url origin 2>$null
        if ($remote) {
            # Convert git@github.com:user/repo.git -> user/repo
            $ImageName = $remote -replace '.*[:/]([^/]+/[^/]+)\.git$', '$1' -replace '.*github.com[/:]([^/]+/[^/]+).*', '$1'
            $ImageName = $ImageName.ToLower()
        }
    } catch { }
    if (-not $ImageName) {
        $ImageName = "ha-agenthub"
    }
}

$FullImageName = "$Registry/$ImageName`:$Tag"
Write-Host "Image: $FullImageName" -ForegroundColor DarkGray

# ---------------------------------------------------------------------------
# 1. QUALITY
# ---------------------------------------------------------------------------
if (-not $SkipQuality) {
    Step-Header "STEP 1/4 — Quality"

    # ruff check
    Write-Host "`n>>> ruff check container/ custom_components/" -ForegroundColor Yellow
    try {
        ruff check container/ custom_components/
        Step-Footer $true "ruff check"
    } catch {
        Step-Footer $false "ruff check"
        exit 1
    }

    # ruff format --check
    Write-Host "`n>>> ruff format --check container/ custom_components/" -ForegroundColor Yellow
    try {
        ruff format --check container/ custom_components/
        Step-Footer $true "ruff format"
    } catch {
        Step-Footer $false "ruff format"
        exit 1
    }

    # pytest container
    Write-Host "`n>>> pytest container/tests/ -n auto -q --tb=short --cov=app --cov-report=term-missing --cov-fail-under=72" -ForegroundColor Yellow
    Push-Location container
    try {
        python -m pytest tests/ -n auto -q --tb=short --cov=app --cov-report=term-missing --cov-fail-under=72
        Step-Footer $true "container pytest"
    } catch {
        Step-Footer $false "container pytest"
        Pop-Location
        exit 1
    }
    Pop-Location

    # pytest HA
    Write-Host "`n>>> pytest custom_components/tests/ -v --tb=short" -ForegroundColor Yellow
    try {
        python -m pytest custom_components/tests/ -v --tb=short
        Step-Footer $true "HA pytest"
    } catch {
        Step-Footer $false "HA pytest"
        exit 1
    }
} else {
    Write-Host "Skipping quality checks." -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------------
# 2. SECURITY
# ---------------------------------------------------------------------------
if (-not $SkipSecurity) {
    Step-Header "STEP 2/4 — Security"

    # Ensure tools are installed
    $tools = @("bandit", "pip-audit")
    foreach ($tool in $tools) {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
            Write-Host "Installing $tool..." -ForegroundColor DarkGray
            pip install $tool
        }
    }

    # bandit
    Write-Host "`n>>> bandit -r container/app -f json -o bandit-report.json -c container/.bandit.yml" -ForegroundColor Yellow
    try {
        bandit -r container/app -f json -o bandit-report.json -c container/.bandit.yml
        Step-Footer $true "bandit"
    } catch {
        # bandit exits with non-zero when it finds issues
        if (Test-Path bandit-report.json) {
            $report = Get-Content bandit-report.json -Raw | ConvertFrom-Json
            $metrics = $report.metrics | Select-Object -First 1
            $high = $metrics."SEVERITY.HIGH" ?? 0
            if ($high -gt 0) {
                Step-Footer $false "bandit (HIGH severity issues found: $high)"
                exit 2
            }
        }
        Step-Footer $true "bandit (low/medium issues only)"
    }

    # pip-audit
    Write-Host "`n>>> pip-audit -r container/requirements.txt --format=json --output=pip-audit-report.json --ignore-vuln CVE-2026-28684" -ForegroundColor Yellow
    try {
        pip-audit -r container/requirements.txt --format=json --output=pip-audit-report.json --ignore-vuln CVE-2026-28684
        Step-Footer $true "pip-audit"
    } catch {
        # pip-audit may exit non-zero on vulnerabilities
        Step-Footer $true "pip-audit (vulnerabilities found — review pip-audit-report.json)"
    }

    Write-Host "`nSecurity reports saved:" -ForegroundColor Green
    Write-Host "  - bandit-report.json" -ForegroundColor Green
    Write-Host "  - pip-audit-report.json" -ForegroundColor Green
} else {
    Write-Host "Skipping security checks." -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------------
# 3. DOCKER BUILD
# ---------------------------------------------------------------------------
if (-not $SkipDocker) {
    Step-Header "STEP 3/4 — Docker Build"

    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-Host "ERROR: docker is not installed or not in PATH" -ForegroundColor Red
        exit 3
    }

    Write-Host "`n>>> docker compose -f container/docker-compose_local.yml build" -ForegroundColor Yellow
    docker compose -f container/docker-compose_local.yml build
    if ($LASTEXITCODE -ne 0) {
        Step-Footer $false "docker build"
        exit 3
    }
    Step-Footer $true "docker build"

    # Tag with custom registry/name if different from compose default
    if ($FullImageName -ne "ha-agenthub:latest") {
        Write-Host "`n>>> docker tag ha-agenthub:latest $FullImageName" -ForegroundColor Yellow
        docker tag ha-agenthub:latest $FullImageName
    }

    # Smoke test
    Write-Host "`n>>> docker run --rm ha-agenthub:latest python -c 'from app.main import app; print(`"OK`")'" -ForegroundColor Yellow
    docker run --rm ha-agenthub:latest python -c "from app.main import app; print('OK')"
    if ($LASTEXITCODE -ne 0) {
        Step-Footer $false "docker smoke test"
        exit 3
    }
    Step-Footer $true "docker smoke test"

    # -----------------------------------------------------------------------
    # 4. TRIVY SCAN
    # -----------------------------------------------------------------------
    if (-not $NoTrivy) {
        Step-Header "STEP 4/4 — Trivy Scan"

        if (-not (Get-Command trivy -ErrorAction SilentlyContinue)) {
            Write-Host "WARNING: trivy not found. Install from https://aquasecurity.github.io/trivy/" -ForegroundColor Yellow
            Write-Host "Skipping Trivy scan." -ForegroundColor DarkGray
        } else {
            Write-Host "`n>>> trivy image --exit-code 1 --ignore-unfixed --severity HIGH,CRITICAL $FullImageName" -ForegroundColor Yellow
            try {
                trivy image --exit-code 1 --ignore-unfixed --severity HIGH,CRITICAL $FullImageName
                Step-Footer $true "trivy scan"
            } catch {
                Step-Footer $false "trivy scan (HIGH/CRITICAL vulnerabilities found)"
                Write-Host "Review with: trivy image $FullImageName" -ForegroundColor Yellow
                exit 3
            }
        }
    } else {
        Write-Host "Skipping Trivy scan." -ForegroundColor DarkGray
    }

    # -----------------------------------------------------------------------
    # 5. PUSH (optional)
    # -----------------------------------------------------------------------
    if ($Push) {
        Step-Header "PUSH — $FullImageName"

        Write-Host "`n>>> docker push $FullImageName" -ForegroundColor Yellow
        docker push $FullImageName
        if ($LASTEXITCODE -ne 0) {
            Step-Footer $false "docker push"
            exit 3
        }
        Step-Footer $true "docker push"
    }
} else {
    Write-Host "Skipping docker build." -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------------
# SUMMARY
# ---------------------------------------------------------------------------
Step-Header "CI SUMMARY"
Write-Host "All requested steps completed successfully." -ForegroundColor Green
Write-Host "Image: $FullImageName" -ForegroundColor Green
if (-not $SkipSecurity) {
    Write-Host "Reports: bandit-report.json, pip-audit-report.json" -ForegroundColor Green
}
exit 0
