"""Actual Alembic round trip on a disposable local DB, not production."""

from datetime import date, timedelta
from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select

from alembic import command
from app.core import config as settings_module
from app.core.config import Settings
from app.models.tables import Hotel, StayScan, utcnow


def test_recheck_index_migration_preserves_rows_and_matches_model(tmp_path, monkeypatch):
    database = tmp_path / "migration_test.db"
    settings = Settings(_env_file=None, database_url="sqlite+aiosqlite:///" + database.as_posix())
    monkeypatch.setattr(settings_module, "get_settings", lambda: settings)
    backend = Path(__file__).resolve().parents[1]
    configuration = Config(str(backend / "alembic.ini"))
    configuration.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(configuration, "0002_stay_scans")
    engine = create_engine("sqlite:///" + database.as_posix())
    try:
        with engine.begin() as connection:
            connection.execute(
                Hotel.__table__.insert().values(
                    id="offline-hotel",
                    provider="marriott",
                    provider_hotel_id="OFFLINE",
                    hotel_name="Migration-only hotel",
                    official_url="https://www.marriott.com/",
                )
            )
            connection.execute(
                StayScan.__table__.insert().values(
                    hotel_id="offline-hotel",
                    check_in=date.today(),
                    check_out=date.today() + timedelta(days=1),
                    adults=2,
                    rooms=1,
                    status="AVAILABLE",
                    job_id="offline-only",
                    observed_at=utcnow(),
                )
            )
        command.upgrade(configuration, "head")
        assert {"ix_stay_scans_recheck_cursor", "ix_stay_scans_window"} <= {
            item["name"] for item in inspect(engine).get_indexes("stay_scans")
        }
        assert "ix_job_stay_attempt" in {item["name"] for item in inspect(engine).get_indexes("crawl_jobs")}
        command.check(configuration)
        command.downgrade(configuration, "0002_stay_scans")
        with engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(StayScan)) == 1
            assert connection.scalar(select(func.count()).select_from(Hotel)) == 1
        assert "ix_stay_scans_recheck_cursor" not in {
            item["name"] for item in inspect(engine).get_indexes("stay_scans")
        }
        command.upgrade(configuration, "head")
        command.check(configuration)
        with engine.connect() as connection:
            assert connection.scalar(select(StayScan.status)) == "AVAILABLE"
    finally:
        engine.dispose()
