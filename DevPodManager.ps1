<#
.SYNOPSIS
    DevPod Manager – WPF UI to manage remote development pods.

.NOTES
    ── CONFIGURATION ──────────────────────────────────────────────────────────
    Edit $script:Config below before first run.

    ── MOCK / LOCAL TESTING ───────────────────────────────────────────────────
    Set the env var before launching to bypass Az.Accounts entirely:
        $env:DEVPOD_MOCK_AUTH = '1'
        .\DevPodManager.ps1

    Point at the local mock server:
        ApiBaseUrl  = 'http://localhost:8080'
        ApiAudience = 'http://localhost'
    ───────────────────────────────────────────────────────────────────────────
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ──────────────────────────────────────────────────────────────────────────────
#  LOAD Az.Accounts ONLY WHEN NOT IN MOCK MODE
# ──────────────────────────────────────────────────────────────────────────────
if (-not $env:DEVPOD_MOCK_AUTH) {
    if (-not (Get-Module -ListAvailable Az.Accounts)) {
        Write-Error 'Az.Accounts module not found. Run: Install-Module Az.Accounts -Scope CurrentUser'
        exit 1
    }
    Import-Module Az.Accounts -ErrorAction Stop
}

# ──────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────
$script:Config = @{
    ApiBaseUrl      = 'http://localhost:8080'
    ApiAudience     = 'http://localhost'
    SshUser         = 'devuser'
    SshPort         = 22
    SshKeyPath      = "$env:USERPROFILE\.ssh\id_rsa"
    GatewayExe      = "$env:LOCALAPPDATA\Programs\JetBrains\JetBrains Gateway\bin\gateway64.exe"
    PollIntervalSec = 30
    HelpersPath     = "$PSScriptRoot\DevPodHelpers.ps1"
}

# ──────────────────────────────────────────────────────────────────────────────
#  DOT-SOURCE HELPERS INTO MAIN SESSION
# ──────────────────────────────────────────────────────────────────────────────
. $script:Config.HelpersPath

# ──────────────────────────────────────────────────────────────────────────────
#  ASSEMBLIES
# ──────────────────────────────────────────────────────────────────────────────
Add-Type -AssemblyName PresentationFramework
Add-Type -AssemblyName PresentationCore
Add-Type -AssemblyName WindowsBase
Add-Type -AssemblyName System.Windows.Forms

# ──────────────────────────────────────────────────────────────────────────────
#  RUNSPACE FACTORY
#  Key insight: every worker runspace dot-sources DevPodHelpers.ps1 directly.
#  No function serialisation, no string interpolation of scriptblocks.
# ──────────────────────────────────────────────────────────────────────────────
function New-WorkerRunspace {
    $rs = [runspacefactory]::CreateRunspace()
    $rs.ApartmentState = 'STA'
    $rs.ThreadOptions  = 'ReuseThread'
    $rs.Open()
    $rs.SessionStateProxy.SetVariable('_Config',      $script:Config)
    $rs.SessionStateProxy.SetVariable('_MockAuth',    "$env:DEVPOD_MOCK_AUTH")
    $rs.SessionStateProxy.SetVariable('_HelpersPath', $script:Config.HelpersPath)
    return $rs
}

# This preamble is prepended to every runspace script via AddScript().
# It restores the mock env var and loads all helper functions.
$script:Preamble = {
    $env:DEVPOD_MOCK_AUTH = $_MockAuth
    . $_HelpersPath
}

# ──────────────────────────────────────────────────────────────────────────────
#  WPF COLOUR HELPERS  (UI thread only – no runspace sharing needed)
# ──────────────────────────────────────────────────────────────────────────────
function Get-StatusBrush ([string]$Status) {
    switch ($Status) {
        'Running'  { return [System.Windows.Media.Brushes]::LimeGreen }
        'Stopped'  { return [System.Windows.Media.Brushes]::Tomato    }
        'Starting' { return [System.Windows.Media.Brushes]::Orange    }
        'Stopping' { return [System.Windows.Media.Brushes]::Orange    }
        default    { return [System.Windows.Media.Brushes]::Gray      }
    }
}

