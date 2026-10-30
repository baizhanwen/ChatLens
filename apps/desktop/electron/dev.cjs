const {spawn}=require('node:child_process');const path=require('node:path');
const cwd=path.resolve(__dirname,'..');const vite=spawn(process.execPath,[path.join(cwd,'node_modules/vite/bin/vite.js')],{cwd,stdio:'inherit'});let electron;
async function boot(){for(let i=0;i<100;i++){try{await fetch('http://127.0.0.1:5178');electron=spawn(require('electron'),['.'],{cwd,stdio:'inherit',env:{...process.env,CHATLENS_DEV:'1'}});electron.on('exit',code=>{vite.kill();process.exit(code||0);});return;}catch{await new Promise(r=>setTimeout(r,200));}}vite.kill();process.exit(1);}boot();
for(const sig of ['SIGINT','SIGTERM'])process.on(sig,()=>{electron?.kill();vite.kill();process.exit();});
