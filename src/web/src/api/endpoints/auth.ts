// auth endpoints — Page 1 (Login) + logout.
// Backend contract (Explore agent 2026-09-01):
//   POST /auth/login  OAuth2PasswordRequestForm → 200 {access_token, token_type, user}
//                   ↳ 401 invalid_credentials, 403 user_disabled
//   POST /auth/logout → 200 {ok: true}  (server is stateless; client wipes state)

import { httpClient } from '../client';
import type { AxiosError } from 'axios';

export interface LoginRequest {
  username: string;
  password: string;
}

export interface AuthenticatedUser {
  id: string;
  username: string;
  isSuperAdmin: boolean;
  status: 'enable' | 'disable';
}

export interface LoginResponse {
  accessToken: string;
  tokenType: 'bearer';
  user: AuthenticatedUser;
}

export async function login(req: LoginRequest): Promise<LoginResponse> {
  // FastAPI OAuth2PasswordRequestForm expects application/x-www-form-urlencoded.
  const form = new URLSearchParams();
  form.set('username', req.username);
  form.set('password', req.password);
  try {
    const res = await httpClient.post<{
      access_token: string;
      token_type: 'bearer';
      user: {
        id: string;
        username: string;
        is_super_admin: boolean;
        status: 'enable' | 'disable';
      };
    }>('/auth/login', form, {
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    });
    return {
      accessToken: res.data.access_token,
      tokenType: res.data.token_type,
      user: {
        id: res.data.user.id,
        username: res.data.user.username,
        isSuperAdmin: Boolean(res.data.user.is_super_admin),
        status: res.data.user.status,
      },
    };
  } catch (err) {
    // Re-throw so the client interceptor can convert 401/403 → NormalizedError.
    throw err as AxiosError;
  }
}

export interface LogoutResponse {
  ok: true;
}

export async function logout(): Promise<LogoutResponse> {
  const res = await httpClient.post<LogoutResponse>('/auth/logout');
  return res.data;
}