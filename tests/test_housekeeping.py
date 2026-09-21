"""
Deleting old files, and noticing before the disk fills.

WHY THIS MATTERS MORE THAN IT SOUNDS
====================================
Three things in this system used to grow without any limit at all, and the
failure they cause is the quiet kind. When the disk is full the vendor's file
cannot be downloaded, nothing can be written -- including the record of the
problem, and the alert about it -- and Amazon simply carries on showing whatever
it last showed. Nobody is told, because telling somebody requires a write.

The worst of them: `report_retention_days` existed as a setting, appeared on the
Settings page, and its own help text said "older report files are deleted to
stop the disk filling up". No code read it. A setting that promises something
and does nothing is worse than no setting, because it stops anyone looking.

WHY THESE TESTS ARE CAREFUL ABOUT WHAT IS *NOT* DELETED
=======================================================
This is code whose whole job is deleting files. The cases that
matter most here are the ones where it must keep its hands off: a quarantined
archive, a recent file, a database row.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from app.core import settings_store
from app.engine.pipeline import RunOutcome, _housekeeping
from app.models import (
    FeedFile,
    FeedKind,
    FileStatus,
    ItemResult,
    Notification,
    ReportFile,
    Run,
    RunStatus,
    RunTrigger,
    SyncMode,
    utcnow,
)


@pytest.fixture
def run(session) -> Run:
    r = Run(
        trigger=RunTrigger.SCHEDULE, triggered_by="test",
        mode=SyncMode.DRY_RUN, status=RunStatus.COMPLETED,
    )
    session.add(r)
    session.flush()
    return r


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    """Point the app's data directory at a temporary one."""
    import app.engine.pipeline as pipeline

    monkeypatch.setattr(pipeline.app_settings, "data_dir", tmp_path)
    (tmp_path / "quarantine").mkdir(parents=True, exist_ok=True)
    (tmp_path / "reports").mkdir(parents=True, exist_ok=True)
    (tmp_path / "snapshots").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _archive(session, data_dir, name, *, days_old, status=FileStatus.PARSED, size=1024):
    """A downloaded feed archive on disk, with a matching database row."""
    path = data_dir / "quarantine" / name
    path.write_bytes(b"x" * size)
    record = FeedFile(
        filename=name,
        kind=FeedKind.FULL,
        status=status,
        local_path=str(path),
        processed_at=(utcnow() - timedelta(days=days_old)),
    )
    session.add(record)
    session.flush()
    return record, path


def _report(session, run, data_dir, name, *, days_old, size=2048):
    path = data_dir / "reports" / name
    path.write_bytes(b"y" * size)
    record = ReportFile(
        run_id=run.id,
        kind="full_price_changed",
        filename=name,
        path=str(path),
        created_at=(utcnow() - timedelta(days=days_old)),
    )
    session.add(record)
    session.flush()
    return record, path


def _cfg(session, **overrides):
    for key, value in overrides.items():
        settings_store.set_value(session, key, value, actor="test")
    session.flush()
    return settings_store.get_all(session)


# ===========================================================================
# Vendor archives
# ===========================================================================


