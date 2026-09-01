# Agentic RAG · Web Frontend (src/web/)

M6 前端 SPA。Vue 3 + Vite + TypeScript + Element Plus + Pinia + vue-router + axios + vue-i18n + vue-echarts。

完整架构设计输入：[docs/前端架构/路由与目录.md](../../前端架构/路由与目录.md) v1.0 + [docs/原型设计/前端页面规划与字段-表映射.md](../../原型设计/前端页面规划与字段-表映射.md) v2.0 + [docs/phase1-mvp/DESIGN_phase1-mvp.md](../../phase1-mvp/DESIGN_phase1-mvp.md) §11.5。

## 环境要求

- Node.js ≥ 18
- pnpm ≥ 8（推荐；也可用 npm/yarn）
- 后端 FastAPI 跑在 `localhost:8000`（参考项目根 README.md 的 `uv run python -m agentic_rag_project` 启动）

## 启动步骤

```bash
cd src/web
cp .env.example .env.local       # 可选；默认值已能跑
pnpm install
pnpm gen:openapi                 # 生成 src/api/types.gen.ts（需后端 /openapi.json 可访问）
pnpm gen:perms                   # 同步 src/constants/permissions.ts（可选；常量表已内置）
pnpm dev                         # 启 http://localhost:5173
```

打开浏览器访问 http://localhost:5173，用 seed 的 `chat_user` / `demo_sys` 登录即可。

## 目录速览（完整见 路由与目录.md §4）

```
src/
├── main.ts            # bootstrap: pinia + router + i18n + Element Plus
├── App.vue            # 顶层 layout: Topbar + router-view
├── router/             # 14 routes + 3 guards (auth / rbac / workspace)
├── stores/            # 7 Pinia stores (auth/workspace/permission/conversation/feedback/audit/sensitive)
├── api/               # axios + SSE/NDJSON reader + OpenAPI types + 12 endpoints
├── components/{layout,common,chat,charts}/
├── views/{auth,chat,admin,profile}/   # 14 路由文件
├── composables/       # 9 个 useXxx
├── constants/         # 5 份常量（internals / audit-actions / ticket-transitions / permissions / error-toast）
├── i18n/              # vue-i18n + 2 locale
├── utils/ + styles/ + tests/
```

## 常用脚本

| 命令 | 说明 |
|------|------|
| `pnpm dev` | 启 Vite dev server :5173 |
| `pnpm build` | 类型检查 + 生产构建到 `dist/` |
| `pnpm preview` | 预览生产构建产物 |
| `pnpm test` | vitest 单元测试 |
| `pnpm type-check` | `vue-tsc --noEmit` |
| `pnpm gen:openapi` | `bash scripts/gen-openapi-ts.sh` —— 后端 `/openapi.json` → `src/api/types.gen.ts` |
| `pnpm gen:perms` | `bash scripts/gen-perm-keys-sync.sh` —— 后端 `rbac/seed.py::PERMISSION_KEYS` → `src/constants/permissions.ts` |
| `pnpm check:perms` | `bash scripts/check-route-perms.sh` —— 静态校验路由 `meta.permKey` ⊂ PERMISSION_KEYS |

## 开发约定

- 视图文件**禁止**直接 `import axios`；必须经 `src/api/endpoints/*` 统一出口
- i18n 文案**禁止**硬编码在 .vue 里；走 `t('keys.someKey')`
- 视图组件路径用 `@/` alias 引用，如 `import Login from '@/views/auth/Login.vue'`
- 新增 .vue 文件必须 TypeScript `<script setup lang="ts">`，避免 `defineComponent` Options API
- 任何 RBAC 拦截由 `router/guards/rbac.guard.ts` 处理，不要在视图内手写 perm 检查