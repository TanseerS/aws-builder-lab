# OpsPilot Troubleshooting

Symptoms, causes, and how to check.

---

## Quick diagnostics

```bash
cd terraform
API=$(terraform output -raw opspilot_api_url)
PREFIX="opspilot-$(terraform output -raw opspilot_region >/dev/null; echo showcase)"

curl -s "$API/health" | jq                # API + DynamoDB reachable?
curl -s "$API/demo/status" | jq '.data'   # demo config + live alarm states
curl -s "$API/metrics/summary" | jq '.data'
```

Every OpsPilot function logs structured JSON, so Logs Insights is the fastest
way in:

```bash
aws logs tail /aws/lambda/opspilot-showcase-investigator --since 15m --follow
aws logs tail /aws/lambda/opspilot-showcase-incident-detector --since 15m
aws logs tail /aws/lambda/opspilot-showcase-remediation --since 15m
aws logs tail /aws/lambda/opspilot-showcase-verifier --since 15m
```

---

## Deployment

### `terraform apply` fails: environment variables exceeded the 4KB limit

A Lambda's entire environment must fit in 4KB. If you have enlarged
`METRIC_CATALOG`, `ALARM_CATALOG`, or added scenarios, you may cross it.

Check what the plan will produce:

```bash
terraform plan -out=tfplan
terraform show -json tfplan | python3 -c "
import json,sys
p=json.load(sys.stdin)
for r in p.get('resource_changes',[]):
    if r['type']!='aws_lambda_function': continue
    a=r['change'].get('after') or {}
    e=a.get('environment') or []
    if not e: continue
    v=(e[0] or {}).get('variables') or {}
    n=sum(len(str(k))+len(str(x)) for k,x in v.items())
    print(('OVER' if n>=4096 else 'ok  '), n, a.get('function_name'))
"
```

The metric catalog is deliberately stored in a compact form
(`{"p": {probes}, "c": {scenario: [keys]}}`) with dimensions resolved at run
time — see `opspilot.evidence.load_metric_catalog`. Keep additions in that form.

### `terraform apply` fails on the CloudTrail bucket policy

CloudTrail validates it can write to the bucket at trail-creation time. If the
bucket policy has not propagated, the create fails. Re-running `terraform apply`
almost always resolves it. Otherwise:

```bash
terraform apply -var 'enable_cloudtrail=false'
```

Investigation still works — change correlation reads the free 90-day CloudTrail
*event history*, which exists whether or not you own a trail.

### The dashboard URL returns an error immediately after apply

CloudFront takes a few minutes to propagate a new distribution. Wait and reload.
To confirm the origin is fine:

```bash
aws s3 ls "s3://$(terraform output -raw opspilot_artifacts_bucket | sed 's/artifacts/frontend/')/"
```

### Changing `demo_baseline_env` has no effect

The demo function declares `lifecycle { ignore_changes = [environment] }`.
Terraform sets the baseline on first create, then stops managing that block —
otherwise every `terraform apply` would fight with an in-flight demo scenario
and silently "fix" a failure you were in the middle of demonstrating.

The consequence is that edits to `local.demo_baseline_env` in `main.tf` do not
propagate to an existing deployment. To pick them up, force a replacement:

```bash
terraform apply -replace='aws_lambda_function.demo_app'
```

The Demo Lab controller and the remediation function still read the *new*
baseline from their own environment variables, so `/demo/reset` will apply the
updated values even before the function itself is replaced.

### `terraform destroy` leaves a bucket behind

All three buckets set `force_destroy = true`, so this is rare. If it happens,
empty and delete manually:

```bash
aws s3 rm "s3://BUCKET" --recursive && aws s3 rb "s3://BUCKET"
```

---

## Detection

### No incident appears after injecting a failure

Work down this list in order.

**1. Did the injection actually apply?**

```bash
curl -s "$API/demo/status" | jq '.data.configuration'
```

`FAILURE_MODE` should be non-`none`. If it is `none`, the injection failed —
check the demo controller's logs.

**2. Is the demo app producing the failure?**

