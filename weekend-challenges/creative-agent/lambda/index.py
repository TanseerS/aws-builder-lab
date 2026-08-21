"""
Weekend Creative Agent - Daily Weather-Themed Art & Poem Generator
--------------------------------------------------------------------
Runs on a schedule (EventBridge Scheduler -> Lambda). On every invocation
it:
  1. Fetches current weather for a fixed location from the free,
     key-less Open-Meteo API.
  2. Rotates through an evolving list of art styles (so the agent's
     visual "voice" changes over time).
  3. Asks a Stability text-to-image model (Bedrock) to paint an
     abstract artwork themed to today's weather + art style.
  4. Asks Amazon Nova Micro (Bedrock) to write a short poem in the same
     mood.
  5. Stores the image + a running manifest.json in S3, which the static
     gallery site (index.html) reads and renders.

No manual trigger is required after deployment - EventBridge Scheduler
calls this function autonomously every day.
"""

import base64
import datetime
import json
import os
import random
import urllib.request

import boto3

s3 = boto3.client("s3")

BUCKET = os.environ["BUCKET_NAME"]
LOCATION_NAME = os.environ.get("LOCATION_NAME", "Nashik")
LAT = os.environ.get("LATITUDE", "19.9975")
LON = os.environ.get("LONGITUDE", "73.7898")
IMAGE_MODEL_ID = os.environ.get(
    "IMAGE_MODEL_ID", "stability.stable-image-core-v1:1"
)
TEXT_MODEL_ID = os.environ.get("TEXT_MODEL_ID", "amazon.nova-micro-v1:0")

# The image model is not offered in every region, so it gets its own
# bedrock-runtime client. The text model runs in the Lambda's own region.
IMAGE_REGION = os.environ.get("IMAGE_MODEL_REGION", "us-west-2")
IMAGE_ASPECT_RATIO = os.environ.get("IMAGE_ASPECT_RATIO", "1:1")

bedrock_image = boto3.client("bedrock-runtime", region_name=IMAGE_REGION)
bedrock_text = boto3.client("bedrock-runtime")

# The agent slowly cycles through these styles, one per run, so its
# creative output visibly evolves over the life of the deployment.
STYLE_ROTATION = [
    "soft watercolor",
    "vibrant ukiyo-e woodblock print",
    "moody cyberpunk neon",
    "warm art deco",
    "dreamy impressionist",
    "retro pixel art",
    "cosmic surrealism",
    "minimalist ink wash",
]

WEATHER_CODES = {
    0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "rime fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 56: "light freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "light freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "light snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail",
    99: "severe thunderstorm with hail",
}


def get_weather():
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        "&current=temperature_2m,weather_code&timezone=auto"
    )
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    current = data.get("current", {})
    code = current.get("weather_code", 0)
    return {
        "temperature": current.get("temperature_2m"),
        "description": WEATHER_CODES.get(code, "changeable skies"),
    }


def get_state():
    try:
        obj = s3.get_object(Bucket=BUCKET, Key="state.json")
        return json.loads(obj["Body"].read())
    except s3.exceptions.NoSuchKey:
        return {"style_index": 0}
    except Exception:
        return {"style_index": 0}


def save_state(state):
    s3.put_object(
        Bucket=BUCKET,
        Key="state.json",
        Body=json.dumps(state).encode(),
        ContentType="application/json",
    )


def get_manifest():
    try:
        obj = s3.get_object(Bucket=BUCKET, Key="manifest.json")
        return json.loads(obj["Body"].read())
    except s3.exceptions.NoSuchKey:
        return []
    except Exception:
        return []


def save_manifest(manifest):
    s3.put_object(
        Bucket=BUCKET,
        Key="manifest.json",
        Body=json.dumps(manifest).encode(),
        ContentType="application/json",
        CacheControl="no-cache",
    )


def generate_image(prompt, seed):
    """Render the prompt with a Stability text-to-image model.

    This API takes a flat prompt plus an aspect_ratio (it has no
    width/height knobs) and answers with base64 PNGs under "images".
    """
    body = {
        "prompt": prompt[:1000],
        "mode": "text-to-image",
        "aspect_ratio": IMAGE_ASPECT_RATIO,
        "output_format": "png",
        # The Stability seed space is narrower than a 32-bit signed int.
        "seed": seed % 4_294_967_295,
    }
    response = bedrock_image.invoke_model(
        modelId=IMAGE_MODEL_ID,
        body=json.dumps(body),
    )
    result = json.loads(response["body"].read())

    # A non-null finish_reason means the model filtered the render, in
    # which case "images" holds a blank frame rather than artwork.
    reason = (result.get("finish_reasons") or [None])[0]
    if reason:
        raise RuntimeError(f"Image generation was filtered: {reason}")

    return base64.b64decode(result["images"][0])


def generate_poem(weather_desc, temperature, style, day_name):
    prompt = (
        "Write a short, evocative 4-line free-verse poem (respond with "
        "ONLY the poem, no title and no explanation) inspired by a "
        f"{weather_desc} {day_name} in {LOCATION_NAME} at {temperature}"
        f"\u00b0C. Let the mood echo a {style} artistic sensibility."
    )
    response = bedrock_text.converse(
        modelId=TEXT_MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 150, "temperature": 0.9},
    )
    return response["output"]["message"]["content"][0]["text"].strip()


def handler(event, context):
    today = datetime.date.today()
    day_name = today.strftime("%A")

    weather = get_weather()
    state = get_state()
    style = STYLE_ROTATION[state["style_index"] % len(STYLE_ROTATION)]

    image_prompt = (
        f"An abstract {style} artwork capturing the feeling of a "
        f"{weather['description']} {day_name} in {LOCATION_NAME}, "
        f"temperature around {weather['temperature']} degrees Celsius, "
        "no text, no watermark, no words, high detail, artistic composition"
    )

    seed = random.randint(0, 2_147_483_646)
    image_bytes = generate_image(image_prompt, seed)
    poem = generate_poem(
        weather["description"], weather["temperature"], style, day_name
    )

    date_str = today.isoformat()
    image_key = f"art/{date_str}.png"
    s3.put_object(
        Bucket=BUCKET,
        Key=image_key,
        Body=image_bytes,
        ContentType="image/png",
    )

    manifest = get_manifest()
    # Replace today's entry if the function runs more than once on the
    # same day (e.g. manual test invoke followed by the schedule).
    manifest = [m for m in manifest if m.get("date") != date_str]
    manifest.insert(0, {
        "date": date_str,
        "day": day_name,
        "image": image_key,
        "poem": poem,
        "style": style,
        "weather": weather["description"],
        "temperature": weather["temperature"],
        "location": LOCATION_NAME,
    })
    manifest = manifest[:30]
    save_manifest(manifest)

    state["style_index"] = state["style_index"] + 1
    save_state(state)

    return {
        "statusCode": 200,
        "body": json.dumps({"date": date_str, "image": image_key, "style": style}),
    }