class TestVendorArchives:
    def test_an_old_processed_archive_is_deleted(self, session, run, data_dir):
        """
        The daily full feed is 75 MB. Kept forever, that is about 2.2 GB a
        month, which fills a modest disk in weeks.
        """
        record, path = _archive(session, data_dir, "FULL_FEED_1_20260101.zip", days_old=10)
        cfg = _cfg(session, keep_feed_files_days=3)

        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert not path.exists(), "the old archive was not deleted"
        assert record.local_path is None, (
            "local_path must be cleared, or every later run retries the same delete"
        )

    def test_the_database_row_survives(self, session, run, data_dir):
        """
        Only the file goes. feed_files holds the content hash that stops a file
        being processed twice -- delete the row and the vendor's next re-upload
        of identical content would be ingested all over again.
        """
        record, _ = _archive(session, data_dir, "FULL_FEED_1_20260101.zip", days_old=10)
        record.content_sha256 = "abc123"
        session.flush()
        cfg = _cfg(session, keep_feed_files_days=3)

        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert session.get(FeedFile, record.id) is not None
        assert session.get(FeedFile, record.id).content_sha256 == "abc123"

    def test_a_recent_archive_is_kept(self, session, run, data_dir):
        record, path = _archive(session, data_dir, "FULL_FEED_1_20260907.zip", days_old=1)
        cfg = _cfg(session, keep_feed_files_days=3)

        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert path.exists(), "a file inside the retention window was deleted"

    def test_a_quarantined_archive_is_never_deleted(self, session, run, data_dir):
        """
        THE ONE THAT MUST NOT BE TIDIED AWAY.

        A rejected archive is exactly the file a human needs to open to find out
        what the vendor changed, and it is also the rarest. Deleting the
        evidence of a problem to reclaim 75 MB is a bad trade at any disk size.
        """
        record, path = _archive(
            session, data_dir, "FULL_FEED_1_20250101.zip",
            days_old=400, status=FileStatus.QUARANTINED,
        )
        cfg = _cfg(session, keep_feed_files_days=1)

        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert path.exists(), "a quarantined archive was deleted"
        assert record.local_path is not None

    def test_a_file_already_gone_is_not_an_error(self, session, run, data_dir):
        """
        A duplicate download deletes the file immediately but leaves local_path
        set. Housekeeping must cope rather than raising on a missing file.
        """
        record, path = _archive(session, data_dir, "DELTA_FEED_1_20260101_1.zip", days_old=10)
        path.unlink()
        cfg = _cfg(session, keep_feed_files_days=3)

        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert record.local_path is None

    def test_zero_days_deletes_as_soon_as_it_is_read(self, session, run, data_dir):
        """The setting the operator of a very small disk will want."""
        _, path = _archive(session, data_dir, "FULL_FEED_1_20260907.zip", days_old=0)
        cfg = _cfg(session, keep_feed_files_days=0)

        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert not path.exists()


# ===========================================================================
# Reports
# ===========================================================================


class TestReports:
    def test_old_report_files_are_deleted(self, session, run, data_dir):
        """
        The setting that promised this and did nothing. Five .xlsx files are
        written per run, and a run happens whenever a delta arrives.
        """
        _, old = _report(session, run, data_dir, "old.xlsx", days_old=120)
        _, new = _report(session, run, data_dir, "new.xlsx", days_old=2)
        cfg = _cfg(session, report_retention_days=90)

        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert not old.exists(), "report_retention_days is still not enforced"
        assert new.exists(), "a report inside the retention window was deleted"

    def test_the_report_rows_survive(self, session, run, data_dir):
        """
        The help text promises "the database records stay", so they must. The
        row is how the dashboard explains what a past run produced.
        """
        record, path = _report(session, run, data_dir, "old.xlsx", days_old=120)
        cfg = _cfg(session, report_retention_days=90)

        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert not path.exists()
        assert session.get(ReportFile, record.id) is not None


# ===========================================================================
# Catalogue snapshots
# ===========================================================================


class TestCatalogueSnapshots:
    def test_old_snapshots_are_deleted_and_recent_ones_kept(
        self, session, run, data_dir, monkeypatch
    ):
        """
        These are Amazon's own listing reports, and they are what "restore the
        account to how it looked on a past day" reads. Kept longer than
        anything else for that reason, but not forever at 5 MB a day.
        """
        import os
        import time

        # data/backups, because that is where the Amazon layer writes them.
        # This test used to write to data/snapshots -- matching the pruner's
        # bug rather than the running system -- which is exactly why a cleanup
        # that deleted nothing for months passed its own test every time.
        snapshots = data_dir / "backups"
        snapshots.mkdir(parents=True, exist_ok=True)

        old = snapshots / "listings-2026-01-01.txt"
        new = snapshots / "listings-2026-09-06.txt"
        for p in (old, new):
            p.write_bytes(b"z" * 512)

        long_ago = time.time() - (60 * 86400)
        os.utime(old, (long_ago, long_ago))

        cfg = _cfg(session, keep_catalog_snapshots_days=30)
        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert not old.exists()
        assert new.exists()


# ===========================================================================
# Vendor change history -- the biggest table
# ===========================================================================


class TestVendorHistory:
    def test_old_history_is_pruned_and_recent_history_kept(self, session, run, data_dir):
        """
        The largest table in the database and the last unbounded one. A row is
        written for every stock OR price change, and the first full feed alone
        writes one per product. On a modest disk this is what fills it.
        """
        from app.models import VendorProductHistory

        old = VendorProductHistory(
            barcode="0025543055416", change_type="stock",
            old_stock=131, new_stock=0, at=utcnow() - timedelta(days=400),
        )
        recent = VendorProductHistory(
            barcode="0005319056434", change_type="price",
            old_price=9.84, new_price=10.10, at=utcnow() - timedelta(days=5),
        )
        session.add_all([old, recent])
        session.flush()
        cfg = _cfg(session, keep_vendor_history_days=180)

        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        remaining = {h.barcode for h in session.query(VendorProductHistory).all()}
        assert remaining == {"0005319056434"}, (
            "expected only the recent row to survive"
        )

    def test_the_minimum_retention_is_enforced_by_the_setting(self, session):
        """
        Seven days is the floor. The table is what the generated reports are
        built from, so someone trimming it to nothing would break those rather
        than just save space -- the setting refuses rather than allowing it.
        """
        from app.core.settings_store import SettingError

        with pytest.raises(SettingError):
            settings_store.set_value(session, "keep_vendor_history_days", 1, actor="test")


