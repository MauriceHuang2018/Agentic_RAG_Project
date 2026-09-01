// Router bootstrap. Three global guards in fixed order:
//   1. auth.guard       — redirect to /login if no JWT
//   2. rbac.guard       — enforce meta.permKey / meta.permKeys
//   3. workspace.guard  — UX gate for routes that need active workspace

import { createRouter, createWebHistory, type Router } from 'vue-router';
import { routes } from './routes';
import { authGuard } from './guards/auth.guard';
import { rbacGuard } from './guards/rbac.guard';
import { workspaceGuard } from './guards/workspace.guard';

export const router: Router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior(_to, _from, savedPosition) {
    return savedPosition ?? { left: 0, top: 0 };
  },
});

router.beforeEach(authGuard);
router.beforeEach(rbacGuard);
router.beforeEach(workspaceGuard);

export default router;