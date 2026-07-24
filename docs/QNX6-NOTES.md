# QNX6 on Harman head units — what we know

Field notes from reverse-engineering the on-disk layout of a Porsche PCM 3.1 drive
(QNX 6.3.2, Harman Becker, SH4). Recorded because the public documentation does not
fully describe this variant, and the differences are the whole reason a normal QNX6
reader fails on these drives.

Everything below was measured against a real 40 GB PCM 3.1 image unless noted.

## Partition layout

MBR, three QNX partitions (types `0x4d` / `0x4e` / `0x4f`):

| part | type | start LBA | size | contents |
|------|------|-----------|------|----------|
| P1 | 0x4d | 32 | ~2.0 GB | system — `/.boot`, `/log`, `/tools` |
| P2 | 0x4e | 4096000 | ~1.0 GB | applications — `/Browser`, `/HBdata`, `/MapStyles`, `/acios`, `/bootscreens`, `/mmi`, `/nobss`, `/qdb_backup`, `/xm` |
| P3 | 0x4f | 6144000 | ~36.9 GB | navigation data |

P3 has **no QNX6 superblock** at the usual offsets — either a different format or a
container. Not yet identified.

## Superblocks

Magic `0x68191122`, little-endian. The power-safe filesystem keeps **two** superblocks
and the live one is whichever has the **higher serial**:

- `base + 0x2000` (start + 8 KB)
- `base + length - 0x1000` (end − 4 KB)

Field offsets, verified:

| offset | field |
|--------|-------|
| 0x00 | magic |
| 0x08 | serial (u64) |
| 0x30 | blocksize |
| 0x34 | num_inodes |
| 0x38 | free_inodes |
| 0x3c | num_blocks |
| 0x40 | free_blocks |
| 0x44 | allocation groups |
| 0x50 | inode-file pointer |
| 0x90 | inode-file levels |

Measured geometry — note the two partitions differ, so don't hardcode either:

```
P1:  blocksize=1024  inodes 61/64000    blocks 2047968  groups=8
P2:  blocksize=1024  inodes 376/128000  blocks 1023984  groups=4
```

Block `B` → byte offset `partition_base + B * blocksize`.

## Directory blocks — the reliable way in

Directory entries are **32 bytes**: `u32 inode`, `u8 namelen`, `name[]`.

Crucially, every directory block is **self-identifying**: entry 0 is `.` and holds the
directory's own inode number; entry 1 is `..` and holds its parent's. So the whole
hierarchy can be rebuilt by scanning the raw partition for that signature — no
superblock, no inode file, no allocation-group math required.

This is what PCM Explorer does, and it's why it still works on damaged drives. It is
strictly more robust than following the documented metadata chain, which on this build
is a dead end (below).

## The unsolved part: inode number → inode struct

Inode structs are 128 bytes:

| offset | field |
|--------|-------|
| 0x00 | size (u64) |
| 0x08 | uid |
| 0x0c | gid |
| 0x14 | mtime |
| 0x20 | mode (u16) |
| 0x24 | block_ptr[16] (u32 each, `0xffffffff` = nil) |
| 0x64 | filelevels |
| 0x65 | status |

QNX6 does **not** store inodes in inode-number order — they're distributed across the
allocation groups, and the mapping is supposed to live in the superblock's "inode file"
(a tree, `levels=2`).

**On this build that chain is a dead end.** Both superblocks' inode-file indirect
pointers read as empty/holes, and nothing in the first 16 MB points at the inode blocks
that *are* populated (physical blocks `0x4008`–`0x4020`). So the public layout does not
describe this variant.

### The strongest lead

Recovered inode numbers land on clean allocation-group boundaries:

- **P2** (`num_inodes=128000`, 4 groups → 32000 per group): root/`.boot`/`.placeholder`
  at `1, 2, 7` (group 0), then `64004, 64005` (group 2), then `96001–96087` (group 3).
- **P1** (`num_inodes=64000`, 8 groups → 8000 per group): `/log` and `/tools` at
  `56001, 56003` (group 7).

Two partitions with different group counts both landing exactly on `num_inodes / groups`
boundaries is strong evidence the scheme is simple group-blocking —
`group = inode / (num_inodes / groups)` — rather than round-robin interleaving.

### Ways to finish it

1. **Empirical correlation.** Directories are self-identifying, so for any candidate
   inode struct you can follow `block_ptr[0]`, read that block, and if it's a directory
   block the `.` entry tells you the struct's *true* inode number. That yields
   ground-truth pairs across the whole disk, from which the permutation can be fitted
   and then tested on held-out pairs. Most promising, needs no external information.
2. **Reverse the QNX tools.** `mkqnx6fs` and `chkqnx6fs` ship on the unit itself
   (`/mnt/data/tools`) and contain the real block-map logic.
3. **Check for a second metadata copy.** A power-safe filesystem keeps two superblocks;
   the inode file may be double-buffered too, and the live copy may simply not be the
   one that was followed.

## Reading a drive without any of this

If the unit still powers on, it can read its own disk. Enabling networking and using
the on-unit shell sidesteps the filesystem question entirely — see
[PCM-Forge](https://github.com/dspl1236/PCM-Forge). Offline reading matters when the
unit is dead, which is exactly when you need it most.
