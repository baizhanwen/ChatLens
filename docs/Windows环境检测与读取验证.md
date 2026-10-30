# Windows 环境检测与读取验证

日期：2026-10-27（Asia/Shanghai）。用户已提供可操作的 Windows 电脑；当前助手直接执行环境仍为 Mac，Windows 结果由用户在实机运行后提供。

## 当前交付范围

首先提供独立、无需 Codex／Python／Node 的 PowerShell 环境检测包。它只收集系统、架构、PowerShell、管理员状态布尔值和微信同名进程版本，不读取聊天正文、数据库页头、凭证或进程内存。Python 和资料目录自动发现暂不执行，字段保留未验证，避免搜索任意 PATH 或重定向资料目录。

检测包不安装依赖、不联网下载或上传、不提权、不关闭或重启微信、不创建服务／计划任务、不写入持久执行策略。启动器的 `-ExecutionPolicy Bypass` 仅用于本次进程；该范围参见 [Microsoft 执行策略说明](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_execution_policies?view=powershell-5.1)。

用户操作：完整解压包，保持 Windows 微信已登录，双击 `Start-Check.cmd`。回传 `results/<本次时间>/result-to-send.json`；报告不包含用户名、电脑名、绝对路径、联系人、会话名、正文或密钥。多个进程、未检测到默认资料目录和运行时未知均保留原状态，不自动猜选账号或安装程序。

## 独立记录的状态

| 验证项 | 状态 | 边界 |
| --- | --- | --- |
| 检测源码静态审查、ZIP 白名单／CRC／哈希 | 通过 | 仅本机文件与静态代码检查，未用 PowerShell 解析或在 Windows 执行 |
| 第一版 Windows 环境检查实际执行 | 通过 | 用户回传独立检测包结果：Windows 11 家庭版中文、build 26200、x64；PowerShell 5.1.26100.9444；微信 4.1.15.56，普通用户权限 |
| 第二版前提检测包静态审查与 ZIP 检查 | 通过 | 四文件白名单、CRC、清单哈希和 BOM／CRLF 检查；不代表 PowerShell 解析或 Windows 执行 |
| 第二版进程／资料位置／Python 前提检测实际执行 | 通过 | 2026-10-29 实机回传：选定主窗口候选与只读访问通过，所选账号目录含 2 个消息分库、2 个 WAL 与联系人库，私有配置及 ACL 通过；可选 Python 未选择 |
| 第三包源码／合成测试／ZIP 检查 | 通过 | 23 项合成／WinAPI mock，46 文件白名单、CRC、清单哈希、编码和官方运行时摘要；没有执行 Windows API |
| 第三包便携运行与真实密钥首页校验 | 通过 | 2026-10-30 用户回传 Python 3.15.2／微信 4.1.15.56，message_0 首页 HMAC 和本机私有保存成功；不等同聊天读取 |
| Windows SQLCipher 正文／WAL 读取前提 | 未验证 | 第四包提供独立匹配驱动，实际加载、三库及正文由实机继续验证 |
| 第四包源码、合成与 ZIP 检查 | 通过 | 新增 30 项合成／WinAPI mock、全套 169 项通过，66 文件完整性校验通过 |
| Windows 后台新文字读取 | 未验证 | 没有取得任何 Windows 真实消息证据 |
| Windows 私聊／群聊、停止后恢复 | 未验证 | 后续按与 Mac 分开的受控用例实测 |

源码位于 `scripts/windows-proof/`，白名单打包入口为 `scripts/build_windows_preflight_package.py`。ZIP 只包含检测脚本、启动器、说明和文件哈希清单，不拷贝 Mac 的数据库、密钥、虚拟环境或私人测试配置。

当前检测包：`artifacts/deliverables/windows-check/ChatLens-Windows-Check-20261027.zip`。打包检查证据：`artifacts/verification/windows-check/2026-10-27/package-review.json`。