# Safe property getter – returns $null instead of throwing on missing properties.
# PSCustomObject in PS5 throws PropertyNotFoundException for missing members;
# this helper uses the NoteProperty list to check existence first.
function Get-Prop {
    param([object]$Obj, [string[]]$Names, $Default = $null)
    foreach ($n in $Names) {
        if ($null -eq $Obj) { break }
        if ($Obj.PSObject.Properties.Match($n).Count -gt 0) {
            $v = $Obj.$n
            if ($null -ne $v -and "$v" -ne '') { return $v }
        }
    }
    return $Default
}

function Get-ToggleBrush ([bool]$IsRunning) {
    if ($IsRunning) {
        return [System.Windows.Media.Brushes]::Tomato
    }
    return [System.Windows.Media.SolidColorBrush]::new(
        [System.Windows.Media.Color]::FromRgb(58, 140, 92))
}

# ──────────────────────────────────────────────────────────────────────────────
#  XAML
# ──────────────────────────────────────────────────────────────────────────────
[xml]$xaml = @'
<Window
    xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
    xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
    Title="DevPod Manager"
    Width="800" Height="580"
    MinWidth="640" MinHeight="400"
    Background="#1E1E2E"
    WindowStartupLocation="CenterScreen"
    FontFamily="Segoe UI" FontSize="13">

    <Window.Resources>
        <Style x:Key="CardStyle" TargetType="Border">
            <Setter Property="Background"   Value="#2A2A3E"/>
            <Setter Property="CornerRadius" Value="8"/>
            <Setter Property="Padding"      Value="16"/>
            <Setter Property="Margin"       Value="6"/>
            <Setter Property="Effect">
                <Setter.Value>
                    <DropShadowEffect Color="Black" BlurRadius="12" ShadowDepth="2" Opacity="0.4"/>
                </Setter.Value>
            </Setter>
        </Style>

        <Style x:Key="BtnBase" TargetType="Button">
            <Setter Property="Foreground"  Value="White"/>
            <Setter Property="BorderBrush" Value="Transparent"/>
            <Setter Property="Padding"     Value="10,5"/>
            <Setter Property="Cursor"      Value="Hand"/>
            <Setter Property="FontWeight"  Value="SemiBold"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Button">
                        <Border Background="{TemplateBinding Background}"
                                CornerRadius="5" Padding="{TemplateBinding Padding}">
                            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsEnabled" Value="False">
                                <Setter Property="Opacity" Value="0.35"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <Style x:Key="BtnToggle" TargetType="Button" BasedOn="{StaticResource BtnBase}">
            <Setter Property="Width"      Value="80"/>
            <Setter Property="Background" Value="#3A8C5C"/>
        </Style>

        <Style x:Key="BtnIde" TargetType="Button" BasedOn="{StaticResource BtnBase}">
            <Setter Property="Background" Value="#3A5F8C"/>
            <Setter Property="Margin"     Value="6,0,0,0"/>
        </Style>

        <Style x:Key="BtnHeader" TargetType="Button" BasedOn="{StaticResource BtnBase}">
            <Setter Property="Background" Value="#7C6AF7"/>
            <Setter Property="FontSize"   Value="12"/>
        </Style>
    </Window.Resources>

    <Grid>
        <Grid.RowDefinitions>
            <RowDefinition Height="52"/>
            <RowDefinition Height="*"/>
            <RowDefinition Height="32"/>
        </Grid.RowDefinitions>

        <Border Grid.Row="0" Background="#13131F">
            <Grid Margin="16,0">
                <StackPanel Orientation="Horizontal" VerticalAlignment="Center">
                    <TextBlock Text="⬡" Foreground="#7C6AF7" FontSize="20" VerticalAlignment="Center" Margin="0,0,8,0"/>
                    <TextBlock Text="DevPod Manager" Foreground="White" FontSize="15"
                               FontWeight="Bold" VerticalAlignment="Center"/>
                </StackPanel>
                <StackPanel Orientation="Horizontal" HorizontalAlignment="Right" VerticalAlignment="Center">
                    <TextBlock x:Name="TxtUser" Foreground="#888" FontSize="12"
                               VerticalAlignment="Center" Margin="0,0,12,0"/>
                    <Button x:Name="BtnRefresh" Content="⟳  Refresh" Style="{StaticResource BtnHeader}"/>
                </StackPanel>
            </Grid>
        </Border>

        <ScrollViewer Grid.Row="1" VerticalScrollBarVisibility="Auto" Padding="4,4,4,0">
            <StackPanel x:Name="PodPanel"/>
        </ScrollViewer>

        <Border Grid.Row="2" Background="#13131F">
            <TextBlock x:Name="TxtStatus" Foreground="#888" FontSize="11"
                       VerticalAlignment="Center" Margin="16,0" Text="Loading pods…"/>
        </Border>
    </Grid>
