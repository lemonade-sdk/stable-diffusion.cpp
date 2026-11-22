#!/usr/bin/env python3
"""
Read-only inspection of compiled binaries: which gfx targets a HIP fat binary
carries, which libraries a binary needs, and which glibc it requires.

Standard library only, so it runs on any runner without a GPU toolchain.
"""

import os
import re
import struct

# Offload bundle entry ids look like "hipv4-amdgcn-amd-amdhsa--gfx1151". The
# GPU code section is ".hip_fatbin" on ELF but ".hip_fat" on PE (8-char section
# names), so the bytes are scanned instead of looking the section up by name.
BUNDLE_ENTRY_RE = re.compile(rb"[a-z0-9_]+-amdgcn-amd-amdhsa-[-0-9a-zA-Z_.:+]*")
BUNDLE_MAGIC = b"__CLANG_OFFLOAD_BUNDLE__"

_GFX_RE = re.compile(r"(gfx[0-9a-fA-F]+)")
_CHUNK = 64 << 20
_OVERLAP = 256


def strip_target_id(entry_id):
    """'hip-amdgcn-amd-amdhsa--gfx90a:xnack+' -> 'gfx90a'."""
    match = _GFX_RE.search(entry_id.split(":", 1)[0])
    return match.group(1) if match else None


def scan_offload_bundles(path):
    """Return (count per bundle entry id, bundle groups found, bytes read)."""
    counts = {}
    magic = 0
    total = 0

    def tally(window, limit):
        # Only matches starting before `limit`; the rest is re-read with the next chunk.
        nonlocal magic
        for match in BUNDLE_ENTRY_RE.finditer(window):
            if match.start() >= limit:
                break
            entry = match.group(0).decode("ascii", "replace")
            counts[entry] = counts.get(entry, 0) + 1
        start = 0
        while True:
            found = window.find(BUNDLE_MAGIC, start)
            if found < 0 or found >= limit:
                break
            magic += 1
            start = found + 1

    tail = b""
    with open(path, "rb") as handle:
        while True:
            buf = handle.read(_CHUNK)
            if not buf:
                tally(tail, len(tail))
                break
            total += len(buf)
            window = tail + buf
            safe = max(len(window) - _OVERLAP, 0)
            tally(window, safe)
            tail = window[safe:]
    return counts, magic, total


def audit_gfx_targets(path, expected):
    """Compare the gfx targets inside a fat binary against the requested list.

    The compiler emits one entry per target per translation unit, so a healthy
    build has the same count for every target.
    """
    counts, magic, total = scan_offload_bundles(path)
    per_target = {}
    for entry, n in counts.items():
        target = strip_target_id(entry)
        if target:
            per_target[target] = per_target.get(target, 0) + n

    expected_set = {strip_target_id(t) or t for t in expected}
    present = set(per_target)
    return {
        "binary": os.path.basename(path),
        "bytes_scanned": total,
        "bundle_magic_count": magic,
        "expected_count": len(expected_set),
        "present_count": len(present),
        "expected": sorted(expected_set),
        "present": sorted(present),
        "missing": sorted(expected_set - present),
        "extra": sorted(present - expected_set),
        "entries_per_target": dict(sorted(per_target.items())),
        "entry_count_uniform": len(set(per_target.values())) <= 1,
    }


def is_elf(path):
    try:
        with open(path, "rb") as handle:
            return handle.read(4) == b"\x7fELF"
    except OSError:
        return False


def elf_sections(path):
    with open(path, "rb") as handle:
        header = handle.read(64)
        if header[:4] != b"\x7fELF" or header[4] != 2:
            raise ValueError(f"{path}: not ELF64")
        e_shoff, = struct.unpack_from("<Q", header, 0x28)
        e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", header, 0x3A)
        if e_shnum == 0 or e_shoff == 0:
            return []

        handle.seek(e_shoff)
        raw = handle.read(e_shentsize * e_shnum)
        sections = []
        for i in range(e_shnum):
            off = i * e_shentsize
            name_off, sh_type, _flags, _addr, sh_offset, sh_size = struct.unpack_from(
                "<IIQQQQ", raw, off)
            sections.append({"name_off": name_off, "type": sh_type,
                             "offset": sh_offset, "size": sh_size})

        shstr = sections[e_shstrndx]
        handle.seek(shstr["offset"])
        strtab = handle.read(shstr["size"])
        for section in sections:
            end = strtab.find(b"\0", section["name_off"])
            section["name"] = strtab[section["name_off"]:end].decode("ascii", "replace")
        return sections


