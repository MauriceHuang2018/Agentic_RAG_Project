// Route table — 14 entries (13 routes + 1 modal-as-route for FeedbackModal).
// Each entry declares `meta.permKey` / `meta.permKeys` consumed by rbac.guard.
// Keep this file in sync with `docs/前端架构/路由与目录.md §3`.
//
// Permission semantics (mirrors rbac.guard.ts comments):
//   - `meta.permKeys: [a, b]` alone → AND (safer default); user must hold ALL.
//   - `meta.permKeys + meta.permMode: 'any'` → OR; user must hold ANY ONE.
//   - `meta.permKey: 'x'` alone → single-key requirement.
//
// Visible-roles column (informational; runtime gating uses meta.permKey):
//   WSA = workspace_admin   KBA = kb_admin       DOO = doc_owner
//   SUA = super_admin       SYA = system_admin
//
// All non-public routes require authentication (auth.guard → /login).

import type { RouteRecordRaw } from 'vue-router';

export const routes: RouteRecordRaw[] = [
  // ─── Page 1 · 登录（公开） ─────────────────────────────────────────
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/auth/Login.vue'),
    meta: { public: true, title: 'login' },
  },

  // ─── Page 2 · 智能问答 ─────────────────────────────────────────────
  {
    path: '/chat',
    name: 'chat',
    component: () => import('@/views/chat/Chat.vue'),
    meta: { permKey: 'chat:ask', workspaceRequired: true, title: 'chat' },
  },

  // ─── Page 3 · 反馈弹窗（dialog 形式，挂在 /chat 下作为子路由） ─────
  {
    path: '/chat/feedback/:messageId',
    name: 'chat-feedback',
    component: () => import('@/components/chat/FeedbackModal.vue'),
    props: true,
    meta: { permKey: 'feedback:submit', workspaceRequired: true, title: 'feedback' },
  },

  // ─── Page 4 · 管理后台概览 ─────────────────────────────────────────
  // OR: any admin who holds evaluation:read OR feedback:read sees the dashboard.
  {
    path: '/admin',
    name: 'admin-dashboard',
    component: () => import('@/views/admin/Dashboard.vue'),
    meta: {
      permKeys: ['evaluation:read', 'feedback:read'],
      permMode: 'any',
      title: 'admin.dashboard',
    },
  },

  // ─── Page 5 · 文档管理 ─────────────────────────────────────────────
  // OR: any doc admin capability (read/write/delete/reindex) grants entry.
  {
    path: '/admin/documents',
    name: 'admin-documents',
    component: () => import('@/views/admin/documents/Documents.vue'),
    meta: {
      permKeys: ['doc:read', 'doc:write', 'doc:delete', 'doc:reindex'],
      permMode: 'any',
      title: 'admin.documents',
    },
  },

  // ─── Page 6 · 反馈查看 ─────────────────────────────────────────────
  // OR: feedback viewer OR ticket transitioner sees the page.
  {
    path: '/admin/feedback',
    name: 'admin-feedback',
    component: () => import('@/views/admin/feedback/Feedback.vue'),
    meta: {
      permKeys: ['feedback:read', 'feedback:ticket:transition'],
      permMode: 'any',
      title: 'admin.feedback',
    },
  },

  // ─── Page 7 · 评估看板 ─────────────────────────────────────────────
  // OR: any of evaluation / csat / drift read grants entry.
  {
    path: '/admin/eval',
    name: 'admin-eval',
    component: () => import('@/views/admin/eval/EvalDashboard.vue'),
    meta: {
      permKeys: ['evaluation:read', 'csat:read', 'drift:read'],
      permMode: 'any',
      title: 'admin.eval',
    },
  },

  // ─── Page 8 · 审计日志 ─────────────────────────────────────────────
  // Single-key requirement; default AND (no permMode needed).
  {
    path: '/admin/audit',
    name: 'admin-audit',
    component: () => import('@/views/admin/audit/AuditLogs.vue'),
    meta: { permKey: 'audit:read', title: 'admin.audit' },
  },

  // ─── Page 9 · 角色管理 ─────────────────────────────────────────────
  // OR: any role admin capability (read/write/delete) grants entry.
  {
    path: '/admin/roles',
    name: 'admin-roles',
    component: () => import('@/views/admin/roles/Roles.vue'),
    meta: {
      permKeys: ['role:read', 'role:write', 'role:delete'],
      permMode: 'any',
      title: 'admin.roles',
    },
  },

  // ─── Page 11 · 用户管理 ────────────────────────────────────────────
  // OR: any user admin or role-assign capability grants entry.
  {
    path: '/admin/user-mgmt',
    name: 'admin-users',
    component: () => import('@/views/admin/users/UserMgmt.vue'),
    meta: {
      permKeys: ['user:read', 'user:write', 'user:delete', 'role:assign'],
      permMode: 'any',
      title: 'admin.users',
    },
  },

  // ─── Page 12 · 工作空间管理 ────────────────────────────────────────
  // OR: any workspace admin capability grants entry.
  {
    path: '/admin/workspaces',
    name: 'admin-workspaces',
    component: () => import('@/views/admin/workspaces/Workspaces.vue'),
    meta: {
      permKeys: ['workspace:read', 'workspace:write', 'workspace:create'],
      permMode: 'any',
      title: 'admin.workspaces',
    },
  },

  // ─── Page 13 · 反馈配置 ────────────────────────────────────────────
  // OR: feedback-config viewer OR writer.
  {
    path: '/admin/feedback-config',
    name: 'admin-feedback-config',
    component: () => import('@/views/admin/feedback-config/FeedbackConfig.vue'),
    meta: {
      permKeys: ['feedback-config:read', 'feedback-config:write'],
      permMode: 'any',
      title: 'admin.feedback-config',
    },
  },

  // ─── Page 15 · 敏感信息维护 ────────────────────────────────────────
  // OR: sensitive-info reader OR updater.
  {
    path: '/admin/sensitive-info',
    name: 'admin-sensitive-info',
    component: () => import('@/views/admin/sensitive-info/SensitiveInfo.vue'),
    meta: {
      permKeys: ['sensitive:read', 'sensitive:update'],
      permMode: 'any',
      title: 'admin.sensitive',
    },
  },

  // ─── Page 14 · 个人中心 ────────────────────────────────────────────
  // AND (default): user must hold BOTH me:read AND me:write. In practice these
  // travel together; the AND default keeps the gate conservative.
  {
    path: '/profile',
    name: 'profile',
    component: () => import('@/views/profile/Profile.vue'),
    meta: { permKeys: ['me:read', 'me:write'], title: 'profile' },
  },

  // ─── 拒绝访问 / 找不到资源（页面级兜底） ─────────────────────────────
  {
    path: '/forbidden',
    name: 'forbidden',
    component: () => import('@/views/errors/Forbidden.vue'),
    meta: { public: true, title: 'errors.forbidden' },
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/errors/NotFound.vue'),
    meta: { public: true, title: 'errors.notFound' },
  },
];

/** Display label resolver used by Topbar / SidebarNav. */
export const ROUTE_LABELS: Readonly<Record<string, string>> = Object.freeze({
  '/chat': '智能问答',
  '/admin': '管理后台',
  '/admin/documents': '文档管理',
  '/admin/feedback': '反馈查看',
  '/admin/eval': '评估看板',
  '/admin/audit': '审计日志',
  '/admin/roles': '角色管理',
  '/admin/user-mgmt': '用户管理',
  '/admin/workspaces': '工作空间管理',
  '/admin/feedback-config': '反馈配置',
  '/admin/sensitive-info': '敏感信息维护',
  '/profile': '个人中心',
});