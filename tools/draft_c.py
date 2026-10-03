#!/usr/bin/env python3
"""Turn one Ghidra pseudo-C function into C++ that MSVC 6 can compile.

The names and types are a starting point for a match. The function symbol is
_fn_<address>, which is what the bank file and the symbol list already use.
"""

import os
import re

_TYPES = (
    ("undefined8", "unsigned __int64"),
    ("undefined4", "unsigned int"),
    ("undefined2", "unsigned short"),
    ("undefined1", "unsigned char"),
    ("undefined", "int"),
    ("ulonglong", "unsigned __int64"),
    ("ushort", "unsigned short"),
    ("ulong", "unsigned long"),
    ("uint", "unsigned int"),
    ("dword", "unsigned int"),
    ("sbyte", "signed char"),
    ("byte", "unsigned char"),
    ("word", "unsigned short"),
    ("bool", "int"),
)
_TYPE_MAP = dict(_TYPES)
_TYPE_RE = re.compile(r"\b(?:%s)\b" % "|".join(name for name, _kind in sorted(_TYPES, key=lambda item: -len(item[0]))))
_GLOBAL_RE = re.compile(r"\b((?:DAT|PTR|UNK)_[0-9A-Fa-f]+)\b")


def draft(addr, pseudo):
    text = (pseudo or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if "{" not in text or text.startswith("Ghidra"):
        return ""
    text = _TYPE_RE.sub(lambda match: _TYPE_MAP[match.group(0)], text)
    text = re.sub(
        r"\b(?:FUN|Unwind)_([0-9A-Fa-f]+)\b",
        lambda match: "_fn_%08X" % int(match.group(1), 16),
        text,
    )
    name = "_fn_%08X" % addr
    found = re.search(r"(?m)^([^\n{]+?)\s+(_fn_[0-9A-Fa-f]+)\s*\(([^)]*)\)", text)
    if not found:
        return ""
    if found.group(2) != name:
        text = text[:found.start(2)] + name + text[found.end(2):]
        found = re.search(r"(?m)^([^\n{]+?)\s+(_fn_[0-9A-Fa-f]+)\s*\(([^)]*)\)", text)
    brace = text.find("{", found.end())
    if brace < 0:
        return ""
    body = text[brace:brace + _span(text, brace)]
    params = found.group(3).strip()
    if not params or params == "void":
        params = "void"
    globals_ = sorted(set(_GLOBAL_RE.findall(found.group(0) + "\n" + body)))
    callees = _callees(body, name)
    lines = ["// Drafted from Ghidra. This compiles; the types are a starting point.", ""]
    for item in globals_:
        lines.append("extern int %s;" % item)
    if globals_:
        lines.append("")
    for callee, _arity_count in callees:
        lines.append('extern "C" int %s(...);' % callee)
    if callees:
        lines.append("")
    lines.append('extern "C" %s %s(%s)' % (found.group(1).strip(), name, params))
    lines.append(body)
    return sanitize_draft("\n".join(lines) + "\n")


def _span(text, open_brace):
    depth = 0
    for index in range(open_brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index - open_brace + 1
    return len(text) - open_brace


def _callees(body, name):
    found = {}
    for match in re.finditer(r"\b(_fn_[0-9A-Fa-f]+)\s*\(", body):
        callee = match.group(1)
        if callee == name:
            continue
        arity = _arity(body, match.end() - 1)
        found[callee] = max(found.get(callee, 0), arity)
    return sorted(found.items())


_TYPE_WORD = (
    r"(?:unsigned\s+|signed\s+)?(?:void|char|short|int|long|float|double|__int64)"
)
_WIN_TYPES = {
    "DWORD": "unsigned long",
    "WORD": "unsigned short",
    "BYTE": "unsigned char",
    "BOOL": "int",
    "HRSRC": "void *",
    "HGLOBAL": "void *",
    "HHOOK": "void *",
    "HOOKPROC": "void *",
    "WPARAM": "unsigned int",
    "LPARAM": "long",
    "LRESULT": "long",
    "ATOM": "unsigned short",
    "COLORREF": "unsigned long",
    "UINT": "unsigned int",
    "LONG": "long",
    "HWND": "void *",
    "HDC": "void *",
    "HGDIOBJ": "void *",
    "HINSTANCE": "void *",
    "HMODULE": "void *",
    "HANDLE": "void *",
    "LPVOID": "void *",
    "LPCSTR": "char *",
    "LPSTR": "char *",
    "LPCVOID": "void *",
}
_WIN_FUNCS = {
    "GetTopWindow": ("void *", "void *"),
    "GetDC": ("void *", "void *"),
    "ReleaseDC": ("int", "void *, void *"),
    "GetDesktopWindow": ("void *", ""),
    "GetWindowDC": ("void *", "void *"),
    "GetWindow": ("void *", "void *, unsigned int"),
    "SetRect": ("int", "tagRECT *, int, int, int, int"),
    "__ftol": ("long", "void"),
    "timeGetTime": ("unsigned long", "void"),
}


def sanitize_draft(text, earlier_file=""):
    """Make one Ghidra draft acceptable to MSVC 6 C++.

    C++ will not assign an int to a pointer. The draft keeps Ghidra's pointer
    locals, declares the symbols those locals point at, and writes the cast.
    """
    block = (text or "").replace("\r\n", "\n")
    block = block.replace("typedef void code();", "typedef int __cdecl code(...);")
    block = block.replace("typedef void __cdecl code(...);", "typedef int __cdecl code(...);")
    if "Drafted from Ghidra" not in block or "{" not in block:
        return text
    found = _definition(block)
    if not found:
        return text
    at, paren_end, brace = found
    signature = block[at:paren_end + 1].replace("__thiscall", " ")
    signature = re.sub(r"[ \t]{2,}", " ", signature)
    body = _ghidra_type_words(block[brace:])
    body = _stack_locals(body)
    types = _declared_types(signature + "\n" + body)
    types["ExceptionList"] = "void *"
    body = _ghidra_bytes(body)
    body = _cast_addresses(body, types)
    body = _cast_pointer_args(body, types)
    extra = _support_decls(signature + "\n" + body, earlier_file + "\n" + block[:at])
    return block[:at] + extra + signature + "\n" + body


def rewrite_banks(root):
    bank_dir = os.path.join(root, "src", "bank")
    changed = []
    if not os.path.isdir(bank_dir):
        return changed
    for name in sorted(os.listdir(bank_dir)):
        if not name.endswith(".cpp"):
            continue
        path = os.path.join(bank_dir, name)
        if prepare_file(path):
            changed.append(name)
    return changed


def prepare_file(path):
    if not os.path.isfile(path):
        return False
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        original = handle.read().replace("\r\n", "\n")
    updated = re.sub(r"\n{3,}", "\n\n", _relax_file(_rewrite_source(original)))
    if updated == original:
        return False
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(updated)
    return True


def _rewrite_source(text):
    pattern = re.compile(r"// FN ([0-9A-Fa-f]{8})\n(.*?)// END \1\n", re.S)
    pieces = []
    last = 0
    for match in pattern.finditer(text):
        pieces.append(text[last:match.start()])
        inner = match.group(2)
        if "Drafted from Ghidra" not in inner:
            pieces.append(match.group(0))
        else:
            cleaned = sanitize_draft(inner, "".join(pieces))
            if not cleaned.endswith("\n"):
                cleaned += "\n"
            pieces.append("// FN %s\n%s// END %s\n" % (match.group(1), cleaned, match.group(1)))
        last = match.end()
    pieces.append(text[last:])
    return "".join(pieces)


def _definition(block):
    index = 0
    token = 'extern "C"'
    while True:
        at = block.find(token, index)
        if at < 0:
            return None
        paren = block.find("(", at)
        if paren < 0:
            return None
        end = _close(block, paren, "(", ")")
        if end is None:
            return None
        if block[end + 1:].lstrip().startswith("{"):
            brace = end + 1 + (len(block[end + 1:]) - len(block[end + 1:].lstrip()))
            return at, end, brace
        index = end + 1


def _close(text, open_at, left, right):
    depth = 0
    for index in range(open_at, len(text)):
        if text[index] == left:
            depth += 1
        elif text[index] == right:
            depth -= 1
            if depth == 0:
                return index
    return None


def _declared_types(text):
    found = {}
    pattern = re.compile(r"\b(%s)\s+((?:\*+\s*)?\w+(?:\s*,\s*(?:\*+\s*)?\w+)*)\s*(?=[,;)=])" % _TYPE_WORD)
    for match in pattern.finditer(text):
        base = re.sub(r"\s+", " ", match.group(1)).strip()
        for stars, name in re.findall(r"(\**)\s*(\w+)", match.group(2)):
            if name.startswith("_fn_") or name.startswith("__"):
                continue
            pointer = (" " + stars) if stars else ""
            found[name] = base + pointer
    for name, repl in _WIN_TYPES.items():
        if re.search(r"\b%s\b" % name, text):
            found.setdefault(name, repl)
    return found


def _cast_addresses(body, types):
    lines = []
    for line in body.split("\n"):
        match = re.match(r"^(\s*)(.+?)\s*=\s*(&\s*.+);\s*$", line)
        if not match or match.group(3).lstrip().startswith("("):
            lines.append(line)
            continue
        cast = _lhs_cast(match.group(2).strip(), types)
        if not cast:
            lines.append(line)
            continue
        lines.append("%s%s = (%s)%s;" % (match.group(1), match.group(2).strip(), cast, match.group(3).strip()))
    return "\n".join(lines)


def _lhs_cast(lhs, types):
    stars = re.match(r"^(\*+)(\w+)$", lhs)
    if stars:
        base = types.get(stars.group(2), "int *")
        count = base.count("*") - len(stars.group(1))
        plain = base.replace("*", "").strip() or "int"
        if count > 0:
            return plain + " " + ("*" * count)
        return plain
    name = re.match(r"^(\w+)$", lhs)
    if name and name.group(1) in types:
        return types[name.group(1)]
    if lhs == "ExceptionList":
        return "void *"
    return ""


def _cast_pointer_args(body, types):
    pieces = []
    index = 0
    while index < len(body):
        match = re.search(r"\b_fn_[0-9A-Fa-f]+\s*\(", body[index:])
        if not match:
            pieces.append(body[index:])
            break
        start = index + match.end()
        end = _close(body, start - 1, "(", ")")
        if end is None:
            pieces.append(body[index:])
            break
        pieces.append(body[index:start])
        pieces.append(_cast_arg_list(body[start:end], types))
        index = end
    return "".join(pieces)


def _cast_arg_list(text, types):
    args = []
    buf = []
    depth = 0
    for char in text:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == "," and depth == 0:
            args.append("".join(buf))
            buf = []
            continue
        buf.append(char)
    args.append("".join(buf))
    casted = []
    for arg in args:
        inner = _cast_pointer_args(arg, types).strip()
        if re.match(r"^[A-Za-z_]\w*$", inner) and "*" in types.get(inner, ""):
            inner = "(int)%s" % inner
        casted.append(inner)
    return ", ".join(casted)


def _ghidra_type_words(text):
    def replace(match):
        kind, width = match.group(1), int(match.group(2))
        wide = width > 4
        if kind == "uint":
            return "unsigned __int64" if wide else "unsigned int"
        return "__int64" if wide else "int"

    return re.sub(r"\b(uint|int)(\d+)\b", replace, text)


def _stack_locals(body):
    names = []
    for name in re.findall(r"\b(stack0x[0-9A-Fa-f]+)\b", body):
        if name not in names:
            names.append(name)
    declared = set(re.findall(r"\b(?:int|unsigned int|void \*)\s+(stack0x[0-9A-Fa-f]+)\b", body))
    missing = [name for name in names if name not in declared]
    if not missing or not body.startswith("{"):
        return body
    added = "\n".join("  int %s;" % name for name in missing)
    newline = body.find("\n")
    if newline < 0:
        return "{\n" + added + "\n" + body[1:]
    return body[:newline + 1] + added + body[newline:]


def _declare_thunks(text):
    missing = []
    for name in re.findall(r"\b(thunk_FUN_[0-9A-Fa-f]+)\b", text):
        if name in missing:
            continue
        if re.search(r'extern "C"[^;\n]*\b%s\b' % name, text):
            continue
        missing.append(name)
    if not missing:
        return text
    block = "\n".join('extern "C" int %s(...);' % name for name in missing) + "\n\n"
    at = text.find("// FN ")
    if at < 0:
        return block + text
    return text[:at] + block + text[at:]


def _sync_prototypes(text, defs):
    lines = []
    for line in text.split("\n"):
        match = re.match(r'\s*extern "C".*\b(_fn_[0-9A-Fa-f]+)\s*\(.*\)\s*;\s*$', line)
        if not match or match.group(1) not in defs:
            lines.append(line)
            continue
        want = defs[match.group(1)] + ";"
        if re.sub(r"\s+", " ", line).strip() == want:
            lines.append(line)
        else:
            lines.append(want)
    return "\n".join(lines)


def _ghidra_bytes(body):
    def assign(match):
        name, start, width, value = match.group(1), int(match.group(2)), int(match.group(3)), match.group(4).strip()
        shift = start * 8
        mask = (1 << (width * 8)) - 1
        if shift == 0:
            return "%s = (%s & ~0x%xU) | ((%s) & 0x%xU)" % (name, name, mask, value, mask)
        return "%s = (%s & ~0x%xU) | (((%s) & 0x%xU) << %d)" % (name, name, mask << shift, value, mask, shift)

    def read(match):
        name, start, width = match.group(1), int(match.group(2)), int(match.group(3))
        shift = start * 8
        mask = (1 << (width * 8)) - 1
        if shift == 0:
            return "((%s) & 0x%xU)" % (name, mask)
        return "(((%s) >> %d) & 0x%xU)" % (name, shift, mask)

    body = re.sub(r"\b([A-Za-z_]\w*)\._(\d)_(\d)_\s*=(?!=)\s*([^;{}\n]+)", assign, body)
    body = re.sub(r"\b([A-Za-z_]\w*)\._(\d)_(\d)_", read, body)
    return _replace_concat(body)


def _replace_concat(body):
    pieces = []
    index = 0
    while index < len(body):
        match = re.search(r"\bCONCAT(\d)(\d)\s*\(", body[index:])
        if not match:
            pieces.append(body[index:])
            break
        start = index + match.start()
        paren = index + match.end() - 1
        end = _close(body, paren, "(", ")")
        if end is None:
            pieces.append(body[index:])
            break
        args = _split_top(body[paren + 1:end])
        hi = args[0] if args else "0"
        lo = args[1] if len(args) > 1 else "0"
        low_bits = int(match.group(2)) * 8
        mask = 0xffffffff if low_bits >= 32 else (1 << low_bits) - 1
        pieces.append(body[index:start])
        pieces.append("(((unsigned int)(%s) << %d) | ((%s) & 0x%xU))" % (hi.strip(), low_bits, lo.strip(), mask))
        index = end + 1
    return "".join(pieces)


def _split_top(text):
    args = []
    buf = []
    depth = 0
    for char in text:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == "," and depth == 0:
            args.append("".join(buf))
            buf = []
            continue
        buf.append(char)
    if buf or args:
        args.append("".join(buf))
    return args


def _c_names(text):
    """C++ mangling hides fn_00631190 as ?fn_00631190@@YAHXZ. Give every one C linkage."""
    text = re.sub(
        r"\bfn_0x([0-9A-Fa-f]+)\b",
        lambda match: "_fn_%08X" % int(match.group(1), 16),
        text,
    )
    text = re.sub(
        r"(?<![_A-Za-z0-9])fn_([0-9A-Fa-f]{8})\b",
        lambda match: "_fn_%s" % match.group(1).upper(),
        text,
    )
    lines = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if 'extern "C"' in line or not re.search(r"\b_fn_[0-9A-Fa-f]{8}\s*\(", line):
            lines.append(line)
            continue
        if re.match(r"(?:return|if|for|while|switch|do|else|case)\b", stripped):
            lines.append(line)
            continue
        if stripped.startswith("extern "):
            lines.append(line.replace("extern ", 'extern "C" ', 1))
            continue
        if re.match(r"\s*(?:[A-Za-z_][\w\s\*]*\s+)_fn_[0-9A-Fa-f]{8}\s*\(", line):
            lines.append(re.sub(r"^(\s*)", r'\1extern "C" ', line, count=1))
            continue
        lines.append(line)
    joined = "\n".join(lines)
    if text.endswith("\n"):
        joined += "\n"
    return joined


def _rename_block_functions(text):
    """The split symbol is _fn_<address>. A FUN_<address> definition does not emit that symbol."""
    pattern = re.compile(r"// FN ([0-9A-Fa-f]{8})\n(.*?)// END \1\n", re.S)

    def repl(match):
        addr = match.group(1).upper()
        body = re.sub(r"\bFUN_%s\b" % addr, "_fn_%s" % addr, match.group(2), flags=re.I)
        return "// FN %s\n%s// END %s\n" % (match.group(1), body, match.group(1))

    return pattern.sub(repl, text)


def _relax_file(text):
    """One prototype per function. Calls go through a cast so an int and a pointer can share it."""
    text = _c_names(_rename_block_functions(text))
    def drafted(match):
        inner = match.group(2)
        if "Drafted from Ghidra" not in inner:
            return match.group(0)
        lines = []
        for line in inner.split("\n"):
            if re.match(r'\s*extern "C".*\b_fn_[0-9A-Fa-f]+\s*\(.*\)\s*;\s*$', line):
                continue
            lines.append(_cast_fn_calls(line))
        cleaned = "\n".join(lines)
        if not cleaned.endswith("\n"):
            cleaned += "\n"
        return "// FN %s\n%s// END %s\n" % (match.group(1), cleaned, match.group(1))

    text = re.sub(r"// FN ([0-9A-Fa-f]{8})\n(.*?)// END \1\n", drafted, text, flags=re.S)
    text = _dedupe_typedefs(text)
    text = _declare_thunks(text)
    defs = _definition_signatures(text)
    text = _sync_prototypes(text, defs)
    declared = {}
    for match in re.finditer(r'extern "C"[^;{]*\b(_fn_[0-9A-Fa-f]+)\s*\(', text):
        declared.setdefault(match.group(1), match.start())
    needed = []
    for match in re.finditer(r"\b(_fn_[0-9A-Fa-f]+)\)\s*\(", text):
        name = match.group(1)
        if name in declared and declared[name] < match.start():
            continue
        if name not in needed:
            needed.append(name)
    if needed:
        lines = []
        for name in needed:
            signature = defs.get(name)
            if signature:
                lines.append(signature + ";")
            else:
                lines.append('extern "C" int %s(...);' % name)
        block = "\n".join(lines) + "\n\n"
        at = text.find("// FN ")
        text = block + text if at < 0 else text[:at] + block + text[at:]
    return _collapse_typedefs(_cast_global_stores(_declare_externs(_hoist_typedefs(text))))


def _cast_global_stores(text):
    names = set(re.findall(r"extern int ((?:DAT|PTR)_[0-9A-Fa-f]+)\s*;", text))
    if not names:
        return text

    def replace(match):
        name, rhs = match.group(1), match.group(2).strip()
        if name not in names or rhs.startswith("(int)") or rhs.startswith("(int)("):
            return match.group(0)
        return "%s = (int)(%s);" % (name, rhs)

    return re.sub(r"\b((?:DAT|PTR)_[0-9A-Fa-f]+)\s*=\s*([^;\n]+);", replace, text)


_NOT_FUNCTIONS = {
    "if", "for", "while", "switch", "return", "sizeof", "do", "goto", "else", "case",
    "asm", "__asm", "int3", "nop", "ret", "jmp", "call", "near", "far",
}


def _declare_externs(text):
    kept = []
    for line in text.splitlines():
        match = re.search(r'extern "C"[^;]*\b([A-Za-z_][A-Za-z0-9_]*)\s*\(', line)
        if match and match.group(1) in _NOT_FUNCTIONS:
            continue
        kept.append(line)
    text = "\n".join(kept)
    if not text.endswith("\n"):
        text += "\n"
    calls = []
    for match in re.finditer(r"(?<![\w.])([A-Za-z_][A-Za-z0-9_]*)\s*\(", text):
        name = match.group(1)
        if name in calls or name in _NOT_FUNCTIONS:
            continue
        if name.startswith("_fn_") or name.startswith("thunk_"):
            continue
        calls.append(name)
    strings = []
    for name in re.findall(r"\b(s_[A-Za-z0-9_]+)\b", text):
        if name not in strings:
            strings.append(name)
    lines = []
    for name in calls:
        if re.search(r'(?:extern "C"[^;\n]*|\btypedef [^;\n]*|(?:^|\n)\s*void\s+)\b%s\b' % name, text):
            continue
        if re.search(r"(?m)^[^\n]*\b%s\s*\([^;\n]*\)\s*;" % re.escape(name), text):
            continue
        if re.search(r"\b%s\s*\([^;]*\)\s*\{" % name, text):
            continue
        convention = "__stdcall " if name[:1].isupper() else ""
        lines.append('extern "C" int %s%s(...);' % (convention, name))
    for name in strings:
        if "extern char %s" % name in text:
            continue
        lines.append("extern char %s[];" % name)
    if not lines:
        return text
    block = "\n".join(lines) + "\n\n"
    at = text.find("// FN ")
    if at < 0:
        return block + text
    return text[:at] + block + text[at:]


def _hoist_typedefs(text):
    kept = []
    typedefs = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("typedef "):
            if stripped not in typedefs:
                typedefs.append(stripped)
            continue
        kept.append(line)
    body = "\n".join(kept)
    for name, repl in _WIN_TYPES.items():
        decl = "typedef %s %s;" % (repl, name)
        if re.search(r"\b%s\b" % name, body) and decl not in typedefs:
            typedefs.append(decl)
    if re.search(r"\btagRECT\b", body) and not any("tagRECT" in item for item in typedefs):
        typedefs.insert(0, "typedef struct tagRECT { long left; long top; long right; long bottom; } tagRECT;")
    if not typedefs:
        return text
    return "\n".join(typedefs) + "\n\n" + body.lstrip("\n")


def _dedupe_typedefs(text):
    seen = set()
    lines = []
    for line in text.split("\n"):
        key = line.strip()
        if key.startswith("typedef "):
            if key in seen:
                continue
            seen.add(key)
        lines.append(line)
    return "\n".join(lines)


def _cast_fn_calls(line):
    if 'extern "C"' in line or "typedef " in line:
        return line

    def replace(match):
        return "(*(int (__cdecl *)(...))%s)(" % match.group(1)

    return re.sub(r"(?<!\(\.\.\.\)\))(_fn_[0-9A-Fa-f]+)\s*\(", replace, line)


def _collapse_typedefs(text):
    """One typedef per name. `code` stays the int function type the drafts call through."""
    name_re = re.compile(r"^typedef\s+.+\b([A-Za-z_]\w*)\s*(?:\([^;]*)?;\s*$")
    lines = text.split("\n")
    groups = {}
    for index, line in enumerate(lines):
        match = name_re.match(line.strip())
        if match:
            groups.setdefault(match.group(1), []).append(index)
    drop = set()
    for name, indexes in groups.items():
        if len(indexes) < 2:
            continue
        keep = indexes[0]
        if name == "code":
            for index in indexes:
                if "int __cdecl code(" in lines[index]:
                    keep = index
                    break
        for index in indexes:
            if index != keep:
                drop.add(index)
    if not drop:
        return text
    return "\n".join(line for index, line in enumerate(lines) if index not in drop)


def _definition_signatures(text):
    found = {}
    index = 0
    token = 'extern "C"'
    while True:
        at = text.find(token, index)
        if at < 0:
            return found
        paren = text.find("(", at)
        if paren < 0:
            return found
        end = _close(text, paren, "(", ")")
        if end is None:
            return found
        if text[end + 1:].lstrip().startswith("{"):
            signature = re.sub(r"\s+", " ", text[at:end + 1]).strip()
            name = re.search(r"\b(_fn_[0-9A-Fa-f]+)\b", signature)
            if name:
                found[name.group(1)] = signature
        index = end + 1


def _support_decls(text, earlier):
    lines = []
    if "ExceptionList" in text and "void *ExceptionList" not in earlier:
        lines.append("extern void *ExceptionList;")
    if re.search(r"\bcode\b", text) and "typedef int __cdecl code(" not in earlier and "typedef void __cdecl code(" not in earlier:
        lines.append("typedef int __cdecl code(...);")
    for name in sorted(set(re.findall(r"&(LAB_[0-9A-Fa-f]+)\b", text))):
        if name + "(" not in earlier:
            lines.append("void %s(void);" % name)
    if re.search(r"\btagRECT\b", text) and "struct tagRECT" not in earlier:
        lines.append("typedef struct tagRECT { long left; long top; long right; long bottom; } tagRECT;")
    for name in sorted(set(re.findall(r"\b(PTR_(?:FUN|LAB)_[0-9A-Fa-f]+)\b", text))):
        if name not in earlier:
            lines.append("extern int %s;" % name)
    for name, (ret, args) in _WIN_FUNCS.items():
        if re.search(r"\b%s\s*\(" % name, text) and name not in earlier:
            arg = args or "void"
            lines.append('extern "C" %s __stdcall %s(%s);' % (ret, name, arg))
    for name, repl in _WIN_TYPES.items():
        if re.search(r"\b%s\b" % name, text) and name not in earlier:
            lines.append("typedef %s %s;" % (repl, name))
    if not lines:
        return ""
    return "\n".join(lines) + "\n\n"


def _arity(text, open_paren):
    depth = 0
    args = 0
    started = False
    for char in text[open_paren:]:
        if char == "(":
            depth += 1
            continue
        if char == ")":
            depth -= 1
            if depth == 0:
                return args + 1 if started else 0
            continue
        if depth == 1:
            if char == "," and depth == 1:
                args += 1
            elif not char.isspace():
                started = True
    return args
