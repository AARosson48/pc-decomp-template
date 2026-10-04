#!/usr/bin/env python3
"""Ask a local Ollama model for a new C draft.

The model sees the four workbench panes and nothing is sent off this PC.
qwen3.6 is preferred when it is installed. Set ollama_model in the project's
tools.json to use a different local model.
"""

import json
import os
import re
import subprocess
import threading
import urllib.error
import urllib.request

HOST = "http://127.0.0.1:11434"
PREFER = ("qwen3.6", "gemma4", "my-gemma", "qwen3", "my-dolphin", "dolphin-llama3")
_LOCK = threading.Lock()
_BUSY = threading.Lock()
_LIMIT = 8000


def ask(project_root, addr, pseudo, cpp, assembly, compiled, score=None):
    ready, note = _ensure()
    if not ready:
        return {"code": "", "note": note}
    model = _model(project_root)
    if not model:
        return {"code": "", "note": "Ollama is running and has no models installed."}
    if not _BUSY.acquire(False):
        return {"code": "", "note": "The local model is still answering the previous click."}
    try:
        return _ask(project_root, model, addr, pseudo, cpp, assembly, compiled, score)
    finally:
        _BUSY.release()


def _ask(project_root, model, addr, pseudo, cpp, assembly, compiled, score):
    name = "_fn_%08X" % addr
    changes = _diff(assembly, compiled)
    if _score_number(score) < 100 and (cpp or "").strip():
        remember(project_root, addr, cpp, score, _failure_why(score, compiled))
    failed = _attempts(project_root, name)
    try:
        raw = _complete(model, _prompt(project_root, name, pseudo, cpp, assembly, compiled, changes, score, failed), 0.2)
        split_end = _split_end(raw, addr)
        if _lecture(raw):
            result = {"code": "", "model": model, "diff": changes, "note": "The model explained the listing instead of returning C. The editor was left unchanged."}
            if split_end:
                result["split_end"] = split_end
            return result
        code = _code(raw)
    except Exception as exc:
        return {"code": "", "note": "The local model did not answer.\n(%s)" % exc, "model": model, "diff": changes}
    result = {"code": "", "model": model, "diff": changes}
    if split_end:
        result["split_end"] = split_end
    if code is None:
        result["note"] = "The model returned assembly instead of C. The editor was left unchanged."
        return result
    if not code:
        result["note"] = "The model did not return C."
        return result
    chosen = _repeat_choice(failed, code, cpp, score)
    if chosen is not None and _attempt_score(chosen) > _score_number(score):
        restored = (chosen.get("code") or "").strip()
        if name not in restored:
            restored = "// The model draft should define %s.\n%s" % (name, restored)
        result["code"] = restored if restored.endswith("\n") else restored + "\n"
        result["note"] = "Restored the draft that scored %s. The editor had scored %s." % (
            _shown_score(chosen.get("score")),
            _shown_score(score),
        )
        return result
    if chosen is not None:
        result["note"] = "The model repeated a lower-scoring attempt. The editor was left unchanged."
        return result
    if name not in code:
        code = "// The model draft should define %s.\n%s" % (name, code)
    result["code"] = code if code.endswith("\n") else code + "\n"
    return result


_COMPILER_ERROR_RE = re.compile(r"(?:fatal error|(?:^|\s)error)\s+C\d+|^FAILED:|ninja: build stopped", re.I)


def _compiler_errors(compiled):
    found = []
    for line in (compiled or "").splitlines():
        text = line.strip()
        if text and _COMPILER_ERROR_RE.search(text) and text not in found:
            found.append(text)
    return found


