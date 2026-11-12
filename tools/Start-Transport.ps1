[CmdletBinding()]
param([ValidateSet('quick', 'widgets')][string]$Frontend = 'quick')

# This file can be dot-sourced by the isolated launcher tests.
function Get-TransportSourceTag {
    param([string]$Root)
    $canonical = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/').ToLowerInvariant()
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($canonical)))).Replace('-', '').ToLowerInvariant().Substring(0, 16) }
    finally { $sha.Dispose() }
}

function Initialize-TransportActivation {
    if ('TransportSourceLauncher.Windows' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace TransportSourceLauncher {
    public static class Windows {
        private delegate bool Callback(IntPtr hwnd, IntPtr parameter);
        [DllImport("user32.dll")] private static extern bool EnumWindows(Callback callback, IntPtr parameter);
        [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern IntPtr GetProp(IntPtr hwnd, string name);
        [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
        [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hwnd);
        [DllImport("user32.dll")] private static extern bool IsIconic(IntPtr hwnd);
        [DllImport("user32.dll")] private static extern bool ShowWindowAsync(IntPtr hwnd, int command);
        [DllImport("user32.dll")] private static extern bool AllowSetForegroundWindow(uint pid);
        [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr hwnd);
        public static bool Activate(string property, uint processId) {
            bool found = false;
            EnumWindows(delegate(IntPtr hwnd, IntPtr unused) {
                uint pid;
                GetWindowThreadProcessId(hwnd, out pid);
                if (GetProp(hwnd, property) != IntPtr.Zero || (processId != 0 && pid == processId && IsWindowVisible(hwnd))) {
                    AllowSetForegroundWindow(pid);
                    if (IsIconic(hwnd)) ShowWindowAsync(hwnd, 9);
                    SetForegroundWindow(hwnd);
                    found = true;
                    return false;
                }
                return true;
            }, IntPtr.Zero);
            return found;
        }
    }
}
'@
}

function Request-TransportSourceActivation {
    param([string]$Tag, [uint32]$ProcessId = 0)
    try {
        $event = [Threading.EventWaitHandle]::OpenExisting("Local\Transport.Source.$Tag.Activate")
        try { [void]$event.Set() } finally { $event.Dispose() }
    } catch [Threading.WaitHandleCannotBeOpenedException] { }
    Initialize-TransportActivation
    [void][TransportSourceLauncher.Windows]::Activate("Transport.Source.$Tag", $ProcessId)
}

function Get-TransportSourceProcesses {
    param([string]$Root)
    $normalized = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/').ToLowerInvariant()
    $mainId = 0
    $uncertainId = 0
    $any = $false
    try {
        # Older launches do not hold the new mutex. Inspect them before touching Git.
        $processes = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction Stop
        foreach ($process in $processes) {
            $command = ([string]$process.CommandLine).Replace('/', '\').ToLowerInvariant()
            $executable = ([string]$process.ExecutablePath).Replace('/', '\').ToLowerInvariant()
            $belongs = $executable.StartsWith($normalized + '\') -or $command.Contains($normalized + '\')
            if (-not $belongs) {
                # A system Python started with a relative entry point does not
                # expose its working directory through Win32_Process. Do not
                # assume it is safe to update or start a second measurement.
                $relativeMain = $command -match '(?:^|\s)-m\s+app\.quick\.application(?:\s|$)'
                foreach ($match in [regex]::Matches($command, '"([^"\r\n]+\.py)"|([^\s"]+\.py)')) {
                    $entry = if ($match.Groups[1].Success) { $match.Groups[1].Value } else { $match.Groups[2].Value }
                    if (-not [IO.Path]::IsPathRooted($entry) -and
                        ($entry -match '(?:^|\\)(transport_ui|main)\.py$|(?:^|\\)app\\quick\\application\.py$|(?:^|\\)app\\ui\\main_window\.py$')) {
                        $relativeMain = $true
                    }
                }
                if ($relativeMain) { $uncertainId = [uint32]$process.ProcessId }
                continue
            }
            $any = $true
            if ($command -match 'transport_ui\.py(?:["\s]|$)|(?:[\\"\s])main\.py(?:["\s]|$)|app\\quick\\application\.py(?:["\s]|$)|app\\ui\\main_window\.py(?:["\s]|$)|(?:^|\s)-m\s+app\.quick\.application(?:\s|$)') {
                $mainId = [uint32]$process.ProcessId
            }
        }
        return [pscustomobject]@{ Known = $true; MainId = $mainId; Any = $any; UncertainId = $uncertainId }
    } catch {
        return [pscustomobject]@{ Known = $false; MainId = 0; Any = $false; UncertainId = 0 }
    }
}

function Show-TransportSourceNotice {
    param([string]$Message, [switch]$Failure)
    Add-Type -AssemblyName System.Windows.Forms
    $icon = if ($Failure) { [Windows.Forms.MessageBoxIcon]::Error } else { [Windows.Forms.MessageBoxIcon]::Information }
    [void][Windows.Forms.MessageBox]::Show($Message, 'Transport Measurement (Source)', [Windows.Forms.MessageBoxButtons]::OK, $icon)
}

function Invoke-TransportSourceBootstrap {
    param([string]$Root, [string]$Frontend, [string]$ReadyName, [string]$LogPath)
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = Join-Path ([Environment]::SystemDirectory) 'cmd.exe'
    # Environment expansion keeps Unicode paths out of generated command syntax.
    $option = if ($Frontend -eq 'widgets') { ' --widgets' } else { '' }
    $info.Arguments = '/d /s /c ""%TRANSPORT_BOOTSTRAP_PATH%"' + $option + ' >"%TRANSPORT_BOOTSTRAP_LOG%" 2>&1"'
    $info.WorkingDirectory = $Root
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.EnvironmentVariables['TRANSPORT_BOOTSTRAP_PATH'] = Join-Path $Root 'Transport_App.bat'
    $info.EnvironmentVariables['TRANSPORT_BOOTSTRAP_LOG'] = $LogPath
    $info.EnvironmentVariables['TRANSPORT_SOURCE_MANAGED_ROOT'] = $Root
    $info.EnvironmentVariables['TRANSPORT_SOURCE_LAUNCHER_PID'] = [string]$PID
    $info.EnvironmentVariables['TRANSPORT_SOURCE_READY_EVENT'] = $ReadyName
    $child = [Diagnostics.Process]::Start($info)
    if (-not $child) { throw 'Could not start the application bootstrap.' }
    try { $child.WaitForExit(); return $child.ExitCode }
    finally { $child.Dispose() }
}

function Start-TransportSource {
    param([string]$Root, [ValidateSet('quick', 'widgets')][string]$Frontend = 'quick')
    $ErrorActionPreference = 'Stop'
    $Root = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $tag = Get-TransportSourceTag $Root
    $launch = New-Object Threading.Mutex($false, "Local\Transport.Source.$tag.Launch")
    $owned = $false
    $running = $ready = $null
    try {
        try { $owned = $launch.WaitOne(0) } catch [Threading.AbandonedMutexException] { $owned = $true }
        if (-not $owned) {
            Request-TransportSourceActivation $tag
            return 0
        }
        $running = New-Object Threading.Mutex($false, "Local\Transport.Source.$tag.Running")
        try { $idle = $running.WaitOne(0) } catch [Threading.AbandonedMutexException] { $idle = $true }
        if (-not $idle) {
            Request-TransportSourceActivation $tag
            return 0
        }
        $running.ReleaseMutex()
        $processes = Get-TransportSourceProcesses $Root
        if ($processes.MainId -ne 0) {
            Request-TransportSourceActivation $tag $processes.MainId
            return 0
        }
        if ($processes.UncertainId) {
            Show-TransportSourceNotice ('A Python application may already be using this checkout, but its working directory cannot be verified.' + "`n`nNo update or new instance was started. Switch to that application, or close it normally before retrying.")
            return 0
        }

        $logDirectory = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) "TransportMeasurement\Source\$tag"
        [void][IO.Directory]::CreateDirectory($logDirectory)
        $logPath = Join-Path $logDirectory 'startup.log'
        if (-not $processes.Known -or $processes.Any) {
            $update = [pscustomobject]@{ Status = 'skipped'; Message = 'Source update skipped: other checkout processes are running or their state could not be verified.' }
        } else {
            . (Join-Path $Root 'tools\Transport-SourceGit.ps1')
            $update = Update-TransportSource $Root
        }
        if ($update.Status -eq 'skipped') {
            Show-TransportSourceNotice ($update.Message + "`n`nStarting the local version. No local changes were discarded.")
        }
        try {
            & (Join-Path $Root 'tools\Update-TransportShortcut.ps1') | Out-Null
        } catch {
            Show-TransportSourceNotice ('Could not create or repair the desktop shortcut. Local startup will continue.' + "`n`n" + $_.Exception.Message)
        }
        $readyName = "Local\Transport.Source.$tag.Ready." + [Guid]::NewGuid().ToString('N')
        $ready = New-Object Threading.EventWaitHandle($false, [Threading.EventResetMode]::AutoReset, $readyName)
        $code = Invoke-TransportSourceBootstrap $Root $Frontend $readyName $logPath
        if ($code -ne 0) { throw "Application setup failed (exit $code). See $logPath" }
        if (-not $ready.WaitOne(30000)) {
            throw "The application did not confirm startup. See $logPath. If an older application is still starting, wait for its window before retrying."
        }
        [IO.File]::WriteAllText((Join-Path $logDirectory 'update.log'), ([DateTime]::Now.ToString('s') + ' ' + $update.Message + [Environment]::NewLine), [Text.Encoding]::UTF8)
        return 0
    } catch {
        Show-TransportSourceNotice $_.Exception.Message -Failure
        return 1
    } finally {
        if ($ready) { $ready.Dispose() }
        if ($running) { $running.Dispose() }
        if ($owned) { $launch.ReleaseMutex() }
        $launch.Dispose()
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    exit (Start-TransportSource (Split-Path -Parent $PSScriptRoot) $Frontend)
}
