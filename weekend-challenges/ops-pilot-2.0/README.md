# OpsPilot

**From incident detection to verified recovery.**

OpsPilot is an autonomous AWS incident lifecycle platform. It detects incidents,
investigates telemetry, correlates infrastructure changes, recommends safe
remediation, verifies recovery, and generates reusable postmortems.

> **What if your AWS infrastructure could investigate its own incidents?**

```bash
cd terraform
terraform init
terraform apply
```

That is the entire installation. No console clicking, no manual resource
creation, no separate build step.

---

## What is OpsPilot?

Most "AI for observability" tools summarise logs. OpsPilot runs the whole
incident lifecycle and holds itself to a higher standard: **every claim it makes
is backed by real state in real AWS services.**

```
Something breaks
       ↓
OpsPilot notices          ← CloudWatch alarm → EventBridge
       ↓
OpsPilot investigates     ← logs, metrics, CloudTrail, live app state
       ↓
OpsPilot finds what changed  ← deterministic change correlation
       ↓
OpsPilot explains why     ← Amazon Bedrock, over normalised evidence
       ↓
OpsPilot recommends a safe fix   ← from a fixed allowlist
       ↓
Engineer approves         ← mandatory human gate
       ↓
OpsPilot fixes it         ← Demo Lab resources only
       ↓
OpsPilot verifies recovery   ← probes the live service
       ↓
OpsPilot writes the postmortem   ← facts from the incident record
       ↓
OpsPilot remembers the incident  ← deterministic recall, no vector DB
```

The product question it answers:

> *What happened, why did it happen, what changed immediately beforehand, what
> should I do, did the fix actually work, and have I encountered this before?*

---

## The Problem

At 03:00 an alarm fires. An engineer wakes up and starts the same ritual every
time:

1. **What is actually broken?** Open CloudWatch, find the right metric.
2. **What do the logs say?** Find the log group, filter, scroll.
3. **What changed?** Open CloudTrail, guess a time window, read raw JSON.
4. **Was it the deploy?** Correlate a deployment timestamp against failure onset
   by eye.
5. **What do I do?** Decide under time pressure with partial information.
6. **Did it work?** Watch a graph and hope.
7. **Write it up.** Usually tomorrow. Usually never.
8. **Have we seen this?** Search Slack. Find nothing.

Steps 1-4 are pure mechanical evidence gathering, and they consume most of the
time to diagnosis. Step 6 is where incidents get closed prematurely. Steps 7-8
are where organisations fail to learn.

## The Solution

OpsPilot automates the mechanical work and keeps humans exactly where judgement
matters.

| Stage | Who does it | Why |
| --- | --- | --- |
| Detect | CloudWatch + EventBridge | Deterministic, already trustworthy |
| Collect evidence | Lambda + AWS APIs | Must be exact and reproducible |
| Correlate changes | Lambda, deterministic scoring | Explainable, auditable |
| Diagnose | Amazon Bedrock | Genuinely a language problem |
| Recommend | Bedrock, constrained to an allowlist | Suggestion, never authority |
| **Approve** | **A human** | **The consequential decision** |
| Remediate | Lambda, allowlisted actions only | Must be bounded |
| Verify | Lambda, probes the live service | Claims need evidence |
| Document | Facts from data, prose from Bedrock | Facts must not be invented |
| Remember | DynamoDB GSI on a failure signature | Recall must be predictable |

The AI is used for exactly one thing: explaining evidence that deterministic
code already collected. It never touches AWS.

---

## Architecture

