'use strict';
const {app,BrowserWindow,ipcMain,dialog,Notification,screen}=require('electron');
const {spawn}=require('node:child_process');
const {randomBytes}=require('node:crypto');
const fs=require('node:fs');const path=require('node:path');
const root=path.resolve(__dirname,'../../..');
app.setName('利明智耀.Chatlens');
app.setAboutPanelOptions({applicationName:'利明智耀.Chatlens',applicationVersion:'0.1.0 开发版',credits:'本地优先 · 原文可追溯 · 原创开发版品牌图形',copyright:'利明智耀'});
let worker,win,port,ready=false,originalBounds=null,dockTarget=null; const token=randomBytes(32).toString('hex');
const routes={GET:new Set(['/health','/state','/conversations','/messages','/messages/context','/analyses','/tasks','/capabilities','/capture/health']),POST:new Set(['/import/preview','/import','/replay','/replay/stop','/analysis','/analysis/retry','/analysis/correct','/tasks/update','/settings','/pause','/resume','/conversations/update','/conversations/delete','/export','/capture/probe','/capture/start','/capture/stop','/capture/trigger','/capture/preview','/capture/dock','/capture/undock'])};
if(process.env.CHATLENS_DEBUG_PORT)app.commandLine.appendSwitch('remote-debugging-port',String(Number(process.env.CHATLENS_DEBUG_PORT)));
if(process.env.CHATLENS_SMOKE_DATA)app.setPath('userData',path.resolve(process.env.CHATLENS_SMOKE_DATA));
const preferencesPath=()=>path.join(app.getPath('userData'),'preferences.json');
function readPreferences(){try{return JSON.parse(fs.readFileSync(preferencesPath(),'utf8'));}catch{return {};}}
function dataDir(){return process.env.CHATLENS_DATA_DIR || readPreferences().dataDir || path.join(app.getPath('userData'),'data');}
async function request(method,route,body){
 const u=new URL('http://127.0.0.1:'+port+'/api'+route);
 if(!ready)throw Error('工作进程尚未就绪，请稍后重试');
 if(!routes[method]?.has(route.split('?')[0])||!route.startsWith('/')||u.origin!=='http://127.0.0.1:'+port)throw Error('不允许的本地操作');
 const response=await fetch(u,{method,headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:method==='POST'?JSON.stringify(body||{}):undefined,signal:AbortSignal.timeout(120000)});
 const result=await response.json();if(!response.ok)throw Error(result.error||'本地请求失败');return result;
}
function startWorker(){return new Promise((resolve,reject)=>{
 const d=dataDir();fs.mkdirSync(d,{recursive:true,mode:0o700});try{fs.chmodSync(d,0o700);}catch{}
 const cmd=app.isPackaged?path.join(process.resourcesPath,'worker',process.platform==='win32'?'chatlens-worker.exe':'chatlens-worker'):(process.env.CHATLENS_PYTHON||'python3');
 const args=app.isPackaged?['--port','0','--data-dir',d]:['-m','services.worker.server','--port','0','--data-dir',d];
 worker=spawn(cmd,args,{cwd:app.isPackaged?process.resourcesPath:root,env:{...process.env,CHATLENS_TOKEN:token,CHATLENS_NATIVE_DIR:app.isPackaged?path.join(process.resourcesPath,'native'):path.join(root,'scripts/native'),CHATLENS_COMPATIBILITY_PATH:app.isPackaged?path.join(process.resourcesPath,'compatibility.json'):path.join(root,'docs/compatibility.json')},stdio:['ignore','pipe','pipe'],windowsHide:true});
 let buffer='';const timeout=setTimeout(()=>reject(Error('本地工作进程启动超时')),30000);
 worker.stdout.on('data',b=>{buffer+=b.toString();for(let i;(i=buffer.indexOf('\n'))>=0;){const line=buffer.slice(0,i);buffer=buffer.slice(i+1);try{const message=JSON.parse(line);if(message.ready&&Number.isInteger(message.port)){port=message.port;ready=true;clearTimeout(timeout);resolve();}}catch{}}});
 worker.stderr.on('data',()=>{});worker.on('error',()=>{clearTimeout(timeout);reject(Error('无法启动本地工作进程，请检查 Python 或安装包'));});
 worker.on('exit',()=>{ready=false;clearTimeout(timeout);if(win&&!app.quitting)win.webContents.send('chatlens:worker-offline');});
 });}
