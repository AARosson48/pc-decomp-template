"""Compile-error cases.

A case matches one MSVC diagnostic. It edits the C file, or it reports that
there is no automatic fix. Nothing in here invents a new function.
"""

import os
import re

_ERROR_RE = re.compile(
    r"^(?P<file>.+?)\((?P<line>\d+)\)\s*:\s*error\s+(?P<code>C\d+)\s*:\s*(?P<detail>.*)$"
)
_NAME_RE = re.compile(r"'([A-Za-z_][A-Za-z0-9_]*)'")
_GLUED_RE = re.compile(
    r'^extern "C"\s+(.+?=\s*[^;]+);\s*(_fn_[0-9A-Fa-f]+\s*\([^)]*\))\s*$'
)
_CALL_CAST_RE = re.compile(
    r"^(?P<indent>\s*)(?P<lhs>.+?)\s*=\s*\((?P<cast>[A-Za-z_][\w\s\*]*)\)\s*"
    r"(?P<fn>\(\*\(int \(__cdecl \*\)\(\.\.\.\)\)_fn_[0-9A-Fa-f]+\))\((?P<args>.*)\);\s*$"
)
_ASSIGN_RE = re.compile(r"^(?P<indent>\s*)(?P<lhs>.+?)\s*=\s*(?P<rhs>.+);\s*$")
_RESOURCE_COMMA_RE = re.compile(
    r"(?P<ptr>\w+)\s*=\s*(?P<fn>LoadResource|FindResourceA|LockResource)\((?P<args>[^()]*)\)"
    r"\s*,\s*(?P<num>\w+)\s*=\s*(?P=ptr)\b"
)

# Return type, argument list. Used only when the compiler says the name is missing.
_WIN_PROTOTYPES = {
    "LoadResource": 'extern "C" void * __stdcall LoadResource(void *module, void *resource);',
    "FindResourceA": 'extern "C" void * __stdcall FindResourceA(void *module, char *name, char *type);',
    "LockResource": 'extern "C" void * __stdcall LockResource(void *resource);',
    "FreeResource": 'extern "C" int __stdcall FreeResource(void *resource);',
}

# Lower runs first on the same line.
_ORDER = {
    "C2598": 0,
    "C2059": 1,
    "C2040": 2,
    "C2100": 3,
    "C2668": 4,
    "C2065": 5,
    "C2440": 6,
}


def parse_errors(log):
    found = []
    for raw in (log or "").splitlines():
        match = _ERROR_RE.match(raw.strip())
        if not match:
            continue
        found.append({
            "file": match.group("file").strip(),
            "line": int(match.group("line")),
            "code": match.group("code"),
            "detail": match.group("detail").strip(),
        })
    return found


def apply(root, log):
    errors = parse_errors(log)
    if not errors:
        return {"changed": False, "report": log, "fixed": [], "open": []}
    rel = errors[0]["file"].replace("/", os.sep)
    path = rel if os.path.isabs(rel) else os.path.join(root, rel)
    if not os.path.isfile(path):
        return {
            "changed": False,
            "report": "No automatic fix.\nThe compiler named %s, and that file is not in this project.\n\n%s" % (errors[0]["file"], log),
            "fixed": [],
            "open": [],
        }
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        before = handle.read()
    text = before
    original_lines = before.splitlines()
    fixed = []
    open_items = []
    ordered = sorted(errors, key=lambda item: (-item["line"], _ORDER.get(item["code"], 50)))
    seen = set()
    for error in ordered:
        key = (error["code"], error["line"], error["detail"])
        if key in seen:
            continue
        seen.add(key)
        source = ""
        if 1 <= error["line"] <= len(original_lines):
            source = original_lines[error["line"] - 1]
        handler = _HANDLERS.get(error["code"])
        if handler is None:
            open_items.append(_open(error))
            continue
        if source and source not in text and error["code"] not in ("C2668", "C2065", "C2100"):
            continue
        updated, note, handled = handler(text, error, source, original_lines)
        if handled:
            text = updated
            if note:
                fixed.append(note)
        else:
            open_items.append(note or _open(error))
    changed = text != before
    if changed:
        if not text.endswith("\n"):
            text += "\n"
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    report = _report(fixed, open_items, log)
    return {"changed": changed, "report": report, "fixed": fixed, "open": open_items}


