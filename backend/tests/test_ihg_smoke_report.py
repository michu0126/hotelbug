"""A quote result and browser-cleanup result are separate diagnostic evidence."""

import json
import sys
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest
import smoke_ihg_browser as smoke

from app.core.errors import ErrorCode, ProviderError


@pytest.mark.parametrize("failure", [None, "quote", "page_cleanup", "session_cleanup"])
async def test_smoke_report_never_calls_partial_success_a_completed_run(monkeypatch, tmp_path, failure):
    report_path = tmp_path / "evidence.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "smoke",
            "--official-url",
            "https://www.ihg.com/avidhotels/hotels/us/en/kingman/igmav/hoteldetail",
            "--date",
            date.today().isoformat(),
            "--report",
            str(report_path),
        ],
    )
    provider = MagicMock()
    provider.search_rates = AsyncMock(return_value=[])
    provider.close = AsyncMock()
    provider._page = None
    sessions_close = AsyncMock()
    monkeypatch.setattr(smoke, "IHGBrowserProvider", MagicMock(return_value=provider))
    monkeypatch.setattr(smoke, "close_browser_sessions", sessions_close)
    if failure == "quote":
        provider.search_rates.side_effect = ProviderError(ErrorCode.RATE_LIMITED, "IHG returned HTTP 429")
    if failure == "page_cleanup":
        provider.close.side_effect = RuntimeError("page cleanup failed")
    if failure == "session_cleanup":
        sessions_close.side_effect = RuntimeError("session cleanup failed")
    if failure:
        with pytest.raises(ProviderError if failure == "quote" else RuntimeError):
            await smoke.main()
    else:
        await smoke.main()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == ("FAILED" if failure else "SUCCEEDED")
    assert report["quote_status"] == ("FAILED" if failure == "quote" else "SUCCEEDED")
    assert report["cleanup_status"] == (
        "FAILED" if failure in {"page_cleanup", "session_cleanup"} else "SUCCEEDED"
    )
    sessions_close.assert_awaited_once()
    assert ("rates" in report) == (failure != "quote")


async def test_smoke_refuses_to_overwrite_existing_evidence(monkeypatch, tmp_path):
    report_path = tmp_path / "evidence.json"
    report_path.write_text("previous evidence", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "smoke",
            "--official-url",
            "https://www.ihg.com/avidhotels/hotels/us/en/kingman/igmav/hoteldetail",
            "--date",
            date.today().isoformat(),
            "--report",
            str(report_path),
        ],
    )
    provider = MagicMock()
    provider.search_rates = AsyncMock()
    monkeypatch.setattr(smoke, "IHGBrowserProvider", MagicMock(return_value=provider))
    with pytest.raises(FileExistsError):
        await smoke.main()
    provider.search_rates.assert_not_awaited()
    assert report_path.read_text(encoding="utf-8") == "previous evidence"
