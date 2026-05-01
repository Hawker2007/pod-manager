# Modern WPF Resource Monitor with Live Graphs
# Uses DispatcherTimer with async data collection for responsive UI

Add-Type -AssemblyName PresentationFramework
Add-Type -AssemblyName PresentationCore
Add-Type -AssemblyName WindowsBase

# XAML for modern UI
$xaml = @"
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Resource Monitor" Height="600" Width="900"
        WindowStartupLocation="CenterScreen"
        Background="#1E1E2E"
        WindowStyle="None"
        AllowsTransparency="True"
        ResizeMode="CanResizeWithGrip">
    <Window.Resources>
        <Style TargetType="TextBlock">
            <Setter Property="Foreground" Value="#CDD6F4"/>
            <Setter Property="FontFamily" Value="Segoe UI"/>
        </Style>
        <Style TargetType="Border" x:Key="CardStyle">
            <Setter Property="Background" Value="#313244"/>
            <Setter Property="CornerRadius" Value="12"/>
            <Setter Property="Padding" Value="20"/>
            <Setter Property="Margin" Value="10"/>
        </Style>
    </Window.Resources>
    <Grid>
        <Grid.RowDefinitions>
            <RowDefinition Height="40"/>
            <RowDefinition Height="*"/>
        </Grid.RowDefinitions>

        <!-- Title Bar -->
        <Border Grid.Row="0" Background="#181825" x:Name="TitleBar">
            <Grid>
                <TextBlock Text="Resource Monitor" FontSize="14" FontWeight="SemiBold"
                           VerticalAlignment="Center" Margin="15,0,0,0"/>
                <StackPanel Orientation="Horizontal" HorizontalAlignment="Right" Margin="0,0,10,0">
                    <Button x:Name="MinimizeBtn" Content="─" Width="30" Height="30"
                            Background="Transparent" Foreground="#CDD6F4" BorderThickness="0"
                            FontSize="14" Cursor="Hand"/>
                    <Button x:Name="CloseBtn" Content="✕" Width="30" Height="30"
                            Background="Transparent" Foreground="#CDD6F4" BorderThickness="0"
                            FontSize="14" Cursor="Hand"/>
                </StackPanel>
            </Grid>
        </Border>

        <!-- Main Content -->
        <Grid Grid.Row="1">
            <Grid.RowDefinitions>
                <RowDefinition Height="*"/>
                <RowDefinition Height="*"/>
                <RowDefinition Height="Auto"/>
            </Grid.RowDefinitions>

            <!-- CPU Section -->
            <Border Grid.Row="0" Style="{StaticResource CardStyle}">
                <DockPanel>
                    <Grid DockPanel.Dock="Top">
                        <Grid.ColumnDefinitions>
                            <ColumnDefinition Width="*"/>
                            <ColumnDefinition Width="Auto"/>
                        </Grid.ColumnDefinitions>
                        <TextBlock Grid.Column="0" Text="CPU Usage" FontSize="18" FontWeight="Bold"/>
                        <TextBlock Grid.Column="1" x:Name="CpuPercent" Text="0%" FontSize="24"
                                   FontWeight="Bold" Foreground="#F5C2E7"/>
                    </Grid>
                    <Border Background="#1E1E2E" CornerRadius="8" Margin="0,10,0,0">
                        <Canvas x:Name="CpuCanvas" ClipToBounds="True"/>
                    </Border>
                </DockPanel>
            </Border>

            <!-- Memory Section -->
            <Border Grid.Row="1" Style="{StaticResource CardStyle}">
                <DockPanel>
                    <Grid DockPanel.Dock="Top">
                        <Grid.ColumnDefinitions>
                            <ColumnDefinition Width="*"/>
                            <ColumnDefinition Width="Auto"/>
                        </Grid.ColumnDefinitions>
                        <TextBlock Grid.Column="0" Text="Memory Usage" FontSize="18" FontWeight="Bold"/>
                        <StackPanel Grid.Column="1" Orientation="Horizontal">
                            <TextBlock x:Name="MemUsed" Text="0 GB" FontSize="16"
                                       Foreground="#A6E3A1" Margin="0,0,5,0"/>
                            <TextBlock Text="/" FontSize="16" Foreground="#6C7086" Margin="0,0,5,0"/>
                            <TextBlock x:Name="MemTotal" Text="0 GB" FontSize="16" Foreground="#6C7086"/>
                            <TextBlock x:Name="MemPercent" Text=" (0%)" FontSize="24"
                                       FontWeight="Bold" Foreground="#A6E3A1" Margin="10,0,0,0"/>
                        </StackPanel>
                    </Grid>
                    <Border Background="#1E1E2E" CornerRadius="8" Margin="0,10,0,0">
                        <Canvas x:Name="MemCanvas" ClipToBounds="True"/>
                    </Border>
                </DockPanel>
            </Border>

            <!-- Stats Grid -->
            <Border Grid.Row="2" Style="{StaticResource CardStyle}">
                <UniformGrid Columns="4" Rows="1">
                    <StackPanel Margin="10">
                        <TextBlock Text="CPU Cores" FontSize="12" Foreground="#6C7086"/>
                        <TextBlock x:Name="CpuCores" Text="0" FontSize="20" FontWeight="Bold"/>
                    </StackPanel>
                    <StackPanel Margin="10">
                        <TextBlock Text="Threads" FontSize="12" Foreground="#6C7086"/>
                        <TextBlock x:Name="ThreadCount" Text="0" FontSize="20" FontWeight="Bold"/>
                    </StackPanel>
                    <StackPanel Margin="10">
                        <TextBlock Text="Processes" FontSize="12" Foreground="#6C7086"/>
                        <TextBlock x:Name="ProcessCount" Text="0" FontSize="20" FontWeight="Bold"/>
                    </StackPanel>
                    <StackPanel Margin="10">
                        <TextBlock Text="Uptime" FontSize="12" Foreground="#6C7086"/>
                        <TextBlock x:Name="Uptime" Text="0h 0m" FontSize="20" FontWeight="Bold"/>
                    </StackPanel>
                </UniformGrid>
            </Border>
        </Grid>
    </Grid>