```
                         ┌─────────────────────┐
                         │     Web Browser     │
                         └──────────┬──────────┘
                                    │ HTTPS
                         ┌──────────▼──────────┐
                         │  CloudFront + OAC   │──── S3 (private, dashboard)
                         └──────────┬──────────┘
                                    │
                         ┌──────────▼──────────┐
                         │  API Gateway (HTTP) │
                         └──────────┬──────────┘
                                    │
                         ┌──────────▼──────────┐
                         │     API Lambda      │
                         └──────────┬──────────┘
                   ┌────────────────┼────────────────┐
                   ▼                ▼                ▼
              DynamoDB             S3           CloudWatch
            (incidents)      (postmortems)       (alarms)


AWS INCIDENT PATH
────────────────────────────────────────────────────────────────

  CloudWatch Alarm  (demo app errors / latency / throttling / config)
         │
         │  default event bus
         ▼
  EventBridge Rule
         │
         ▼
  Incident Detector Lambda ──────► DynamoDB  (conditional write = idempotent)
         │
         │  "OpsPilot Incident Detected"  →  opspilot-showcase-events bus
         ▼
  Investigator Lambda
         │
         ├── CloudWatch Logs      (bounded, error-prioritised)
         ├── CloudWatch Metrics   (bounded series per incident type)
         ├── CloudTrail           (LookupEvents, 90-day event history)
         ├── OpsPilot change log  (DynamoDB, sub-second fidelity)
         ├── Live demo app state  (Lambda GetFunctionConfiguration)
         └── Incident memory      (DynamoDB GSI on failure signature)
         │
         ▼
  Change Correlation   (deterministic scoring: proximity + blast radius + service)
         │
         ▼
  Amazon Bedrock  (Converse API, normalised evidence in → JSON out)
         │
         ▼
  Root Cause + Confidence + Evidence + Recommendation
         │
         ▼
  ┌──────────────────┐
  │  HUMAN APPROVAL  │   ← there is no EventBridge rule that bypasses this
  └────────┬─────────┘
           │  "OpsPilot Remediation Approved"
           ▼
  Remediation Lambda   (allowlisted action, Demo Lab resources only)
           │  "OpsPilot Remediation Completed"
           ▼
  Verifier Lambda      (probes the live service over a bounded window)
           │  "OpsPilot Verification Completed"
           ▼
  Postmortem Lambda ──────► S3 (Markdown) + DynamoDB (metadata)
```

Full detail: [`docs/architecture.md`](docs/architecture.md).

---

## AWS Services

Every service here is present for a specific reason.

| Service | Why it is used |
| --- | --- |
| **CloudWatch Alarms** | The detection trigger. Five alarms watch the Demo Lab on 60s periods so a failure is caught in ~2 minutes. |
| **CloudWatch Metrics** | Quantitative evidence: error counts, invocation rate, duration, throttles. Also how recovery is measured. |
| **CloudWatch Logs** | Qualitative evidence. Collection is bounded and error-prioritised, never a whole stream. |
| **EventBridge** | Decouples the pipeline. Each stage emits an event; the next stage subscribes. Any stage can fail or be replayed independently. |
| **Lambda** | All compute. Nine functions with one responsibility each, one IAM role each. |
| **DynamoDB** | Incident store (on-demand), change log, and the Demo Lab's own table. Two GSIs power the dashboard and incident memory. |
| **S3** | Postmortem documents, the dashboard bundle, and CloudTrail logs. All encrypted, all private. |
| **CloudTrail** | The independent record of what changed in the account. This is what makes change correlation real rather than assumed. |
| **API Gateway** | HTTP API fronting the dashboard's backend and the demo app. |
| **Bedrock** | Root-cause explanation and postmortem narrative. Nothing else. |
| **CloudFront** | HTTPS for the dashboard, with OAC so the S3 bucket stays private. |
| **IAM** | Nine least-privilege roles. The remediation boundary is enforced here, not just in code. |

Deliberately **not** used: NAT Gateways, ECS, EKS, RDS, OpenSearch, VPCs,
provisioned Bedrock throughput, EC2, vector databases, embeddings, Bedrock
Agents, Bedrock Knowledge Bases, or any external AI provider.

---

## AI Architecture

The design principle: **deterministic code gathers facts and takes actions; the
model only explains.**

