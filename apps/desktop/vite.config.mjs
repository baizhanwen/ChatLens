import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';
// Keep dev CSP strict: Vite can transform JSX without the inline React refresh preamble.
export default defineConfig(({command})=>({plugins:command==='build'?[react()]:[],base:'./',server:{host:'127.0.0.1',port:5178,strictPort:true}}));