def _section(sections, name):
    return next((s for s in sections if s["name"] == name), None)


def elf_dynamic(path, sections=None):
    """Return (DT_NEEDED names, DT_RPATH/DT_RUNPATH strings)."""
    sections = sections if sections is not None else elf_sections(path)
    dynamic = _section(sections, ".dynamic")
    dynstr = _section(sections, ".dynstr")
    if not dynamic or not dynstr:
        return [], []

    with open(path, "rb") as handle:
        handle.seek(dynstr["offset"])
        strs = handle.read(dynstr["size"])
        handle.seek(dynamic["offset"])
        blob = handle.read(dynamic["size"])

    needed, runpath = [], []
    for off in range(0, len(blob) - 15, 16):
        tag, val = struct.unpack_from("<qQ", blob, off)
        if tag == 0:
            break
        if tag in (1, 15, 29):
            end = strs.find(b"\0", val)
            if end < 0:
                continue
            text = strs[val:end].decode("ascii", "replace")
            (needed if tag == 1 else runpath).append(text)
    return needed, runpath


_GLIBC_RE = re.compile(rb"GLIBC_(\d+\.\d+(?:\.\d+)?)")


def elf_glibc_floor(path, sections=None):
    """Newest GLIBC_x.y symbol version the binary needs, e.g. (2, 38), or None."""
    sections = sections if sections is not None else elf_sections(path)
    dynstr = _section(sections, ".dynstr")
    if not dynstr:
        return None
    with open(path, "rb") as handle:
        handle.seek(dynstr["offset"])
        blob = handle.read(dynstr["size"])
    versions = {tuple(int(p) for p in m.decode().split("."))
                for m in _GLIBC_RE.findall(blob)}
    return max(versions) if versions else None


def is_pe(path):
    try:
        with open(path, "rb") as handle:
            if handle.read(2) != b"MZ":
                return False
            handle.seek(0x3C)
            pe_off, = struct.unpack("<I", handle.read(4))
            handle.seek(pe_off)
            return handle.read(4) == b"PE\0\0"
    except (OSError, struct.error):
        return False


def pe_imports(path):
    """DLL names from the import table; [] if the file cannot be parsed."""
    with open(path, "rb") as handle:
        data = handle.read()

    try:
        pe_off, = struct.unpack_from("<I", data, 0x3C)
        nsections, = struct.unpack_from("<H", data, pe_off + 6)
        opt_size, = struct.unpack_from("<H", data, pe_off + 20)
        opt_off = pe_off + 24
        magic, = struct.unpack_from("<H", data, opt_off)
        dd_off = opt_off + (112 if magic == 0x20B else 96)
        import_rva, import_size = struct.unpack_from("<II", data, dd_off + 8)
        if not import_rva or not import_size:
            return []

        sections = []
        sec_off = opt_off + opt_size
        for i in range(nsections):
            raw = data[sec_off + i * 40: sec_off + i * 40 + 40]
            if len(raw) < 40:
                break
            vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", raw, 8)
            sections.append((vaddr, max(vsize, rawsize), rawptr, rawsize))

        def rva_to_off(rva):
            for vaddr, vsize, rawptr, rawsize in sections:
                if vaddr <= rva < vaddr + vsize:
                    delta = rva - vaddr
                    return rawptr + delta if delta < rawsize else None
            return None

        names = []
        table = rva_to_off(import_rva)
        if table is None:
            return []
        while True:
            entry = data[table: table + 20]
            if len(entry) < 20 or entry == b"\0" * 20:
                break
            name_rva, = struct.unpack_from("<I", entry, 12)
            name_off = rva_to_off(name_rva)
            if name_off is not None:
                end = data.find(b"\0", name_off)
                names.append(data[name_off:end].decode("ascii", "replace"))
            table += 20
        return names
    except (struct.error, IndexError, ValueError):
        return []
