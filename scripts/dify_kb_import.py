#!/usr/bin/env python3
"""Import a local folder tree into Dify Knowledge Bases.

This is intentionally independent from Sherpa's own knowledge pipeline. It maps
folders to Dify knowledge bases and uploads files through Dify's Knowledge Base
API.
"""

from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import hashlib
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "https://api.dify.ai/v1"
DEFAULT_EXTENSIONS = {
    ".csv",
    ".docx",
    ".htm",
    ".html",
    ".json",
    ".jsonl",
    ".md",
    ".markdown",
    ".pdf",
    ".pptx",
    ".rtf",
    ".txt",
    ".xlsx",
    ".xml",
}
DEFAULT_EXCLUDES = ["~$*", "*.tmp", "*.temp", ".DS_Store", "Thumbs.db"]
METADATA_FIELDS = {
    "source_relative_path": "string",
    "source_folder": "string",
    "source_dataset_key": "string",
}
TERMINAL_INDEXING_STATUSES = {"completed", "error", "paused"}


class DifyApiError(RuntimeError):
    def __init__(self, status: int | None, message: str, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


@dataclasses.dataclass(frozen=True)
class ImportItem:
    path: Path
    rel_path: str
    dataset_key: str
    dataset_name: str
    document_name: str

    @property
    def source_folder(self) -> str:
        folder = str(Path(self.rel_path).parent).replace("\\", "/")
        return "" if folder == "." else folder


class DifyClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float,
        retries: int,
        retry_sleep: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.retries = retries
        self.retry_sleep = retry_sleep

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> Any:
        url = self.base_url + path
        if query:
            pairs: list[tuple[str, str]] = []
            for key, value in query.items():
                if value is None:
                    continue
                if isinstance(value, bool):
                    pairs.append((key, str(value).lower()))
                elif isinstance(value, (list, tuple)):
                    pairs.extend((key, str(item)) for item in value)
                else:
                    pairs.append((key, str(value)))
            url += "?" + urllib.parse.urlencode(pairs)

        headers = {"Authorization": f"Bearer {self.api_key}"}
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type

        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        last_error: DifyApiError | None = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                    if not raw:
                        return {}
                    return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                payload = _read_error_payload(exc)
                message = _error_message(exc.code, payload)
                last_error = DifyApiError(exc.code, message, payload)
                if exc.code not in {429, 500, 502, 503, 504} or attempt == self.retries:
                    raise last_error
            except urllib.error.URLError as exc:
                last_error = DifyApiError(None, f"network_error: {exc.reason}", None)
                if attempt == self.retries:
                    raise last_error

            time.sleep(self.retry_sleep * (2**attempt))

        assert last_error is not None
        raise last_error

    def list_knowledge_bases(self, keyword: str | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = self.request(
                "GET",
                "/datasets",
                query={"page": page, "limit": 100, "keyword": keyword, "include_all": True},
            )
            items.extend(payload.get("data", []))
            if not payload.get("has_more"):
                return items
            page += 1

    def find_knowledge_base_by_name(self, name: str) -> dict[str, Any] | None:
        for kb in self.list_knowledge_bases(keyword=name):
            if kb.get("name") == name:
                return kb
        return None

    def create_knowledge_base(
        self,
        *,
        name: str,
        description: str,
        indexing_technique: str,
        permission: str,
        embedding_model: str | None,
        embedding_model_provider: str | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": name,
            "description": description,
            "indexing_technique": indexing_technique,
            "permission": permission,
            "provider": "vendor",
        }
        if embedding_model:
            body["embedding_model"] = embedding_model
        if embedding_model_provider:
            body["embedding_model_provider"] = embedding_model_provider
        try:
            return self.request("POST", "/datasets", json_body=body)
        except DifyApiError as exc:
            if exc.status == 409:
                existing = self.find_knowledge_base_by_name(name)
                if existing:
                    return existing
            raise

    def list_documents(self, dataset_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = self.request(
                "GET",
                f"/datasets/{dataset_id}/documents",
                query={"page": page, "limit": 100},
            )
            items.extend(payload.get("data", []))
            if not payload.get("has_more"):
                return items
            page += 1

    def create_document_by_file(
        self,
        dataset_id: str,
        file_path: Path,
        upload_name: str,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        body, content_type = _multipart_body(file_path, upload_name, config)
        return self.request(
            "POST",
            f"/datasets/{dataset_id}/document/create-by-file",
            body=body,
            content_type=content_type,
        )

    def update_document_by_file(
        self,
        dataset_id: str,
        document_id: str,
        file_path: Path,
        upload_name: str,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        body, content_type = _multipart_body(file_path, upload_name, config)
        return self.request(
            "POST",
            f"/datasets/{dataset_id}/documents/{document_id}/update-by-file",
            body=body,
            content_type=content_type,
        )

    def get_indexing_status(self, dataset_id: str, batch: str) -> list[dict[str, Any]]:
        payload = self.request("GET", f"/datasets/{dataset_id}/documents/{batch}/indexing-status")
        return payload.get("data", [])

    def list_metadata_fields(self, dataset_id: str) -> list[dict[str, Any]]:
        payload = self.request("GET", f"/datasets/{dataset_id}/metadata")
        return payload.get("doc_metadata", [])

    def create_metadata_field(self, dataset_id: str, name: str, field_type: str) -> dict[str, Any]:
        return self.request("POST", f"/datasets/{dataset_id}/metadata", json_body={"name": name, "type": field_type})

    def update_document_metadata(
        self,
        dataset_id: str,
        document_id: str,
        metadata_list: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/datasets/{dataset_id}/documents/metadata",
            json_body={
                "operation_data": [
                    {
                        "document_id": document_id,
                        "metadata_list": metadata_list,
                        "partial_update": True,
                    }
                ]
            },
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve()
    if not root.exists() or not root.is_dir():
        print(f"error: root directory not found: {root}", file=sys.stderr)
        return 2

    items = build_import_plan(root, args)
    if not items:
        print("No importable files found.")
        return 0

    print_plan_summary(items, args)
    if args.dry_run:
        return 0

    api_key = args.api_key or os.environ.get("DIFY_API_KEY")
    if not api_key:
        print("error: DIFY_API_KEY is required unless --dry-run is used", file=sys.stderr)
        return 2

    client = DifyClient(
        base_url=args.base_url,
        api_key=api_key,
        timeout=args.timeout,
        retries=args.retries,
        retry_sleep=args.retry_sleep,
    )
    document_config = build_document_config(args)
    state_file = Path(args.state_file).resolve() if args.state_file else None
    if state_file:
        state_file.parent.mkdir(parents=True, exist_ok=True)

    counters = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}
    metadata_cache: dict[str, dict[str, dict[str, Any]]] = {}

    for dataset_key, group_items in group_by_dataset(items).items():
        dataset_name = group_items[0].dataset_name
        description = f"Imported from {root} / {dataset_key}"
        print(f"\n[KB] {dataset_name} ({len(group_items)} files)")
        try:
            kb = ensure_knowledge_base(client, args, dataset_name, description)
            dataset_id = kb["id"]
            existing_by_name = {}
            if args.on_existing in {"skip", "update"}:
                existing_by_name = documents_by_name(client.list_documents(dataset_id))
            if args.metadata:
                metadata_cache[dataset_id] = ensure_metadata_fields(client, dataset_id)
        except Exception as exc:
            counters["failed"] += len(group_items)
            print(f"  failed to prepare KB: {exc}", file=sys.stderr)
            for item in group_items:
                write_state(state_file, item, "failed", error=str(exc))
            if args.fail_fast:
                break
            continue

        for item in group_items:
            try:
                outcome = import_one_file(
                    client,
                    args,
                    dataset_id,
                    existing_by_name,
                    item,
                    document_config,
                    metadata_cache.get(dataset_id, {}),
                )
                counters[outcome] += 1
                write_state(state_file, item, outcome)
                if args.sleep > 0:
                    time.sleep(args.sleep)
            except Exception as exc:
                counters["failed"] += 1
                write_state(state_file, item, "failed", error=str(exc))
                print(f"  [failed] {item.rel_path}: {exc}", file=sys.stderr)
                if args.fail_fast:
                    break

    print("\nDone.")
    print(
        f"created={counters['created']} updated={counters['updated']} "
        f"skipped={counters['skipped']} failed={counters['failed']}"
    )
    return 1 if counters["failed"] else 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload a local folder tree into Dify Knowledge Bases.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("root", type=Path, help="Input root directory.")
    parser.add_argument("--base-url", default=os.environ.get("DIFY_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--api-key", default=None, help="Dify API key. Prefer DIFY_API_KEY.")
    parser.add_argument("--dry-run", action="store_true", help="Only print the import plan.")
    parser.add_argument("--dataset-prefix", default="", help="Prefix added to every Dify KB name.")
    parser.add_argument(
        "--dataset-depth",
        type=int,
        default=1,
        help="Number of directory levels under root used as the KB boundary. 0 imports all files into one KB.",
    )
    parser.add_argument(
        "--leaf-datasets",
        action="store_true",
        help="Use each file's containing folder as the KB boundary instead of --dataset-depth.",
    )
    parser.add_argument("--permission", choices=["only_me", "all_team_members", "partial_members"], default="only_me")
    parser.add_argument("--indexing-technique", choices=["high_quality", "economy"], default="high_quality")
    parser.add_argument("--embedding-model", default=None)
    parser.add_argument("--embedding-model-provider", default=None)
    parser.add_argument("--doc-form", choices=["text_model", "hierarchical_model", "qa_model"], default="text_model")
    parser.add_argument("--doc-language", default="Japanese")
    parser.add_argument("--process-rule-mode", choices=["automatic"], default="automatic")
    parser.add_argument(
        "--on-existing",
        choices=["skip", "update", "create"],
        default="skip",
        help="Behavior when a Dify document with the same generated name already exists in the target KB.",
    )
    parser.add_argument(
        "--document-name-mode",
        choices=["relative", "filename"],
        default="relative",
        help="Use a KB-local relative path or the basename as the Dify document filename.",
    )
    parser.add_argument("--max-document-name", type=int, default=180)
    parser.add_argument("--extensions", default=",".join(sorted(DEFAULT_EXTENSIONS)), help="Comma-separated extensions, or '*' for all files.")
    parser.add_argument("--include", action="append", default=[], help="fnmatch pattern against the root-relative path.")
    parser.add_argument("--exclude", action="append", default=[], help="fnmatch pattern against the root-relative path.")
    parser.add_argument("--include-hidden", action="store_true")
    parser.add_argument("--metadata", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--wait", action="store_true", help="Wait for Dify indexing to finish after each upload.")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--wait-timeout", type=float, default=900.0)
    parser.add_argument("--timeout", type=float, default=120.0, help="HTTP request timeout seconds.")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-sleep", type=float, default=2.0)
    parser.add_argument("--sleep", type=float, default=0.0, help="Sleep between file uploads.")
    parser.add_argument("--state-file", default=None, help="Optional JSONL result log path.")
    parser.add_argument("--fail-fast", action="store_true")
    return parser.parse_args(argv)


def build_import_plan(root: Path, args: argparse.Namespace) -> list[ImportItem]:
    extensions = parse_extensions(args.extensions)
    excludes = DEFAULT_EXCLUDES + args.exclude
    raw_items: list[tuple[Path, str, str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        rel_parts = Path(rel).parts
        if not args.include_hidden and any(part.startswith(".") for part in rel_parts):
            continue
        if excludes and any(fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(path.name, pattern) for pattern in excludes):
            continue
        if args.include and not any(fnmatch.fnmatch(rel, pattern) for pattern in args.include):
            continue
        if extensions is not None and path.suffix.lower() not in extensions:
            continue
        dataset_key = dataset_key_for(rel_parts, root.name, args)
        document_name = document_name_for(rel_parts, dataset_key, args)
        raw_items.append((path, rel, dataset_key, document_name))

    dataset_names = assign_dataset_names(sorted({item[2] for item in raw_items}), root.name, args.dataset_prefix)
    return [
        ImportItem(path=path, rel_path=rel, dataset_key=dataset_key, dataset_name=dataset_names[dataset_key], document_name=document_name)
        for path, rel, dataset_key, document_name in raw_items
    ]


def parse_extensions(raw: str) -> set[str] | None:
    if raw.strip() == "*":
        return None
    extensions = set()
    for part in raw.split(","):
        part = part.strip().lower()
        if not part:
            continue
        extensions.add(part if part.startswith(".") else f".{part}")
    return extensions


def dataset_key_for(rel_parts: tuple[str, ...], root_name: str, args: argparse.Namespace) -> str:
    parent_parts = rel_parts[:-1]
    if args.leaf_datasets:
        key_parts = parent_parts
    elif args.dataset_depth <= 0:
        key_parts = ()
    else:
        key_parts = parent_parts[: args.dataset_depth]
    return "/".join(key_parts) if key_parts else f".{root_name}"


def document_name_for(rel_parts: tuple[str, ...], dataset_key: str, args: argparse.Namespace) -> str:
    if args.document_name_mode == "filename":
        return limit_name(rel_parts[-1], args.max_document_name)
    group_parts = tuple() if dataset_key.startswith(".") else tuple(dataset_key.split("/"))
    local_parts = rel_parts[len(group_parts) :] if rel_parts[: len(group_parts)] == group_parts else rel_parts
    name = "__".join(local_parts)
    return limit_name(name, args.max_document_name)


def assign_dataset_names(dataset_keys: list[str], root_name: str, prefix: str) -> dict[str, str]:
    result: dict[str, str] = {}
    used: dict[str, str] = {}
    for key in dataset_keys:
        display = root_name if key.startswith(".") else key.replace("/", " / ")
        base_name = f"{prefix}{display}"
        name = limit_name(base_name, 40)
        if name in used and used[name] != key:
            name = limit_name(f"{base_name}-{short_hash(key)}", 40)
        result[key] = name
        used[name] = key
    return result


def limit_name(name: str, max_len: int) -> str:
    cleaned = " ".join(name.replace("\x00", "").split())
    if len(cleaned) <= max_len:
        return cleaned
    suffix = "-" + short_hash(cleaned)
    keep = max(1, max_len - len(suffix))
    return cleaned[:keep].rstrip(" ._-") + suffix


def short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]


def print_plan_summary(items: list[ImportItem], args: argparse.Namespace) -> None:
    groups = group_by_dataset(items)
    mode = "dry-run" if args.dry_run else "run"
    print(f"Dify KB import plan ({mode})")
    print(f"files={len(items)} knowledge_bases={len(groups)} on_existing={args.on_existing}")
    for dataset_key, group_items in groups.items():
        print(f"  - {group_items[0].dataset_name}: {len(group_items)} files ({dataset_key})")
        for item in group_items[:5]:
            print(f"      {item.rel_path} -> {item.document_name}")
        if len(group_items) > 5:
            print(f"      ... {len(group_items) - 5} more")


def group_by_dataset(items: list[ImportItem]) -> dict[str, list[ImportItem]]:
    groups: dict[str, list[ImportItem]] = {}
    for item in items:
        groups.setdefault(item.dataset_key, []).append(item)
    return dict(sorted(groups.items(), key=lambda pair: pair[0]))


def ensure_knowledge_base(
    client: DifyClient,
    args: argparse.Namespace,
    name: str,
    description: str,
) -> dict[str, Any]:
    existing = client.find_knowledge_base_by_name(name)
    if existing:
        print(f"  using existing KB: {existing['id']}")
        return existing
    kb = client.create_knowledge_base(
        name=name,
        description=description[:400],
        indexing_technique=args.indexing_technique,
        permission=args.permission,
        embedding_model=args.embedding_model,
        embedding_model_provider=args.embedding_model_provider,
    )
    print(f"  created KB: {kb['id']}")
    return kb


def documents_by_name(documents: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for document in documents:
        name = document.get("name")
        if isinstance(name, str) and name not in result:
            result[name] = document
    return result


def import_one_file(
    client: DifyClient,
    args: argparse.Namespace,
    dataset_id: str,
    existing_by_name: dict[str, dict[str, Any]],
    item: ImportItem,
    document_config: dict[str, Any],
    metadata_fields: dict[str, dict[str, Any]],
) -> str:
    existing = existing_by_name.get(item.document_name)
    if existing and args.on_existing == "skip":
        print(f"  [skip] {item.rel_path}")
        return "skipped"

    if existing and args.on_existing == "update":
        response = client.update_document_by_file(
            dataset_id,
            existing["id"],
            item.path,
            item.document_name,
            document_config,
        )
        action = "updated"
    else:
        response = client.create_document_by_file(dataset_id, item.path, item.document_name, document_config)
        action = "created"

    document = response.get("document", {})
    document_id = document.get("id")
    batch = response.get("batch")
    print(f"  [{action}] {item.rel_path} -> {item.document_name}")
    if document_id and metadata_fields:
        update_source_metadata(client, dataset_id, document_id, item, metadata_fields)
    if args.wait and batch:
        wait_for_indexing(client, dataset_id, batch, args.poll_interval, args.wait_timeout)
    if document_id:
        existing_by_name[item.document_name] = document
    return action


def build_document_config(args: argparse.Namespace) -> dict[str, Any]:
    config: dict[str, Any] = {
        "indexing_technique": args.indexing_technique,
        "doc_form": args.doc_form,
        "process_rule": {"mode": args.process_rule_mode},
    }
    if args.doc_language:
        config["doc_language"] = args.doc_language
    if args.embedding_model:
        config["embedding_model"] = args.embedding_model
    if args.embedding_model_provider:
        config["embedding_model_provider"] = args.embedding_model_provider
    return config


def ensure_metadata_fields(client: DifyClient, dataset_id: str) -> dict[str, dict[str, Any]]:
    existing = {field["name"]: field for field in client.list_metadata_fields(dataset_id)}
    for name, field_type in METADATA_FIELDS.items():
        if name not in existing:
            try:
                existing[name] = client.create_metadata_field(dataset_id, name, field_type)
            except DifyApiError as exc:
                if exc.status == 409:
                    existing = {field["name"]: field for field in client.list_metadata_fields(dataset_id)}
                else:
                    raise
    return {name: existing[name] for name in METADATA_FIELDS if name in existing}


def update_source_metadata(
    client: DifyClient,
    dataset_id: str,
    document_id: str,
    item: ImportItem,
    fields: dict[str, dict[str, Any]],
) -> None:
    values = {
        "source_relative_path": item.rel_path,
        "source_folder": item.source_folder,
        "source_dataset_key": item.dataset_key,
    }
    metadata_list = [
        {"id": field["id"], "name": name, "value": values[name]}
        for name, field in fields.items()
        if name in values
    ]
    if metadata_list:
        client.update_document_metadata(dataset_id, document_id, metadata_list)


def wait_for_indexing(
    client: DifyClient,
    dataset_id: str,
    batch: str,
    poll_interval: float,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    last_status = ""
    while True:
        statuses = client.get_indexing_status(dataset_id, batch)
        status = ",".join(sorted({entry.get("indexing_status", "unknown") for entry in statuses}))
        if status != last_status:
            print(f"    indexing: {status}")
            last_status = status
        if statuses and all(entry.get("indexing_status") in TERMINAL_INDEXING_STATUSES for entry in statuses):
            errors = [entry.get("error") for entry in statuses if entry.get("indexing_status") == "error"]
            if errors:
                raise DifyApiError(None, f"indexing_error: {errors}", statuses)
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(f"indexing did not finish within {timeout:.0f}s (batch={batch})")
        time.sleep(poll_interval)


def write_state(state_file: Path | None, item: ImportItem, outcome: str, error: str | None = None) -> None:
    if not state_file:
        return
    record = {
        "ts": int(time.time()),
        "outcome": outcome,
        "dataset_key": item.dataset_key,
        "dataset_name": item.dataset_name,
        "relative_path": item.rel_path,
        "document_name": item.document_name,
    }
    if error:
        record["error"] = error
    with state_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _multipart_body(file_path: Path, upload_name: str, config: dict[str, Any]) -> tuple[bytes, str]:
    boundary = f"----sherpa-dify-{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    def add_text(name: str, value: str) -> None:
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        chunks.append(value.encode("utf-8"))
        chunks.append(b"\r\n")

    def add_file(name: str, path: Path, filename: str) -> None:
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        safe_filename = _ascii_filename_fallback(filename)
        encoded_filename = urllib.parse.quote(filename, safe="")
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{safe_filename}"; filename*=UTF-8\'\'{encoded_filename}\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(path.read_bytes())
        chunks.append(b"\r\n")

    add_text("data", json.dumps(config, ensure_ascii=False))
    add_file("file", file_path, upload_name)
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _ascii_filename_fallback(filename: str) -> str:
    fallback = []
    for char in filename:
        if char == '"':
            fallback.append("'")
        elif char == "\\":
            fallback.append("_")
        elif 32 <= ord(char) < 127:
            fallback.append(char)
        else:
            fallback.append("_")
    value = "".join(fallback).strip()
    return value or "upload.bin"


def _read_error_payload(exc: urllib.error.HTTPError) -> Any:
    raw = exc.read()
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return raw.decode("utf-8", errors="replace")


def _error_message(status: int, payload: Any) -> str:
    if isinstance(payload, dict):
        code = payload.get("code")
        message = payload.get("message")
        if code or message:
            return f"http_{status}: {code or 'error'}: {message or ''}".strip()
    return f"http_{status}: {payload}"


if __name__ == "__main__":
    raise SystemExit(main())
