"""
Image generation with Gemini Flash.

Only the "image" kind of visual card comes here. Diagrams are Mermaid text and animations are HTML, both written by the
text model. A visual card's prompt, its elements and the agenda item it serves become the image prompt, so the picture
stays inside the class scope.

Settings (backend/.env):
  GEMINI_API_KEY         the key; without it, image cards get the built-in placeholder graphic instead
  GEMINI_IMAGE_MODEL     the Gemini image model        (default gemini-2.5-flash-image)
  GEMINI_IMAGE_ASPECT    aspect ratio of the picture   (default 16:9)
  PUBLIC_BASE_URL        if set, saved image links are absolute (for exports); otherwise they start with /generated/

Generated files are saved in backend/generated/ and served from /generated/.
"""

import base64
import hashlib
import os
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

load_dotenv()

DEFAULT_IMAGE_MODEL = "gemini-2.5-flash-image"
GENERATED_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "generated")
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


def images_enabled() -> bool:
    """Picture visuals (web search and Gemini) are off unless IMAGES_ENABLED=1. While off, planned images become flow diagrams."""
    return (os.getenv("IMAGES_ENABLED") or "0").strip().lower() in ("1", "true", "yes", "on")


def without_image_option(prompt: str) -> str:
    """The prompt with the 'image' choice taken out, so the model is never told it may plan a picture. No change when images are on."""
    if images_enabled():
        return prompt
    for old, new in (
        ('"mermaid | animation | image"', '"mermaid | animation"'),
        (', or "image" for high-impact conceptual diagrams', ''),
        ('"animation" (a step-by-step change over time) or "image".', '"animation" (a step-by-step change over time).'),
    ):
        prompt = prompt.replace(old, new)
    return prompt


class ImageUnavailable(Exception):
    """No image could be made. The message says why, in words fit to show a user."""


def gemini_api_key() -> Optional[str]:
    key = os.getenv("GEMINI_API_KEY")
    return key.strip() if key and key.strip() else None


def image_configured() -> bool:
    return bool(gemini_api_key())


def image_model() -> str:
    return os.getenv("GEMINI_IMAGE_MODEL") or DEFAULT_IMAGE_MODEL


def build_image_prompt(spec: Dict[str, Any], running_example: Optional[str] = None) -> str:
    """The text sent to the image model, from a visual request: intent, labels, scope and the running example."""
    labels: List[str] = [str(n) for n in (spec.get("nodes") or [])][:7]
    scope = spec.get("serves") or spec.get("title") or spec.get("intent") or ""
    lines = [
        "Create one clean, flat, vector-style educational illustration on a plain white background, high contrast, "
        "generous spacing, a calm palette of blue and teal with one orange accent.",
        f"It must show: {spec.get('intent') or spec.get('title') or 'the concept'}.",
        f"It serves this part of a lecture and nothing else: {scope}.",
    ]
    if running_example:
        lines.append(f"If it shows a system, use this example and its names: {running_example}.")
    if labels:
        lines.append("Show at most 7 elements. The only text allowed in the picture is these short labels, spelled exactly: "
                     + "; ".join(labels) + ".")
    else:
        lines.append("Use at most 7 elements and as little text as possible: single short words only.")
    lines.append("No paragraphs of text, no real brand logos or trademarks, no photographs of real people, no watermark.")
    return " ".join(lines)


def generate_image(prompt: str, aspect_ratio: Optional[str] = None, timeout: int = 120) -> Tuple[bytes, str]:
    """One image from Gemini. Returns (bytes, mime type). Raises ImageUnavailable with a readable reason."""
    key = gemini_api_key()
    if not key:
        raise ImageUnavailable("GEMINI_API_KEY is not configured.")
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": aspect_ratio or os.getenv("GEMINI_IMAGE_ASPECT", "16:9")},
        },
    }
    try:
        # the key goes in a header, not the URL, so it never lands in a log line
        resp = requests.post(ENDPOINT.format(model=image_model()), json=body, timeout=timeout,
                             headers={"x-goog-api-key": key, "Content-Type": "application/json"})
    except requests.RequestException as e:
        raise ImageUnavailable(f"The image service could not be reached ({type(e).__name__}).")
    if resp.status_code != 200:
        detail = ""
        try:
            detail = (resp.json().get("error") or {}).get("message", "")
        except Exception:
            detail = resp.text[:160]
        if resp.status_code in (400, 401, 403) and ("API key" in detail or "API_KEY" in detail or resp.status_code in (401, 403)):
            raise ImageUnavailable("The Gemini API key was rejected. Check GEMINI_API_KEY.")
        if resp.status_code == 429:
            raise ImageUnavailable("The Gemini image limit was reached. Try again in a minute, or raise the plan's quota.")
        raise ImageUnavailable(f"Gemini returned HTTP {resp.status_code}: {detail[:160]}")
    try:
        parts = resp.json()["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError, ValueError):
        raise ImageUnavailable("Gemini answered without an image (the request may have been blocked by its safety filter).")
    for part in parts:
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            return base64.b64decode(inline["data"]), inline.get("mimeType") or inline.get("mime_type") or "image/png"
    raise ImageUnavailable("Gemini answered with text and no image. Make the visual card's prompt more concrete.")


def save_image(data: bytes, mime: str, name_hint: str, directory: str = GENERATED_DIR) -> str:
    """Save the picture under backend/generated/ and return the link it is served from."""
    os.makedirs(directory, exist_ok=True)
    digest = hashlib.sha1(data).hexdigest()[:10]
    safe = "".join(ch for ch in name_hint if ch.isalnum() or ch in "-_")[:40] or "image"
    filename = f"{safe}_{digest}.{EXTENSIONS.get(mime, 'png')}"
    with open(os.path.join(directory, filename), "wb") as f:
        f.write(data)
    base = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")
    return f"{base}/generated/{filename}"


def make_image_asset(spec: Dict[str, Any], running_example: Optional[str] = None) -> Dict[str, Any]:
    """Prompt, generate and save. Returns the asset dict the rest of the app stores for an image visual."""
    prompt = build_image_prompt(spec, running_example)
    data, mime = generate_image(prompt)
    url = save_image(data, mime, str(spec.get("id") or "image"))
    return {"id": spec.get("id", "img_01"), "type": "image", "content": url, "alt_text": spec.get("intent") or spec.get("title") or "",
            "caption": f"Figure: {spec.get('title') or spec.get('intent') or ''}", "licence": "AI-generated (Gemini)",
            "source": "gemini", "model": image_model(), "generated": True}
