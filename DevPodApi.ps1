#Requires -Version 5.1
<#
.SYNOPSIS
    DevPod Mock API Server – local HTTP listener that mimics the DevPod REST API.

.DESCRIPTION
    Starts a lightweight HttpListener on http://localhost:8080 (configurable).
    Supports all endpoints consumed by DevPodManager.ps1:

        GET  /api/v1/devpods              → list pods assigned to "current user"
        GET  /api/v1/devpods/{id}         → get single pod (status)
        POST /api/v1/devpods/{id}/start   → start a pod  (async simulation)
        POST /api/v1/devpods/{id}/stop    → stop  a pod  (async simulation)

    Token validation:
        Any Bearer token is accepted (the manager will send a real AAD token
        which we simply ignore – no real auth needed for local dev).

    State is held in-memory; restarting the script resets everything.

.USAGE
    # Terminal 1 – start the mock server
    .\DevPodMockApi.ps1

    # Terminal 2 – point DevPodManager at it
    # In DevPodManager.ps1 set:
    #   ApiBaseUrl  = 'http://localhost:8080'
    #   ApiAudience = 'http://localhost'      (anything – token is ignored)

    Press Ctrl+C to stop the server.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ──────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────
$cfg = @{
    BaseUrl          = 'http://localhost:8080/'
    StartDelayMin    = 4     # seconds before a "Starting" pod becomes "Running"
    StartDelayMax    = 10
    StopDelayMin     = 2
    StopDelayMax     = 6
    SshHost          = '127.0.0.1'   # reported hostname for all mock pods
    SshPort          = 2222          # reported SSH port
}

# ──────────────────────────────────────────────────────────────────────────────
#  INITIAL POD DATA  –  feel free to add / edit entries
# ──────────────────────────────────────────────────────────────────────────────
$script:Pods = [System.Collections.Generic.Dictionary[string,hashtable]]::new()

@(
    @{
        id       = 'pod-001'
        name     = 'backend-dev'
        status   = 'Running'
        hostname = $cfg.SshHost
        sshPort  = $cfg.SshPort
        owner    = 'dev@example.com'
        region   = 'westeurope'
        image    = 'ubuntu-22.04-dev'
        cpu      = 4
        memoryGb = 8
    }
    @{
        id       = 'pod-002'
        name     = 'frontend-sandbox'
        status   = 'Stopped'
        hostname = ''
        sshPort  = $cfg.SshPort
        owner    = 'dev@example.com'
        region   = 'eastus'
        image    = 'ubuntu-22.04-node'
        cpu      = 2
        memoryGb = 4
    }
    @{
        id       = 'pod-003'
        name     = 'ml-training-gpu'
        status   = 'Stopped'
        hostname = ''
        sshPort  = $cfg.SshPort
        owner    = 'dev@example.com'
        region   = 'eastus2'
        image    = 'ubuntu-22.04-cuda'
        cpu      = 8
        memoryGb = 32
    }
    @{
        id       = 'pod-004'
        name     = 'infra-toolbox'
        status   = 'Running'
        hostname = $cfg.SshHost
        sshPort  = $cfg.SshPort
        owner    = 'dev@example.com'
        region   = 'westeurope'
        image    = 'ubuntu-22.04-infra'
        cpu      = 2
        memoryGb = 4
    }
) | ForEach-Object { $script:Pods[$_.id] = $_ }

# ──────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ──────────────────────────────────────────────────────────────────────────────
function ConvertTo-Json2 ([object]$obj) {
    # Wrapper – works on PS5 and PS7
    return ($obj | ConvertTo-Json -Depth 10 -Compress)
}

function Write-Response {
    param(
        [System.Net.HttpListenerResponse]$Response,
        [int]    $StatusCode  = 200,
        [string] $Body        = '{}',
        [string] $ContentType = 'application/json'
    )
    $Response.StatusCode    = $StatusCode
    $Response.ContentType   = "$ContentType; charset=utf-8"
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Body)
    $Response.ContentLength64 = $bytes.Length
    $Response.OutputStream.Write($bytes, 0, $bytes.Length)
    $Response.OutputStream.Close()
}

function Write-Json {
    param($Response, $Object, [int]$Code = 200)
    Write-Response -Response $Response -StatusCode $Code -Body (ConvertTo-Json2 $Object)
}

function Write-Error404 ($Response, $msg = 'Not found') {
    Write-Response -Response $Response -StatusCode 404 -Body (ConvertTo-Json2 @{ error = $msg })
}

