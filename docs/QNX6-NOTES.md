# QNX6 on Harman head units — on-disk layout

Reverse-engineered from a Porsche PCM 3.1 drive (QNX 6.3.2, Harman Becker, SH4) and
verified against it. Written down because the public documentation doesn't describe
where these images actually put things, and one wrong constant makes the whole
filesystem look proprietary when it isn't.

Everything here is measured, not inferred.

## The block origin — the one thing that matters

Every block number stored anywhere on the filesystem — superblock root-node pointers,
indirect-block entries, inode block pointers — is relative to the **start of the data
area**, not the start of the partition:

```
image_offset(block B) = partition_base + 0x3000 + B * blocksize
```

```
0x0000 - 0x1FFF   boot block        (8 blocks)
0x2000 - 0x2FFF   superblock area   (4 blocks; the struct itself at 0x2000)
0x3000            data block 0
```

Get this wrong and every indirect chain lands 12 KiB early, in the middle of unrelated
file data, and reads as though it were full of holes. That single mistake is what made
this filesystem look like an undocumented Harman variant. **It isn't one** — once the
origin is right, the layout matches the public QNX6 one exactly.

Sanity check available at mount time, true on both partitions:

```
(partition_length / blocksize) - num_blocks == 16      # 12 front + 4 tail superblock
```

## Partition layout

| part | type | start LBA | size | filesystem | contents |
|------|------|-----------|------|------------|----------|
| P1 | 0x4d | 32 | ~2.0 GB | QNX6 | `/.boot`, `/log` (watchdog dumps), `/tools` |
| P2 | 0x4e | 4096000 | ~1.0 GB | QNX6 | `/Browser`, `/HBdata`, `/MapStyles`, `/acios`, `/bootscreens`, `/mmi`, `/nobss`, `/qdb_backup`, `/xm` |
| P3 | 0x4f | 6144000 | ~36.9 GB | **QNX4** | navigation data — different filesystem, not covered here |

## Superblock

Magic `0x68191122` little-endian. The power-safe filesystem keeps **two** copies:

- `base + 0x2000` (front)
- `base + length - 0x1000` (tail)

**Always pick the one with the higher `u64` serial at `+0x08`.** Which copy is live is not
consistent — on the measured drive P1's live copy is the front one and P2's is the tail
one. The stale copy is readable but disagrees by a few inodes (a transaction that was in
flight), so choosing wrong gives subtly wrong answers rather than an obvious failure.

| offset | field |
|--------|-------|
| 0x00 | magic |
| 0x08 | serial (u64) — higher wins |
| 0x30 | blocksize |
| 0x34 | num_inodes |
| 0x38 | free_inodes |
| 0x3c | num_blocks |
| 0x40 | free_blocks |
| 0x44 | allocation groups |
| 0x48 | root node: **inode table** |
| 0x98 | root node: **block bitmap** |
| 0xE8 | root node: **long filenames** |
| 0x138 | root node: spare |

Each root node is 80 bytes: `size` u64 `@+0x00`, `ptr[16]` u32 `@+0x08`, `levels` u8
`@+0x48`, `mode` u8 `@+0x49`.

Measured geometry — the two partitions differ, so don't hardcode either:

```
P1  bs 1024  inodes 61/64000    blocks 2047968  groups 8   live SB @0x6000      serial 20599
P2  bs 1024  inodes 376/128000  blocks 1023984  groups 4   live SB @0xBB7FF000  serial 143950
```

## The block map

One routine drives everything — file data, the inode table, the block bitmap, the
long-filename table. Only the root pointers and the level count differ.

```python
ppb = blocksize // 4                      # 256 pointers per indirect block

def map_block(ptrs, levels, n):           # logical block n -> physical block
    span = ppb ** levels
    if n // span >= 16:
        return None
    b, n = ptrs[n // span], n % span
    for _ in range(levels):
        if b == NIL:
            return None
        span //= ppb
        b = u32_le(read(boff(b), blocksize), (n // span) * 4)
        n %= span
    return None if b == NIL else b
```

`levels` caps file size: 0 → 16 KiB (16 direct blocks), 1 → 4 MiB, 2 → 1 GiB, 3 → 256 GiB.
Never special-case "direct vs indirect" — the uniform routine covers every case. A `None`
result is a sparse hole and reads as zeros; that's legal, not an error.

## Inodes

