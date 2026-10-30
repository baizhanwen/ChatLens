# Fixed-runtime local key prerequisite proof. Never print selected private paths.
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$script:FailureReason = 'keyproof_launcher_failed'
$locationPushed = $false
$child = $null

function Stop-Check {
    param([string]$Reason)
    $script:FailureReason = $Reason
    throw [InvalidOperationException]::new($Reason)
}
function Local-Directory {
    param([string]$PathValue)
    try {
        if ([string]::IsNullOrEmpty($PathValue) -or $PathValue -notmatch '^[A-Za-z]:\\') { return $false }
        $full = [IO.Path]::GetFullPath($PathValue)
        $root = [IO.Path]::GetPathRoot($full)
        $drive = New-Object IO.DriveInfo($root)
        if ($drive.DriveType -ne [IO.DriveType]::Fixed) { return $false }
        $current = $root
        foreach ($part in $full.Substring($root.Length).Split([char]'\', [StringSplitOptions]::RemoveEmptyEntries)) {
            $current = Join-Path $current $part
            $info = Get-Item -LiteralPath $current -Force -ErrorAction Stop
            if (-not $info.PSIsContainer -or ($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { return $false }
        }
        return $true
    } catch { return $false }
}
function Local-File {
    param([string]$PathValue)
    try {
        if (-not (Local-Directory ([IO.Path]::GetDirectoryName($PathValue)))) { return $false }
        $info = Get-Item -LiteralPath $PathValue -Force -ErrorAction Stop
        return (-not $info.PSIsContainer -and ($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0)
    } catch { return $false }
}
function Read-SmallJson {
    param([string]$PathValue, [long]$Maximum)
    if (-not (Local-File $PathValue)) { Stop-Check 'selected_json_not_local_regular_file' }
    $info = Get-Item -LiteralPath $PathValue -Force -ErrorAction Stop
    if ($info.Length -gt $Maximum -or $info.Length -lt 2) { Stop-Check 'selected_json_size_invalid' }
    return ([IO.File]::ReadAllText($PathValue, [Text.Encoding]::UTF8) | ConvertFrom-Json -ErrorAction Stop)
}
function Verify-Payload {
    param([string]$PackageRoot)
    $script:FailureReason = 'manifest_validation_failed'
    $manifestPath = Join-Path $PackageRoot 'KEYPROOF-MANIFEST.json'
    $manifest = Read-SmallJson $manifestPath 262144
    if ($manifest.schema_version -ne 1) { Stop-Check 'manifest_schema_unsupported' }
    $files = @($manifest.files)
    if ($files.Count -lt 6 -or $files.Count -gt 512) { Stop-Check 'manifest_file_count_invalid' }
    $names = @{}
    foreach ($entry in $files) {
        $name = [string]$entry.name
        if ($name -notmatch '^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$') { Stop-Check 'manifest_payload_name_invalid' }
        foreach ($segment in $name.Split([char]'/')) {
            if ($segment -eq '.' -or $segment -eq '..' -or $segment.EndsWith('.') -or $segment -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)') {
                Stop-Check 'manifest_payload_name_refused'
            }
        }
        $lower = $name.ToLowerInvariant()
        if ($lower -eq 'keyproof-manifest.json' -or $lower -eq 'results' -or $lower.StartsWith('results/') -or $names.ContainsKey($lower)) {
            Stop-Check 'manifest_payload_duplicate_or_reserved'
        }
        if ([string]$entry.sha256 -notmatch '^[0-9a-fA-F]{64}$' -or [string]$entry.bytes -notmatch '^\d{1,12}$') {
            Stop-Check 'manifest_payload_digest_or_size_invalid'
        }
        $path = Join-Path $PackageRoot $name.Replace([char]'/', [char]'\')
        if (-not (Local-File $path)) { Stop-Check 'manifest_payload_not_local_regular_file' }
        $info = Get-Item -LiteralPath $path -Force -ErrorAction Stop
        if ([long]$info.Length -ne [long]$entry.bytes) { Stop-Check 'manifest_payload_size_mismatch' }
        $digest = (Get-FileHash -LiteralPath $path -Algorithm SHA256 -ErrorAction Stop).Hash
        if (-not [string]::Equals($digest, [string]$entry.sha256, [StringComparison]::OrdinalIgnoreCase)) { Stop-Check 'manifest_payload_hash_mismatch' }
        $names[$lower] = $true
    }
    foreach ($required in @('runtime/python.exe', 'runtime/python315.dll', 'runtime/python315.zip', 'runtime/python315._pth', 'windows_keyproof_core.py', 'windows_keyproof_platform.py', 'Verify-Key.ps1', 'Start-KeyProof.cmd')) {
        if (-not $names.ContainsKey($required.ToLowerInvariant())) { Stop-Check 'manifest_required_payload_missing' }
    }
    # Enumerate this package only, never results/private or any account folder.
    $queue = New-Object Collections.Queue
    $queue.Enqueue([ordered]@{ path = $PackageRoot; relative = '' })
    $visited = 0
    while ($queue.Count -gt 0) {
        $directory = $queue.Dequeue()
        foreach ($info in @(Get-ChildItem -LiteralPath $directory.path -Force -ErrorAction Stop)) {
            $visited += 1
            if ($visited -gt 1024) { Stop-Check 'manifest_package_entry_limit' }
            if (($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { Stop-Check 'manifest_package_reparse_refused' }
            $relative = $info.Name
            if ($directory.relative -ne '') { $relative = $directory.relative + '/' + $info.Name }
            $lower = $relative.ToLowerInvariant()
            if ($info.PSIsContainer) {
                if ($lower -eq 'results') { continue }
                $queue.Enqueue([ordered]@{ path = $info.FullName; relative = $relative })
            } elseif ($lower -ne 'keyproof-manifest.json' -and -not $names.ContainsKey($lower)) {
                Stop-Check 'manifest_unlisted_payload_refused'
            }
        }
    }
}

try {
    $packageRoot = [IO.Path]::GetFullPath($PSScriptRoot)
    if (-not (Local-Directory $packageRoot)) { Stop-Check 'local_non_reparse_package_directory_required' }
    Verify-Payload $packageRoot
    $nativeArch = $env:PROCESSOR_ARCHITECTURE
    if (-not [string]::IsNullOrEmpty($env:PROCESSOR_ARCHITEW6432)) { $nativeArch = $env:PROCESSOR_ARCHITEW6432 }
    if ($env:OS -ne 'Windows_NT' -or $nativeArch -ne 'AMD64' -or -not [Environment]::Is64BitProcess) { Stop-Check 'windows_x64_process_required' }
    if ([Threading.Thread]::CurrentThread.ApartmentState -ne 'STA') { Stop-Check 'sta_launcher_required' }
    Push-Location -LiteralPath $packageRoot
    $locationPushed = $true
    Write-Host '文件完整性检查完成。下面使用包内固定 Python，无需安装，不使用你电脑上的其他 Python。'
    Write-Host '本步骤将只读检查你选中的微信主进程内存，寻找并核对密钥结构。'
    Write-Host '内存块可能包含聊天内容；程序仅解析密钥，不保存内存转储、不解密聊天正文、不上传。'
    Write-Host '本步骤不提权、不修改或重启微信；密钥前置验证不等于后台聊天读取已通过。'
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object Windows.Forms.OpenFileDialog
    $configPath = $null
    try {
        $dialog.Title = '选择第二个检测包在本机生成的 results/.../private/config.json；不要回传该文件。'
        $dialog.Filter = 'config.json|config.json'
        $dialog.CheckFileExists = $true
        $dialog.Multiselect = $false
        $dialog.RestoreDirectory = $true
        if ($dialog.ShowDialog() -eq [Windows.Forms.DialogResult]::OK) { $configPath = $dialog.FileName }
    } finally { $dialog.Dispose() }
    if ($null -eq $configPath) { Stop-Check 'local_private_config_selection_cancelled' }
    $script:FailureReason = 'private_config_validation_failed'
    $config = Read-SmallJson $configPath 16384
    $processNumber = 0
    if ($config.schema_version -ne 1 -or -not [int]::TryParse([string]$config.selected_pid, [ref]$processNumber) -or $processNumber -le 0) {
        Stop-Check 'private_config_schema_or_pid_invalid'
    }
    $expectedExe = [string]$config.exe_path
    $dbRoot = [string]$config.db_root
    if (-not (Local-File $expectedExe) -or -not (Local-Directory $dbRoot) -or [IO.Path]::GetFileName($dbRoot.TrimEnd([char]'\')) -ine 'db_storage') {
        Stop-Check 'private_config_paths_not_local_or_unverified'
    }
    $script:FailureReason = 'selected_process_revalidation_failed'
    $process = Get-Process -Id $processNumber -ErrorAction Stop
    if ($process.ProcessName -ine 'Weixin' -and $process.ProcessName -ine 'WeChat') { Stop-Check 'selected_process_name_changed' }
    $metadata = Get-CimInstance -ClassName Win32_Process -Filter ('ProcessId=' + $processNumber) -Property 'ProcessId', 'ExecutablePath' -ErrorAction Stop
    $currentExe = [string]$metadata.ExecutablePath
    if (-not (Local-File $currentExe) -or -not [string]::Equals([IO.Path]::GetFullPath($currentExe), [IO.Path]::GetFullPath($expectedExe), [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Check 'selected_process_executable_changed'
    }
    $version = [string][Diagnostics.FileVersionInfo]::GetVersionInfo($currentExe).ProductVersion
    if ($version -cne '4.1.15.56') { Stop-Check 'selected_client_version_not_supported' }
    if ($process.MainWindowHandle -eq [IntPtr]::Zero) { Stop-Check 'selected_main_window_required' }
    $creationTime = [long]$process.StartTime.ToFileTimeUtc()
    if ($creationTime -le 0) { Stop-Check 'selected_process_creation_time_unavailable' }
    $resultsRoot = Join-Path $packageRoot 'results'
    $existingResults = Get-Item -LiteralPath $resultsRoot -Force -ErrorAction SilentlyContinue
    if ($null -ne $existingResults) {
        if (-not (Local-Directory $resultsRoot)) { Stop-Check 'local_results_parent_required' }
    } else { [void](New-Item -ItemType Directory -Path $resultsRoot -ErrorAction Stop) }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $outputDirectory = Join-Path $resultsRoot ($stamp + '-keyproof')
    $runtime = Join-Path $packageRoot 'runtime\python.exe'
    $core = Join-Path $packageRoot 'windows_keyproof_core.py'
    $arguments = '-I -S -B -X utf8 "' + $core + '" --config "' + $configPath + '" --creation-time ' + $creationTime.ToString([Globalization.CultureInfo]::InvariantCulture) + ' --client-version 4.1.15.56 --output "' + $outputDirectory + '"'
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $runtime
    $start.Arguments = $arguments
    $start.WorkingDirectory = $packageRoot
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $script:FailureReason = 'fixed_runtime_launch_failed'
    $child = New-Object Diagnostics.Process
    $child.StartInfo = $start
    [void]$child.Start()
    $stdoutTask = $child.StandardOutput.ReadToEndAsync()
    $stderrTask = $child.StandardError.ReadToEndAsync()
    $child.WaitForExit()
    $stdoutText = $stdoutTask.GetAwaiter().GetResult()
    [void]$stderrTask.GetAwaiter().GetResult()
    $allowed = @(
        'Finished. Send only this run''s result-to-send.json. Keep private on this PC.',
        'Report could not be saved. Do not send private files.',
        'Stopped before report creation. Re-run from a fresh local output folder.')
    foreach ($line in $stdoutText -split '\r?\n') { if ($allowed -ccontains $line) { Write-Host $line } }
    Write-Host ('固定运行时步骤已结束；退出码：' + $child.ExitCode)
    Write-Host ('如已生成，请只回传：results\' + $stamp + '-keyproof\result-to-send.json')
    Write-Host '不要发送 private 文件夹或第二包的 config.json。此步骤仍未验证聊天正文读取。'
    exit $child.ExitCode
} catch {
    Write-Host '密钥前置验证未完成。'
    Write-Host ('reason=' + $script:FailureReason + '; exception_type=' + $_.Exception.GetType().FullName)
    Write-Host '没有后台聊天读取通过结论。请只反馈以上固定状态，不发送私有配置或密钥。'
    exit 2
} finally {
    if ($null -ne $child) { try { $child.Dispose() } catch {} }
    if ($locationPushed) { try { Pop-Location -ErrorAction Stop } catch {} }
}
