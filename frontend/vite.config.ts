import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({plugins:[react()],base:'/vocabatron/',build:{sourcemap:false,assetsInlineLimit:0},server:{host:'127.0.0.1'}});
