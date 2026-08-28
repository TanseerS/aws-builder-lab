# OpsPilot Architecture

How OpsPilot is built, and why each decision was made the way it was.

---

## 1. System architecture

OpsPilot is entirely serverless and entirely Terraform-managed. There is no VPC,
no container, no always-on compute.

```
                              ┌──────────────────┐
                              │   Web Browser    │
                              └────────┬─────────┘
                                       │ HTTPS
                    ┌──────────────────▼──────────────────┐
                    │  CloudFront (OAC) ──► S3 (private)  │  dashboard bundle
                    └──────────────────┬──────────────────┘
                                       │
                    ┌──────────────────▼──────────────────┐
                    │      API Gateway HTTP API           │
                    └──────────────────┬──────────────────┘
                          ┌────────────┴────────────┐
                          ▼                         ▼
                  ┌───────────────┐        ┌────────────────┐
                  │  API Lambda   │        │  Demo App      │  GET /demo/app
                  └───────┬───────┘        └────────────────┘
                          │
       ┌──────────────────┼──────────────────┬────────────────────┐
       ▼                  ▼                  ▼                    ▼
  DynamoDB            S3 (artifacts)    CloudWatch        Demo Controller
  (incidents)         (postmortems)     (alarm state)     (own IAM role)
```

### Component responsibilities

| Component | Responsibility | Never does |
| --- | --- | --- |
| API Lambda | Serve the dashboard's data, gate approval | Modify the demo function directly |
| Incident Detector | Turn one alarm transition into one incident | Investigate |
| Investigator | Collect evidence, correlate changes, call Bedrock | Change anything |
| Remediation | Execute one allowlisted action | Decide whether to act |
| Verifier | Determine whether recovery is real | Assume success |
| Postmortem | Write the incident up | Invent facts |
| Demo Controller | Inject and clear controlled failures | Touch non-Demo-Lab resources |
| Demo App | Be the thing that breaks | Know OpsPilot exists |
| Traffic Generator | Keep alarm datapoints flowing | Anything else |

The demo application deliberately does **not** import the OpsPilot shared layer.
It is the subject of the platform, not a part of it, and keeping that boundary
explicit prevents the demo from accidentally depending on OpsPilot internals.

---

## 2. Event flow

Stages are coupled through EventBridge, never through direct invocation. Any
stage can fail, be retried, or be replaced without the others knowing.

```
 CloudWatch Alarm ──────────────► default event bus
        │                              │
        │   detail-type:               │  filtered to this deployment's
        │   "CloudWatch Alarm          │  five alarms, state == ALARM
        │    State Change"             ▼
        │                     ┌──────────────────┐
        └────────────────────►│ Incident Detector│
                              └────────┬─────────┘
                                       │ conditional PutItem (idempotent)
                                       ▼
                                   DynamoDB
                                       │
                     "OpsPilot Incident Detected"
                                       │
                          opspilot-showcase-events bus
                                       ▼
                              ┌──────────────────┐
                              │   Investigator   │◄──── "Reinvestigation
                              └────────┬─────────┘        Requested"
                                       │
                    "OpsPilot Investigation Completed"
                                       │
                       ╔═══════════════▼═══════════════╗
                       ║   NO RULE ROUTES FROM HERE    ║
                       ║   TO REMEDIATION. The only    ║
                       ║   path forward is a human     ║
                       ║   calling POST .../approve.   ║
                       ╚═══════════════┬═══════════════╝
                                       │
                     "OpsPilot Remediation Approved"
                                       ▼
                              ┌──────────────────┐
                              │   Remediation    │
                              └────────┬─────────┘
                     "OpsPilot Remediation Completed"
                                       ▼
                              ┌──────────────────┐
                              │     Verifier     │
                              └────────┬─────────┘
                    "OpsPilot Verification Completed"
                                       ▼
                              ┌──────────────────┐
                              │   Postmortem     │──► S3 + DynamoDB
                              └──────────────────┘
```

### Why two buses

