param(
    [switch]$ValidateOnly,
    [switch]$NoBrowser,
    [string]$ProjectRoot = (Split-Path $PSScriptRoot -Parent)
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$composeArgs = @('compose', '--env-file', 'deploy/local/docker.env', '-f', 'compose.yml', '-f', 'compose.napcat.yml', '-f', 'deploy/local/compose.local.yml')

function Test-DockerEngine {
    try { $kind = (& docker info --format '{{.OSType}}' 2>$null | Out-String).Trim() }
    catch { return $false }
    if ($LASTEXITCODE -ne 0) { return $false }
    if ($kind -ne 'linux') { throw 'Please switch Docker Desktop to Linux containers.' }
    return $true
}

function Get-LocalServiceUrl([int]$Port) {
    $address = (& docker @composeArgs port bot $Port | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $address -notmatch '^127\.0\.0\.1:(\d+)$') {
        throw 'Management ports must be published on 127.0.0.1.'
    }
    return ('http://127.0.0.1:' + $Matches[1])
}

function Wait-Web([string]$Url) {
    $deadline = (Get-Date).AddSeconds(120)
    do {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -eq 200) { return }
        } catch { }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw "Management page did not become ready: $Url"
}

Push-Location $ProjectRoot
try {
    foreach ($file in @('deploy/local/docker.env', 'deploy/local/compose.local.yml', 'deploy/local/workspace/config.local.yml')) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
            throw "Missing local deployment configuration: $file. See docs/DOCKER.md."
        }
    }
    if (-not (Get-ChildItem -LiteralPath 'deploy/local/napcat-config' -Filter 'onebot11_*.json' -File -ErrorAction SilentlyContinue)) {
        throw 'Missing private NapCat account configuration. See docs/DOCKER.md.'
    }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Install Docker Desktop first.' }
    Write-Host '[1/4] Checking local deployment configuration...'
    & docker @composeArgs config --quiet
    if ($LASTEXITCODE -ne 0) { throw 'Compose configuration validation failed.' }
    if ($ValidateOnly) { Write-Host 'Configuration is valid. No containers or browsers were started.'; return }

    Write-Host '[2/4] Checking Docker Engine...'
    if (-not (Test-DockerEngine)) {
        $dockerCommand = Get-Command docker
        $candidates = @((Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'))
        if ($dockerCommand.Source) {
            $candidates += Join-Path (Split-Path (Split-Path (Split-Path $dockerCommand.Source -Parent) -Parent) -Parent) 'Docker Desktop.exe'
        }
        $desktop = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
        if (-not $desktop) { throw 'Start Docker Desktop, wait for Engine, then run this file again.' }
        Start-Process -FilePath $desktop -WindowStyle Hidden
        $deadline = (Get-Date).AddSeconds(180)
        while (-not (Test-DockerEngine)) {
            if ((Get-Date) -gt $deadline) { throw 'Docker Engine startup timed out.' }
            Start-Sleep -Seconds 3
        }
    }
    Write-Host '[3/4] Building and starting containers. The first build can take several minutes...'
    Write-Host 'Stop any old native bot/NapCat using the same account before logging in.'
    & docker @composeArgs up -d --build
    if ($LASTEXITCODE -ne 0) { throw 'Build/start failed. Check Docker Desktop network and proxy settings.' }

    Write-Host '[4/4] Waiting for management pages...'
    $botUrl = Get-LocalServiceUrl 8766
    $napcatUrl = Get-LocalServiceUrl 6099
    Wait-Web ($botUrl + '/')
    Wait-Web ($napcatUrl + '/')
    $tokenCode = "from pathlib import Path; print(Path('/workspace/.secrets/admin-token').read_text().strip())"
    $adminToken = (& docker @composeArgs exec -T bot python -c $tokenCode | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $adminToken -notmatch '^[A-Za-z0-9_-]{20,200}$') {
        throw 'Unable to read the local management token. Check bot container status.'
    }
    $adminUrl = $botUrl + '/#token=' + $adminToken
    Write-Host ('Bot: ' + $adminUrl)
    Write-Host ('NapCat: ' + $napcatUrl)
    Write-Host 'NapCat password: NAPCAT_WEBUI_TOKEN in deploy/local/docker.env (keep private).'
    Write-Host 'Scan QQ on first use, then start platform workers from the bot dashboard.'
    Write-Host 'Deployment starts management services only; it does not start the AI worker.'
    if (-not $NoBrowser) {
        Start-Process $adminUrl
        Start-Process $napcatUrl
    }
} catch {
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
} finally {
    Pop-Location
}
