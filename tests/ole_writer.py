"""A minimal OLE2 / Compound File writer, for building ``.msg`` test fixtures.

``.msg`` is an OLE2 compound file, so testing the reader means having a real
compound file to read. ``olefile`` only reads, and no writer is on PyPI, so the
few sectors an Outlook message needs are laid out here directly.

Only what a message needs is implemented: a directory tree of storages and
streams, the mini stream for anything under the 4096 byte cutoff, and one FAT.
There is no DIFAT (never enough sectors to need one) and no transaction log.

Reference: MS-CFB, "Compound File Binary File Format".
"""
from __future__ import annotations

import math
import struct
from typing import Iterable, Optional

SECTOR = 512
MINI = 64
CUTOFF = 4096
EOC = 0xFFFFFFFE
FREESECT = 0xFFFFFFFF
FATSECT = 0xFFFFFFFD

SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_DIR_ENTRY = 128


class _Node:
    """One directory entry."""

    def __init__(self, name: str, kind: str):
        self.name = name
        self.kind = kind                  # "root" | "storage" | "stream"
        self.data = b""
        self.children: dict[str, _Node] = {}
        self.index = -1
        self.start = EOC
        self.size = 0
        self.left = -1
        self.right = -1
        self.child = -1

    @property
    def obj_type(self) -> int:
        return {"root": 5, "storage": 1, "stream": 2}[self.kind]


def _sort_key(name: str) -> tuple[int, str]:
    """CFB name order: shorter names first, then uppercased."""
    return (len(name.encode("utf-16-le")), name.upper())


def _dir_bytes(node: _Node) -> bytes:
    name = node.name.encode("utf-16-le") + b"\x00\x00"
    if len(name) > 64:
        raise ValueError(f"directory name too long: {node.name!r}")
    out = bytearray(_DIR_ENTRY)
    out[0:len(name)] = name
    struct.pack_into("<H", out, 64, len(name))
    out[66] = node.obj_type
    out[67] = 1                                    # colour: black
    struct.pack_into("<iii", out, 68, node.left, node.right, node.child)
    # 80:16 clsid, 96:4 state, 100:8 created, 108:8 modified all stay zero.
    struct.pack_into("<I", out, 116, node.start)
    struct.pack_into("<Q", out, 120, node.size)
    return bytes(out)


def _pack_fat(entries: list[int], sectors: int) -> bytes:
    out = bytearray(sectors * SECTOR)
    for i, value in enumerate(entries):
        struct.pack_into("<I", out, i * 4, value)
    return bytes(out)


