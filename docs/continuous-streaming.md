# Continuous streaming plan and implementation

The old writer waited for an entire 1–10 GiB chunk, hashed it, wrote it, and
synchronously committed a tape filemark. Read-ahead overlapped the next read but
could not remove the per-chunk barrier.

## Plan

1. Separate RAM budgets from frame size. Queue small frames from tar or SSH and
   refill each released slot while the writer proceeds.
2. Track a bounded window of unconfirmed writes. Query the drive's medium position
   without flushing; release only fully confirmed frames. Keep a synchronous
   fallback for missing telemetry or a recovery window that fills.
3. Use format 3, remove per-frame filemarks, and allow multi-frame overlap on
   replacement volumes.
4. Report queue occupancy, recovery occupancy, confirmed bytes, and flush reasons.
5. Exercise delayed failures, corruption, tape rollover, format validation, SSH,
   and the standalone build before publishing the implementation result.

Steps 1–5 are implemented and automated validation has passed. Physical tape
qualification remains a separate check on the target drive.

## Data path and bounds

Local tar and the SSH source produce frames of at most 4 MiB. Smaller RAM budgets
or volume caps reduce frame size. The writer updates the archive digest for each
frame, calculates its payload digest, and writes its header and 64 KiB records.
Hashing never waits for a whole GiB-sized buffer. The source thread reserves queue
space before reading; its cancellation path terminates the source process before
joining the thread. Source failures cannot produce a completion marker.

`--buffer-size` remains 64 KiB–10 GiB, default 1 GiB. The read-ahead queue and
recovery window each get that budget, with active-frame and allocation overhead.
Recovery accounting includes headers and rounded payload sizes. Small budgets
have a 128 KiB minimum recovery window; the source can hold an active frame and
one queued frame. Volume caps can reduce recovery capacity. No archive bytes are
spooled to disk.

## Durability and recovery

READ POSITION short form returns separate host and medium object locations. The
writer counts every successful record and synchronous filemark. It validates that
the reported host position equals this count and that the next-unwritten medium
position advances monotonically. Unknown/overflow positions, short responses, or
inconsistent counts never release data. Unsupported commands or SG_IO permissions
turn off position tracking for that volume. Transport and deferred medium errors
enter the same replay path as failed writes.

The Linux tape descriptor is in variable-block mode. Kernel asynchronous writes,
immediate rewind, and immediate filemark options are disabled; drive buffering
is preserved. SG_IO queries are issued by the same writer thread, between writes.
They may drain kernel pending writes but do not request a drive-buffer flush.
The implementation does not infer durability from successful write calls, the
host-side MTIOCPOS position, or estimated buffer byte counts.

Queries occur after approximately 16 MiB of records and before exhausting
recovery space. Frames whose final record precedes the next-unwritten position
are released. If space remains insufficient, a synchronous filemark commits the
window. There are also synchronous commits for each volume header, planned volume
end, and backup completion. A final commit happens even if telemetry already
confirmed the completion frame. This is continuous streaming when telemetry is
usable and the recovery window is sufficient; no universal zero-pause guarantee
is made for source speed, drive mechanics, unsupported telemetry, or tape changes.

On an error, retain the entire uncertain suffix and write it again on a replacement
tape. Every frame's old physical position is cleared before replay. The replacement
volume header names the first replayed sequence and its preceding chain digest.
Three consecutive volumes without confirmed frame progress stop the backup. A
failed volume-header initialization aborts immediately: skipping that numbered
volume would create an unreadable tape set.

## Format

Tar uses format 3. It has `TAPE-STREAM-3` checksummed
headers, small payload frames, and a bounded `replay_bytes` declaration in every
volume header. Readers keep a bounded history
of sequence numbers and header digests, not old payloads. An overlapping frame must
match its remembered header exactly; payload checksums and the full archive digest
are also checked during verify/restore. Gaps, reordered or changed duplicates, and
inconsistent recovery declarations are errors. Only new frames advance archive
counts and are delivered to tar.

Filemarks occur only at commit boundaries. Metadata-only reading can seek over
file-backed records but must read/discard physical tape records in format 3.
Restore extracts privately and publishes only a fully validated chain. Other
format prefixes or volume format declarations are rejected. Format-3 backups
created by v1.0.0 remain readable without any conversion.

## Validation

Tests use real GNU tar, loopback SSH, file-backed media, and a simulated drive with
accepted writes ahead of its durable position. They cover continuous writes with
only header/final commits, bounded fallback, complete and partial lost tails,
multiple surviving duplicates, final flush failure, planned volume caps, changed
replays, read-ahead refill/start timing, position parsing and failure propagation,
and rejection of unsupported tape formats. The large binary test writes
and restores 10 GiB while asserting that individual frames remain small.

The README documents how to run the source and standalone suites, the optional
10 GiB round trip, and the Debian 11 smoke test without Python installed. Format-3
full and incremental tapes produced by the released v1.0.0 executable were also
restored successfully with the simplified reader after removing older formats.

Physical qualification: use scratch tapes on the target drive, verify a complete
backup, and exercise end-of-medium rollover and restore. Observe `committed` moving
while writes continue; `recovery buffer full` indicates that confirmation is not
keeping up with the configured recovery budget. Hardware throughput and actual
backhitch behavior cannot be established by simulated media tests.

## References

- [Linux SCSI tape driver](https://www.kernel.org/doc/html/latest/scsi/st.html):
  variable records, asynchronous writes, and synchronous filemark semantics.
- [HPE LTO READ POSITION short form](https://support.hpe.com/hpesc/public/docDisplay?docId=sd00001234en_us&page=GUID-D7147C7F-2016-0901-0922-00000000068A.html):
  separate host/medium object locations and position validity flags.
- [Linux SG_IO interface](https://github.com/torvalds/linux/blob/v6.1/include/scsi/sg.h)
  and [st ioctl implementation](https://github.com/torvalds/linux/blob/v6.1/drivers/scsi/st.c).
