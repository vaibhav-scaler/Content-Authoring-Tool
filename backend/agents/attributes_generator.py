import re
"""
Agent 3: Attributes Generator Agent
Generates the visual assets of the outline's visual cards, one card at a time, right after the card they sit on is written.
Who makes what:
  diagram    (Mermaid flow)  Gemini Flash
  image      (a picture)     Gemini Flash image model (agents/image_gen.py)
  animation  (HTML)          Claude Sonnet 5.5
Each result says where it came from (source: gemini | claude | placeholder). When a provider has no key or fails, the
card gets a labelled placeholder, never a silent fake.
"""

import html
import os
import json
from typing import Dict, Any, Optional, List
from dotenv import load_dotenv

load_dotenv()

from agents.llm import LLMUnavailable, call_llm_chat, llm_configured, mock_fallback_allowed

EXECUTION_MODE = os.getenv("EXECUTION_MODE", "hybrid")


# ── Attributes Generator System Prompt ───────────────────────────────────────

ATTR_GEN_SYSTEM = """\
You are the Attributes Generator for Scaler's script authoring tool.
{block_a}

RULES
- 7 nodes or fewer. Before/after is two separate visuals. Flows show one step per stage.
- Use only terms already defined in the card or earlier.
- Use names from the running example exactly.
- No paid assets or copyrighted material. Free resources only.
"""


def _generate_visual_raw(
    visual_request: Dict[str, Any],
    card_text: str,
    running_example: str,
    block_a_context: Optional[str] = None,
    script_id: Optional[int] = None
) -> Dict[str, Any]:
    """
    Generates a visual asset based on the visual_request from the Writer agent.

    visual_request keys: id, type, intent, nodes, step, card_id
    Returns: {"id": str, "type": str, "content": str, "alt_text": str,
              "caption": str, "licence": str|null}
    """
    vis_type = visual_request.get("type", "mermaid")
    from agents.image_gen import images_enabled
    if vis_type == "image" and not images_enabled():
        vis_type = "mermaid"                 # pictures are switched off (IMAGES_ENABLED): draw a flow diagram instead

    if vis_type == "mermaid":
        return _generate_mermaid(visual_request, card_text, running_example, block_a_context, script_id=script_id)
    elif vis_type == "animation":
        return _generate_animation(visual_request, card_text, running_example, block_a_context, script_id=script_id)
    elif vis_type == "image":
        return _generate_image_spec(visual_request, card_text, running_example, script_id=script_id)
    else:
        return {
            "id": visual_request.get("id", "vis_unknown"),
            "type": vis_type,
            "content": f"[[VISUAL: {visual_request.get('intent', 'unknown')}]]",
            "alt_text": visual_request.get("intent", "Visual placeholder"),
            "caption": f"Visual: {visual_request.get('intent', '')}",
            "licence": None
        }


def generate_visuals_batch(
    visual_requests: List[Dict[str, Any]],
    cards: List[Dict[str, Any]],
    running_example: str,
    block_a_context: Optional[str] = None,
    script_id: Optional[int] = None
) -> List[Dict[str, Any]]:
    """
    Generates all visual assets for a section's visual_requests.
    Returns a list of generated assets.
    """
    results = []
    card_text_map = {c.get("card_id", ""): c.get("body_markdown", "") for c in cards}

    for req in visual_requests:
        card_id = req.get("card_id", "")
        card_text = card_text_map.get(card_id, "")
        try:
            asset = generate_visual(req, card_text, running_example, block_a_context, script_id=script_id)
        except LLMUnavailable as e:                          # a card goes without its visual rather than with an unrelated one; Regenerate fixes it
            print(f"Visual {req.get('id')} skipped: {e}")
            continue
        results.append(asset)

    return results


# ── Mermaid Generation ───────────────────────────────────────────────────────

def mermaid_provider() -> Optional[str]:
    """Who draws the flow diagrams. MERMAID_PROVIDER=anthropic|gemini forces one; otherwise Gemini when it has a key, else Claude.
    None when neither is configured."""
    wanted = (os.getenv("MERMAID_PROVIDER") or "").strip().lower()
    order = [wanted] if wanted in ("anthropic", "gemini") else ["gemini", "anthropic"]
    return next((p for p in order if llm_configured(p)), None)


