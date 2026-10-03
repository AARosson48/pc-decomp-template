#!/usr/bin/env python3
"""Read and update one decomp project: banks, functions, source, and scores."""

import json
import os
import re
import subprocess
import threading
import time

import banks

FN_START = "// FN %08X\n"
FN_END = "// END %08X\n"


def _read(path):
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


class Project:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        self._blob = None
        self._text_va = None
        self._starts = None
        self._score_cache = None
        self._pass_lock = threading.Lock()
        self._pass = {"running": False}

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
            "dtk": data.get("dtk") or "",
            "objdiff": data.get("objdiff_cli") or "",
            "msvc": msvc,
            "cl": os.path.join(bin_dir, "cl.exe"),
            "dumpbin": os.path.join(bin_dir, "dumpbin.exe"),
            "binary_ninja": data.get("binary_ninja") or "",
        }

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

    def banks(self):
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
        if not found:
            return found
        self._load_text()
        scores = self.function_scores()
        starts = sorted(self._starts)
        for bank in found:
            bank.update(self._bank_summary(bank, starts, scores))
        return found

    def _bank_summary(self, bank, starts, scores):
        rows = [item for item in starts if bank["start"] <= item < bank["end"]]
        if not rows or rows[0] != bank["start"]:
            rows.insert(0, bank["start"])
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
        return {
            "functions": len(rows),
            "scored": len(values),
            "complete": complete,
            "match_percent": average,
        }

    def functions(self, bank_name):
        self._load_text()
        bank = next(item for item in self.banks() if item["name"] == bank_name)
        starts = [item for item in sorted(self._starts) if bank["start"] <= item < bank["end"]]
        if not starts or starts[0] != bank["start"]:
            starts.insert(0, bank["start"])
        scores = self.function_scores()
        overrides = self._function_ends()
        rows = []
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else bank["end"]
            forced = overrides.get("%08X" % start)
            if forced and start < forced <= end:
                end = forced
            name = "_fn_%08X" % start
            rows.append({
                "addr": start,
                "end": end,
                "size": end - start,
                "name": name,
                "match_percent": scores.get(name),
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

    def _ensure_symbol(self, addr, size):
        path = os.path.join(self.root, "config", "dtk_symbols.txt")
        text = _read(path)
        name = "_fn_%08X" % addr
        line = "%s = .text:0x%08X; // type:function size:0x%X\n" % (name, addr, size)
        pattern = re.compile(r"(?m)^%s = \.text:.*$" % name)
        if pattern.search(text):
            text = pattern.sub(line.rstrip("\n"), text, count=1)
        else:
            if text and not text.endswith("\n"):
                text += "\n"
            text += line
        _write(path, text)

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
        path = os.path.join(self.root, "build", "scores.json")
        if not os.path.isfile(path):
            self._score_cache = None
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle) or {}
        except (OSError, ValueError):
            data = {}
        if name in data:
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
            except RuntimeError as exc:
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
                error="",
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
        obj = os.path.join(self.root, "build", "src", bank_name + ".obj")
        if not objdiff or not os.path.isfile(objdiff):
            raise FileNotFoundError("objdiff was not found.")
        if not os.path.isfile(target):
            raise FileNotFoundError("Base object %s is missing." % target)
        diff_path = os.path.join(self.root, "build", "last-diff.json")
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
        resolved = _compiled_listing(self.root, addr)
        if resolved:
            return resolved
        obj = os.path.join(self.root, "build", "src", bank_name + ".obj")
        if not os.path.isfile(obj):
            return "Score this function to compile your C. Compiled assembly is the dumpbin listing of that object."
        tools = self.tools()
        dumpbin = tools["dumpbin"]
        if not os.path.isfile(dumpbin):
            return "dumpbin.exe was not found under %s" % tools["msvc"]
        env = os.environ.copy()
        msvc = tools["msvc"]
        env["PATH"] = os.path.join(msvc, "VC98", "Bin") + os.pathsep + os.path.join(msvc, "Common", "MSDev98", "Bin")
        env["INCLUDE"] = os.path.join(msvc, "VC98", "Include")
        proc = subprocess.run(
            [dumpbin, "/disasm", "/nologo", obj],
            capture_output=True,
            text=True,
            env=env,
        )
        if proc.returncode != 0:
            return proc.stderr or proc.stdout or "dumpbin failed"
        return _slice_dumpbin(proc.stdout, "_fn_%08X" % addr)

    def compile_bank(self, bank_name):
        tools = self.tools()
        if not os.path.isfile(tools["cl"]):
            raise FileNotFoundError("cl.exe was not found. tools.json msvc_root is %s" % tools["msvc"])
        src = self.cpp_path(bank_name)
        if not os.path.isfile(src):
            raise FileNotFoundError("no C++ file for %s" % bank_name)
        import draft_c
        draft_c.prepare_file(src)
        out = os.path.join(self.root, "build", "src", bank_name + ".obj")
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
        return out

    def resplit(self):
        tools = self.tools()
        dtk = tools["dtk"]
        if not dtk or not os.path.isfile(dtk):
            return "decomp-toolkit was not found, so the split objects were not refreshed."
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
        note = self.resplit()
        obj = self.compile_bank(bank_name)
        tools = self.tools()
        objdiff = tools["objdiff"]
        target = os.path.join(self.root, "build", "base", "obj", bank_name + ".o")
        if not objdiff or not os.path.isfile(objdiff):
            return {"note": note + " objdiff was not found.", "match_percent": None, "source_assembly": self.source_assembly(bank_name, addr)}
        if not os.path.isfile(target):
            return {"note": note + " Base object %s is missing." % target, "match_percent": None, "source_assembly": self.source_assembly(bank_name, addr)}
        diff_path = os.path.join(self.root, "build", "last-diff.json")
        proc = subprocess.run(
            [objdiff, "diff", "-1", target, "-2", obj, "-o", diff_path, "_fn_%08X" % addr],
            capture_output=True,
            text=True,
        )
        name = "_fn_%08X" % addr
        percent = None
        if os.path.isfile(diff_path):
            with open(diff_path, "r", encoding="utf-8") as handle:
                diff = json.load(handle)
            found = _scores_from_diff(diff)
            if found:
                self._remember_scores(found)
            percent = found.get(name)
            if percent is None:
                percent = _match_percent(diff, name)
        message = note
        if proc.returncode != 0:
            message += " " + (proc.stderr or proc.stdout or "objdiff failed")
        return {
            "note": message.strip(),
            "match_percent": percent,
            "source_assembly": self.source_assembly(bank_name, addr),
            "object": obj,
        }

    def build_report(self):
        banks = self.banks()
        units = []
        for bank in banks:
            name = bank["name"]
            target = "build/base/obj/" + name + ".o"
            compiled = "build/src/" + name + ".obj"
            base = compiled if os.path.isfile(os.path.join(self.root, compiled.replace("/", os.sep))) else None
            units.append({
                "name": name,
                "target_path": target,
                "base_path": base,
                "metadata": {"complete": False, "progress_categories": ["bank"]},
            })
        payload = {
            "custom_make": "ninja",
            "build_target": False,
            "build_base": False,
            "progress_categories": [{"id": "bank", "name": "Banks"}],
            "units": units,
        }
        _write(os.path.join(self.root, "objdiff.json"), json.dumps(payload, indent=2) + "\n")
        tools = self.tools()
        objdiff = tools["objdiff"]
        report = os.path.join(self.root, "build", "report.json")
        os.makedirs(os.path.dirname(report), exist_ok=True)
        if not objdiff or not os.path.isfile(objdiff):
            _write(report, json.dumps({"units": len(units), "note": "objdiff was not found"}, indent=2) + "\n")
            return {"report": report, "note": "Wrote objdiff.json. objdiff was not found."}
        proc = subprocess.run(
            [objdiff, "report", "generate", "-p", self.root, "-o", report, "-f", "json"],
            capture_output=True,
            text=True,
        )
        note = (proc.stdout or proc.stderr or "").strip()
        if proc.returncode != 0:
            raise RuntimeError(note or "objdiff report failed")
        summary = {"report": report, "note": note or "Wrote build/report.json", "units": len(units)}
        if os.path.isfile(report):
            try:
                with open(report, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                measures = data.get("measures") or {}
                summary["matched_code_percent"] = measures.get("matched_code_percent")
                summary["fuzzy_match_percent"] = measures.get("fuzzy_match_percent")
                summary["total_functions"] = measures.get("total_functions")
                summary["matched_functions"] = measures.get("matched_functions")
                found = _report_scores(data)
                found.update(self._diff_unit_scores(units))
                self._remember_scores(found)
                summary["scored_functions"] = len(found)
                summary["complete_functions"] = sum(1 for value in found.values() if value is not None and float(value) >= 100)
            except (OSError, ValueError):
                self._score_cache = None
        return summary

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
            diff_path = os.path.join(self.root, "build", "last-diff.json")
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

    def function_scores(self):
        if self._score_cache is not None:
            return self._score_cache
        found = {}
        report = os.path.join(self.root, "build", "report.json")
        if os.path.isfile(report):
            try:
                with open(report, "r", encoding="utf-8") as handle:
                    found.update(_report_scores(json.load(handle)))
            except (OSError, ValueError):
                pass
        saved = os.path.join(self.root, "build", "scores.json")
        if os.path.isfile(saved):
            try:
                with open(saved, "r", encoding="utf-8") as handle:
                    for name, percent in (json.load(handle) or {}).items():
                        key = _fn_key(name)
                        if key and percent is not None:
                            found[key] = percent
            except (OSError, ValueError):
                pass
        self._score_cache = found
        return found

    def _remember_score(self, name, percent):
        self._remember_scores({name: percent})

    def _remember_scores(self, updates):
        if not updates:
            self._score_cache = None
            return
        path = os.path.join(self.root, "build", "scores.json")
        current = {}
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    current = json.load(handle) or {}
            except (OSError, ValueError):
                current = {}
        for name, percent in updates.items():
            key = _fn_key(name)
            if key and percent is not None:
                current[key] = percent
        _write(path, json.dumps(current, indent=2, sort_keys=True) + "\n")
        self._score_cache = None


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
    text = str(name or "").strip()
    if text.startswith("@"):
        text = text[1:]
    text = text.split("@", 1)[0].strip().strip("_")
    if not text.lower().startswith("fn_"):
        return None
    return "_fn_" + text[3:].upper()


def _percent_of(item):
    if item.get("fuzzy_match_percent") is not None:
        return item.get("fuzzy_match_percent")
    return item.get("match_percent")


def _report_scores(data):
    found = {}
    for unit in data.get("units") or []:
        for fn in unit.get("functions") or []:
            key = _fn_key(fn.get("name"))
            percent = _percent_of(fn)
            if key and percent is not None:
                found[key] = percent
    return found


def _compiled_listing(project_root, addr):
    path = os.path.join(project_root, "build", "last-diff.json")
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


def _scores_from_diff(data):
    left = {}
    for sym in (data.get("left") or {}).get("symbols") or []:
        key = _fn_key(sym.get("name"))
        if key and sym.get("instructions"):
            left[key] = sym["instructions"]
    found = {}
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