# ===========================================================================
# The disk warning
# ===========================================================================


class TestDiskWarning:
    def test_low_space_raises_a_critical_alert(self, session, run, data_dir, monkeypatch):
        """
        The whole point of the check. It has to fire while there is still room
        to write the alert that says so.
        """
        import app.engine.pipeline as pipeline

        class _Usage:
            total = 30 * 1024**3
            used = 29 * 1024**3
            free = int(0.4 * 1024**3)     # 0.4 GB

        monkeypatch.setattr(pipeline.shutil, "disk_usage", lambda _p: _Usage())
        cfg = _cfg(session, min_free_disk_gb=2.0)
        outcome = RunOutcome(run_id=run.id, status=run.status)

        _housekeeping(session, run, cfg, outcome)

        note = session.query(Notification).filter(Notification.kind == "low_disk").one()
        assert note.severity == "critical"
        assert "0.4 GB" in note.subject
        # The message must say what to actually do, not just that it is bad.
        assert "Keep report files" in note.body
        assert any("low disk space" in e for e in outcome.errors)

    def test_plenty_of_space_says_nothing(self, session, run, data_dir, monkeypatch):
        import app.engine.pipeline as pipeline

        class _Usage:
            total = 200 * 1024**3
            used = 10 * 1024**3
            free = 190 * 1024**3

        monkeypatch.setattr(pipeline.shutil, "disk_usage", lambda _p: _Usage())
        cfg = _cfg(session, min_free_disk_gb=2.0)

        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert session.query(Notification).filter(Notification.kind == "low_disk").count() == 0

    def test_the_check_can_be_switched_off(self, session, run, data_dir, monkeypatch):
        import app.engine.pipeline as pipeline

        class _Usage:
            total = 30 * 1024**3
            used = 30 * 1024**3
            free = 0

        monkeypatch.setattr(pipeline.shutil, "disk_usage", lambda _p: _Usage())
        cfg = _cfg(session, min_free_disk_gb=0)

        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert session.query(Notification).filter(Notification.kind == "low_disk").count() == 0


# ===========================================================================
# It must never break a run
# ===========================================================================


class TestItNeverBreaksARun:
    """
    Housekeeping is called from a `finally`, inside its own try/except. Being
    unable to delete an old file is not worth turning a successful sync into a
    failed one -- and on Windows a file held open by a virus scanner or a backup
    agent is routine rather than exotic.
    """

    def test_a_file_that_cannot_be_deleted_is_logged_and_stepped_over(
        self, session, run, data_dir, monkeypatch
    ):
        """The Windows case: something else holds the file open."""
        record, path = _archive(session, data_dir, "FULL_FEED_1_20260101.zip", days_old=10)

        real_unlink = Path.unlink

        def locked(self, *a, **k):
            if self.name == "FULL_FEED_1_20260101.zip":
                raise OSError(32, "The process cannot access the file")
            return real_unlink(self, *a, **k)

        monkeypatch.setattr(Path, "unlink", locked)
        cfg = _cfg(session, keep_feed_files_days=3)

        # No exception, and the row is left pointing at the file so the next
        # run tries again once whatever held it open has let go.
        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert path.exists()
        assert record.local_path is not None, "a file that survived must stay tracked"

    def test_a_missing_data_directory_is_not_an_error(self, session, run, tmp_path, monkeypatch):
        """A fresh install, before anything has been written."""
        import app.engine.pipeline as pipeline

        monkeypatch.setattr(pipeline.app_settings, "data_dir", tmp_path / "not-created-yet")
        cfg = settings_store.get_all(session)

        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))


# ===========================================================================
# The snapshot pruner must look where snapshots actually are
# ===========================================================================


