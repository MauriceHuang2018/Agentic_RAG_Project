// Guardrail error codes emitted by backend query-guardrail (4031~4034).
// Frontend maps HTTP 403 + {detail: "guardrail_block", extra.rule} → toast text.
// Source: docs/phase1-mvp/DESIGN_phase1-mvp.md §7.4.

export const GUARDRAIL_CODES = {
  SENSITIVE_WORD: 4031,
  PROMPT_INJECTION: 4032,
  PII: 4033,
  OUT_OF_SCOPE: 4034,
} as const;

export type GuardrailCode = (typeof GUARDRAIL_CODES)[keyof typeof GUARDRAIL_CODES];

/** Block reason strings returned by backend `extra.rule`. */
export type GuardrailRule =
  | 'sensitive_word'
  | 'prompt_injection'
  | 'pii'
  | 'out_of_scope';