def _generate_mermaid(
    visual_request: Dict[str, Any],
    card_text: str,
    running_example: str,
    block_a_context: Optional[str] = None,
    script_id: Optional[int] = None
) -> Dict[str, Any]:
    """Generates a valid Mermaid diagram with max 7 nodes."""
    vis_id = visual_request.get("id", "mermaid_01")
    intent = visual_request.get("intent", "system flow")
    extra_line = f"ALSO: {visual_request['extra']}\n" if visual_request.get("extra") else ""
    nodes = visual_request.get("nodes", [])

    mermaid_note = None
    provider = mermaid_provider()
    if provider and EXECUTION_MODE in ("live", "hybrid"):
        try:
            prompt = (
                f"Generate a clean, valid Mermaid.js diagram.\n"
                f"INTENT: {intent}\n{extra_line}"
                f"NODES (max 7): {json.dumps(nodes[:7])}\n"
                f"RUNNING EXAMPLE: {running_example}\n"
                f"MUST SERVE (show nothing outside this scope): {visual_request.get('serves') or visual_request.get('title') or intent}\n"
                f"THE CARD THIS DIAGRAM SITS IN (match its terms and names exactly): {(card_text or '')[:900]}\n"
                f"Prefer flowchart TD, flowchart LR, sequenceDiagram, or stateDiagram.\n"
                f"Return ONLY the raw Mermaid code, no markdown fences.\n"
                f"Max 7 nodes. Flat, clear alphanumeric labels. No HTML in labels."
            )

            raw = call_llm_chat(messages=[{"role": "user", "content": prompt}], max_tokens=1500, temperature=0.2, provider=provider, agent="mermaid", script_id=script_id)

            # Strip markdown fences if present
            if "```mermaid" in raw:
                raw = raw.split("```mermaid")[1].split("```")[0].strip()
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0].strip()

            # Rule R5: 7 nodes or fewer. Ask once more, with a lower number, rather than ship a diagram that breaks it.
            from agents.rule_checker import _diagram_nodes
            from agents.shared_context import LIMITS
            if _diagram_nodes(raw) > LIMITS["max_diagram_nodes"]:
                again = call_llm_chat(
                    messages=[{"role": "user", "content": prompt + f"\nYour last diagram had {_diagram_nodes(raw)} nodes. "
                               f"Redraw it with at most {LIMITS['max_diagram_nodes'] - 1} nodes, merging steps."}],
                    max_tokens=1500, temperature=0.2, provider=provider, agent="mermaid", script_id=script_id)
                if "```" in again:
                    again = again.split("```mermaid")[-1].split("```")[0].strip() if "```mermaid" in again else again.split("```")[1].strip()
                if _diagram_nodes(again) <= _diagram_nodes(raw):
                    raw = again

            from agents.rule_checker import quote_mermaid_labels
            raw = quote_mermaid_labels(raw)                  # a | or ( ) in a box text is a syntax error unless the text is in quotes
            return {
                "id": vis_id,
                "type": "mermaid",
                "content": raw,
                "alt_text": intent,
                "caption": f"Figure: {intent}",
                "licence": None,
                "source": "gemini" if provider == "gemini" else "claude",
                "generated": True
            }

        except Exception as e:
            mermaid_note = str(e)[:200]
            print(f"Mermaid generation warning: {e}")
    elif not provider:
        if not mock_fallback_allowed():
            raise LLMUnavailable("The AI is not configured, so this diagram cannot be drawn. Add the AI key in backend/.env and restart.")
        mermaid_note = "Placeholder flow. Set ANTHROPIC_API_KEY (or GEMINI_API_KEY) to generate real diagrams."

    # Fallback static diagram: only from the planner's own step names. Generic boxes (Start, Process, End) say nothing about this class, so they are never shown.
    if not nodes and not mock_fallback_allowed():
        raise LLMUnavailable(f"The diagram could not be drawn ({mermaid_note or 'no answer from the AI'}). Use Regenerate to try again.")
    node_labels = nodes[:7] if nodes else ["Start", "Process", "End"]
    lines = ["graph LR"]
    for i in range(len(node_labels) - 1):
        safe_a = node_labels[i].replace(" ", "_")
        safe_b = node_labels[i + 1].replace(" ", "_")
        lines.append(f"  {safe_a}[{node_labels[i]}] --> {safe_b}[{node_labels[i+1]}]")

    return {
        "id": vis_id,
        "type": "mermaid",
        "content": "\n".join(lines),
        "alt_text": intent,
        "caption": f"Figure: {intent}",
        "licence": None,
        "source": "placeholder",
        "generated": False,
        "note": mermaid_note or "Placeholder flow."
    }