</Window>
'@

# ──────────────────────────────────────────────────────────────────────────────
#  PARSE XAML
# ──────────────────────────────────────────────────────────────────────────────
$reader     = [System.Xml.XmlNodeReader]::new($xaml)
$window     = [System.Windows.Markup.XamlReader]::Load($reader)
$podPanel   = $window.FindName('PodPanel')
$txtStatus  = $window.FindName('TxtStatus')
$txtUser    = $window.FindName('TxtUser')
$btnRefresh = $window.FindName('BtnRefresh')

# ──────────────────────────────────────────────────────────────────────────────
#  POD CARD  (always built on the UI thread)
# ──────────────────────────────────────────────────────────────────────────────
function New-PodCard {
    param([psobject]$Pod)

    $podId  = Get-Prop $Pod @("id","podId","name")
    $name   = Get-Prop $Pod @("name","podName") -Default $podId
    $status = Get-Prop $Pod @("status") -Default "Unknown"
    $host_  = Get-Prop $Pod @("hostname","host") -Default ""
    $port_  = [int](Get-Prop $Pod @("sshPort","port") -Default $script:Config.SshPort)

    # Card border
    $card           = [System.Windows.Controls.Border]::new()
    $card.Tag       = $podId
    $card.Style     = $window.FindResource('CardStyle')
    $card.MinHeight = 95

    # Two-column grid
    $grid  = [System.Windows.Controls.Grid]::new()
    $cL    = [System.Windows.Controls.ColumnDefinition]::new()
    $cR    = [System.Windows.Controls.ColumnDefinition]::new()
    $cL.Width = [System.Windows.GridLength]::new(1, [System.Windows.GridUnitType]::Star)
    $cR.Width = [System.Windows.GridLength]::Auto
    $grid.ColumnDefinitions.Add($cL)
    $grid.ColumnDefinitions.Add($cR)
    $card.Child = $grid

    # Info panel (left)
    $info = [System.Windows.Controls.StackPanel]::new()
    [System.Windows.Controls.Grid]::SetColumn($info, 0)

    $nameRow             = [System.Windows.Controls.StackPanel]::new()
    $nameRow.Orientation = [System.Windows.Controls.Orientation]::Horizontal

    $dot                      = [System.Windows.Shapes.Ellipse]::new()
    $dot.Width                = 10
    $dot.Height               = 10
    $dot.Margin               = [System.Windows.Thickness]::new(0,0,8,0)
    $dot.Fill                 = Get-StatusBrush $status
    $dot.VerticalAlignment    = [System.Windows.VerticalAlignment]::Center

    $nameBlk            = [System.Windows.Controls.TextBlock]::new()
    $nameBlk.Text       = $name
    $nameBlk.Foreground = [System.Windows.Media.Brushes]::White
    $nameBlk.FontSize   = 14
    $nameBlk.FontWeight = [System.Windows.FontWeights]::SemiBold

    $nameRow.Children.Add($dot)     | Out-Null
    $nameRow.Children.Add($nameBlk) | Out-Null
    $info.Children.Add($nameRow)    | Out-Null

    $statusBlk            = [System.Windows.Controls.TextBlock]::new()
    $statusBlk.Text       = $status
    $statusBlk.Foreground = Get-StatusBrush $status
    $statusBlk.FontSize   = 12
    $statusBlk.Margin     = [System.Windows.Thickness]::new(18,2,0,0)
    $info.Children.Add($statusBlk) | Out-Null

    $hostBlk            = [System.Windows.Controls.TextBlock]::new()
    $hostBlk.Text       = if ($host_) { "SSH: ${host_}:${port_}" } else { 'Host: —' }
    $hostBlk.Foreground = [System.Windows.Media.SolidColorBrush]::new(
                              [System.Windows.Media.Color]::FromRgb(136,136,136))
    $hostBlk.FontSize   = 11
    $hostBlk.Margin     = [System.Windows.Thickness]::new(18,3,0,0)
    $info.Children.Add($hostBlk) | Out-Null

    $grid.Children.Add($info) | Out-Null

    # Button panel (right)
    $btns                     = [System.Windows.Controls.StackPanel]::new()
    $btns.Orientation         = [System.Windows.Controls.Orientation]::Horizontal
    $btns.VerticalAlignment   = [System.Windows.VerticalAlignment]::Center
    $btns.HorizontalAlignment = [System.Windows.HorizontalAlignment]::Right
    [System.Windows.Controls.Grid]::SetColumn($btns, 1)

    $isRunning = ($status -eq 'Running')

    # -- Toggle button
    $btnToggle            = [System.Windows.Controls.Button]::new()
    $btnToggle.Style      = $window.FindResource('BtnToggle')
    $btnToggle.Content    = if ($isRunning) { '⏹ Stop' } else { '▶ Start' }
    $btnToggle.Background = Get-ToggleBrush $isRunning

    # Pack all per-card state into a hashtable stored on the button Tag.
    # PS5 event handlers cannot close over outer variables - Tag is the
    # reliable way to carry state into add_Click / add_Click handlers.
    $cardState = @{
        PodId     = $podId
        Host      = $host_
        Port      = $port_
        Toggle    = $btnToggle
        StatusBlk = $statusBlk
        Dot       = $dot
        VSCode    = $null   # filled in after button creation
        GW        = $null
    }
    $btnToggle.Tag = $cardState

    $btnToggle.add_Click({
        param($sender, $e)
        $s      = $sender.Tag          # hashtable with all card refs
        $isStop = ($s.Toggle.Content -like '*Stop*')

        $s.Toggle.IsEnabled     = $false
        $s.StatusBlk.Text       = if ($isStop) { 'Stopping…' } else { 'Starting…' }
        $s.Dot.Fill             = [System.Windows.Media.Brushes]::Orange
        $s.StatusBlk.Foreground = [System.Windows.Media.Brushes]::Orange

        $rs = New-WorkerRunspace
        $rs.SessionStateProxy.SetVariable('_PodId',     $s.PodId)
        $rs.SessionStateProxy.SetVariable('_IsStop',    $isStop)
        $rs.SessionStateProxy.SetVariable('_SshHost',   $s.Host)
        $rs.SessionStateProxy.SetVariable('_SshPort',   $s.Port)
        $rs.SessionStateProxy.SetVariable('_Win',       $window)
        $rs.SessionStateProxy.SetVariable('_Toggle',    $s.Toggle)
        $rs.SessionStateProxy.SetVariable('_StatusBlk', $s.StatusBlk)
        $rs.SessionStateProxy.SetVariable('_Dot',       $s.Dot)
        $rs.SessionStateProxy.SetVariable('_BtnVSCode', $s.VSCode)
        $rs.SessionStateProxy.SetVariable('_BtnGW',     $s.GW)

        $ps = [powershell]::Create()
        $ps.Runspace = $rs
        [void]$ps.AddScript($script:Preamble)
        [void]$ps.AddScript({
            try {
                if ($_IsStop) {
                    Stop-DevPod -PodId $_PodId -Config $_Config
                    $_Win.Dispatcher.Invoke([action]{
                        $_Toggle.Content       = '▶ Start'
                        $_Toggle.Background    = [System.Windows.Media.SolidColorBrush]::new([System.Windows.Media.Color]::FromRgb(58,140,92))
                        $_StatusBlk.Text       = 'Stopped'
                        $_StatusBlk.Foreground = [System.Windows.Media.Brushes]::Tomato
                        $_Dot.Fill             = [System.Windows.Media.Brushes]::Tomato
                        if ($_BtnVSCode) { $_BtnVSCode.IsEnabled = $false }
                        if ($_BtnGW)     { $_BtnGW.IsEnabled     = $false }
                        $_Toggle.IsEnabled     = $true
                    })
                } else {
                    Start-DevPod -PodId $_PodId -Config $_Config
                    $sshOk = $false
                    if ($_SshHost) {
                        $sshOk = Wait-SshReachable -Hostname $_SshHost -Port $_SshPort
                    }
                    $_Win.Dispatcher.Invoke([action]{
                        $_Toggle.Content       = '⏹ Stop'
                        $_Toggle.Background    = [System.Windows.Media.Brushes]::Tomato
                        $_StatusBlk.Text       = 'Running'
                        $_StatusBlk.Foreground = [System.Windows.Media.Brushes]::LimeGreen
                        $_Dot.Fill             = [System.Windows.Media.Brushes]::LimeGreen
                        if ($_BtnVSCode) { $_BtnVSCode.IsEnabled = $sshOk }
                        if ($_BtnGW)     { $_BtnGW.IsEnabled     = $sshOk }
                        $_Toggle.IsEnabled     = $true
                    })
                }
            } catch {
                $msg = $_.Exception.Message
                $_Win.Dispatcher.Invoke([action]{
                    $_StatusBlk.Text       = "Error: $msg"
                    $_StatusBlk.Foreground = [System.Windows.Media.Brushes]::OrangeRed
                    $_Toggle.IsEnabled     = $true
                })
            }
        })
        $ps.BeginInvoke() | Out-Null
    })
    $btns.Children.Add($btnToggle) | Out-Null

    # -- VS Code button
    $btnVSCode           = [System.Windows.Controls.Button]::new()
    $btnVSCode.Style     = $window.FindResource('BtnIde')
    $btnVSCode.Content   = ' VS Code'
    $btnVSCode.ToolTip   = 'Open in VS Code Remote-SSH'
    $btnVSCode.IsEnabled = $isRunning -and ($host_ -ne '')
    $btnVSCode.Tag       = $cardState
    $btnVSCode.add_Click({
        param($sender, $e)
        $s    = $sender.Tag
        $user = $script:Config.SshUser
        $target = if ($s.Port -ne 22) { "${user}@$($s.Host):$($s.Port)" } else { "${user}@$($s.Host)" }
        $uri  = "vscode://vscode-remote/ssh-remote+$([uri]::EscapeDataString($target))/home/$user"
        Start-Process $uri
    })
    $btns.Children.Add($btnVSCode) | Out-Null

    # -- Gateway button
    $btnGW           = [System.Windows.Controls.Button]::new()
    $btnGW.Style     = $window.FindResource('BtnIde')
    $btnGW.Content   = ' Gateway'
    $btnGW.ToolTip   = 'Open in JetBrains Gateway'
    $btnGW.IsEnabled = $isRunning -and ($host_ -ne '')
    $btnGW.Tag       = $cardState
    $btnGW.add_Click({
        param($sender, $e)
        $s     = $sender.Tag
        $gwExe = $script:Config.GatewayExe
        $user  = $script:Config.SshUser
        $path  = "/home/$user"
        if (Test-Path $gwExe) {
            $args_ = "ssh --host $($s.Host) --port $($s.Port) --username $user --project-path `"$path`""
            if ($script:Config.SshKeyPath) { $args_ += " --private-key `"$($script:Config.SshKeyPath)`"" }
            Start-Process -FilePath $gwExe -ArgumentList $args_
        } else {
            $keyArg = if ($script:Config.SshKeyPath) {
                "&privateKeyPath=$([uri]::EscapeDataString($script:Config.SshKeyPath))"
            } else { '' }
            $uri = "jetbrains-gateway://connect#host=$($s.Host)&port=$($s.Port)&user=${user}&projectPath=$([uri]::EscapeDataString($path))${keyArg}"
            Start-Process $uri
        }
    })
    $btns.Children.Add($btnGW) | Out-Null

    # Back-fill IDE button refs into the shared cardState hashtable
    $cardState['VSCode'] = $btnVSCode
    $cardState['GW'] = $btnGW

    $grid.Children.Add($btns) | Out-Null

    # Attach metadata for Update-PodCard
    $card | Add-Member -NotePropertyName 'PodId'     -NotePropertyValue $podId     -Force
    $card | Add-Member -NotePropertyName 'StatusDot' -NotePropertyValue $dot       -Force
    $card | Add-Member -NotePropertyName 'StatusTxt' -NotePropertyValue $statusBlk -Force
    $card | Add-Member -NotePropertyName 'HostTxt'   -NotePropertyValue $hostBlk   -Force
    $card | Add-Member -NotePropertyName 'BtnToggle' -NotePropertyValue $btnToggle -Force
    $card | Add-Member -NotePropertyName 'BtnVSCode' -NotePropertyValue $btnVSCode -Force
    $card | Add-Member -NotePropertyName 'BtnGW'     -NotePropertyValue $btnGW     -Force
    $card | Add-Member -NotePropertyName 'Hostname'  -NotePropertyValue $host_     -Force
    $card | Add-Member -NotePropertyName 'SshPort'   -NotePropertyValue $port_     -Force

    return $card
}

