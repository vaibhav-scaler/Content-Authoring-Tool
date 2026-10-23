"""
The one way every agent talks to a language model.

Two providers, each with a fixed job:

  anthropic   Claude Sonnet 5.5 (default model claude-sonnet-5-5). Writes everything that is text: the outline, every
              card of the script, the reviews, the repairs, the chat assistant, and the HTML animations.
  gemini      Gemini Flash. Makes the visuals that are not text: Mermaid flow diagrams here (provider="gemini"), and
              pictures in agents/image_gen.py.

A call names its provider. There is no silent switch from one to the other: if the provider has no key, or cannot
answer, the call raises LLMUnavailable with a reason fit to show a user.

Settings (backend/.env):
  ANTHROPIC_API_KEY        Claude key
  ANTHROPIC_MODEL          its model                           (default claude-sonnet-5-5)
  ANTHROPIC_TIMEOUT_SECONDS  longest one request may take      (default 180)
  GEMINI_API_KEY           Gemini key (also used for pictures)
  GEMINI_TEXT_MODEL        Gemini model for Mermaid flows      (default gemini-2.5-flash)
  LLM_MAX_WAIT_SECONDS     longest a call keeps retrying a rate limit or an overload (default 120)
  LLM_BUDGET_USD           spending limit for Claude/OpenRouter calls, all time (default 15; 0 means no limit)
  ALLOW_MOCK_FALLBACK      1 lets agents fall back to sample content when the model is down (default 0)
"""

import contextvars
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

from agents.image_gen import gemini_api_key

load_dotenv()

log = logging.getLogger("llm")

DEFAULT_CLAUDE_MODEL = "claude-sonnet-5-5"
DEFAULT_OPENROUTER_MODEL = "anthropic/claude-3.5-sonnet"
OPENROUTER_CHAT_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_GEMINI_TEXT_MODEL = "gemini-2.5-flash"
GEMINI_TEXT_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Price list, in US dollars per million tokens: model -> (input, output). A model that is not here has NO known price; its calls
# are recorded with cost 0 and cost_source "unknown", and the dashboard says so, instead of being charged at some other model's rate.
# The price a provider reports for a call (OpenRouter does) always wins over this list.
# To add or change a price without editing code, set LLM_PRICES_JSON='{"model-id": [input, output]}' in backend/.env.
MODEL_PRICES: Dict[str, tuple] = {
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "anthropic/claude-3.5-sonnet": (3.0, 15.0),
    "anthropic/claude-sonnet-4.6": (3.0, 15.0),
}


def price_list() -> Dict[str, tuple]:
    prices = dict(MODEL_PRICES)
    try:
        for model, pair in json.loads(os.getenv("LLM_PRICES_JSON") or "{}").items():
            prices[model] = (float(pair[0]), float(pair[1]))
    except (ValueError, TypeError, IndexError):
        log.warning("LLM_PRICES_JSON is not valid JSON of the form {\"model\": [input, output]}; it was ignored.")
    return prices


def compute_cost(model: str, prompt_tokens: int, completion_tokens: int, reported_cost: Optional[float] = None) -> tuple:
    """(cost in dollars, where the number came from). The provider's own figure first, then our price list, else unknown."""
    if reported_cost is not None:
        return float(reported_cost), "provider"
    price = price_list().get(model)
    if price is None:
        return 0.0, "unknown"
    return prompt_tokens / 1_000_000.0 * price[0] + completion_tokens / 1_000_000.0 * price[1], "table"


# Which script the current request is working on. Endpoints set it once; every model call made while serving that request,
# including the ones made by agents that know nothing about scripts, is then recorded against it.
_usage_script: "contextvars.ContextVar[Optional[int]]" = contextvars.ContextVar("usage_script_id", default=None)


def bind_usage_script(script_id: Optional[int]) -> None:
    _usage_script.set(script_id)


MAX_SLEEP_SECONDS = 20
DEFAULT_MAX_WAIT_SECONDS = 120
PROVIDERS = ("anthropic", "openrouter", "gemini")


class LLMUnavailable(Exception):
    """The model could not produce an answer. The message says why, in words fit to show a user."""