CloudWatch publishes alarm state changes only to the **default** bus, so the
ingress rule necessarily lives there — filtered to this deployment's five alarm
names so OpsPilot never reacts to unrelated alarms in the account.

Everything internal flows over the custom bus `opspilot-showcase-events`, which
keeps OpsPilot's own traffic isolated and makes its event vocabulary explicit.

### Retry policy per stage

| Rule | Retries | Reasoning |
| --- | --- | --- |
| Alarm → Detector | 3 | Idempotent; losing a detection loses the incident |
| Incident Detected → Investigator | 2 | Read-only; safe to repeat |
| Remediation Approved → Remediation | **0** | Mutates state. A failure moves the incident to FAILED for a human, rather than retrying a change blindly |
| Remediation Completed → Verifier | 1 | Read-only |
| Verification Completed → Postmortem | 2 | Idempotent overwrite of one S3 key |

---

## 3. Incident state machine

```
                        ┌───────────┐
                        │ DETECTED  │
                        └─────┬─────┘
                              ▼
                     ┌────────────────┐
              ┌─────►│ INVESTIGATING  │◄────────────┐
              │      └───────┬────────┘             │
              │              ▼                      │
              │  ┌────────────────────────┐         │  reinvestigate
              │  │ ROOT_CAUSE_IDENTIFIED  │         │  (from any
              │  └───────────┬────────────┘         │   non-active
              │              ▼                      │   state)
              │  ┌────────────────────────┐         │
              │  │   AWAITING_APPROVAL    │         │
              │  └───┬────────────────┬───┘         │
              │      │ approve        │ reject      │
              │      ▼                ▼             │
              │ ┌─────────────┐  ┌──────────┐       │
              │ │ REMEDIATING │  │  FAILED  │───────┤
              │ └──────┬──────┘  └──────────┘       │
              │        ▼                            │
              │  ┌────────────┐                     │
              │  │ VERIFYING  │                     │
              │  └─────┬──────┘                     │
              │        │                            │
              │   ┌────┴─────┐                      │
              │   ▼          ▼                      │
              │ ┌──────────┐ ┌────────┐             │
              └─│ RESOLVED │ │ FAILED │─────────────┘
                └──────────┘ └────────┘
```

Transitions are enforced in two places:

1. `models.can_transition()` documents the legal graph.
2. `dynamo.transition()` takes an `expected_status` guard and issues a DynamoDB
   `ConditionExpression`. If two workers race, one loses and returns `None`
   rather than double-driving the incident.

`ROOT_CAUSE_IDENTIFIED` is a real resting state: it is where an incident stops
when the analysis produced no allowlisted remediation, and the dashboard shows
"manual remediation required".

---

## 4. DynamoDB schema

### `opspilot-<env>-incidents`

On-demand billing, encrypted, PITR enabled, 90-day TTL.

| Key | Attribute | Notes |
| --- | --- | --- |
| PK | `incident_id` | `INC-<YYYYMMDD>-<6 hex>` — derived from the dedupe key |

**GSI `status-detected_at-index`** (`status` / `detected_at`) — drives the
dashboard's active and historical views, and the metrics summary. Querying six
open statuses is far cheaper than scanning.

**GSI `signature-detected_at-index`** (`signature` / `detected_at`) — incident
memory. See §8.

Selected attributes:

```
incident_id            INC-20260828-367FFC
dedupe_key             sha256(alarm_name + 5-minute bucket)[:32]
status                 RESOLVED
severity               HIGH
title, description
detected_at            2026-08-28T03:49:22Z
updated_at, resolved_at
time_to_resolve_minutes  4.85
source                 cloudwatch-alarm
alarm_name, alarm_reason
affected_service       opspilot-showcase-demo-app
incident_type          lambda_error
signature              opspilot-showcase-demo-app|lambda_error
root_cause             { description, confidence, category }
confidence             0.95
evidence               [ "..." ]
contributing_factors   [ "..." ]
timeline               [ { timestamp, event, kind, icon, source, detail } ]
changes                [ { timestamp, service, resource, action, actor,
                           details, source, correlation, correlation_score,
                           correlation_reasons, minutes_before_incident } ]
change_summary         "configuration_change on ... 1.55 minutes before onset"
recommendations        [ { proposed_action, action, title, risk, reason,
                           allowlisted, applicable, executable } ]
approved_action, approved_by, approved_at
remediation_status     SUCCEEDED
remediation_detail     { action, target, applied, verified_configuration, ... }
verification_status    VERIFIED
verification_detail    { status, reason, checks[], window_seconds }
postmortem_location    s3://.../postmortems/INC-....md
postmortem_key, postmortem_narrative_source
similar_incidents      [ { incident_id, title, resolution, outcome, ... } ]
ai_status              OK | FALLBACK | UNAVAILABLE | PENDING
ai_summary
evidence_sources       { source: { available, note } }
investigation_count
ttl                    epoch seconds, 90 days out
```

Floats are stored as `Decimal` (see `dynamo.to_dynamo`) because DynamoDB rejects
native floats; `from_dynamo` reverses this on read.

### `opspilot-<env>-changes`

The OpsPilot change log. PK `change_id`, GSI `scope-timestamp-index`
(`scope` = constant `"GLOBAL"` / `timestamp`), 30-day TTL.

A single-partition GSI is unusual, but correct here: the volume is tiny, and it
turns "what changed between T1 and T2?" into one bounded `Query` instead of a
`Scan`.

### `opspilot-<env>-demo-table`

The demo application's own table. **Provisioned at 1 RCU / 1 WCU on purpose** —
inside the DynamoDB free tier, and low enough that a modest write burst produces
genuine `ProvisionedThroughputExceededException` errors and real CloudWatch
throttle metrics. The `database_throttle` scenario is a real incident, not a
simulated one.

---

## 5. Evidence collection

Six independent sources, each bounded and each individually fault-tolerant.

| Source | API | Bound |
| --- | --- | --- |
| Alarm state | `cloudwatch:DescribeAlarms` | one alarm |
| Metrics | `cloudwatch:GetMetricData` | `MAX_METRIC_POINTS` (100), 3–4 series |
| Logs | `logs:FilterLogEvents` | `MAX_LOG_EVENTS` (100), error-prioritised |
| CloudTrail | `cloudtrail:LookupEvents` | `MAX_CLOUDTRAIL_EVENTS` (50), write events |
| Change log | DynamoDB `Query` | 100 rows in the lookback window |
| Incident memory | DynamoDB `Query` on GSI | `MAX_SIMILAR_INCIDENTS` (3) |
| App state | `lambda:GetFunctionConfiguration` | one function |

### Why bounded

Two reasons, both real:

1. **Cost.** Unbounded log collection against a busy service is expensive in
   both API calls and Bedrock tokens.
2. **Small-model compatibility.** The default model is a basic text model. Handing
   it 50,000 lines of logs makes its answer *worse*, not better.

### Prioritisation, not truncation

`_prioritise_logs` does not simply take the newest N events. It partitions log
lines into "signal" (containing `error`, `exception`, `throttl`, `timeout`,
`denied`, …) and "noise", takes signal first, backfills with the most recent
noise, then restores chronological order. What survives truncation is what
matters.

The prompt budget (`MAX_PROMPT_CHARS`) is consumed in priority order — alarm,
changes, metrics, app state, logs, history — so the highest-value evidence is
never the part that gets cut.

### Graceful degradation

Every collector returns an `EvidenceResult` carrying `available` alongside the
data. The investigation stores this map and the dashboard renders it:

```
UP    cloudwatch_alarm      collected
UP    cloudwatch_metrics    4 metric series collected
DOWN  cloudtrail            CloudTrail evidence unavailable: AccessDeniedException
```

This is the difference between "no changes were found" and "we could not look".
OpsPilot always says which one it means.

---

## 6. Change correlation

The capability that separates OpsPilot from a log summariser.

### Two sources, deliberately

