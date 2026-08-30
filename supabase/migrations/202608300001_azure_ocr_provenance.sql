-- Mark My Words — Azure OCR provenance on locked exemplars
-- Migration: 202608300001_azure_ocr_provenance.sql
--
-- Azure Read output is a teacher-reviewable OCR candidate. These fields retain
-- concise, non-content provenance with the teacher's locked exemplar without
-- storing a raw provider response, endpoint, key, operation URL, or document.
--
-- Existing rows remain valid: text-layer/DOCX/TXT records have an empty source
-- and false review flag unless later updated through a teacher workflow.

alter table public.essay_memory
    add column if not exists ocr_source text not null default '';

alter table public.essay_memory
    add column if not exists ocr_metadata jsonb not null default '{}'::jsonb;

alter table public.essay_memory
    add column if not exists transcript_reviewed boolean not null default false;

comment on column public.essay_memory.ocr_source is
    'Text acquisition route, e.g. azure_document_intelligence or text_layer.';
comment on column public.essay_memory.ocr_metadata is
    'Safe Azure OCR review metadata only; never a raw response or credential.';
comment on column public.essay_memory.transcript_reviewed is
    'True when the teacher applied/confirmed an Azure OCR transcript before lock.';

-- Preserve any historical correction rows but retire the old glossary write
-- path. Azure Read does not consume teacher corrections as an OCR prompt.
revoke insert, update on table public.transcript_corrections from authenticated;
