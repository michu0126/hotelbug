"""Offline, credential-free extraction of observed Hilton room-card price samples."""

import argparse
import base64
import json
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

from adapters.hilton_rates import parse_public_rates
from adapters.hilton_samples import (
    ROOM_SEARCH_OPERATION,
    ROOM_SEARCH_PATH,
    SampleValidationError,
    parse_room_search,
    validate_search_context,
)
from adapters.hilton_urls import rooms_url
from models import Hotel, Stay


def response_json(entry):
    content = entry["response"]["content"]
    body = content["text"]
    if content.get("encoding") == "base64":
        body = base64.b64decode(body, validate=True).decode("utf-8")
    return json.loads(body)


def analyze(paths):
    report = {
        "files": [],
        "observations": [],
        "rejected": [],
        "network_requests": 0,
        "monitoring_history_writes": 0,
        "notifications": 0,
        "duplicate_observations": 0,
        "public_rate_quotes": [],
        "excluded_rates": [],
        "duplicate_public_rates": 0,
    }
    seen = {}
    seen_rates = {}
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        entries = document["log"]["entries"]
        file_result = {
            "file": path.name,
            "entries": len(entries),
            "room_searches": 0,
            "room_observations": 0,
            "public_rate_quotes": 0,
        }
        for index, entry in enumerate(entries):
            url = urlsplit(entry["request"]["url"])
            if url.scheme != "https" or url.netloc != "www.hilton.com" or url.path != ROOM_SEARCH_PATH:
                continue
            try:
                request = json.loads(entry["request"].get("postData", {}).get("text", ""))
            except ValueError:
                continue
            if not isinstance(request, dict) or request.get("operationName") != ROOM_SEARCH_OPERATION:
                continue
            file_result["room_searches"] += 1
            try:
                if entry["request"]["method"] != "POST" or entry["response"]["status"] != 200:
                    raise SampleValidationError("Room-price request did not succeed")
                started = datetime.fromisoformat(entry["startedDateTime"].replace("Z", "+00:00"))
                if started.tzinfo is None:
                    raise SampleValidationError("Capture timestamp lacks a timezone")
                variables = request.get("variables", {})
                response = response_json(entry)
                context = validate_search_context(variables, response)
                if variables.get("selectedRoomTypeCode"):
                    identity, _, check_in, check_out, adults, rooms, _, _, _ = context
                    stay = Stay(check_in, check_out, adults, rooms)
                    source_url = rooms_url(identity, stay)
                    hotel = Hotel("hilton", identity, identity, source_url)
                    parsed = parse_public_rates(
                        variables, response, hotel, stay, observed_at=started, source_url=source_url
                    )
                    file_result["public_rate_quotes"] += len(parsed.quotes)
                    for quote in parsed.quotes:
                        item = quote.to_dict()
                        item.pop("observation_id")  # A repeated export is not a new independent query.
                        key = json.dumps(item, sort_keys=True, ensure_ascii=True)
                        source = {"file": path.name, "entry": index}
                        if key in seen_rates:
                            seen_rates[key]["sources"].append(source)
                            report["duplicate_public_rates"] += 1
                        else:
                            item.update(
                                observation_id="HAR:" + sha256(key.encode()).hexdigest(), sources=[source]
                            )
                            seen_rates[key] = item
                            report["public_rate_quotes"].append(item)
                    report["excluded_rates"].extend(parsed.excluded)
                    continue
                observations = parse_room_search(variables, response)
            except (SampleValidationError, KeyError, ValueError, TypeError) as exc:
                report["rejected"].append(
                    {
                        "file": path.name,
                        "entry": index,
                        "reason": str(exc)
                        if isinstance(exc, SampleValidationError)
                        else "Invalid or incomplete captured response",
                    }
                )
                continue
            file_result["room_observations"] += len(observations)
            for observation in observations:
                item = {**observation.to_dict(), "request_started_at": started.isoformat()}
                key = json.dumps(item, sort_keys=True, ensure_ascii=True)
                source = {"file": path.name, "entry": index}
                if key in seen:
                    seen[key]["sources"].append(source)
                    report["duplicate_observations"] += 1
                else:
                    seen[key] = {**item, "sources": [source]}
                    report["observations"].append(seen[key])
        report["files"].append(file_result)
    return report


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=Path("data/hilton-har-analysis.json"))
    args = parser.parse_args()
    report = analyze(args.files)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "unique_observations": len(report["observations"]),
                "duplicates": report["duplicate_observations"],
                "rejected": len(report["rejected"]),
                "parsed_public_rate_quotes": len(report["public_rate_quotes"]),
                "historical_quotes_sent_to_monitor": 0,
                "network_requests": 0,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    cli()
