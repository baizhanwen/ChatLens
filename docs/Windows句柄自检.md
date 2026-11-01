# Windows 原生文件元信息自检

这是一项独立诊断，不是新的微信采集包。继续保留原r5包、原`collector-data`和原微信登录状态；本工具只检查自检进程和自建虚构文件，不读取微信、聊天、配置或密钥，不调用模型，不绕过正式账号隔离。版本为`20261101-02`。

## 为什么需要这一步

本次用户回传的连接诊断显示：5个程序树成员均检查完成，枚举3591个句柄、2776项类型名，但识别的`File`类型、数据库路径候选均为0。它说明当前程序未取得可用的文件依据，不能据此判定微信未登录，也不足以确定是名称格式、字符串解析还是Windows接口的文件元信息能力问题。原生自检用于把这些原因进一步分开。

测试电脑AI随后报告：单尾NULL使原File比较失败；修正后识别367个File，但这些句柄仍无对象名称。它还报告了受控Python文件的类似结果。这些属于外部实验报告，原脚本未在本机审阅或运行；不据此宣称所有Windows版本都不提供名称。当前不需要重复该电脑已报告的PSS实验，下一步优先测试独立的文件映射路径接口。

微软的[PSS_HANDLE_ENTRY说明](https://learn.microsoft.com/en-us/windows/win32/api/processsnapshot/ns-processsnapshot-pss_handle_entry)明确类型名和对象名长度均以字节计，字符串不保证有NULL终止符。因此不能只看到`File=0`就把长度换算改成按字符数读取，也不能无限读取到终止符。

## 交给Windows本机AI执行

1. 在独立的ChatLens源码目录取得最新`main`，保留已有本地修改。入口为`scripts/windows-proof/windows_pss_selfcheck.py`，脚本自包含，只依赖Python标准库。
2. 使用此前已核对的r5便携包中`runtime\python.exe`执行，传入`--mapped-path`。无需安装Python、Node、Codex或其他依赖；不启动ChatLens采集服务，不选择私有资料目录。
3. 保存stdout的完整JSON为UTF-8文件，核对`schema_version=1`、`tool_version=20261101-02`、`kind=chatlens_windows_mapped_path_selfcheck`、固定状态和计数，再反馈这个JSON。不要把包含用户名的本地运行路径一起贴回。

PowerShell示例（由本机AI替换下面两个程序路径）：

```powershell
$chatlensPython = 'C:\替换为原r5包\runtime\python.exe'
$chatlensSelfcheck = 'C:\替换为ChatLens源码\scripts\windows-proof\windows_pss_selfcheck.py'
$chatlensResult = & $chatlensPython -I -S -B -X utf8 $chatlensSelfcheck --mapped-path
$chatlensParsed = $chatlensResult | ConvertFrom-Json
$chatlensReport = Join-Path $env:TEMP ('ChatLens-PSS-Selfcheck-' + [guid]::NewGuid().ToString('N') + '.json')
$chatlensResult | Set-Content -Encoding UTF8 $chatlensReport
$chatlensReport
```

最后一行只在本机指出报告位置。报告文件只含固定状态、计数和布尔值，不包含句柄值、PID、原始类型名、文件名、私人路径、微信账号或聊天。

## 映射路径模式

`--mapped-path`只给自身新建的4096字节零文件建立映射，调用`GetCurrentProcess`和`K32GetMappedFileNameW`核对这块自有映射的完整路径。该[官方接口](https://learn.microsoft.com/en-us/windows/win32/api/psapi/nf-psapi-getmappedfilenamew)的容量与返回长度按字符计，返回0、截断、无法完整匹配或清理失败分别保留实际状态。它查询映射的文件名，不读取微信内存页，也不复制其他进程的句柄。

只有自己的映射确实建立、路径完整精确匹配、映射／文件／父临时目录均清理完成时才通过；原始地址、句柄、文件名和路径不输出。独立子进程最长6秒。此项通过只证明这台Windows对这个虚构映射的接口行为，不证明微信数据库存在可用映射，也不证明当前登录账号。

## 原PSS模式（保留用于其他环境）

工具创建并持有一个虚构临时文件，捕获自己进程的句柄元信息。通过内部的已知文件句柄确认该对象是否进入快照，再分别核对类型名、完整路径以及单个末尾NULL的情况；原始字符串只在自检进程内部使用。不会把任意内嵌NULL当作可接受路径。

捕获沿用`0x0c`句柄／名称标记，不克隆地址空间、不读取内存页、不复制其他进程句柄。枚举有数量与用时限制，独立子进程另有6秒时限，结束时释放自有快照、标记和临时文件。

自检通过只表示本机对这个虚构文件的接口行为已经核对，不代表微信账号核验或真实聊天采集通过。非Windows、超时、捕获不完整、缺失文件类型或名称必须保留各自实际状态；源码已独立修正单尾NULL解析；账号路径核验的新路线仍需Windows映射自检结果，不能仅凭自建文件通过就开放微信采集。
