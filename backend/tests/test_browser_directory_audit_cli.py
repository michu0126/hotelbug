import json
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from audit_ihg_catalog import (
    audit_settings,
    emit_progress,
    ensure_audit_database,
    main,
    parse_args,
    run_batch,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.tables import AppSetting, Base, ProviderStatus, utcnow
from app.providers.marriott_cn_catalog import ROOT as CN_ROOT


def test_legacy_state_directory_cli_remains_available(tmp_path):
    args = parse_args(["--state-dir", str(tmp_path / "audit")])
    assert args.state_dir == tmp_path / "audit"
    assert args.database is args.session_dir is args.report is None
    assert args.provider == "ihg"


def test_explicit_database_session_and_report_are_resolved(tmp_path):
    database = tmp_path / "existing.sqlite"
    database.touch()
    session_dir = tmp_path / "project-browser"
    report = tmp_path / "new-report.json"
    args = parse_args(
        [
            "--provider",
            "marriott",
            "--database",
            str(database),
            "--session-dir",
            str(session_dir),
            "--report",
            str(report),
            "--max-pages",
            "3",
        ]
    )
    assert args.database == database
    assert args.state_dir is None
    assert args.report == report and not report.exists()
    settings = audit_settings(args)
    assert settings.browser_session_dir == str(session_dir)
    assert settings.job_timeout_seconds == 300
    assert settings.provider_policy("marriott").enabled
    assert not settings.provider_policy("ihg").enabled
    assert settings.telegram_bot_token.get_secret_value() == settings.telegram_chat_id == ""


@pytest.mark.parametrize("case", ["missing_database", "both_locations", "existing_report"])
def test_invalid_paths_are_rejected_without_overwriting_evidence(tmp_path, case):
    database = tmp_path / "existing.sqlite"
    database.write_text("Existing audit evidence", encoding="utf-8")
    report = tmp_path / "existing.json"
    report.write_text('{"keep":true}', encoding="utf-8")
    arguments = {
        "missing_database": ["--database", str(tmp_path / "missing.sqlite")],
        "both_locations": ["--database", str(database), "--state-dir", str(tmp_path)],
        "existing_report": ["--database", str(database), "--report", str(report)],
    }
    with pytest.raises(SystemExit) as exc:
        parse_args(arguments[case])
    assert exc.value.code == 2
    assert database.read_text(encoding="utf-8") == "Existing audit evidence"
    assert report.read_text(encoding="utf-8") == '{"keep":true}'
    assert not (tmp_path / "missing.sqlite").exists()


async def test_explicit_resume_requires_marker_even_for_empty_database(sessions):
    with pytest.raises(AssertionError, match="existing isolated directory audit marker"):
        await ensure_audit_database(sessions, "marriott", require_marker=True)
    async with sessions() as db:
        assert (await db.scalars(select(AppSetting))).all() == []
    await ensure_audit_database(sessions, "marriott")
    await ensure_audit_database(sessions, "marriott", require_marker=True)
    with pytest.raises(AssertionError, match="Different directory audit provider"):
        await ensure_audit_database(sessions, "ihg", require_marker=True)


async def test_resumed_cn_audit_reports_selected_root_without_old_en_coverage(sessions, queue):
    now = utcnow()
    async with sessions() as db, db.begin():
        db.add(AppSetting(key="audit:directory-only", value={"provider": "marriott"}))
        db.add(
            AppSetting(
                key="catalog:marriott",
                value={
                    "root": CN_ROOT,
                    "pages": {
                        CN_ROOT: {
                            "status": "SUCCEEDED",
                            "last_scan_at": now.timestamp(),
                            "next_scan_at": (now + timedelta(days=14)).timestamp(),
                            "directory_urls": [],
                            "hotel_ids": ["AAAAB", "AAAAC"],
                            "reported_total": 2,
                            "page_complete": True,
                        }
                    },
                },
            )
        )
        db.add(ProviderStatus(provider="marriott", status="BLOCKED", blocked_until=now + timedelta(hours=1)))
    args = parse_args(["--provider", "marriott", "--state-dir", "unused-offline-test"])
    report = await run_batch(sessions, queue, audit_settings(args), provider="marriott", emit=lambda _: None)
    assert report["selected_directory_root"] == CN_ROOT
    assert report["outcome"] == "PROVIDER_PAUSED"
    assert report["coverage"]["official_worldwide_hotels"] == 2
    assert report["coverage"]["directory_unique_hotels"] == 2
    assert report["coverage"]["catalog_coverage_status"] == "FULL_CATALOG_VERIFIED"


def test_default_progress_flushes_immediately(monkeypatch):
    output = Mock()
    monkeypatch.setattr("builtins.print", output)
    emit_progress("worker starting")
    output.assert_called_once_with("worker starting", flush=True)


async def marked_database(path, provider="marriott"):
    engine = create_async_engine("sqlite+aiosqlite:///" + path.as_posix())
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        await ensure_audit_database(sessions, provider)
    finally:
        await engine.dispose()


async def test_cli_resumes_marked_database_and_writes_new_report(tmp_path, monkeypatch):
    database = tmp_path / "audit.sqlite"
    await marked_database(database)
    report = tmp_path / "reports" / "resume.json"
    batch = AsyncMock(return_value={"outcome": "NO_DIRECTORY_DUE", "directory_jobs_succeeded_this_run": 0})
    close = AsyncMock()
    monkeypatch.setattr("audit_ihg_catalog.run_batch", batch)
    monkeypatch.setattr("audit_ihg_catalog.close_browser_sessions", close)
    await main(
        [
            "--provider",
            "marriott",
            "--database",
            str(database),
            "--session-dir",
            str(tmp_path / "browser"),
            "--report",
            str(report),
        ]
    )
    saved = json.loads(report.read_text(encoding="utf-8"))
    assert saved["database"] == str(database)
    assert saved["outcome"] == "NO_DIRECTORY_DUE"
    assert saved["cleanup_status"] == "SUCCEEDED"
    assert batch.await_count == close.await_count == 1
    assert batch.await_args.kwargs["provider"] == "marriott"


async def test_cli_rejects_other_provider_before_any_worker_run(tmp_path, monkeypatch):
    database = tmp_path / "audit.sqlite"
    await marked_database(database, provider="ihg")
    report = tmp_path / "resume.json"
    batch = AsyncMock()
    monkeypatch.setattr("audit_ihg_catalog.run_batch", batch)
    monkeypatch.setattr("audit_ihg_catalog.close_browser_sessions", AsyncMock())
    with pytest.raises(AssertionError, match="Different directory audit provider"):
        await main(["--provider", "marriott", "--database", str(database), "--report", str(report)])
    batch.assert_not_awaited()
    saved = json.loads(report.read_text(encoding="utf-8"))
    assert saved["outcome"] == "AUDIT_ERROR"
    assert saved["diagnostic_error_type"] == "AssertionError"
    assert saved["cleanup_status"] == "SUCCEEDED"
