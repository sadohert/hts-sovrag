create extension if not exists vector;

create table if not exists documents (
  id             text primary key,
  title          text not null,
  department     text not null,          -- DND, CSE, PS, PSPC, SSC, TBS, GC_WIDE
  classification text not null,          -- UNCLASSIFIED | PROTECTED_A | PROTECTED_B
  doc_type       text not null,          -- DP | DRR | POLICY | OAG
  fiscal_year    text,
  source_url     text not null,
  lang           text not null default 'en'
);

create table if not exists chunks (
  id            bigserial primary key,
  doc_id        text not null references documents(id) on delete cascade,
  ordinal       int not null,
  heading_path  text,
  text          text not null,
  tsv           tsvector generated always as (to_tsvector('english', text)) stored,
  embedding     vector(1024),
  unique (doc_id, ordinal)
);

create index if not exists chunks_embedding_hnsw on chunks using hnsw (embedding vector_cosine_ops);
create index if not exists chunks_tsv_gin on chunks using gin (tsv);
create index if not exists chunks_doc_id on chunks (doc_id);