function safeSender(e){if(!win||e.sender!==win.webContents)throw Error('拒绝非工作台调用');}
ipcMain.handle('chatlens:api',async(e,{method,route,body})=>{safeSender(e);return request(method,route,body);});
ipcMain.handle('chatlens:layout',async(e,p)=>{safeSender(e);if(p.action==='restore'){let native={};if(dockTarget){try{native=await request('POST','/capture/undock',dockTarget);}catch{native={manual:true,reason:'无法自动恢复客户端，请手动调整其窗口'};}}win.setMinimumSize(1150,720);if(originalBounds)win.setBounds(originalBounds);originalBounds=null;dockTarget=null;return {mode:'workspace',native};}if(!['dock','manual'].includes(p.action)||!['wechat','qq'].includes(p.platform)||(p.action==='dock'&&!String(p.window_id||'').match(/^\d+$/)))throw Error('请先指定微信或QQ的真实窗口ID');const area=screen.getDisplayMatching(win.getBounds()).workArea;const rightWidth=Math.max(400,Math.min(490,Math.round(area.width*.34)));const gap=12;const clientBounds={x:area.x+gap,y:area.y+gap,width:Math.max(420,area.width-rightWidth-gap*3),height:area.height-gap*2};if(!originalBounds)originalBounds=win.getBounds();win.setMinimumSize(400,600);win.setBounds({x:area.x+area.width-rightWidth-gap,y:area.y+gap,width:rightWidth,height:area.height-gap*2});dockTarget=p.action==='dock'?{platform:p.platform,window_id:p.window_id}:null;let native;if(p.action==='manual'){native={manual:true,reason:'manual_layout_requested'};}else try{native=await request('POST','/capture/dock',{...dockTarget,bounds:clientBounds});}catch{native={manual:true,reason:'客户端自动停靠未通过，请将真实聊天窗口手动摆放到左侧'};}return {mode:'sidecar',workArea:area,clientBounds,analysisBounds:win.getBounds(),native};});
ipcMain.handle('chatlens:info',e=>{safeSender(e);return {version:app.getVersion(),dataDir:dataDir(),platform:process.platform,packaged:app.isPackaged};});
ipcMain.handle('chatlens:open-file',async e=>{safeSender(e);const result=await dialog.showOpenDialog(win,{title:'导入可读聊天文件',properties:['openFile'],filters:[{name:'UTF-8 JSONL / CSV',extensions:['jsonl','csv']}]});if(result.canceled)return null;const f=result.filePaths[0];if(fs.statSync(f).size>100*1024*1024)throw Error('文件超过100MB，请按时段拆分导入');return {name:path.basename(f),text:fs.readFileSync(f,'utf8'),format:path.extname(f).toLowerCase()==='.csv'?'csv':'jsonl'};});
ipcMain.handle('chatlens:demo',e=>{safeSender(e);const dir=app.isPackaged?path.join(process.resourcesPath,'fixtures'):path.join(root,'fixtures');let files=fs.readdirSync(dir).filter(x=>x.endsWith('.jsonl')&&!/1000|10000|quality|benchmark|acceptance/i.test(x));if(files.includes('demo-all.jsonl'))files=['demo-all.jsonl'];return {name:'完全虚构的演示会话',text:files.map(f=>fs.readFileSync(path.join(dir,f),'utf8').trim()).filter(Boolean).join('\n'),format:'jsonl'};});
ipcMain.handle('chatlens:save',async(e,p)=>{safeSender(e);if(typeof p.content!=='string'||p.content.length>100*1024*1024)throw Error('导出数据超出限制');const r=await dialog.showSaveDialog(win,{title:'保存本地报告',defaultPath:path.basename(String(p.filename||'利明智耀.Chatlens报告.md'))});if(r.canceled)return {saved:false};fs.writeFileSync(r.filePath,p.content,{mode:0o600});return {saved:true,path:r.filePath};});
ipcMain.handle('chatlens:data-dir',async e=>{safeSender(e);const r=await dialog.showOpenDialog(win,{title:'选择本地数据目录（重启后生效）',properties:['openDirectory','createDirectory']});if(r.canceled)return null;fs.mkdirSync(app.getPath('userData'),{recursive:true,mode:0o700});fs.writeFileSync(preferencesPath(),JSON.stringify({dataDir:r.filePaths[0]}),{mode:0o600});return {dataDir:r.filePaths[0],restartRequired:true};});
ipcMain.handle('chatlens:notify',(e,p)=>{safeSender(e);if(Notification.isSupported())new Notification({title:'利明智耀.Chatlens · 本地待办提醒',body:String(p.content||'有一项待办到期').slice(0,240)}).show();return {ok:true};});
app.whenReady().then(async()=>{try{await startWorker();win=new BrowserWindow({width:1510,height:980,minWidth:1150,minHeight:720,title:'利明智耀.Chatlens',icon:path.join(__dirname,'../assets/icon.png'),backgroundColor:'#f6f7fb',webPreferences:{preload:path.join(__dirname,'preload.cjs'),contextIsolation:true,nodeIntegration:false,sandbox:true}});win.webContents.setWindowOpenHandler(()=>({action:'deny'}));win.webContents.on('will-navigate',e=>e.preventDefault());if(process.env.CHATLENS_DEV==='1')await win.loadURL('http://127.0.0.1:5178');else await win.loadFile(path.join(__dirname,'../dist/index.html'));if(process.env.CHATLENS_SCREENSHOT){setTimeout(async()=>{fs.mkdirSync(path.dirname(process.env.CHATLENS_SCREENSHOT),{recursive:true});const image=await win.webContents.capturePage();fs.writeFileSync(process.env.CHATLENS_SCREENSHOT,image.toPNG());if(process.env.CHATLENS_SMOKE_EXIT)app.quit();},5000);}}catch(err){dialog.showErrorBox('利明智耀.Chatlens 启动失败',err.message);app.quit();}});
app.on('before-quit',()=>{app.quitting=true;if(worker)worker.kill('SIGTERM');});app.on('window-all-closed',()=>app.quit());