def _prompt(project_root, name, pseudo, cpp, assembly, compiled, changes, score, failed):
    project, exe = _project_facts(project_root)
    score_line = _score_line(score)
    errors = _compiler_errors(compiled)
    if errors:
        problem = "The C/C++ code does not compile. Fix the compiler errors in this prompt before chasing an assembly match."
    elif changes.startswith("The instruction lists already"):
        problem = "The C/C++ code already matches the source assembly."
    else:
        problem = "The C/C++ code does not compile to match the source assembly."
    lines = [
        "This is a Windows decompilation of the project %s." % project,
        "The original program is the 32-bit executable %s." % exe,
        "It was built with Microsoft Visual C++ 6.0, compiler cl 12.00, optimization /O2.",
        "The goal is matching C or C++. Compile this one function with that compiler and get the same instructions as the source assembly.",
        "A match means the compiled assembly and the source assembly are the same instructions. Close is not a match.",
        score_line,
        problem,
        "Change the C so the compiled assembly becomes the source assembly.",
        "Do not copy the pseudo C. The current C was copied from the pseudo C, and that is why the score is not 100.",
        "Do not return the current C unchanged.",
        "Do not return a lower-scoring attempt. A draft that scored higher than the current C is not a failure. You may return that draft.",
        "Your reply is compiled as C. Do not write push, pop, mov, lea, test, cmp, call, jmp, je, jne, or ret.",
        "Do not explain. No headings, no plan, and no paragraph before the code.",
        "Do not write about optimization, nop padding, volatile, or inline assembly.",
        "Do not use volatile, __asm, or a cast whose purpose is to stop the compiler from deleting code.",
        "",
    ]
    if errors:
        lines.append("COMPILER ERRORS")
        lines.append("The last build failed. These are the compiler errors. The new C must not produce them.")
        lines.extend(errors[:40])
        lines.append("")
    else:
        lines.append("Fix these assembly differences:")
    compiled_rows = _insns(compiled)
    if not errors and compiled_rows and all(row == "nop" or row.startswith("nop ") for row in compiled_rows):
        lines.append("The compiled listing is only nop. That is padding, not this function. Ignore it and follow the source assembly.")
    padding = _padding_split(assembly)
    if padding:
        lines.append("Padding nops start at %s. Those nops are not part of this function." % padding)
        lines.append("Before the C, write this line so Ghidra splits the function there: SPLIT %s" % padding)
    if not errors:
        actions = _actions(assembly, compiled)
        if actions:
            lines.extend(actions)
        elif changes and not changes.startswith("The instruction"):
            lines.append(changes)
        else:
            lines.append("No instruction difference was found.")
    lines.extend(_best_lines(failed, score))
    lines.extend(_failed_lines(failed, score))
    lines.extend([
        "",
        "SOURCE ASSEMBLY",
        "This is the target. The new C must compile to these instructions.",
        _whole(assembly),
        "",
        "COMPILER OUTPUT" if errors else "COMPILED ASSEMBLY",
        "This is the build log. Fix the compiler errors above." if errors else "This is what the current C compiled to. It is wrong where it differs from the source assembly.",
        _clip(compiled),
        "",
        "C/C++ CODE",
        "This is the current editor. Replace it when a listed draft scored higher, or when different C will score higher.",
        _clip(cpp),
        "",
        "PSEUDO C CODE",
        "Control-flow hint only. Do not copy it. Do not return it.",
        _clip(pseudo),
        "",
        "Rules for the new C/C++:",
        "- One function, extern \"C\" %s. Do not rename it." % name,
        "- Put extern declarations for globals and called functions above that function.",
        "- MSVC 6 has no __thiscall keyword. Do not write __thiscall.",
        "- If the source assembly loads ecx and then calls, that callee is __fastcall.",
        "- If the source assembly pushes an argument, calls, and the next instruction is not add esp, that callee is __stdcall.",
        "- If the source assembly pushes, calls, then add esp, that callee is __cdecl.",
        "- If the source assembly does mov eax, <argument> after a call, return that argument. Do not return the callee's result.",
        "- If the source assembly stores 0 into a stack slot, keep a local int set to 0.",
        "- No stdint.h, auto, or nullptr.",
        "- Reply with one cpp code block and no other text.",
        "- The function body must not contain assembly mnemonics.",
    ])
    if _assembly_count(cpp) >= 2:
        lines.append("The C/C++ code is invalid because it contains assembly. Replace those lines with C.")
    seen = []
    for ident in re.findall(r"error C2065: '([^']+)'", compiled or ""):
        if ident not in seen and ident != name:
            seen.append(ident)
    if seen:
        lines.append("Declare these above the function:")
        for ident in seen:
            lines.append(_declare(ident, assembly))
    return "\n".join(lines)