```
┌──────────────────────────────────────────────────────────────┐
│  DETERMINISTIC (Lambda + boto3)                              │
│                                                              │
│  • Queries CloudWatch, CloudTrail, DynamoDB, Lambda          │
│  • Normalises evidence into one schema                       │
│  • Scores change correlation arithmetically                  │
│  • Executes remediation from a fixed allowlist               │
│  • Probes the live service to verify recovery                │
│  • Writes every fact in the postmortem                       │
└───────────────────────────┬──────────────────────────────────┘
                            │  normalised evidence (JSON, bounded)
                            ▼
┌──────────────────────────────────────────────────────────────┐
│  AMAZON BEDROCK  (Converse API)                              │
│                                                              │
│  • Reads evidence, returns JSON                              │
│  • No tools. No AWS credentials. No network access.          │
│  • Cannot call an API, run a command, or name a resource     │
│    that gets used                                            │
└───────────────────────────┬──────────────────────────────────┘
                            │  {summary, root_cause, confidence,
                            │   evidence, recommended_actions}
                            ▼
┌──────────────────────────────────────────────────────────────┐
│  VALIDATION                                                  │
│                                                              │
│  • Strip Markdown fences, extract the JSON object, repair    │
│    trailing commas, parse, validate the schema               │
│  • Map the recommendation onto the allowlist - or refuse     │
│  • On any failure: honest fallback, confidence 0             │
└──────────────────────────────────────────────────────────────┘
```

**Why this split?** Because the failure modes are completely different. A model
that hallucinates a log line produces a wrong explanation — recoverable, and
visible to the operator. A model that can call `DeleteFunction` produces an
outage. OpsPilot makes the second failure mode structurally impossible.

**Why a small model?** The default is `amazon.nova-lite-v1:0`, chosen because
the hard part of this problem is *evidence collection*, not reasoning. Once the
evidence is normalised, explaining it is well within a basic text model. Any
Converse-compatible model works:

```bash
terraform apply -var 'bedrock_model_id=amazon.nova-pro-v1:0'
```

Only the Converse API is used, so nothing is provider-specific.

### When Bedrock fails

It will, eventually — throttling, a model that emits prose instead of JSON, a
timeout. OpsPilot handles each case and **never invents a diagnosis**:

| Failure | Behaviour |
| --- | --- |
| Throttling / 5xx | Retry with exponential backoff and jitter (4 attempts) |
| Still failing | `ai_status: UNAVAILABLE`, confidence 0, incident continues |
| Markdown fences | Stripped (the default model does this routinely) |
| Prose around JSON | Balanced-brace extraction, string-aware |
| Trailing commas, `True`/`None` | Repaired |
| Unparseable | `ai_status: FALLBACK`, confidence 0 |
| Schema mismatch | Rejected, fallback used |

In every fallback case the incident is still created, still investigated with
deterministic evidence, and still offered a safe scenario-appropriate
remediation. The dashboard says plainly that AI analysis was unavailable.

---

## Incident Lifecycle

```
DETECTED ──► INVESTIGATING ──► ROOT_CAUSE_IDENTIFIED ──► AWAITING_APPROVAL
                                                                │
                                                    ┌───────────┴──────────┐
                                                 approve                reject
                                                    │                      │
                                                    ▼                      ▼
                                              REMEDIATING              FAILED
                                                    │
                                                    ▼
                                                VERIFYING
                                                    │
                                        ┌───────────┴───────────┐
                                    verified              not verified
                                        │                       │
                                        ▼                       ▼
                                    RESOLVED                 FAILED
```

**Detect** — A CloudWatch alarm enters ALARM. EventBridge delivers the
transition. The detector derives incident metadata from a Terraform-generated
alarm catalog (a lookup, not a guess) and writes the incident with a
*deterministic* primary key derived from `alarm_name + time bucket`, so
DynamoDB's conditional write makes duplicate delivery impossible.

**Investigate** — Six evidence sources are queried in parallel, each bounded and
independently fault-tolerant. A failure in one is reported as unavailable rather
than crashing the investigation.

**Correlate** — Every change in the lookback window is scored on temporal
proximity, blast radius, and whether it touches the failing service, then
labelled `likely_contributor`, `possible_contributor`, `unrelated`, or
`after_incident`. Changes that happened *after* onset are scored zero — they
cannot have caused it.

**Diagnose** — Bedrock receives the normalised evidence and returns structured
JSON.

**Recommend** — The model's text is normalised and looked up in the allowlist.
Unrecognised recommendations stay visible to the operator but are not
executable.

**Approve** — Nothing happens without this. There is no EventBridge rule from
"Investigation Completed" to remediation; the only path is a human calling
`POST /incidents/{id}/approve`.

**Remediate** — One allowlisted action against one Terraform-named function.