function Write-Error400 ($Response, $msg = 'Bad request') {
    Write-Response -Response $Response -StatusCode 400 -Body (ConvertTo-Json2 @{ error = $msg })
}

function Get-RandomDelay ([int]$Min, [int]$Max) {
    return Get-Random -Minimum $Min -Maximum ($Max + 1)
}

function Write-Log ([string]$Method, [string]$Path, [int]$Status, [string]$Note = '') {
    $time  = Get-Date -Format 'HH:mm:ss.fff'
    $color = switch ($Status) {
        { $_ -lt 300 } { 'Green'  }
        { $_ -lt 400 } { 'Yellow' }
        default        { 'Red'    }
    }
    Write-Host "[$time] " -NoNewline
    Write-Host "$Method" -ForegroundColor Cyan -NoNewline
    Write-Host " $Path " -NoNewline
    Write-Host $Status -ForegroundColor $color -NoNewline
    if ($Note) { Write-Host "  $Note" -ForegroundColor DarkGray } else { Write-Host '' }
}

# ──────────────────────────────────────────────────────────────────────────────
#  ASYNC TRANSITION SIMULATION
#  Spawns a background thread that flips status after a delay.
# ──────────────────────────────────────────────────────────────────────────────
function Start-PodTransition {
    param(
        [string]$PodId,
        [string]$IntermediateStatus,   # e.g. 'Starting'
        [string]$FinalStatus,          # e.g. 'Running'
        [int]   $DelaySeconds,
        [string]$FinalHostname = '',
        [int]   $FinalSshPort  = $cfg.SshPort
    )

    $script:Pods[$PodId]['status'] = $IntermediateStatus

    $rs = [runspacefactory]::CreateRunspace()
    $rs.Open()
    $rs.SessionStateProxy.SetVariable('pods',       $script:Pods)
    $rs.SessionStateProxy.SetVariable('podId',      $PodId)
    $rs.SessionStateProxy.SetVariable('finalStatus',$FinalStatus)
    $rs.SessionStateProxy.SetVariable('delaySec',   $DelaySeconds)
    $rs.SessionStateProxy.SetVariable('finalHost',  $FinalHostname)
    $rs.SessionStateProxy.SetVariable('finalPort',  $FinalSshPort)

    $ps = [powershell]::Create()
    $ps.Runspace = $rs
    [void]$ps.AddScript({
        Start-Sleep -Seconds $delaySec
        $pods[$podId]['status']   = $finalStatus
        $pods[$podId]['hostname'] = $finalHost
        $pods[$podId]['sshPort']  = $finalPort
    })
    $ps.BeginInvoke() | Out-Null
}