def clear_attempts(project_root, addr=None):
    """Drop saved attempts. A score from the old diff is not a failed draft."""
    path = os.path.join(project_root, "build", "ai_log.json")
    if addr is None:
        removed = 0
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle) or {}
                removed = sum(len(rows) for rows in data.values() if isinstance(rows, list))
            except (OSError, ValueError):
                removed = 0
            os.remove(path)
        return {"cleared": removed, "functions": "all"}
    name = "_fn_%08X" % int(addr)
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle) or {}
        except (OSError, ValueError):
            data = {}
    rows = data.pop(name, []) or []
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    return {"cleared": len(rows), "name": name}


def remember(project_root, addr, code, score=None, why=""):
    text = (code or "").strip()
    if not text or _score_number(score) >= 100:
        return
    name = "_fn_%08X" % int(addr)
    path = os.path.join(project_root, "build", "ai_log.json")
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle) or {}
        except (OSError, ValueError):
            data = {}
    rows = data.get(name) or []
    number = None if score is None or score == "" else _score_number(score)
    reason = (why or "This C did not match the source assembly.")[:240]
    for row in rows:
        if _same(row.get("code") or "", text):
            if number is not None and number < 100 and number >= _attempt_score(row):
                row["score"] = number
                if reason:
                    row["why"] = reason
            break
    else:
        rows.append({
            "code": text[:4000],
            "score": number if number is not None and number < 100 else None,
            "why": reason,
        })
    data[name] = rows[-8:]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def _attempts(project_root, name):
    path = os.path.join(project_root, "build", "ai_log.json")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle) or {}
    except (OSError, ValueError):
        return []
    return data.get(name) or []


def _failure_why(score, compiled):
    for line in (compiled or "").splitlines():
        if "error C" in line:
            return line.strip()[:240]
    number = _score_number(score)
    if number >= 100:
        return "Score was 100."
    if score is None or score == "":
        return "This C did not match the source assembly."
    return "Score was %s." % (int(number) if number == int(number) else number)


def _score_number(score):
    if score is None or score == "":
        return -1
    try:
        return float(score)
    except (TypeError, ValueError):
        return -1


def _shown_score(score):
    if score is None or score == "":
        return "unscored"
    number = _score_number(score)
    if number < 0:
        return "unscored"
    if number == int(number):
        return str(int(number))
    text = "%s" % number
    return text.rstrip("0").rstrip(".")


def _attempt_score(item):
    if not isinstance(item, dict):
        return -1.0
    score = item.get("score")
    if score is None or score == "":
        return -1.0
    return _score_number(score)


def _best_attempt(failed):
    best = None
    for item in failed or []:
        if best is None or _attempt_score(item) > _attempt_score(best):
            best = item
    if best is None or _attempt_score(best) < 0:
        return None
    return best


def _repeat_choice(failed, code, cpp, score):
    matched = None
    for item in failed or []:
        if _same(code, item.get("code") or "") and (matched is None or _attempt_score(item) > _attempt_score(matched)):
            matched = item
    if matched is None:
        return None
    if _attempt_score(matched) > _score_number(score):
        return matched
    best = _best_attempt(failed)
    if best is not None and _attempt_score(best) > _score_number(score) and not _same(best.get("code") or "", cpp or ""):
        return best
    return matched