**Verify** — The service is probed live, repeatedly, over a bounded window. An
incident is *never* marked resolved because remediation returned without an
exception.

**Learn** — A postmortem is written to S3 and the incident becomes searchable
history.

---

## Deployment

**Prerequisites**

1. AWS CLI configured with credentials.
2. IAM permissions to create the resources listed above.
3. Terraform >= 1.5.
4. **Bedrock model access enabled for `amazon.nova-lite-v1:0` in your region.**

> Item 4 is the one genuine prerequisite that Terraform cannot automate. Bedrock
> model access is granted per-account through the console
> (*Bedrock → Model access*) and there is no Terraform resource for it. If you
> skip it, everything still deploys and incidents still work — analysis just
> falls back to the deterministic path and the dashboard says
> "AI investigation unavailable".

```bash
git clone <repo>
cd opspilot/terraform
terraform init
terraform apply
```

Roughly 111 managed resources, about four minutes (most of it CloudFront).

```
Outputs:

opspilot_dashboard_url = "https://d3cfo2c7525p2.cloudfront.net"
opspilot_api_url       = "https://673pt3y1k3.execute-api.us-east-1.amazonaws.com/"
opspilot_region        = "us-east-1"
opspilot_bedrock_model = "amazon.nova-lite-v1:0"
opspilot_demo_instructions = <step-by-step demo walkthrough>
```

**Configuration**

| Variable | Default | Purpose |
| --- | --- | --- |
| `aws_region` | `us-east-1` | Deployment region |
| `environment` | `showcase` | Name prefix for every resource |
| `bedrock_model_id` | `amazon.nova-lite-v1:0` | Any Converse-compatible text model |
| `bedrock_max_tokens` | `2000` | Generation ceiling |
| `bedrock_temperature` | `0.1` | Low: this is diagnosis, not creative writing |
| `change_lookback_minutes` | `15` | Change correlation window |
| `max_log_events` | `100` | Evidence bound |
| `max_prompt_chars` | `18000` | Prompt budget |
| `verification_checks` | `6` | Recovery probes after remediation |
| `enable_cloudtrail` | `true` | Create a project-owned trail |
| `enable_traffic_generator` | `true` | Keep alarm datapoints flowing |

---

## Demo

Open the dashboard URL from the Terraform output.

1. The header shows **● System Healthy** and the incident table is empty.
2. Click **Inject Lambda Error**. This rewrites the demo function's environment
   — a real `lambda:UpdateFunctionConfiguration` call that lands in CloudTrail
   exactly like a production deploy.
3. Wait ~2-3 minutes. The alarm fires, EventBridge delivers it, and the incident
   appears as `DETECTED`, then `INVESTIGATING`, then `AWAITING_APPROVAL`.
4. Open the incident. You get the timeline, the correlated change, the evidence,
   the root cause with a confidence score, and the recommended remediation.
5. Click **Approve Remediation**.
6. Watch it move through `REMEDIATING` → `VERIFYING` → `RESOLVED`, with the
   verification probes shown individually.
7. Click **Load postmortem**.
8. Inject the same scenario again — the new incident recalls the first one under
   *Similar Past Incidents*.

Full script: [`docs/demo.md`](docs/demo.md). Command-line equivalent:

```bash
API=$(terraform output -raw opspilot_api_url)

curl -s "$API/health"
curl -s -X POST "$API/demo/inject" -H 'content-type: application/json' \
     -d '{"scenario":"lambda_error"}'
curl -s "$API/incidents"
curl -s "$API/incidents/INC-.../"
curl -s -X POST "$API/incidents/INC-.../approve" \
     -H 'content-type: application/json' -d '{"action":"reset_demo_error_mode"}'
```

### Failure scenarios

| Scenario | What actually happens | Alarm |
| --- | --- | --- |
| `lambda_error` | Demo function raises an unhandled exception | `AWS/Lambda Errors` |
| `lambda_latency` | Demo function sleeps 4s per request | `AWS/Lambda Duration` |
| `application_error` | Demo app returns HTTP 500 for ~80% of requests | `OpsPilot/DemoApp HttpErrors` |
| `database_throttle` | Demo app bursts writes past the table's 1 WCU | `OpsPilot/DemoApp DbThrottles` |
| `configuration_error` | Demo app switched to an invalid config profile | `OpsPilot/DemoApp ConfigErrors` |

