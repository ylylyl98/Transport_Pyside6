# Dot-sourcing this file only defines functions; no Git commands run until called.
function Invoke-TransportGit {
    param([string]$Root, [string[]]$Arguments, [int]$TimeoutSeconds = 20)
    # CommandLineToArgvW-compatible quoting, including trailing backslashes.
    $quoted = foreach ($arg in @('-C', $Root, '-c', 'submodule.recurse=false', '-c', 'gc.auto=0', '-c', 'core.hooksPath=NUL') + $Arguments) {
        '"' + ([regex]::Replace([string]$arg, '(\\*)"|(\\+)$', {
            param($m)
            if ($m.Groups[2].Success) { return ($m.Groups[2].Value * 2) }
            return ($m.Groups[1].Value * 2) + '\"'
        })) + '"'
    }
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = 'git.exe'
    $info.Arguments = $quoted -join ' '
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $info.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $info.EnvironmentVariables['GIT_TERMINAL_PROMPT'] = '0'
    $info.EnvironmentVariables['GCM_INTERACTIVE'] = 'Never'
    $info.EnvironmentVariables['GIT_OPTIONAL_LOCKS'] = '0'
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    try {
        if (-not $process.Start()) { throw 'Could not start Git' }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            try {
                $killer = New-Object System.Diagnostics.ProcessStartInfo
                $killer.FileName = 'taskkill.exe'
                $killer.Arguments = '/PID ' + $process.Id + ' /T /F'
                $killer.UseShellExecute = $false
                $killer.CreateNoWindow = $true
                $killProcess = [System.Diagnostics.Process]::Start($killer)
                if ($killProcess) { [void]$killProcess.WaitForExit(3000); $killProcess.Dispose() }
            } catch { try { $process.Kill() } catch { } }
            return [pscustomobject]@{ Code = -1; Out = ''; Error = 'Git command timed out' }
        }
        return [pscustomobject]@{ Code = $process.ExitCode; Out = $stdout.Result.Trim(); Error = $stderr.Result.Trim() }
    } catch {
        return [pscustomobject]@{ Code = -1; Out = ''; Error = $_.Exception.Message }
    } finally { $process.Dispose() }
}

function Update-TransportSource {
    param([string]$Root)
    try {
        $skip = { param($reason) [pscustomobject]@{ Status = 'skipped'; Message = "Source update skipped: $reason"; Reason = $reason } }
        $top = Invoke-TransportGit $Root @('rev-parse', '--show-toplevel')
        if ($top.Code -ne 0 -or -not $top.Out -or
            [System.IO.Path]::GetFullPath($top.Out).TrimEnd('\', '/') -ine [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')) {
            return & $skip 'specified path is not the repository root or Git is unavailable'
        }
        $branch = Invoke-TransportGit $Root @('symbolic-ref', '--quiet', '--short', 'HEAD')
        if ($branch.Code -ne 0 -or $branch.Out -cne 'main') { return & $skip 'current branch is not main' }
        $remote = Invoke-TransportGit $Root @('config', '--get', 'branch.main.remote')
        $merge = Invoke-TransportGit $Root @('config', '--get', 'branch.main.merge')
        if ($remote.Code -ne 0 -or -not $remote.Out -or $remote.Out -eq '.' -or $merge.Code -ne 0 -or $merge.Out -cne 'refs/heads/main') {
            return & $skip 'main has no configured remote main upstream'
        }
        # Reject ambiguous configuration and option-like remote names.
        if ($remote.Out -match '[\r\n]' -or $remote.Out.StartsWith('-')) { return & $skip 'invalid configured remote' }
        $head = Invoke-TransportGit $Root @('rev-parse', '--verify', 'HEAD^{commit}')
        if ($head.Code -ne 0) { return & $skip 'cannot read HEAD' }
        $check = {
            $status = Invoke-TransportGit $Root @('status', '--porcelain=v1', '--untracked-files=all', '--ignore-submodules=none')
            if ($status.Code -ne 0 -or $status.Out) { return 'working tree or index is not clean' }
            foreach ($name in @('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'REBASE_HEAD', 'rebase-merge', 'rebase-apply', 'sequencer', 'BISECT_LOG', 'index.lock')) {
                $path = Invoke-TransportGit $Root @('rev-parse', '--git-path', $name)
                if ($path.Code -ne 0) { return 'cannot inspect Git operation state' }
                $full = if ([System.IO.Path]::IsPathRooted($path.Out)) { $path.Out } else { Join-Path $Root $path.Out }
                if (Test-Path -LiteralPath $full) { return 'another Git operation is in progress' }
            }
            return ''
        }
        $reason = & $check
        if ($reason) { return & $skip $reason }
        $fetch = Invoke-TransportGit $Root @('fetch', '--no-tags', '--no-recurse-submodules', '--no-write-fetch-head', '--', $remote.Out, '+refs/heads/main:refs/transport-update/main') 45
        if ($fetch.Code -ne 0) { return & $skip 'fetch failed or timed out' }
        $target = Invoke-TransportGit $Root @('rev-parse', '--verify', 'refs/transport-update/main^{commit}')
        if ($target.Code -ne 0 -or $target.Out -notmatch '^[0-9a-fA-F]{40,64}$') { return & $skip 'cannot verify fetched commit' }
        $afterBranch = Invoke-TransportGit $Root @('symbolic-ref', '--quiet', '--short', 'HEAD')
        $afterHead = Invoke-TransportGit $Root @('rev-parse', '--verify', 'HEAD^{commit}')
        $reason = & $check
        if ($reason -or $afterBranch.Code -ne 0 -or $afterBranch.Out -cne 'main' -or $afterHead.Code -ne 0 -or $afterHead.Out -cne $head.Out) {
            return & $skip 'repository changed during fetch or is no longer clean'
        }
        if ($target.Out -ceq $head.Out) { return [pscustomobject]@{ Status = 'current'; Message = 'Source is up to date.' } }
        $ancestor = Invoke-TransportGit $Root @('merge-base', '--is-ancestor', $head.Out, $target.Out)
        if ($ancestor.Code -ne 0) { return & $skip 'local main is ahead or diverged from remote main' }
        $result = Invoke-TransportGit $Root @('merge', '--ff-only', $target.Out) 45
        if ($result.Code -ne 0) { return & $skip 'fast-forward failed or timed out' }
        $final = Invoke-TransportGit $Root @('rev-parse', '--verify', 'HEAD^{commit}')
        if ($final.Code -ne 0 -or $final.Out -cne $target.Out) { return & $skip 'could not verify fast-forward result' }
        return [pscustomobject]@{ Status = 'updated'; Message = 'Source updated to remote main.' }
    } catch {
        return [pscustomobject]@{ Status = 'skipped'; Message = 'Source update skipped: unexpected Git error'; Reason = $_.Exception.Message }
    }
}