class LLMBudgetExceeded(LLMUnavailable):
    """The spending limit (LLM_BUDGET_USD) has been reached, so no further paid call is made."""


DEFAULT_BUDGET_USD = 15.0
BUDGET_WARN_FRACTION = 0.8


def budget_usd() -> float:
    """The spending limit in dollars. 0 or a negative number turns the limit off."""
    try:
        return float(os.getenv("LLM_BUDGET_USD", DEFAULT_BUDGET_USD))
    except ValueError:
        return DEFAULT_BUDGET_USD


def spent_usd() -> float:
    """Everything recorded so far, all time. If the log cannot be read the answer is 0, so a broken log never blocks work."""
    try:
        from sqlalchemy import func
        import database, models
        db = database.SessionLocal()
        try:
            return float(db.query(func.coalesce(func.sum(models.APIUsageLog.estimated_cost_usd), 0.0)).scalar() or 0.0)
        finally:
            db.close()
    except Exception as err:
        log.warning("Could not read spend for the budget check: %s", err)
        return 0.0


def budget_status() -> Dict[str, Any]:
    limit, spent = budget_usd(), spent_usd()
    return {
        "limit_usd": round(limit, 2) if limit > 0 else None,
        "spent_usd": round(spent, 4),
        "remaining_usd": round(max(0.0, limit - spent), 4) if limit > 0 else None,
        "used_fraction": round(spent / limit, 4) if limit > 0 else 0.0,
        "exceeded": limit > 0 and spent >= limit,
        "warning": limit > 0 and spent >= limit * BUDGET_WARN_FRACTION,
    }


# Money promised to calls that are running right now but not yet written to the log. Without it, several parallel calls could each
# see "enough budget left" and together go over.
_reserved_usd = 0.0
_reserve_lock = threading.Lock()
UNKNOWN_PRICE_FALLBACK = (15.0, 75.0)      # per million tokens; assume the dearest when a model has no listed price
CHARS_PER_TOKEN = 3.0                        # low on purpose, so the input estimate runs high


def worst_case_cost(model: str, messages: List[Dict[str, str]], system: Optional[str], max_tokens: int) -> float:
    """The most this call can cost: every input character counted generously, and the full max_tokens answer."""
    chars = len(system or "") + sum(len(str(m.get("content", ""))) for m in messages)
    in_tokens = chars / CHARS_PER_TOKEN + 50
    price = price_list().get(model, UNKNOWN_PRICE_FALLBACK)
    return in_tokens / 1_000_000.0 * price[0] + max_tokens / 1_000_000.0 * price[1]


def reserve_budget(estimate: float) -> float:
    """Checks that spent + calls in flight + this call's worst case stays inside the limit, and holds that amount. Returns it
    so the caller can release it. Raises LLMBudgetExceeded if it would not fit."""
    global _reserved_usd
    limit = budget_usd()
    if limit <= 0:
        return 0.0
    spent = spent_usd()
    with _reserve_lock:
        if spent + _reserved_usd + estimate > limit:
            left = max(0.0, limit - spent - _reserved_usd)
            raise LLMBudgetExceeded(
                f"The ${limit:.2f} AI budget would be exceeded: ${spent:.2f} spent, ${left:.2f} left, and this call could cost up to ${estimate:.2f}. "
                "Raise LLM_BUDGET_USD in backend/.env and restart to continue.")
        _reserved_usd += estimate
    return estimate


def release_budget(amount: float) -> None:
    global _reserved_usd
    if amount:
        with _reserve_lock:
            _reserved_usd = _reserved_usd - amount
            if _reserved_usd < 1e-9:      # floating-point crumbs
                _reserved_usd = 0.0


def check_budget() -> None:
    """Cheap version for callers with no estimate: refuse once the limit is already reached."""
    st = budget_status()
    if st["exceeded"]:
        raise LLMBudgetExceeded(
            f"The ${st['limit_usd']:.2f} AI budget is used up (${st['spent_usd']:.2f} spent). "
            "Raise LLM_BUDGET_USD in backend/.env and restart to continue.")


# ---- keys, models, status ------------------------------------------------------------------------------------------

