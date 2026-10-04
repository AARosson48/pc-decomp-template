#!/usr/bin/env python3
"""Cut a PE .text section into bank units of about 16KB.

Cuts land on 4-aligned function starts: the image entry, call targets, and
bytes that follow MSVC int 3 padding. A jump stays in the function above it.
An address that lands inside an instruction stays in that function too. A
0xCC byte in a displacement or ModRM byte is not padding.
This is the first split, before any original .cpp path is known.
"""

import os
import struct
import sys

BANK = 0x4000


def _u16(data, offset):
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


def _i32(data, offset):
    return struct.unpack_from("<i", data, offset)[0]


def pe_sections(path):
    with open(path, "rb") as handle:
        data = handle.read()
    if data[:2] != b"MZ":
        raise ValueError("not a PE executable")
    lfanew = _u32(data, 0x3C)
    if data[lfanew:lfanew + 4] != b"PE\0\0":
        raise ValueError("not a PE executable")
    coff = lfanew + 4
    nsections = _u16(data, coff + 2)
    optsize = _u16(data, coff + 16)
    opt = coff + 20
    magic = _u16(data, opt)
    if magic != 0x10B:
        raise ValueError("only PE32 is supported")
    entry = _u32(data, opt + 16)
    image_base = _u32(data, opt + 28)
    section_off = opt + optsize
    sections = []
    for index in range(nsections):
        off = section_off + index * 40
        name = data[off:off + 8].split(b"\0", 1)[0].decode("ascii", "replace")
        vsize, va, rawsize, rawptr = struct.unpack_from("<IIII", data, off + 8)
        flags = _u32(data, off + 36)
        sections.append({
            "name": name,
            "vsize": vsize,
            "va": va,
            "rawsize": rawsize,
            "rawptr": rawptr,
            "flags": flags,
        })
    return data, image_base, image_base + entry, sections


def _text_section(sections):
    for section in sections:
        if section["name"] == ".text":
            return section
    for section in sections:
        if section["flags"] & 0x20:
            return section
    raise ValueError("no .text section")


def function_starts(blob, text_va):
    starts = set()
    limit = len(blob)
    index = 0
    while index + 5 <= limit:
        opcode = blob[index]
        if opcode in (0xE8, 0xE9):
            # E8 calls a function. E9 jumps inside the function that contains it.
            if opcode == 0xE8:
                target = text_va + index + 5 + _i32(blob, index + 1)
                if text_va <= target < text_va + limit and target % 4 == 0:
                    starts.add(target)
            index += 5
            continue
        index += 1
    index = 0
    while index < limit:
        if blob[index] != 0xCC:
            index += 1
            continue
        end = index
        while end < limit and blob[end] == 0xCC:
            end += 1
        start = text_va + end
        if end > index and end < limit and start % 4 == 0:
            starts.add(start)
        index = max(end, index + 1)
    # An unconditional jump is the end of the function above it, not a new one.
    starts = {
        addr for addr in starts
        if not _starts_with_jump(blob, text_va, addr)
    }
    return _drop_interior_starts(blob, text_va, starts)


def _drop_interior_starts(blob, text_va, starts):
    """A start inside an instruction of the function above it is not a function."""
    try:
        from capstone import CS_ARCH_X86, CS_MODE_32, Cs
    except ImportError:
        return starts
    ordered = sorted(starts)
    if len(ordered) < 2:
        return starts
    decoder = Cs(CS_ARCH_X86, CS_MODE_32)
    kept = [ordered[0]]
    limit = len(blob)
    for addr in ordered[1:]:
        prev = kept[-1]
        off = prev - text_va
        stop = addr - text_va
        if off < 0 or stop > limit:
            kept.append(addr)
            continue
        window = blob[off:min(limit, stop + 15)]
        interior = False
        for insn in decoder.disasm(window, prev):
            if insn.address >= addr:
                break
            if insn.address < addr < insn.address + insn.size:
                interior = True
                break
        if not interior:
            kept.append(addr)
    return set(kept)


def _starts_with_jump(blob, text_va, addr):
    index = addr - text_va
    return 0 <= index < len(blob) and blob[index] in (0xE9, 0xEB)


def bank_ranges(text_va, text_end, starts):
    points = [text_va]
    points.extend(sorted(start for start in starts if text_va < start < text_end))
    cuts = [text_va]
    for start in points[1:]:
        if start - cuts[-1] >= BANK:
            cuts.append(start)
    if cuts[-1] != text_end:
        cuts.append(text_end)
    if len(cuts) == 2 and text_end - text_va > BANK:
        cuts = list(range(text_va, text_end, BANK))
        if cuts[-1] != text_end:
            cuts.append(text_end)
    return list(zip(cuts, cuts[1:]))


def section_lines(sections):
    kinds = {
        ".text": "code",
        ".rdata": "rodata",
        ".data": "data",
        ".rsrc": "rodata",
    }
    lines = ["Sections:"]
    for section in sections:
        kind = kinds.get(section["name"], "data")
        lines.append("\t%s       type:%s align:4096" % (section["name"], kind))
    lines.append("")
    return lines


def write_splits(exe_path, splits_path):
    _data, image_base, entry, sections = pe_sections(exe_path)
    text = _text_section(sections)
    raw_end = min(text["rawptr"] + text["rawsize"], len(_data))
    blob = _data[text["rawptr"]:raw_end]
    span = text["vsize"] or text["rawsize"]
    text_va = image_base + text["va"]
    text_end = text_va + span
    if len(blob) < span:
        text_end = text_va + len(blob)
        blob = blob[:text_end - text_va]
    starts = function_starts(blob, text_va)
    if entry % 4 == 0 and text_va <= entry < text_end:
        starts.add(entry)
    ranges = bank_ranges(text_va, text_end, starts)
    lines = section_lines(sections)
    for start, end in ranges:
        lines.append("bank/%08X:" % start)
        lines.append("\t.text       start:0x%08X end:0x%08X" % (start, end))
        lines.append("")
    os.makedirs(os.path.dirname(splits_path), exist_ok=True)
    with open(splits_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines).rstrip() + "\n")
    return len(ranges)


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: banks.py <exe> <splits.txt>")
    count = write_splits(sys.argv[1], sys.argv[2])
    print("wrote %s banks" % count)


if __name__ == "__main__":
    main()
