# 利明智耀 · ChatLens Web 工作台

首页为基于已采文字的 AI 对话：今天重点、待跟进客户、最近约定与风险都可以直接提问。保留会话消息和采集状态页面，统一采用暖白、鼠尾草绿与深森林绿设计及利明智耀标识。

消息数、发送者、排行、会话分布与最近消息先走本机统计，无需模型配置；分析问题继续使用已配置模型，混合问题先查询再分析。输入时会显示实际日期与会话计划，支持“那昨天呢”等结构化追问。统计总数来自完整匹配数据，引用仅为代表原消息；身份和日期不完整时明确说明。详见[查询分流与统计](../../docs/查询分流与统计.md)。

## 本机使用

从项目根目录 `Start-ChatLens.command` 打开 Mac 本机入口，按页面完成当前登录微信的识别、首次本机初始化和会话选择。Windows r3从 `Start-ChatLens.cmd` 打开接入引导，复用旧 `collector-data`，区分程序、账号读取、配置、范围和实际采集阶段；本人登录后可同页重新检测并开始。未就绪时说明原因，原消息可以明确离线浏览，工作台始终提供连接引导入口。步骤见 [Windows新版测试包](../../docs/Windows新版测试包.md)，旧交付包不因源码更新而自动具备这些功能。Mac 接入步骤见 [本机 Mac Web](../../docs/本机MacWeb.md)。

Mac及Windows r3设置或侧栏的「管理关注范围」可多选1–50个私聊／群聊，跨搜索保留选择；每个账号和关注范围分别保存数据与 AI 历史，旧范围可切回。Windows未完成账号读取核验时入口仍可找到，但会说明为何暂不能修改。本人身份核验后原消息标明“本人／收到”，无法核验的方向明确显示未知；统计和引用使用同一判断。见[收发身份与多会话](../../docs/收发身份与多会话.md)。

工作台使用启动器提供的 `http://127.0.0.1:端口` 地址。登录片段在建立 HttpOnly 本机会话后移除；不要分享完整登录地址。普通静态 HTTP 服务没有本项目 API，会显示连接错误。

## 连接 MiniMax 或其他模型

从侧栏进入「设置」，在模型配置区域选择 MiniMax 国内、国际或自定义服务，填写 API 地址、模型和 API Key，再明确点击「保存到本机」。保存配置不会自动测试或发送聊天。需要核对连接时，点击「测试已保存配置」；测试不带聊天文字。

MiniMax 预设为 `https://api.minimax.cn/v1`、`https://api.minimax.io/v1`，默认模型 `MiniMax-M3.1-Flash`，也可编辑模型名称。预设依据 [国内官方接入文档](https://platform.minimax.cn/docs/api-reference/text-chat-openai) 与 [国际官方接入文档](https://platform.minimax.io/docs/api-reference/text-chat-openai)。实际 API 授权与连接是否可用，以用户自己的配置和显式测试为准。

密钥输入框使用 password 类型，保存或离开设置页后清空本次输入，不放入 localStorage／sessionStorage，不回显已保存的原值。离页只清除未保存输入，不删除本机已经保存的密钥；删除仍需用户明确选择并保存。生产配置与对话记录由本机后端保存在受保护目录中，不进入公开源码或证据。

同一 API 地址只更换模型时，会保留已存密钥；更换 API 地址后需重新配置密钥。模型目标变化后，云端聊天片段的发送范围仍需重新确认。

## 提问与证据

- 快捷提问只填入草稿，点击发送才提交。每轮可选择今天、最近 7 天或全部已采文字，以及全部关注会话或其中一个会话。
- 云端模型会先显示当前 HOST 与模型；明确勾选后，才允许发送本次范围内与问题相关的文字片段。更换目标、会话或时间范围后需重新确认。
- 支持多轮、新对话与本机历史。范围改变会开始新的上下文，旧对话仍可在左侧查看。
- 当前生成期间显示真实的检索／生成阶段，完成后显示回答；可以取消，失败后明确重试，没有模拟逐字输出。
- 回答中的 `[E1]` 等证据可点击，消息页会定位并高亮原消息及附近上下文，支持返回 AI 回答或最新消息。引用窗口不覆盖正常采集游标，新消息不会抢走原文阅读位置。
- 知识库只说明已采会话、文字数量、采集起点与最近保存，不代表完整微信历史或训练过模型。未配置模型时提示配置；没有匹配证据时显示系统提示。

消息页继续支持真实会话切换、正文搜索、最新 100 条／更早分页和增量去重。采集状态区分初始化、运行、停止、错误、心跳过期及页面断开；已有内容在断开时保留，并明确提示可能已过期。页面不代发微信消息。

## 独立界面预览

直接打开同目录 `index.html` 的 `file://` 模式仍无需 Python、Node 或 Codex。AI 对话、会话、计数、采集状态均明确为虚构示例，不访问真实聊天或模型，不保存真实模型配置。旧独立 ZIP 仍保留其当时版本，不包含后续后端或 AI 变化。

## 验证

前端为 HTML／CSS／原生 JavaScript，无 Node 构建要求。以下浏览器脚本用于开发验收，需已有 Playwright 与 Chromium，可通过 `CHATLENS_PLAYWRIGHT_MODULE`、`CHATLENS_BROWSER_EXECUTABLE` 指定现有位置；不要求普通用户安装这些工具。

```bash
node scripts/web-preview-smoke.cjs --output .build/new-preview-evidence
node scripts/web-live-smoke.cjs --output .build/new-reader-evidence
node scripts/web-ai-smoke.cjs --output .build/new-ai-evidence
node scripts/web-windows-connection-smoke.cjs --output .build/new-windows-connection-evidence
node scripts/web-windows-multi-smoke.cjs --output .build/new-windows-multi-evidence
```

每次使用新的输出目录，保留历史证据。`file://` 脚本独立验证虚构 AI 和原消息／采集交互；reader 脚本验证实际本机 HTTP 与合成消息库，AI 服务未接入时保持明确不可用；AI 脚本使用实际 `AiAssistant`、`KnowledgeIndex`、临时 `CollectorStore` 与真实本机 HTTP，供应商端为明确的本机合成服务，不模拟浏览器 API 响应。覆盖配置与显式测试、云端确认门槛、多轮／范围／取消／错误重试、引用定位、消息与采集回归及 390px 适配。

上述合成验收不等于真实 MiniMax、Windows 新 AI 包或实际手机已通过验证。真实模型调用仅由用户在本机配置并主动触发；不在聊天里索取 Key，也不把真实正文或登录地址写入公开截图和报告。