```bash
curl -i "$API/demo/app"
aws logs tail /aws/lambda/opspilot-showcase-demo-app --since 5m
```

**3. Is the alarm firing?**

```bash
curl -s "$API/demo/status" | jq '.data.alarms'
```

If every alarm is `OK` or `INSUFFICIENT_DATA` several minutes after injection,
the metric is not reaching CloudWatch. The most common cause is **no traffic**:
alarms can only evaluate datapoints that exist.

```bash
aws lambda get-function-configuration \
  --function-name opspilot-showcase-traffic-generator \
  --query 'FunctionName'

aws events list-rules --name-prefix opspilot-showcase-demo-traffic
```

If you deployed with `enable_traffic_generator=false`, generate traffic
yourself:

```bash
for i in $(seq 1 10); do curl -s "$API/demo/app" >/dev/null; done
```

`INSUFFICIENT_DATA` on the custom-metric alarms (`HttpErrors`, `ConfigErrors`)
before any traffic has flowed is normal — the metric does not exist until the
demo app first emits it.

**4. Did EventBridge deliver the transition?**

```bash
aws cloudwatch describe-alarm-history \
  --alarm-name opspilot-showcase-demo-lambda-errors \
  --max-records 5 --query 'AlarmHistoryItems[].[Timestamp,HistorySummary]' --output table

aws logs tail /aws/lambda/opspilot-showcase-incident-detector --since 10m
```

The rule only matches `state.value == "ALARM"`. An alarm already *in* ALARM
publishes nothing — there is no state change. Reset the environment, wait for it
to return to OK, then inject again.

### Two incidents for one failure

Each scenario is designed to trip exactly one alarm. If you see two, check which
alarms fired:

```bash
curl -s "$API/demo/status" | jq '.data.alarms_firing'
```

If a crash produced both `Errors` and `HttpErrors`, the demo app's metric
semantics have been changed — see `_emit_metrics` in `lambda/demo_app/handler.py`
and the "one failure, one alarm, one incident" section of
[`architecture.md`](architecture.md).

### The same alarm opened several incidents over time

Expected, if the alarm genuinely re-entered ALARM more than five minutes apart.
The dedupe key buckets by `alarm_name + 5-minute window`; flapping inside one
bucket collapses to a single incident, but separate episodes are separate
incidents.

---

## Investigation

### The incident is stuck in `DETECTED`

The investigator was never triggered, or it failed.

```bash
aws logs tail /aws/lambda/opspilot-showcase-investigator --since 15m
aws events list-rules --event-bus-name opspilot-showcase-events
```

Retry from the dashboard's **Re-investigate** button, or:

```bash
curl -s -X POST "$API/incidents/$ID/reinvestigate" | jq
```

### The incident is stuck in `INVESTIGATING`

The investigator started and did not finish. Almost always a Bedrock timeout on
a slow model. Its timeout is 180s; the Bedrock read timeout is 60s with 4
retries.

```bash
aws logs filter-log-events \
  --log-group-name /aws/lambda/opspilot-showcase-investigator \
  --filter-pattern '{ $.event = "bedrock_*" }' --max-items 20
```

Re-investigate to retry.

### `ai_status` is `UNAVAILABLE`

Bedrock could not be reached. **This is handled, not broken** — the incident
still exists, still has deterministic evidence, and still offers a safe default
remediation. The dashboard says "AI investigation unavailable".

Most common cause: **model access is not enabled for your account.**

```bash
aws bedrock list-foundation-models --region us-east-1 \
  --query 'modelSummaries[?modelId==`amazon.nova-lite-v1:0`].modelId'

# Confirm you can actually invoke it:
cat > /tmp/m.json <<'EOF'
[{"role":"user","content":[{"text":"Return only JSON: {\"ok\": true}"}]}]
EOF
aws bedrock-runtime converse --region us-east-1 \
  --model-id amazon.nova-lite-v1:0 --messages file:///tmp/m.json \
  --inference-config '{"maxTokens":50,"temperature":0.1}'
```