def classify_report(log):
    errors = parse_errors(log)
    if not errors:
        return log
    lines = ["No automatic fix:"]
    for error in errors:
        lines.append("- " + _open(error))
    return "\n".join(lines) + "\n\n" + log


def _open(error):
    return "%s line %s: %s" % (error["code"], error["line"], error["detail"])


def _report(fixed, open_items, log):
    parts = []
    if fixed:
        parts.append("Fixed:\n" + "\n".join("- " + item for item in fixed))
    if open_items:
        parts.append("No automatic fix:\n" + "\n".join("- " + item for item in open_items))
    parts.append(log)
    return "\n\n".join(parts)


def _quoted_name(detail):
    match = _NAME_RE.search(detail or "")
    return match.group(1) if match else ""


def _types(detail):
    found = re.findall(r"from '([^']*)' to '([^']*)'", detail or "")
    if not found:
        return "", ""
    return found[0]


def _fix_linkage(text, error, source, _lines):
    if 'extern "C" return' not in source:
        return text, _open(error), False
    return (
        text.replace(source, source.replace('extern "C" return', "return"), 1),
        "C2598 line %s: took extern \"C\" off a return" % error["line"],
        True,
    )


def _fix_keyword_declaration(text, error, source, _lines):
    if not re.match(r'^\s*extern\b.*\bgoto\s*\(', source or ""):
        return text, _open(error), False
    lines = [line for line in text.splitlines() if line != source]
    updated = "\n".join(lines)
    if text.endswith("\n"):
        updated += "\n"
    return updated, "C2143 line %s: removed the declaration of goto" % error["line"], True


def _fix_return_syntax(text, error, source, _lines):
    computed = re.match(r"^(\s*)goto\s+\(.*\)\s*([A-Za-z_]\w*)\s*;\s*$", source or "")
    if computed and "syntax error : '('" in (error.get("detail") or ""):
        replacement = "%s%s();\n%sreturn 0;" % (computed.group(1), computed.group(2), computed.group(1))
        return (
            text.replace(source, replacement, 1),
            "C2059 line %s: a goto cannot take an address, so this calls %s" % (error["line"], computed.group(2)),
            True,
        )
    if error["detail"] != "syntax error : 'return'" and "syntax error : 'return'" not in error["detail"]:
        return text, _open(error), False
    if 'extern "C" return' not in source:
        return text, _open(error), False
    return (
        text.replace(source, source.replace('extern "C" return', "return"), 1),
        "C2059 line %s: took extern \"C\" off a return" % error["line"],
        True,
    )


def _fix_ambiguous(text, error, _source, _lines):
    name = _quoted_name(error["detail"])
    if not name:
        return text, _open(error), False
    extra = 'extern "C" int __stdcall %s(...);' % name
    others = [
        line for line in text.splitlines()
        if name in line and line.strip() != extra and re.search(r"\b%s\s*\(" % re.escape(name), line)
    ]
    if extra not in text or not others:
        return text, "C2668 %s: no automatic fix" % name, False
    lines = [line for line in text.splitlines() if line.strip() != extra]
    updated = "\n".join(lines)
    if text.endswith("\n"):
        updated += "\n"
    return updated, "C2668 %s: removed the extra __stdcall prototype" % name, True


def _fix_glued(text, error, source, _lines):
    match = _GLUED_RE.match(source.strip())
    if not match or source not in text:
        return text, _open(error), False
    statement, signature = match.group(1).strip(), match.group(2).strip()
    if statement.startswith("ExceptionList = &"):
        statement = "ExceptionList = (void *)" + statement.split("=", 1)[1].strip()
    indent = re.match(r"\s*", source).group(0)
    replacement = '%sextern "C" void %s' % (indent, signature)
    updated = text.replace(source, replacement, 1)
    needle = replacement
    at = updated.find(needle)
    if at < 0:
        return text, _open(error), False
    rest = updated[at + len(needle):]
    brace = re.search(r"\n([ \t]*)\{", rest)
    if not brace:
        return text, _open(error), False
    insert_at = at + len(needle) + brace.end()
    updated = updated[:insert_at] + "\n" + brace.group(1) + "  " + statement + ";" + updated[insert_at:]
    return updated, "C2040 line %s: split the statement off the function signature" % error["line"], True