# ── Animation Generation ────────────────────────────────────────────────────

def _generate_animation(
    visual_request: Dict[str, Any],
    card_text: str,
    running_example: str,
    block_a_context: Optional[str] = None,
    script_id: Optional[int] = None
) -> Dict[str, Any]:
    """Generates a self-contained HTML animation with Play/Replay, light/dark mode."""
    vis_id = visual_request.get("id", "anim_01")
    intent = visual_request.get("intent", "step-by-step animation")
    extra_line = f"ALSO: {visual_request['extra']}\n" if visual_request.get("extra") else ""
    nodes = visual_request.get("nodes", [])

    animation_note = None
    if llm_configured("anthropic") and EXECUTION_MODE in ("live", "hybrid"):
        try:
            from agents.safe_html import animation_is_complete
            prompt = (
                f"Generate a self-contained HTML file with inline CSS and JS.\n"
                f"INTENT: {intent}\n{extra_line}"
                f"STEPS/NODES: {json.dumps(nodes[:7])}\n"
                f"RUNNING EXAMPLE: {running_example}\n"
                f"MUST SERVE (show nothing outside this scope): {visual_request.get('serves') or visual_request.get('title') or intent}\n"
                f"THE CARD THIS ANIMATION SITS IN (match its terms and names exactly): {(card_text or '')[:900]}\n\n"
                "REQUIREMENTS: the animation is a WALKTHROUGH. A learner must always see which step they are on, what it is, and how it fits the whole flow.\n"
                "- THE FLOW: draw every step across the top (or down the side) as a numbered node, joined by arrows, in order. 4 to 6 steps, each with a short name taken from the "
                "card's own words. The current step is highlighted, the steps already done are ticked or dimmed, and the steps still to come are faded.\n"
                "- THE CURRENT STEP: a clearly visible panel that reads \"Step N of M: <step name>\" and, under it, one plain sentence on what happens in this step and why. "
                "Show the card's real example moving through the flow (the actual names and values), not abstract boxes.\n"
                "- CONTROLS: Back, Play/Pause, Next and Replay buttons. It plays by itself, about 3 seconds per step, and stops on the last step.\n"
                "- No external assets or CDN links. Light and dark mode (use prefers-color-scheme). Clean, modern, readable at a glance.\n"
                "- BE COMPACT. The whole file must stay under about 3000 tokens: short shared CSS classes, no comments, no repeated rules, vanilla JS only. "
                "A file that is cut off shows nothing, so finish it: the last thing in your answer is </html>.\n\n"
                "Return ONLY the HTML code, no markdown fences."
            )

            def ask(text):
                raw = call_llm_chat(messages=[{"role": "user", "content": text}], max_tokens=7000, temperature=0.2, provider="anthropic", agent="animation", script_id=script_id)
                if "```html" in raw:
                    raw = raw.split("```html")[1].split("```")[0].strip()
                elif "```" in raw:
                    raw = raw.split("```")[1].split("```")[0].strip()
                return raw

            from agents.safe_html import animation_shows_steps
            html_content = ask(prompt)
            if not animation_is_complete(html_content):
                # the answer was cut off or came back empty: ask once more, for something much smaller
                html_content = ask(prompt + "\n\nYOUR LAST ANSWER WAS CUT OFF AND SHOWED NOTHING. Write a much smaller version: 4 steps, minimal CSS, under 2000 tokens, and end with </html>.")
            elif not animation_shows_steps(html_content):
                # it plays but it does not say which step it is on: one more try, told exactly that
                html_content = ask(prompt + "\n\nYOUR LAST ANSWER DID NOT SHOW WHICH STEP IT WAS ON. It must show every step as a numbered node in a flow, a panel reading "
                                   "\"Step N of M: <name>\", and Back, Play/Pause, Next and Replay buttons.")
            if animation_is_complete(html_content) and animation_shows_steps(html_content):
                return {
                    "id": vis_id,
                    "type": "animation",
                    "content": html_content,
                    "alt_text": intent,
                    "caption": f"Interactive: {intent}",
                    "licence": None,
                    "source": "claude",
                    "generated": True
                }
            animation_note = "The AI's animation was cut off or did not show its steps, so this is a simple step-by-step walkthrough. Regenerate to try again."

        except Exception as e:
            animation_note = str(e)[:200]
            print(f"Animation generation warning: {e}")
    elif not llm_configured("anthropic"):
        if not mock_fallback_allowed():
            raise LLMUnavailable("The AI is not configured, so this animation cannot be made. Add the AI key in backend/.env and restart.")
        animation_note = "Placeholder animation. Set ANTHROPIC_API_KEY to generate real animations."
    if not nodes and not mock_fallback_allowed():
        raise LLMUnavailable(f"The animation could not be made ({animation_note or 'no answer from the AI'}). Use Regenerate to try again.")

    # Fallback: a small step player that always works
    from agents.safe_html import step_player_html
    fallback_html = step_player_html(str(intent)[:120], nodes)

    return {
        "id": vis_id,
        "type": "animation",
        "content": fallback_html,
        "alt_text": intent,
        "caption": f"Interactive: {intent}",
        "licence": None,
        "source": "placeholder",
        "generated": False,
        "note": animation_note or "Placeholder animation."
    }