These are real failures producing real metrics. `database_throttle` in
particular provokes genuine DynamoDB throttling — the demo table is provisioned
at 1 RCU / 1 WCU precisely so that real rejections are reproducible inside the
free tier. Its alarm watches the application's own throttle count rather than
`AWS/DynamoDB` directly, because the AWS throttle metrics are sparse and take
~8 minutes to return an alarm to OK once throttling stops, which makes repeat
demos unusable. The count is not synthetic — it is the number of writes DynamoDB
actually rejected — and the authoritative `AWS/DynamoDB WriteThrottleEvents`
series is still collected as investigation evidence.

---

## Example Incident

Real output from a verified run, lightly abridged. Every value below was read
back from the deployed system.

```
INC-20260828-367FFC          HIGH          lambda_error

Summary
  The Lambda function 'opspilot-showcase-demo-app' is returning errors,
  triggering an alarm.

Investigation Timeline
  03:47:49  🚀  configuration_change on opspilot-showcase-demo-app
                                                    [opspilot-change-log]
  03:48:12  🚀  fault_injection on opspilot-showcase-demo-app
                correlation: likely_contributor      [opspilot-change-log]
  03:48:12  📈  Fault injection performed to simulate a Lambda error.
                                                      [bedrock-analysis]
  03:49:22  🔴  CloudWatch alarm opspilot-showcase-demo-lambda-errors
                entered ALARM                              [cloudwatch]
  03:49:22  🤖  OpsPilot opened incident                      [opspilot]
  03:49:25  🤖  OpsPilot investigation started                [opspilot]
  03:49:28  🤖  Root cause identified                         [opspilot]
  03:51:36  👤  Remediation approved by verification-run          [human]
  03:51:40  🔧  Remediation executed: Clear demo Lambda error injection
  03:54:13  ✅  Recovery verified

Root Cause                                     category: fault_injection
  The incident was likely caused by a fault injection that was performed
  1 minute before the incident.
                                              ████████████████████░  95%

Evidence
  • Change correlation: fault_injection on opspilot-showcase-demo-app
    1.22 minutes before onset
  • A fault injection was performed on the Lambda function
    'opspilot-showcase-demo-app' at 03:48:12Z to simulate an error.
  • The Lambda function started returning errors at 03:48:00Z, as
    indicated by the alarm datapoint of 12.0 errors.

Evidence Sources
  ● cloudwatch_alarm      collected
  ● cloudwatch_metrics    4 metric series collected
  ● cloudwatch_logs       collected
  ● cloudtrail            CloudTrail delivery is not instantaneous;
                          very recent changes may not appear yet
  ● opspilot_change_log   Changes recorded by OpsPilot at the moment
                          they were applied
  ● application_state     collected
  ● incident_memory       Deterministic recall on
                          (affected_service, incident_type)

Recommended Remediation
  reset_demo_error_mode — Clear demo Lambda error injection
  Risk: LOW          Allowlisted ✓

  [ APPROVE REMEDIATION ]   [ REJECT ]

── after approval ─────────────────────────────────────────────────────

Remediation
  applied:  {"FAILURE_MODE": "none", "ERROR_RATE": "0"}
  target:   opspilot-showcase-demo-app
  outcome:  SUCCEEDED (configuration re-read and confirmed)

Verification                                              VERIFIED
  +  0s  ✓  HTTP 200 · 144ms · alarm ALARM
  + 30s  ✓  HTTP 200 ·  33ms · alarm ALARM
  + 60s  ✓  HTTP 200 ·  41ms · alarm ALARM
  + 90s  ✓  HTTP 200 ·  17ms · alarm ALARM
  +120s  ✓  HTTP 200 ·  17ms · alarm OK
  +150s  ✓  HTTP 200 ·  15ms · alarm OK

  Application healthy on 6/6 probes; alarm state OK; error metrics still
  show 4.0 event(s) in the window (pre-fix datapoints)

Status: RESOLVED          MTTR: 4.85 minutes
Postmortem: s3://opspilot-showcase-artifacts-.../postmortems/INC-...md
```

