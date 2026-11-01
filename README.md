# 利明智耀.Chatlens

本项目已于 2026-10-30 迁移为独立目录和 Git 仓库，当前本机位置为 `~/Developer/ChatLens`。代码、测试、历史证据、交付包和本机私有资料均已保留。源码与已审查的脱敏验证记录纳入 Git；密钥、私有资料、虚拟环境、构建产物、旧截图日志及二进制交付文件留在本地并由 `.gitignore` 排除。迁移说明见 [独立项目迁移记录](docs/独立项目迁移记录.md)。

首页现为 **AI 对话工作台**：已采集的聊天作为本机可检索知识库，用于总结重点、整理跟进与约定；支持多轮、会话／时间范围和可跳转原消息的回答引用。MiniMax 地址、模型与 API Key 在侧栏「设置 → 模型配置」中由本人填写，保存不调用模型，测试连接不带聊天；正式提问只发送明确确认范围内的相关片段，不训练模型、不自动上传整套历史。说明和官方接口来源见 [AI 聊天知识库](docs/AI聊天知识库.md)。本机 Mac 已完成真实 MiniMax 连接、一轮基于4条已采文字的回答与4处引用原文核对；其他客户环境、Windows新AI交付包及长期质量仍单独验收。

问题现先经过本机查询规划：人数、消息数、活跃排行、会话分布、最近消息与日期趋势直接查询完整匹配数据，不需要模型；总结与业务判断交给模型，混合问题先统计再分析。支持自然语言日期、已选会话／发送人／字面内容筛选及结构化追问，计划和历史展示实际日期。无法完整解释的条件先分析或澄清，不忽略条件后给出精确数字。范围和本人身份等边界见 [查询分流与统计](docs/查询分流与统计.md)。

Mac 和新版 Windows Web 测试包可在「设置 → 管理关注范围」选择1–50个私聊或群聊。每账号、每关注范围独立保存消息、检查点和 AI 历史，可切回旧范围；切换使旧页面请求立即失效，运行中识别到另一个微信账号时先断开旧域，不复制旧账号的配置。本人身份经账号候选与所选私聊发送者 ID 交叉核验后，收到和发出分开统计，未知方向独立保留；无需重写消息库或重新建立旧基线。说明见[收发身份与多会话](docs/收发身份与多会话.md)。这些新增模块均使用标准库，没有新增第三方安装依赖。

