-- RPC for RAG search: nearest ocr_documents rows by cosine distance.
-- Run once in the Supabase SQL Editor (re-running is safe: create or replace).
-- Requires the ocr_documents table from db/supabase.py docstring.
-- vector(768) must match GEMINI_EMBEDDING_DIM and the table column.
-- filter_session = null searches all sessions.

create or replace function match_ocr_documents(
    query_embedding vector(768),
    match_count     int  default 5,
    filter_session  text default null
)
returns table (id uuid, session_id text, title text, text text, similarity float)
language sql stable
as $$
    select d.id, d.session_id, d.title, d.text,
           1 - (d.embedding <=> query_embedding) as similarity
    from ocr_documents d
    where filter_session is null or d.session_id = filter_session
    order by d.embedding <=> query_embedding
    limit match_count;
$$;
