# The Weather Muse — an always-on creative agent

Built for the **AWS Builder Center Weekend Challenge: Set Your Creative App Free**.

Every day, with no human involved, this agent:

1. Checks the current weather for a fixed location (free Open-Meteo API, no key).
2. Picks the next style in a slowly rotating palette (watercolor → ukiyo-e →
   cyberpunk neon → art deco → impressionist → pixel art → cosmic surrealism →
   ink wash → repeat), so its visual "voice" evolves over time.
3. Asks **Amazon Nova Canvas** (Bedrock) to paint an abstract artwork themed
   to today's weather + style.
4. Asks **Amazon Nova Micro** (Bedrock) to write a short poem in the same mood.
5. Saves everything to S3. A static gallery page reads `manifest.json` and
   renders the growing collection — the "tool you never have to open."

## Architecture

```
 EventBridge Scheduler (rate: 1 day)
        │  invokes
        ▼
   AWS Lambda (Python 3.12) ──► Open-Meteo API (weather)
        │        │
        │        ├──► Amazon Bedrock: Nova Canvas   (image)
        │        └──► Amazon Bedrock: Nova Micro     (poem)
        ▼
   S3 bucket (static website hosting)
        ├── index.html        (gallery UI)
        ├── manifest.json     (rolling list of last 30 days)
        ├── state.json        (style rotation counter)
        └── art/YYYY-MM-DD.png
```

All resources are provisioned by Terraform. No servers to manage.

## Prerequisites

1. An AWS account (Free Tier is fine — Lambda, S3, EventBridge Scheduler and
   CloudWatch Logs invocations here are all well within Free Tier limits;
   Bedrock Nova model invocations are billed per request but are inexpensive,
   a few cents per day at most for one image + one short poem).
2. **Enable Bedrock model access** (one-time, per account/region, usually
   instant): AWS Console → Amazon Bedrock → Model access → request access to
   **Amazon Nova Canvas** and **Amazon Nova Micro** in the region you deploy
   to (default `us-east-1`).
3. [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.5
4. AWS CLI configured (`aws configure`) with credentials that can create
   IAM roles, Lambda functions, S3 buckets, and EventBridge schedules.

## Deploy

```bash
unzip weekend-creative-agent.zip
cd weekend-creative-agent

terraform init
terraform apply
```

Review the plan and type `yes`. On `us-east-1` with defaults you don't need
to pass any variables — it deploys out of the box. To point it at your own
city, override variables:

```bash
terraform apply \
  -var="location_name=Nashik" \
  -var="latitude=19.9975" \
  -var="longitude=73.7898"
```

Terraform will:

- Create the S3 gallery bucket + upload the site
- Create the Lambda function and its IAM role
- Create the EventBridge Scheduler rule (runs daily, autonomously, forever)
- Invoke the Lambda once immediately (via `local-exec` + AWS CLI) so the
  gallery already has today's creation for your screenshots/article —
  disable with `-var="trigger_initial_run=false"` if you'd rather wait for
  the first scheduled run.

When it finishes, copy the `gallery_website_url` output into your browser.

## Testing / re-triggering manually

```bash
terraform output manual_invoke_command
# then run the printed command, e.g.:
aws lambda invoke --function-name weather-muse-agent --region us-east-1 out.json && cat out.json
```

Check `CloudWatch Logs → /aws/lambda/weather-muse-agent` if something fails
(most commonly: Bedrock model access not yet granted in that region).

## Tear down

```bash
terraform destroy
```

## Notes for the Builder Center submission

- **Article Requirements**: use `article-draft.md` in this repo as a
  starting point — fill in the sections marked `TODO`, add a screenshot of
  the deployed gallery, then publish it on AWS Builder Center with the title
  and `agents` tag as specified in the challenge rules.
- **Evidence of autonomous output**: after `terraform apply`, wait a day (or
  temporarily set `schedule_expression = "rate(1 hour)"` for testing) and
  take a screenshot showing two different dated entries in the gallery, plus
  a CloudWatch Logs screenshot showing the EventBridge Scheduler-triggered
  invocation (not a manual one) — this is the strongest evidence for the
  "Relevance & Functionality" judging category.
- **AWS services used**: Lambda, Amazon Bedrock (Nova Canvas, Nova Micro),
  S3 (storage + static website hosting), EventBridge Scheduler, IAM,
  CloudWatch Logs.
