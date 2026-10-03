#!/usr/bin/env python3
"""Read and update one decomp project: banks, functions, source, and scores."""

import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time

import banks
import names

FN_START = "// FN %08X\n"
FN_END = "// END %08X\n"


def _inside(path, root):
    if not path or not root:
        return False
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) == os.path.abspath(root)
    except ValueError:
        return False


def _read(path):
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _align_cdecl_symbols(path):
    """The retail object uses _fn_XXXXXXXX. cdecl emits __fn_XXXXXXXX and fastcall emits @_fn_XXXXXXXX@N."""
    if not path or not os.path.isfile(path):
        return 0
    with open(path, "rb") as handle:
        data = bytearray(handle.read())
    if len(data) < 20 or data[:2] != b"\x4c\x01":
        return 0
    symptr, nsymbols = struct.unpack_from("<II", data, 8)
    strings = symptr + nsymbols * 18
    if symptr <= 0 or nsymbols <= 0 or strings + 4 > len(data):
        return 0
    strsize = struct.unpack_from("<I", data, strings)[0]
    if strsize < 4 or strings + strsize > len(data):
        return 0
    blob = bytes(data[strings:strings + strsize])
    new_blob = bytearray(b"\0\0\0\0")
    offset_map = {}
    renamed = 0
    pos = 4
    while pos < len(blob):
        end = blob.find(b"\0", pos)
        if end < 0:
            break
        raw = blob[pos:end]
        match = re.fullmatch(br"(?:__fn_|@_fn_)([0-9A-Fa-f]{8})(?:@\d+)?", raw)
        if match and raw != b"_fn_" + match.group(1).upper():
            raw = b"_fn_" + match.group(1).upper()
            renamed += 1
        offset_map[pos] = len(new_blob)
        new_blob += raw + b"\0"
        pos = end + 1
    if renamed == 0:
        return 0
    index = 0
    while index < nsymbols:
        slot = symptr + index * 18
        aux = data[slot + 17]
        if data[slot:slot + 4] == b"\0\0\0\0":
            old = struct.unpack_from("<I", data, slot + 4)[0]
            new = offset_map.get(old)
            if new is not None:
                struct.pack_into("<I", data, slot + 4, new)
        index += 1 + aux
    struct.pack_into("<I", new_blob, 0, len(new_blob))
    with open(path, "wb") as handle:
        handle.write(data[:strings] + new_blob + data[strings + strsize:])
    return renamed


def _ninja_exe():
    found = shutil.which("ninja")
    if found:
        return found
    scripts = os.path.join(os.path.dirname(sys.executable), "Scripts", "ninja.exe")
    if os.path.isfile(scripts):
        return scripts
    return ""


def _one_unit_log(log):
    """Keep the first failing file. Later ninja steps are other banks."""
    lines = (log or "").splitlines()
    kept = []
    failed = False
    for line in lines:
        if failed and re.match(r"\s*\[\d+/\d+\]", line):
            break
        if "error C" in line:
            failed = True
        kept.append(line)
    text = "\n".join(kept).strip()
    return text or (log or "")


