from __future__ import annotations

from drivecanary.scrub import btrfs_found_errors, summarize

FINISHED = """UUID:             c2d4
Scrub started:    Sun Sep 14 03:00:00 2026
Status:           finished
Duration:         5:12:33
Total to scrub:   10.24TiB
Rate:             558.12MiB/s
Error summary:    no errors found
"""
RUNNING = """UUID:             c2d4
Scrub started:    Mon Sep 22 02:00:01 2026
Status:           running
Duration:         0:10:05
Time left:        2:30:00
ETA:              Mon Sep 22 04:40:06 2026
Total to scrub:   5.43TiB
Bytes scrubbed:   1.00TiB  (18.42%)
Rate:             488.23MiB/s
Error summary:    no errors found
"""
ERRORS = FINISHED.replace("no errors found", "csum=3\n  Corrected:      3\n  Uncorrectable:  0\n  Unverified:     0")
OLD = """scrub status for c2d4
\tscrub started at Sun Sep 14 03:00:00 2026 and finished after 00:05:12
\ttotal bytes scrubbed: 1.00TiB with 0 errors
"""


def test_btrfs_finished_says_when_and_how_long() -> None:
    assert summarize("btrfs", FINISHED) == "finished 2026-09-14 08:12, took 5:12:33; no errors found"
    assert btrfs_found_errors(FINISHED) is None


def test_btrfs_finish_crosses_midnight_and_days() -> None:
    text = FINISHED.replace("03:00:00", "22:30:00").replace("5:12:33", "30:00:00")
    assert summarize("btrfs", text) == "finished 2026-09-16 04:30, took 30:00:00; no errors found"


def test_btrfs_running() -> None:
    assert summarize("btrfs", RUNNING) == "running, since 2026-09-22 02:00, 18.42% done, 2:30:00 left; no errors found"


def test_btrfs_errors_are_found() -> None:
    assert summarize("btrfs", ERRORS) == "finished 2026-09-14 08:12, took 5:12:33; csum=3"
    assert btrfs_found_errors(ERRORS) == "csum=3"


def test_btrfs_other_states() -> None:
    aborted = summarize("btrfs", FINISHED.replace("finished", "aborted"))
    assert aborted.startswith("aborted after 5:12:33 (started 2026-09-14 03:00)")
    assert summarize("btrfs", "UUID: c2d4\n\tno stats available\n") == "never scrubbed"
    assert summarize("btrfs", OLD) == "finished 2026-09-14 03:05, took 00:05:12; 0 errors"
    assert summarize("btrfs", "something   nobody\nexpected") == "something nobody expected"
    assert summarize("btrfs", None) == "" and summarize("btrfs", "") == ""


def test_zfs() -> None:
    done = "scrub repaired 0B in 05:12:33 with 0 errors on Sun Sep 14 05:12:35 2026"
    assert summarize("zfs", done) == "finished 2026-09-14 05:12, took 05:12:33; 0 errors, repaired 0B"
    old = "scrub repaired 0B in 0 days 05:12:33 with 2 errors on Sun Sep 14 05:12:35 2026"
    assert summarize("zfs", old) == "finished 2026-09-14 05:12, took 0 days 05:12:33; 2 errors, repaired 0B"
    assert summarize("zfs", "none requested") == "never scrubbed"
    assert summarize("zfs", "scrub in progress since Sun Sep 14 03:00:00 2026") == "running since 2026-09-14 03:00"
    resilver = "resilvered 1.2T in 03:00:00 with 0 errors on Sun Sep 14 05:12:35 2026"
    assert summarize("zfs", resilver).startswith("resilvered")


def test_md_passes_through() -> None:
    line = "[==>.....]  resync = 12.6% (1234/9876) finish=127.5min speed=33440K/sec"
    assert summarize("md", "      " + line) == " ".join(line.split())