</Window>
"@

# Load XAML
$reader = [System.Xml.XmlReader]::Create([System.IO.StringReader]::new($xaml))
$window = [System.Windows.Markup.XamlReader]::Load($reader)

# Get controls
$cpuPercent = $window.FindName("CpuPercent")
$memPercent = $window.FindName("MemPercent")
$memUsed = $window.FindName("MemUsed")
$memTotal = $window.FindName("MemTotal")
$cpuCores = $window.FindName("CpuCores")
$threadCount = $window.FindName("ThreadCount")
$processCount = $window.FindName("ProcessCount")
$uptime = $window.FindName("Uptime")
$cpuCanvas = $window.FindName("CpuCanvas")
$memCanvas = $window.FindName("MemCanvas")

# Window controls
$minimizeBtn = $window.FindName("MinimizeBtn")
$closeBtn = $window.FindName("CloseBtn")
$titleBar = $window.FindName("TitleBar")

$minimizeBtn.Add_Click({ $window.WindowState = "Minimized" })
$closeBtn.Add_Click({ $window.Close() })
$titleBar.Add_MouseLeftButtonDown({ $window.DragMove() })

# Create polylines
$cpuGraph = New-Object System.Windows.Shapes.Polyline
$cpuGraph.Stroke = [System.Windows.Media.Brushes]::Fuchsia
$cpuGraph.StrokeThickness = 2
$cpuCanvas.Children.Add($cpuGraph)

$memGraph = New-Object System.Windows.Shapes.Polyline
$memGraph.Stroke = [System.Windows.Media.Brushes]::LimeGreen
$memGraph.StrokeThickness = 2
$memCanvas.Children.Add($memGraph)