当前 Mac 无 PowerShell，不能直接执行 Windows 专属 API。Windows 实机状态以用户回传为证，不据静态审查或 ZIP 完整性检查填写实机通过。

## 第一份实机回传与下一步

已收到 2026-10-27 11:36:55（Asia/Shanghai）生成的环境报告，脱敏归档为 `artifacts/verification/windows-check/2026-10-27/received-environment-summary.json`。原始回传仅留在私有目录。

报告包含五个同名 `Weixin` 进程，版本均为 4.1.15.56。第一版只按名称枚举，尚未区分主／辅助进程，不能视为五个账号，也不据此关闭进程。Python、资料目录、进程读取权限和真实聊天读取仍是未验证；本次消息读取数为 0。

下一份前提检测包使用 `Check-ReaderSetup.ps1` 与 `Start-ReaderSetup.cmd`：在本机区分进程候选、仅请求查询／读取权限并立即关闭句柄，由用户选择当前账号 `db_storage` 和可选已有 Python。不会扫描内存或提取密钥。实际路径只保留在受保护的本机私有配置，公共回传仍只包含元信息。

已生成 `artifacts/deliverables/windows-check/ChatLens-Windows-ReaderSetup-20261027.zip`，SHA256 为 `1fc9dee7432e28b34c6161a7afe672f2a12819e22567ef4a5311da80d881b56d`。检查证据为 `artifacts/verification/windows-check/2026-10-27/reader-setup-package-review.json`。通过 `python3 scripts/build_windows_preflight_package.py --stage reader-setup --output <新的ZIP路径>` 可重新打包；已有目标不会覆盖。

解压到 Windows 本机普通文件夹后双击 `Start-ReaderSetup.cmd`。优先选择主窗口候选，无法确定则留空；选择当前账号的 `db_storage`，已有 Python 不明确时取消。只回传本次 `result-to-send.json`，不回传 `private`。Python 只检查文件元信息，不证明解释器真实性或依赖就绪；窗口布尔值仅比较主窗口句柄，不能用作真正后台采集的证据。

2026-10-29 13:26:12（Asia/Shanghai）第二包实机结果已回传并归档，脱敏证据为 `artifacts/verification/windows-check/2026-10-29/received-reader-setup-summary.json`。报告无错误，未读取内存、数据库首页、密钥或消息。原始回传仅留私有目录；实际路径与第二包的 `private/config.json` 继续留在用户的 Windows 电脑。

## 后续读取路线的审查边界

Windows 读取不能直接照搬本机 Mac 初始化或 POSIX 文件锁。当前候选为仅申请进程查询／读取权限、筛选所需数据库候选并作首页 HMAC 验证的窄化实现；现成项目的扫描范围、账号选择、日志和输出行为仍需审查，不自动执行第三方 CLI。