def _fix_indirection(text, error, source, _lines):
    names = re.findall(r"\*((?:DAT|PTR)_[0-9A-Fa-f]+)\b", source)
    if not names:
        return text, _open(error), False
    updated = text
    changed = []
    for name in names:
        old = "extern int %s;" % name
        new = "extern int *%s;" % name
        if old not in updated:
            if ("extern int *%s;" % name) in updated:
                changed.append("")
            continue
        updated = updated.replace(old, new)
        changed.append(name)
    if not changed:
        return text, _open(error), False
    names = [name for name in changed if name]
    if not names:
        return updated, "", True
    return updated, "C2100 line %s: declared %s as a pointer" % (error["line"], ", ".join(names)), True


def _fix_undeclared(text, error, source, lines):
    name = _quoted_name(error["detail"])
    if not name:
        return text, _open(error), False
    if name in _WIN_PROTOTYPES:
        return _declare_win(text, name, error)
    if not re.search(r"&%s\b" % re.escape(name), text):
        return text, "C2065 %s: no automatic fix" % name, False
    function = _enclosing_function(lines, error["line"])
    if not function:
        return text, "C2065 %s: no automatic fix" % name, False
    return _declare_local(text, function, name, error)


def _declare_win(text, name, error):
    lines = []
    dropped = False
    extra = 'extern "C" int __stdcall %s(...);' % name
    for line in text.splitlines():
        if line.strip() == extra:
            dropped = True
            continue
        lines.append(line)
    updated = "\n".join(lines)
    if text.endswith("\n"):
        updated += "\n"
    prototype = _WIN_PROTOTYPES[name]
    if prototype not in updated:
        at = updated.find("// FN ")
        block = prototype + "\n"
        updated = block + updated if at < 0 else updated[:at] + block + updated[at:]
    note = "C2065 %s: added the Win32 prototype" % name
    if dropped:
        note += " and removed the int prototype"
    return updated, note, True


def _enclosing_function(lines, line_no):
    index = min(line_no, len(lines)) - 1
    while index >= 0:
        match = re.search(r"\b(_fn_[0-9A-Fa-f]+)\s*\(", lines[index])
        if match:
            return match.group(1)
        index -= 1
    return ""


def _declare_local(text, function, name, error):
    match = re.search(r"extern \"C\"[^\n]*\b%s\s*\([^)]*\)" % function, text)
    if not match:
        return text, "C2065 %s: no automatic fix" % name, False
    rest = text[match.end():]
    brace = re.search(r"\{", rest)
    if not brace:
        return text, "C2065 %s: no automatic fix" % name, False
    body_start = match.end() + brace.end()
    end = text.find("// END ", body_start)
    body = text[body_start:end if end >= 0 else body_start + 400]
    if re.search(r"(?m)^\s*\w[\w\s\*]*\s+%s\s*;" % re.escape(name), body):
        return text, "C2065 %s: no automatic fix" % name, False
    decl = "\n  void *%s;" % name
    updated = text[:body_start] + decl + text[body_start:]
    return updated, "C2065 line %s: declared void *%s in %s" % (error["line"], name, function), True


def _fix_convert(text, error, source, _lines):
    if source not in text:
        return text, _open(error), False
    src_type, dst_type = _types(error["detail"])
    if not dst_type:
        return text, _open(error), False
    call = _CALL_CAST_RE.match(source)
    if call and _is_integer(src_type) and _is_pointer(dst_type):
        replacement = "%s%s = (%s)(unsigned int)(%s(%s));" % (
            call.group("indent"),
            call.group("lhs"),
            dst_type,
            call.group("fn"),
            call.group("args"),
        )
        return text.replace(source, replacement, 1), "C2440 line %s: cast the call to %s" % (error["line"], dst_type), True
    assign = _ASSIGN_RE.match(source)
    if not assign:
        replaced = _resource_comma(source)
        if not replaced or replaced == source:
            return text, _open(error), False
        return text.replace(source, replaced, 1), "C2440 line %s: cast both sides of the resource load" % error["line"], True
    rhs = assign.group("rhs").strip()
    if _has_top_comma(rhs):
        replaced = _resource_comma(source)
        if replaced is None or replaced == source or replaced not in text and source not in text:
            return text, _open(error), False
        if replaced == source:
            return text, _open(error), False
        return text.replace(source, replaced, 1), "C2440 line %s: cast both sides of the resource load" % error["line"], True
    peeled = "(%s)" % src_type
    if src_type and rhs.startswith(peeled):
        rhs = rhs[len(peeled):].strip()
    elif rhs.startswith("(") and _is_integer(dst_type) and _is_pointer(src_type):
        rhs = re.sub(r"^\([^)]*\)\s*", "", rhs, count=1)
    elif rhs.startswith("("):
        return text, _open(error), False
    if rhs.startswith("&") and dst_type in ("int", "unsigned int"):
        cast = "(unsigned int)"
    elif _is_pointer(dst_type) or _is_integer(dst_type):
        cast = "(%s)" % dst_type
    else:
        return text, _open(error), False
    replacement = "%s%s = %s%s;" % (assign.group("indent"), assign.group("lhs"), cast, rhs)
    return text.replace(source, replacement, 1), "C2440 line %s: cast the assignment to %s" % (error["line"], dst_type), True