| Source | Latency | Coverage |
| --- | --- | --- |
| OpsPilot change log | Sub-second | Only changes OpsPilot itself made |
| CloudTrail | Minutes | Every control-plane change in the account |

Neither alone is sufficient. CloudTrail is authoritative but slow; the change log
is instant but narrow. OpsPilot merges them, de-duplicates on
`(action, resource, minute)`, and tags every entry with its `source` so the
operator can see which is which.

This is not a workaround — it mirrors how real SRE tooling works, ingesting
CI/CD deployment events alongside cloud audit logs.

### Scoring

Deterministic arithmetic, no AI:

| Signal | Weight |
| --- | --- |
| Within 2 minutes of onset | +0.45 |
| Within 5 minutes | +0.35 |
| Within 15 minutes | +0.20 |
| Within 60 minutes | +0.05 |
| Action has wide blast radius | +0.30 |
| Touches the failing service | +0.20 |
| Recorded directly by OpsPilot | +0.10 |
| **Occurred after onset** | **score = 0** |
| **Restorative action** (reset, rollback, remediation) | **capped at 0.30** |

```
score ≥ 0.60  →  likely_contributor
score ≥ 0.35  →  possible_contributor
otherwise     →  unrelated
```

Two rules matter more than the weights:

- **Changes after onset score zero.** They cannot have caused it.
- **Restorative changes are capped.** A reset, rollback or remediation moves the
  system *towards* health. Without this rule, OpsPilot's own reset — applied
  moments before a fault injection, recent and high blast radius — outranks the
  fault that actually broke things.

Every score carries `correlation_reasons`, so the ranking is explainable rather
than a black box.

### What it does not claim

The labels are *correlation strengths*, not causation. `likely_contributor`
means "this is worth looking at first", and both the UI and the postmortem
present it that way.

---

## 7. Bedrock integration

### The contract

```
Deterministic Lambda ──► normalised evidence (JSON, bounded)
                              │
                              ▼
                    Bedrock Converse API
                    • no tools
                    • no credentials
                    • no network access
                    • temperature 0.1
                              │
                              ▼
                     JSON ──► validation ──► allowlist lookup
```

The model is asked for an explanation and a *label*. It cannot act, and no
string it returns is ever used as a resource identifier.

### Why the Converse API only

`converse` is Bedrock's provider-neutral interface. Using it — rather than a
model-specific `invoke_model` body — means changing `bedrock_model_id` to any
other text model just works. No prompt is written against a particular model
family's quirks.

### Prompt design for basic models

- Short, imperative system prompt.
- The exact output schema, inline.
- The allowlist, so recommendations are drawn from a closed set.
- Evidence in priority order within a character budget.
- Explicit instruction that history is context, not proof.

### Resilient parsing

The default model returns fenced JSON (```` ```json ... ``` ````) in practice.
The parser handles that and more, in order:

1. Strip Markdown fences (preferring the contents of the first fenced block).
2. Extract the outermost balanced `{...}` — string-aware, so braces and escaped
   quotes inside strings do not confuse it.
3. Repair trailing commas, `NaN`/`Infinity`, and Python `True`/`False`/`None`.
4. `json.loads` each candidate in turn.
5. Validate and coerce every field.

`parse_json_response` returns `None` rather than raising. **A malformed model
response never breaks the incident workflow.**

### Failure handling

| Condition | Result |
| --- | --- |
| Throttling / 5xx / timeout | Exponential backoff with jitter, 4 attempts |
| Retries exhausted | `BedrockUnavailable` → `ai_status: UNAVAILABLE` |
| Unparseable output | `ai_status: FALLBACK` |
| Schema mismatch | `ai_status: FALLBACK` |

In every failure case the fallback analysis has **confidence 0**, severity
`UNKNOWN`, empty evidence, and a summary that says analysis was unavailable.
OpsPilot never fabricates a diagnosis. The incident still proceeds, still
carries deterministic evidence, and still offers the safe scenario-appropriate
remediation so a human is not left stranded.

---

## 8. Incident memory without a vector database