def get_openrouter_api_key() -> Optional[str]:
    key = os.getenv("OPENROUTER_API_KEY")
    return key.strip() if key and key.strip() else None


def openrouter_model() -> str:
    return os.getenv("OPENROUTER_MODEL") or DEFAULT_OPENROUTER_MODEL


def get_anthropic_api_key() -> Optional[str]:
    key = os.getenv("ANTHROPIC_API_KEY")
    return key.strip() if key and key.strip() else None


def claude_model() -> str:
    return os.getenv("ANTHROPIC_MODEL") or DEFAULT_CLAUDE_MODEL


def gemini_text_model() -> str:
    return os.getenv("GEMINI_TEXT_MODEL") or DEFAULT_GEMINI_TEXT_MODEL


def llm_configured(provider: str = "anthropic") -> bool:
    """True when the provider has a key. With no argument: check if Sonnet/text is configured (OpenRouter or Anthropic)."""
    if provider in ("anthropic", "openrouter"):
        return bool(get_openrouter_api_key() or get_anthropic_api_key())
    return bool(gemini_api_key())


def mock_fallback_allowed() -> bool:
    """Agents may return sample content only in mock mode, or when ALLOW_MOCK_FALLBACK is switched on."""
    return os.getenv("EXECUTION_MODE", "live") == "mock" or os.getenv("ALLOW_MOCK_FALLBACK", "0") == "1"


def provider_status() -> Dict[str, Any]:
    """What is configured, without any secret. For the admin status page."""
    from agents.image_gen import image_configured, image_model
    primary_text_provider = "openrouter" if get_openrouter_api_key() else "anthropic"
    active_model = openrouter_model() if primary_text_provider == "openrouter" else claude_model()
    return {
        "script_and_animation": {
            "provider": primary_text_provider,
            "model": active_model,
            "configured": llm_configured("anthropic"),
            "openrouter_enabled": bool(get_openrouter_api_key()),
        },
        "mermaid_flows": {"provider": "gemini", "model": gemini_text_model(), "configured": llm_configured("gemini")},
        "images": {"provider": "gemini", "model": image_model(), "configured": image_configured()},
    }


def estimate_tokens(text: str) -> int:
    """A rough count (3.5 characters a token) that errs high. Used to size groups of cards for review."""
    return int(len(text) / 3.5) + 1


def record_api_usage(
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_seconds: float,
    agent: str = "general",
    status: str = "success",
    script_id: Optional[int] = None,
    reported_cost: Optional[float] = None,
    error_message: Optional[str] = None,
) -> float:
    """Saves one model call: tokens, cost, time, which agent and which script. Returns the cost.
    The script is the one bound to this request (bind_usage_script) unless one is given."""
    prompt_tokens, completion_tokens = int(prompt_tokens or 0), int(completion_tokens or 0)
    cost, source = compute_cost(model, prompt_tokens, completion_tokens, reported_cost) if status == "success" else (0.0, "unknown")
    if script_id is None:
        script_id = _usage_script.get()
    try:
        from database import SessionLocal
        from models import APIUsageLog
        db = SessionLocal()
        try:
            db.add(APIUsageLog(
                script_id=script_id, provider=provider, model=model, agent=agent or "general",
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, total_tokens=prompt_tokens + completion_tokens,
                estimated_cost_usd=round(cost, 6), latency_seconds=round(latency_seconds, 3), status=status,
                cost_source=source, error_message=(error_message or None) and error_message[:300]))
            db.commit()
        finally:
            db.close()
    except Exception as err:       # a usage record must never break the request that made the call
        log.warning("Could not persist API usage record: %s", err)
    return cost


# ---- the request ---------------------------------------------------------------------------------------------------

_last_reply: Dict[str, Any] = {}      # the most recent reply, kept so a parse failure can say what came back


def _status_of(e: Exception) -> Optional[int]:
    code = getattr(e, "status_code", None)
    return code if isinstance(code, int) else None


