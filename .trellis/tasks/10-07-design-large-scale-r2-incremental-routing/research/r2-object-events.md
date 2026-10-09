# R2 object-event discovery for incremental routing

**Task:** `10-07-design-large-scale-r2-incremental-routing`
**Scope:** Research and design only. No application code was changed and no real R2 operation was executed.

## Executive recommendation

Use **Cloudflare R2 `object-create` event notifications delivered to a Cloudflare Queue, consumed by the existing container through the Queue HTTP pull API**, with a durable local object/version index and a slower polling reconciliation loop as a correctness backstop.

This is the best fit for the current single-container FastAPI deployment because the container can make outbound HTTPS calls without adding an inbound webhook or a Cloudflare Worker. It provides low, asynchronous detection latency and Queue-level at-least-once delivery, while the reconciler handles notification configuration gaps, expired messages, and operational outages. It is not an exactly-once design: every processing step must be idempotent.

Do **not** treat an R2 event as sufficient to route an object by itself. The notification contains the bucket, key, size, ETag, action, and event time, but not the model paths inside a `.tar.gz`. Unless the producer starts publishing trustworthy model metadata, each new archive still requires one object read and archive-member inspection. The event path removes repeated full-bucket archive reads; it does not remove the archive read for each newly discovered archive.

## Repository baseline and constraints

The current repository has the following relevant behavior:

- `backend/app.py::run_scan_job` lists every configured source bucket with `ListObjectsV2`, then scans every listed object.
- `backend/scanner.py` skips non-`.tar.gz` keys and reads each archive stream to extract `roots/primary/<model-name>/...` member paths. A per-object timeout and bounded transport retry policy already exist.
- `backend/sync.py` creates deduplicated actions and, only after explicit administrator sync/preflight flow, checks the target and uses server-side `CopyObject`. Existing target keys are skipped; the intended safety boundary is no overwrite, no delete, and no bucket creation.
- `backend/storage.py` persists credential-free jobs, reports, and mappings as JSON under the mounted data directory. It has atomic file replacement but no per-object event/index store or transactional work queue.
- `backend/app.py` runs one process-local scan task and one process-local sync task. The deployment intentionally runs one Uvicorn process. On restart, active jobs are marked interrupted and R2 connection credentials are lost because they are kept only in process memory.
- The Compose deployment mounts `/var/lib/r2-model-scanner` as durable storage, but currently stores no R2 credentials there.

These constraints mean that an unattended event consumer will need a deliberate credential lifecycle (startup-injected secret or an explicit reconnect/pause state) and durable object/work state. It must not put R2 access keys, Queue API tokens, or other secrets in the existing JSON reports.

## What Cloudflare provides

### Native R2 event notifications

R2 event notifications are native bucket rules that send messages to a **Cloudflare Queue** when an object changes. They are not a direct arbitrary-URL webhook mechanism.

Relevant official documentation:

- [R2 Event notifications](https://developers.cloudflare.com/r2/buckets/event-notifications/)
- [R2 S3 API compatibility](https://developers.cloudflare.com/r2/api/s3/api/)
- [R2 consistency model](https://developers.cloudflare.com/r2/reference/consistency/)

The `object-create` event is emitted for:

- `PutObject`
- `CopyObject`
- `CompleteMultipartUpload`

It means “new object or existing object overwritten,” not only a never-before-seen key. `object-delete` is a separate event type and is not needed for the current append/routing path unless deletion reconciliation is added later. A suffix/prefix filter can limit notifications, so the initial rule should normally select `object-create` and `.tar.gz` (and any known source prefix). There may be up to 100 event-notification rules per bucket.

The documented event body includes:

```json
{
  "account": "...",
  "action": "PutObject",
  "bucket": "source-bucket",
  "object": {
    "key": "archive.tar.gz",
    "size": 65536,
    "eTag": "..."
  },
  "eventTime": "2024-05-24T19:36:44.379Z"
}
```

The exact property names and optional `copySource` field are documented on the R2 event-notification page. No archive member list, model name, or application routing decision is included.

### Cloudflare Queues delivery

R2 publishes into Queues; the Queue is the delivery/retry boundary. Official references:

- [Cloudflare Queues](https://developers.cloudflare.com/queues/)
- [Delivery guarantees](https://developers.cloudflare.com/queues/reference/delivery-guarantees/)
- [How Queues works](https://developers.cloudflare.com/queues/reference/how-queues-works/)
- [Batching, retries, and delays](https://developers.cloudflare.com/queues/configuration/batching-retries/)
- [Pull consumers](https://developers.cloudflare.com/queues/configuration/pull-consumers/)
- [Queue limits](https://developers.cloudflare.com/queues/platform/limits/)
- [Dead-letter queues](https://developers.cloudflare.com/queues/configuration/dead-letter-queues/)
- [Queue metrics](https://developers.cloudflare.com/queues/observability/metrics/)

The important semantics are:

- Queues provides **at-least-once** delivery. A message can be delivered more than once; exactly-once processing is the application's responsibility.
- Queue delivery order is not guaranteed. Processing must not rely on event order.
- A consumer can acknowledge individual messages or retry them. A failed delivery is retried three times by default; `max_retries` is configurable.
- After the retry limit, a message is deleted unless a dead-letter queue is configured. A DLQ is therefore required for an auditable failure path.
- Queue messages have configurable retention up to 14 days on the documented paid limits; the documented free-plan retention is 24 hours. Messages at the retention limit are deleted. This is a recovery window, not permanent event storage.
- Queue throughput is documented as 5,000 messages/second per queue. A source workload above that needs multiple queues/rules or a different design.
- A single failed message can cause a whole delivered batch to be retried unless individual messages are explicitly acknowledged. Acknowledging after durable local persistence, rather than after the archive scan/copy, reduces redelivery of already accepted work.

The R2 event body shown in the official documentation does not expose an application-level unique event ID. Do not design deduplication around an undocumented Queue message identifier. Use an object-version identity such as `(source_bucket, object_key, eTag, size)` and retain event time/action as diagnostic fields. If a future API exposes a stable message ID, it can be stored as an additional diagnostic/idempotency key, not as the only correctness key.

## Delivery choices for this application

| Choice | Detection path and latency | Duplicate/loss behavior | Operational requirements | Fit to this repository |
| --- | --- | --- | --- | --- |
| **R2 event notification -> Queue -> Worker consumer** | Queue invokes a Cloudflare Worker. Push is asynchronous and auto-scales. The Queue docs expose a default maximum batch wait of 5 seconds (configurable 0–60 seconds), but Cloudflare does not state a hard R2 event-generation latency target in the cited R2 docs. | At-least-once, unordered. Retry/DLQ behavior is available. A Worker failure must leave the message unacknowledged/retried. | R2 rule, Queue, Worker project/binding, consumer configuration, DLQ, monitoring, and a secure Worker-to-FastAPI call if the container is the processor. | Reliable, but adds a second deployable runtime and a webhook-like bridge. It is not the minimal single-container choice. |
| **R2 event notification -> Queue -> HTTP pull from FastAPI** | The container pulls over outbound HTTPS. Pull returns immediately when messages are available; an empty pull returns immediately rather than holding a long-poll connection. Detection delay is therefore pull-loop/backoff delay plus Queue/R2 asynchronous delivery. | At-least-once. The container must acknowledge only after durable acceptance, and retry/leave unacknowledged on failure. Pull uses a visibility timeout (default 30 seconds, maximum 12 hours); a too-short lease can redeliver while work is still running. | Queue API token with both `queues#read` and `queues#write` because acknowledgement mutates Queue state; one lifecycle-managed puller; DLQ; outbound connectivity; durable pending/index state; metrics and restart recovery. | **Recommended.** No inbound public endpoint or Worker is required, and it fits one Uvicorn process if concurrency is kept bounded. Credentials and lifecycle recovery must be redesigned before unattended operation. |
| **Direct R2 webhook** | No native direct arbitrary-HTTP webhook is documented for R2 object notifications. The native destination is a Queue. | A custom HTTP endpoint has no R2 event-delivery guarantee by itself. A Worker can forward Queue messages and only return success/ack after the FastAPI endpoint durably accepts them, but then Worker/HTTP retry semantics must also be designed. | Public HTTPS endpoint, authentication/signature, replay protection, timeout/retry policy, and a Worker bridge if Queue delivery is retained. | Not recommended as the primary path. It adds an ingress surface and a second retry protocol without improving the Queue's durability. |
| **Direct periodic polling with `ListObjectsV2`** | The current app already lists all keys. A poll interval bounds discovery delay approximately by `poll interval + list duration`; a large bucket can make each reconciliation expensive. R2 documents strong consistency for object listing, but no change cursor or event offset is provided by the S3 API. | No provider event can be lost because the next reconciliation observes current state. Poll failure is retried. A key-only index misses overwrites, so compare a version fingerprint (ETag/size and, where needed, a metadata HEAD). | Durable seen/version index, paginated list retries, periodic full reconciliation, alerting on stale poll, and a policy for deletion/overwrite. | **Required fallback/backstop.** It is simpler and dependency-light but too expensive as the sole high-frequency detector at 1–2 TB. |
| **Upstream producer metadata** | The uploader can put model name/version in a trusted key prefix or metadata record and emit a task directly. Latency depends on the producer. | Depends on the producer's retry/idempotency contract. | Producer changes, schema/versioning, authentication, and a source-of-truth policy. | Useful optimization, but not available in the current contract because model names are inside the archive. Do not bypass archive inspection using untrusted metadata. |

## Latency and cost implications

### Current full scan

For each source bucket, the current scan pays for:

1. Every page of object listing, even for objects already processed.
2. One archive read for every `.tar.gz`, even for an unchanged object. Model extraction is streaming, but it must inspect archive members; the worst case is a read through the archive.
3. Later, explicit target checks and server-side copies for selected objects. `CopyObject` avoids sending the complete archive through the FastAPI process, but it still consumes provider operations and target storage bandwidth internally.

At 1–2 TB, the dominant avoidable cost is repeated archive reads and the wall time/timeout/retry exposure, not merely the list metadata calls.

### Event-driven incremental scan

For each matching create/overwrite event, the expected work is:

1. Receive a small Queue message.
2. `HEAD`/metadata validation if needed.
3. Read the one new/changed archive and extract model names.
4. Persist model associations and enqueue one or more deduplicated routing actions.
5. Perform the existing explicit, non-overwriting server-side copy only when routing policy/mapping permits.

The event path therefore changes the recurring cost from “scan every historical archive” to “scan only event-selected candidates plus periodic metadata reconciliation.” It does not make archive-content model detection free.

Cloudflare's cited documentation does **not** promise a fixed R2 notification latency. Queue batching provides a configurable delivery wait bound after a message is available (default maximum 5 seconds for push consumer batches; up to 60 seconds), while pull latency is controlled by the pull loop. Any product requirement should therefore say “asynchronous, normally seconds under no backlog,” not promise a hard sub-second SLA.

## Handling duplicates, lost events, overwrites, and restarts

### Durable identity and state

A future incremental index should be keyed at least by:

```text
(source_bucket, object_key, object_version_fingerprint)
```

Use the event's ETag and size as the initial fingerprint. Store `event_time`, `action`, observed size/ETag, and timestamps for diagnostics. If a later `object-create` event has a different fingerprint for the same key, it is a new version candidate; do not collapse it into the old key-only record.

Suggested state transitions are:

```text
received -> accepted -> scanning -> classified -> pending_route -> copied
                                      |-> unmatched/no mapping
                                      |-> failed_retryable -> retry_wait
                                      |-> failed_terminal
                                      |-> skipped_existing_target
```

Persist attempts, next retry time, classification, and a safe error category. Never persist credentials or raw provider error bodies.

A duplicate event for a completed fingerprint should become a no-op. A duplicate while work is pending should be coalesced. A failed retryable item should remain durable after process restart. A malformed archive should be terminal for that fingerprint, matching the current scanner's corruption policy; transport failures and timeouts remain retryable.

For a high-volume index, use a transactional per-object store (SQLite on the existing named volume is a reasonable single-container MVP) rather than repeatedly rewriting one large JSON array. The current JSON storage convention can continue for user-facing reports and mappings, but it is not a sufficient work queue/index by itself at large object counts.

### Event acknowledgement boundary

With HTTP pull:

1. Pull a batch with a visibility timeout longer than the short ingestion operation, not longer than the full 120-second archive scan.
2. Validate the event and durably insert/coalesce the work item.
3. Acknowledge that message after the durable insert succeeds.
4. Process the durable work item independently; retry it in the local index when the R2 read or routing action fails.

If the application acknowledges before durable acceptance, a crash can lose the event. If it holds the Queue lease while performing a 120-second archive scan, the lease can expire and produce duplicates. The split between “Queue ingestion” and “archive processing” is important.

For a Worker push consumer, use the same boundary: the Worker should only successfully complete/acknowledge after it has durably forwarded the event to the FastAPI endpoint or another durable store. A webhook response of HTTP 2xx should mean “persisted,” not merely “request body parsed.”

### Missing events

Neither the R2 event-notification page nor the Queue pages cited here provide a replay cursor for an R2 bucket's complete object event history. Events can be missed when:

- the notification rule is absent, disabled, or misconfigured;
- Queue retention expires before recovery;
- a message reaches `max_retries` without a DLQ;
- the consumer acknowledges before persistence;
- deployment/restart handling drops local pending work; or
- an outage lasts longer than the retention window.

The mitigation is a periodic reconciliation using `ListObjectsV2`, comparing each listed object to the durable fingerprint index and enqueuing unseen or changed versions. R2 documents strong consistency for list operations, so the reconciler can observe the current state without relying on eventual list propagation. It still needs repeated runs because a paginated reconciliation is not an event-history replay and writes can continue while pages are being processed.

### Overwrites and stale events

`object-create` includes overwrites. Events can arrive out of order when the same key is written repeatedly. Before scanning, re-read current metadata and, where the SDK path supports it, use a conditional read (`If-Match` against the event ETag). If the object has changed, process the current version as a new candidate rather than associating the archive content with a stale event.

The existing safety rule is significant: target objects are not overwritten. If version B arrives after version A was already copied to the target key, the incremental worker must record `skipped_existing_target`/`source_changed_requires_review` rather than silently replacing the target. Changing that rule would be a separate product/security decision, not an incidental consequence of event support.

### Restart and credential recovery

The current app deliberately loses R2 credentials at restart. That is compatible with an administrator-triggered scan, but not with an unattended event consumer. Before enabling continuous processing, choose one of these explicit modes:

- inject R2 credentials and the Queue pull token through the deployment secret mechanism at startup;
- keep the puller paused until an administrator re-enters the connection and then resume durable pending work; or
- integrate an external secret manager.

Do not write either R2 secret keys or Queue API tokens to `/var/lib/r2-model-scanner`. If credentials are unavailable, the consumer should not acknowledge work that cannot be durably processed; it should pause or retain the Queue message within the documented retention/retry policy and alert.

## Concrete design for this single-container deployment

The future topology should be:

```text
R2 source bucket(s)
    | object-create rule, optional prefix/suffix filter
    v
Cloudflare Queue + DLQ
    | outbound HTTPS HTTP-pull consumer
    v
FastAPI process: event ingestor
    | durable insert/coalesce, then acknowledge
    v
Durable object/version index + pending work
    | one bounded local worker
    v
existing archive scanner -> persisted model association -> saved mapping lookup
    | explicit routing only
    v
existing target preflight / non-overwriting server-side CopyObject

Periodic ListObjectsV2 reconciler ------------------------------^
```

Recommended operating limits for the MVP:

- One puller and one bounded archive-processing worker in the one Uvicorn process, preserving the current process-local single-worker assumption.
- Use Queue batching for ingestion, but keep archive processing outside the Queue lease.
- Back off empty HTTP pulls because an empty pull is still a Queue read operation; use a short active interval and a larger idle interval, with a maximum delay appropriate to the required freshness.
- Configure an R2 event rule per source bucket (or shared Queue where supported), filtered to `.tar.gz` and relevant prefixes.
- Configure a DLQ and alert on non-zero DLQ messages, Queue backlog, retry count, and oldest-message age. Queue metrics expose backlog, consumer concurrency, operations, and retries through the documented dashboard/GraphQL/REST surfaces.
- Run a periodic full metadata reconciliation even when the event path appears healthy. The interval should be chosen from the tolerated recovery gap and list cost; it should be much slower than event consumption (for example, hourly or daily after measuring object count and list duration).
- Preserve the current archive timeout/retry/corruption classifications and the explicit mapping/preflight/copy boundary.
- Treat missing `(source bucket, model)` mappings as durable `unmatched/no mapping` work, not as permission to create a target bucket or guess a destination.

### Future repository change areas (not implemented in this research task)

- `backend/r2_client.py`: expose the metadata needed for version comparison and, if chosen, conditional reads; retain read-only behavior for discovery.
- `backend/scanner.py`: reuse archive classification for event candidates; no archive extraction to disk is needed.
- `backend/storage.py`: add a durable object/version index and pending-work records, preferably transactionally; continue to keep reports credential-free.
- `backend/app.py`: add lifecycle-managed ingestion/reconciliation workers, restart recovery, backpressure, and explicit connection/secret readiness states. Do not let a restart claim work completed.
- `backend/sync.py`: reuse deduplicated actions and make the existing no-overwrite behavior explicit for source-version changes.
- Deployment configuration/docs: supply Queue and R2 runtime secrets without putting them in the data volume; document the single-process limitation and the Queue/DLQ resources.

No change to these modules is part of the current research deliverable.

## Staged rollout plan

1. **Prepare infrastructure:** create a Queue and DLQ, configure source-bucket `object-create` rules with archive filters, create the least-privilege Queue pull token, and add backlog/retry/DLQ alerts. Do not run these operations as part of this task.
2. **Build the state model:** add a durable version-keyed index and pending work states. Establish the idempotency and overwrite policy in tests before enabling production events.
3. **Backfill with overlap:** enable notifications before the historical backfill starts. Run the existing full read-only scan to seed historical fingerprints and model associations while the Queue retains new events. Drain/coalesce events after the baseline and reconcile the bucket to close the overlap gap. If the backfill can exceed Queue retention, the reconciler remains mandatory rather than relying on Queue backlog.
4. **Enable incremental processing:** start the puller and bounded worker. Acknowledge only after durable event acceptance; process new archives and route only with saved mappings and the existing explicit copy boundary.
5. **Operate with reconciliation:** keep periodic `ListObjectsV2` comparison enabled, monitor queue age/backlog/retries/DLQ and local pending states, and periodically verify that an event-injected sample is visible in the index and report.
6. **Only then reduce full scans:** retain an operator-triggered full scan for recovery and audits, but stop using it as the normal path for every incremental update.

## Decision summary

| Requirement | Decision |
| --- | --- |
| Discover newly created archives quickly | R2 `object-create` -> Queue; HTTP pull from container |
| Avoid repeated archive reads | Durable `(bucket,key,version)` index; scan only unseen/changed candidates |
| Model is only inside `.tar.gz` | Keep one archive read per new/changed archive; event metadata alone is insufficient |
| Duplicate events | At-least-once design; coalesce by object-version fingerprint and make all states idempotent |
| Lost/expired events | Queue DLQ plus periodic strong-consistency `ListObjectsV2` reconciliation |
| Overwrites | Detect changed fingerprint; preserve no target overwrite and report review-needed source changes |
| Single-container deployment | One puller/worker in the process; outbound pull avoids a public webhook and Worker bridge |
| Restart | Durable pending/index state plus an explicit startup credential/readiness policy; do not acknowledge work that cannot be recovered |
| Security boundary | No credentials in reports/data volume; no bucket creation, source deletion, or implicit target overwrite |

## Source URLs

1. [Cloudflare R2 Event notifications](https://developers.cloudflare.com/r2/buckets/event-notifications/)
2. [Cloudflare Queues](https://developers.cloudflare.com/queues/)
3. [Queues delivery guarantees](https://developers.cloudflare.com/queues/reference/delivery-guarantees/)
4. [How Queues works](https://developers.cloudflare.com/queues/reference/how-queues-works/)
5. [Queues batching, retries, and delays](https://developers.cloudflare.com/queues/configuration/batching-retries/)
6. [Queues HTTP pull consumers](https://developers.cloudflare.com/queues/configuration/pull-consumers/)
7. [Queues limits](https://developers.cloudflare.com/queues/platform/limits/)
8. [Queues dead-letter queues](https://developers.cloudflare.com/queues/configuration/dead-letter-queues/)
9. [Queues metrics](https://developers.cloudflare.com/queues/observability/metrics/)
10. [R2 S3 API compatibility](https://developers.cloudflare.com/r2/api/s3/api/)
11. [R2 consistency model](https://developers.cloudflare.com/r2/reference/consistency/)
12. [Queues configuration](https://developers.cloudflare.com/queues/configuration/configure-queues/)
