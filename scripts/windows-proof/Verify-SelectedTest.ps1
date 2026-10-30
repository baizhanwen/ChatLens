# Fixed portable runtime; private paths and keys stay on this PC.
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$script:FailureReason = 'selected_test_launcher_failed'
$locationPushed = $false

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
    $manifestPath = Join-Path $PackageRoot 'SELECTED-MANIFEST.json'
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
        if ($lower -eq 'selected-manifest.json' -or $lower -eq 'results' -or $lower.StartsWith('results/') -or $names.ContainsKey($lower)) {
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
    foreach ($required in @('runtime/python.exe', 'runtime/python314.dll', 'runtime/python314.zip', 'runtime/python314._pth', 'runtime/sqlcipher3/_sqlite3.cp314-win_amd64.pyd', 'runtime/zstandard/backend_c.cp314-win_amd64.pyd', 'windows_selected_engine.py', 'windows_selected_runner.py', 'windows_selected_native.py', 'windows_keyproof_core.py', 'windows_keyproof_platform.py', 'Verify-SelectedTest.ps1', 'Start-SelectedTest.cmd', 'README-SELECTED.txt', 'THIRD-PARTY-SELECTED-NOTICES.txt')) {
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
            } elseif ($lower -ne 'selected-manifest.json' -and -not $names.ContainsKey($lower)) {
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
    Write-Host '文件完整性检查完成。本包使用固定便携 Python，无需安装，也不使用其他 Python。'
    Write-Host '本次只验证你在本机选定的私聊和测试群，共四条新文字；不做历史导出或停机恢复验证。'
    Write-Host '程序先在本机进行虚构加密库及 WAL 冒烟，再初始化所需 message_0/message_1/contact 密钥。'
    Write-Host '初始化会只读检查所选微信进程内存；内存块可能含聊天，仅解析密钥，不保存转储、不上传。'
    Write-Host '随后只用联系人元信息定位两个指定会话，建立新消息基线。不要提前发送测试码。'
    Write-Host '等程序显示 READY 和四个随机测试码，再按程序提示回车计时，并让同一位测试朋友发送。'
    Write-Host '微信保持登录和联网，桌面微信放后台，不打开被测私聊或群。程序不会替你发送消息。'
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object Windows.Forms.OpenFileDialog
    $keyPath = $null
    try {
        $dialog.Title = '选择第三包本机生成的 private/validated-message0-key.json；不要回传此文件。'
        $dialog.Filter = 'validated-message0-key.json|validated-message0-key.json'
        $dialog.CheckFileExists = $true
        $dialog.Multiselect = $false
        $dialog.RestoreDirectory = $true
        if ($dialog.ShowDialog() -eq [Windows.Forms.DialogResult]::OK) { $keyPath = $dialog.FileName }
    } finally { $dialog.Dispose() }
    if ($null -eq $keyPath) { Stop-Check 'local_private_key_selection_cancelled' }
    $script:FailureReason = 'private_key_metadata_validation_failed'
    $config = Read-SmallJson $keyPath 16384
    if ($config.schema_version -ne 1) { Stop-Check 'private_key_schema_unsupported' }
    $expectedExe = [string]$config.exe_path
    if (-not (Local-File $expectedExe) -or [IO.Path]::GetFileName($expectedExe) -notmatch '^(Weixin|WeChat)\.exe$') {
        Stop-Check 'private_key_executable_not_local_or_supported'
    }
    # Never use the historical PID. Match the exact locally saved executable.
    $script:FailureReason = 'current_main_process_revalidation_failed'
    $candidates = @()
    $rows = @(Get-CimInstance -ClassName Win32_Process -Filter "Name='Weixin.exe' OR Name='WeChat.exe'" -Property 'ProcessId', 'ExecutablePath' -ErrorAction Stop)
    foreach ($row in $rows) {
        try {
            $currentExe = [string]$row.ExecutablePath
            if (-not [string]::Equals($currentExe, $expectedExe, [StringComparison]::OrdinalIgnoreCase)) { continue }
            if (-not (Local-File $currentExe)) { continue }
            $version = [string][Diagnostics.FileVersionInfo]::GetVersionInfo($currentExe).ProductVersion
            if ($version -cne '4.1.15.56') { continue }
            $process = Get-Process -Id ([int]$row.ProcessId) -ErrorAction Stop
            if ($process.ProcessName -ine 'Weixin' -and $process.ProcessName -ine 'WeChat') { continue }
            if ($process.MainWindowHandle -eq [IntPtr]::Zero) { continue }
            $candidates += $process
        } catch { continue }
    }
    if ($candidates.Count -eq 0) { Stop-Check 'matching_current_main_window_not_found' }
    $selected = 0
    if ($candidates.Count -gt 1) {
        Write-Host '找到多个符合已保存程序路径和版本的主窗口候选；请选择本次已登录微信。'
        for ($index = 0; $index -lt $candidates.Count; $index++) {
            Write-Host ('候选 ' + ($index + 1) + '：PID=' + $candidates[$index].Id)
        }
        $answer = Read-Host '输入候选编号，留空取消'
        $choice = 0
        if (-not [int]::TryParse($answer, [ref]$choice) -or $choice -lt 1 -or $choice -gt $candidates.Count) {
            Stop-Check 'current_main_process_selection_cancelled_or_invalid'
        }
        $selected = $choice - 1
    }
    $process = $candidates[$selected]
    $processNumber = [int]$process.Id
    $creationTime = [long]$process.StartTime.ToFileTimeUtc()
    if ($creationTime -le 0) { Stop-Check 'selected_process_creation_time_unavailable' }
    $resultsRoot = Join-Path $packageRoot 'results'
    $existingResults = Get-Item -LiteralPath $resultsRoot -Force -ErrorAction SilentlyContinue
    if ($null -ne $existingResults) {
        if (-not (Local-Directory $resultsRoot)) { Stop-Check 'local_results_parent_required' }
    } else { [void](New-Item -ItemType Directory -Path $resultsRoot -ErrorAction Stop) }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $outputDirectory = Join-Path $resultsRoot ($stamp + '-selected-live')
    $runtime = Join-Path $packageRoot 'runtime\python.exe'
    $core = Join-Path $packageRoot 'windows_selected_runner.py'
    $creationText = $creationTime.ToString([Globalization.CultureInfo]::InvariantCulture)
    $processText = $processNumber.ToString([Globalization.CultureInfo]::InvariantCulture)
    $script:FailureReason = 'fixed_runtime_selected_test_failed'
    # Preserve live stdout and the console stdin: the fixed runner is interactive.
    $savedPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $runtime '-I' '-S' '-B' '-X' 'utf8' $core '--key-file' $keyPath '--pid' $processText '--creation-time' $creationText '--output' $outputDirectory 2>$null
        $nativeExit = $LASTEXITCODE
    } finally { $ErrorActionPreference = $savedPreference }
    if ($null -eq $nativeExit) { Stop-Check 'fixed_runtime_exit_code_unavailable' }
    Write-Host ('测试步骤已结束；退出码：' + $nativeExit)
    Write-Host ('如已生成，请只回传：results\' + $stamp + '-selected-live\result-to-send.json')
    Write-Host '不要发送 private 文件夹、validated-message0-key.json 或其他密钥文件。'
    Write-Host '四条受控消息是否读取成功，以本次脱敏报告为准。'
    exit $nativeExit
} catch {
    Write-Host '指定会话测试未完成。'
    Write-Host ('reason=' + $script:FailureReason + '; exception_type=' + $_.Exception.GetType().FullName)
    Write-Host '没有新增的后台读取通过结论。只反馈固定状态，不发送私有路径、配置或密钥。'
    exit 2
} finally {
    if ($locationPushed) { try { Pop-Location -ErrorAction Stop } catch {} }
}