def _has_top_comma(text):
    depth = 0
    for char in text:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            return True
    return False


def _resource_comma(source):
    match = _RESOURCE_COMMA_RE.search(source)
    if not match:
        return None
    original = match.group(0)
    if "(void *)" in original:
        return None
    replacement = "%s = (void *)%s(%s), %s = (unsigned int)%s" % (
        match.group("ptr"),
        match.group("fn"),
        match.group("args"),
        match.group("num"),
        match.group("ptr"),
    )
    return source.replace(original, replacement, 1)


def _fix_second_linkage(text, error, source, lines):
    name = _quoted_name(error["detail"])
    if not name or "second C linkage" not in (error.get("detail") or ""):
        return text, _open(error), False
    if not source or name not in source or 'extern "C"' not in source:
        return text, _open(error), False
    ellipsis = 'extern "C" int %s(...);' % name
    earlier = [line for line in lines[:error["line"] - 1] if line.strip() == ellipsis]
    before = "\n".join(lines[:error["line"] - 1]).replace(ellipsis, "")
    if earlier and source.strip() != ellipsis and not re.search(r"\b%s\s*\(" % re.escape(name), before):
        updated = text.replace(ellipsis + "\n", "", 1)
        if updated == text:
            updated = text.replace(ellipsis, "", 1)
        return updated, "C2733 %s: removed the extra (...) declaration" % name, True
    updated_lines = [line for line in text.splitlines() if line != source]
    updated = "\n".join(updated_lines)
    if text.endswith("\n"):
        updated += "\n"
    if updated == text:
        return text, _open(error), False
    return updated, "C2733 %s: removed the second declaration" % name, True


def _fix_pointer_bitwise(text, error, source, _lines):
    detail = error.get("detail") or ""
    if "left operand has type" not in detail or "*" not in detail:
        return text, _open(error), False
    updated = re.sub(r"\(([A-Za-z_]\w*)\)\s*(>>|<<)", r"((unsigned int)\1) \2", source)
    updated = re.sub(r"\(([A-Za-z_]\w*)\s*&", r"((unsigned int)\1 &", updated)
    assign = re.match(r"^(\s*)([A-Za-z_]\w*)\s*=\s*(.+);\s*$", updated)
    if assign and not assign.group(3).lstrip().startswith("(unsigned char *)"):
        updated = "%s%s = (unsigned char *)(%s);" % (assign.group(1), assign.group(2), assign.group(3))
    if updated == source:
        return text, _open(error), False
    return text.replace(source, updated, 1), "C2296 line %s: cast the pointer before the bitwise operator" % error["line"], True


def _fix_redefinition(text, error, _source, _lines):
    name = _quoted_name(error["detail"])
    if not name:
        return text, _open(error), False
    import draft_c
    updated = draft_c._collapse_typedefs(text)
    if updated == text:
        return text, "C2371 %s: no automatic fix" % name, False
    return updated, "C2371 %s: kept one typedef" % name, True


def _is_pointer(name):
    return "*" in (name or "")


def _is_integer(name):
    return name in ("int", "unsigned int", "long", "unsigned long", "short", "unsigned short", "char", "unsigned char")


_HANDLERS = {
    "C2598": _fix_linkage,
    "C2059": _fix_return_syntax,
    "C2143": _fix_keyword_declaration,
    "C2668": _fix_ambiguous,
    "C2040": _fix_glued,
    "C2100": _fix_indirection,
    "C2065": _fix_undeclared,
    "C2440": _fix_convert,
    "C2371": _fix_redefinition,
    "C2733": _fix_second_linkage,
    "C2296": _fix_pointer_bitwise,
}