def build_ole(streams: dict[tuple[str, ...], bytes]) -> bytes:
    """Build a compound file holding ``streams``.

    Keys are paths: ``("__substg1.0_0037001F",)`` is a stream at the root and
    ``("attach", "__substg1.0_37010102")`` is one inside a storage.
    """
    root = _Node("Root Entry", "root")
    for path, data in streams.items():
        if not path:
            raise ValueError("a stream needs a name")
        node = root
        for component in path[:-1]:
            node = node.children.setdefault(component, _Node(component, "storage"))
        leaf = node.children.setdefault(path[-1], _Node(path[-1], "stream"))
        leaf.data = data

    order: list[_Node] = []

    def visit(node: _Node) -> None:
        order.append(node)
        for child in node.children.values():
            visit(child)

    visit(root)
    for i, node in enumerate(order):
        node.index = i

    def wire(parent: _Node) -> Optional[_Node]:
        """Turn a parent's children into a balanced search tree."""
        items = sorted(parent.children.values(), key=lambda n: _sort_key(n.name))
        if not items:
            return None
        mid = len(items) // 2
        node = items[mid]
        left = wire_from(items[:mid])
        right = wire_from(items[mid + 1:])
        node.left = left.index if left else -1
        node.right = right.index if right else -1
        return node

    def wire_from(items: list[_Node]) -> Optional[_Node]:
        if not items:
            return None
        mid = len(items) // 2
        node = items[mid]
        left = wire_from(items[:mid])
        right = wire_from(items[mid + 1:])
        node.left = left.index if left else -1
        node.right = right.index if right else -1
        return node

    for node in order:
        child = wire(node)
        node.child = child.index if child else -1

    # ---------------------------------------------------------------- streams
    mini_nodes = [n for n in order
                  if n.kind == "stream" and 0 < len(n.data) < CUTOFF]
    regular_nodes = [n for n in order
                     if n.kind == "stream" and len(n.data) >= CUTOFF]

    mini_data = bytearray()
    mini_fat: list[int] = []
    for node in mini_nodes:
        node.size = len(node.data)
        count = max(1, math.ceil(len(node.data) / MINI))
        node.start = len(mini_fat)
        for i in range(count):
            mini_fat.append(node.start + i + 1 if i < count - 1 else EOC)
        mini_data += node.data
        mini_data += b"\x00" * (count * MINI - len(node.data))
    if mini_data:
        mini_data += b"\x00" * (-len(mini_data) % SECTOR)

    mini_sectors = len(mini_data) // SECTOR
    minifat_sectors = math.ceil(len(mini_fat) * 4 / SECTOR) if mini_fat else 0
    dir_sectors = math.ceil(len(order) * _DIR_ENTRY / SECTOR)
    regular_counts = [math.ceil(len(n.data) / SECTOR) for n in regular_nodes]

    # ----------------------------------------------------------------- layout
    fat_sectors = 1
    while True:
        total = fat_sectors + dir_sectors + mini_sectors + minifat_sectors \
            + sum(regular_counts)
        if math.ceil(total * 4 / SECTOR) <= fat_sectors:
            break
        fat_sectors += 1

    cursor = fat_sectors
    dir_start = cursor
    cursor += dir_sectors
    mini_start = cursor if mini_sectors else EOC
    cursor += mini_sectors
    minifat_start = cursor if minifat_sectors else EOC
    cursor += minifat_sectors
    for node, count in zip(regular_nodes, regular_counts):
        node.start = cursor
        node.size = len(node.data)
        cursor += count
    total_sectors = cursor

    fat = [FREESECT] * (fat_sectors * SECTOR // 4)
    for i in range(fat_sectors):
        fat[i] = FATSECT
    for i in range(dir_sectors):
        first = dir_start + i
        fat[first] = first + 1 if i < dir_sectors - 1 else EOC
    if mini_sectors:
        for i in range(mini_sectors):
            at = mini_start + i
            fat[at] = at + 1 if i < mini_sectors - 1 else EOC
    if minifat_sectors:
        for i in range(minifat_sectors):
            at = minifat_start + i
            fat[at] = at + 1 if i < minifat_sectors - 1 else EOC
    for node, count in zip(regular_nodes, regular_counts):
        for i in range(count):
            at = node.start + i
            fat[at] = at + 1 if i < count - 1 else EOC
    # Entries past the last sector describe nothing, so they stay free.
    for i in range(total_sectors, len(fat)):
        fat[i] = FREESECT

    root.start = mini_start if mini_sectors else EOC
    root.size = len(mini_data)

    # ------------------------------------------------------------------ bytes
    sectors: list[Optional[bytes]] = [None] * total_sectors
    for i in range(fat_sectors):
        sectors[i] = _pack_fat(fat[i * 128:(i + 1) * 128], 1)
    directory = bytearray()
    for node in order:
        directory += _dir_bytes(node)
    directory += b"\x00" * (-len(directory) % SECTOR)
    for i in range(dir_sectors):
        sectors[dir_start + i] = bytes(directory[i * SECTOR:(i + 1) * SECTOR])
    if mini_sectors:
        for i in range(mini_sectors):
            sectors[mini_start + i] = bytes(mini_data[i * SECTOR:(i + 1) * SECTOR])
    if minifat_sectors:
        minifat = bytearray(minifat_sectors * SECTOR)
        for i, value in enumerate(mini_fat):
            struct.pack_into("<I", minifat, i * 4, value)
        for i in range(minifat_sectors):
            sectors[minifat_start + i] = bytes(minifat[i * SECTOR:(i + 1) * SECTOR])
    for node, count in zip(regular_nodes, regular_counts):
        blob = node.data + b"\x00" * (count * SECTOR - len(node.data))
        for i in range(count):
            sectors[node.start + i] = blob[i * SECTOR:(i + 1) * SECTOR]

    header = bytearray(SECTOR)
    header[0:8] = SIGNATURE
    struct.pack_into("<H", header, 24, 0x003E)          # minor version
    struct.pack_into("<H", header, 26, 0x0003)          # major version 3
    struct.pack_into("<H", header, 28, 0xFFFE)          # little endian
    struct.pack_into("<H", header, 30, 9)               # 512 byte sectors
    struct.pack_into("<H", header, 32, 6)               # 64 byte mini sectors
    struct.pack_into("<I", header, 40, 0)               # v3 has no dir count
    struct.pack_into("<I", header, 44, fat_sectors)
    struct.pack_into("<I", header, 48, dir_start)
    struct.pack_into("<I", header, 52, 0)               # transaction signature
    struct.pack_into("<I", header, 56, CUTOFF)
    struct.pack_into("<I", header, 60, minifat_start)
    struct.pack_into("<I", header, 64, minifat_sectors)
    struct.pack_into("<I", header, 68, EOC)             # no DIFAT sector
    struct.pack_into("<I", header, 72, 0)               # no DIFAT count
    difat = [i if i < fat_sectors else FREESECT for i in range(109)]
    for i, value in enumerate(difat):
        struct.pack_into("<I", header, 76 + i * 4, value)

    body = b"".join(sectors[i] if sectors[i] is not None else b"\x00" * SECTOR
                    for i in range(total_sectors))
    return bytes(header) + body


# --------------------------------------------------------------- msg builder
def build_msg(*, subject: str = "", sender: str = "", to: str = "", body: str = "",
              attachments: Iterable[tuple[str, bytes]] = (),
              sender_address: str = "") -> bytes:
    """Build an OLE2 file shaped like an Outlook ``.msg``.

    Only the property streams the reader looks at are written, which is what
    makes this a fair test: if the reader depends on anything else, it fails.
    """
    streams: dict[tuple[str, ...], bytes] = {
        ("__substg1.0_0037001F",): subject.encode("utf-16-le"),
        ("__substg1.0_0C1F001F",): sender.encode("utf-16-le"),
        ("__substg1.0_0E04001F",): to.encode("utf-16-le"),
        ("__substg1.0_1000001F",): body.encode("utf-16-le"),
    }
    if sender_address:
        streams[("__substg1.0_5D01001F",)] = sender_address.encode("utf-16-le")

    for i, (name, payload) in enumerate(attachments):
        root = f"__attach_version1.0_#{i:08d}"
        # The long filename is the unicode property (001F); the payload is binary
        # (0102). Upper-case hex and 001F for text is what Outlook itself writes,
        # so the fixture reads the way a real message does.
        streams[(root, "__substg1.0_3707001F")] = name.encode("utf-16-le")
        streams[(root, "__substg1.0_37010102")] = payload
    return build_ole(streams)
