<#
.SYNOPSIS
    Developer task runner for RetailPulse on Windows.

.DESCRIPTION
    A Makefile equivalent for PowerShell. Wraps the commands used daily so the
    exact invocation lives in the repo rather than in someone's shell history.

.EXAMPLE
    .\scripts\dev.ps1 setup      # create .venv and install everything
    .\scripts\dev.ps1 infra      # start postgres, redis, kafka
    .\scripts\dev.ps1 migrate    # run Alembic migrations for every service
    .\scripts\dev.ps1 test       # run the full test suite
    .\scripts\dev.ps1 lint       # ruff check
    .\scripts\dev.ps1 run product-service
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('setup', 'infra', 'infra-down', 'migrate', 'test', 'lint', 'run')]
    [string]$Task,

    [Parameter(Position = 1)]
    [string]$Service
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'

# Ports are fixed per service so the gateway, Compose and k8s all agree.
$ServicePorts = @{
    'api-gateway'        = 8000
    'product-service'    = 8001
    'inventory-service'  = 8002
    'order-service'      = 8003
    'user-service'       = 8004
    'payment-service'    = 8005
    'fulfilment-service' = 8006
    'analytics-service'  = 8007
    'ml-service'         = 8008
}

function Get-ServiceDirs {
    Get-ChildItem (Join-Path $RepoRoot 'services') -Directory -ErrorAction SilentlyContinue
}

switch ($Task) {
    'setup' {
        Write-Host '==> Creating virtual environment' -ForegroundColor Cyan
        if (-not (Test-Path $Python)) { py -3.11 -m venv (Join-Path $RepoRoot '.venv') }
        & $Python -m pip install --upgrade pip
        & $Python -m pip install -r (Join-Path $RepoRoot 'requirements-dev.txt')
        & $Python -m pip install -e (Join-Path $RepoRoot 'libs\retailpulse_common')
        foreach ($dir in Get-ServiceDirs) {
            $req = Join-Path $dir.FullName 'requirements.txt'
            if (Test-Path $req) {
                Write-Host "==> Installing deps for $($dir.Name)" -ForegroundColor Cyan
                & $Python -m pip install -r $req
            }
        }
    }

    'infra' {
        Set-Location $RepoRoot
        docker compose up -d postgres redis kafka
        Write-Host '==> Waiting for health checks' -ForegroundColor Cyan
        docker compose ps
    }

    'infra-down' {
        Set-Location $RepoRoot
        docker compose down
    }

    'migrate' {
        foreach ($dir in Get-ServiceDirs) {
            if (Test-Path (Join-Path $dir.FullName 'alembic.ini')) {
                Write-Host "==> Migrating $($dir.Name)" -ForegroundColor Cyan
                Push-Location $dir.FullName
                & $Python -m alembic upgrade head
                Pop-Location
            }
        }
    }

    'test' {
        $failed = @()
        foreach ($dir in Get-ServiceDirs) {
            if (Test-Path (Join-Path $dir.FullName 'tests')) {
                Write-Host "==> Testing $($dir.Name)" -ForegroundColor Cyan
                Push-Location $dir.FullName
                & $Python -m pytest
                if ($LASTEXITCODE -ne 0) { $failed += $dir.Name }
                Pop-Location
            }
        }
        if ($failed.Count -gt 0) {
            Write-Host "FAILED: $($failed -join ', ')" -ForegroundColor Red
            exit 1
        }
        Write-Host 'All service test suites passed.' -ForegroundColor Green
    }

    'lint' {
        Set-Location $RepoRoot
        & $Python -m ruff check .
    }

    'run' {
        if (-not $Service) { throw 'Specify a service, e.g. .\scripts\dev.ps1 run product-service' }
        $dir = Join-Path $RepoRoot "services\$Service"
        if (-not (Test-Path $dir)) { throw "No such service: $Service" }
        $port = $ServicePorts[$Service]
        Write-Host "==> $Service on http://localhost:$port/docs" -ForegroundColor Green
        Push-Location $dir
        & $Python -m uvicorn app.main:app --host 0.0.0.0 --port $port --reload
        Pop-Location
    }
}
