#!/usr/bin/env pwsh
#requires -Version 7.2
<#
.SYNOPSIS
    Quick build and push — only docker, no quality/security.

.PARAMETER Registry
    Container registry (default: ghcr.io).

.PARAMETER ImageName
    Image name (default: auto-detected from git remote).

.PARAMETER Tag
    Image tag (default: latest).

.PARAMETER NoCache
    Build without docker cache.
#>

[CmdletBinding()]
param(
    [string] $Registry = "ghcr.io",
    [string] $ImageName = "",
    [string] $Tag = "latest",
    [switch] $NoCache
)

$ErrorActionPreference = "Stop"

# Detect image name from git remote if not provided
if (-not $ImageName) {
    try {
        $remote = git remote get-url origin 2>$null
        if ($remote) {
            $ImageName = $remote -replace '.*[:/]([^/]+/[^/]+)\.git$', '$1' -replace '.*github.com[/:]([^/]+/[^/]+).*', '$1'
            $ImageName = $ImageName.ToLower()
        }
    } catch { }
    if (-not $ImageName) {
        $ImageName = "ha-agenthub"
    }
}

$FullImageName = "$Registry/$ImageName`:$Tag"
Write-Host "Building: $FullImageName" -ForegroundColor Cyan

$cacheArg = $NoCache ? "--no-cache" : ""

# Build
docker compose -f container/docker-compose_local.yml build $cacheArg
if ($LASTEXITCODE -ne 0) { exit 1 }

# Tag
docker tag ha-agenthub:latest $FullImageName

# Smoke test
docker run --rm ha-agenthub:latest python -c "from app.main import app; print('OK')"
if ($LASTEXITCODE -ne 0) { exit 1 }

# Push
Write-Host "Pushing: $FullImageName" -ForegroundColor Cyan
docker push $FullImageName
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host "Done: $FullImageName" -ForegroundColor Green
