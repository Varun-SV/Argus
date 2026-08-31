"""Round-4 transactional report publication for PR #22.

A report bundle is one derived trust unit.  Regeneration therefore stages every
member first and preserves the previously published bundle until all staged
bytes are durable.  If publication or post-publication regeneration verification
fails, the old named members are restored before the error is returned.
"""
from __future__ import annotations

import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import reports_runtime as _runtime
from .finalization import FinalizationTrustState
from .store import AtesStoreBusy, AtesStoreError, _PinnedDirectory, _WriterLock

_LOCK_WAIT_SECONDS = 5.0
_LOCK_RETRY_SECONDS = 0.01


def _exists(directory: _PinnedDirectory, name: str) -> bool:
    try:
        if os.name == "nt":
            return os.path.lexists(directory.path / name)
        if directory._fd is None:
            raise _runtime.ReportError("pinned reports directory has no descriptor")
        os.stat(name, dir_fd=directory._fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise _runtime.ReportError(f"cannot inspect report member {name}") from exc


def _replace(directory: _PinnedDirectory, source: str, target: str) -> None:
    try:
        if os.name == "nt":
            os.replace(directory.path / source, directory.path / target)
        else:
            if directory._fd is None:
                raise _runtime.ReportError("pinned reports directory has no descriptor")
            os.replace(
                source,
                target,
                src_dir_fd=directory._fd,
                dst_dir_fd=directory._fd,
            )
    except _runtime.ReportError:
        raise
    except OSError as exc:
        raise _runtime.ReportError(
            f"cannot replace report member {source} -> {target}"
        ) from exc


def _unlink(directory: _PinnedDirectory, name: str) -> None:
    try:
        if os.name == "nt":
            (directory.path / name).unlink(missing_ok=True)
        else:
            if directory._fd is None:
                raise _runtime.ReportError("pinned reports directory has no descriptor")
            try:
                os.unlink(name, dir_fd=directory._fd)
            except FileNotFoundError:
                pass
    except _runtime.ReportError:
        raise
    except OSError as exc:
        raise _runtime.ReportError(f"cannot remove report member {name}") from exc


def _rollback(
    reports: _PinnedDirectory,
    *,
    staged: dict[str, str],
    backups: dict[str, str],
    published: list[str],
    token: str,
) -> None:
    """Restore the exact previously named bundle or fail explicitly ambiguous."""
    trash: list[str] = []
    try:
        # First move every newly published target out of the way.  We keep the
        # bytes until the prior names are restored so a failed rollback never
        # destroys the only remaining copy silently.
        for name in reversed(published):
            if not _exists(reports, name):
                raise _runtime.ReportError(
                    f"new report member disappeared during rollback: {name}"
                )
            trash_name = f".{name}.failed-{token}"
            if _exists(reports, trash_name):
                raise _runtime.ReportError("report rollback trash name unexpectedly exists")
            _replace(reports, name, trash_name)
            trash.append(trash_name)

        # Restore every member that existed before the transaction. Members that
        # did not previously exist remain absent because their new copy was moved
        # to trash above.
        for name, backup_name in backups.items():
            if not _exists(reports, backup_name):
                raise _runtime.ReportError(
                    f"report rollback backup disappeared for {name}"
                )
            if _exists(reports, name):
                raise _runtime.ReportError(
                    f"report rollback target unexpectedly exists for {name}"
                )
            _replace(reports, backup_name, name)

        reports.fsync()

        # Once the old bundle is durable again, temporary/new bytes may be
        # discarded. Any failure here is surfaced rather than hidden.
        for stage_name in staged.values():
            if _exists(reports, stage_name):
                _unlink(reports, stage_name)
        for trash_name in trash:
            if _exists(reports, trash_name):
                _unlink(reports, trash_name)
        reports.fsync()
    except BaseException as exc:
        raise _runtime.ReportError(
            "report publication failed and rollback is incomplete or ambiguous"
        ) from exc


@contextmanager
def _report_transaction(root: Path):
    """Hold a report-directory-scoped writer lock for one whole generation."""
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    run_pin = reports = lock = None
    while True:
        run_pin = _PinnedDirectory(root)
        try:
            reports = run_pin.ensure_child("reports", "ATES reports directory")
            run_pin.assert_child_identity("reports", reports, "ATES reports directory")
            lock = _WriterLock(reports)
            lock.assert_authoritative()
            break
        except AtesStoreBusy as exc:
            if reports is not None:
                reports.close()
            run_pin.close()
            reports = run_pin = None
            if time.monotonic() >= deadline:
                raise _runtime.ReportError(
                    "timed out waiting for report writer authority"
                ) from exc
            time.sleep(_LOCK_RETRY_SECONDS)
        except BaseException:
            if reports is not None:
                reports.close()
            if run_pin is not None:
                run_pin.close()
            raise
    try:
        yield run_pin, reports, lock
    finally:
        if lock is not None:
            try:
                lock.close()
            except BaseException:
                pass
        if reports is not None:
            try:
                reports.close()
            except BaseException:
                pass
        if run_pin is not None:
            try:
                run_pin.close()
            except BaseException:
                pass


def _render_reports_locked(
    root: Path,
    run_pin: _PinnedDirectory,
    reports: _PinnedDirectory,
    lock: _WriterLock,
    *,
    approval_key_resolver=None,
):
    """Render while the caller owns the report-scoped writer authority."""
    # Build the model only after acquiring authority. Detached-ledger changes
    # remain detectable by the post-publication verifier, while other report
    # writers cannot interfere with this transaction's names or backups.
    lock.assert_authoritative()
    model = _runtime._model(root, approval_key_resolver)
    members = _runtime._rendered(model)
    manifest_bytes = _runtime._json(_runtime._manifest(root, members))
    desired = dict(members)
    desired["report-manifest-0001.json"] = manifest_bytes

    token = uuid.uuid4().hex
    staged: dict[str, str] = {}
    backups: dict[str, str] = {}
    published: list[str] = []
    paths: dict[str, Path] = {}

    try:
        run_pin.assert_child_identity("reports", reports, "ATES reports directory")
        lock.assert_authoritative()

        # Stage *all* bytes under private names first. A late renderer/write
        # failure cannot touch the previously published bundle.
        for name, data in desired.items():
            stage_name = f".{name}.stage-{token}"
            if _exists(reports, stage_name):
                raise _runtime.ReportError("report staging name unexpectedly exists")
            _runtime._write(reports, stage_name, data)
            if _runtime._pinned_bytes(
                reports.path, stage_name, f"staged report {name}"
            ) != data:
                raise _runtime.ReportError(f"staged report changed before commit: {name}")
            staged[name] = stage_name

        run_pin.assert_child_identity("reports", reports, "ATES reports directory")

        # Preserve every currently named member before replacing anything.
        for name in desired:
            if not _exists(reports, name):
                continue
            # Refuse to move unsafe/symlinked old state under the guise of a
            # transactional backup.
            _runtime._pinned_bytes(reports.path, name, f"existing report {name}")
            backup_name = f".{name}.backup-{token}"
            if _exists(reports, backup_name):
                raise _runtime.ReportError("report backup name unexpectedly exists")
            _replace(reports, name, backup_name)
            backups[name] = backup_name

        reports.fsync()

        # Commit the complete staged generation. Any failure after the first
        # replacement is rolled back to the exact preserved generation below.
        for name, stage_name in staged.items():
            _replace(reports, stage_name, name)
            published.append(name)
            paths[name] = reports.path / name
        reports.fsync()
        run_pin.assert_child_identity("reports", reports, "ATES reports directory")
        lock.assert_authoritative()

        checked = _runtime.verify_report_bundle(
            root,
            approval_key_resolver=approval_key_resolver,
        )
        if checked.trust_state is not FinalizationTrustState.REGENERATED_VERIFIED:
            raise _runtime.ReportError(
                checked.error or "regenerated report verification failed"
            )
        lock.assert_authoritative()

    except BaseException as exc:
        if reports is not None:
            try:
                _rollback(
                    reports,
                    staged=staged,
                    backups=backups,
                    published=published,
                    token=token,
                )
            except _runtime.ReportError as rollback_exc:
                raise rollback_exc from exc
        if isinstance(exc, _runtime.ReportError):
            raise
        if isinstance(exc, (OSError, AtesStoreError)):
            raise _runtime.ReportError("cannot publish report bundle safely") from exc
        raise

    # The new generation is now durable and verified. Cleanup is outside the
    # rollback boundary: once any restoration backup is removed, rolling back
    # could destroy the only complete bundle. Surface cleanup errors while
    # leaving the committed public members and any remaining backups intact.
    try:
        for backup_name in backups.values():
            if _exists(reports, backup_name):
                _unlink(reports, backup_name)
        reports.fsync()
    except (OSError, AtesStoreError, _runtime.ReportError) as exc:
        raise _runtime.ReportError(
            "report bundle committed, but backup cleanup failed"
        ) from exc

    return _runtime.ReportBundle(
        root,
        root / "reports",
        paths["report.json"],
        paths["report.md"],
        paths["report.html"],
        paths["junit.xml"],
        paths["report-manifest-0001.json"],
        checked.trust_state,
    )


def render_reports(
    run_dir: Path | str,
    *,
    approval_key_resolver=None,
):
    root = _runtime._root(run_dir)
    # Report publication has its own cross-process writer authority. Detached
    # ledgers retain their independent append lock; the post-publication
    # verifier detects any ledger change and triggers rollback while no second
    # renderer can touch this generation's names/backups.
    with _report_transaction(root) as (run_pin, reports, lock):
        lock.assert_authoritative()
        return _render_reports_locked(
            root,
            run_pin,
            reports,
            lock,
            approval_key_resolver=approval_key_resolver,
        )


def install() -> None:
    _runtime.render_reports = render_reports


__all__ = ["install"]
