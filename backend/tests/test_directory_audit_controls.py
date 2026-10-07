import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from audit_ihg_catalog import install_directory_diagnostics, public_ihg_directory_controls

from app.core.errors import ErrorCode, ProviderError
from app.providers.registry import FACTORIES


def test_directory_controls_only_project_public_fields():
    html = """<h1>Example Hotels</h1><h2>Featured Hotels in Example</h2>
        <div data-render-hotel-count="true"><span id="cmp-card__title-bar-count">20</span></div>
        <h4><a href="https://www.ihg.com/example?private=SECRET">Hotel</a></h4>
        <button class="more-hotels" aria-label="More hotels">Show More</button>
        <button disabled data-testid="next-page">Next</button>
        <input value="SECRET"><script>SECRET</script><button>Sign in</button>"""
    result = public_ihg_directory_controls(html)
    assert result["counter"] == "20" and result["hotel_heading_links"] == 1
    assert result["featured_headings"] == ["Featured Hotels in Example"]
    assert [button["text"] for button in result["pagination_controls"]] == ["Show More", "Next"]
    assert result["pagination_controls"][1]["disabled"] is True
    assert "SECRET" not in json.dumps(result) and "Sign in" not in json.dumps(result)
    assert result["unsupported_hotel_links"] == [
        {
            "name": "Hotel",
            "scheme": "https",
            "hostname": "www.ihg.com",
            "path": "/example",
            "has_query": True,
            "has_fragment": False,
        }
    ]


@pytest.mark.parametrize("diagnostic_fails", [False, True])
async def test_same_page_diagnostic_preserves_original_error_and_never_navigates(
    monkeypatch, capsys, diagnostic_fails
):
    instance = MagicMock()
    page = instance._page
    page.url = "https://www.ihg.com/bangladesh"
    page.is_closed.return_value = False
    page.content = AsyncMock(
        return_value="<h1>Bangladesh Hotels</h1>",
        side_effect=RuntimeError("SECRET") if diagnostic_fails else None,
    )
    error = ProviderError(ErrorCode.PROVIDER_CHANGED, "Original directory error")
    instance.discover_catalog = AsyncMock(side_effect=error)
    monkeypatch.setitem(FACTORIES, "ihg", lambda: instance)
    install_directory_diagnostics("ihg")
    with pytest.raises(ProviderError) as raised:
        await FACTORIES["ihg"]().discover_catalog({"official_url": page.url})
    assert raised.value is error
    output = capsys.readouterr().out
    assert "SECRET" not in output
    result = json.loads(output)
    if diagnostic_fails:
        assert result == {"public_ihg_directory_controls_unavailable": True}
    else:
        assert result["public_ihg_directory_source"] == page.url
        assert result["public_ihg_directory_controls"]["heading"] == "Bangladesh Hotels"
    page.goto.assert_not_called()