Recall is a DynamoDB GSI lookup on a deterministic signature:

```python
signature = f"{affected_service.lower()}|{incident_type.lower()}"
# → "opspilot-showcase-demo-app|lambda_error"
```

`Query` the `signature-detected_at-index` descending, take the top N, fall back
to matching `alarm_name` if the signature yields too few.

**Why not embeddings?** Cost, latency, and an entire additional service to run
and pay for, in exchange for fuzzy matching over a problem where the failure
taxonomy is already known and discrete. The trade-off is stated honestly in the
README's Limitations: this is exact matching, not similarity search, and two
incidents with the same underlying cause but different signatures will not match.

Retrieved incidents are condensed to `{incident_id, title, root_cause,
resolution, outcome, time_to_resolve}` and passed into the prompt under a
heading that tells the model plainly:

> These are historical incidents with a similar signature. They may provide
> useful context, but do not assume the current incident has the same root cause.

---

## 9. Remediation safety design

Five independent controls. Each is sufficient on its own; together they make
unsafe remediation structurally impossible.

```
   Model output: "restore_previous_lambda_version"
        │
   [1]  │  The model has no tools, no credentials, no network.
        │  It returned text, nothing more.
        ▼
   [2]  ALLOWLIST LOOKUP  ─────────────────────────────────┐
        normalise → resolve_action()                       │
        No match → no action. Incident marked              │  refused
        "manual remediation required", reasoning still     │
        visible to the operator.                           │
        │                                                  │
   [3]  ▼  HUMAN APPROVAL                                  │
        No EventBridge rule routes investigation →         │
        remediation. The only path is POST .../approve,    │
        which re-validates:                                │
          • incident exists                                │
          • status == AWAITING_APPROVAL                    │
          • action was actually recommended                │
          • action is allowlisted (re-checked server side) │
          • target is inside the Demo Lab                  │
        │                                                  │
   [4]  ▼  TERRAFORM-SUPPLIED TARGET                       │
        The function name comes from an environment        │
        variable set by Terraform. Nothing from the model  │
        or the incident record is used as an identifier.   │
        Writable env keys are restricted to six fault      │
        flags; values come from the baseline or the spec.  │
        │                                                  │
   [5]  ▼  IAM                                             │
        lambda:UpdateFunctionConfiguration on exactly one  │
        ARN. Even with every check above bypassed, AWS     │
        refuses anything else.                             │
        ▼                                                  ▼
   Demo Lab function only                          Manual remediation
```

### The allowlist

Six operations, none destructive:

| Key | Effect | Risk |
| --- | --- | --- |
| `reset_demo_lambda` | Clear every fault flag, restore baseline | LOW |
| `reset_demo_error_mode` | Clear error injection | LOW |
| `reset_demo_latency_mode` | Clear latency injection | LOW |
| `restore_demo_configuration` | Reapply config profile and table target | LOW |
| `restore_previous_demo_version` | Roll back to the last change-log snapshot | MEDIUM |
| `reset_demo_db_throttle` | Clear the write-amplification flag | LOW |

Model phrasings are normalised (`"Restore Previous Lambda Version"` →
`restore_previous_demo_version`) through an alias table. A last-resort
containment match applies **only when exactly one candidate matches** —
ambiguous input like `"reset"` is refused rather than guessed, because guessing
here would be a safety failure.

### Success is measured, not assumed

The remediation function re-reads the function configuration after writing it
and compares against what it intended to apply. A mismatch is a failure. An
absent exception is not success.

---

## 10. Verification flow

An incident is never resolved because remediation returned cleanly.

```
 remediation completed
        │
        ▼
 ┌──────────────────────────────────────────────────┐
 │  for check in 0..VERIFICATION_CHECKS (6):        │
 │     sleep VERIFICATION_INTERVAL_SECONDS (30)     │
 │     ├── invoke the demo app for real  ← decisive │
 │     └── read the alarm state       ← corroborating│
 └──────────────────────────────────────────────────┘
        │
        ▼
 collect error metrics across the window
        │
        ▼
 VERIFIED  if the final probe is healthy
           and ≥ n-1 probes were healthy
 else VERIFICATION_FAILED
```

