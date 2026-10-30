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
| 第三包便携运行与真实密钥首页校验 | 未验证 | 等待用户 Windows 实机回传；不等同聊天读取 |
| Windows 解密能力初始化 | 未验证 | 需根据实机架构、微信版本和访问条件选择窄化方案 |
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
