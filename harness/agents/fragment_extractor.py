"""Fragment Extractor Agent — extract structured memories from transcripts.

Runs AFTER skill_writer. Extracts 2-3 key fragments from the session
and writes them to the fragments table for future injector use.

Independent agent: own prompt, own LLM session, no observer/skill-writer context.
"""

import json
from pathlib import Path
from typing import Optional


def build_prompt(session_content: str, tags: list, quality_score: float, skill_type: str) -> str:
    """Build a focused prompt for fragment extraction."""
    return f"""You are a Fragment Extractor. Your ONLY job: extract 2-3 concise, reusable memory fragments from a work session.

Session content:
{_truncate(session_content, 2000)}

Session tags: {tags}
Skill quality: {quality_score:.2f}
Skill type: {skill_type}

Extract 2-3 fragments. Each fragment should be a DECLARATIVE FACT, not an instruction.
Good: "WebFetch is blocked on .gov.cn domains; use browser-use MCP as fallback."
Bad: "Always use browser-use instead of WebFetch."

Output format (STRICT JSON array):
```json
[
  {{"tag": "<tag>", "content": "<declarative fact, max 300 chars>"}},
  ...
]
```

Focus on:
- Environment quirks discovered (API limits, blocked domains, tool workarounds)
- User preferences stated (style choices, default values, workflow preferences)
- Reusable insights (patterns that apply across tasks)

Do NOT extract:
- Task-specific steps ("first run X, then Y")
- Session outcomes ("completed contract review for client Z")
- Temporary state ("currently working on Phase 2")
"""


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... [truncated]"


def _call_llm(prompt: str, base_url: str, token: str, model: str = "deepseek-v4-pro") -> Optional[str]:
    import requests
    try:
        resp = requests.post(
            f"{base_url}/v1/messages",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
            },
            json={"model": model, "max_tokens": 1000, "temperature": 0.2,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if "content" in data and isinstance(data["content"], list):
            blocks = [i.get("text", "") for i in data["content"]
                      if i.get("type") == "text" and i.get("text")]
            return "\n".join(blocks) if blocks else None
        if "choices" in data:
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[fragment_extractor] LLM call failed: {e}")
    return None


def _parse_fragments(result: str) -> list[dict]:
    """Parse JSON array from LLM output. Returns list of {tag, content} dicts."""
    import re
    # Try to extract JSON array from markdown code block or raw text
    match = re.search(r'```(?:json)?\s*(\[.+?\])\s*```', result, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    # Try raw parse
    try:
        return json.loads(result)
    except json.JSONDecodeError:
        pass
    return []


def run(session_content: str, tags: list, quality_score: float,
        skill_type: str, config: dict) -> list[dict]:
    """Main entry point for the fragment extractor agent.

    Returns list of fragment dicts with {tag, content}.
    """
    if not config.get("refiner", {}).get("enabled", False):
        return []

    # Skip if quality too low
    if quality_score < 0.3:
        print("[fragment_extractor] Quality too low, skipping.")
        return []

    # Get API credentials
    settings_path = Path.home() / ".claude" / "settings.json"
    base_url = "https://api.deepseek.com/anthropic"
    token = ""
    if settings_path.exists():
        try:
            s = json.load(open(settings_path, "r", encoding="utf-8"))
            env = s.get("env", {})
            base_url = env.get("ANTHROPIC_BASE_URL", base_url)
            token = env.get("ANTHROPIC_AUTH_TOKEN", "")
        except Exception:
            pass
    if not token:
        print("[fragment_extractor] No API token.")
        return []

    prompt = build_prompt(session_content, tags, quality_score, skill_type)
    print(f"[fragment_extractor] Calling LLM...")
    result = _call_llm(prompt, base_url, token,
                       config.get("refiner", {}).get("model", "deepseek-v4-pro"))

    if not result:
        return []

    fragments = _parse_fragments(result)
    print(f"[fragment_extractor] Extracted {len(fragments)} fragments")

    # Write to database
    if fragments:
        _store_fragments(fragments)

    return fragments


def _store_fragments(fragments: list[dict]):
    """Write fragments to SQLite fragments table."""
    try:
        import sqlite3
        harness_dir = Path(__file__).resolve().parent.parent
        conn = sqlite3.connect(str(harness_dir / "state.db"), timeout=2)
        for f in fragments:
            conn.execute(
                """INSERT INTO fragments(tag, trigger_phrases, content, confidence, created_at)
                   VALUES(?,?,?,?,unixepoch())""",
                (f.get("tag", "general"), json.dumps(f.get("tag", "")),
                 f.get("content", "")[:2000], 0.7),
            )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[fragment_extractor] DB write failed: {e}")
