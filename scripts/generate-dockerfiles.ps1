<#
.SYNOPSIS
    Regenerate each service's Dockerfile from the shared template.

.DESCRIPTION
    The spec asks for a Dockerfile per service, and each one must build
    independently -- no shared base image that has to be built first, which
    would couple every service's build to one artifact.

    The cost of that independence is nine near-identical files, so they are
    generated rather than hand-maintained. Editing one by hand is a mistake:
    the next run of this script overwrites it. Change the template instead.

.EXAMPLE
    .\scripts\generate-dockerfiles.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$TemplatePath = Join-Path $RepoRoot 'infra\docker\service.Dockerfile.template'

if (-not (Test-Path $TemplatePath)) {
    throw "Template not found at $TemplatePath"
}

# Ports are fixed per service so Compose, Kubernetes and the gateway's route
# table all agree. Changing one here means changing it in all three.
$Services = [ordered]@{
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

$template = Get-Content $TemplatePath -Raw

foreach ($service in $Services.Keys) {
    $port = $Services[$service]
    $target = Join-Path $RepoRoot "services\$service\Dockerfile"

    if (-not (Test-Path (Split-Path -Parent $target))) {
        Write-Warning "Skipping $service -- directory does not exist."
        continue
    }

    $header = "# Dockerfile for $service (generated from infra/docker/service.Dockerfile.template).`n" +
              "# Regenerate with: .\scripts\generate-dockerfiles.ps1`n`n"
    $content = $template -replace '__SERVICE__', $service -replace '__PORT__', $port

    Set-Content -Path $target -Value ($header + $content) -NoNewline -Encoding utf8
    Write-Host "  wrote services/$service/Dockerfile (port $port)"
}

Write-Host "`nGenerated $($Services.Count) Dockerfiles." -ForegroundColor Green
