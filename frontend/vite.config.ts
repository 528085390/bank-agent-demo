import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [vue()],
  test: {
    // The view tests mount `App.vue`, so they need a DOM. The pure suites do not care.
    environment: 'jsdom',
  },
  server: {
    proxy: {
      // `changeOrigin: false` is deliberate, not the default being left alone: with it on,
      // http-proxy rewrites `Host` to the upstream target, so the backend sees `:8000` while
      // the browser's `Origin` says `:5173`. The origin check compares the two and rejects a
      // request that is in fact same-origin. Keeping the browser's `Host` is also what the
      // real nginx does, so dev and prod exercise the same code path.
      '/api': { target: process.env.BANK_AGENT_API_TARGET || 'http://127.0.0.1:8000', changeOrigin: false },
    },
    watch: {
      // 原子写入（编辑器、工具链）会在源码目录旁临时创建
      // `.<name>.<pid>.<uuid>.tmpdir/<name>.tmp`。Windows 上该临时文件此刻仍被
      // 写者占用，chokidar 去 watch 会抛 EBUSY，而 FSWatcher 的 error 未被捕获，
      // 整个 dev server 进程随之退出。忽略这些瞬时文件，避免被并发编辑打断。
      ignored: ['**/.*.tmpdir/**', '**/*.tmp'],
    },
  },
})
