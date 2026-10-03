#!/usr/bin/env python3
"""MCP server for one decomp project. Point an AI client at this command.

The tools return the same four views as the workbench: C/C++, pseudo C,
source assembly from the original executable, and compiled assembly from the C.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import workspace

PROJECT = None

TOOLS = [
    {
        "name": "list_banks",
        "description": "List bank slices in the project.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_functions",
        "description": "List functions in one bank. bank is like bank/00401000.",
        "inputSchema": {
            "type": "object",
            "properties": {"bank": {"type": "string"}},
            "required": ["bank"],
        },
    },
    {
        "name": "get_function",
        "description": "C/C++ source, Ghidra pseudo C, source assembly from the executable, and compiled assembly from the C.",
        "inputSchema": {
            "type": "object",
            "properties": {"addr": {"type": "string"}},
            "required": ["addr"],
        },
    },
    {
        "name": "save_cpp",
        "description": "Write this function into the bank's C++ file and the symbol list.",
        "inputSchema": {
            "type": "object",
            "properties": {"addr": {"type": "string"}, "code": {"type": "string"}},
            "required": ["addr", "code"],
        },
    },
    {
        "name": "score",
        "description": "Save, re-split, compile the bank with MSVC 6 /O2, and return the objdiff percent plus compiled assembly.",
        "inputSchema": {
            "type": "object",
            "properties": {"addr": {"type": "string"}, "code": {"type": "string"}},
            "required": ["addr"],
        },
    },
    {
        "name": "build_report",
        "description": "Write objdiff.json and build/report.json for the project.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _text(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}]}


def call_tool(name, args):
    proj = PROJECT
    if name == "list_banks":
        return proj.banks()
    if name == "list_functions":
        return proj.functions(args["bank"])
    addr = int(str(args.get("addr", "0")), 0) if "addr" in args else 0
    if name == "get_function":
        bank = workspace.bank_of(proj, addr)
        row = next(item for item in proj.functions(bank["name"]) if item["addr"] == addr)
        return {
            "addr": addr,
            "bank": bank["name"],
            "size": row["size"],
            "cpp": proj.read_cpp(bank["name"], addr),
            "pseudo_c": proj.pseudo_c(addr),
            "assembly": proj.assembly(addr, row["size"]),
            "source_assembly": proj.source_assembly(bank["name"], addr),
        }
    if name == "save_cpp":
        bank = workspace.bank_of(proj, addr)
        row = next(item for item in proj.functions(bank["name"]) if item["addr"] == addr)
        path = proj.save_cpp(bank["name"], addr, row["size"], args.get("code") or "")
        return {"path": path}
    if name == "score":
        bank = workspace.bank_of(proj, addr)
        row = next(item for item in proj.functions(bank["name"]) if item["addr"] == addr)
        if args.get("code"):
            proj.save_cpp(bank["name"], addr, row["size"], args["code"])
        return proj.score(bank["name"], addr, row["size"])
    if name == "build_report":
        return proj.build_report()
    raise ValueError("unknown tool " + name)


def handle(message):
    method = message.get("method")
    msg_id = message.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "pc-decomp-workbench", "version": "0.1.0"},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params") or {}
        try:
            payload = call_tool(params.get("name"), params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": msg_id, "result": _text(payload)}
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True},
            }
    if msg_id is None:
        return None
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "method not found"}}


def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if line in (b"", b"\n", b"\r\n"):
            break
        key, value = line.decode("utf-8").split(":", 1)
        headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length") or 0)
    if not length:
        return None
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def write_message(payload):
    body = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(("Content-Length: %s\r\n\r\n" % len(body)).encode("ascii"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def main():
    global PROJECT
    if len(sys.argv) < 2:
        sys.exit("usage: mcp_server.py <project folder>")
    PROJECT = workspace.Project(sys.argv[1])
    while True:
        message = read_message()
        if message is None:
            break
        reply = handle(message)
        if reply is not None:
            write_message(reply)


if __name__ == "__main__":
    main()
