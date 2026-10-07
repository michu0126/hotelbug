import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from audit_ihg_price import install_stay_diagnostics

from app.core.errors import ErrorCode, ProviderError
from app.providers.registry import FACTORIES
from app.schemas.domain import RateRequest


@pytest.mark.parametrize("diagnostic_fails", [False, True])
@pytest.mark.parametrize(
    "foreign_url,state",
    [("https://example.com/private", "OTHER_ORIGIN"), ("about:blank", "UNCOMMITTED_BLANK")],
)
async def test_same_hilton_journey_is_read_once_and_diagnostics_preserve_original_error(
    monkeypatch, capsys, diagnostic_fails, foreign_url, state
):
    instance = MagicMock()
    page = instance._page
    page.url = "https://www.hilton.com/en/?private=SECRET"
    page.is_closed.return_value = False
    instance.hotel_name = "Public hotel name"
    foreign = MagicMock()
    foreign.url = foreign_url
    foreign.is_closed.return_value = False
    instance._journey_pages = [page, page, foreign]
    selectors = []

    def locator(selector):
        selectors.append(selector)
        control = MagicMock()
        control.evaluate_all = AsyncMock(
            side_effect=RuntimeError("SECRET") if diagnostic_fails else None,
            return_value=[],
        )
        return control

    page.locator.side_effect = locator
    error = ProviderError(ErrorCode.TIMEOUT, "Recorded original failure")
    instance.search_rates = AsyncMock(side_effect=error)
    monkeypatch.setitem(FACTORIES, "hilton", lambda: instance)
    install_stay_diagnostics("hilton")
    with pytest.raises(ProviderError) as caught:
        await FACTORIES["hilton"]().search_rates(
            RateRequest(provider_hotel_id="AUSAQAQ", check_in="2026-10-20", check_out="2026-10-21")
        )
    assert caught.value is error
    output = capsys.readouterr().out
    assert "SECRET" not in output and len(output.splitlines()) == 2
    projected = json.loads(output.splitlines()[0])
    assert json.loads(output.splitlines()[1]) == {"public_hilton_page_state": state, "booking_stage": None}
    if diagnostic_fails:
        assert projected == {"public_hilton_stay_controls_unavailable": True}
    else:
        assert projected["public_hilton_stay_controls"]["page_path"] == "/en/"
        assert len(selectors) == 3 and "hotel-card-AUSAQAQ" in selectors[-1]
    page.goto.assert_not_called()
    foreign.locator.assert_not_called()
