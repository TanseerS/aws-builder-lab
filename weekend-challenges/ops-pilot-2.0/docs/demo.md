# OpsPilot Demo Guide

A complete demonstration in about five minutes.

---

## Before you start

```bash
cd terraform
terraform output opspilot_dashboard_url
terraform output opspilot_api_url
```

Open the dashboard URL. If you have just applied for the first time, give
CloudFront a couple of minutes to finish propagating.

Confirm the starting state:

- Header reads **● System Healthy**
- All tiles read `0`
- The incident table says *No incidents recorded*
- The Demo Lab footer shows `Demo app: healthy`, `Alarms firing: 0`

If a previous run left the environment dirty, click **Reset Environment**.

---

## The five-minute script

### 1. Set the scene (30s)

> "This is OpsPilot. Everything on this screen is read from real AWS services —
> DynamoDB, CloudWatch, S3. There is no mock data.
>
> Below is a Demo Lab: a small Lambda application that OpsPilot is allowed to
> break and repair. Let's break it."

### 2. Inject the failure (10s)

Click **Inject Lambda Error**.

> "That just rewrote the demo function's environment variables. It is a real
> `lambda:UpdateFunctionConfiguration` call — the same API a deployment uses —
> so it lands in CloudTrail exactly like a production deploy would."

A toast confirms the injection and tells you roughly when the alarm will fire.

### 3. Wait for detection (~60–90s)

This is the part worth narrating rather than skipping:

> "Nothing is polling for this. The demo app is now failing; CloudWatch is
> aggregating its error metric over a one-minute period. When the alarm crosses
> its threshold it will publish a state change to EventBridge, and an
> EventBridge rule delivers that to OpsPilot's incident detector."

Watch the Demo Lab footer: `Alarms firing` goes to `1`. Shortly after, the
incident appears in the table as `DETECTED`, then `INVESTIGATING`, then
`AWAITING_APPROVAL`. The header flips to red.

Typical timing: incident opens 60–90 seconds after injection; investigation
completes within another 10–20 seconds.

### 4. Open the incident (60s)

Click the row. Walk the detail view left to right.

**Summary** — one sentence from Bedrock, grounded in the collected evidence.

**Investigation Timeline** — the key visual. Point at the ordering:

```
🚀  fault_injection on opspilot-showcase-demo-app     [opspilot-change-log]
🚀  UpdateFunctionConfiguration on ...                [cloudtrail]
🔴  CloudWatch alarm ...-demo-lambda-errors entered ALARM
🤖  OpsPilot investigation started
🤖  Root cause identified
```

> "The change is above the alarm. That relationship — *what changed immediately
> before the failure* — is the whole point."

**Infrastructure Changes** — each change is scored and labelled:

> "This is deterministic scoring, not AI: how close to onset, how wide the blast
> radius, does it touch the failing service. Changes that happened *after* the
> incident score zero — they cannot have caused it. And a reset or rollback is
> capped, because restoring health can't be what broke things.
>
> Note the two sources. CloudTrail is authoritative but takes minutes to
> deliver. OpsPilot's own change log is instant but only covers changes OpsPilot
> made. Neither alone is enough, so we correlate against both and label which is
> which."

**Root Cause** — with a confidence bar and category.

**Evidence** — the observed facts the conclusion rests on.

**Evidence Sources** — every source with UP/DOWN and a note.

> "If CloudTrail had been unavailable, this would say so. OpsPilot distinguishes
> 'we found no changes' from 'we could not look'."

### 5. Approve remediation (30s)

Scroll to **Recommended Remediation**.

> "Bedrock proposed an action. It did not *choose* one — it returned a label,
> which OpsPilot looked up in a fixed allowlist of six operations. Anything
> outside that list is refused and shown as 'manual remediation required'.
>
> The model has no tools, no credentials and no network access. And the IAM role
> that performs remediation can modify exactly one Lambda function ARN — this
> demo app. Even if every software check were bypassed, AWS itself would refuse
> anything else.
>
> Nothing happens until a human clicks this button."

Click **Approve Remediation**.

### 6. Watch recovery (~2–3 min)

The incident moves `REMEDIATING` → `VERIFYING` → `RESOLVED`.

Open the **Verification** card:

```
+  0s  ✓  HTTP 200 · 144ms · alarm ALARM
+ 30s  ✓  HTTP 200 ·  33ms · alarm ALARM
+ 60s  ✓  HTTP 200 ·  41ms · alarm ALARM
+ 90s  ✓  HTTP 200 ·  17ms · alarm ALARM
+120s  ✓  HTTP 200 ·  17ms · alarm OK
+150s  ✓  HTTP 200 ·  15ms · alarm OK
```

> "OpsPilot does not mark this resolved because remediation returned without an
> error. It invokes the actual service, repeatedly, and checks.
>
> Look at the alarm column: the service was healthy from the first probe, but
> the alarm took two more minutes to catch up. That lag is normal — CloudWatch
> has to observe a full healthy period. So the live probe decides and the alarm
> is reported as corroborating evidence. Verifying against the alarm alone would
> have failed a real recovery."

### 7. Show the postmortem (30s)

Click **Load postmortem**.

> "Written to S3 the moment the incident resolved. Every fact here — timestamps,
> the correlated change, the verification probes, the evidence-source table —
> comes from the stored incident record. Only the narrative sections are
> model-written, and if Bedrock were unavailable those would fall back to
> deterministic text."

### 8. Show incident memory (60s)

