# INC-2025-0412 — Elevated 502s on task sync

# Elevated 502s on task sync

 SEV-2 Resolved Duration 47 min Detected Apr 12 · 14:07 Owner Devon Park 

TL;DR

 A config rollout lowered the database connection-pool limit on the `sync-worker` tier from 64 to 8, exhausting connections under normal afternoon load. The sync API returned 502s for roughly 21% of requests over 47 minutes. We mitigated by reverting the config and cycling the worker fleet; no data was lost. 

## Timeline

14:02

 Config change `cfg-9a12` promoted to production via the standard rollout pipeline. 

14:06

 Impact starts. Sync workers begin queueing on pool checkout; p95 latency climbs past 4s and the load balancer starts returning 502s. 

14:07

 Alert fires: `sync_5xx_rate > 2%` for 60s. On-call (Devon) acknowledges. 

14:18

 Initial hypothesis is a bad deploy of the API service; last two application deploys are rolled back with no effect. 

14:31

 Mira joins and notices pool-wait saturation in the worker dashboard. Investigation pivots to infra config rather than app code. 

14:44

 Mitigated. `cfg-9a12` reverted; worker fleet cycled. 5xx rate drops below 0.2% within three minutes. 

14:49

 Monitors green for 5 minutes. Incident declared resolved; status page updated. 

## Root cause

 PR `#4888` made connection-pool limits configurable per worker tier. The default for the new `sync-worker` key was meant to inherit the global value (64) but was hard-coded to 8 during a local test and committed. The config linter only validates type, not magnitude, so the change passed CI. 

 Because config rollouts and code deploys go through separate pipelines, the on-call's first instinct — rolling back the most recent application deploys — had no effect and cost roughly 13 minutes of diagnosis time. 

infra/config/workers.yaml

 pool:

 global_max_connections: 64

 tiers:

- sync-worker: { max_connections: 64 }

+ sync-worker: { max_connections: 8 } # debug value, do not ship

 webhook-worker: { max_connections: 32 }

## Impact

| Requests failed (502) | ~41,200 |
| --- | --- |
| Peak error rate | 21.4% |
| Users affected | ~2,300 workspaces |
| Data loss | None — clients retried |
| SLA breach | No (within monthly budget) |

## Action items

 DP Revert cfg-9a12 and restore pool limit to 64 Apr 12 

 MO Add config-linter range check for `max_connections` (warn < 32) Apr 18 

 SR Surface "recent config rollouts" alongside deploys in the on-call dashboard Apr 25 

 PA Canary config changes to one worker AZ for 10 min before fleet-wide promote May 02