`AccessDeniedException` means model access must be granted in the console under
**Bedrock → Model access**. There is no Terraform resource for this; it is the
one genuine manual prerequisite.

Other causes: the model id is not available in `var.aws_region`, or sustained
`ThrottlingException` (the logs will show `bedrock_retry` lines).

### `ai_status` is `FALLBACK`

Bedrock responded, but the output could not be used — unparseable, or it did not
match the schema.

```bash
aws logs filter-log-events \
  --log-group-name /aws/lambda/opspilot-showcase-investigator \
  --filter-pattern '{ $.event = "bedrock_json_parse_failed" }' --max-items 5
```

The logged `preview` field shows the first 300 characters of what the model
returned. If a particular model returns prose consistently, try a stronger one:

```bash
terraform apply -var 'bedrock_model_id=amazon.nova-pro-v1:0'
```

### No changes were correlated

Two different situations, and the UI distinguishes them.

**"No infrastructure changes were found"** — the window was genuinely empty.
Widen it:

```bash
terraform apply -var 'change_lookback_minutes=30'
```

**"CloudTrail evidence unavailable"** — the lookup failed. Check the
`evidence_sources` map on the incident:

```bash
curl -s "$API/incidents/$ID" | jq '.evidence_sources'
```

Remember that **CloudTrail delivery is not instantaneous.** A change made
seconds before an incident may not be visible yet. This is why OpsPilot also
correlates against its own change log, and why every change carries a `source`.

### The wrong change is ranked highest

Scoring weights live in `lambda/shared/python/opspilot/change_correlator.py`.
Two rules are worth knowing before you tune anything:

- Changes after onset score zero.
- Restorative actions (`remediation`, `configuration_reset`, `restore`,
  `rollback`) are capped at 0.30 — a change that restores health cannot be the
  cause of a new failure.

Every ranked change carries `correlation_reasons` explaining its score:

```bash
curl -s "$API/incidents/$ID" | jq '.changes[0] | {action, correlation_score, correlation_reasons}'
```

---

## Remediation

### Approval returns `manual_remediation_required`

Nothing the analysis proposed mapped to an allowlisted action. This is the
safety boundary working. Inspect what was proposed:

```bash
curl -s "$API/incidents/$ID" | jq '.recommendations'
```

`allowlisted: false` means the label was not recognised. Either the model
proposed something genuinely outside the allowlist, or a new alias belongs in
`_ALIASES` in `remediation_actions.py`.

### Approval returns `action_not_recommended`

You asked to run an action this investigation did not recommend. Deliberate: the
approval endpoint will only execute from the recommendations attached to *that*
incident. Approve with no `action` field to take the first executable
recommendation.

### Approval returns `invalid_state`

The incident is not in `AWAITING_APPROVAL`. Check its status — it may already
have been remediated, rejected, or still be investigating.

### Remediation failed

```bash
curl -s "$API/incidents/$ID" | jq '.remediation_detail'
aws logs tail /aws/lambda/opspilot-showcase-remediation --since 15m
```

`"Configuration did not take effect"` means the function configuration was
written but read back differently — usually a concurrent update. Reset the
environment and re-run.

Note that remediation has **zero** EventBridge retries by design: a failed
mutation moves the incident to `FAILED` for a human rather than retrying blindly.

---

## Verification

### Verification failed but the service looks fine

```bash
curl -s "$API/incidents/$ID" | jq '.verification_detail.checks'
```

Each probe records `healthy`, `status_code`, `duration_ms` and `alarm_state`.
`VERIFICATION_FAILED` means the final probe was unhealthy, or probes were
intermittent.

If the fault takes longer than the window to clear, widen it:

```bash
terraform apply -var 'verification_checks=10' -var 'verification_interval_seconds=30'
```

The verifier's Lambda timeout is computed from these, so both scale together.

### The verdict mentions the alarm still clearing

Expected and honest. CloudWatch alarm evaluation lags real recovery — the alarm
must observe a full healthy period. The live probe is decisive; the alarm state
is recorded as corroborating evidence and the lag is stated explicitly rather
than hidden.

---

## Postmortems

### `postmortem_not_ready`

