# DevPodHelpers.ps1
# Dot-sourced by every runspace so all workers share the same function definitions.
# Never run directly.

# ──────────────────────────────────────────────────────────────────────────────
#  AUTH
# ──────────────────────────────────────────────────────────────────────────────
function Get-AadToken {
    param([string]$Resource)

    if ($env:DEVPOD_MOCK_AUTH) {
        return 'mock-token'
    }

    try {
        $ctx = Get-AzContext
        if (-not $ctx) { throw 'no context' }
    } catch {
        Connect-AzAccount | Out-Null
    }

    return (Get-AzAccessToken -ResourceUrl $Resource).Token
}

# ──────────────────────────────────────────────────────────────────────────────
#  API CALLS
# ──────────────────────────────────────────────────────────────────────────────
function Invoke-PodApi {
    param(
        [string]$Method,
        [string]$Path,
        [hashtable]$Config
    )
    $token   = Get-AadToken -Resource $Config.ApiAudience
    $headers = @{ Authorization = "Bearer $token"; 'Content-Type' = 'application/json' }
    return Invoke-RestMethod -Uri "$($Config.ApiBaseUrl)$Path" -Headers $headers -Method $Method
}

function Get-DevPods {
    param([hashtable]$Config)
    $resp = Invoke-PodApi -Method Get -Path '/api/v1/devpods' -Config $Config
    $list = if ($null -ne $resp.pods)  { $resp.pods  } `
       elseif ($null -ne $resp.value) { $resp.value } `
       else                           { $resp        }
    return @($list)
}

function Start-DevPod {
    param([string]$PodId, [hashtable]$Config)
    Invoke-PodApi -Method Post -Path "/api/v1/devpods/$PodId/start" -Config $Config | Out-Null
}

function Stop-DevPod {
    param([string]$PodId, [hashtable]$Config)
    Invoke-PodApi -Method Post -Path "/api/v1/devpods/$PodId/stop" -Config $Config | Out-Null
}

# ──────────────────────────────────────────────────────────────────────────────
#  SSH POLL
# ──────────────────────────────────────────────────────────────────────────────
function Wait-SshReachable {
    param(
        [string]$Hostname,
        [int]   $Port        = 22,
        [int]   $TimeoutSec  = 180,
        [int]   $RetryDelay  = 4
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $tcp = [System.Net.Sockets.TcpClient]::new()
            $ar  = $tcp.BeginConnect($Hostname, $Port, $null, $null)
            if ($ar.AsyncWaitHandle.WaitOne(2000) -and $tcp.Connected) {
                $tcp.Close()
                return $true
            }
            $tcp.Close()
        } catch {}
        Start-Sleep -Seconds $RetryDelay
    }
    return $false
}
