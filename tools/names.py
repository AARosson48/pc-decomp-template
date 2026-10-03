"""Rename one function everywhere a source name is written.

The source uses the name you chose. The symbol list stores the decorated
name MSVC emits for that definition, which is what objdiff pairs. A C++
symbol is shown through Microsoft's UNDNAME. A cdecl or fastcall decoration
is stripped locally, because UNDNAME leaves those unchanged.
"""

import os
import re
import subprocess

KEYWORDS = {
    "auto", "break", "case", "char", "const", "continue", "default", "do",
    "double", "else", "enum", "extern", "float", "for", "goto", "if", "int",
    "long", "register", "return", "short", "signed", "sizeof", "static",
    "struct", "switch", "typedef", "union", "unsigned", "void", "volatile",
    "while",
}
SOURCE_EXT = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".inl"}
_DEMANGLED = {}


def display_name(symbol, undname=""):
    text = str(symbol or "")
    if text.startswith("?"):
        decoded = _undname(text, undname)
        match = re.search(r"([A-Za-z_][A-Za-z0-9_]*::)*[A-Za-z_][A-Za-z0-9_]*(?=\()", decoded)
        if match:
            return match.group(0)
        return decoded or text
    match = re.fullmatch(r"@+([A-Za-z_][A-Za-z0-9_]*)@\d+", text)
    if match:
        return match.group(1)
    match = re.fullmatch(r"_([A-Za-z_][A-Za-z0-9_]*)@\d+", text)
    if match:
        return match.group(1)
    if text.startswith("_") and not text.startswith("__imp"):
        return text[1:]
    return text


def decorate(name, signature, symbol):
    """The symbol objdiff should see for this definition."""
    current = str(symbol or "")
    if current.startswith("?"):
        return re.sub(r"^\?[^@]+", "?" + name, current, count=1)
    params = ""
    found = re.search(r"\([^;]*\)", signature or "")
    if found:
        params = found.group(0)[1:-1]
    if "__fastcall" in (signature or ""):
        return "@%s@%d" % (name, _arg_bytes(params))
    if "__stdcall" in (signature or ""):
        return "_%s@%d" % (name, _arg_bytes(params))
    return "_" + name


def check_name(name):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""):
        raise ValueError("A function name is a C identifier.")
    if name in KEYWORDS:
        raise ValueError("%s is a C keyword." % name)
    return name


def address_names(addr):
    text = "%08X" % addr
    return ["__fn_" + text, "_fn_" + text, "fn_" + text, "FUN_" + text]


def replace_identifiers(text, addr, old_name, new_name):
    forms = address_names(addr)
    pattern = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(item) for item in sorted(forms, key=len, reverse=True)), re.I)
    updated = pattern.sub(new_name, text)
    old = old_name or ""
    if old and old != new_name and old.lower() not in {item.lower() for item in forms}:
        updated = re.sub(r"\b%s\b" % re.escape(old), new_name, updated)
    return updated


def symbol_at(text, addr):
    match = re.search(r"(?m)^(\S+) = \.text:0x%08X;" % addr, text or "")
    return match.group(1) if match else ""


def rewrite_symbol_line(text, addr, symbol, size):
    pattern = re.compile(r"(?m)^(\S+) = \.text:0x%08X; // type:function size:0x[0-9A-Fa-f]+\s*$" % addr)
    line = "%s = .text:0x%08X; // type:function size:0x%X" % (symbol, addr, size)
    if pattern.search(text or ""):
        return pattern.sub(line, text, count=1), True
    if text and not text.endswith("\n"):
        text += "\n"
    return (text or "") + line + "\n", False


def rewrite_reccmp(text, addr, symbol):
    pattern = re.compile(r"(?m)^(0x%08X\|)[^|]+(\|)" % addr, re.I)
    if not pattern.search(text or ""):
        return text, False
    return pattern.sub(r"\g<1>%s\2" % symbol, text, count=1), True


def source_files(root):
    found = []
    for folder in ("src", "include"):
        base = os.path.join(root, folder)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirs, files in os.walk(base):
            for name in files:
                if os.path.splitext(name)[1].lower() in SOURCE_EXT:
                    found.append(os.path.join(dirpath, name))
    return found


def _arg_bytes(params):
    body = (params or "").strip()
    if not body or body == "void":
        return 0
    total = 0
    depth = 0
    current = []
    parts = []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    if current:
        parts.append("".join(current))
    for part in parts:
        text = part.strip()
        if not text or text == "...":
            continue
        if re.search(r"\b(?:double|long\s+long|__int64)\b", text):
            total += 8
        else:
            total += 4
    return total


def _undname(symbol, undname):
    if symbol in _DEMANGLED:
        return _DEMANGLED[symbol]
    if not undname or not os.path.isfile(undname):
        return symbol
    proc = subprocess.run([undname, "-f", symbol], capture_output=True, text=True)
    decoded = symbol
    for line in (proc.stdout or "").splitlines():
        if "==" in line:
            decoded = line.split("==", 1)[1].strip()
            break
    _DEMANGLED[symbol] = decoded
    return decoded