The inode table is **itself a file**, whose block map is the superblock's first root node.
Inode N (1-based) lives at byte `(N-1) * 128` of that file:

```python
def inode_offset(N):
    fo = (N - 1) * 128
    if fo >= rn_inodes.size:
        return None
    db = map_block(rn_inodes.ptr, rn_inodes.levels, fo // blocksize)
    return None if db is None else boff(db) + (fo % blocksize)
```

Struct (128 bytes) — identical to the public layout:

| offset | field |
|--------|-------|
| 0x00 | size (u64) |
| 0x08 | uid | 
| 0x0c | gid |
| 0x10 | ftime |
| 0x14 | mtime |
| 0x18 | atime |
| 0x1c | ctime |
| 0x20 | mode (u16) |
| 0x24 | block_ptr[16] (u32, `0xffffffff` = nil) |
| 0x64 | filelevels |
| 0x65 | status |

`status`: 0 = free, **1 and 3 = live**, 2 = unlinked but not yet released. Filtering on
`status in (1, 3)` yields exactly `num_inodes - free_inodes` on both partitions; counting
`!= 0` instead over-counts by the unlinked ones.

### There is no inode → offset formula, and there cannot be

This is a copy-on-write filesystem. Rewritten metadata gets relocated, so inode offsets
aren't even monotonic: on the measured drive inode 16 sits at `0x7E002780` but inode 17 at
`0x7D033000` — 16 MB *backwards*. The inode file's own logical blocks run
`0x3ffc, 0x3ffd, 0xc0, 0x3fff, 0x4000, 0xc3 …`, COW relocations interleaved with the
original `mkqnx6fs` layout.

Fitting a closed form reproduces 0.1% of offsets. Walking the tree reproduces 100%.

> **A correction worth recording.** An earlier pass at this filesystem — including an
> earlier draft of this document — concluded that inode numbers were "scrambled across
> allocation groups," on the evidence that recovered inode numbers landed on clean
> `num_inodes / groups` boundaries. That observation is real (QNX6 does hand out inode
> numbers per allocation group) but the conclusion drawn from it was wrong. Nothing is
> scrambled. The apparent disorder was COW relocation, and the reason the metadata chain
> looked empty was the missing `0x3000`, not a proprietary layout.

## Directories

Directory data is an array of 32-byte entries: `u32 inode`, `u8 namelen`, `name[27]`.

- `inode == 0` → **tombstone** (deleted entry). Skip it. The name bytes are often still
  readable, so failing to skip these invents files that don't exist.
- `namelen == 0xFF` → **long name**, stored out of line. The block index is a `u32` at
  **`+8`** (not `+5`): `block = map_block(rn_longfile.ptr, rn_longfile.levels, index)`,
  and that block holds a `u16` length followed by the name. Getting the offset wrong
  silently drops files rather than erroring.
- otherwise → plain name of that length.

Directories are ordinary files: read them through the same block map, and read **all**
their blocks — one directory on the measured drive is 9,216 bytes / 258 entries.

Symlinks (`mode & 0xF000 == 0xA000`) store their target as file contents, e.g.
`/acios/SDS.iso` → `/mnt/nav/pkgdb/SDS_NA_4_4_1/SDS_Data.iso`.

## Verifying an implementation

Four checks, each catching a different class of error. PCM Explorer ships them as
`verify` and all four pass on both partitions:

1. **Geometry** — `(plen / bs) - num_blocks == 16`. Catches a wrong block origin instantly.
2. **Inode census** — count `status in (1,3)` over `1..num_inodes`; must equal
   `num_inodes - free_inodes`.
3. **Block bitmap popcount** — count clear bits in the bitmap (reached through a
   *different* root node and block chain) and compare to `free_blocks`. This one is
   independent of all inode logic, so it validates the block origin on its own.
4. **Directory identity** — every directory's first entry must be `.` pointing at itself
   and its second `..`.

Beyond those, content-level validation is what actually proves byte correctness: PNG
chunk CRCs, ELF section tables (several files end *exactly* at EOF, which proves the last
block of a multi-level map is right), and SQLite `PRAGMA integrity_check`.

## If the unit still powers on

You don't need any of this. A live head unit can read its own disk over a network shell —
see [PCM-Forge](https://github.com/dspl1236/PCM-Forge). Offline reading matters when the
unit is dead, which is exactly when you need it most.
