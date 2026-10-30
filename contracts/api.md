# ChatLens 本地 API v1

所有路径 `/api/*`，JSON，`Authorization: Bearer <启动token>`；仅回环地址。Electron 主进程代理，renderer 不获 token 或模型密钥。错误 `{error,details?}`；成功对象如下。

- GET `/health` → `{ok,version}`
- GET `/state` → `{paused,mode,last_message_at,analysis_pending,replay,settings,data_dir,message_count}`
- GET `/conversations` → `{conversations:[{id,platform,account_id,source_conversation_id,name,timezone,whitelisted,coverage_start,coverage_end,gaps,message_count,demo}]}`
- GET `/messages?conversation_id=&q=&sender=&start=&end=&limit=100&offset=0` → `{messages:[Message],total,limit,offset}`。conversation_id 使用上面不透明 id。排序 sent_at,id；ISO8601日期。
- GET `/messages/context?id=<message-id>&radius=5` → `{messages:[Message]}`
- GET `/analyses?conversation_id=` → `{analyses:[{id,kind,status,result,input_ids,generated_at,model,prompt_version,...}]}`
- GET `/tasks` → `{tasks:[{id,conversation_id,content,owner,due_at,status,evidence_ids,...}],jobs:[...]}`
- GET `/capabilities` → `{capabilities:<docs/compatibility.json 内容>}`
- POST `/import/preview` `{text,format:'jsonl'|'csv'}` → `{valid_count,errors:[{line,error}],sample:[Message]}`
- POST `/import` `{text,format}` → `{inserted,updated,unchanged,errors}`。部分有效导入，错误行明确返回。
- POST `/replay` `{text,format,speed:1}` → `{running,total,...}`，speed 为事件/秒。`/replay/stop` 暂停；相同输入重启自动续游标。
- POST `/analysis` `{conversation_id,kind:'summary'|'todos'|'customer'|'communication'|'qa'|'daily'|'weekly',question?,start?,end?}` → `{job_id,status}` 异步；GET analyses 轮询。
- POST `/analysis/retry` `{job_id}` → `{job_id,status}`
- POST `/analysis/correct` `{id,result}` → `{ok}` 用户修正，evidence_ids 必须有效。
- POST `/tasks/update` `{id,content?,owner?,due_at?,status?}` → `{task}`；status 为 candidate/confirmed/done/dismissed。
- POST `/settings` `{model_mode:'rules'|'local'|'cloud',model_url?,model_name?,cloud_conversation_ids?:[],retention_days?,analysis_window_seconds?}` → `{settings}`。密钥仅 worker 环境 CHATLENS_MODEL_API_KEY。
- POST `/pause`、`/resume` `{}` → `{paused}`。
- POST `/conversations/update` `{id,whitelisted?,name?,timezone?}` → `{ok}`。
- POST `/conversations/delete` `{id}` → `{ok,deleted_messages}`。
- POST `/export` `{conversation_id?,kind:'daily'|'weekly'|'todos'|'customer'|'messages',format:'markdown'|'json'|'csv',start?,end?}` → `{filename,content,mime}`；Electron 保存文件。

Message 必填输入：platform(wechat/qq)、account_id、conversation_id、sender_id（未知用unknown）、source_id、sent_at（含时区 ISO8601）、type、content；id/observed_at/revision/deleted/capture_mode 可省略采用确定默认。支持 name/conversation_name/timezone/reference_id/attachment_name/confidence/source_fragment/demo。内部 conversation_id 返回复合不透明 id，原始值在 source_conversation_id。

## 视觉主路线与真实窗口停靠

- GET `/capture/health` → `{state,status,capture_mode,scope,reason,gaps,cursor,last_success_at,trigger_count,capture_count,model_calls,skipped_unchanged,next_allowed_at,pending_trigger,watch_changes,monitor_state,...}`。`status`六态独立于进程状态；waiting不等于真实验证通过。
- GET `/capture/windows?platform=wechat` 或 POST `/capture/probe` `{platform}` → `{status,reason,native,window_candidates,capture_route,vision_available:false,...}`，只读元信息，不取截图或原文。renderer通常使用POST。
- POST `/capture/preview` `{platform,window_id,vision_crop:[x,y,w,h],wait_seconds:10}` → `{data_url,scope:'local_preview_only',vision_crop}`。只本地预览，最多15秒等待目标窗口切前台，裁剪失败不回退原图。
- POST `/capture/start` `{platform,account_id,conversation_id,expected_conversation,window_id,acknowledged_scope:true,vision_crop:[x,y,w,h],crop_acknowledged:true,watch_changes:false,watch_roi?,max_wait_seconds?:5}` → capture health；模型取已保存设置。仅显式开始时启用此会话白名单，之后关闭白名单不能被入库回调重新打开。
- POST `/capture/trigger` `{reason:'manual'}` → capture health。其他已定义入口：foreground_transition/content_changed/scroll_stable/idle；接口存在不代表原生事件都已实现。
- POST `/capture/stop` `{}` → health，停止后丢弃模型迟到结果，保存批次checkpoint；重复stop不会复活已删除的checkpoint。
- POST `/capture/dock` `{platform,window_id,bounds:{x,y,width,height}}` → `{ok,status,reason?,changed,original_bounds?,new_bounds?,platform?,window_id?}`。仅系统窗口定位/缩放，未嵌入其他进程；先核对PID与客户端。
- POST `/capture/undock` `{}` → 同上，恢复本次记录的原窗口位置；未有成功停靠记录时返回受限。

设置额外允许：`vision_mode:'disabled'|'local'|'cloud'`、`vision_url`、`vision_name`、`vision_cloud_consent`、`vision_window_id`、`vision_platform`、`vision_account_id`、`vision_conversation_id`、`vision_expected_conversation`、`vision_acknowledged_scope`、`vision_crop`、`vision_crop_acknowledged`、`vision_debounce_seconds`（默认1.2）、`vision_cooldown_seconds`（默认5）、`vision_periodic_fallback_seconds`（默认0）。旧vision_interval不再作为默认采集调度依据。改变vision设置会停止现有采集，需明确重新开始，避免旧授权继续外发。

本地视觉仅回环；云端HTTPS且明确授权，上传前必须本机标题OCR身份门控通过。Windows该门控未实现，云端视觉本版拒绝。视觉模型返回的visible_title还要与expected_conversation确定性核对，不只信任布尔值。无可见原时间时内部sent_at仅取得时间占位，metadata.original_timestamp=null、timestamp_quality=observed_only_not_original。

`on_batch(events,checkpoint)`是适配器持久化集成入口：一帧事件和完整previous/cursor事务写入；`Store.ingest(...,require_whitelisted=True)`在事务内检查白名单。删除会话或过期清理连同含原文匹配片段的checkpoint清除。

Electron `chatlens:layout` 白名单IPC管理主工作台／右侧分析窗布局；停靠不是视觉读取授权。真实聊天仍由用户直接操作原客户端。全局暂停停止采集，恢复不自动开始截图。

### Versioned message contract

Machine-readable schema: `contracts/message.schema.json`. `revision` must increase for edits/recalls. Identical content never serves as the sole identity. `conversation_type` is explicit `direct` or `group`; unknown type never contributes attributed reply intervals. Visible fragments preserve sender/time uncertainty in metadata. Date-only task deadlines remain `YYYY-MM-DD`; the worker does not invent a time of day.

Capture writes use a transactionally enforced whitelist. Explicit start creates/enables one bound conversation; callbacks cannot re-enable a disabled whitelist. `tasks[].evidence_status` is `valid` or `invalid`; stale/withdrawn source results must be presented as invalid until recalculated and confirmed.