Three things in that output are worth pointing at, because they are where a
system like this usually oversells itself:

**The change is labelled `likely_contributor`, not "the cause."** Correlation
strength is not causation, and the UI never pretends otherwise.

**The alarm was still in ALARM while the service was already healthy.** Probes
at +0s through +90s show HTTP 200 with the alarm not yet cleared — CloudWatch
needs to observe a full healthy period. The verdict states this rather than
hiding it, and the live probe is what decides.

**The leftover error metrics are disclosed.** Errors from *before* the fix are
still inside the metric window, so the verdict says so explicitly instead of
quietly ignoring them or failing the verification over them.

## Security

### Least privilege

Nine Lambda functions, nine execution roles, no shared role. No
`AdministratorAccess`, no `PowerUserAccess`, no `"Action": "*"`.

Wildcard *resources* appear in exactly three places, all read-only, all because
the AWS API in question has no resource-level permission model:

- `cloudwatch:DescribeAlarms` / `GetMetricData`
- `cloudtrail:LookupEvents`
- `bedrock:InvokeModel` on `foundation-model/*` (the model id is a variable)

Every mutating permission is scoped to a specific ARN.

### The remediation safety boundary

This is the part that matters. Five independent controls stand between a model's
output and an AWS API call:

1. **The model cannot act.** It has no tools, no credentials, no network. It
   returns text.
2. **The allowlist.** Model output is normalised and looked up in a fixed table
   of six operations. No match → no action, and the incident is marked "manual
   remediation required". Arbitrary strings, shell commands, AWS CLI fragments
   and resource ARNs all fail this lookup.
3. **Human approval.** There is no code path from investigation to remediation
   that does not pass through `POST /incidents/{id}/approve`, and the API
   re-validates the incident state and the action server-side.
4. **Terraform-supplied targets.** The function name being modified comes from
   an environment variable set by Terraform. Nothing from the model or the
   incident record is ever used as a resource identifier.
5. **IAM.** The remediation role can call `lambda:UpdateFunctionConfiguration`
   on exactly one ARN — the Demo Lab application. Even if every check above were
   bypassed, AWS itself would refuse anything else.

Even the *keys* it may write are constrained: only the six fault-flag
environment variables listed in `demo_mutable_env_keys`, and only to values from
the Terraform baseline or the allowlist spec.

None of the six allowlisted actions is destructive. Every one of them clears a
fault flag or restores a known-good configuration.

### Data security

- S3: SSE-AES256, versioning, public access blocked, TLS-only bucket policies.
- DynamoDB: encryption at rest, PITR on the incident table.
- No credentials, API keys or secrets in code, config or environment. IAM roles
  only.
- Structured logs redact any credential-shaped key, and no request payloads are
  logged.
- OpsPilot stores incident telemetry about its own Demo Lab. It collects no user
  data.

---

## Cost

Designed to sit inside or near the AWS Free Tier.

| Driver | Notes |
| --- | --- |
| **Lambda** | The traffic generator is the main consumer: 1 invocation/min ≈ 43k/month against a 1M free-tier allowance. Everything else is incident-driven. |
| **Bedrock** | Charged per token. A full investigation is roughly 4-8k input tokens plus ~1k output. On Nova Lite that is a fraction of a cent per incident. |
| **DynamoDB** | On-demand for incidents (pennies at this volume). The demo table is provisioned at 1 RCU / 1 WCU — inside the 25/25 free tier. |
| **CloudWatch** | The five custom metrics from the demo app are the only recurring line item (~$0.30/metric/month beyond free tier). Alarms: 10 free. |
| **CloudTrail** | First copy of management events in a region is free. Only S3 storage is charged, and logs expire after 30 days. |
| **CloudFront** | 1TB/month free tier; a dashboard uses a rounding error of that. |
| **S3** | Kilobytes of Markdown. Noise. |
| **API Gateway** | Charged per million requests. |

Idle cost with the traffic generator running is a small number of dollars per
month, dominated by the custom CloudWatch metrics. To drop it further:

```bash
terraform apply -var 'enable_traffic_generator=false' -var 'enable_cloudtrail=false'
```

(Detection becomes slower and less reliable without synthetic traffic, because
alarms need datapoints to evaluate.)