# ── Image Spec Generation ───────────────────────────────────────────────────

def _search_web_educational_image(visual_request: Dict[str, Any], card_text: str = "", script_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """A free Wikimedia Commons picture that fits the card (see agents/web_images.py), or None."""
    if visual_request.get("skip_web"):
        return None
    try:
        from agents.web_images import find_image
        return find_image(visual_request, card_text, script_id=script_id)
    except Exception as e:
        print(f"Web image search skipped: {e}")
        return None


def _generate_image_spec(
    visual_request: Dict[str, Any],
    card_text: str,
    running_example: str,
    script_id: Optional[int] = None
) -> Dict[str, Any]:
    """Returns image spec: a free web picture first, then Gemini, then a vector placeholder."""
    vis_id = visual_request.get("id", "img_01")
    intent = visual_request.get("intent", "diagram")
    nodes = visual_request.get("nodes", [])

    # Tier 1: a free Wikimedia Commons picture, chosen to fit the card, saved locally with its licence and credit
    web_asset = _search_web_educational_image({**visual_request, "id": vis_id}, card_text, script_id=script_id)
    if web_asset:
        return web_asset

    # Tier 2: With a Gemini key, generate image via Gemini Image API
    placeholder_note = None
    from agents.image_gen import ImageUnavailable, image_configured, make_image_asset
    if image_configured() and EXECUTION_MODE in ("live", "hybrid"):
        try:
            return make_image_asset({**visual_request, "id": vis_id}, running_example)
        except ImageUnavailable as e:
            placeholder_note = str(e)
            print(f"Image generation warning: {e}")

    spec = {
        "subject": intent,
        "labels": nodes[:7],
        "layout": "horizontal flow" if len(nodes) <= 4 else "grid",
        "style": "2D Excalidraw-style blueprint, dark background, hand-drawn wireframe"
    }

    # Render clean, attractive SVG infographic for immediate browser presentation
    box_elements = []
    if not nodes and not mock_fallback_allowed():
        raise LLMUnavailable(f"The picture could not be made ({placeholder_note or 'no picture was found or generated'}). Use Regenerate to try again.")
    labels = nodes[:6] if nodes else ["Input", "Processing", "Validation", "Output"]
    total_w = 720
    box_w = min(140, int((total_w - 40) / max(1, len(labels))))
    gap = int((total_w - (box_w * len(labels))) / max(1, len(labels) + 1))
    
    for i, lbl in enumerate(labels):
        x = gap + i * (box_w + gap)
        box_elements.append(f'''
            <g transform="translate({x}, 70)">
                <rect width="{box_w}" height="65" rx="8" fill="#1e293b" stroke="#38bdf8" stroke-width="2"/>
                <text x="{box_w // 2}" y="38" fill="#f8fafc" font-size="13" font-weight="600" text-anchor="middle" font-family="system-ui, sans-serif">{html.escape(lbl[:18])}</text>
            </g>
        ''')
        if i < len(labels) - 1:
            arrow_x = x + box_w
            box_elements.append(f'''
                <path d="M {arrow_x + 4} 102 L {arrow_x + gap - 4} 102" stroke="#94a3b8" stroke-width="2" marker-end="url(#arrow)"/>
            ''')

    svg_content = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 740 180" style="width:100%; max-width:740px; background:#0f172a; border-radius:10px; border:1px solid #334155;">
        <defs>
            <marker id="arrow" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                <path d="M 0 0 L 10 5 L 0 10 z" fill="#94a3b8"/>
            </marker>
        </defs>
        <text x="24" y="34" fill="#38bdf8" font-size="15" font-weight="700" font-family="system-ui, sans-serif">{html.escape(str(intent))}</text>
        {''.join(box_elements)}
    </svg>'''

    return {
        "id": vis_id,
        "type": "image",
        "content": svg_content,
        "alt_text": intent,
        "caption": f"Visual Architecture: {intent}",
        "licence": "Generated / Free",
        "source": "placeholder",
        "generated": False,
        "note": placeholder_note or "Placeholder graphic. Set GEMINI_API_KEY to generate real pictures."
    }


# ── Legacy API compatibility ─────────────────────────────────────────────────

def generate_mermaid_diagram(concept_title: str, domain_analogy: str, is_recap: bool = False) -> Dict[str, Any]:
    """Legacy wrapper for backward compatibility."""
    req = {
        "id": f"mermaid_{concept_title[:10].lower().replace(' ', '_')}",
        "type": "mermaid",
        "intent": concept_title,
        "nodes": ["Client", "Gateway", "Stream", "Worker", "Store"] if is_recap else [],
        "card_id": "legacy"
    }
    result = generate_visual(req, "", domain_analogy)
    return {
        "type": result["type"],
        "content": result["content"],
        "caption": result["caption"]
    }


def generate_html_animation(concept_title: str) -> Dict[str, Any]:
    """Legacy wrapper for backward compatibility."""
    req = {
        "id": f"anim_{concept_title[:10].lower().replace(' ', '_')}",
        "type": "animation",
        "intent": concept_title,
        "nodes": [],
        "card_id": "legacy"
    }
    result = generate_visual(req, "", "")
    return {
        "type": result["type"],
        "content": result["content"],
        "caption": result["caption"]
    }


def generate_image_asset(concept_title: str, prompt_description: str) -> Dict[str, Any]:
    """Legacy wrapper for backward compatibility."""
    req = {
        "id": f"img_{concept_title[:10].lower().replace(' ', '_')}",
        "type": "image",
        "intent": concept_title,
        "nodes": [],
        "card_id": "legacy"
    }
    result = generate_visual(req, "", prompt_description)
    return {
        "type": result["type"],
        "content": result["content"],
        "caption": result["caption"]
    }


_GENERATED_CAPTION = ("Figure: ", "Interactive: ", "Visual Architecture: ", "Visual: ")


def visual_label(visual_request: Dict[str, Any]) -> str:
    """A short label for a visual: the visual's own title, else the start of what it was asked to show. Never the whole instruction."""
    title = str(visual_request.get("title") or "").strip()
    if title:
        return title[:90]
    first = re.split(r"[:.;]", str(visual_request.get("intent") or "Visual").strip(), maxsplit=1)[0].strip()
    return (first or "Visual")[:90]


def generate_visual(visual_request: Dict[str, Any], *args, **kwargs) -> Dict[str, Any]:
    """The visual from its generator, with a short caption and alt text. The generators echo the request's instruction back as the caption, which is
    not something to show a learner; a caption that came from a web picture (with its credit) is kept."""
    asset = _generate_visual_raw(visual_request, *args, **kwargs)
    if isinstance(asset, dict) and str(asset.get("caption") or "").startswith(_GENERATED_CAPTION):
        asset["caption"] = visual_label(visual_request)
        asset["alt_text"] = visual_label(visual_request)
    return asset
