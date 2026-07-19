"""AI trivia for the Album Spotlight (Home view).

Provider-agnostic: point it at Anthropic (Claude) or Google (Gemini) with a
key, and it returns a small structured blurb about an album. Everything is
opt-in and fail-safe — a missing package, a missing key, or an API hiccup
returns ``None`` and the Home view simply shows the album without trivia. This
module never raises and never blocks (callers run it off the UI thread).

Keys are supplied by the caller (from the in-app Settings screen or config);
nothing is read from the environment or written to disk here.
"""

from __future__ import annotations

import json

PROVIDERS = ("anthropic", "gemini")

# Default models per provider. Both are overridable via the `ai_model` setting.
# Anthropic defaults to the flagship; set ai_model to e.g. "claude-haiku-4-5"
# for a cheaper/faster blurb. Gemini defaults to a current fast model.
DEFAULT_MODELS = {
    "anthropic": "claude-opus-4-8",
    "gemini": "gemini-2.5-flash",
}

# What we ask the model for, and what we parse back out.
_FIELDS = ("trivia", "year", "label", "genre")


def provider_label(provider: str) -> str:
    return {"anthropic": "Claude (Anthropic)", "gemini": "Gemini (Google)"}.get(
        provider, provider
    )


def default_model(provider: str) -> str:
    return DEFAULT_MODELS.get(provider, "")


def is_configured(provider: str, anthropic_api_key: str, gemini_api_key: str) -> bool:
    """True when the selected provider has a key to use."""
    if provider == "anthropic":
        return bool(anthropic_api_key)
    if provider == "gemini":
        return bool(gemini_api_key)
    return False


def _build_prompt(album: str, artist: str, year: str = "", genre: str = "") -> str:
    known = []
    if year:
        known.append(f"year {year}")
    if genre:
        known.append(f"genre {genre}")
    hint = f" (known: {', '.join(known)})" if known else ""
    return (
        f'Give a short "album of the day" spotlight for the album '
        f'"{album}" by {artist}{hint}. '
        "Reply with ONLY a JSON object (no code fences, no prose) with keys: "
        '"trivia" (2-3 engaging sentences of interesting factual context about '
        "the album — recording, reception, significance; avoid inventing "
        'specifics you are unsure of), "year" (release year as a string, or ""), '
        '"label" (record label, or ""), "genre" (primary genre, or ""). '
        "If you are unsure of a field, use an empty string."
    )


def _parse(text: str) -> dict | None:
    """Pull a JSON object out of a model reply, tolerating stray code fences
    or leading prose."""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        # strip a ```json ... ``` fence
        s = s.split("\n", 1)[-1] if "\n" in s else s
        s = s.rsplit("```", 1)[0]
    # narrow to the first {...} block if the model added chatter
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        s = s[start : end + 1]
    try:
        data = json.loads(s)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return {k: str(data.get(k, "") or "") for k in _FIELDS}


def _run_anthropic(prompt: str, api_key: str, model: str) -> dict | None:
    try:
        import anthropic
    except Exception:
        return None
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model or DEFAULT_MODELS["anthropic"],
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )
        return _parse(text)
    except Exception:
        return None


def _run_gemini(prompt: str, api_key: str, model: str) -> dict | None:
    try:
        from google import genai
    except Exception:
        return None
    try:
        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model=model or DEFAULT_MODELS["gemini"],
            contents=prompt,
        )
        return _parse(getattr(resp, "text", "") or "")
    except Exception:
        return None


def generate_album_spotlight(
    *,
    provider: str,
    anthropic_api_key: str,
    gemini_api_key: str,
    model: str,
    album: str,
    artist: str,
    year: str = "",
    genre: str = "",
) -> dict | None:
    """Return ``{"trivia","year","label","genre"}`` for an album, or ``None``.

    Blocking network + LLM call — run it in a worker/executor, never on the UI
    thread. Any failure (no key, package missing, network, bad JSON) is a
    silent ``None``.
    """
    if not is_configured(provider, anthropic_api_key, gemini_api_key):
        return None
    prompt = _build_prompt(album, artist, year, genre)
    if provider == "anthropic":
        return _run_anthropic(prompt, anthropic_api_key, model)
    if provider == "gemini":
        return _run_gemini(prompt, gemini_api_key, model)
    return None