候选算法的一手来源为 [wcdb-key-tool 的 Windows 源码](https://github.com/TANGandXUE/wcdb-key-tool/blob/79f1b5b92e12c66aa281b4a60a3c478b5f547dfa/wcdb_key_tool_windows.py)。对象布局、64 位指针和偏移对版本有要求；源码存在不代表用户实机可用。所需 Python、数据库库及 Windows 探针均在后续确认、隔离配置并记录版本，不选择收费服务。

## 第三包：选定消息库首页密钥校验

2026-10-29 准备第三包 `ChatLens-Windows-KeyProof-20261029.zip`，入口 `Start-KeyProof.cmd`。仅消费用户在 Windows 本机选择的第二包私有配置；重新核对同一进程的路径、版本、x64 架构与创建时间后，只读扫描其内存查找 Config.Cipher 结构，只允许完整 96 位 hex literal 中的 salt 与所选 `message_0` 首页一致的候选，不进行通用原始密钥扫描或调试器回退。

工具只读指定 `message/message_0.db` 的前 4096 字节密文，以 PBKDF2-HMAC-SHA512（2 次、32 字节）派生首页认证密钥，校验完整 64 字节 HMAC。密钥通过后才写入本机当前用户受保护的私有文件；公开报告不含密钥、salt、账号、路径、内存地址或片段。不会 AES 解密、打开 SQLite、查询正文或读取 WAL。需要说明：扫描内存块可能同时接触聊天所在内存，保证的是只解析密钥结构且不保存／输出其他内容，不能宣称没有读过正文所在内存。

扫描总限 90 秒、累计请求 2 GiB、单次 1 MiB；marker、引用、候选和区域查询分别有限额。找不到兼容结构返回未验证／待外部条件，不自动提权、注入、暂停、重启微信或扩大路线。该结构来源按 Windows 4.1.11 布局描述，微信 4.1.15.56 是否兼容仍待实机验证；首页校验成功也不代表另一个消息库、联系人库、WAL、会话归属或新文字读取通过。

运行时来自 [Python 官方 3.15.2 发布页](https://www.python.org/downloads/release/python-3152/) 的 Windows x64 embeddable ZIP，原始 SHA256 为 `d012f3aa29cf07436dae8aea1fec0fd195c2612a0b059b1f915ae0afb9ad1f62`，实际下载摘要匹配。不将摘要相符称作签名信任链验证通过。包保留官方全部运行时文件和许可证，只在 `python315._pth` 增加包根路径 `..`，禁用 site，固定以 `-I -S -B -X utf8` 启动；不使用本机任意 Python、不修改全局环境或安装 pip。算法来源 MIT 许可附于 `THIRD-PARTY-NOTICES.txt`。

本机 23 项合成／WinAPI mock 测试通过，证据为 `artifacts/verification/windows-check/2026-10-29/synthetic-keyproof-tests.txt`；包含完整 HMAC、错误 key、分块／缺口、扫描限额、PID 复用、Win64 类型、路径身份、写前 ACL、既有文件保护，以及公开报告写失败不报成功。它们不代表 Windows API、便携运行时或真实密钥已在用户电脑执行。

交付 ZIP 为 `artifacts/deliverables/windows-check/ChatLens-Windows-KeyProof-20261029.zip`（13,459,576 字节），SHA256 为 `01316297161c51517cdffbe7b777d7deee646b6131c75043ded99e8297b882a4`。仅包含 39 个官方运行时文件、6 个公开脚本／说明和 1 个清单；ZIP CRC、逐文件哈希、来源一致性、编码和私人材料扫描通过，证据为 `artifacts/verification/windows-check/2026-10-29/keyproof-package-review.json`。打包入口为 `python3 scripts/build_windows_keyproof_package.py`（新输出不覆盖旧文件）。Windows 便携启动、系统 API、真实 key 和聊天读取仍为未验证。

## 2026-10-30：旧进程配置需要刷新

用户在 Windows 使用前一天第二包生成的私有配置运行第三包。截图显示包完整性检查通过，但在进程重新核对阶段报 `selected_process_revalidation_failed`，异常类型为 `Microsoft.PowerShell.Commands.ProcessCommandException`。按源码控制流，失败位于 `Get-Process -Id`，尚未启动便携 Python 或内存扫描，消息读取仍为 0。最可能为旧配置记录的进程已退出或重启；没有远程检查当前 Windows 状态，不将推断作为直接实测。

处理：保持当前微信登录，重新运行已有的 `Start-ReaderSetup.cmd`，按最新列表选择主窗口候选，选择同一授权账号目录，Python 可继续取消；随后运行第三包并选择本次新生成的 `private/config.json`。两个步骤之间保持微信运行。配置和密钥留在 Windows 本机，不回传。本次不重打历史包、不更改扫描范围、不自动切换账号或提权。

脱敏记录为 `artifacts/verification/windows-check/2026-10-30/keyproof-process-refresh-required.json`，原始截图仅存私有目录。此失败不能作为密钥、解密或后台读取能力已通过／不支持的依据。

## 2026-10-30：密钥校验成功回传

用户刷新配置后回传 2026-10-30 10:44:08（Asia/Shanghai）生成的第三包报告。Python 3.15.2、微信 4.1.15.56，进程身份与首页读取成功，`key_verification_status=通过`，`private_key_saved=true`、`private_acl_verified=true`。源码成功路径仅在完整首页 HMAC 和受保护文件写入均成功后返回通过；本轮支持选定 message_0 的直接密钥认证和本机保存结论。

仅 1 个候选通过；实际请求读取约 880 MB 进程内存，未保存转储、未解密或解析聊天。进程修改、提权、联网均为 false，消息读取数为 0，业务读取状态仍未验证。不能把这一库的密钥当作其他库的有效密钥，也不能将其视为 SQLCipher 查询、WAL、两会话、后台读取或所有 Windows 版本支持。

脱敏报告为 `artifacts/verification/windows-check/2026-10-30/received-keyproof-success-summary.json`。原始报告仅作私有归档，没有请求或取得用户的私有配置、密钥或数据库。用户下一轮明确选择继续一处私聊与一处测试群；需先验证相关库的密钥、Windows SQLCipher 加载与按名称定位，再建立基线并开始受控发送。

## 第四包：选定私聊和测试群的四条新文字

交付为 `artifacts/deliverables/windows-check/ChatLens-Windows-SelectedTest-20261030.zip`，入口 `Start-SelectedTest.cmd`。ZIP 为 15,448,424 字节、66 文件，SHA256 为 `a72598ff86a7cd18fd89a6f134551369423af9935934d9587c4de014fa4f9e7f`。源码及静态审查、66 文件白名单／CRC／清单哈希、编码和私人材料扫描通过，证据为 `artifacts/verification/windows-selected/2026-10-30/package-review.json`。实际 Windows 读取待用户回传，不能把本机测试替代实机。

本包单独使用 [Python 3.14.10 官方 x64 embed](https://www.python.org/downloads/release/python-31410/)、[sqlcipher3 0.6.4 上游 cp314 Windows wheel](https://pypi.org/project/sqlcipher3/0.6.4/) 和 [zstandard 0.25.0](https://pypi.org/project/zstandard/0.25.0/)，三个原文件的固定摘要在 `SELECTED-MANIFEST.json`。没有把 cp314 扩展放入第三包 Python 3.15；新包自带完整匹配环境，不安装 pip 或修改全局环境，保留 Python、wrapper、SQLCipher BSD3、OpenSSL Apache2.0 和 Zstandard 许可。包构建入口为 `scripts/build_windows_selected_package.py`，既有目标不会覆盖。

运行在本机选择第三包的 `private/validated-message0-key.json`，仅用其中的程序路径和账号目录；启动器重新匹配同路径、4.1.15.56 的当前主窗口，多候选需要本机选择，并用创建时间绑定句柄。本机私有新目录先做与账号无关的加密 DB、已提交／未提交 WAL、只读拒写和压缩冒烟，再认证固定 message_0、message_1、contact 三个首页，内存定位仍限 90 秒／2 GiB。没有扩到其他账号或自动新增库；两个消息分库的集合变化会停止。

两个名称由用户在 Windows 输入，仅通过联系人四字段精确 BINARY 唯一匹配，群和私聊类型分开确认。正式读取只打开两个选定 hash 表和新消息发送人映射；先 MAX(local_id) 建基线，不读取旧正文。程序显示 READY 和四个随机测试码后，用户回车并让同一位受控发送人在私聊和群各发两条。正文、发送人、会话归属、唯一事件和读取前后及最终的后台／未锁屏状态都参与通过判定。后台按前台窗口所属进程判断，包含同 PID 弹窗；未知状态不能确认后台。

本轮只持有 RAM 游标，不提供停止／崩溃恢复。每轮短事务和 ASC 分页，全部查询和状态采样完成后才推进 RAM 游标；忙锁重试不先提交部分进度。基线时不存在的所选表在另一固定分库新建后会从零处理，不查询其他会话正文。正文原值和身份只在内存，公开结果仅 alias、测试编号、匹配布尔、时间和不可逆事件摘要；密钥／配置使用当前用户受保护私有文件。本机 SQLCipher 正常 WAL 锁可能更新 SHM 元信息；非空 WAL 缺 SHM 时预检拒绝，不主动重建 SHM、checkpoint、改源库记录或复制活库当一致快照。

新增 30 项合成测试覆盖实际 SQLCipher／WAL、范围隔离、605 条分页、忙锁回滚、压缩类型、群发送人前缀、新表出现、桌面锁／前台保守判断、固定三库预检、三库密钥筛选及最终背景门控。全套 169 项通过，记录为同目录 `synthetic-validation.json` 和测试日志。Windows 驱动加载、新增两库密钥、联系人解析、四条真实文字及 WAL 实机场景仍待回传；不存在“已全会话实时接入”的结论。

## 第四包 r2：可见输入提示、阶段进度与硬超时

用户报告原版在虚构驱动通过提示后约五分钟没有后续可见输出，手动 Ctrl+C 后得到 `selected_test_interrupted`。这只说明中断已被处理，尚不能确定当时是原生调用等待还是已进入看不见的联系人输入。原始失败描述为 `artifacts/verification/windows-selected/2026-10-30-r2/reported-silent-wait.json`，尚未以未知停点推断读取能力。

确认存在的代码问题：[CPython 3.14.10 的终端输入分支](https://github.com/python/cpython/blob/v3.14.10/Python/bltinmodule.c) 调用 [readline 实现](https://github.com/python/cpython/blob/v3.14.10/Parser/myreadline.c#L279-L311) 时，可将 `input(prompt)` 提示写入 C stderr；原启动器 `2>$null` 会抑制该提示。非终端分支并非同一路径，故不能单凭描述断定本次已触发。r2 保留 stderr 隐私抑制，改为三处 stdout 明确提示并 flush 后调用无提示 input()。

r2 每个阶段写固定白名单进度，包含阶段枚举、UTC 时间和三个 tty 布尔，不包含名称、路径、PID、原文、测试码或密钥。进度文件绑定本次输出名称和父进程创建的随机文件，原子替换；Windows 父读取使用共享 DELETE，避免妨碍替换。独立父进程只监督固定包内 Python 启动的本次 runner，stdin／stdout 继承以保留交互，stderr 丢弃；每十秒在非输入等待阶段显示状态。

时限按组累计：初始配置至三库认证／保存／WAL 检查共 120 秒，输入后的联系人定位及基线共 120 秒，收取阶段 330 秒（内部正常收取仍 300 秒），最终保存 30 秒。不同初始化子步骤不重置总额，未知阶段不能转换成无限等待；三个人工输入阶段没有硬限时。原 90 秒扫描预算仅属协作式检查，不能中断单次阻塞原生调用，所以 r2 外加父进程监督；超时或 Ctrl+C 仅 terminate／必要时 kill 自己持有的 Popen 子进程，不按名字找进程、不动微信。

父进程保存 `supervisor.json`。超时、异常或中断且没有子报告时生成 `result-to-send.json`，已有子报告则保留并另写 `supervisor-result-to-send.json`。未知消息读取数写 null，并标明尚未验证，不能据此断言零读取。保存失败不能作为成功退出。

交付 `artifacts/deliverables/windows-check/ChatLens-Windows-SelectedTest-20261030-r2.zip`，15,457,641 字节、68 文件，SHA256 为 `ff01179fb3e22b0c90b130b0ba1ad54d51bfe7e754cb200cf27cebb7fa800bbd`。原版 ZIP 和原有结果保留。57 项相关合成／WinAPI mock／PTY／子进程测试及全套 196 项通过；包白名单、CRC、逐项摘要、依赖、许可、编码和脱敏核对通过，见同目录 `package-review.json` 与 `synthetic-validation.json`。Windows r2 交互、监督和四条真实新消息仍待实机，不以本机测试替代。