# ──────────────────────────────────────────────────────────────────────────────
#  UPDATE EXISTING CARD  (UI thread only)
# ──────────────────────────────────────────────────────────────────────────────
function Update-PodCard {
    param([System.Windows.Controls.Border]$Card, [psobject]$Pod)

    $status  = Get-Prop $Pod @("status") -Default "Unknown"
    $host_   = Get-Prop $Pod @("hostname","host") -Default $Card.Hostname
    $port_   = [int](Get-Prop $Pod @("sshPort","port") -Default $Card.SshPort)
    $running = ($status -eq 'Running')

    $Card.StatusDot.Fill       = Get-StatusBrush $status
    $Card.StatusTxt.Text       = $status
    $Card.StatusTxt.Foreground = Get-StatusBrush $status
    $Card.HostTxt.Text         = if ($host_) { "SSH: ${host_}:${port_}" } else { 'Host: —' }
    $Card.BtnToggle.Content    = if ($running) { '⏹ Stop' } else { '▶ Start' }
    $Card.BtnToggle.Background = Get-ToggleBrush $running
    $Card.BtnVSCode.IsEnabled  = $running -and ($host_ -ne '')
    $Card.BtnGW.IsEnabled      = $running -and ($host_ -ne '')
    $Card.Hostname             = $host_
    $Card.SshPort              = $port_
}

