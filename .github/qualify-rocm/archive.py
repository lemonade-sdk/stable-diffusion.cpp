#!/usr/bin/env python3
"""
Unpack a published ROCm archive the way a user's unzip would.

Both Tier 0 and Tier 1 need the archive's files on disk, and the two operating
systems pack them differently: the Windows zip is flat, while the Linux zip
holds build/bin/ with its versioned library names stored as symlinks. Rather
than encode either layout, extract() returns whichever folder sd-server landed
in -- the same thing Lemonade does when it searches the extracted folder for
sd-server.

Every entry is checked to stay inside the destination, so a damaged or hostile
archive cannot write anywhere else.
"""

import os
import stat
import zipfile

SERVER = {"ubuntu": "sd-server", "win": "sd-server.exe"}
CLI = {"ubuntu": "sd-cli", "win": "sd-cli.exe"}


def extract(archive, dest):
    """Extract `archive` into `dest` and return the folder holding sd-server.

    The Linux zip is made with `zip -y`, so its versioned .so names are stored as
    symlinks. zipfile.extractall would write them out as small text files the
    loader cannot use, so they are recreated as symlinks here.
    """
    if not archive.endswith(".zip"):
        raise ValueError(f"unsupported archive type: {archive}")
    dest = os.path.abspath(dest)
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            target = os.path.abspath(os.path.join(dest, info.filename))
            if os.path.commonpath([dest, target]) != dest:
                raise ValueError(f"{archive}: entry escapes the archive: {info.filename}")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode) and os.name != "nt":
                link = zf.read(info).decode("utf-8")
                if os.path.isabs(link) or ".." in link.split("/"):
                    raise ValueError(f"{archive}: symlink leaves its folder: {info.filename} -> {link}")
                os.makedirs(os.path.dirname(target), exist_ok=True)
                if os.path.lexists(target):
                    os.remove(target)
                os.symlink(link, target)
                continue
            zf.extract(info, dest)
            if mode and not info.is_dir() and os.name != "nt":
                os.chmod(target, stat.S_IMODE(mode) | stat.S_IRUSR)

    for dirpath, _dirs, files in os.walk(dest):
        if set(SERVER.values()) & set(files):
            return dirpath
    raise ValueError(f"sd-server not found anywhere inside {archive}")