Postmortems are generated only when an incident reaches `RESOLVED` or `FAILED`.
If the incident is terminal but the document is missing:

```bash
aws logs tail /aws/lambda/opspilot-showcase-postmortem --since 15m
aws s3 ls "s3://$(cd terraform && terraform output -raw opspilot_artifacts_bucket)/postmortems/"
```

### Narrative sections read generically

Check `postmortem_narrative_source`. `deterministic` means Bedrock was
unavailable for the narrative and the document fell back to template prose. All
the *facts* are still correct — they come from the incident record either way.

---

## Dashboard

### "OpsPilot configuration is missing"

`config.js` did not load. It is generated by Terraform and uploaded to the
frontend bucket:

```bash
aws s3 ls "s3://PREFIX-frontend-SUFFIX/"   # expect config.js, index.html, app.js, styles.css
terraform apply                            # regenerates and re-uploads
```

### "API unreachable"

```bash
curl -s "$API/health"
```

If curl works but the browser does not, it is CORS or a stale cached bundle.
Hard-reload. The API sets `Access-Control-Allow-Origin: *`, and the HTTP API has
CORS configured at the gateway.

### The dashboard shows stale data

It polls every 15 seconds. Click **Refresh** to force it.

### Changes to the frontend do not appear

Objects are uploaded with `Cache-Control: no-cache, must-revalidate`, but
CloudFront may still hold an edge copy:

```bash
aws cloudfront create-invalidation \
  --distribution-id $(aws cloudfront list-distributions \
    --query "DistributionList.Items[?Comment=='OpsPilot showcase dashboard'].Id" \
    --output text) \
  --paths '/*'
```

---

## Cost

### Higher than expected

The usual suspects, in order:

1. **Custom CloudWatch metrics** — five, at ~$0.30/metric/month beyond the free
   tier. This is normally the largest recurring item.
2. **Traffic generator** — 1 invocation/minute ≈ 43k/month. Inside the free
   tier, but it does keep the demo app warm and producing metrics.
3. **Bedrock tokens** — per investigation. Injecting failures repeatedly costs
   real money, though a fraction of a cent each on Nova Lite.
4. **CloudTrail S3 storage** — logs expire after 30 days.

To minimise idle cost:

```bash
terraform apply -var 'enable_traffic_generator=false' -var 'enable_cloudtrail=false'
```

Detection becomes slower and less reliable without synthetic traffic, because
alarms need datapoints to evaluate.

---

## Development

```bash
python -m pytest tests/ -q          # unit tests, no AWS needed
./scripts/package_lambdas.sh        # syntax, handlers, layer layout, zips
cd terraform && terraform fmt -check && terraform validate
./scripts/smoke_test.sh             # full lifecycle against a live deployment
```

### Tests fail with `ModuleNotFoundError: opspilot`

`tests/conftest.py` puts `lambda/shared/python` on `sys.path`. Run pytest from
the repository root.

### Adding a remediation action

1. Add an `ActionSpec` to `ALLOWED_ACTIONS` in `remediation_actions.py`.
2. Add likely model phrasings to `_ALIASES`.
3. If it writes new environment keys, add them to `demo_mutable_env_keys` and
   `demo_baseline_env` in `terraform/main.tf`.
4. Add a test asserting it resolves — and that nearby unsafe strings do not.

The IAM boundary does not need widening as long as the action only changes the
demo function's configuration. If it needs a new AWS API, that is a deliberate
expansion of the safety boundary and should be reviewed as one.

### Adding a failure scenario

1. Add a branch to `lambda/demo_app/handler.py`, emitting a **specific** metric.
   Do not also emit `HttpErrors` — that would double-count and open two
   incidents.
2. Add the scenario to `SCENARIOS` in `lambda/demo_controller/handler.py`.
3. Add an alarm in `terraform/cloudwatch.tf` and an entry in `local.alarm_names`.
4. Add an `alarm_catalog` entry so the detector can classify it.
5. Add metric probes to `local.metric_probes` / `local.metric_scenarios`.
6. Add a button in `frontend/index.html`.