# Initialize
$totalMemory = (Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory
$memTotal.Text = "{0:F1} GB" -f ($totalMemory / 1GB)
$cpuCores.Text = (Get-CimInstance Win32_Processor | Measure-Object NumberOfCores -Sum).Sum

$cpuCounter = New-Object System.Diagnostics.PerformanceCounter("Processor", "% Processor Time", "_Total")
$cpuCounter.NextValue() | Out-Null
Start-Sleep -Milliseconds 100

# Data storage
$cpuData = [System.Collections.Generic.List[double]]::new()
$memData = [System.Collections.Generic.List[double]]::new()
$maxPoints = 100

# Shared data for async updates
$global:cpuVal = 0
$global:memPct = 0
$global:memUsedVal = 0
$global:dataLock = [System.Threading.ReaderWriterLockSlim]::new()

# Background data collection job
$bgJob = Start-Job -ScriptBlock {
    param($maxPoints)
    
    $cpuCounter = New-Object System.Diagnostics.PerformanceCounter("Processor", "% Processor Time", "_Total")
    $cpuCounter.NextValue() | Out-Null
    Start-Sleep -Milliseconds 100
    
    $cpuData = [System.Collections.Generic.List[double]]::new()
    $memData = [System.Collections.Generic.List[double]]::new()
    
    while ($true) {
        try {
            $cpu = [math]::Round($cpuCounter.NextValue(), 1)
            $mem = Get-CimInstance Win32_OperatingSystem
            $memUsed = ($mem.TotalVisibleMemorySize - $mem.FreePhysicalMemory) / 1MB
            $memTotal = $mem.TotalVisibleMemorySize / 1MB
            $memPct = [math]::Round(($memUsed / $memTotal) * 100, 1)
            
            $cpuData.Add($cpu)
            if ($cpuData.Count -gt $maxPoints) { $cpuData.RemoveAt(0) }
            $memData.Add($memPct)
            if ($memData.Count -gt $maxPoints) { $memData.RemoveAt(0) }
            
            # Output data for parent to read
            [PSCustomObject]@{
                CpuVal = $cpu
                MemPct = $memPct
                MemUsedVal = $memUsed
                CpuData = $cpuData.ToArray()
                MemData = $memData.ToArray()
            }
        } catch {}
        Start-Sleep -Milliseconds 500
    }
} -ArgumentList $maxPoints

# Timer for UI updates
$timer = New-Object System.Windows.Threading.DispatcherTimer
$timer.Interval = [TimeSpan]::FromMilliseconds(500)
$timer.Add_Tick({
    # Check for new data from background job
    if ($bgJob.HasMoreData) {
        $data = Receive-Job $bgJob | Select-Object -Last 1
        if ($data) {
            $global:cpuVal = $data.CpuVal
            $global:memPct = $data.MemPct
            $global:memUsedVal = $data.MemUsedVal
            
            # Update text
            $cpuPercent.Text = "$($data.CpuVal)%"
            $memUsed.Text = "{0:F1} GB" -f $data.MemUsedVal
            $memPercent.Text = " ($($data.MemPct)%)"
            
            # Update graphs
            $cpuGraph.Points.Clear()
            $memGraph.Points.Clear()
            
            $canvasWidth = $cpuCanvas.ActualWidth
            $canvasHeight = $cpuCanvas.ActualHeight
            
            if ($canvasWidth -gt 0 -and $canvasHeight -gt 0) {
                for ($i = 0; $i -lt $data.CpuData.Count; $i++) {
                    $x = ($i / $maxPoints) * $canvasWidth
                    $y = $canvasHeight - ($data.CpuData[$i] / 100) * $canvasHeight
                    $cpuGraph.Points.Add([System.Windows.Point]::new($x, $y))
                }
                
                for ($i = 0; $i -lt $data.MemData.Count; $i++) {
                    $x = ($i / $maxPoints) * $canvasWidth
                    $y = $canvasHeight - ($data.MemData[$i] / 100) * $canvasHeight
                    $memGraph.Points.Add([System.Windows.Point]::new($x, $y))
                }
            }
            
            # Update stats less frequently
            if ((Get-Date).Second % 2 -eq 0) {
                $threadCount.Text = (Get-Process | ForEach-Object { $_.Threads.Count } | Measure-Object -Sum).Sum
                $processCount.Text = (Get-Process).Count
                $uptimeSeconds = (Get-CimInstance Win32_OperatingSystem).LocalDateTime - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
                $uptime.Text = "{0}h {1}m" -f [math]::Floor($uptimeSeconds.TotalHours), $uptimeSeconds.Minutes
            }
        }
    }
})

$timer.Start()

# Cleanup on close
$window.Add_Closing({
    $timer.Stop()
    Stop-Job $bgJob -ErrorAction SilentlyContinue
    Remove-Job $bgJob -ErrorAction SilentlyContinue
    $cpuCounter.Dispose()
})

# Show dialog
$window.ShowDialog() | Out-Null
