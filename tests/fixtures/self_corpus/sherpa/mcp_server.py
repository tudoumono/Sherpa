def tool_defs():
    return [
        {"name": "ping_tool", "description": "疎通", "inputSchema": {"type": "object"}},
        {"name": "orphan_tool", "description": "設計書に出ない", "inputSchema": {"type": "object"}},
        {"name": "ping_tool", "description": "重複定義", "inputSchema": {"type": "object"}},
    ]
