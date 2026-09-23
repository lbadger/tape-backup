# Changelog

## 0.1.0

Initial release of the Linux tape backup and restore tool.

- Full and incremental GNU tar archives across multiple tape volumes.
- Read-back verification and SHA-256 checksums before checkpointing data.
- End-of-tape rollover, short-write recovery, and resumable backups and restores.
- Ordered full-plus-incremental restore with deletion and rename handling.
- Configurable tape device, defaulting to `/dev/nst0`, and media-loader hooks.
- File-backed media for testing and inspection.
- Standalone Linux x86-64 executable and Docker/native build scripts.
- 34 automated tests covering archive operations, injected failures, and the binary.

The binary embeds Python and targets glibc 2.31 or newer. GNU tar, `mt` (from
`mt-st`), and normal Linux runtime libraries including zlib remain required.
Physical tape hardware has not been tested; automated checks use simulated media.
Preserve the catalogs independently of the backup machine: they are required for
restore.
