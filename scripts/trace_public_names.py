"""`scripts/conversation_trace.py`（make trace）が、伏せ字（MASK=1）でもそのまま出してよい公開の名前の表
（公開のモデル名・拡張子・コマンドの名前）。"""
from __future__ import annotations

OPENAI_PUBLIC_MODELS = frozenset({
    "gpt-3.5-turbo", "gpt-4", "gpt-4-turbo", "gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini",
    "gpt-4.1-nano", "gpt-4.5-preview", "gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-5-codex", "gpt-5.1",
    "gpt-5.1-codex", "gpt-5.1-codex-mini", "gpt-5.1-codex-max", "gpt-5.2", "gpt-5.2-codex", "gpt-5.3-codex",
    "gpt-5.4", "gpt-5.4-mini", "gpt-5.5", "gpt-5.6", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-6-astra",
    "gpt-6-luna", "o1", "o1-mini", "o1-pro", "o3", "o3-mini", "o3-pro", "o4-mini", "codex-mini-latest",
    "gpt-oss-20b", "gpt-oss-120b", "text-embedding-3-small", "text-embedding-3-large",
    "text-embedding-ada-002",
    "gpt-4o-2024-05-13", "gpt-4o-2024-08-06", "gpt-4o-2024-11-20", "gpt-4o-mini-2024-07-18",
    "gpt-4-turbo-2024-04-09", "gpt-4.1-2025-04-14", "gpt-4.1-mini-2025-04-14", "gpt-4.1-nano-2025-04-14",
    "o1-2024-12-17", "o1-mini-2024-09-12", "o3-2025-04-16", "o3-mini-2025-01-31", "o4-mini-2025-04-16",
    "gpt-5-2025-08-07", "gpt-5-mini-2025-08-07", "gpt-5-nano-2025-08-07",
})
OLLAMA_TAGS = frozenset({"latest", "0.5b", "1b", "1.5b", "2b", "3b", "4b", "7b", "8b", "9b", "12b", "13b",
                          "14b", "20b", "27b", "32b", "34b", "70b", "72b", "120b", "235b", "671b"})
OLLAMA_PUBLIC = frozenset({"llama3", "llama3.1", "llama3.2", "llama3.3", "llama4", "qwen2.5", "qwen2.5-coder", "qwen3",
                  "qwen3-coder", "gemma2", "gemma3", "mistral", "mistral-nemo", "mixtral", "phi3", "phi4",
                  "deepseek-r1", "deepseek-coder-v2", "codellama", "command-r", "granite3.3", "nomic-embed-text",
                  "mxbai-embed-large", "bge-m3", "gpt-oss"})
# 伏せ字でもそのまま付けてよい拡張子（小文字で比べる）。
PUBLIC_EXTS = frozenset({
    ".md", ".txt", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".html", ".htm", ".pdf", ".doc",
    ".docx", ".docm", ".xls", ".xlsx", ".xlsm", ".ppt", ".pptx", ".rtf", ".log", ".ini", ".cfg", ".conf",
    ".properties", ".sql", ".ddl", ".cbl", ".cob", ".cpy", ".jcl", ".java", ".js", ".ts", ".py", ".c",
    ".h", ".cpp", ".hpp", ".cs", ".vb", ".bas", ".sh", ".bat", ".ps1", ".go", ".rb", ".php", ".kt",
    ".pl", ".toml", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".vsd", ".vsdx", ".zip",
})
PUBLIC_PROGRAMS = frozenset({
    "rg", "grep", "egrep", "fgrep", "sed", "awk", "cat", "head", "tail", "ls", "find", "wc", "nl", "sort",
    "uniq", "cut", "tr", "file", "stat", "du", "git", "jq", "diff", "echo", "printf", "pwd", "cd", "test",
    "xargs", "iconv", "nkf", "od", "xxd", "strings", "bash", "sh", "zsh", "perl", "python", "python3",
    "node", "timeout", "env", "true", "mkdir", "cp", "mv", "rm", "tree", "realpath", "basename",
    "dirname", "date", "sha256sum", "md5sum", "cmp", "comm", "column", "tee", "touch", "readlink",
    "unzip", "zip", "tar", "gzip", "zcat", "pdftotext", "fd", "less", "more",
})


def public_model(value: str) -> bool:
    if value in OPENAI_PUBLIC_MODELS:
        return True
    name, sep, tag = value.partition(":")
    return name in OLLAMA_PUBLIC and (not sep or tag in OLLAMA_TAGS)
