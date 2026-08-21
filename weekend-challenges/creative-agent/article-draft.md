<!--
  Publish this on AWS Builder Center (builder.aws.com) between
  Aug 21, 2026 12:00 AM PT and Aug 24, 2026 1:00 PM PT.
  Required title format: "Weekend Creative Agent Challenge: [Name of Your Project]"
  Required tag: agents (plus #aws-community-builders is recommended for reach)
  Minimum 500 words across all sections below.
-->

# Weekend Creative Agent Challenge: The Weather Muse

Tags: `agents`, `aws-community-builders`, `serverless`, `bedrock`, `terraform`

## Vision & What It Does

The challenge prompt asks for a tool you never have to open — an agent that
makes something new on its own and has it ready when you return. I built
**The Weather Muse**: a fully autonomous creative agent that wakes up once a
day, checks the current weather over TODO_CITY, and produces a small piece
of art inspired by that moment — an abstract painting from Amazon Nova
Canvas paired with a short poem from Amazon Nova Micro, both themed to the
day's weather, temperature, and an evolving art style.

Nobody prompts it. Nobody opens an app to "generate" anything. It simply
runs, quietly, on a schedule, and by the time I check the gallery there is
already something new waiting — today's mood, painted and written before I
asked. Over time the collection becomes a kind of visual weather diary of
TODO_CITY, with a style that slowly drifts through watercolor, ukiyo-e,
cyberpunk neon, art deco, impressionism, pixel art, cosmic surrealism, and
ink wash, one step further each day.

## How You Built It

I wanted the whole thing to be a single `terraform apply` — no manual
console clicking, no glue scripts left for "later." The build came together
in three layers:

**Trigger layer.** Amazon EventBridge Scheduler calls the Lambda function
once a day. This was the cleanest way to get "always-on, autonomous"
behavior without running any persistent compute — the agent doesn't exist
between invocations, which keeps it squarely inside Free Tier territory.

**Brain layer.** A single Python 3.12 Lambda function does everything:
fetches weather from the free Open-Meteo API (no API key, no cost), builds a
prompt from the weather + today's rotating style, calls Bedrock's
`InvokeModel` for Nova Canvas (image) and `Converse` for Nova Micro (poem),
then writes the results to S3.

**Memory & presentation layer.** Rather than a database, I kept state as two
small JSON files in S3: `manifest.json` (a rolling window of the last 30
days of creations) and `state.json` (just the style-rotation counter). A
static `index.html` page fetches `manifest.json` client-side and renders the
gallery — no backend needed to view it, which also means the site loads
instantly and costs essentially nothing to host.

The trickiest part was getting the Nova Canvas request/response shape right
— it uses `taskType: TEXT_IMAGE` with a `textToImageParams` object rather
than a chat-style message, unlike the text models. Once I separated "image
call" from "text call" into two clearly distinct functions with their own
payload shapes, everything else was straightforward. I also added a
`local-exec` provisioner that invokes the Lambda once immediately after
`terraform apply`, purely so the gallery isn't empty the first time you open
it — the scheduler takes over from there.

## AWS Services Used / Architecture Overview

- **AWS Lambda** — the agent's runtime, Python 3.12
- **Amazon Bedrock (Nova Canvas + Nova Micro)** — image and poem generation
- **Amazon EventBridge Scheduler** — daily autonomous trigger
- **Amazon S3** — static website hosting for the gallery + storage for
  generated art, manifest, and rotation state
- **AWS IAM** — least-privilege roles for the Lambda and the Scheduler
- **Amazon CloudWatch Logs** — execution logs / observability

```
EventBridge Scheduler (rate: 1/day)
        │
        ▼
   AWS Lambda ──► Open-Meteo (weather)
        │
        ├──► Bedrock Nova Canvas (image)
        └──► Bedrock Nova Micro  (poem)
        │
        ▼
   S3 (static site + manifest.json + art/*.png)
```

Everything is defined in Terraform — `terraform apply` provisions the
bucket, IAM roles, Lambda, and schedule end to end.

## What You Learned

TODO — write 2-4 sentences in your own words, for example: what surprised
you about Nova Canvas's prompt/response format, what you'd add next (e.g. an
agent that reads yesterday's poem before writing today's, to build real
continuity), or what you learned about designing a genuinely stateless,
event-driven "always-on" agent versus a long-running one.

## Link to App or Repo

- Live gallery: TODO_WEBSITE_URL (from `terraform output gallery_website_url`)
- Source: TODO_GITHUB_REPO_URL