# ──────────────────────────────────────────────────────────────────────────────
#  RESULT QUEUE  –  runspace pushes here; UI thread drains it
#  Using a ConcurrentQueue means zero shared-state race conditions.
# ──────────────────────────────────────────────────────────────────────────────
$script:ResultQueue = [System.Collections.Concurrent.ConcurrentQueue[hashtable]]::new()

# Called exclusively on the UI/dispatcher thread – safe to touch WPF objects
# and call New-PodCard / Update-PodCard which live in the main session.
function Apply-PodData {
    param([hashtable]$Result)

    if ($Result.ContainsKey('error')) {
        $txtStatus.Text       = "Error: $($Result['error'])"
        $btnRefresh.IsEnabled = $true
        return
    }

    $podData = $Result['pods']   # array of plain hashtables

    # Build lookup of cards already on screen
    $existing = @{}
    foreach ($child in @($podPanel.Children)) {
        if ($null -ne $child.PodId) { $existing[$child.PodId] = $child }
    }

    foreach ($pd in $podData) {
        $pObj = [pscustomobject]$pd
        if ($existing.ContainsKey($pd['id'])) {
            Update-PodCard -Card $existing[$pd['id']] -Pod $pObj
        } else {
            $podPanel.Children.Add((New-PodCard -Pod $pObj)) | Out-Null
        }
    }

    # Remove cards for pods no longer returned by the API
    $liveIds = $podData | ForEach-Object { $_['id'] }
    $stale   = @($podPanel.Children) | Where-Object { $null -ne $_.PodId -and $_.PodId -notin $liveIds }
    foreach ($c in $stale) { $podPanel.Children.Remove($c) }

    if ($podPanel.Children.Count -eq 0) {
        $msg            = [System.Windows.Controls.TextBlock]::new()
        $msg.Text       = 'No DevPods assigned to your account.'
        $msg.Foreground = [System.Windows.Media.Brushes]::Gray
        $msg.HorizontalAlignment = [System.Windows.HorizontalAlignment]::Center
        $msg.Margin     = [System.Windows.Thickness]::new(0,40,0,0)
        $podPanel.Children.Add($msg) | Out-Null
    }

    $txtStatus.Text       = "Updated: $(Get-Date -Format 'HH:mm:ss')  ·  $($podData.Count) pod(s)"
    $btnRefresh.IsEnabled = $true
}

