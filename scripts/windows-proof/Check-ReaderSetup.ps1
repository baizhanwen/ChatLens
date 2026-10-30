# Local, interactive prerequisites only. No chat contents, memory or keys are read.
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$script:SafeErrors = @()

function Safe-Error {
    param([string]$Area, [string]$Reason, [System.Exception]$Exception)
    $script:SafeErrors += [ordered]@{ area = $Area; reason = $Reason; exception_type = $Exception.GetType().FullName }
}
function Safe-Version {
    param($Value)
    if ($null -ne $Value -and [string]$Value -match '^\d{1,6}(\.\d{1,6}){1,5}([abrc]+\d{1,3})?$') { return [string]$Value }
    return $null
}
function Safe-Arch {
    param($Value)
    switch (([string]$Value).ToUpperInvariant()) { 'AMD64' { 'x64' }; 'ARM64' { 'arm64' }; 'X86' { 'x86' }; default { 'unknown' } }
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
            $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
            if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { return $false }
        }
        return $true
    } catch { return $false }
}
function Local-File {
    param([string]$PathValue)
    try {
        if (-not (Local-Directory ([IO.Path]::GetDirectoryName($PathValue)))) { return $false }
        $item = Get-Item -LiteralPath $PathValue -Force -ErrorAction Stop
        return (-not $item.PSIsContainer -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0)
    } catch { return $false }
}
function PE-Architecture {
    param([string]$PathValue)
    $stream = $null; $reader = $null
    try {
        if (-not (Local-File $PathValue)) { return 'unknown' }
        $stream = [IO.File]::Open($PathValue, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        $reader = New-Object IO.BinaryReader($stream)
        if ($stream.Length -lt 64 -or $reader.ReadUInt16() -ne 0x5A4D) { return 'unknown' }
        $stream.Position = 60
        $offset = $reader.ReadInt32()
        if ($offset -lt 64 -or $offset -gt ($stream.Length - 6)) { return 'unknown' }
        $stream.Position = $offset
        if ($reader.ReadUInt32() -ne 0x4550) { return 'unknown' }
        switch ($reader.ReadUInt16()) { 0x8664 { 'x64' }; 0x14c { 'x86' }; 0xaa64 { 'arm64' }; default { 'unknown' } }
    } catch { return 'unknown' }
    finally { if ($null -ne $reader) { $reader.Dispose() } elseif ($null -ne $stream) { $stream.Dispose() } }
}
function Save-PrivateConfig {
    param([string]$Directory, $Config)
    $privateFile = Join-Path $Directory 'config.json'
    $ownedDirectory = $false; $ownedFile = $false
    $privateStream = $null; $privateWriter = $null
    try {
        [void](New-Item -ItemType Directory -Path $Directory -ErrorAction Stop)
        $ownedDirectory = $true
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        try { $currentSid = $identity.User } finally { $identity.Dispose() }
        $acl = New-Object Security.AccessControl.DirectorySecurity
        $acl.SetOwner($currentSid)
        $acl.SetAccessRuleProtection($true, $false)
        $rule = New-Object Security.AccessControl.FileSystemAccessRule -ArgumentList @(
            $currentSid, [Security.AccessControl.FileSystemRights]::FullControl,
            [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit',
            [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow)
        [void]$acl.AddAccessRule($rule)
        Set-Acl -LiteralPath $Directory -AclObject $acl -ErrorAction Stop
        $actual = Get-Acl -LiteralPath $Directory -ErrorAction Stop
        $rules = @($actual.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
        if (-not $actual.AreAccessRulesProtected -or $actual.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $currentSid.Value -or $rules.Count -ne 1) {
            throw [IO.IOException]::new('private_acl_verification_failed')
        }
        if ($rules[0].IdentityReference.Value -ne $currentSid.Value -or $rules[0].AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) {
            throw [IO.IOException]::new('private_acl_verification_failed')
        }
        # No actual path has been written before the private directory ACL passed.
        $utf8 = New-Object Text.UTF8Encoding($false)
        $privateJson = $Config | ConvertTo-Json -Depth 6
        $privateStream = [IO.File]::Open($privateFile, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $ownedFile = $true
        $privateWriter = New-Object IO.StreamWriter -ArgumentList @($privateStream, $utf8)
        $privateWriter.Write($privateJson)
        $privateWriter.Flush()
        $privateWriter.Dispose()
        $privateWriter = $null; $privateStream = $null
        $fileAcl = Get-Acl -LiteralPath $privateFile -ErrorAction Stop
        $fileRules = @($fileAcl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
        if ($fileRules.Count -eq 0 -or @($fileRules | Where-Object { $_.IdentityReference.Value -ne $currentSid.Value -or $_.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow }).Count -ne 0) {
            throw [IO.IOException]::new('private_file_acl_verification_failed')
        }
        return $true
    } catch {
        $saveException = $_.Exception
        if ($null -ne $privateWriter) { $privateWriter.Dispose(); $privateWriter = $null; $privateStream = $null }
        elseif ($null -ne $privateStream) { $privateStream.Dispose(); $privateStream = $null }
        if ($ownedDirectory -and $ownedFile) { Remove-Item -LiteralPath $privateFile -Force -ErrorAction SilentlyContinue }
        Safe-Error 'private_config' 'private_config_acl_or_save_failed' $saveException
        return $false
    }
}

try {
    if ($env:OS -ne 'Windows_NT' -or -not (Local-Directory $PSScriptRoot)) {
        Write-Host '未验证：请在 Windows 本机普通目录中运行检测包。'
        exit 2
    }
    if ([Threading.Thread]::CurrentThread.ApartmentState -ne 'STA') {
        Write-Host '待外部条件：sta_required。请双击 Start-ReaderSetup.cmd。'
        exit 2
    }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $result = [ordered]@{
        schema_version = 1; stage = 'reader_setup'; recorded_at_utc = [DateTime]::UtcNow.ToString('o')
        status = '未验证'; status_scope = 'reader_setup_preconditions_only'; reason = 'setup_incomplete'
        business_read_status = '未验证'; chat_messages_read = 0
        os = [ordered]@{ version = $null; build = $null; native_architecture = Safe-Arch $env:PROCESSOR_ARCHITECTURE; process_architecture = Safe-Arch $env:PROCESSOR_ARCHITECTURE }
        powershell = [ordered]@{ version = $PSVersionTable.PSVersion.ToString(); edition = [string]$PSVersionTable.PSEdition }
        process_candidates = @(); selected_process_pid = $null; account_count_status = '未验证'
        process_selection_status = '未验证'; db_storage_selection_status = '未验证'; python_selection_status = '未验证'
        database_metadata = [ordered]@{ message_db_count = $null; wal_count = $null; contact_db_exists = $null }
        python_metadata = [ordered]@{ file_version = $null; architecture = 'unknown'; executable_started = $false; signature_verified = $false }
        private_config_saved = $false; private_acl_verified = $false
        operations = [ordered]@{ process_memory_read = $false; keys_read = $false; database_headers_read = $false; network_requested = $false; installed_dependencies = $false; privileges_elevated = $false; processes_changed = $false }
        errors = @()
    }
    if (-not [string]::IsNullOrEmpty($env:PROCESSOR_ARCHITEW6432)) { $result.os.native_architecture = Safe-Arch $env:PROCESSOR_ARCHITEW6432 }
    try {
        $system = Get-CimInstance -ClassName Win32_OperatingSystem -Property 'Version', 'BuildNumber' -ErrorAction Stop
        $result.os.version = Safe-Version $system.Version
        if ([string]$system.BuildNumber -match '^\d{1,10}$') { $result.os.build = [string]$system.BuildNumber }
    } catch { Safe-Error 'os' 'os_metadata_unavailable' $_.Exception }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class ReaderSetupNative {
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern IntPtr OpenProcess(UInt32 desiredAccess, [MarshalAs(UnmanagedType.Bool)] bool inheritHandle, UInt32 processId);
    [DllImport("kernel32.dll", SetLastError=true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool CloseHandle(IntPtr handle);
    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool IsIconic(IntPtr window);
}
'@
    Add-Type -AssemblyName System.Windows.Forms
    $running = @(Get-Process -Name 'WeChat', 'Weixin' -ErrorAction SilentlyContinue)
    $running = @($running | Where-Object { $_.ProcessName -ieq 'WeChat' -or $_.ProcessName -ieq 'Weixin' } | Sort-Object Id)
    $processIds = @($running | ForEach-Object { [int]$_.Id })
    $privateExecutables = @{}
    foreach ($process in $running) {
        $processNumber = [int]$process.Id
        $row = [ordered]@{ pid = $processNumber; parent_pid = $null; session_id = $null; candidate_kind = 'unknown'
            has_main_window_handle = $null; is_foreground = $null; is_minimized = $null
            product_version = $null; exe_architecture = 'unknown'; query_vm_read_access = $false; open_process_win32_error = $null }
        try {
            $metadata = Get-CimInstance -ClassName Win32_Process -Filter ('ProcessId=' + $processNumber) -Property 'ProcessId', 'ParentProcessId', 'SessionId', 'ExecutablePath' -ErrorAction Stop
            $row.parent_pid = [int]$metadata.ParentProcessId
            $row.session_id = [int]$metadata.SessionId
            $exe = [string]$metadata.ExecutablePath
            if (-not [string]::IsNullOrEmpty($exe) -and (Local-File $exe)) {
                $privateExecutables[$processNumber] = $exe
                $row.product_version = Safe-Version ([Diagnostics.FileVersionInfo]::GetVersionInfo($exe).ProductVersion)
                $row.exe_architecture = PE-Architecture $exe
            }
        } catch { Safe-Error 'process_metadata' 'process_metadata_unavailable' $_.Exception }
        try {
            $window = $process.MainWindowHandle
            $row.has_main_window_handle = $window -ne [IntPtr]::Zero
            if ($row.has_main_window_handle) {
                $row.is_foreground = [ReaderSetupNative]::GetForegroundWindow() -eq $window
                $row.is_minimized = [ReaderSetupNative]::IsIconic($window)
            }
        } catch { Safe-Error 'window_metadata' 'window_boolean_metadata_unavailable' $_.Exception }
        if ($null -ne $row.parent_pid) {
            if ($processIds -contains $row.parent_pid) { $row.candidate_kind = 'child_or_helper_candidate' }
            elseif ($row.has_main_window_handle -eq $true) { $row.candidate_kind = 'main_with_window_candidate' }
            else { $row.candidate_kind = 'root_without_window_candidate' }
        }
        $handle = [ReaderSetupNative]::OpenProcess([uint32]0x410, $false, [uint32]$processNumber)
        if ($handle -eq [IntPtr]::Zero) { $row.open_process_win32_error = [Runtime.InteropServices.Marshal]::GetLastWin32Error() }
        else {
            $row.query_vm_read_access = $true
            $row.open_process_win32_error = 0
            [void][ReaderSetupNative]::CloseHandle($handle)
        }
        $result.process_candidates += $row
    }
    Write-Host ''
    Write-Host '本检测只确认读取前置条件，不读取聊天、内存或密钥，不安装或重启任何程序。'
    Write-Host '以下为进程候选，候选数量不代表微信账号数量：'
    for ($index = 0; $index -lt $result.process_candidates.Count; $index++) {
        $row = $result.process_candidates[$index]
        Write-Host ('[' + ($index + 1) + '] PID=' + $row.pid + ' parent=' + $row.parent_pid + ' kind=' + $row.candidate_kind + ' window=' + $row.has_main_window_handle + ' version=' + $row.product_version + ' read_access=' + $row.query_vm_read_access)
    }
    $selected = $null; $dbRoot = $null; $pythonPath = $null
    if ($result.process_candidates.Count -gt 0) {
        $choice = Read-Host '请输入待验证主进程候选的编号（留空则跳过）'
        $number = 0
        if ([int]::TryParse($choice, [ref]$number) -and $number -ge 1 -and $number -le $result.process_candidates.Count) {
            $selected = $result.process_candidates[$number - 1]
            $result.selected_process_pid = $selected.pid
            $result.process_selection_status = '通过'
        }
    }
    if ($null -ne $selected) {
        $folder = New-Object Windows.Forms.FolderBrowserDialog
        try {
            $folder.Description = '选择本机当前微信账号的 db_storage 文件夹；不选择 xwechat_files 总目录。可取消。'
            $folder.RootFolder = [Environment+SpecialFolder]::MyComputer
            $folder.ShowNewFolderButton = $false
            if ($folder.ShowDialog() -eq [Windows.Forms.DialogResult]::OK) {
                $chosen = $folder.SelectedPath
                if ((Local-Directory $chosen) -and [IO.Path]::GetFileName($chosen.TrimEnd([char]'\')) -ieq 'db_storage') {
                    $messageDirectory = Join-Path $chosen 'message'
                    if (Local-Directory $messageDirectory) {
                        $files = @(Get-ChildItem -LiteralPath $messageDirectory -File -Force -ErrorAction Stop | Select-Object -First 2049)
                        if ($files.Count -le 2048) {
                            $safeFiles = @($files | Where-Object { ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0 })
                            $result.database_metadata.message_db_count = @($safeFiles | Where-Object { $_.Name -match '^message_[0-9]+\.db$' }).Count
                            $result.database_metadata.wal_count = @($safeFiles | Where-Object { $_.Name -match '^message_[0-9]+\.db-wal$' }).Count
                            $result.database_metadata.contact_db_exists = Local-File (Join-Path $chosen 'contact\contact.db')
                            if ($result.database_metadata.message_db_count -gt 0) {
                                $dbRoot = $chosen
                                $result.db_storage_selection_status = '通过'
                            }
                        }
                    }
                }
            }
        } catch { Safe-Error 'db_selection' 'selected_directory_metadata_unavailable' $_.Exception }
        finally { $folder.Dispose() }
        $pythonDialog = New-Object Windows.Forms.OpenFileDialog
        try {
            $pythonDialog.Title = '可选：选择已安装的 python.exe。未安装或不确定时直接取消。'
            $pythonDialog.Filter = 'python.exe|python.exe'
            $pythonDialog.CheckFileExists = $true
            $pythonDialog.Multiselect = $false
            if ($pythonDialog.ShowDialog() -eq [Windows.Forms.DialogResult]::OK) {
                $chosenPython = $pythonDialog.FileName
                if ([IO.Path]::GetFileName($chosenPython) -ieq 'python.exe' -and $chosenPython -notmatch '[\\/]WindowsApps[\\/]' -and (Local-File $chosenPython)) {
                    $result.python_metadata.file_version = Safe-Version ([Diagnostics.FileVersionInfo]::GetVersionInfo($chosenPython).FileVersion)
                    $result.python_metadata.architecture = PE-Architecture $chosenPython
                    if ($result.python_metadata.architecture -eq 'x64') {
                        $pythonPath = $chosenPython
                        $result.python_selection_status = '通过'
                    }
                }
            }
        } catch { Safe-Error 'python_selection' 'selected_interpreter_metadata_unavailable' $_.Exception }
        finally { $pythonDialog.Dispose() }
    }
    $resultsRoot = Join-Path $PSScriptRoot 'results'
    $existingResults = Get-Item -LiteralPath $resultsRoot -Force -ErrorAction SilentlyContinue
    if ($null -ne $existingResults -and -not (Local-Directory $resultsRoot)) { throw [IO.IOException]::new('local_results_directory_required') }
    $runDirectory = Join-Path $resultsRoot ($stamp + '-reader-setup')
    [void](New-Item -ItemType Directory -Path $runDirectory -ErrorAction Stop)
    if ($null -ne $selected -and $null -ne $dbRoot) {
        $exePath = $null
        if ($privateExecutables.ContainsKey([int]$selected.pid)) { $exePath = $privateExecutables[[int]$selected.pid] }
        $privateConfig = [ordered]@{ schema_version = 1; run_id = $stamp; selected_pid = $selected.pid; exe_path = $exePath; db_root = $dbRoot; python_path = $pythonPath }
        $saved = Save-PrivateConfig (Join-Path $runDirectory 'private') $privateConfig
        $result.private_config_saved = $saved
        $result.private_acl_verified = $saved
        if ($saved -and $selected.query_vm_read_access -and $null -ne $exePath) {
            $result.status = '通过'; $result.reason = 'selected_local_preconditions_collected_only'
        } elseif (-not $saved) { $result.status = '失败'; $result.reason = 'private_config_acl_or_save_failed' }
        else { $result.status = '待外部条件'; $result.reason = 'selected_process_read_access_or_executable_unverified' }
    } else { $result.status = '未验证'; $result.reason = 'process_or_db_storage_selection_incomplete' }
    $result.errors = @($script:SafeErrors)
    $publicFile = Join-Path $runDirectory 'result-to-send.json'
    $utf8 = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($publicFile, ($result | ConvertTo-Json -Depth 12), $utf8)
    Write-Host ''
    Write-Host ('前置条件检测：' + $result.status + '；业务读取仍为未验证，聊天消息读取 0 条。')
    Write-Host ('请只回传：results\' + $stamp + '-reader-setup\result-to-send.json')
    Write-Host '不要发送 private 文件夹；其中只有你本机选择的路径配置，没有密钥。'
    exit 0
} catch {
    Write-Host '前置条件检测：失败。'
    Write-Host ('原因：reader_setup_operation_failed；异常类型：' + $_.Exception.GetType().FullName)
    Write-Host '未验证微信聊天读取；未执行安装、提权或重启。'
    exit 2
}