def _best_lines(failed, score):
    best = _best_attempt(failed)
    if best is None or _attempt_score(best) <= _score_number(score):
        return []
    code = (best.get("code") or "").strip().splitlines()
    lines = [
        "",
        "BEST DRAFT SO FAR",
        "This C scored %s, which is higher than the current score. Return this C, or write different C that will score higher." % _shown_score(best.get("score")),
        "\n".join(code[:40]),
    ]
    if len(code) > 40:
        lines.append("...")
    return lines


def _failed_lines(failed, score):
    current = _score_number(score)
    worse = [item for item in (failed or []) if _attempt_score(item) <= current]
    if not worse:
        return []
    lines = ["", "LOWER SCORING ATTEMPTS", "Do not return any of these. Each one scored the same or worse than the current C."]
    for index, item in enumerate(worse[-5:], 1):
        lines.append("Attempt %s. Score %s. %s" % (index, _shown_score(item.get("score")), item.get("why") or ""))
        code = (item.get("code") or "").strip().splitlines()
        lines.append("\n".join(code[:40]))
        if len(code) > 40:
            lines.append("...")
    return lines


def _actions(assembly, compiled):
    left = _insns(assembly)
    right = _insns(compiled)
    if not left or not right:
        return []
    orders = []
    if any(row.startswith("add esp") for row in right) and not any(row.startswith("add esp") for row in left):
        orders.append("The compiled assembly cleans the stack with add esp. The source assembly does not. The callee is __stdcall. Pass the argument in the call.")
    for index, row in enumerate(left):
        if row.startswith("call ") and index and left[index - 1].startswith("mov ecx"):
            orders.append("The source assembly loads ecx and then calls. That callee is __fastcall. The compiled assembly must load ecx before that call.")
            break
    for index, row in enumerate(left):
        if row.startswith("mov eax") and index and left[index - 1].startswith("call "):
            orders.append("After a call, the source assembly overwrites eax. Return the argument that was passed in. Do not return the callee's result.")
            break
    if any(re.search(r"\[esp.*\], 0\b", row) for row in left):
        orders.append("The source assembly stores 0 in a stack slot. Keep a local int set to 0.")
    for index in range(max(len(left), len(right))):
        source = left[index] if index < len(left) else "(missing)"
        built = right[index] if index < len(right) else "(missing)"
        if source != built:
            orders.append("Source instruction: %s" % source)
            orders.append("Compiled instruction: %s" % built)
            orders.append("Change the C so the compiled instruction becomes the source instruction.")
            if len(orders) >= 24:
                break
    return orders


def _split_end(text, addr):
    match = re.search(r"(?i)\bSPLIT\s+(?:0x)?([0-9A-Fa-f]{5,8})\b", text or "")
    if not match:
        return None
    end = int(match.group(1), 16)
    if end <= addr:
        return None
    return end


def _padding_split(assembly):
    rows = []
    for line in (assembly or "").splitlines():
        match = re.match(r"^([0-9A-Fa-f]{8})\s+(\S+)", line.strip())
        if match:
            rows.append((match.group(1).upper(), match.group(2).lower()))
    if len(rows) < 2 or rows[0][1] == "nop":
        return None
    first_nop = None
    for address, mnemonic in rows:
        if mnemonic == "nop":
            if first_nop is None:
                first_nop = address
        elif first_nop is not None:
            return None
    return first_nop


def _score_line(score):
    if score is None or score == "":
        return "The current score is unknown because this function has not been scored. A finished function scores 100."
    try:
        number = float(score)
    except (TypeError, ValueError):
        return "The current score is unknown because this function has not been scored. A finished function scores 100."
    shown = int(number) if number == int(number) else number
    if number >= 100:
        return "The current score is 100. This function is complete."
    return "The current score is %s. 100 means complete. %s is not complete." % (shown, shown)


_ASM_LINE = re.compile(
    r"^(?:push|pop|movzx|movsx|mov|lea|test|cmp|call|jmp|je|jne|jz|jnz|ja|jb|jg|jl|"
    r"ret|add|sub|xor|or|and|nop|inc|dec|shl|shr|imul|neg|not)\b",
    re.I,
)