class _ProviderHTTPError(Exception):
    """A provider answered with an error status. The status and any Retry-After travel with it so the retry logic can act on them."""
    def __init__(self, status_code: int, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


def _retry_after(e: Exception) -> Optional[float]:
    """Seconds the provider asked us to wait: the Retry-After header when the error carries a response."""
    if getattr(e, "retry_after", None):
        return float(e.retry_after)
    headers = getattr(getattr(e, "response", None), "headers", None)
    if headers:
        try:
            value = headers.get("retry-after") or headers.get("Retry-After")
            if value:
                return float(value)
        except (TypeError, ValueError):
            pass
    match = re.search(r"try again in (\d+(?:\.\d+)?)\s*s", str(e), re.I)
    return float(match.group(1)) if match else None


def openrouter_reported_cost(usage: Dict[str, Any]) -> Optional[float]:
    """What a call really cost, from OpenRouter's usage block. With a key of your own behind OpenRouter (BYOK) it reports cost 0 and puts
    the real charge, made by the provider, in cost_details.upstream_inference_cost; both are added then. If the total comes out 0 for a call
    that used tokens, None is returned so the price list is used instead: a budget must never read a paid call as free."""
    if not isinstance(usage, dict):
        return None
    cost = usage.get("cost")
    upstream = (usage.get("cost_details") or {}).get("upstream_inference_cost")
    if cost is None and upstream is None:
        return None
    total = float(cost or 0)
    if usage.get("is_byok") and upstream is not None:
        total += float(upstream)
    if total <= 0 and (usage.get("prompt_tokens") or usage.get("completion_tokens")):
        return None
    return total


def _openrouter(messages: List[Dict[str, str]], system: Optional[str], max_tokens: int, temperature: float, agent: str = "general", script_id: Optional[int] = None) -> str:
    """One request to OpenRouter. Every outcome is recorded: a success with its tokens and cost, a failure with its reason.
    Errors that are worth retrying (a rate limit, an overload, a timeout) are raised so that call_llm_chat retries them."""
    api_key = get_openrouter_api_key()
    if not api_key:
        raise LLMUnavailable("OPENROUTER_API_KEY is not configured in backend/.env.")

    model = openrouter_model()
    msgs: List[Dict[str, str]] = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.extend(messages)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://scaler.com",
        "X-Title": "Scaler Script Authoring Engine",
        "Content-Type": "application/json"
    }

    # What the agent asked for is what is sent. A ceiling is applied only if one is set in backend/.env, and then it is logged.
    ceiling = os.getenv("OPENROUTER_MAX_TOKENS_CEILING")
    sent_max_tokens = max_tokens
    if ceiling and ceiling.isdigit() and max_tokens > int(ceiling):
        log.warning("OpenRouter [%s]: max_tokens %d lowered to the OPENROUTER_MAX_TOKENS_CEILING of %s", agent, max_tokens, ceiling)
        sent_max_tokens = int(ceiling)

    payload = {
        "model": model,
        "messages": msgs,
        "max_tokens": sent_max_tokens,
        "temperature": temperature,
        "usage": {"include": True},          # ask OpenRouter to report what the call really cost
    }

    t0 = time.time()
    try:
        resp = requests.post(OPENROUTER_CHAT_ENDPOINT, headers=headers, json=payload,
                             timeout=float(os.getenv("OPENROUTER_TIMEOUT_SECONDS", "180")))
    except requests.RequestException as e:      # a timeout or a dropped connection: record it, then let the caller retry
        record_api_usage("openrouter", model, 0, 0, time.time() - t0, agent=agent, status="error", script_id=script_id,
                         error_message=f"{type(e).__name__}: {e}")
        raise
    latency = time.time() - t0

    if resp.status_code != 200:
        try:
            err_msg = resp.json().get("error", {}).get("message", resp.text[:200])
        except Exception:
            err_msg = resp.text[:200]
        retry_after = None
        try:
            retry_after = float((getattr(resp, "headers", None) or {}).get("Retry-After"))
        except (TypeError, ValueError):
            pass
        record_api_usage("openrouter", model, 0, 0, latency, agent=agent, status="error", script_id=script_id,
                         error_message=f"HTTP {resp.status_code}: {err_msg}")
        raise _ProviderHTTPError(resp.status_code, f"OpenRouter API error ({resp.status_code}): {err_msg}", retry_after)

    data = resp.json()
    choices = data.get("choices", [])
    if not choices:
        record_api_usage("openrouter", model, 0, 0, latency, agent=agent, status="error", script_id=script_id,
                         error_message="the answer had no choices")
        raise LLMUnavailable("OpenRouter returned an empty choices list.")

    reply_text = (choices[0].get("message", {}).get("content") or "").strip()
    usage = data.get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)

    cost = record_api_usage("openrouter", model, prompt_tokens, completion_tokens, latency, agent=agent, status="success",
                            script_id=script_id, reported_cost=openrouter_reported_cost(usage))
    log.info("OpenRouter [%s] %s: %s prompt + %s completion tokens | %.2fs | $%.5f", agent, model, prompt_tokens, completion_tokens, latency, cost)
    _last_reply.update(provider="openrouter", model=model, finish=choices[0].get("finish_reason"), text=reply_text[:600])
    return reply_text


