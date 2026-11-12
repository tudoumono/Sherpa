--
-- PostgreSQL database dump
--


-- Dumped from database version 16.15 (Debian 16.15-1.pgdg13+2)
-- Dumped by pg_dump version 16.15 (Debian 16.15-1.pgdg13+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: documents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.documents (
    id integer NOT NULL,
    kb_id text DEFAULT 'global'::text NOT NULL,
    version text NOT NULL,
    name text NOT NULL,
    layer text DEFAULT 'version'::text NOT NULL,
    scope_path text,
    doctype text,
    branch text,
    original_path text,
    md_path text,
    status text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    importance text,
    importance_reason text,
    importance_source text
);


--
-- Name: documents_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.documents_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: documents_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.documents_id_seq OWNED BY public.documents.id;


--
-- Name: ingest_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ingest_runs (
    id integer NOT NULL,
    kb_id text DEFAULT 'global'::text NOT NULL,
    version text,
    layer text DEFAULT 'version'::text NOT NULL,
    source_doc_ids jsonb,
    status text DEFAULT 'extracting'::text NOT NULL,
    extraction_snapshot jsonb,
    published_snapshot jsonb,
    created_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    published_at timestamp with time zone,
    progress jsonb,
    op text,
    payload jsonb,
    fingerprint text,
    app_version text,
    arb_keys text[],
    claimed_by text,
    CONSTRAINT ingest_runs_status_check CHECK ((status = ANY (ARRAY['queued'::text, 'extracting'::text, 'auto_published'::text, 'auto_published_with_flags'::text, 'failed'::text])))
);


--
-- Name: ingest_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ingest_runs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ingest_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ingest_runs_id_seq OWNED BY public.ingest_runs.id;


--
-- Name: name_ledger; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.name_ledger (
    world text NOT NULL,
    name text NOT NULL,
    kind text DEFAULT ''::text NOT NULL,
    source text NOT NULL,
    rel_path text NOT NULL
);


--
-- Name: name_ledger_built; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.name_ledger_built (
    world text NOT NULL,
    built_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: worlds; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.worlds (
    kb_id text DEFAULT 'global'::text NOT NULL,
    world_id text NOT NULL,
    root_path text NOT NULL,
    label text,
    storage_mode text DEFAULT 'external_reference'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_sig text,
    last_synced_at timestamp with time zone,
    last_manifest jsonb,
    last_doc_count integer,
    last_scan_report jsonb,
    last_scan_report_at timestamp with time zone,
    resolve_applied_sig text,
    resolve_settings jsonb,
    CONSTRAINT worlds_storage_mode_check CHECK ((storage_mode = ANY (ARRAY['external_reference'::text, 'managed_copy'::text])))
);


--
-- Name: documents id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents ALTER COLUMN id SET DEFAULT nextval('public.documents_id_seq'::regclass);


--
-- Name: ingest_runs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ingest_runs ALTER COLUMN id SET DEFAULT nextval('public.ingest_runs_id_seq'::regclass);


--
-- Name: documents documents_kb_id_version_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_kb_id_version_name_key UNIQUE (kb_id, version, name);


--
-- Name: documents documents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_pkey PRIMARY KEY (id);


--
-- Name: ingest_runs ingest_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ingest_runs
    ADD CONSTRAINT ingest_runs_pkey PRIMARY KEY (id);


--
-- Name: name_ledger_built name_ledger_built_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.name_ledger_built
    ADD CONSTRAINT name_ledger_built_pkey PRIMARY KEY (world);


--
-- Name: worlds worlds_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.worlds
    ADD CONSTRAINT worlds_pkey PRIMARY KEY (kb_id, world_id);


--
-- Name: doc_ver; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX doc_ver ON public.documents USING btree (kb_id, version);


--
-- Name: name_ledger_world_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX name_ledger_world_name ON public.name_ledger USING btree (world, name);


--
-- Name: run_queued; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX run_queued ON public.ingest_runs USING btree (kb_id, id) WHERE (status = 'queued'::text);


--
-- Name: run_ver; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX run_ver ON public.ingest_runs USING btree (kb_id, version);


--
-- Name: run_ver_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX run_ver_id ON public.ingest_runs USING btree (kb_id, version, id DESC);


--
-- Name: run_ver_published; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX run_ver_published ON public.ingest_runs USING btree (kb_id, version, id DESC) WHERE (published_at IS NOT NULL);


--
-- Name: run_ver_published_es; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX run_ver_published_es ON public.ingest_runs USING btree (kb_id, version, id DESC) WHERE ((published_at IS NOT NULL) AND (extraction_snapshot ? 'es'::text));


--
-- Name: worlds_root; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX worlds_root ON public.worlds USING btree (kb_id, root_path);


--
-- PostgreSQL database dump complete
--


