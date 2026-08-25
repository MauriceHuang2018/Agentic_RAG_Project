"""Generator — LLM-backed answer generation (DESIGN 2.2 #9).

Calls LiteLLM with retrieved chunks, conversation history, and model config.
Returns answer + citations. Implemented as part of M2/M3 (T2.4 + T3.2).
"""