def _claude(messages: List[Dict[str, str]], system: Optional[str], max_tokens: int, temperature: float, agent: str = "general", script_id: Optional[int] = None) -> str:
    import anthropic
    t0 = time.time()
    client = anthropic.Anthropic(api_key=get_anthropic_api_key(), max_retries=0,
                                 timeout=float(os.getenv("ANTHROPIC_TIMEOUT_SECONDS", "180")))
    kwargs: Dict[str, Any] = {"model": claude_model(), "max_tokens": max_tokens, "temperature": temperature,
                              "messages": [m for m in messages if m["role"] != "system"]}
    system_text = "\n\n".join(([system] if system else []) + [m["content"] for m in messages if m["role"] == "system"])
    if system_text:
        kwargs["system"] = system_text
    try:
        response = client.messages.create(**kwargs)
    except Exception as e:
        low = str(e).lower()
        # a model that does not take a temperature setting: ask again without it
        if "temperature" in low and any(k in low for k in ("deprecated", "not supported", "invalid")):
            kwargs.pop("temperature", None)
            response = client.messages.create(**kwargs)
        else:
            record_api_usage("anthropic", claude_model(), 0, 0, time.time() - t0, agent=agent, status="error", script_id=script_id)
            raise
    latency = time.time() - t0
    text = "".join(getattr(b, "text", "") for b in (response.content or []) if getattr(b, "type", "text") == "text")
    _last_reply.update(provider="anthropic", model=claude_model(), finish=getattr(response, "stop_reason", None), text=text[:600])
    usage = getattr(response, "usage", None)
    prompt_tokens = getattr(usage, "input_tokens", 0) or 0
    completion_tokens = getattr(usage, "output_tokens", 0) or 0
    cost = record_api_usage("anthropic", claude_model(), prompt_tokens, completion_tokens, latency, agent=agent, status="success", script_id=script_id)
    log.info("claude %s: %s prompt + %s answer tokens | Cost: $%.5f", claude_model(), prompt_tokens, completion_tokens, cost)
    return text.strip()


class _GeminiHTTPError(Exception):
    def __init__(self, status_code: int, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


def _gemini(messages: List[Dict[str, str]], system: Optional[str], max_tokens: int, temperature: float, json_mode: bool) -> str:
    system_text = "\n\n".join(([system] if system else []) + [m["content"] for m in messages if m["role"] == "system"])
    body: Dict[str, Any] = {
        "contents": [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]}
                     for m in messages if m["role"] != "system"],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
    }
    if system_text:
        body["systemInstruction"] = {"parts": [{"text": system_text}]}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    resp = requests.post(GEMINI_TEXT_ENDPOINT.format(model=gemini_text_model()), json=body, timeout=120,
                         headers={"x-goog-api-key": gemini_api_key() or "", "Content-Type": "application/json"})
    if resp.status_code != 200:
        try:
            detail = (resp.json().get("error") or {}).get("message", "")
        except Exception:
            detail = resp.text[:200]
        wait = resp.headers.get("Retry-After") if hasattr(resp, "headers") else None
        raise _GeminiHTTPError(resp.status_code, detail or f"HTTP {resp.status_code}", float(wait) if wait and str(wait).replace(".", "").isdigit() else None)
    data = resp.json()
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        raise LLMUnavailable("Gemini answered without text (the request may have been blocked by its safety filter).")
    text = "".join(p.get("text", "") for p in parts)
    _last_reply.update(provider="gemini", model=gemini_text_model(), finish=(data["candidates"][0].get("finishReason")), text=text[:600])
    return text.strip()


