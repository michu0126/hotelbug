# Marriott country-directory parser release — 2026-10-18

## Verified fix

- GitHub revision: `b03e5974d3a486952650b61145462fc8e590c98f`.
- Local corresponding revision: `a6d857b`.
- A normal Marriott CN country-directory request now uses the observed single-field `DISTANCE ASC` sorting. The old parser only accepted a four-field ordering and rejected otherwise valid HTTP 200 directory responses.
- Accept both observed sorting forms; preserve all country, pagination, hotel identity, and completeness checks.
- Actual public Chrome response replay: old parser rejects; new parser accepts Bhutan's `PBHPR` and `PBHMD` (2/2 hotels).
- Local tests: 72 targeted tests passed; full suite 2,049 passed.
- GitHub workflow [39069005655](https://github.com/michu0126/hotelbug/actions/runs/39069005655): verification and all three image publications succeeded.

## NAS deployment scope

- Replace only the hotelbug Worker image with the immutable revision above. API, frontend, scheduler, PostgreSQL, Redis, and Container Manager are not restarted.
- Preserve concurrency 1, proxy, persistent browser sessions, Cookie connections, settings, and hotel/price history.
- Backup directory: `/volume1/docker/hotel/release-backup-b03e597-20261018`; original Compose, environment, and a validated PostgreSQL custom-format backup.
- Stale GHA browser IPC symlinks owned by the verified stopped Worker were moved (not deleted) into `/data/profile-lock-backup-b03e597-20261018`. Cookies and profile contents were retained.
- Deployment is not proof of global or 365-day completion. Production ingestion verification is recorded below when complete.

## Other real observations (not successful collection claims)

- One isolated NAS Marriott English quote API test returned HTTP 403. No quote was obtained, no automatic retry was made, and no Telegram request was sent.
- Normal Chrome Marriott CN calendar traffic exposed a public calendar response with 19 price intervals covering 34 dates, for one room and one adult. This response is research evidence only, not integrated into the two-adult production hint pipeline. Tax/member conditions remain unconfirmed; intervals are not final bookable room offers.
- Successful IHG/GHA paths were not changed or manually re-tested. Other groups' provider pauses were not force-cleared.

## Production verification

- Deployment completed successfully. New Worker container `f113514ecc5b` started at `2026-10-17T21:17:12Z` (October 18, 05:17 Shanghai); all six hotelbug containers are healthy. Other five container IDs and running lifetimes stayed unchanged.
- Worker parser SHA-256 is `a8cdfb801ac766448964b1d240ac9110f91757bafa177559331fe9699eadefd9`, identical to local source.
- All six saved connection revision IDs remained unchanged; no Cookie values were emitted in verification output.
- At `2026-10-17T21:22:50Z`, the normal Worker completed `DISCOVER_CATALOG` for Hungary successfully. SQL confirms **10 automatically ingested Marriott hotels**: BUDCY, BUDDX, BUDER, BUDFD, BUDFP, BUDHU, BUDKC, BUDLC, BUDWH, BUDXR. Marriott state is `ONLINE`, last error cleared.
- The scheduler generated a pending `FETCH_CALENDAR_HINTS` task and queued hotel health-check task for the new Marriott catalog. No manual hotel insertion was performed.
- At this verification point, Marriott price-history rows since the new Worker started are **0**. Directory success does not yet establish price/calendar collection success or global/year coverage.
- A single published Bhutan directory URL was submitted through the ordinary authenticated job API as a bounded validation (`ffaa79fc-052f-41cb-9858-1de78638e72c`). It was initially still queued when the independently scheduled Hungary job supplied production proof; no repeated validation submissions were made.
- Follow-up SQL at approximately `2026-10-17T21:24Z` confirms Bhutan hotels `PBHMD` and `PBHPR` also persisted, bringing Marriott to **12 hotels**. There are now **2 pending calendar-hint tasks**, one for each initial country batch. Marriott calendar-hint history is still **0** at this point.
- The Bhutan validation job is `SUCCEEDED` with no error. No manual price fetch was submitted to bypass normal queue order.
- The NAS frontend's proxied `/api/health/ready` endpoint returns HTTP 200 with `status=ready` after deployment.