Click **← All incidents**, then **Inject Lambda Error** again.

When the second incident finishes investigating, open it and scroll to
**Similar Past Incidents**.

> "It recalled the first incident, with its resolution and outcome, and passed
> that into the analysis as context.
>
> No embeddings, no vector database. It is a DynamoDB index on a deterministic
> signature: affected service plus incident type. And the model is told
> explicitly that past incidents are context, not proof — it must not assume the
> same root cause."

### 9. Clean up

Click **Reset Environment**.

---

## The other scenarios

| Button | What actually happens | Alarm | Typical detection |
| --- | --- | --- | --- |
| **Inject Lambda Error** | Unhandled exception on every invocation | `AWS/Lambda Errors` | 60–90s |
| **Inject High Latency** | 4-second sleep per request | `AWS/Lambda Duration` | 90–150s |
| **Inject DB Throttling** | Write burst past the table's 1 WCU | `OpsPilot/DemoApp DbThrottles` | 60–150s |
| **Inject Application Errors** | HTTP 500 for ~80% of requests | `OpsPilot/DemoApp HttpErrors` | 90–150s |
| **Inject Configuration Failure** | Invalid config profile, every request rejected | `OpsPilot/DemoApp ConfigErrors` | 90–150s |

Each scenario maps to exactly one alarm, so one failure opens one incident.

**DB Throttling** is the most interesting technically: the demo table is
provisioned at 1 RCU / 1 WCU, inside the free tier, so a burst of ~4KB writes
produces genuine DynamoDB rejections. The incident record carries both the
application's throttle count and the authoritative
`AWS/DynamoDB WriteThrottleEvents` series. Nothing about it is simulated.

One quirk worth knowing if you demo it twice: DynamoDB accumulates burst
capacity while idle, so the first injection after a quiet period may take an
extra minute to start throttling while that credit drains.

**Configuration Failure** is the best one for showing change correlation, since
the config change and the failure are unambiguously linked.

---

## Command-line walkthrough

```bash
API=$(cd terraform && terraform output -raw opspilot_api_url)

curl -s "$API/health" | jq

curl -s -X POST "$API/demo/inject" \
  -H 'content-type: application/json' \
  -d '{"scenario":"configuration_error"}' | jq '.data.message'

# Watch the alarms move.
watch -n 10 "curl -s $API/demo/status | jq '.data.alarms'"

# Once the incident exists:
ID=$(curl -s "$API/incidents?limit=1" | jq -r '.data[0].incident_id')

curl -s "$API/incidents/$ID" | jq '{
  status, severity, incident_type,
  root_cause: .root_cause.description,
  confidence: .root_cause.confidence,
  change_summary,
  evidence
}'

# The correlated changes, ranked:
curl -s "$API/incidents/$ID" | jq '.changes[] | select(.correlation != "unrelated") | {
  action, resource, correlation, correlation_score, minutes_before_incident, source
}'

# Try something outside the allowlist - it is refused:
curl -s -X POST "$API/incidents/$ID/approve" \
  -H 'content-type: application/json' \
  -d '{"action":"delete_everything"}' | jq

# Approve the real recommendation:
ACTION=$(curl -s "$API/incidents/$ID" | jq -r '.recommendations[] | select(.executable) | .action' | head -1)
curl -s -X POST "$API/incidents/$ID/approve" \
  -H 'content-type: application/json' \
  -d "{\"action\":\"$ACTION\",\"approved_by\":\"demo\"}" | jq

# Verification detail:
curl -s "$API/incidents/$ID" | jq '.verification_detail | {status, reason}'

# The postmortem:
curl -s "$API/incidents/$ID/postmortem" | jq -r '.data.markdown' | head -50

curl -s -X POST "$API/demo/reset" | jq '.data.message'
```

---

## Automated end-to-end run

```bash
./scripts/smoke_test.sh
```

Seventeen steps covering the whole lifecycle: health, injection, real alarm
evaluation, detection, investigation, Bedrock analysis, change correlation,
allowlist refusal of a hostile action, approval, remediation, live-service
verification, resolution, postmortem generation and section checks, historical
retrieval, and incident recall on a repeat failure.

Takes 8–12 minutes because it waits on real CloudWatch alarm evaluation. Run a
different scenario with `./scripts/smoke_test.sh "" database_throttle`.

---

## Talking points

**"Isn't this just an AI wrapper on CloudWatch?"**
The AI does one job: explain evidence that deterministic code already collected.
It has no tools, no credentials, and no network. Detection, evidence collection,
change correlation, remediation and verification are all ordinary Lambda code.
Turn Bedrock off and OpsPilot still detects, investigates, correlates,
remediates and verifies — it just stops explaining.

**"What stops it doing something destructive?"**
Five independent controls, described in the README's Security section. The one
that matters most is IAM: the remediation role can modify exactly one Lambda
function ARN.

**"How do you know the fix worked?"**
It invokes the service and checks. Six probes over 150 seconds. If the service
is not healthy at the end, the incident goes to FAILED, not RESOLVED.

**"Why not a vector database for incident memory?"**
Because a DynamoDB GSI on `(service, incident_type)` answers the same question
for free, with predictable behaviour. The trade-off — exact matching rather than
similarity search — is documented rather than hidden.

**"What's the catch?"**
Several, all in the README's Limitations. The most important: CloudTrail
delivery is not instantaneous, so change correlation uses two sources and labels
which one each change came from. OpsPilot does not claim complete visibility into
every AWS change.
