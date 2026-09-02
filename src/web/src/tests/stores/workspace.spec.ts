// workspace store contract tests (T6.1 — closed 2026-09-02).
//
// Pins the behaviour that the chat page depends on:
//   * `setFromLogin` populates the list and auto-selects the first
//     workspace (so the user can immediately send a query).
//   * `setFromLogin([])` leaves the list empty — the chat page
//     renders an empty-state alert instead of sending requests with a
//     bogus id.
//   * `ensureFallback()` does NOT inject the historical
//     `00000000-...-0000` placeholder (that broke every chat request
//     with 403 `not_a_member_of_workspace`).
//   * `__system__` rows are filtered from `visibleWorkspaces` even if a
//     future caller forgets to filter upstream (defence in depth — the
//     backend already filters in `_resolve_user_context`).
//   * `activeWorkspaceId` round-trips through localStorage so a reload
//     restores the user's choice.

import { beforeEach, describe, it, expect } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import { useWorkspaceStore, type WorkspaceItem } from '@/stores/workspace';

const ACME_HQ: WorkspaceItem = {
  id: '11111111-1111-1111-1111-111111111111',
  name: 'Acme Corp · 总公司',
  isolationLevel: 'logical',
  status: 'enable',
};
const ACME_RD: WorkspaceItem = {
  id: '22222222-2222-2222-2222-222222222222',
  name: 'Acme Corp · 研发部',
  isolationLevel: 'logical',
  status: 'enable',
};

describe('workspace store', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    localStorage.clear();
  });

  it('setFromLogin populates the list and auto-selects the first workspace', () => {
    const ws = useWorkspaceStore();
    ws.setFromLogin([ACME_HQ, ACME_RD]);

    expect(ws.workspaces).toHaveLength(2);
    expect(ws.visibleWorkspaces).toHaveLength(2);
    expect(ws.activeWorkspaceId).toBe(ACME_HQ.id);
    expect(ws.activeWorkspace?.name).toBe('Acme Corp · 总公司');
  });

  it('setFromLogin with empty list stays empty (no fake UUID)', () => {
    const ws = useWorkspaceStore();
    ws.setFromLogin([]);

    expect(ws.workspaces).toEqual([]);
    expect(ws.visibleWorkspaces).toEqual([]);
    expect(ws.activeWorkspaceId).toBeNull();
  });

  it('ensureFallback no longer injects the all-zero placeholder', () => {
    // Pre-2026-09-02 this seeded `00000000-0000-0000-0000-000000000000`
    // and chat requests 403'd every time. Now it's a no-op so the
    // chat page can render the "no workspace" empty state.
    const ws = useWorkspaceStore();
    ws.ensureFallback();

    expect(ws.workspaces).toEqual([]);
    expect(ws.activeWorkspaceId).toBeNull();
    expect(
      ws.workspaces.find(
        (w) => w.id === '00000000-0000-0000-0000-000000000000',
      ),
    ).toBeUndefined();
  });

  it('ensureFallback is a no-op even after workspaces are loaded', () => {
    const ws = useWorkspaceStore();
    ws.setFromLogin([ACME_HQ]);
    ws.ensureFallback();

    expect(ws.workspaces).toHaveLength(1);
    expect(ws.activeWorkspaceId).toBe(ACME_HQ.id);
  });

  it('filters __system__ from visibleWorkspaces', () => {
    const ws = useWorkspaceStore();
    ws.setList([
      ACME_HQ,
      { id: 'sys-uuid', name: '__system__', isolationLevel: 'logical', status: 'enable' },
    ]);

    expect(ws.workspaces).toHaveLength(2);
    expect(ws.visibleWorkspaces).toHaveLength(1);
    expect(ws.visibleWorkspaces[0]?.id).toBe(ACME_HQ.id);
  });

  it('persists activeWorkspaceId to localStorage', () => {
    const ws = useWorkspaceStore();
    ws.setFromLogin([ACME_HQ, ACME_RD]);
    ws.setActive(ACME_RD.id);

    expect(localStorage.getItem('agentic_rag.active_workspace_id')).toBe(ACME_RD.id);

    // clear() wipes storage too
    ws.clear();
    expect(localStorage.getItem('agentic_rag.active_workspace_id')).toBeNull();
  });

  it('falls back to first visible workspace when active id disappears', () => {
    const ws = useWorkspaceStore();
    ws.setFromLogin([ACME_HQ, ACME_RD]);
    ws.setActive(ACME_RD.id);

    // Admin disabled acme-rd — refresh list, only hq remains.
    ws.setList([ACME_HQ]);

    expect(ws.activeWorkspaceId).toBe(ACME_HQ.id);
  });
});