新版 Web 保留暖白、鼠尾草绿与深森林绿界面及利明智耀标识。Windows 当前测试包为 **`ChatLens-Windows-Web-20261101-r6.zip`**，见 [GitHub 测试发行版](https://github.com/baizhanwen/ChatLens/releases/tag/windows-web-20261101-r6)。关闭旧入口后，把新包完整解压到新目录，运行 **`Start-ChatLens.cmd`**，选择原来的整个 **`collector-data`**；原消息、密钥、模型配置、AI历史、基线和检查点原位保留。r6以只读文件映射元信息核验账号，程序树完整检查两轮并精确匹配原资料根，未知、其他账号或不完整结果均不放行。采集前后台保护与多会话隔离保留。核验就绪后点击「开始采集」，状态来自本轮实际心跳。无需安装Python、Node或Codex，云分析另由本人配置并确认范围。

测试电脑已回传自建文件的映射路径自检通过，证明该接口可用；r6额外的区域分类、真实微信映射、消息收发和恢复仍须独立实测。原r5在该电脑缺少PSS文件名依据，源码单尾NULL修复已包含在r6。不能把一台电脑或自建文件通过概括为所有客户可用：当前客户端限微信4.1.15.56原生x64，其他Windows构建、微信版本、分库布局和第二台Windows分别记录。参见[Windows测试指南](docs/Windows新版测试包.md)、[兼容性验证矩阵](docs/Windows兼容性验证矩阵.md)及[独立原生自检](docs/Windows句柄自检.md)。旧包及历史证据保留。

Mac 入口为根目录 [Start-ChatLens.command](Start-ChatLens.command)，使用本项目已准备的隔离环境。启动后自动检测当前登录的微信账号，首次在本机初始化读取能力，然后在网页选择需要关注的1–50个私聊或群聊；无需客户找账号目录、旧测试配置或密钥文件。每个账号、关注范围的读取配置、消息与检查点分别保存在私有目录。客户另一台 Mac 的依赖安装、终端权限及完整初始化仍需独立验收，不能把这台开发机概括为免依赖分发。步骤和实测边界见 [Mac Web 指南](docs/本机MacWeb.md)。

直接打开 [apps/web/index.html](apps/web/index.html) 的 `file://` 模式仍是虚构前端预览；HTTP 模式连接失败不会回退为虚构数据。只读 API、停止清理、合成加密源与本机 HTTP 浏览器联调分别测试，新 Windows Web 包的真实消息展示仍需独立实机核对，不能由旧采集成功代替。后面的原 Electron 说明和既有客户端专项记录分别保留。界面说明见 [前端 README](apps/web/README.md)，设计变量见 [统一规范](docs/前端设计规范.md)。

本地优先的微信／QQ视觉聊天工作台。提供按需视觉采集、显式关注模式、历史导入、演示回放、原文检索、摘要、待办、客户与沟通分析、检索问答和本地报告导出。

当前交付包含原开发版和后续受控数据库读取工具：源码与合成验收、本机探针、真实客户端专项分别记录。**尚未完成四端真实聊天与真实视觉模型验收，不能声明四端完整支持或全会话实时接入。** 当前数据库读取范围与状态见下节。

## 新版 Web 方向与数据库读取依赖记录

更新于 2026-11-01。上文及后面的启动、视觉读取、Electron 构建说明属于原开发版。后续方向为本机采集服务配合 Web 界面，优先处理选定微信会话在启用后收到的新文字消息。

2026-11-01 Windows 微信 4.1.15.56 已完成两轮选定私聊／群聊后台文字读取，每轮四条，正文、发送人和会话归属均匹配，每条各取得一次。首轮先发消息再开始采集，证明基线后的消息可读回；第二轮记录约 98 秒实际收取阶段，支持采集运行中陆续接收四条新文字。Python 3.14.10、SQLCipher 4.19.0 community／SQLite 3.53.4 的包内加载与合成 WAL／压缩检查通过。上述 r2 两轮未包含停止恢复；持续版最新实测见下一段，严格端到端延迟与长期稳定性仍未验证。无需 Codex 的便携读取已在这台 Windows 执行，尚不等同另一台电脑或普通用户完整安装验收。脱敏证据见 [首轮结果](artifacts/verification/windows-selected/2026-11-01/received-selected-text-summary.json) 与 [第二轮结果](artifacts/verification/windows-selected/2026-11-01/received-active-polling-summary.json)，详见 [Windows 验证记录](docs/Windows环境检测与读取验证.md)。下文带日期的旧状态保留作历史记录。

持续采集与恢复实现已完成，Windows 包为 `artifacts/deliverables/windows-check/ChatLens-Windows-Collector-20261101.zip`，入口 `Start-Collector.cmd`。首次在本机选择成功 r2 的 `private/selected-config.json`，配对密钥在同目录；以后重开同一个包自动复用 `collector-data`。READY 即已采集，不再按开始回车；Ctrl+C 正常停止，恢复从已提交位置补采。两个选定会话的新文字正文与游标一起保存到本机权限保护的普通 SQLite，未加密，不上传或调用模型。85 项新增相关测试、全套 281 项和一万条合成停止恢复对照通过。2026-11-01 实机两阶段也已通过：首次保存四条，重启恢复四条、仅新增停机期间两条，最终六条正文／发送人／会话归属匹配、各一次，基线及旧四条保存时间不变，两次正常停止。范围限这台 Windows／微信 4.1.15.56 的两会话六条与一次正常停止恢复；长期稳定、真实崩溃恢复、锁屏及最小化仍未验证。[实机证据](artifacts/verification/continuous-collector/2026-11-01/received-windows-stop-recovery-summary.json) 和操作边界见 [持续采集与恢复](docs/持续采集与恢复.md)。

已在 macOS 27.0、Apple Silicon arm64、微信 4.1.11（269136）进行两轮本机验证：2026-10-26 在微信隐藏且非前台时完整读取文件传输助手两条文字；2026-10-27 读取用户选定私聊和测试群共六条受控来信，正文、发送人和归属全部匹配、各一次，并在停止后重启补回其中两条、没有重报旧事件。Mac 的另一台独立电脑、最小化、100 条规模及长期稳定性尚未验证；Windows 最新边界见上一段。详见 [单会话报告](docs/微信后台读取验证.md) 与 [私聊群聊报告](docs/私聊群聊后台读取验证.md)。

以下记录用于后续独立交付和最终安装说明；目前仍是验证脚本，尚无普通用户的一键初始化流程。

| 环境或依赖 | 当前用途与已验证范围 |
| --- | --- |
| Codex／ChatGPT | 开发和测试时使用；数据库读取脚本没有其运行依赖，单独读取不需要模型账号 |
| Python | 元信息探针、首次初始化使用本机 Apple Python 3.9.6；只读消息验证使用隔离 Python 3.12.14。最终打包方案待验证 |
| Windows 便携验证运行时 | 第三包捆绑 Python 官方 3.15.2 x64 embeddable，仅在包目录运行，原始 ZIP SHA256 与官方发布页一致；不安装到系统、不安装 pip。2026-10-30 实机完成 message_0 首页密钥校验，正文与 WAL 读取未验证 |
| Windows 选定会话测试依赖 | 第四包自带 Python 3.14.10 官方 x64 embeddable、`sqlcipher3==0.6.4` 普通 cp314 Windows x64 wheel、`zstandard==0.25.0` cp314 wheel。与第三包分开，版本匹配；不安装全局环境。原始摘要固定在清单，Windows 实际加载先做私有虚构 DB／WAL 验证 |
| Windows 持续采集依赖 | 新包复用上述固定 Python／SQLCipher／zstandard，目标私有消息库使用 Python 标准库 `sqlite3`，官方 `_sqlite3.pyd`、`sqlite3.dll` 与标准库包已核对入包。进程／私有 ACL 用 Windows 系统 API，单实例锁用 `msvcrt`；不安装其他 Python、Node、Codex、服务或开机自启 |
| 本机 Web 服务 | 使用同一便携 Python 的标准库 `http.server`／`sqlite3`，只绑定 `127.0.0.1` 随机端口，通过当前运行的随机会话凭据和 HttpOnly Cookie 访问；界面为原生 HTML／CSS／JS，无 npm 或前端构建运行依赖。名称元信息只查询原会话和已采消息中的发送人，不读取其他会话正文 |
| Mac Web 与首次初始化 | 运行使用项目隔离 Python 3.12.14／SQLCipher／zstandard 和只取元信息的 Swift 探针；当前账号由唯一微信进程及其已打开库文件检测，不凭旧账号目录猜测。首次初始化另用 Apple 系统 Python 3.9.6、Xcode 的 LLDB Python 模块、ditto／codesign，仅处理私有临时副本，账号及所需消息分库／联系人库范围显式绑定；本人完成退出和登录。Node／Codex／云端模型不是读取依赖；当前尚未打包这些 Mac 运行时给另一台机器 |
| Apple 开发工具 | 当前 Mac 初始化使用 LLDB 及其 Python 绑定；复制和临时签名使用系统 `ditto`、`codesign`。原生状态探针由 Swift 编译。独立用户需检测相应工具并按官方方式准备，不能假定随 Python 自动提供 |
| `sqlcipher3-wheels==0.5.7` | 在隔离环境中安装二进制 wheel，以 SQLCipher 只读打开加密消息库及 WAL |
| `zstandard==0.25.0` | 在隔离环境中解码压缩的文字正文 |
| 原生状态探针 | 当前测试使用编译后的 `scripts/native/bin/macos-probe` 核对微信是否运行、是否在前台；不以状态探针代替正文读取 |
| Node.js／npm | 原 Electron／React 开发与构建依赖；新版 Web 的开发依赖、构建与普通用户运行依赖将在实现后分别说明 |

首次使用还需要以下本机条件：

1. 微信已登录，电脑正常运行并联网。
2. 实际运行采集程序的应用或终端具有所需文件访问权限。本次测试授予的是宿主 ChatGPT.app 的完全磁盘访问；其他用户和独立发布程序需要在其本机重新授权。
3. 用户在自己电脑上初始化所需数据库密钥。本次 Mac 方式为保存草稿、正常退出微信，仅对私有临时副本签名和调试，必要时由本人手机确认登录，结束后恢复原版。它仍依赖微信版本兼容性，不应作为所有版本均可用的承诺。
4. 密钥及私人资料仅保存在本机私有目录，不随 GitHub 源码、公开证据或发布包分发；云端模型配置属于后续摘要分析环节，与本地读取分别验证。

本轮隔离工具环境为 `.build/wechat-read-venv`，没有修改全局 Python／Node 环境。Windows 初始化、依赖、权限与真实读取需在 Windows 实机验证后补写。最终 README 还需补齐独立启动、路径和账号检测、会话选择、持续运行与恢复、故障处理，以及无 Codex 电脑的安装验收结果。

私聊／群聊验证入口及状态见 [私聊群聊后台读取验证](docs/私聊群聊后台读取验证.md)。新验证代码增加联系人名称精确解析、发送人核对和私有检查点恢复，沿用上述 Python 依赖；名称解析额外需要本机 `contact/contact.db` 的解密能力，与消息库分别初始化、验证。当前检查点锁使用 POSIX `flock`，Windows 文件锁与接入仍需另行适配验收。读取权限与会话范围以用户私有配置为准，公开报告不包含联系人名称、账号标识、正文或密钥。

Windows 先运行独立环境检测包，入口 `scripts/windows-proof/Start-Check.cmd`，使用系统 PowerShell，暂不要求 Python、Node 或 Codex。它仅生成环境元信息，不读取聊天或密钥；Windows 真正后台读取仍是未验证。检测包运行与后续步骤见 [Windows 环境检测与读取验证](docs/Windows环境检测与读取验证.md)。

2026-10-27 已收到第一份 Windows 实机检测结果：Windows 11 x64（build 26200）、PowerShell 5.1、微信 4.1.15.56。检测包执行通过；五个同名进程尚未区分主／辅助角色，Python、数据位置、解密和后台读取仍待后续验证。

下一份前提检测包为 `artifacts/deliverables/windows-check/ChatLens-Windows-ReaderSetup-20261027.zip`，解压后运行 `Start-ReaderSetup.cmd`。它仅检查进程候选和访问条件，由用户在本机选择账号 `db_storage` 与可选已有 Python；不执行 Python、不读取内存、密钥或聊天。实际路径只留本机受保护配置，只回传新生成的 `result-to-send.json`。源码静态审查与 ZIP 完整性检查通过，第二包 Windows 实机执行和业务读取仍为未验证。

2026-10-29 第二包实机执行通过：已选定主窗口候选、只读进程访问通过，所选目录含 2 个消息分库、2 个 WAL 与联系人库，私有配置及 ACL 保存通过。可选 Python 未选择；尚未读取密钥或聊天。新状态详见 Windows 验证文档，旧日期记录保留用于追溯。

下一轮入口为第三包 `Start-KeyProof.cmd`，由用户在 Windows 本机选择第二包 `results/<本次时间>-reader-setup/private/config.json`。工具会只读扫描选定微信进程的内存来定位密钥结构；内存块可能包含聊天，但只解析密钥结构，不保存内存转储，也不解密或解析聊天。只对 `message/message_0.db` 的首页作完整 HMAC-SHA512 校验，成功后该库密钥仅留本机受保护的 `private`。扫描限 90 秒／2 GiB，不自动提权、切换进程或扩大到其他账号、库和读取路线；当前仅允许微信 4.1.15.56。便携运行时保留 PSF 许可证，算法来源及 MIT 许可随包附带；此阶段不依赖 SQLCipher／zstandard 或云端模型，首页校验也不代表后台消息读取通过。

第三包下载产物为 `artifacts/deliverables/windows-check/ChatLens-Windows-KeyProof-20261029.zip`。源码静态复核、23 项合成／WinAPI mock 测试及 ZIP 检查通过；Windows 便携启动、实际密钥校验仍待用户实机回传。后续首次真实消息测试需单独记录会话范围、正文对照与后台状态，不能把首页 HMAC 当作聊天读取通过。

2026-10-30 实机回传已确认：Windows 微信 4.1.15.56、包内 Python 3.15.2 上，选定 `message/message_0.db` 的直接密钥通过完整首页 HMAC 校验，且成功保存到当前用户 ACL 保护的本机私有文件。没有解密数据库、查询聊天、修改进程、提权或联网，消息读取 0。该结果不涵盖其他消息分库、联系人库或后台读取；下一轮按用户选择，仅验证一处私聊和一处测试群的受控新文字。

第四包当前重试版本为 `artifacts/deliverables/windows-check/ChatLens-Windows-SelectedTest-20261030-r2.zip`，入口 `Start-SelectedTest.cmd`，原版 ZIP 保留作历史记录。在 Windows 本机选择第三包 `private/validated-message0-key.json`；工具会匹配当前同路径、同版本的微信主窗口，避免直接复用旧 PID。先验证包内 SQLCipher／WAL／压缩合成样本，再仅校验本账号 message_0、message_1、contact 三库的所需密钥；会话名称由用户本机输入，联系人只作精确元信息定位。建立两个会话的新消息基线并显示 READY 后，按回车开始五分钟测试，让同一位受控发送人各发两条屏幕指定文字。回传本次 `result-to-send.json` 与 `supervisor.json`，若另有 `supervisor-result-to-send.json` 则一并回传；密钥和私人配置留在 Windows。

第四包的 30 项新增合成／WinAPI mock、全套 169 项自动测试及 ZIP 完整性检查通过；尚未在用户 Windows 验证新驱动、联系人解析、两个消息分库及四条受控新文字。只读 SQLite WAL 使用正常锁，可能更新 SHM 锁元信息，不承诺 SHM 零写入；不会写源库记录、主动 checkpoint、重启微信或自动发消息。此包不含停止后恢复、100 条规模或长期稳定性验收。详见 Windows 验证文档及 `artifacts/verification/windows-selected/2026-10-30/`。

r2 修复旧包控制台输入提示可能被 stderr 重定向隐藏的问题：所有提示改由 stdout 明确显示，另记录固定阶段和终端状态。独立父进程监督本次测试子进程，初始化累计 120 秒、输入后定位与基线累计 120 秒、收取阶段 330 秒、最终保存 30 秒；输入等待不计入这些时限。非等待阶段每十秒显示进度，超时仅停止自己启动的测试子进程，不关闭微信。内存扫描原有 90 秒协作式预算仍保留。r2 的 57 项相关测试、全套 196 项通过，含 stderr 隐藏下的终端提示与真实虚构 sleep 子进程硬超时；Windows r2 实机效果及四条真实消息仍未验证。证据在 `artifacts/verification/windows-selected/2026-10-30-r2/`。

## 启动

需要 Node.js 22+、npm、Python 3.9+（本机分别在3.9.6、构建用3.12.14验证），开发仅Python标准库；Windows另需锁定的tzdata。

```bash
cd ~/Developer/ChatLens
sh scripts/dev.sh
```

首次脚本会安装锁定的npm依赖，再启动Electron桌面应用。也可手动执行：

```bash
npm ci --prefix apps/desktop
npm run dev --prefix apps/desktop
```

Windows：`powershell -NoProfile -File scripts/dev.ps1`。缺少Windows环境，此入口及安装、权限、客户端读取仍待实机验收。

## 无模型体验

启动后点击“用虚构样本开始”或“体验演示”，预览后导入。三个会话共1080条完全虚构消息，持续显示演示标识。样本覆盖项目群、客户与个人沟通、相同文本、引用及媒体占位。`fixtures/replay-10000.jsonl`另含10000条乱序、重复、更新与撤回事件，用于恢复测试。

选择会话检索原文，在右侧切换摘要／待办／客户／沟通／问答。每项结论可以点击证据回到上下文；任务候选可修正、确认、完成或忽略。规则模式输出明确标识规则摘录，不冒充AI。报告支持Markdown、JSON、CSV。

## 视觉读取

选择“采集与接入”，先检测平台与窗口，再配置模型、账号、会话、预期可见会话标题和窗口ID。窗口必须属于所选客户端。确认本地裁剪预览包含当前会话标题与正文，排除其他会话预览与输入框；参考裁剪参数不是已经完成四端适配。

默认事件模式：手动启动读取一次，后续手动读取或重新进入前台触发；防抖、最长等待、冷却及相同图像跳过控制调用。默认不周期截图，不开启关注模式。

关注模式必须显式开启。macOS原生组件通过ScreenCaptureKit在本地观察选定窗口ROI变化（1fps），仅在变化后触发识别；这是持续局部画面感知，不能表述为完全不读画面。Windows关注变化监测尚未实现。高速刷屏、窗口遮挡、后台、最小化、锁屏、会话切换可能产生缺口；没有消息重叠时显示可能缺口。

没有视觉模型时该模式不能取得消息；没有可见原始时间时保留取得时间占位和未知标识，不据此解析相对截止日期或计算回复间隔。实际模型识别准确率未验收。

真实聊天小窗采用系统窗口停靠与旁侧分析窗，用户仍直接操作原微信／QQ；跨应用直接嵌入在macOS没有通用受支持方案。辅助功能权限不足时手动摆放。停靠和视觉采集是独立操作，停靠本身不读取聊天。

## 模型配置与隐私

默认本地规则分析，视觉模型禁用。文本与视觉适配器兼容 `/chat/completions` JSON协议；URL填写例如 `http://127.0.0.1:PORT/v1`，型号填写服务提供的精确名称。不同模型多模态支持需真实验证。未自动安装或购买模型。

密钥只通过启动工作进程的环境变量提供：

```bash
# 在本机私密终端设置，不要把真实值写入仓库或聊天。
export CHATLENS_MODEL_API_KEY='<文本模型密钥>'
export CHATLENS_VISION_API_KEY='<视觉模型密钥>'
sh scripts/dev.sh
```

本地模型只允许回环地址。云端需要HTTPS和显式会话／截图范围授权。凭据不传入渲染进程、日志或导出。模型故障不影响保存、导入和检索，失败任务可重试。聊天内的指令只作为数据，不执行工具或消息发送。

Electron渲染进程禁用Node、启用上下文隔离与sandbox，仅白名单IPC；Python仅监听127.0.0.1动态端口且要求随机会话令牌，拒绝浏览器Origin请求。默认无调试端口，测试才显式打开。

数据默认位于Electron用户数据目录下的 `data/chatlens.sqlite3`（设置页显示准确路径，可更换并重启），目录0700、数据库0600（Windows文件权限语义待验收）。SQLite未加密，不宣称加密存储。截图临时文件处理后删除；用户自行导出的报告含原文，属于用户管理的副本。

全局暂停、会话白名单、保留天数和删除会话均在界面可用。删除会话清理消息、FTS、分析、任务及相关采集checkpoint；本应用不自动建立聊天备份。操作系统快照、外部备份、用户手动导出不在应用删除范围。

## 构建与验证

自动测试新生成的合成报告默认写入 `.build/test-evidence/`，不会覆盖 `docs/evidence/` 中的历史报告；需要保留某一轮结果时，通过 `CHATLENS_TEST_EVIDENCE_DIR` 指定独立输出目录。

```bash
python3 -m unittest discover -s tests -v
sh scripts/build_native_macos.sh
sh scripts/build-macos.sh
```

macOS脚本生成包含Python工作进程的arm64应用、DMG和ZIP。原生helper本机编译，应用为未公证开发版；签名、Gatekeeper首次运行、另一台Mac的安装和卸载仍需发布验收。Windows执行 `powershell -NoProfile -File scripts/build-windows.ps1`，须在Windows实机构建和验收，不能用macOS构建替代。

可复现探针：`python3 scripts/probe_clients.py --output .build/current-probe.json`，只读元信息；不会复制聊天库。原生合成窗口测试：`scripts/native/bin/macos-watch --self-test`、`--self-test --pause-test`。

见 [施工状态](docs/施工状态.md)、[兼容性表](docs/compatibility.json)、[接入验证](docs/接入验证记录.md)、[视觉设计](docs/视觉读取设计.md)、[验收报告](docs/验收报告.md)、[已知限制](docs/已知限制.md)、[操作手册](docs/操作手册.md)。原开发版证据保留在 `docs/evidence/`，后续脱敏验证见 `artifacts/verification/`；新测试和探针使用独立输出，个人原文和截图不得提交公开仓库。