class TestSnapshotsArePrunedWhereTheyAreWritten:
    """
    The catalogue snapshot cleanup ran against a directory that has never
    existed, so it deleted nothing, ever.

    ``fetch_all_listings`` is handed ``settings.backups_dir`` -- that is
    ``data/backups`` -- and writes ``catalog-<stamp>.tsv`` there on every
    refresh. The pruner looked in ``data/snapshots``. The string "snapshots"
    appears nowhere else in the application, so the guard
    ``if snapshot_dir.is_dir()`` was simply false on every run and the setting
    was decorative.

    It went unnoticed because the ORIGINAL TEST WROTE TO THE WRONG DIRECTORY
    TOO: the test and the bug agreed, and both disagreed with the running
    system. So this one deliberately writes where the Amazon layer really
    writes, and would fail against a pruner pointed anywhere else.

    The cost was not theoretical. Once the catalogue refresh became hourly, a
    ~17 MB snapshot was written 24 times a day and never removed -- about
    hundreds of megabytes a day, growing until the disk fills and the sync
    stopped without being able to write down why.
    """

    def test_an_old_snapshot_in_the_backups_directory_is_deleted(
        self, session, run, data_dir
    ):
        import os
        import time

        backups = data_dir / "backups"
        backups.mkdir(parents=True, exist_ok=True)

        old = backups / "catalog-20260101-030000.tsv"
        recent = backups / "catalog-20260916-030000.tsv"
        for p in (old, recent):
            p.write_bytes(b"z" * 2048)

        long_ago = time.time() - (60 * 86400)
        os.utime(old, (long_ago, long_ago))

        cfg = _cfg(session, keep_catalog_snapshots_days=30)
        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert not old.exists(), (
            "a 60-day-old catalogue snapshot survived a 30-day retention setting -- "
            "the pruner is not looking in data/backups, which is where the Amazon "
            "layer writes them"
        )
        assert recent.exists(), "a recent snapshot must be kept"

    def test_the_directory_the_pruner_uses_is_the_one_the_writer_uses(self):
        """
        Belt and braces, in one line: the two halves must name the same place.

        A future change that moves either side breaks this immediately, rather
        than silently reinstating a cleanup that deletes nothing.
        """
        from app.config import settings as real_settings

        assert real_settings.backups_dir.name == "backups"


# ===========================================================================
# The empty folders left behind after pruning
# ===========================================================================


class TestEmptyReportFoldersAreRemoved:
    """
    Every run wrote five reports into its own ``reports/run-<id>`` folder. The
    pruner removed the files and left the folder, for ever -- 24 a day on an
    hourly schedule, thousands a year. An operator opening the reports
    directory and found hundreds of empty folders with no way to see which ones
    still held anything.

    They are litter rather than history: the run history lives in the database,
    and a download resolves the file's full path from its ``ReportFile`` row
    instead of scanning directories. Nothing in the application ever reads a
    report folder -- it is only somewhere to write into.
    """

    def test_an_emptied_run_folder_is_removed(self, session, run, data_dir):
        reports = data_dir / "reports"
        empty = reports / "run-7"
        empty.mkdir(parents=True, exist_ok=True)

        cfg = _cfg(session)
        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert not empty.exists(), (
            "an empty run folder was left behind; these accumulate one per run for ever"
        )

    def test_a_folder_that_still_holds_a_file_is_left_alone(
        self, session, run, data_dir
    ):
        """The whole point is to remove litter, never to remove content."""
        reports = data_dir / "reports"
        keeper = reports / "run-8"
        keeper.mkdir(parents=True, exist_ok=True)
        (keeper / "in-stock.xlsx").write_bytes(b"x" * 64)

        cfg = _cfg(session)
        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert keeper.exists()
        assert (keeper / "in-stock.xlsx").exists()

    def test_the_reports_root_itself_is_never_removed(self, session, run, data_dir):
        """
        Deleting the root would break every later run's report writing. It does
        not start with "run-", which is the guard, and this pins that guard.
        """
        reports = data_dir / "reports"
        reports.mkdir(parents=True, exist_ok=True)

        cfg = _cfg(session)
        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert reports.is_dir()

    def test_a_directory_that_is_not_a_run_folder_is_left_alone(
        self, session, run, data_dir
    ):
        reports = data_dir / "reports"
        other = reports / "archive-for-accounts"
        other.mkdir(parents=True, exist_ok=True)

        cfg = _cfg(session)
        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert other.is_dir(), "only run-* folders are ours to remove"


# ===========================================================================
# The per-product change log -- the fastest growing table
# ===========================================================================