class Project:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        self._blob = None
        self._text_va = None
        self._starts = None
        self._score_cache = None
        self._pass_lock = threading.Lock()
        self._pass = {"running": False}
        self._job_lock = threading.Lock()
        self._job = {"running": False}

    def exe_path(self):
        local = os.path.join(self.root, "project.local.json")
        if os.path.isfile(local):
            with open(local, "r", encoding="utf-8") as handle:
                saved = json.load(handle).get("exe")
            if saved and os.path.isfile(saved):
                return saved
            if saved:
                name = os.path.basename(saved)
                candidate = os.path.join(self.root, "orig", name)
                if os.path.isfile(candidate):
                    return candidate
        orig = os.path.join(self.root, "orig")
        if os.path.isdir(orig):
            for name in os.listdir(orig):
                if name.lower().endswith(".exe"):
                    return os.path.join(orig, name)
        raise FileNotFoundError("no executable in orig/")

    def tools(self):
        path = os.path.join(self.root, "tools.json")
        data = {}
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        msvc = data.get("msvc_root") or os.environ.get("MSVC6_ROOT") or r"C:\projects\MSVC600"
        bin_dir = os.path.join(msvc, "VC98", "Bin")
        return {
            "dtk": self._tool_bin("dtk.exe", data.get("dtk") or ""),
            "objdiff": self._tool_bin("objdiff-cli.exe", data.get("objdiff_cli") or ""),
            "msvc": msvc,
            "cl": os.path.join(bin_dir, "cl.exe"),
            "dumpbin": os.path.join(bin_dir, "dumpbin.exe"),
            "binary_ninja": data.get("binary_ninja") or "",
        }

    def _tool_bin(self, filename, configured):
        tool_home = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        candidates = []
        if configured and (_inside(configured, self.root) or _inside(configured, tool_home)):
            candidates.append(configured)
        candidates.append(os.path.join(self.root, "tools", "bin", filename))
        candidates.append(os.path.join(tool_home, "tools", "bin", filename))
        for candidate in candidates:
            if candidate and os.path.isfile(candidate):
                return candidate
        return ""

    def _load_text(self):
        if self._blob is not None:
            return
        data, _image_base, entry, sections = banks.pe_sections(self.exe_path())
        text = banks._text_section(sections)
        raw_end = min(text["rawptr"] + text["rawsize"], len(data))
        blob = data[text["rawptr"]:raw_end]
        text_va = _image_base + text["va"]
        span = text["vsize"] or len(blob)
        if len(blob) < span:
            span = len(blob)
        blob = blob[:span]
        starts = banks.function_starts(blob, text_va)
        if entry % 4 == 0 and text_va <= entry < text_va + span:
            starts.add(entry)
        self._blob = blob
        self._text_va = text_va
        self._starts = starts

    def _split_banks(self):
        found = []
        text = _read(os.path.join(self.root, "config", "splits.txt"))
        name = None
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("bank/") and stripped.endswith(":"):
                name = stripped[:-1]
                continue
            match = re.search(r"start:(0x[0-9A-Fa-f]+)\s+end:(0x[0-9A-Fa-f]+)", line)
            if name and match:
                found.append({
                    "name": name,
                    "start": int(match.group(1), 16),
                    "end": int(match.group(2), 16),
                })
                name = None
        return found

    def banks(self):
        found = self._split_banks()
        if not found:
            return found
        self._load_text()
        touched, reported = self._score_maps()
        starts = sorted(self._starts)
        for bank in found:
            bank.update(self._bank_summary(bank, starts, touched, reported))
        return found

    def _count_scores(self, rows, scores):
        values = []
        complete = 0
        for start in rows:
            percent = scores.get("_fn_%08X" % start)
            if percent is None:
                continue
            number = float(percent)
            values.append(number)
            if number >= 100:
                complete += 1
        average = round(sum(values) / len(values), 1) if values else None
        return len(values), complete, average

    def _bank_summary(self, bank, starts, touched, reported):
        rows = [item for item in starts if bank["start"] <= item < bank["end"]]
        if not rows or rows[0] != bank["start"]:
            rows.insert(0, bank["start"])
        scored, complete, average = self._count_scores(rows, touched)
        report_scored, report_complete, report_average = self._count_scores(rows, reported)
        return {
            "functions": len(rows),
            "scored": scored,
            "complete": complete,
            "match_percent": average,
            "reported": report_scored,
            "report_complete": report_complete,
            "report_percent": report_average,
        }

    def functions(self, bank_name):
        self._load_text()
        bank = next(item for item in self.banks() if item["name"] == bank_name)
        starts = [item for item in sorted(self._starts) if bank["start"] <= item < bank["end"]]
        if not starts or starts[0] != bank["start"]:
            starts.insert(0, bank["start"])
        touched, reported = self._score_maps()
        overrides = self._function_ends()
        by_addr = self._symbols_by_addr()
        undname = self._undname()
        rows = []
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else bank["end"]
            forced = overrides.get("%08X" % start)
            if forced and start < forced <= end:
                end = forced
            key = "_fn_%08X" % start
            symbol = by_addr.get(start) or key
            rows.append({
                "addr": start,
                "end": end,
                "size": end - start,
                "name": names.display_name(symbol, undname),
                "symbol": symbol,
                "match_percent": touched.get(key),
                "report_percent": reported.get(key),
            })
        return rows

    def _bytes(self, addr, size):
        self._load_text()
        offset = addr - self._text_va
        if offset < 0 or offset + size > len(self._blob):
            raise ValueError("address is outside .text")
        return self._blob[offset:offset + size]

    def assembly(self, addr, size):
        blob = self._bytes(addr, size)
        try:
            from capstone import CS_ARCH_X86, CS_MODE_32, Cs
        except ImportError:
            return blob.hex(" ")
        md = Cs(CS_ARCH_X86, CS_MODE_32)
        md.skipdata = True
        lines = []
        for insn in md.disasm(blob, addr):
            lines.append("%08X  %-8s %s" % (insn.address, insn.mnemonic, insn.op_str))
        return "\n".join(lines)

    def cpp_path(self, bank_name):
        return os.path.join(self.root, "src", bank_name + ".cpp")

    def read_cpp(self, bank_name, addr):
        text = _read(self.cpp_path(bank_name))
        start = FN_START % addr
        end = FN_END % addr
        begin = text.find(start)
        if begin < 0:
            return self._stub(addr)
        stop = text.find(end, begin)
        if stop < 0:
            return text[begin:]
        return text[begin + len(start):stop].strip("\n")

    def _stub(self, addr):
        return "extern \"C\" int _fn_%08X(void)\n{\n    return 0;\n}" % addr

    def save_cpp(self, bank_name, addr, size, code):
        path = self.cpp_path(bank_name)
        text = _read(path)
        start = FN_START % addr
        end = FN_END % addr
        block = start + code.strip("\n") + "\n" + end
        begin = text.find(start)
        if begin >= 0:
            stop = text.find(end, begin)
            if stop < 0:
                stop = len(text)
            else:
                stop += len(end)
            text = text[:begin] + block + text[stop:]
        else:
            if text and not text.endswith("\n"):
                text += "\n"
            text += "\n" + block
        _write(path, text if text.endswith("\n") else text + "\n")
        self._ensure_symbol(addr, size)
        return path

    def _undname(self):
        return os.path.join(self.tools()["msvc"], "Common", "Tools", "UNDNAME.EXE")

    def _symbol_text(self):
        return _read(os.path.join(self.root, "config", "dtk_symbols.txt"))

    def _symbols_by_addr(self):
        found = {}
        for match in re.finditer(r"(?m)^(\S+) = \.text:0x([0-9A-Fa-f]+);", self._symbol_text()):
            found[int(match.group(2), 16)] = match.group(1)
        return found

    def _symbol_name(self, addr):
        return self._symbols_by_addr().get(addr, "")

    def _symbol_index(self):
        found = {}
        for filename in ("dtk_symbols.txt", "symbols_all.txt", "symbols.txt"):
            for match in re.finditer(r"(?m)^(\S+) = \.text:0x([0-9A-Fa-f]+);", _read(os.path.join(self.root, "config", filename))):
                found[match.group(1)] = "_fn_%08X" % int(match.group(2), 16)
        return found

    def refactor_function(self, addr, new_name, size):
        new_name = names.check_name((new_name or "").strip())
        symbol_text = self._symbol_text()
        current_symbol = names.symbol_at(symbol_text, addr)
        old_name = names.display_name(current_symbol, self._undname()) if current_symbol else ""
        if new_name == old_name:
            return {"name": new_name, "symbol": current_symbol, "files": 0}
        bank = bank_of(self, addr)
        block = self.read_cpp(bank["name"], addr)
        signature = block.split("{", 1)[0]
        symbol = names.decorate(new_name, signature, current_symbol)
        owner = self._symbol_index().get(symbol)
        if owner and owner != "_fn_%08X" % addr:
            raise ValueError("%s is already the symbol for another function." % symbol)
        changed = []
        for path in names.source_files(self.root):
            original = _read(path)
            updated = names.replace_identifiers(original, addr, old_name, new_name)
            if updated != original:
                _write(path, updated)
                changed.append(path)
        symbol_path = os.path.join(self.root, "config", "dtk_symbols.txt")
        rewritten, _existed = names.rewrite_symbol_line(_read(symbol_path), addr, symbol, size)
        _write(symbol_path, rewritten if rewritten.endswith("\n") else rewritten + "\n")
        changed.append(symbol_path)
        for filename, rewriter in (
            ("symbols_all.txt", lambda text: names.rewrite_symbol_line(text, addr, symbol, size)[0]),
            ("symbols.txt", lambda text: names.rewrite_symbol_line(text, addr, symbol, size)[0]),
        ):
            path = os.path.join(self.root, "config", filename)
            if not os.path.isfile(path):
                continue
            updated = rewriter(_read(path))
            _write(path, updated if updated.endswith("\n") else updated + "\n")
            changed.append(path)
        reccmp = os.path.join(self.root, "config", "reccmp.csv")
        if os.path.isfile(reccmp):
            updated, did = names.rewrite_reccmp(_read(reccmp), addr, symbol)
            if did:
                _write(reccmp, updated if updated.endswith("\n") else updated + "\n")
                changed.append(reccmp)
        self._score_cache = None
        return {
            "name": new_name,
            "symbol": symbol,
            "files": len(changed),
            "bank": bank["name"],
            "cpp": self.read_cpp(bank["name"], addr),
        }

    def _ensure_symbol(self, addr, size):
        path = os.path.join(self.root, "config", "dtk_symbols.txt")
        text = _read(path)
        current = names.symbol_at(text, addr) or ("_fn_%08X" % addr)
        rewritten, _existed = names.rewrite_symbol_line(text, addr, current, size)
        _write(path, rewritten if rewritten.endswith("\n") else rewritten + "\n")

    def pseudo_c(self, addr):
        import ghidra_session
        return ghidra_session.decompile(self.root, self.exe_path(), addr)

    def apply_split(self, addr, end):
        bank = bank_of(self, addr)
        row = next(item for item in self.functions(bank["name"]) if item["addr"] == addr)
        if not (addr < end <= row["end"]):
            return {"note": "That end is outside this function.", "pseudo": ""}
        path = os.path.join(self.root, "build", "function_ends.json")
        data = {}
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle) or {}
            except (OSError, ValueError):
                data = {}
        data["%08X" % addr] = end
        _write(path, json.dumps(data, indent=2) + "\n")
        self._forget_score("_fn_%08X" % addr)
        import ghidra_session
        text = ghidra_session.split_function(self.root, self.exe_path(), addr, end)
        return {
            "pseudo": text,
            "assembly": self.assembly(addr, end - addr),
            "size": end - addr,
            "end": end,
            "note": "Ghidra now ends this function at %08X." % end,
        }

    def _function_ends(self):
        path = os.path.join(self.root, "build", "function_ends.json")
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle) or {}
        except (OSError, ValueError):
            return {}
        found = {}
        for key, value in data.items():
            try:
                found["%08X" % int(str(key), 16)] = int(value)
            except (TypeError, ValueError):
                continue
        return found

    def _forget_score(self, name):
        path = os.path.join(self.root, "build", "diff", "scores.json")
        data = self._read_scores(path, os.path.join(self.root, "build", "scores.json"))
        if name not in data:
            self._score_cache = None
            return
        del data[name]
        _write(path, json.dumps(data, indent=2, sort_keys=True) + "\n")
        self._score_cache = None

    def ghidra_pass_status(self):
        with self._pass_lock:
            return dict(self._pass)

    def start_ghidra_pass(self, bank_name):
        with self._pass_lock:
            if self._pass.get("running"):
                return dict(self._pass)
            self._pass = {
                "running": True,
                "all": False,
                "bank": bank_name,
                "banks_done": 0,
                "banks_total": 1,
                "phase": "drafting",
                "done": 0,
                "total": 0,
                "drafted": 0,
                "kept": 0,
                "skipped": 0,
                "addr": None,
                "note": "",
                "error": "",
            }
        threading.Thread(target=self._run_ghidra_pass, args=(bank_name,), daemon=True).start()
        return self.ghidra_pass_status()

    def start_ghidra_all(self):
        names = [bank["name"] for bank in self.banks()]
        with self._pass_lock:
            if self._pass.get("running"):
                return dict(self._pass)
            self._pass = {
                "running": True,
                "all": True,
                "bank": names[0] if names else "",
                "banks_done": 0,
                "banks_total": len(names),
                "phase": "drafting",
                "done": 0,
                "total": 0,
                "drafted": 0,
                "kept": 0,
                "skipped": 0,
                "addr": None,
                "note": "",
                "error": "",
            }
        threading.Thread(target=self._run_ghidra_all, args=(names,), daemon=True).start()
        return self.ghidra_pass_status()

    def _pass_update(self, **fields):
        with self._pass_lock:
            self._pass.update(fields)

    def _run_ghidra_pass(self, bank_name):
        try:
            self._ghidra_pass(bank_name)
        except Exception as exc:
            self._pass_update(running=False, phase="done", error=str(exc))

    def _run_ghidra_all(self, names):
        drafted = kept = skipped = complete = scored = 0
        failed = []
        try:
            for index, name in enumerate(names):
                self._pass_update(
                    bank=name,
                    banks_done=index,
                    banks_total=len(names),
                    phase="drafting",
                    done=0,
                    total=0,
                    addr=None,
                    error="",
                )
                try:
                    result = self._ghidra_pass(name, finish=False)
                except Exception as exc:
                    failed.append("%s (%s)" % (name, exc))
                    continue
                drafted += result["drafted"]
                kept += result["kept"]
                skipped += result["skipped"]
                complete += result["complete"]
                scored += result["scored"]
                if result.get("error"):
                    failed.append(name)
            note = "All banks: drafted %s, left %s edited, skipped %s. %s complete, %s scored." % (
                drafted, kept, skipped, complete, scored,
            )
            error = ""
            if failed:
                shown = ", ".join(item.split()[0].rsplit("/", 1)[-1] for item in failed[:8])
                extra = "" if len(failed) <= 8 else " +" + str(len(failed) - 8)
                error = "%s banks did not compile: %s%s" % (len(failed), shown, extra)
            self._pass_update(
                running=False,
                phase="done",
                bank=names[-1] if names else "",
                banks_done=len(names),
                drafted=drafted,
                kept=kept,
                skipped=skipped,
                complete=complete,
                scored=scored,
                note=note,
                error=error,
                addr=None,
            )
        except Exception as exc:
            self._pass_update(running=False, phase="done", error=str(exc))

    def _ghidra_pass(self, bank_name, finish=True):
        rows = self.functions(bank_name)
        by_addr = {row["addr"]: row for row in rows}
        drafted = set()
        kept = 0
        skipped = 0
        self._pass_update(total=len(rows), phase="drafting")
        for index, row in enumerate(rows):
            addr = row["addr"]
            self._pass_update(done=index, addr=addr, drafted=len(drafted), kept=kept, skipped=skipped)
            current = self.read_cpp(bank_name, addr)
            if not _replaceable_draft(current, addr):
                kept += 1
                continue
            pseudo = self._pseudo_ready(addr)
            code = ""
            if pseudo and "{" in pseudo and not pseudo.startswith("Ghidra"):
                code = self.draft_cpp(addr, pseudo).get("code") or ""
            if not code:
                skipped += 1
                continue
            self.save_cpp(bank_name, addr, row["size"], code)
            drafted.add(addr)
        self._pass_update(done=len(rows), drafted=len(drafted), kept=kept, skipped=skipped, phase="compiling", addr=None)
        for row in rows:
            self._ensure_symbol(row["addr"], row["size"])
        note = self.resplit()
        reverted = set()
        compiled = False
        compile_error = ""
        for _attempt in range(40):
            try:
                self.compile_bank(bank_name)
                compiled = True
                break
            except (RuntimeError, FileNotFoundError) as exc:
                compile_error = str(exc)
                blamed = _error_addrs(_read(self.cpp_path(bank_name)), compile_error)
                fresh = [addr for addr in blamed if addr in drafted]
                blocked = [addr for addr in blamed if addr not in drafted]
                if blocked or not fresh:
                    break
                for addr in fresh:
                    self.save_cpp(bank_name, addr, by_addr[addr]["size"], self._stub(addr))
                    drafted.discard(addr)
                    reverted.add(addr)
                    skipped += 1
                self._pass_update(drafted=len(drafted), skipped=skipped)
        if not compiled:
            detail = compile_error.strip().splitlines()
            shown = " ".join(detail[-4:])[:500] if detail else "The bank did not compile."
            result = {
                "drafted": len(drafted),
                "kept": kept,
                "skipped": skipped,
                "complete": 0,
                "scored": 0,
                "error": shown,
            }
            if finish:
                self._pass_update(running=False, phase="done", error=shown, note=note, **{
                    key: result[key] for key in ("drafted", "kept", "skipped")
                })
            else:
                self._pass_update(drafted=len(drafted), kept=kept, skipped=skipped, error=shown, note=note)
            return result
        self._pass_update(phase="scoring", drafted=len(drafted), skipped=skipped)
        found = self._score_compiled_bank(bank_name)
        for addr in reverted:
            self._forget_score("_fn_%08X" % addr)
        names = {row["name"] for row in rows}
        relevant = {name: percent for name, percent in found.items() if name in names}
        complete = sum(1 for value in relevant.values() if float(value) >= 100)
        summary = "Ghidra pass drafted %s, left %s edited, skipped %s. %s complete, %s scored." % (
            len(drafted),
            kept,
            skipped,
            complete,
            len(relevant),
        )
        result = {
            "drafted": len(drafted),
            "kept": kept,
            "skipped": skipped,
            "complete": complete,
            "scored": len(relevant),
            "error": "",
        }
        if finish:
            self._pass_update(
                running=False,
                phase="done",
                note=(note + " " + summary).strip(),
                **result,
            )
        return result

    def _pseudo_ready(self, addr):
        text = ""
        for _try in range(40):
            text = self.pseudo_c(addr) or ""
            if not text.startswith("Ghidra is"):
                return text
            time.sleep(2)
        return text

    def _score_compiled_bank(self, bank_name):
        tools = self.tools()
        objdiff = tools["objdiff"]
        target = os.path.join(self.root, "build", "base", "obj", bank_name + ".o")
        obj = os.path.join(self.root, "build", "diff", "obj", bank_name + ".obj")
        if not objdiff or not os.path.isfile(objdiff):
            raise FileNotFoundError("objdiff was not found.")
        if not os.path.isfile(target):
            raise FileNotFoundError("Base object %s is missing." % target)
        diff_path = os.path.join(self.root, "build", "diff", "last-diff.json")
        proc = subprocess.run(
            [objdiff, "diff", "-1", target, "-2", obj, "-o", diff_path],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0 or not os.path.isfile(diff_path):
            raise RuntimeError((proc.stderr or proc.stdout or "objdiff failed").strip())
        with open(diff_path, "r", encoding="utf-8") as handle:
            found = _scores_from_diff(json.load(handle))
        if found:
            self._remember_scores(found)
        return found

    def draft_cpp(self, addr, pseudo=None):
        import draft_c
        if not pseudo:
            pseudo = self.pseudo_c(addr)
        code = draft_c.draft(addr, pseudo)
        if code:
            return {"code": code}
        note = pseudo if (pseudo or "").startswith("Ghidra") else "Ghidra did not return a function to draft."
        return {"code": "", "note": note}

    def source_assembly(self, bank_name, addr):
        end = None
        for row in self.functions(bank_name):
            if row["addr"] == addr:
                end = row["end"]
                break
        obj = os.path.join(self.root, "build", "diff", "obj", bank_name + ".obj")
        if os.path.isfile(obj):
            listing = _coff_listing(obj, addr, end)
            if listing:
                return listing
        resolved = _compiled_listing(self.root, addr)
        if resolved:
            return resolved
        if not os.path.isfile(obj):
            return "Build this bank to compile your C. Compiled assembly is the listing of that object."
        return "This object has no _fn_%08X." % addr

    def compile_bank(self, bank_name):
        tools = self.tools()
        if not os.path.isfile(tools["cl"]):
            raise FileNotFoundError("cl.exe was not found. tools.json msvc_root is %s" % tools["msvc"])
        src = self.cpp_path(bank_name)
        if not os.path.isfile(src):
            raise FileNotFoundError("no C++ file for %s" % bank_name)
        import draft_c
        draft_c.prepare_file(src)
        out = os.path.join(self.root, "build", "diff", "obj", bank_name + ".obj")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        msvc = tools["msvc"]
        env = os.environ.copy()
        env["PATH"] = os.path.join(msvc, "VC98", "Bin") + os.pathsep + os.path.join(msvc, "Common", "MSDev98", "Bin")
        env["INCLUDE"] = os.path.join(msvc, "VC98", "Include")
        include = os.path.join(self.root, "include")
        cmd = [tools["cl"], "/nologo", "/O2", "/c"]
        if os.path.isdir(include):
            cmd.append("/I" + include)
        cmd.extend(["/Fo" + out, src])
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=self.root)
        if proc.returncode != 0:
            raise RuntimeError((proc.stdout or "") + (proc.stderr or ""))
        _align_cdecl_symbols(out)
        return out

    def resplit(self):
        tools = self.tools()
        dtk = tools["dtk"]
        if not dtk or not os.path.isfile(dtk):
            return "dtk.exe was not found in this project's tools/bin or in this tool's tools/bin."
        proc = subprocess.run(
            [dtk, "-C", self.root, "coff", "split", "--no-update", "config/dtk.yml", "build/base"],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "dtk split failed").strip())
        return "Split objects refreshed."

    def score(self, bank_name, addr, size):
        self._ensure_symbol(addr, size)
        obj, _fixes = self.build_unit(bank_name)
        result = self.diff_function(bank_name, addr)
        result["object"] = obj
        result["source_assembly"] = self.source_assembly(bank_name, addr)
        return result

    def configure(self):
        tools = self.tools()
        if not tools["dtk"] or not os.path.isfile(tools["dtk"]):
            raise FileNotFoundError("dtk.exe was not found in this project's tools/bin or in this tool's tools/bin.")
        if not tools["objdiff"] or not os.path.isfile(tools["objdiff"]):
            raise FileNotFoundError("objdiff-cli.exe was not found in this project's tools/bin or in this tool's tools/bin.")
        if not os.path.isfile(tools["cl"]):
            raise FileNotFoundError("cl.exe was not found. tools.json msvc_root is %s" % tools["msvc"])
        names = self._split_unit_names()
        compiled = []
        for name in names:
            src = "src/" + name + ".cpp"
            if os.path.isfile(os.path.join(self.root, src.replace("/", os.sep))):
                compiled.append((name, src))
        self._write_objdiff(names)
        self._write_ninja(tools, compiled)
        return compiled

    def _split_unit_names(self):
        names = []
        for line in _read(os.path.join(self.root, "config", "splits.txt")).splitlines():
            stripped = line.strip()
            if stripped.endswith(":") and not stripped.startswith(".") and stripped not in ("Sections:",):
                if re.match(r"[\w./+-]+", stripped[:-1]):
                    names.append(stripped[:-1])
        return names

    def _object_rel(self, name):
        stem = name[:-4] if name.endswith(".cpp") else name
        directory, leaf = os.path.split(stem)
        leaf = leaf.replace("_", "__")
        return "/".join(part for part in (directory.replace("\\", "/"), leaf) if part) + ".o"

    def _split_inputs(self):
        base = "orig"
        obj = ""
        for line in _read(os.path.join(self.root, "config", "dtk.yml")).splitlines():
            if line.startswith("object_base:"):
                base = line.split(":", 1)[1].strip()
            elif line.startswith("object:"):
                obj = line.split(":", 1)[1].strip()
        inputs = ["config/dtk.yml", "config/splits.txt", "config/dtk_symbols.txt"]
        if obj:
            inputs.append(base.replace("\\", "/") + "/" + obj)
        return inputs

    def _objdiff_payload(self, names):
        units = []
        for name in names:
            metadata = {"complete": False}
            if name.endswith(".cpp") or name.startswith("bank/"):
                metadata["progress_categories"] = ["bank" if name.startswith("bank/") else "engine"]
            report_obj = "build/report/obj/" + name + ".obj"
            base = report_obj if os.path.isfile(os.path.join(self.root, report_obj.replace("/", os.sep))) else None
            units.append({
                "name": name,
                "target_path": "build/base/obj/" + self._object_rel(name),
                "base_path": base,
                "metadata": metadata,
            })
        return {
            "$schema": "https://raw.githubusercontent.com/encounter/objdiff/main/config.schema.json",
            "custom_make": "ninja",
            "build_target": False,
            "build_base": False,
            "watch_patterns": ["*.c", "*.cpp", "*.h", "*.py", "*.yml", "*.txt", "*.json"],
            "ignore_patterns": ["build/**/*", "orig/**/*"],
            "progress_categories": [{"id": "bank", "name": "Banks"}],
            "units": units,
        }

    def _write_objdiff(self, names):
        _write(os.path.join(self.root, "objdiff.json"), json.dumps(self._objdiff_payload(names), indent=2) + "\n")

    def _write_ninja(self, tools, compiled):
        dtk = tools["dtk"].replace("\\", "/")
        msvc = tools["msvc"].replace("\\", "/")
        cl = msvc + "/VC98/Bin/cl.exe"
        include = msvc + "/VC98/Include"
        bindir = msvc + "/VC98/Bin"
        msdev = msvc + "/Common/MSDev98/Bin"
        extra = " /I include" if os.path.isdir(os.path.join(self.root, "include")) else ""
        compile_rules = """
rule cc
  command = cmd /s /c "for %%I in ("$out") do mkdir "%%~dpI" 2>nul & set INCLUDE=%s& set PATH=%s;%s& %s /nologo /O2 /c%s /Fo"$out" $in"
  description = cl $in
""" % (include, bindir, msdev, cl, extra)
        lines = []
        for unit, src in compiled:
            lines.append("build %s: cc %s" % ("build/diff/obj/" + unit + ".obj", src))
            lines.append("build %s: cc %s" % ("build/report/obj/" + unit + ".obj", src))
        text = """\
# Generated by the workbench. ninja splits the executable. Diff and report compile a unit into their own folders.
rule split
  command = %s coff split --no-update config/dtk.yml build/base
  description = split
%s
build build/base/config.json: split %s
%s
default build/base/config.json
""" % (dtk, compile_rules, " ".join(self._split_inputs()), "\n".join(lines))
        _write(os.path.join(self.root, "build.ninja"), text)

    def _ninja(self, targets):
        exe = _ninja_exe()
        if not exe:
            raise FileNotFoundError("ninja.exe was not found next to this Python or on PATH.")
        proc = subprocess.run(
            [exe, "-C", self.root] + list(targets),
            capture_output=True,
            text=True,
        )
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if proc.returncode != 0:
            raise RuntimeError(output or "ninja failed")
        return output

    def _compile_target(self, target, on_phase=None):
        try:
            self._ninja([target])
            _align_cdecl_symbols(os.path.join(self.root, target.replace("/", os.sep)))
            return ""
        except RuntimeError as exc:
            log = _one_unit_log(str(exc))
            import build_cases
            outcome = build_cases.apply(self.root, log)
            if not outcome["changed"]:
                raise RuntimeError(outcome["report"])
            if on_phase:
                on_phase("compile", "Applied automatic fixes")
            try:
                self._ninja([target])
            except RuntimeError as again:
                raise RuntimeError(
                    outcome["report"] + "\n\nCompiled again.\n" + build_cases.classify_report(_one_unit_log(str(again)))
                )
            _align_cdecl_symbols(os.path.join(self.root, target.replace("/", os.sep)))
            return outcome["report"]

    def build_unit(self, bank_name, on_phase=None):
        src = self.cpp_path(bank_name)
        if not os.path.isfile(src):
            raise FileNotFoundError("no C++ file for %s" % bank_name)

        def phase(name, note):
            if on_phase:
                on_phase(name, note)

        phase("configure", "Writing build.ninja")
        self.configure()
        phase("split", "Splitting the executable")
        self._ninja(["build/base/config.json"])
        import draft_c
        draft_c.prepare_file(src)
        phase("compile", "Compiling " + bank_name)
        rel = "build/diff/obj/" + bank_name + ".obj"
        fixes = self._compile_target(rel, phase)
        obj = os.path.join(self.root, rel.replace("/", os.sep))
        return obj, fixes

    def _missing_symbol_note(self, bank_name, addr, name, missing_left, missing_right):
        block = self.read_cpp(bank_name, addr)
        fun = "FUN_%08X" % addr
        if missing_right and re.search(r"\bFUN_%08X\s*\(" % addr, block, re.I):
            return "This block defines %s. Build renames it to %s, then Diff." % (fun, name)
        if missing_right and re.search(r"\b%s\s*\(" % re.escape(name), block):
            return "The source names %s. This object was built from a different name. Build again, then Diff." % name
        if missing_left and not missing_right:
            return "The retail object has no %s." % name
        return "objdiff did not pair %s." % name

    def diff_function(self, bank_name, addr):
        tools = self.tools()
        objdiff = tools["objdiff"]
        if not objdiff or not os.path.isfile(objdiff):
            raise FileNotFoundError("objdiff was not found.")
        retail = os.path.join(self.root, "build", "base", "obj", self._object_rel(bank_name).replace("/", os.sep))
        built = os.path.join(self.root, "build", "diff", "obj", bank_name.replace("/", os.sep) + ".obj")
        if not os.path.isfile(retail):
            raise FileNotFoundError("The retail object is missing. Build splits the executable.")
        if not os.path.isfile(built):
            raise FileNotFoundError("This unit is not built. Build compiles it.")
        diff_path = os.path.join(self.root, "build", "diff", "last-diff.json")
        name = "_fn_%08X" % addr
        proc = subprocess.run(
            [objdiff, "diff", "-1", retail, "-2", built, "-o", diff_path, name],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0 or not os.path.isfile(diff_path):
            raise RuntimeError((proc.stderr or proc.stdout or "objdiff failed").strip())
        with open(diff_path, "r", encoding="utf-8") as handle:
            diff = json.load(handle)
        left = _symbol_instructions(diff.get("left"), name)
        right = _symbol_instructions(diff.get("right"), name)
        percent = _symbol_percent(diff, name)
        if percent is None and left is not None and right is not None:
            percent = _instruction_percent(left, right, addr)
        if left is None or right is None:
            if percent is None:
                note = self._missing_symbol_note(bank_name, addr, name, left is None, right is None)
                return {
                    "state": "mismatch",
                    "note": note,
                    "diff": note,
                    "match_percent": None,
                    "source_assembly": self.source_assembly(bank_name, addr),
                    "cpp": self.read_cpp(bank_name, addr),
                }
            self._remember_scores({name: percent})
            return {
                "state": "match" if percent >= 100 else "mismatch",
                "note": "Bytes match." if percent >= 100 else "Diff mismatch.",
                "diff": "" if percent >= 100 else "Diff mismatch.",
                "match_percent": percent,
                "source_assembly": self.source_assembly(bank_name, addr),
            }
        left_lines = _format_instructions(left, addr)
        right_lines = _format_instructions(right, addr)
        if percent is None:
            text = _mismatch_text(left_lines, right_lines)
            return {
                "state": "mismatch",
                "note": "Diff mismatch.",
                "diff": text or "The instructions differ.",
                "match_percent": None,
                "source_assembly": self.source_assembly(bank_name, addr),
            }
        if percent >= 100:
            self._remember_scores({name: 100})
            return {
                "state": "match",
                "note": "Bytes match.",
                "diff": "",
                "match_percent": 100,
                "source_assembly": self.source_assembly(bank_name, addr),
            }
        text = _mismatch_text(left_lines, right_lines)
        if percent is not None:
            self._remember_scores({name: percent})
        return {
            "state": "mismatch",
            "note": "Diff mismatch.",
            "diff": text or "The instructions differ.",
            "match_percent": percent,
            "source_assembly": self.source_assembly(bank_name, addr),
        }

    def job_status(self):
        with self._job_lock:
            return dict(self._job)

    def _job_update(self, **fields):
        with self._job_lock:
            self._job.update(fields)

    def start_build(self, bank_name, addr, size, code):
        with self._job_lock:
            if self._job.get("running"):
                return dict(self._job)
            self._job = {
                "running": True,
                "kind": "build",
                "phase": "save",
                "note": "Saving the function",
                "error": "",
                "result": None,
            }
        threading.Thread(
            target=self._run_build,
            args=(bank_name, addr, size, code),
            daemon=True,
        ).start()
        return self.job_status()

    def _run_build(self, bank_name, addr, size, code):
        try:
            self.save_cpp(bank_name, addr, size, code)
            obj, fixes = self.build_unit(bank_name, on_phase=lambda phase, note: self._job_update(phase=phase, note=note))
            note = "Built %s. Diff compares it to the retail object." % os.path.basename(obj)
            if fixes:
                note = "Applied automatic fixes. " + note
            self._job_update(
                running=False,
                phase="built",
                note=note,
                error="",
                result={
                    "object": obj,
                    "source_assembly": self.source_assembly(bank_name, addr),
                    "cpp": self.read_cpp(bank_name, addr),
                    "fixes": fixes,
                },
            )
        except Exception as exc:
            cpp = ""
            try:
                cpp = self.read_cpp(bank_name, addr)
            except Exception:
                cpp = ""
            self._job_update(
                running=False,
                phase="failed",
                note="Build failed",
                error=str(exc),
                result={"cpp": cpp} if cpp else None,
            )

    def _hundred_banks(self):
        self._load_text()
        touched, _reported = self._score_maps()
        starts = sorted(self._starts or [])
        chosen = []
        for bank in self._split_banks():
            rows = [item for item in starts if bank["start"] <= item < bank["end"]]
            if not rows or rows[0] != bank["start"]:
                rows.insert(0, bank["start"])
            complete = False
            for start in rows:
                percent = touched.get("_fn_%08X" % start)
                if percent is not None and float(percent) >= 100:
                    complete = True
                    break
            if complete and os.path.isfile(self.cpp_path(bank["name"])):
                chosen.append(bank["name"])
        return chosen

    def _place_report_object(self, bank_name):
        src = self.cpp_path(bank_name)
        diff_obj = os.path.join(self.root, "build", "diff", "obj", bank_name.replace("/", os.sep) + ".obj")
        report_obj = os.path.join(self.root, "build", "report", "obj", bank_name.replace("/", os.sep) + ".obj")
        source_time = os.path.getmtime(src) if os.path.isfile(src) else 0
        if os.path.isfile(report_obj) and os.path.getmtime(report_obj) >= source_time:
            return
        if os.path.isfile(diff_obj) and os.path.getmtime(diff_obj) >= source_time:
            os.makedirs(os.path.dirname(report_obj), exist_ok=True)
            shutil.copyfile(diff_obj, report_obj)
            return
        self._compile_target("build/report/obj/" + bank_name + ".obj")

    def start_report_hundreds(self):
        with self._job_lock:
            if self._job.get("running"):
                return dict(self._job)
            self._job = {
                "running": True,
                "kind": "report",
                "phase": "report",
                "note": "Finding banks with a 100% diff",
                "error": "",
                "result": None,
            }
        threading.Thread(target=self._run_report_hundreds, daemon=True).start()
        return self.job_status()

    def _run_report_hundreds(self):
        try:
            names = self._hundred_banks()
            if not names:
                self._job_update(running=False, phase="done", note="No diff is 100%.", error="", result=None)
                return
            self.configure()
            import draft_c
            failed = []
            for index, bank_name in enumerate(names, 1):
                self._job_update(
                    phase="report",
                    note="Reporting 100%% (%s/%s) %s" % (index, len(names), bank_name),
                )
                src = self.cpp_path(bank_name)
                try:
                    if os.path.isfile(src):
                        draft_c.prepare_file(src)
                    self._place_report_object(bank_name)
                except Exception:
                    failed.append(bank_name)
            self._job_update(phase="report", note="Writing the report for %s banks" % (len(names) - len(failed)))
            result = self._generate_report()
            result["hundreds"] = True
            result["failed"] = failed
            result["note"] = "Reported %s banks with a 100%% diff. %s" % (len(names) - len(failed), result["note"])
            if failed:
                result["note"] += " %s banks did not compile." % len(failed)
            self._job_update(running=False, phase="done", note=result["note"], error="", result=result)
        except Exception as exc:
            self._job_update(running=False, phase="failed", note="Report failed", error=str(exc), result=None)

    def start_report(self, bank_name):
        with self._job_lock:
            if self._job.get("running"):
                return dict(self._job)
            self._job = {
                "running": True,
                "kind": "report",
                "phase": "configure",
                "note": "Compiling %s for the report" % bank_name,
                "error": "",
                "result": None,
            }
        threading.Thread(target=self._run_report, args=(bank_name,), daemon=True).start()
        return self.job_status()

    def _run_report(self, bank_name):
        try:
            self.configure()
            import draft_c
            src = self.cpp_path(bank_name)
            if os.path.isfile(src):
                draft_c.prepare_file(src)
            self._job_update(phase="report", note="Compiling %s into the report" % bank_name)
            self._compile_target("build/report/obj/" + bank_name + ".obj")
            self._job_update(phase="report", note="Writing the report for %s" % bank_name)
            self._write_objdiff(self._split_unit_names())
            result = self._generate_report()
            result["note"] = "Reported %s. %s" % (bank_name, result["note"])
            self._job_update(running=False, phase="done", note=result["note"], error="", result=result)
        except Exception as exc:
            self._job_update(running=False, phase="failed", note="Report failed", error=str(exc), result=None)

    def build_report(self):
        self.configure()
        return self._generate_report()

    def _objdiff_generate(self, objdiff, full, units, out, timeout):
        narrowed = dict(full)
        narrowed.pop("custom_make", None)
        narrowed["units"] = units
        config_path = os.path.join(self.root, "objdiff.json")
        _write(config_path, json.dumps(narrowed, indent=2) + "\n")
        proc = None
        try:
            proc = subprocess.Popen(
                [objdiff, "report", "generate", "-p", self.root, "-o", out, "-f", "json"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.root,
                text=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise
            if proc.returncode != 0 or not os.path.isfile(out):
                raise RuntimeError((stderr or stdout or "objdiff report failed").strip())
        finally:
            _write(config_path, json.dumps(full, indent=2) + "\n")

    def _generate_report(self):
        tools = self.tools()
        objdiff = tools["objdiff"]
        if not objdiff or not os.path.isfile(objdiff):
            raise FileNotFoundError("objdiff was not found.")
        names = self._split_unit_names()
        full = self._objdiff_payload(names)
        ready = [unit for unit in full["units"] if unit.get("base_path")]
        if not ready:
            raise RuntimeError("This bank did not produce an object for the report.")
        for unit in ready:
            _align_cdecl_symbols(os.path.join(self.root, unit["base_path"].replace("/", os.sep)))
        out = os.path.join(self.root, "build", "report", "report.json")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        skipped = []
        try:
            self._objdiff_generate(objdiff, full, ready, out, 5)
        except subprocess.TimeoutExpired:
            good = []
            for unit in ready:
                try:
                    self._objdiff_generate(objdiff, full, [unit], out + ".one", 5)
                except subprocess.TimeoutExpired:
                    skipped.append(unit["name"])
                    continue
                good.append(unit)
            if os.path.isfile(out + ".one"):
                os.remove(out + ".one")
            if not good:
                raise RuntimeError("objdiff did not finish for %s." % ", ".join(skipped))
            self._objdiff_generate(objdiff, full, good, out, 20)
        import scrub_report
        scrub_report.scrub(out, self.root)
        self._score_cache = None
        _touched, reported = self._score_maps()
        values = []
        for percent in reported.values():
            try:
                values.append(float(percent))
            except (TypeError, ValueError):
                continue
        present = 0
        for name in self._split_unit_names():
            if name in skipped:
                continue
            if os.path.isfile(os.path.join(self.root, "build", "report", "obj", name + ".obj")):
                present += 1
        complete = sum(1 for value in values if value >= 100)
        partial = sum(1 for value in values if 50 <= value < 100)
        note = "%s units in the report." % present
        if skipped:
            note += " objdiff did not finish %s." % ", ".join(skipped)
        return {
            "report": out,
            "note": note,
            "units": present,
            "scored_functions": len(values),
            "complete_functions": complete,
            "partial_functions": partial,
        }

    def _diff_unit_scores(self, units):
        tools = self.tools()
        objdiff = tools["objdiff"]
        if not objdiff or not os.path.isfile(objdiff):
            return {}
        found = {}
        for unit in units:
            base = unit.get("base_path")
            target = unit.get("target_path")
            if not base or not target:
                continue
            base_abs = os.path.join(self.root, base.replace("/", os.sep))
            target_abs = os.path.join(self.root, target.replace("/", os.sep))
            if not os.path.isfile(base_abs) or not os.path.isfile(target_abs):
                continue
            diff_path = os.path.join(self.root, "build", "diff", "last-diff.json")
            proc = subprocess.run(
                [objdiff, "diff", "-1", target_abs, "-2", base_abs, "-o", diff_path],
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0 or not os.path.isfile(diff_path):
                continue
            try:
                with open(diff_path, "r", encoding="utf-8") as handle:
                    found.update(_scores_from_diff(json.load(handle)))
            except (OSError, ValueError):
                continue
        return found

    def _read_scores(self, path, legacy):
        source = path if os.path.isfile(path) else legacy
        found = {}
        if not os.path.isfile(source):
            return found
        try:
            with open(source, "r", encoding="utf-8") as handle:
                data = json.load(handle) or {}
        except (OSError, ValueError):
            return found
        for name, percent in data.items():
            key = _fn_key(name) or name
            if percent is not None:
                found[key] = percent
        return found

    def _report_json_path(self):
        new = os.path.join(self.root, "build", "report", "report.json")
        old = os.path.join(self.root, "build", "report.json")
        if os.path.isfile(new):
            return new
        return old if os.path.isfile(old) else new

    def _score_maps(self):
        if self._score_cache is not None:
            return self._score_cache
        touched = self._read_scores(os.path.join(self.root, "build", "diff", "scores.json"), os.path.join(self.root, "build", "scores.json"))
        reported = {}
        report = self._report_json_path()
        if os.path.isfile(report):
            try:
                with open(report, "r", encoding="utf-8") as handle:
                    reported = _report_scores(json.load(handle), self._symbol_index())
            except (OSError, ValueError):
                reported = {}
        self._score_cache = (touched, reported)
        return self._score_cache

    def function_scores(self):
        touched, _reported = self._score_maps()
        return touched

    def _remember_score(self, name, percent):
        self._remember_scores({name: percent})

    def _remember_scores(self, updates):
        if not updates:
            self._score_cache = None
            return
        path = os.path.join(self.root, "build", "diff", "scores.json")
        current = self._read_scores(path, os.path.join(self.root, "build", "scores.json"))
        for name, percent in updates.items():
            key = _fn_key(name)
            if key and percent is not None:
                current[key] = percent
        _write(path, json.dumps(current, indent=2, sort_keys=True) + "\n")
        self._score_cache = None


def _coff_listing(path, addr, end=None):
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return ""
    if len(data) < 20:
        return ""
    nsections = struct.unpack_from("<H", data, 2)[0]
    symptr, nsymbols = struct.unpack_from("<II", data, 8)
    if nsections <= 0 or symptr <= 0 or symptr >= len(data):
        return ""
    sections = []
    offset = 20
    for _index in range(nsections):
        rawsize, rawptr, relocptr = struct.unpack_from("<III", data, offset + 16)
        nreloc = struct.unpack_from("<H", data, offset + 32)[0]
        sections.append((rawptr, rawsize, relocptr, nreloc))
        offset += 40
    string_table = symptr + nsymbols * 18
    stop = end if end and end > addr else addr + 1
    found = []
    seen = set()
    for index in range(nsymbols):
        slot = symptr + index * 18
        if slot + 18 > len(data):
            break
        raw = data[slot:slot + 8]
        if raw[:4] == b"\0\0\0\0":
            start = string_table + struct.unpack_from("<I", raw, 4)[0]
            name_end = data.find(b"\0", start)
            name = data[start:name_end].decode("ascii", "replace")
        else:
            name = raw.split(b"\0", 1)[0].decode("ascii", "replace")
        key = _fn_key(name)
        if not key or key in seen:
            continue
        sym_addr = int(key[-8:], 16)
        if not (addr <= sym_addr < stop):
            continue
        value, section = struct.unpack_from("<Ih", data, slot + 8)
        if not 0 < section <= len(sections):
            continue
        seen.add(key)
        found.append((sym_addr, section, value))
    if not found:
        return ""
    parts = []
    for sym_addr, section, value in sorted(found):
        rawptr, rawsize, relocptr, nreloc = sections[section - 1]
        blob = data[rawptr:rawptr + rawsize]
        if value < 0 or value >= len(blob):
            value = 0
        relocs = {}
        for index in range(nreloc):
            slot = relocptr + index * 10
            if slot + 10 > len(data):
                break
            at, symbol, kind = struct.unpack_from("<IIH", data, slot)
            if kind not in (0x6, 0x14):
                continue
            relocs[at] = _coff_symbol_text(data, symptr, nsymbols, string_table, symbol)
        text = _disassemble_function(blob[value:], sym_addr, {key - value: label for key, label in relocs.items() if key >= value})
        text = _without_trailing_nops(text)
        if text:
            parts.append(text)
    return "\n".join(parts)


def _coff_symbol_text(data, symptr, nsymbols, string_table, index):
    if index < 0 or index >= nsymbols:
        return ""
    slot = symptr + index * 18
    raw = data[slot:slot + 8]
    if raw[:4] == b"\0\0\0\0":
        start = string_table + struct.unpack_from("<I", raw, 4)[0]
        stop = data.find(b"\0", start)
        name = data[start:stop].decode("ascii", "replace")
    else:
        name = raw.split(b"\0", 1)[0].decode("ascii", "replace")
    match = re.search(r"fn_0x([0-9A-Fa-f]+)", name, re.I)
    if match:
        return "0x%x" % int(match.group(1), 16)
    match = re.search(r"(?:fn_|LAB_|DAT_|Unwind_)([0-9A-Fa-f]{6,8})", name, re.I)
    if match:
        return "0x%x" % int(match.group(1), 16)
    plain = re.match(r"\?([A-Za-z0-9_]+)@@", name)
    if plain:
        return plain.group(1)
    return name.lstrip("_") or name


def _without_trailing_nops(text):
    lines = text.splitlines()
    while lines and lines[-1].split()[-1:] == ["nop"]:
        lines.pop()
    return "\n".join(lines)


def _disassemble_function(blob, addr, relocs):
    try:
        from capstone import CS_ARCH_X86, CS_MODE_32, Cs
    except ImportError:
        return ""
    lines = []
    md = Cs(CS_ARCH_X86, CS_MODE_32)
    for insn in md.disasm(blob, addr):
        operand = insn.op_str
        for at, text in relocs.items():
            if insn.address - addr <= at < insn.address - addr + insn.size and text:
                operand = text
                break
        lines.append(("%08X  %-8s %s" % (insn.address, insn.mnemonic, operand)).rstrip())
    return "\n".join(lines)


def _slice_dumpbin(text, symbol):
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if symbol in line and not line.startswith(" "):
            start = index
            break
        if line.strip().startswith(symbol):
            start = index
            break
    if start is None:
        return text[-4000:] if len(text) > 4000 else text
    chunk = []
    for line in lines[start:]:
        if chunk and line and not line.startswith(" ") and not line.startswith("\t") and symbol not in line:
            break
        chunk.append(line)
    return "\n".join(chunk)


def _replaceable_draft(code, addr):
    text = (code or "").strip()
    if not text or "Drafted from Ghidra" in text:
        return True
    name = "_fn_%08X" % addr
    lines = [line for line in text.splitlines() if line.strip()]
    return len(lines) <= 6 and "return 0;" in text and name in text


def _error_addrs(source, message):
    owners = {}
    current = None
    for index, line in enumerate(source.splitlines(), 1):
        match = re.match(r"// FN ([0-9A-Fa-f]{8})$", line.strip())
        if match:
            current = int(match.group(1), 16)
        owners[index] = current
    found = []
    for match in re.finditer(r"\((\d+)\)\s*:\s*(?:fatal )?error C", message or ""):
        addr = owners.get(int(match.group(1)))
        if addr and addr not in found:
            found.append(addr)
    return found


def _fn_key(name):
    text = str(name or "")
    match = re.search(r"fn_0x([0-9A-Fa-f]+)", text, re.I)
    if match:
        return "_fn_%08X" % int(match.group(1), 16)
    match = re.search(r"fn_([0-9A-Fa-f]{8})", text, re.I)
    if not match:
        return None
    return "_fn_" + match.group(1).upper()


def _percent_of(item):
    if item.get("fuzzy_match_percent") is not None:
        return item.get("fuzzy_match_percent")
    return item.get("match_percent")


def _report_scores(data, symbols=None):
    symbols = symbols or {}
    found = {}
    for unit in data.get("units") or []:
        for fn in unit.get("functions") or []:
            raw = fn.get("name")
            key = _fn_key(raw) or symbols.get(raw)
            percent = _percent_of(fn)
            if key and percent is not None:
                found[key] = percent
    return found


def _compiled_listing(project_root, addr):
    path = os.path.join(project_root, "build", "diff", "last-diff.json")
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return ""
    want = "_fn_%08X" % addr
    for sym in (data.get("right") or {}).get("symbols") or []:
        if _fn_key(sym.get("name")) != want or not sym.get("instructions"):
            continue
        return "\n".join(_format_instructions(sym["instructions"], addr))
    return ""


def _format_instructions(instructions, base):
    lines = []
    offset = 0
    index_at = {}
    cursor = 0
    for item in instructions:
        index_at[cursor] = len(index_at)
        cursor += int((item.get("instruction") or {}).get("size") or 0)
    for item in instructions:
        inner = item.get("instruction") or {}
        mnemonic, operands = _operands(inner, base, index_at)
        lines.append(("%08X  %-8s %s" % (base + offset, mnemonic, operands)).rstrip())
        offset += int(inner.get("size") or 0)
    return lines


def _operands(inner, base, index_at):
    mnemonic = ""
    chunks = []
    for part in inner.get("parts") or []:
        if "opcode" in part:
            mnemonic = part["opcode"].get("mnemonic") or ""
            continue
        if "basic" in part:
            chunks.append(part["basic"])
            continue
        arg = part.get("arg") or {}
        if arg.get("opaque") and arg["opaque"] != "short":
            chunks.append(arg["opaque"])
        elif "unsigned" in arg:
            chunks.append("0x%x" % int(arg["unsigned"]))
        elif "signed" in arg:
            chunks.append(_signed_text(arg["signed"]))
        elif "branch_dest" in arg:
            dest = int(arg["branch_dest"])
            chunks.append("0x%x" % (base + dest))
        elif arg.get("reloc"):
            chunks.append(_reloc_addr(inner.get("formatted") or ""))
    text = "".join(chunks).strip()
    if "[" in text and "dword ptr" not in text:
        text = text.replace("[", "dword ptr [")
    return mnemonic, text


def _signed_text(value):
    number = int(value)
    if number < 0:
        return "-0x%x" % abs(number)
    return "0x%x" % number


def _reloc_addr(formatted):
    match = re.search(r"DAT_([0-9A-Fa-f]+)", formatted or "")
    if match:
        return "0x%x" % int(match.group(1), 16)
    match = re.search(r"fn_([0-9A-Fa-f]{8})", formatted or "", re.I)
    if match:
        return "0x%x" % int(match.group(1), 16)
    return (formatted or "").lower()


def _instruction_tokens(instructions, base):
    index_at = {}
    cursor = 0
    for item in instructions:
        index_at[cursor] = len(index_at)
        cursor += int((item.get("instruction") or {}).get("size") or 0)
    tokens = []
    for item in instructions:
        inner = item.get("instruction") or {}
        mnemonic = ""
        args = []
        for part in inner.get("parts") or []:
            if "opcode" in part:
                mnemonic = part["opcode"].get("mnemonic") or ""
                continue
            arg = part.get("arg") or {}
            if arg.get("opaque") and arg["opaque"] != "short":
                args.append(arg["opaque"].lower())
            elif "unsigned" in arg:
                args.append("0x%x" % int(arg["unsigned"]))
            elif "signed" in arg:
                args.append(_signed_text(arg["signed"]))
            elif "branch_dest" in arg:
                dest = int(arg["branch_dest"])
                if dest in index_at:
                    args.append("L%d" % index_at[dest])
                else:
                    args.append("0x%x" % (base + dest))
            elif arg.get("reloc"):
                args.append(_reloc_addr(inner.get("formatted") or ""))
        tokens.append((mnemonic, tuple(args)))
    return tokens


def _instruction_percent(left_ins, right_ins, base):
    left = _instruction_tokens(left_ins, base)
    right = _instruction_tokens(right_ins, base)
    total = max(len(left), len(right))
    if not total:
        return None
    same = 0
    for index in range(min(len(left), len(right))):
        if left[index] == right[index]:
            same += 1
    return round(100.0 * same / total, 1)


def _symbol_percent(diff, name):
    """Objdiff's own percent. One differing operand is not a missed instruction."""
    want = _fn_key(name)
    if not want:
        return None
    for side in ("right", "left"):
        for sym in (diff.get(side) or {}).get("symbols") or []:
            if _fn_key(sym.get("name")) == want and sym.get("match_percent") is not None:
                return round(float(sym["match_percent"]), 1)
    return None


def _symbol_instructions(side, name):
    want = _fn_key(name)
    for sym in (side or {}).get("symbols") or []:
        if _fn_key(sym.get("name")) == want and sym.get("instructions"):
            return sym["instructions"]
    return None


def _objdiff_percent(diff):
    found = []
    for side in ("left", "right"):
        for section in (diff.get(side) or {}).get("sections") or []:
            if section.get("match_percent") is not None:
                found.append(float(section["match_percent"]))
    if not found:
        return None
    return round(sum(found) / len(found), 1)


def _mismatch_text(left_lines, right_lines):
    rows = []
    count = 0
    total = max(len(left_lines), len(right_lines))
    for index in range(total):
        left = left_lines[index] if index < len(left_lines) else ""
        right = right_lines[index] if index < len(right_lines) else ""
        if left == right:
            continue
        count += 1
        if count <= 20:
            if left:
                rows.append("retail  " + left)
            if right:
                rows.append("built   " + right)
            rows.append("")
    if not rows:
        return ""
    text = "\n".join(rows).rstrip()
    if count > 20:
        text += "\n\n%s differing instructions." % count
    return text


def _scores_from_diff(data):
    found = {}
    for sym in (data.get("right") or {}).get("symbols") or []:
        key = _fn_key(sym.get("name"))
        if not key or key in found or sym.get("match_percent") is None:
            continue
        found[key] = round(float(sym["match_percent"]), 1)
    if found:
        return found
    left = {}
    for sym in (data.get("left") or {}).get("symbols") or []:
        key = _fn_key(sym.get("name"))
        if key and sym.get("instructions"):
            left[key] = sym["instructions"]
    for sym in (data.get("right") or {}).get("symbols") or []:
        key = _fn_key(sym.get("name"))
        if not key or not sym.get("instructions") or key not in left:
            continue
        try:
            base = int(key[-8:], 16)
        except ValueError:
            continue
        percent = _instruction_percent(left[key], sym["instructions"], base)
        if percent is not None:
            found[key] = percent
    return found


def _match_percent(node, name):
    found = None
    want = _fn_key(name)

    def walk(item):
        nonlocal found
        if isinstance(item, dict):
            if want and _fn_key(item.get("name")) == want and _percent_of(item) is not None:
                found = _percent_of(item)
            for value in item.values():
                walk(value)
        elif isinstance(item, list):
            for value in item:
                walk(value)

    walk(node)
    return found


def bank_of(project, addr):
    for bank in project.banks():
        if bank["start"] <= addr < bank["end"]:
            return bank
    raise ValueError("address is not in a bank")
