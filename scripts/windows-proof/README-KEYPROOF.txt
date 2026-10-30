ChatLens Windows 单库密钥校验（实验验证包）
日期：2026-10-29

目标
第二包已在你的电脑通过。此包只验证你选定账号的 message/message_0.db
首页 HMAC，不解密数据库，不打开聊天表，不读取 WAL，不分析消息。
当前只允许 Windows x64 + 微信 4.1.15.56。该版本的内存布局尚待实机验证。

这一轮会做什么
- 从你在本机选择的第二包 private/config.json 取得进程与账号目录。
- 核对当前进程路径、版本、创建时间与 x64 架构。
- 只读扫描选定微信进程的可读内存，定位 Config.Cipher 密钥结构。
  内存块可能包含聊天内容；工具只解析密钥结构，不保存或输出其他内容。
- 只读取指定 message_0.db 的前 4096 字节密文，核对完整 HMAC。
- 验证成功才把该库的密钥写入当前用户受保护的本机 private 目录。

不会安装或更新系统 Python，不使用已有 Python，不安装 pip，不联网，
不提权，不注入、不暂停、不关闭或重启微信，不自动切换其他账号。
不生成内存 dump，不解密或导出数据库，不调用模型或上传资料。
扫描最多 90 秒、累计请求内存最多 2 GiB，单次不超过 1 MiB。
达到上限或找不到兼容布局会停止，不会自动扩大路线。

运行步骤
1. 将整个 ZIP 解压到 Windows 本机普通文件夹，如“下载”。不能直接在 ZIP 内运行。
2. 保持刚才的微信登录，不要重启或切换账号。
3. 双击 Start-KeyProof.cmd，不需要管理员方式启动。
4. 弹出文件选择窗口时，选择“第二个检测包”里本次结果的：
   results\时间-reader-setup\private\config.json
   这是在 Windows 本机选择，不需要把 config.json 发给我。
5. 等待窗口显示完成，只回传“第三个包”内本次生成的：
   results\时间-keyproof\result-to-send.json
6. private 文件夹、密钥和数据库留在 Windows 本机，不要回传。

如果显示进程已改变、版本不符或找不到 config，请先回传窗口中的静态原因。
如果你已重启微信，需要先重新运行第二包得到当前进程配置。

便携环境与来源
包内是 Python 官方 3.15.2 Windows x64 embeddable，运行限本目录；
原始 ZIP 的 SHA256、来源和包内文件哈希记在 KEYPROOF-MANIFEST.json。
runtime\python315._pth 仅增加一行 ..，用于导入包根两个自有模块；
保留 site 禁用，启动固定使用 -I -S -B -X utf8，不读取系统 Python 环境。
Python 许可证见 runtime\LICENSE.txt，算法来源和许可见 THIRD-PARTY-NOTICES.txt。

证据边界
Mac 上的合成测试、源码审查及 ZIP 检查，不等于 Windows API 已运行。
即便首页 HMAC 通过，也只证明这一个库的候选密钥有效；
message_1、联系人库、WAL、会话归属、后台新消息与重启补采仍需独立实测。
