-- Auto-create the LiteLLM metadata database on first postgres start.
--
-- The official postgres image only auto-creates the database named in
-- `POSTGRES_DB` (here: `rag_business`). This init script is mounted at
-- `/docker-entrypoint-initdb.d/` and runs after the image's bootstrap,
-- so by the time it executes the `rag` role already exists.
--
-- LiteLLM later runs `prisma db push` against this DB on its first
-- startup, creating `LiteLLM_VerificationToken`, `LiteLLM_TeamTable`,
-- etc. — all `LiteLLM_`-prefixed, so they do not collide with the
-- business schema in `rag_business`.

CREATE DATABASE rag_litellm OWNER rag;