### Why the live probe is decisive and the alarm is not

CloudWatch alarm evaluation lags real recovery by design — the alarm must
observe a full healthy period before it transitions. In a real run:

```
+  0s  healthy=True  HTTP 200  144ms  alarm=ALARM
+ 30s  healthy=True  HTTP 200   33ms  alarm=ALARM
+ 60s  healthy=True  HTTP 200   41ms  alarm=ALARM
+ 90s  healthy=True  HTTP 200   17ms  alarm=ALARM
+120s  healthy=True  HTTP 200   17ms  alarm=OK
+150s  healthy=True  HTTP 200   15ms  alarm=OK
```

The service was healthy from the first probe; the alarm caught up two minutes
later. Letting the alarm veto would have failed a genuine recovery.

So the alarm is recorded as evidence and reported in the verdict — including
when it is still clearing — but the live service decides. Likewise, error metrics
from *before* the fix remain inside the metric window, so they are reported as an
explicit caveat rather than treated as ongoing failure:

> Application healthy on 6/6 probes; alarm state OK; error metrics still show
> 4.0 event(s) in the window (pre-fix datapoints)

The verifier also holds back time: it checks the remaining Lambda budget before
each sleep and truncates the window rather than being killed mid-verification
without recording a verdict.

### Why sleeping in a Lambda

The window is ~150 seconds. A Step Functions state machine would add a service,
IAM surface, and Terraform complexity to solve a problem that `time.sleep()`
solves for a fraction of a cent. The function's timeout is computed from the
window plus headroom.

---

## 11. Security model

### Roles

Nine functions, nine execution roles, no sharing. No `AdministratorAccess`, no
`PowerUserAccess`, no `"Action": "*"`.

| Role | Can read | Can write |
| --- | --- | --- |
| API | incidents, alarms, postmortem objects | incidents; invoke demo controller |
| Detector | — | incidents, events |
| Investigator | incidents, change log, alarms, metrics, demo logs, CloudTrail, demo config, Bedrock | incidents, events |
| Remediation | incidents, demo config | incidents, change log, events, **demo function config** |
| Verifier | incidents, alarms, metrics, demo logs, demo config | incidents, events |
| Postmortem | incidents, Bedrock | incidents, events, postmortem objects |
| Demo Controller | demo config, alarms | change log, **demo function config** |
| Demo App | — | own logs, demo table |
| Traffic Generator | — | invoke demo function |

Only two roles can change AWS state at all, and both are scoped to the same
single function ARN.

### Wildcard resources

Three, all read-only, all because the API has no resource-level permission
model. Each is annotated in `iam.tf`:

- `cloudwatch:DescribeAlarms`, `GetMetricData`, `ListMetrics`
- `cloudtrail:LookupEvents`
- `bedrock:InvokeModel` on `foundation-model/*` (the model id is a variable)

### Data protection

- S3: SSE-AES256, bucket keys, versioning, public access blocked on all three
  buckets, and explicit `DenyInsecureTransport` bucket policies.
- Dashboard bucket is private; CloudFront reaches it through OAC.
- DynamoDB: encryption at rest on all tables, PITR on incidents.
- No credentials, keys or secrets anywhere in code, environment or state.
- `logging_utils` redacts any field whose key looks credential-bearing, and no
  request payloads are logged.

---

## 12. Error handling philosophy

The rule throughout: **degrade honestly, never fabricate, never crash the
lifecycle.**