def _names(provider: str) -> str:
    if provider == "openrouter":
        return "OpenRouter (Sonnet)"
    return "Claude" if provider == "anthropic" else "Gemini"


def key_limit_message(provider: str, error_text: str) -> str:
    """The provider says the API KEY has reached a spending limit set on the provider's side (OpenRouter: "API key budget limit exceeded (daily limit) ... $7.00 of $7.00 used;
    resets 2026-10-23T00:00:00.000Z"). That is not a rejected key and not our own LLM_BUDGET_USD: say what it is, when it resets, and what to do."""
    import datetime as _dt
    import re as _re
    used = _re.search(r"\$([\d.]+) of \$([\d.]+) used", error_text or "")
    kind = (_re.search(r"\((\w+) limit\)", error_text or "") or [None, "spending"])[1]
    when = ""
    m = _re.search(r"resets (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", error_text or "")
    if m:
        try:
            utc = _dt.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
            ist = utc + _dt.timedelta(hours=5, minutes=30)
            when = f" It resets at {ist.strftime('%d %b, %H:%M')} IST ({utc.strftime('%d %b, %H:%M')} UTC)."
        except ValueError:
            pass
    spent = f" (${used.group(1)} of ${used.group(2)} used)" if used else ""
    return (f"{_names(provider)} says this API key has reached its {kind} spending limit{spent}.{when} "
            "Raise that limit in the key's settings on the provider's website, use another key, or wait until it resets. Nothing is wrong with the app.")


def call_llm_chat(
    messages: List[Dict[str, str]],
    system: Optional[str] = None,
    max_tokens: int = 4000,
    temperature: float = 0.2,
    json_mode: bool = False,
    provider: str = "anthropic",
    agent: str = "general",
    script_id: Optional[int] = None,
) -> str:
    """One chat completion from the named provider (or OpenRouter if configured). Returns reply text."""
    # If the call asks for anthropic, but OPENROUTER_API_KEY is configured, route via OpenRouter Sonnet
    effective_provider = provider
    if provider == "anthropic" and get_openrouter_api_key():
        effective_provider = "openrouter"
    elif provider == "gemini" and not gemini_api_key() and get_openrouter_api_key():
        # Fallback for diagrams/visuals: Use Sonnet since it is the only enabled key
        effective_provider = "openrouter"

    if effective_provider not in PROVIDERS:
        raise ValueError(f"unknown provider {effective_provider!r}")
    if not llm_configured(effective_provider):
        key_name = "OPENROUTER_API_KEY" if effective_provider == "openrouter" else ("ANTHROPIC_API_KEY" if effective_provider == "anthropic" else "GEMINI_API_KEY")
        raise LLMUnavailable(f"{_names(effective_provider)} is not configured. Set {key_name} in backend/.env and restart.")
    held = 0.0
    if effective_provider in ("anthropic", "openrouter"):    # the paid calls this log tracks
        model_name = claude_model() if effective_provider == "anthropic" else openrouter_model()
        held = reserve_budget(worst_case_cost(model_name, messages, system, max_tokens))
    try:
        return _call_with_retries(effective_provider, messages, system, max_tokens, temperature, json_mode, agent, script_id)
    finally:
        release_budget(held)


