ChatLens Windows 环境检测包
日期：2026-10-27

目的
先检查 Windows、PowerShell、正在运行的微信版本和架构。
本包不读取聊天正文、不取密钥、不扫描进程内存、不调用模型或网络服务，
不安装软件、不提升权限、不关闭或重启微信。环境检测结果不代表微信读取通过。

怎么运行
1. 将整个 ZIP 拷到 Windows 电脑，并先完整解压。
2. 在 Windows 上正常登录微信，保持微信运行。
3. 双击 Start-Check.cmd，用普通方式运行即可。
4. 等窗口显示检测结果。结果文件位于本包 results 下的本次时间目录。
5. 将其中 result-to-send.json 的内容复制回本聊天，或把这个 JSON 文件发回来。
   只回传这个脱敏结果，不需要发送微信数据库、密钥或个人聊天材料。

运行条件
- 面向 Windows 10／11；实际结果需在你的电脑验证。
- 使用 Windows 自带的 Windows PowerShell，目标兼容 5.1；不依赖 Codex。
- 不要求先安装 Python、Node.js 或第三方 PowerShell 模块。
- 启动器的执行策略参数只用于本次进程，不写入全局策略。

结果包含什么
系统版本、架构、PowerShell 版本、是否以管理员运行、微信主进程数量和版本，
以及固定错误编号。Python 和资料目录自动发现本版不执行，结果保留“未验证”。
结果不应包含用户名、电脑名、安装绝对路径、联系人、会话名、正文或密钥。
后续按需要在本机确认 Python 和资料位置；本版不会全盘搜索。

如果没成功
请保留窗口，把窗口显示的错误编号或 result-to-send.json 发回来。
目前 Mac 上无法执行 Windows 专属 API；脚本经代码审查，Windows 实机尚待验证。

下一步
收到环境信息后，再准备匹配该 Windows／微信版本的本地读取初始化与受控消息测试。
届时沿用“后台读取＋私聊／群聊＋停止后恢复”的验收口径，分别记录实机结果。

包内文件
Check-Windows.ps1        只读环境检测
Start-Check.cmd          使用系统 PowerShell 启动检测并保留窗口
README-FIRST.txt        本说明
PACKAGE-MANIFEST.json   源码文件哈希和包的范围说明
