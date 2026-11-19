import os

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

CANDIDATE_MODELS = [
    os.getenv("GEMINI_MODEL"),
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]
CANDIDATE_MODELS = [m for m in CANDIDATE_MODELS if m]

PROMPT = """You are a network engineer. Look at the attached network diagram image.

Return ONLY valid JSON with these keys:
- "devices": a list of devices, each with "name" and "type"
- "links": a list of connections, each with "from" and "to"
- "networks": a list of subnets shown in the image
- "addresses": a list of IP addresses shown, each with "device", "ip" (without any mask), and "network" (the subnet it belongs to, like 10.0.0.0/24)
- "routing": any routing protocol information shown in the image
- "ambiguities": a list of things that are missing or unclear

Rules:
- Only use information that is clearly visible in the image.
- If something is not visible or unclear, do NOT guess. Add a note about it to "ambiguities" instead.
- Do not invent interface names. If interface names (such as GigabitEthernet0/0) are not visible, add that to "ambiguities".
- Also add to "ambiguities" anything the diagram does not show: gateways, router IDs, and routing protocol details.
- Where a number like ".1" is shown next to a device, combine it with the subnet label to give the full IP address.
"""


def analyze_image(path):
    ext = os.path.splitext(path)[1].lower()
    mime_type = "image/png" if ext == ".png" else "image/jpeg"

    with open(path, "rb") as f:
        image_bytes = f.read()

    api_key = os.getenv("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key) if api_key else genai.Client()
    last_err = None
    for model_name in CANDIDATE_MODELS:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    PROMPT,
                ],
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            return response.text
        except Exception as e:
            last_err = e
            continue
    raise last_err or RuntimeError("No compatible Gemini model could generate content.")


if __name__ == "__main__":
    import sys
    print(analyze_image(sys.argv[1]))
