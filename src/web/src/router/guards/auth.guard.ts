// auth.guard — redirect to /login when no valid JWT is present.
// JWT presence + signature validity are NOT re-verified here; that happens in
// the API client (axios interceptor) on the next request. The guard's job is
// to gate access by token presence alone so unauthenticated users land on
// /login before triggering 401 on a real API call.

import type { NavigationGuardWithThis } from 'vue-router';
import { useAuthStore } from '@/stores/auth';

const PUBLIC_PATHS = new Set<string>(['/login']);

export const authGuard: NavigationGuardWithThis<undefined> = function (to, _from, next) {
  const auth = useAuthStore();
  const isPublic = PUBLIC_PATHS.has(to.path);

  if (isPublic) {
    // Allow entry; logout flow bounces authenticated users away from /login.
    if (auth.isAuthenticated && to.path === '/login') {
      return next({ path: '/chat', replace: true });
    }
    return next();
  }

  if (!auth.isAuthenticated) {
    // Capture intended target so login page can bounce back after success.
    return next({
      path: '/login',
      query: { redirect: to.fullPath },
      replace: true,
    });
  }

  return next();
};