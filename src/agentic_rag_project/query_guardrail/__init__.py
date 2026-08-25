"""Query Guardrail — pre-LLM input compliance (DESIGN 2.2 #4).

Catches sensitive words, prompt injection, and PII before the query reaches
the LLM. Per ALIGNMENT R13, all LLM-bound inputs MUST pass through this
node — business code is not allowed to bypass it. Implemented in T5.4.
"""