"""Audit only the currently advertised, rendered public directory graph.

Hotel counts are deduplicated by official identity, never summed across cities.
Complete traversal without a published worldwide total is not global coverage.
"""

from datetime import datetime, timedelta


def directory_coverage(data: dict, root: str, now: datetime, freshness_days: int) -> dict:
    pages = data.get("pages", {})
    pending = [root]
    visited = set()
    hotels = set()
    complete = failed = stale = unverified = waiting = redirected = 0
    cutoff = (now - timedelta(days=freshness_days)).timestamp()
    while pending:
        url = pending.pop()
        if url in visited:
            continue
        visited.add(url)
        info = pages.get(url, {})
        children, identities = info.get("directory_urls"), info.get("hotel_ids")
        metadata = (
            isinstance(children, list)
            and all(isinstance(item, str) for item in children)
            and isinstance(identities, list)
            and all(isinstance(item, str) and item for item in identities)
        )
        root_redirect = (
            url != root
            and info.get("status") == "FAILED"
            and info.get("last_error") == "DIRECTORY_REDIRECT"
            and info.get("redirected_to") == root
        )
        if root_redirect:
            redirected += 1
            pending.append(root)
        elif metadata:
            pending.extend(children)
        status = info.get("status")
        if status == "FAILED":
            failed += 1
        elif status not in {"SUCCEEDED", "PARTIAL"}:
            waiting += 1
        elif not metadata:
            # Old rows have no graph or per-page identities. Do not infer them
            # from cumulative accepted URLs, which may be stale or overlapping.
            unverified += 1
        else:
            observed = info.get("last_scan_at")
            fresh = isinstance(observed, (int, float)) and cutoff < observed <= now.timestamp()
            if not fresh:
                stale += 1
            else:
                hotels.update(identities)
                if info.get("page_complete") is True:
                    complete += 1
                else:
                    unverified += 1
    root_info = pages.get(root, {})
    total = root_info.get("reported_total")
    total = total if type(total) is int and total >= 0 else None
    if failed:
        status = "FAILED_PAGES"
    elif waiting:
        status = "NOT_STARTED" if not root_info.get("last_scan_at") else "IN_PROGRESS"
    elif unverified:
        status = "UNVERIFIED_PAGES"
    elif stale:
        status = "STALE_PAGES"
    elif total is None:
        status = "TRAVERSED_TOTAL_UNVERIFIED"
    elif len(hotels) != total:
        status = "COUNT_MISMATCH"
    else:
        status = "FULL_CATALOG_VERIFIED"
    return {
        "catalog_coverage_status": status,
        "catalog_coverage_complete": status == "FULL_CATALOG_VERIFIED",
        "reachable_directory_pages": len(visited),
        "complete_directory_pages": complete,
        "pending_directory_pages": waiting,
        "unverified_directory_pages": unverified,
        "stale_directory_pages": stale,
        "reachable_failed_directory_pages": failed,
        "redirected_directory_pages": redirected,
        "directory_unique_hotels": len(hotels),
        "official_worldwide_hotels": total,
    }