| Failure | Behaviour |
| --- | --- |
| Bedrock unavailable | Incident proceeds; `ai_status: UNAVAILABLE`; confidence 0; UI says "AI investigation unavailable" |
| Bedrock returns junk | `ai_status: FALLBACK`; deterministic evidence retained |
| CloudTrail unavailable | Change correlation continues on the change log; source marked unavailable |
| Logs unavailable | Investigation continues; source marked unavailable |
| Metrics unavailable | Investigation continues; verifier falls back to the live probe |
| Change log unavailable | CloudTrail alone is used |
| Incident memory unavailable | Investigation continues without history |
| EventBridge publish fails | Logged, surfaced on the timeline, incident stays visible and retriable |
| Remediation refused | Incident → FAILED with `MANUAL_REQUIRED` |
| Remediation fails | Incident → FAILED, error recorded, no retry |
| Verification fails | Incident → FAILED, not RESOLVED |
| S3 upload fails | Incident stays resolved; postmortem error recorded |
| Concurrent workers race | Conditional write loses; the loser exits cleanly |

### Idempotency

`incident_id` is derived deterministically from
`sha256(alarm_name + 5-minute bucket)`, so a replayed EventBridge delivery
resolves to the **same primary key** and the `attribute_not_exists(incident_id)`
condition makes duplicate creation atomically impossible. This matters: a random
id with the same conditional write would look correct and enforce nothing.

Async invocations of the demo app are configured with
`maximum_retry_attempts = 0`. Lambda's default of two async retries would triple
an injected fault's error count and keep errors arriving for minutes after
remediation — re-tripping the alarm and opening a spurious second incident.

### One failure, one alarm, one incident

The demo application emits its custom metrics on a most-specific-signal-wins
basis:

| Failure | Signal | Deliberately not emitted |
| --- | --- | --- |
| Unhandled crash | `AWS/Lambda Errors` | `HttpErrors` — the request never got a response |
| Invalid configuration | `ConfigErrors` | `HttpErrors` |
| Injected 500 | `HttpErrors` | — |
| DynamoDB throttling | `DbThrottles` | `HttpErrors` — the request still succeeds |

### Two lessons from live testing

**Choose metrics that exist at the dimension you alarm on.** The throttle alarm
was first written against `AWS/DynamoDB ThrottledRequests` keyed on `TableName`.
AWS publishes that metric only with a two-dimension key (`TableName` +
`Operation`), so the alarm sat in `INSUFFICIENT_DATA` and the scenario silently
never fired.

**Prefer continuously-emitted metrics for alarms that must reset.** AWS's
DynamoDB throttle metrics are *sparse*: when nothing throttles they publish
nothing, and CloudWatch then takes ~8 minutes to apply `notBreaching` and return
the alarm to OK. Until it does, a repeat injection produces no `OK -> ALARM`
transition and therefore no incident. The alarm now watches the demo app's own
`DbThrottles`, emitted on every request including zeros, so it clears in under a
minute like every other scenario. The value is still real — it counts writes
DynamoDB actually rejected — and the authoritative `WriteThrottleEvents` series
is retained as investigation evidence.

Emitting a generic error metric alongside a specific one double-counts the same
failure and opens two incidents for it — an alarm storm in miniature. This was
found and fixed during live testing.

---

## 13. Cost design

| Decision | Reason |
| --- | --- |
| arm64 (Graviton) Lambdas | ~20% cheaper per ms than x86_64 |
| HTTP API, not REST API | Cheaper; OpsPilot needs no REST-only features |
| On-demand DynamoDB for incidents | Spiky, low volume |
| Provisioned 1/1 for the demo table | Free tier, and enables real throttling |
| CloudTrail: single region, management events, write-only | First copy of management events is free; data events are the expensive part |
| 30-day trail expiry, 7-day artifact version expiry | Storage stays negligible |
| CloudFront PriceClass_100 | Cheapest edge footprint |
| Traffic generator at 1/min | 43k invocations/month against a 1M free allowance |
| 5 custom metrics, not 6 | Dropped a metric that duplicated a free AWS one |
| Layer for shared code | One upload, not nine copies |
| No VPC, NAT, ECS, EKS, RDS, OpenSearch | Each would exceed the entire rest of the bill |

The largest recurring line item is the five custom CloudWatch metrics.
`enable_traffic_generator=false` and `enable_cloudtrail=false` reduce idle cost
further, at the cost of slower and less reliable detection.