def _call_with_retries(effective_provider, messages, system, max_tokens, temperature, json_mode, agent, script_id) -> str:
    try:
        patience = float(os.getenv("LLM_MAX_WAIT_SECONDS", DEFAULT_MAX_WAIT_SECONDS))
    except ValueError:
        patience = DEFAULT_MAX_WAIT_SECONDS
    started = time.time()
    attempt = 0
    while True:
        attempt += 1
        try:
            if effective_provider == "openrouter":
                text = _openrouter(messages, system, max_tokens, temperature, agent=agent, script_id=script_id)
            elif effective_provider == "anthropic":
                text = _claude(messages, system, max_tokens, temperature, agent=agent, script_id=script_id)
            else:
                text = _gemini(messages, system, max_tokens, temperature, json_mode)

            if text:
                return text
            error_text, status = "an empty answer", None
        except LLMUnavailable:
            raise
        except Exception as e:
            error_text, status = str(e), _status_of(e)
            low = error_text.lower()
            if "limit exceeded" in low and ("budget" in low or "daily" in low or "weekly" in low or "monthly" in low or "credit" in low):
                raise LLMUnavailable(key_limit_message(effective_provider, error_text))
            if status in (401, 403) or "authentication_error" in low or "invalid x-api-key" in low or "api key not valid" in low:
                raise LLMUnavailable(f"The {_names(effective_provider)} API key was rejected. Check backend/.env.")
            if "credit balance" in low or "insufficient credit" in low or status == 402 or ("billing" in low and status in (400, 402, 403)):
                raise LLMUnavailable(f"The {_names(effective_provider)} account has no credit left. Add credit, then try again.")
            retryable = status in (408, 409, 429, 500, 502, 503, 504, 529) or any(
                k in low for k in ("overloaded", "rate_limit", "rate limit", "timed out", "timeout", "connection"))
            if not retryable:
                raise LLMUnavailable(f"{_names(effective_provider)} could not answer: {error_text[:240]}")
            log.warning("%s call failed (attempt %d): %s", effective_provider, attempt, error_text[:200])
            wait = (_retry_after(e) if not isinstance(e, _GeminiHTTPError) else e.retry_after) or min(MAX_SLEEP_SECONDS, 2 ** attempt)
            if time.time() - started + wait > patience:
                raise LLMUnavailable(f"{_names(effective_provider)} is busy or at its rate limit right now ({error_text[:160]}). Try again shortly.")
            time.sleep(min(MAX_SLEEP_SECONDS, wait))
            continue
        # an empty answer: one more try, then give up
        if attempt >= 2:
            raise LLMUnavailable(f"{_names(effective_provider)} returned {error_text}.")



def parse_json_reply(text: str):
    """The JSON inside a model reply. Handles a ```json fence around it and chatter before or after.
    A code fence INSIDE the JSON (a code block in a card body) is data, not a wrapper. Raises ValueError when
    there is no valid JSON."""
    raw = (text or "").strip()
    starts = [i for i in (raw.find("{"), raw.find("[")) if i >= 0]
    fence = raw.find("```")
    if fence >= 0 and (not starts or fence < min(starts)):
        # the reply is wrapped: take what is between the first fence line and the last fence
        body_start = raw.find("\n", fence)
        body_end = raw.rfind("```")
        raw = raw[body_start + 1:body_end if body_end > fence else len(raw)].strip()
        starts = [i for i in (raw.find("{"), raw.find("[")) if i >= 0]
    if not starts:
        raise ValueError("no JSON object in the reply")
    start = min(starts)
    end = max(raw.rfind("}"), raw.rfind("]"))
    if end < start:
        raise ValueError("the JSON in the reply is cut off")
    return json.loads(raw[start:end + 1])


def call_llm_json(
    messages: List[Dict[str, str]],
    system: Optional[str] = None,
    max_tokens: int = 4000,
    temperature: float = 0.2,
    attempts: int = 2,
    provider: str = "anthropic",
    agent: str = "general",
    script_id: Optional[int] = None,
):
    """Like call_llm_chat but returns parsed JSON, asking again once if the reply is not valid JSON."""
    last = None
    for _ in range(attempts):
        text = call_llm_chat(messages=messages, system=system, max_tokens=max_tokens, temperature=temperature,
                             json_mode=True, provider=provider, agent=agent, script_id=script_id)
        try:
            return parse_json_reply(text)
        except ValueError as e:
            last = e
            if str(_last_reply.get("finish") or "").lower() in ("max_tokens", "length"):
                max_tokens = min(int(max_tokens * 1.7), 8000)       # the reply was cut off, not malformed: ask again with more room
            log.warning("reply was not valid JSON (%s): model=%s finish=%s starts=%r", e,
                        _last_reply.get("model"), _last_reply.get("finish"), (_last_reply.get("text") or "")[:300])
    raise LLMUnavailable(f"The AI model's reply was not valid JSON ({last}). Try again.")