# ──────────────────────────────────────────────────────────────────────────────
#  QUEUE DRAIN TIMER  –  ticks every 200 ms on the UI thread
# ──────────────────────────────────────────────────────────────────────────────
$script:DrainTimer          = [System.Windows.Threading.DispatcherTimer]::new()
$script:DrainTimer.Interval = [timespan]::FromMilliseconds(200)
$script:DrainTimer.add_Tick({
    $item = $null
    while ($script:ResultQueue.TryDequeue([ref]$item)) {
        Apply-PodData -Result $item
    }
})

# ──────────────────────────────────────────────────────────────────────────────
#  REFRESH  –  fires API call in background; result goes into the queue
# ──────────────────────────────────────────────────────────────────────────────
function Invoke-Refresh {
    $txtStatus.Text       = 'Refreshing…'
    $btnRefresh.IsEnabled = $false

    $rs = New-WorkerRunspace
    # Pass the queue so the worker can enqueue its result
    $rs.SessionStateProxy.SetVariable('_Queue', $script:ResultQueue)

    $ps = [powershell]::Create()
    $ps.Runspace = $rs
    [void]$ps.AddScript($script:Preamble)
    [void]$ps.AddScript({
        try {
            $pods = Get-DevPods -Config $_Config


            # Serialise to plain hashtables - safe PS5 property access via PSObject.Properties
            function Get-Field {
                param($Obj, [string[]]$Names, $Default = $null)
                foreach ($n in $Names) {
                    if ($Obj.PSObject.Properties.Match($n).Count -gt 0) {
                        $v = $Obj.$n
                        if ($null -ne $v -and "$v" -ne '') { return $v }
                    }
                }
                return $Default
            }
            $podData = @($pods | ForEach-Object {
                $p = $_
                @{
                    id       = [string](Get-Field $p @('id','podId','name'))
                    name     = [string](Get-Field $p @('name','podName') -Default (Get-Field $p @('id','podId')))
                    status   = [string](Get-Field $p @('status') -Default 'Unknown')
                    hostname = [string](Get-Field $p @('hostname','host') -Default '')
                    sshPort  = [int]   (Get-Field $p @('sshPort','port')  -Default $_Config.SshPort)
                }
            })

            $_Queue.Enqueue(@{ pods = $podData })
        } catch {
            $_Queue.Enqueue(@{ error = $_.Exception.Message })
        }
    })
    $ps.BeginInvoke() | Out-Null
}

# ──────────────────────────────────────────────────────────────────────────────
#  WIRE UP + LAUNCH
# ──────────────────────────────────────────────────────────────────────────────
if ($env:DEVPOD_MOCK_AUTH) {
    $txtUser.Text = 'mockuser@example.com (mock)'
} else {
    try { $ctx = Get-AzContext; if ($ctx) { $txtUser.Text = $ctx.Account.Id } } catch {}
}

$btnRefresh.add_Click({ Invoke-Refresh })

$timer          = [System.Windows.Threading.DispatcherTimer]::new()
$timer.Interval = [timespan]::FromSeconds($script:Config.PollIntervalSec)
$timer.add_Tick({ Invoke-Refresh })

$window.add_Loaded({ $script:DrainTimer.Start(); Invoke-Refresh; $timer.Start() })
$window.add_Closed({ $timer.Stop(); $script:DrainTimer.Stop() })

$app = [System.Windows.Application]::new()
$app.Run($window) | Out-Null