def _assembly_count(text):
    return sum(1 for line in (text or "").splitlines() if _ASM_LINE.match(line.strip()))


def _c_statements(text):
    count = 0
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "/*", "*", "#")):
            continue
        if _ASM_LINE.match(stripped):
            continue
        if stripped in ("{", "}"):
            continue
        if re.search(r"(;|\{|\bif\b|\breturn\b|\bfor\b|\bwhile\b)", stripped):
            count += 1
    return count


def _project_facts(project_root):
    project = os.path.basename(project_root)
    exe = "the game executable"
    yml = os.path.join(project_root, "config", "dtk.yml")
    if os.path.isfile(yml):
        with open(yml, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("name:"):
                    project = line.split(":", 1)[1].strip() or project
                elif line.startswith("object:"):
                    exe = line.split(":", 1)[1].strip() or exe
    config = os.path.join(project_root, "config", "project.json")
    if os.path.isfile(config):
        try:
            with open(config, "r", encoding="utf-8") as handle:
                names = json.load(handle).get("exe_names") or []
            if names:
                exe = names[0]
        except (OSError, ValueError):
            pass
    return project, exe


def _declare(ident, assembly):
    if not ident.startswith("_fn_"):
        return "extern int %s;" % ident
    return 'extern "C" int %s %s(int);' % (_convention(ident, assembly), ident)


def _convention(ident, assembly):
    try:
        target = "%x" % int(ident.split("_fn_")[-1], 16)
    except ValueError:
        return "__cdecl"
    rows = _insns(assembly)
    for index, row in enumerate(rows):
        if not row.startswith("call "):
            continue
        called = re.sub(r"[^0-9a-f]", "", row.split(" ", 1)[-1]).lstrip("0")
        if called != target and ident.lower() not in row:
            continue
        prev = rows[index - 1] if index else ""
        nxt = rows[index + 1] if index + 1 < len(rows) else ""
        if prev.startswith("mov ecx"):
            return "__fastcall"
        if nxt.startswith("add esp"):
            return "__cdecl"
        if prev.startswith("push"):
            return "__stdcall"
    return "__cdecl"


def _complete(model, prompt, temperature):
    payload = {
        "model": model,
        "stream": False,
        "think": False,
        "messages": [{"role": "user", "content": prompt}],
        "options": {"temperature": temperature, "num_predict": 700},
    }
    reply = _post("/api/chat", payload, 180)
    return ((reply.get("message") or {}).get("content") or "").strip()


def _diff(assembly, compiled):
    left = _insns(assembly)
    right = _insns(compiled)
    if not left:
        return "Source assembly had no instructions."
    if "error C" in (compiled or ""):
        return "The compiled pane is compiler errors, so there is no listing to diff yet."
    if not right:
        return "Compiled assembly has no instructions. Score the draft first so this comparison exists."
    rows = []
    for index in range(max(len(left), len(right))):
        source = left[index] if index < len(left) else ""
        built = right[index] if index < len(right) else ""
        if source != built:
            rows.append("source:   %s\ncompiled: %s" % (source or "(none)", built or "(none)"))
    if not rows:
        return "The instruction lists already match."
    return "\n".join(rows[:30])


def _insns(text):
    rows = []
    names = (
        "push|pop|movzx|movsx|mov|lea|test|cmp|call|jmp|je|jne|jz|jnz|ja|jb|jg|jl|"
        "ret|add|sub|xor|or|and|nop|inc|dec|shl|shr|imul|neg|not"
    )
    for line in (text or "").splitlines():
        line = re.sub(r"\s+", " ", line.strip().lower())
        line = re.sub(r"^[0-9a-f]{6,8}:?\s+", "", line)
        line = re.sub(r"^(?:[0-9a-f]{2}\s+)+", "", line)
        line = re.sub(r"\bdat_([0-9a-f]+)\b", r"0x\1", line)
        match = re.match(r"(?:%s)\b.*" % names, line)
        if match and not line.startswith("summary"):
            rows.append(re.sub(r"\s+", " ", match.group(0)).strip())
    return rows


def _same(left, right):
    def norm(text):
        text = re.sub(r"(?m)^\s*//.*$", "", text or "")
        return re.sub(r"\s+", "", text)

    return norm(left) == norm(right) and norm(left) != ""


def _ensure():
    if _up():
        return True, ""
    exe = os.path.join(os.environ.get("LOCALAPPDATA") or "", "Programs", "Ollama", "ollama.exe")
    if not os.path.isfile(exe):
        return False, "Ollama is not installed. The Ask AI button uses a model on this PC."
    with _LOCK:
        if _up():
            return True, ""
        subprocess.Popen([exe, "serve"], creationflags=0x08000000)
    for _ in range(20):
        if _up():
            return True, ""
        threading.Event().wait(0.5)
    return False, "Ollama is installed but its server did not start."


def _up():
    try:
        _post("/api/tags", None, 2)
        return True
    except Exception:
        return False


def _model(project_root):
    chosen = os.environ.get("OLLAMA_MODEL") or ""
    path = os.path.join(project_root, "tools.json")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                chosen = json.load(handle).get("ollama_model") or chosen
        except (OSError, ValueError):
            pass
    try:
        listed = _post("/api/tags", None, 5)
    except Exception:
        return chosen
    names = [item.get("name") or "" for item in listed.get("models") or []]
    if chosen and any(name == chosen or name.startswith(chosen + ":") for name in names):
        return chosen if chosen in names else next(name for name in names if name.startswith(chosen + ":"))
    for prefer in PREFER:
        for name in names:
            if name == prefer or name.startswith(prefer + ":"):
                return name
    return names[0] if names else ""


def _post(path, payload, timeout):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(HOST + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as handle:
        return json.loads(handle.read().decode("utf-8"))


def _clip(text):
    text = (text or "").strip()
    if len(text) <= _LIMIT:
        return text
    return text[:_LIMIT] + "\n..."


def _whole(text):
    """The target listing is the function. Cutting it hides the instructions that have to match."""
    text = (text or "").strip()
    if len(text) <= 200000:
        return text
    return text[:200000] + "\n... The rest of this function was cut."


_CODE_START = re.compile(
    r"^(?://|/\*|#|extern\b|typedef\b|struct\b|union\b|enum\b|static\b|const\b|"
    r"unsigned\b|signed\b|int\b|void\b|char\b|short\b|long\b|float\b|double\b|_fn_)"
)


def _lecture(text):
    sample = (text or "").lower()
    marks = ("optimized away", "inline assembly", "volatile", "nop padding", "the problem", "hard-coded")
    return sum(1 for mark in marks if mark in sample) >= 2


def _code(text):
    text = re.sub(r"(?s)<think>.*?</think>", "", text)
    text = re.sub(r"(?s)^.*?</think>", "", text).strip()
    fenced = re.findall(r"```(?:c|cpp|c\+\+)?\s*([\s\S]*?)```", text, flags=re.I)
    body = fenced[-1].strip() if fenced else text
    lines = [line for line in body.splitlines() if not re.match(r"(?i)^\s*SPLIT\s+", line)]
    start = 0
    while start < len(lines) and not _CODE_START.match(lines[start].strip()):
        start += 1
    body = "\n".join(lines[start:]).strip()
    body = body.replace("__thiscall", "__fastcall")
    if _assembly_count(body) >= 2:
        kept = [line for line in body.splitlines() if not _ASM_LINE.match(line.strip())]
        body = "\n".join(kept).strip()
        if _c_statements(body) == 0:
            return None
    if "_fn_" in body or "extern" in body:
        return body
    return ""
