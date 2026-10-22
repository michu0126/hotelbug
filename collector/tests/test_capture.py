import json
from types import SimpleNamespace

from scripts.capture_har import JsonSink


class Response:
    def __init__(self, body, content_type="application/json", fail=False):
        self.headers = {"content-type": content_type}
        self.request = SimpleNamespace(resource_type="xhr", method="POST")
        self.status = 200
        self.url = "https://www.hilton.com/offline-fixture"
        self.data, self.fail = body, fail

    async def body(self):
        if self.fail:
            raise RuntimeError("sensitive request details must not be logged")
        return self.data


async def test_all_json_responses_are_saved_verbatim_including_duplicate_and_text_mime(tmp_path):
    sink = JsonSink(tmp_path)
    for response in (Response(b'{"price":60}'), Response(b'{"price":60}'), Response(b"[1,2]", "text/plain")):
        sink.schedule(response)
    rows = await sink.finish()
    assert len(rows) == 3
    assert [(tmp_path / row["file"]).read_bytes() for row in rows] == [
        b'{"price":60}',
        b'{"price":60}',
        b"[1,2]",
    ]
    assert len(json.loads((tmp_path / "manifest.json").read_text())) == 3


async def test_missing_and_invalid_json_are_recorded_not_silently_ignored(tmp_path):
    sink = JsonSink(tmp_path)
    sink.schedule(Response(b"{bad-json}"))
    sink.schedule(Response(b"", fail=True))
    rows = await sink.finish()
    assert rows[0]["valid_json"] is False
    assert rows[1]["file"] is None and rows[1]["error"] == "RuntimeError"
    assert "sensitive" not in (tmp_path / "manifest.json").read_text()