# ──────────────────────────────────────────────────────────────────────────────
#  REQUEST ROUTER
# ──────────────────────────────────────────────────────────────────────────────
function Invoke-Route {
    param([System.Net.HttpListenerContext]$ctx)

    $req    = $ctx.Request
    $resp   = $ctx.Response
    $method = $req.HttpMethod.ToUpper()

    # Strip base path + leading slash, split into segments
    $rawPath = $req.Url.AbsolutePath.TrimEnd('/')
    $segments = $rawPath -split '/' | Where-Object { $_ -ne '' }
    # Expected shapes:
    #   api / v1 / devpods
    #   api / v1 / devpods / {id}
    #   api / v1 / devpods / {id} / start|stop

    # Validate minimum prefix
    if ($segments.Count -lt 3 -or $segments[0] -ne 'api' -or $segments[1] -ne 'v1' -or $segments[2] -ne 'devpods') {
        Write-Log $method $rawPath 404
        Write-Error404 $resp 'Unknown route'
        return
    }

    # ── GET /api/v1/devpods  ──────────────────────────────────────────────────
    if ($method -eq 'GET' -and $segments.Count -eq 3) {
        $list = @($script:Pods.Values)
        $body = ConvertTo-Json2 @{ pods = $list; total = $list.Count }
        Write-Log $method $rawPath 200 "$($list.Count) pod(s)"
        Write-Response -Response $resp -Body $body
        return
    }

    # ── GET /api/v1/devpods/{id}  ─────────────────────────────────────────────
    if ($method -eq 'GET' -and $segments.Count -eq 4) {
        $id = $segments[3]
        if (-not $script:Pods.ContainsKey($id)) {
            Write-Log $method $rawPath 404 "id=$id"
            Write-Error404 $resp "Pod '$id' not found"
            return
        }
        $pod  = $script:Pods[$id]
        $body = ConvertTo-Json2 @{ pod = $pod; status = $pod.status }
        Write-Log $method $rawPath 200 "$id → $($pod.status)"
        Write-Response -Response $resp -Body $body
        return
    }

    # ── POST /api/v1/devpods/{id}/start  ─────────────────────────────────────
    if ($method -eq 'POST' -and $segments.Count -eq 5 -and $segments[4] -eq 'start') {
        $id = $segments[3]
        if (-not $script:Pods.ContainsKey($id)) {
            Write-Log $method $rawPath 404 "id=$id"
            Write-Error404 $resp "Pod '$id' not found"; return
        }
        $pod = $script:Pods[$id]
        if ($pod.status -eq 'Running') {
            Write-Log $method $rawPath 200 "$id already Running"
            Write-Json $resp @{ message = 'Pod is already running'; status = 'Running' }
            return
        }
        if ($pod.status -in @('Starting','Stopping')) {
            Write-Log $method $rawPath 409 "$id is $($pod.status)"
            Write-Response -Response $resp -StatusCode 409 -Body (ConvertTo-Json2 @{ error = "Pod is currently $($pod.status)" })
            return
        }
        $delay = Get-RandomDelay $cfg.StartDelayMin $cfg.StartDelayMax
        Start-PodTransition -PodId $id `
                            -IntermediateStatus 'Starting' `
                            -FinalStatus        'Running' `
                            -DelaySeconds       $delay `
                            -FinalHostname      $cfg.SshHost `
                            -FinalSshPort       $cfg.SshPort
        Write-Log $method $rawPath 202 "$id Starting → Running in ~${delay}s"
        Write-Json $resp @{ message = 'Pod start initiated'; status = 'Starting'; estimatedReadySec = $delay } -Code 202
        return
    }

    # ── POST /api/v1/devpods/{id}/stop  ──────────────────────────────────────
    if ($method -eq 'POST' -and $segments.Count -eq 5 -and $segments[4] -eq 'stop') {
        $id = $segments[3]
        if (-not $script:Pods.ContainsKey($id)) {
            Write-Log $method $rawPath 404 "id=$id"
            Write-Error404 $resp "Pod '$id' not found"; return
        }
        $pod = $script:Pods[$id]
        if ($pod.status -eq 'Stopped') {
            Write-Log $method $rawPath 200 "$id already Stopped"
            Write-Json $resp @{ message = 'Pod is already stopped'; status = 'Stopped' }
            return
        }
        if ($pod.status -in @('Starting','Stopping')) {
            Write-Log $method $rawPath 409 "$id is $($pod.status)"
            Write-Response -Response $resp -StatusCode 409 -Body (ConvertTo-Json2 @{ error = "Pod is currently $($pod.status)" })
            return
        }
        $delay = Get-RandomDelay $cfg.StopDelayMin $cfg.StopDelayMax
        Start-PodTransition -PodId $id `
                            -IntermediateStatus 'Stopping' `
                            -FinalStatus        'Stopped' `
                            -DelaySeconds       $delay `
                            -FinalHostname      '' `
                            -FinalSshPort       $cfg.SshPort
        Write-Log $method $rawPath 202 "$id Stopping → Stopped in ~${delay}s"
        Write-Json $resp @{ message = 'Pod stop initiated'; status = 'Stopping'; estimatedStopSec = $delay } -Code 202
        return
    }

    # ── Fallthrough ───────────────────────────────────────────────────────────
    Write-Log $method $rawPath 404
    Write-Error404 $resp 'Route not found'
}

# ──────────────────────────────────────────────────────────────────────────────
#  MAIN LOOP
# ──────────────────────────────────────────────────────────────────────────────
$listener = [System.Net.HttpListener]::new()
$listener.Prefixes.Add($cfg.BaseUrl)

try {
    $listener.Start()
} catch [System.Net.HttpListenerException] {
    Write-Host ''
    Write-Host '  ERROR: Could not bind to ' -ForegroundColor Red -NoNewline
    Write-Host $cfg.BaseUrl -ForegroundColor Yellow
    Write-Host '  Try running as Administrator, or change the port in $cfg.BaseUrl' -ForegroundColor Red
    Write-Host ''
    exit 1
}