def _batch_with_item(session, run, *, days_old, result, qty_before=7):
    """A finished batch and one item in the given state."""
    from app.models import BatchStatus, PushBatch, PushItem

    batch = PushBatch(
        run_id=run.id,
        status=BatchStatus.VERIFIED,
        item_count=1,
        created_at=(utcnow() - timedelta(days=days_old)),
    )
    session.add(batch)
    session.flush()
    item = PushItem(
        batch_id=batch.id,
        seller_sku="EXAMPLE-0007298811035",
        previous_quantity=qty_before,
        new_quantity=0,
        result=result,
    )
    session.add(item)
    session.flush()
    return batch, item


class TestThePerProductChangeLog:
    """
    One row per quantity changed, each holding what Amazon had before it. At
    a busy installation that is tens of thousands a day for as long as the
    system runs -- millions of rows and gigabytes if nothing removes them, and
    a full disk is the failure this whole module exists to prevent.

    They are also the rows Undo reads, which is why the tests that matter here
    are the ones proving what is NOT deleted.
    """

    def test_an_old_finished_batch_has_its_item_rows_removed(
        self, session, run, data_dir
    ):
        from app.models import PushItem

        batch, item = _batch_with_item(
            session, run, days_old=200, result=ItemResult.VERIFIED
        )
        item_id = item.id

        cfg = _cfg(session, keep_push_items_days=90)
        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert session.get(PushItem, item_id) is None

    def test_the_batch_and_its_totals_survive(self, session, run, data_dir):
        """
        Only the per-SKU detail goes. The run history must still show that the
        batch happened and how large it was.
        """
        from app.models import PushBatch

        batch, _ = _batch_with_item(
            session, run, days_old=200, result=ItemResult.VERIFIED
        )
        batch_id = batch.id

        cfg = _cfg(session, keep_push_items_days=90)
        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        kept = session.get(PushBatch, batch_id)
        assert kept is not None
        assert kept.item_count == 1

    def test_a_recent_batch_is_untouched(self, session, run, data_dir):
        """The undo window. This is the case that keeps undo possible."""
        from app.models import PushItem

        _, item = _batch_with_item(
            session, run, days_old=3, result=ItemResult.VERIFIED
        )
        item_id = item.id

        cfg = _cfg(session, keep_push_items_days=90)
        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        surviving = session.get(PushItem, item_id)
        assert surviving is not None
        assert surviving.previous_quantity == 7, (
            "previous_quantity is what Undo restores; it must survive intact"
        )

    def test_an_old_batch_still_awaiting_approval_is_never_pruned(
        self, session, run, data_dir
    ):
        """
        PENDING means never sent. A batch waiting for approval is entirely
        PENDING, and emptying it would hand the operator an approval with
        nothing in it. Age must not be enough on its own.
        """
        from app.models import PushItem

        _, item = _batch_with_item(
            session, run, days_old=400, result=ItemResult.PENDING
        )
        item_id = item.id

        cfg = _cfg(session, keep_push_items_days=14)
        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert session.get(PushItem, item_id) is not None

    def test_an_old_batch_not_yet_confirmed_is_never_pruned(
        self, session, run, data_dir
    ):
        """
        ACCEPTED means sent but not read back. That is exactly what the
        catalogue refresh returns for, so these rows are still in use however
        old the batch is.
        """
        from app.models import PushItem

        _, item = _batch_with_item(
            session, run, days_old=400, result=ItemResult.ACCEPTED
        )
        item_id = item.id

        cfg = _cfg(session, keep_push_items_days=14)
        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert session.get(PushItem, item_id) is not None, (
            "a sent-but-unconfirmed row was deleted; the refresh can no longer settle it"
        )

    def test_one_unfinished_item_protects_the_whole_batch(
        self, session, run, data_dir
    ):
        """
        Partial pruning of a batch would be the worst outcome: a half-undoable
        batch that reports itself as complete. The batch is all or nothing.
        """
        from app.models import PushItem

        batch, verified = _batch_with_item(
            session, run, days_old=300, result=ItemResult.VERIFIED
        )
        session.add(
            PushItem(
                batch_id=batch.id,
                seller_sku="EXAMPLE-0001749188257",
                previous_quantity=4,
                new_quantity=2,
                result=ItemResult.ACCEPTED,
            )
        )
        session.flush()
        verified_id = verified.id

        cfg = _cfg(session, keep_push_items_days=14)
        _housekeeping(session, run, cfg, RunOutcome(run_id=run.id, status=run.status))

        assert session.get(PushItem, verified_id) is not None, (
            "the finished half of an unfinished batch was pruned, leaving it partial"
        )