---

## Cleanup

```bash
cd terraform
terraform destroy
```

Everything is removed. Buckets use `force_destroy`, so postmortems and trail
logs are deleted with them — export anything you want to keep first.

---

## Limitations

Stated plainly, because a showcase that oversells itself is worse than one that
does less.

**CloudTrail is not instantaneous.** Event history typically lags real API
activity by several minutes; trail delivery to S3 can take ~15 minutes. A change
made seconds before an incident may not be visible to CloudTrail yet. OpsPilot
mitigates this by correlating against *two* sources — CloudTrail and its own
change log, which records changes at the moment they are applied — and labels
every change with its source. **OpsPilot does not claim complete visibility into
every AWS change.** Changes made outside this deployment, or in another region,
or before the trail existed, may simply not be found.

**The Demo Lab is the only thing OpsPilot can remediate.** By design. Pointing
it at production would mean writing new allowlist entries and widening the IAM
boundary — deliberately not a configuration toggle.

**Incident memory is deterministic, not semantic.** Recall matches on
`(affected_service, incident_type)`. Two incidents with the same underlying cause
but different signatures will not match each other. This is a conscious
trade-off against introducing embeddings and a vector database; it is
predictable and free, but it is not similarity search.

**Root-cause confidence is the model's self-assessment.** It is not a calibrated
probability. Treat it as a rough signal and read the evidence.

**Verification uses a bounded window.** The default is six probes over 150
seconds. A failure that recurs after that window closes will not reopen the
incident automatically — it will fire the alarm again and open a new one.

**Alarm thresholds are tuned for demo speed, not production.** Single 60-second
evaluation periods would be far too twitchy for a real service.

**Single region, single account.** There is no cross-region or cross-account
aggregation.

**The dashboard is unauthenticated.** It is a showcase deployment; the API is
public. Anyone with the URL can inject demo failures and approve remediations
against the Demo Lab. Do not treat this as a production posture — putting it
behind Cognito or IAM auth would be the first change for real use.

**The approval gate is a real gate, but a single one.** There is no
multi-party approval, no change freeze awareness, and no rate limit on
approvals beyond API Gateway throttling.

---

## Repository layout

```
opspilot/
├── terraform/          all infrastructure; apply is the only build step
│   ├── main.tf         locals: naming, alarm catalog, metric probes
│   ├── iam.tf          nine least-privilege roles
│   ├── lambda.tf       nine functions + the shared layer
│   ├── eventbridge.tf  the event-driven pipeline
│   ├── cloudwatch.tf   five demo alarms + OpsPilot's own alarms
│   ├── cloudtrail.tf   management-events trail, with limits documented
│   ├── demo_lab.tf     the sample application and traffic generator
│   └── frontend.tf     CloudFront + private S3
├── lambda/
│   ├── shared/         the runtime library, shipped as a Lambda layer
│   ├── api/            all API routes
│   ├── incident_detector/
│   ├── investigator/
│   ├── remediation/
│   ├── verifier/
│   ├── postmortem/
│   ├── demo_app/       the sample application (no OpsPilot dependency)
│   ├── demo_controller/
│   └── traffic_generator/
├── frontend/           HTML, CSS, vanilla JS
├── scripts/            packaging check, end-to-end smoke test
├── tests/              unit tests for parsing, allowlist, models, correlation
└── docs/               architecture, demo script, troubleshooting
```

Change correlation lives in `lambda/shared/python/opspilot/change_correlator.py`
rather than in its own Lambda: it is pure computation over already-collected
evidence, so a separate function would add a network hop and an IAM role for no
benefit.

## Testing

```bash
# Unit tests (no AWS required)
python -m pytest tests/ -q

# Packaging and handler verification
./scripts/package_lambdas.sh

# Terraform
cd terraform && terraform fmt -check && terraform validate

# Full lifecycle against a live deployment
./scripts/smoke_test.sh
```

The smoke test exercises the real path end to end: health, injection, alarm,
detection, investigation, Bedrock analysis, allowlist refusal of a hostile
action, approval, remediation, verification, resolution, postmortem, history and
incident recall.

## License

MIT — see [LICENSE](LICENSE).