# Pretty banner
Write-Host ''
Write-Host '  ┌─────────────────────────────────────────────────────┐' -ForegroundColor DarkCyan
Write-Host '  │          DevPod Mock API Server  –  running         │' -ForegroundColor DarkCyan
Write-Host '  ├─────────────────────────────────────────────────────┤' -ForegroundColor DarkCyan
Write-Host "  │  URL  : $($cfg.BaseUrl.PadRight(43))│" -ForegroundColor Cyan
Write-Host "  │  Pods : $("$($script:Pods.Count) loaded".PadRight(43))│" -ForegroundColor Cyan
Write-Host '  ├─────────────────────────────────────────────────────┤' -ForegroundColor DarkCyan
Write-Host '  │  DevPodManager.ps1 settings:                        │' -ForegroundColor DarkGray
Write-Host "  │    ApiBaseUrl  = 'http://localhost:8080'             │" -ForegroundColor DarkGray
Write-Host "  │    ApiAudience = 'http://localhost'                  │" -ForegroundColor DarkGray
Write-Host '  ├─────────────────────────────────────────────────────┤' -ForegroundColor DarkCyan
Write-Host '  │  Press Ctrl+C to stop                               │' -ForegroundColor DarkGray
Write-Host '  └─────────────────────────────────────────────────────┘' -ForegroundColor DarkCyan
Write-Host ''
Write-Host '  Time       Method  Path                                   Status  Note' -ForegroundColor DarkGray
Write-Host '  ─────────────────────────────────────────────────────────────────────' -ForegroundColor DarkGray

# Trap Ctrl+C for clean shutdown
$null = Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action {
    Write-Host "`n  Shutting down mock API…" -ForegroundColor Yellow
    $listener.Stop()
}

try {
    while ($listener.IsListening) {
        # GetContext blocks; use BeginGetContext for non-blocking
        $asyncResult = $listener.BeginGetContext($null, $null)

        # Wait with a short timeout so Ctrl+C can break the loop
        while (-not $asyncResult.IsCompleted) {
            if ([console]::KeyAvailable) {
                $key = [console]::ReadKey($true)
                if ($key.Key -eq 'C' -and $key.Modifiers -band [ConsoleModifiers]::Control) {
                    throw [System.OperationCanceledException]'User cancelled'
                }
            }
            Start-Sleep -Milliseconds 100
        }

        $ctx = $listener.EndGetContext($asyncResult)

        # Handle each request in a thread-pool runspace so the loop stays free
        $rs = [runspacefactory]::CreateRunspace()
        $rs.Open()
        $rs.SessionStateProxy.SetVariable('ctx',         $ctx)
        $rs.SessionStateProxy.SetVariable('Pods',        $script:Pods)
        $rs.SessionStateProxy.SetVariable('cfg',         $cfg)
        $rs.SessionStateProxy.SetVariable('routeFn',     ${function:Invoke-Route})
        $rs.SessionStateProxy.SetVariable('writeLogFn',  ${function:Write-Log})
        $rs.SessionStateProxy.SetVariable('writeResp',   ${function:Write-Response})
        $rs.SessionStateProxy.SetVariable('writeJson',   ${function:Write-Json})
        $rs.SessionStateProxy.SetVariable('writeErr404', ${function:Write-Error404})
        $rs.SessionStateProxy.SetVariable('writeErr400', ${function:Write-Error400})
        $rs.SessionStateProxy.SetVariable('transitionFn',${function:Start-PodTransition})
        $rs.SessionStateProxy.SetVariable('toJson2',     ${function:ConvertTo-Json2})
        $rs.SessionStateProxy.SetVariable('randomDelay', ${function:Get-RandomDelay})

        $ps = [powershell]::Create(); $ps.Runspace = $rs
        [void]$ps.AddScript({
            # Re-define helpers in this runspace
            . ([scriptblock]::Create("function ConvertTo-Json2    { $toJson2     }"))
            . ([scriptblock]::Create("function Write-Response     { $writeResp   }"))
            . ([scriptblock]::Create("function Write-Json         { $writeJson   }"))
            . ([scriptblock]::Create("function Write-Error404     { $writeErr404 }"))
            . ([scriptblock]::Create("function Write-Error400     { $writeErr400 }"))
            . ([scriptblock]::Create("function Write-Log          { $writeLogFn  }"))
            . ([scriptblock]::Create("function Get-RandomDelay    { $randomDelay }"))
            . ([scriptblock]::Create("function Start-PodTransition{ $transitionFn }"))
            . ([scriptblock]::Create("function Invoke-Route       { $routeFn     }"))

            try   { Invoke-Route -ctx $ctx }
            catch { Write-Host "  [ERROR] $_" -ForegroundColor Red }
        })
        $ps.BeginInvoke() | Out-Null
    }
} catch [System.OperationCanceledException] {
    # normal Ctrl+C exit
} finally {
    $listener.Stop()
    $listener.Close()
    Write-Host ''
    Write-Host '  Mock API stopped.' -ForegroundColor Yellow
    Write-Host ''
}