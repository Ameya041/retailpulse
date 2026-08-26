<#
.SYNOPSIS
    Regenerate the per-service Kubernetes manifests from the shared templates.

.DESCRIPTION
    Nine services with near-identical Deployments is exactly the shape of
    configuration that drifts when hand-maintained: someone fixes a probe on
    one service and the other eight keep the bug. Generating from one reviewed
    template means a fix lands everywhere at once.

    The generated files are committed, so what runs in the cluster is
    reviewable in the diff -- generation is a authoring convenience, not a
    deploy-time step.

    Edit k8s/templates/*.template, not the generated files.

.EXAMPLE
    .\scripts\generate-k8s.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$TemplateDir = Join-Path $RepoRoot 'k8s\templates'

# Resource sizing is per service rather than uniform, because they are not
# alike: ml-service loads a model and a history snapshot into memory, the
# gateway is I/O bound and needs almost none.
$Services = @(
    @{ Name = 'api-gateway';        Port = 8000; Replicas = 2; Cpu = '100m'; Mem = '192Mi'; CpuLimit = '500m'; MemLimit = '384Mi' }
    @{ Name = 'product-service';    Port = 8001; Replicas = 2; Cpu = '100m'; Mem = '256Mi'; CpuLimit = '500m'; MemLimit = '512Mi' }
    @{ Name = 'inventory-service';  Port = 8002; Replicas = 2; Cpu = '150m'; Mem = '256Mi'; CpuLimit = '600m'; MemLimit = '512Mi' }
    @{ Name = 'order-service';      Port = 8003; Replicas = 2; Cpu = '150m'; Mem = '256Mi'; CpuLimit = '600m'; MemLimit = '512Mi' }
    @{ Name = 'user-service';       Port = 8004; Replicas = 2; Cpu = '100m'; Mem = '256Mi'; CpuLimit = '500m'; MemLimit = '512Mi' }
    @{ Name = 'payment-service';    Port = 8005; Replicas = 2; Cpu = '100m'; Mem = '256Mi'; CpuLimit = '500m'; MemLimit = '512Mi' }
    @{ Name = 'fulfilment-service'; Port = 8006; Replicas = 2; Cpu = '100m'; Mem = '256Mi'; CpuLimit = '500m'; MemLimit = '512Mi' }
    @{ Name = 'analytics-service';  Port = 8007; Replicas = 2; Cpu = '150m'; Mem = '384Mi'; CpuLimit = '700m'; MemLimit = '768Mi' }
    # Single replica: it holds a model and a full history snapshot in memory,
    # so a second replica doubles the memory for read-only work that is not
    # currently a bottleneck.
    @{ Name = 'ml-service';         Port = 8008; Replicas = 1; Cpu = '200m'; Mem = '512Mi'; CpuLimit = '1';    MemLimit = '1Gi' }
)

$Workers = @(
    @{ Name = 'inventory-worker';  Service = 'inventory-service' }
    @{ Name = 'order-worker';      Service = 'order-service' }
    @{ Name = 'payment-worker';    Service = 'payment-service' }
    @{ Name = 'fulfilment-worker'; Service = 'fulfilment-service' }
    @{ Name = 'analytics-worker';  Service = 'analytics-service' }
)

$serviceTemplate = Get-Content (Join-Path $TemplateDir 'service.yaml.template') -Raw
$workerTemplate = Get-Content (Join-Path $TemplateDir 'worker.yaml.template') -Raw

# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
$header = @"
# GENERATED FILE -- do not edit.
# Regenerate with: .\scripts\generate-k8s.ps1
# Source template: k8s/templates/service.yaml.template

"@

$out = New-Object System.Text.StringBuilder
[void]$out.Append($header)

foreach ($svc in $Services) {
    $block = $serviceTemplate `
        -replace '__NAME__', $svc.Name `
        -replace '__PORT__', $svc.Port `
        -replace '__REPLICAS__', $svc.Replicas `
        -replace '__CPU_REQUEST__', $svc.Cpu `
        -replace '__MEM_REQUEST__', $svc.Mem `
        -replace '__CPU_LIMIT__', $svc.CpuLimit `
        -replace '__MEM_LIMIT__', $svc.MemLimit
    [void]$out.AppendLine($block)
}

Set-Content -Path (Join-Path $RepoRoot 'k8s\04-services.yaml') -Value $out.ToString() -NoNewline -Encoding utf8
Write-Host "  wrote k8s/04-services.yaml ($($Services.Count) services)"

# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------
$workerHeader = @"
# GENERATED FILE -- do not edit.
# Regenerate with: .\scripts\generate-k8s.ps1
# Source template: k8s/templates/worker.yaml.template

"@

$out = New-Object System.Text.StringBuilder
[void]$out.Append($workerHeader)

foreach ($worker in $Workers) {
    $block = $workerTemplate `
        -replace '__NAME__', $worker.Name `
        -replace '__SERVICE__', $worker.Service
    [void]$out.AppendLine($block)
}

Set-Content -Path (Join-Path $RepoRoot 'k8s\05-workers.yaml') -Value $out.ToString() -NoNewline -Encoding utf8
Write-Host "  wrote k8s/05-workers.yaml ($($Workers.Count) workers)"

# ---------------------------------------------------------------------------
# Horizontal Pod Autoscalers
# ---------------------------------------------------------------------------
$hpaOut = New-Object System.Text.StringBuilder
[void]$hpaOut.AppendLine(@"
# GENERATED FILE -- do not edit.
# Regenerate with: .\scripts\generate-k8s.ps1
#
# Autoscaling on CPU. These services are stateless -- no in-memory session, no
# local disk, every request self-contained -- which is precisely what makes
# adding a replica safe. A service holding per-user state in memory could not
# be scaled this way without a sticky-session hack.
#
# The workers are deliberately absent: a Kafka consumer's useful parallelism
# is capped by its partition count, so an HPA would add pods that sit idle.
"@)

foreach ($svc in $Services) {
    # ml-service is excluded: it is memory-bound and single-replica by design.
    if ($svc.Name -eq 'ml-service') { continue }

    [void]$hpaOut.AppendLine(@"
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: $($svc.Name)
  namespace: retailpulse
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: $($svc.Name)
  minReplicas: $($svc.Replicas)
  maxReplicas: 5
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          # 70%, not 90%. Scaling up takes time -- schedule, pull, start,
          # pass a readiness probe -- so the trigger has to leave headroom to
          # absorb load while that happens.
          averageUtilization: 70
    - type: Resource
      resource:
        name: memory
        target:
          type: Utilization
          averageUtilization: 80
  behavior:
    scaleUp:
      # React quickly to load.
      stabilizationWindowSeconds: 30
      policies:
        - type: Percent
          value: 100
          periodSeconds: 30
    scaleDown:
      # Scale down slowly. Aggressive scale-down causes thrashing: traffic
      # dips, pods are removed, traffic returns, pods are added again.
      stabilizationWindowSeconds: 300
      policies:
        - type: Percent
          value: 50
          periodSeconds: 60
"@)
}

Set-Content -Path (Join-Path $RepoRoot 'k8s\06-hpa.yaml') -Value $hpaOut.ToString() -NoNewline -Encoding utf8
Write-Host "  wrote k8s/06-hpa.yaml"

Write-Host "`nGenerated Kubernetes manifests." -ForegroundColor Green
