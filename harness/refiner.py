"""Refiner — generate skill files from observations using LLM."""

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml


def load_config(config_path: Optional[Path] = None) -> dict:
    if config_path is None:
        config_path = Path(__file__).resolve().parent / "harness_config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get_api_credentials() -> tuple:
    """Read API credentials from Claude Code settings.json."""
    settings_path = Path.home() / ".claude" / "settings.json"
    if settings_path.exists():
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
        env = settings.get("env", {})
        base_url = env.get("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic")
        token = env.get("ANTHROPIC_AUTH_TOKEN", os.environ.get("ANTHROPIC_AUTH_TOKEN", ""))
        return base_url, token
    return None, None


def _build_skill_prompt(session_content: str, observation: dict, action: str) -> str:
    """Build the LLM prompt for skill generation based on observation action."""

    prompts = {
        "patch_skill": f"""You are updating an existing Claude Code skill based on a user correction.

The user corrected the assistant during this session. Here's the session context:

{_truncate(session_content, 3000)}

The correction was about: {observation.get('summary', '')}
Tags: {observation.get('tags', [])}

Generate an updated SKILL.md file in this format:

```markdown
---
name: <skill-name>
description: <one-line>
tags: [{', '.join(observation.get('tags', ['general']))}]
version: <bump-from-previous>
auto_generated: true
harness_confidence: {observation.get('confidence', 0.7)}
---

# <Skill Name>

## When to Use
...

## Updated Guidance (from session {observation.get('session_id', '')})
{observation.get('reason', '')}

## How To
...
```

Focus on the CORRECTION — what changed and why. Keep it concise (under 1500 tokens).
""",
        "create_skill": f"""You are creating a new Claude Code skill from a productive work session.

Session summary: {observation.get('summary', '')}
Tags: {observation.get('tags', [])}

Context from the session:
{_truncate(session_content, 3000)}

Generate a new SKILL.md file in this format:

```markdown
---
name: <kebab-case-skill-name>
description: <one-line-summary>
tags: [{', '.join(observation.get('tags', ['general']))}]
version: 1
auto_generated: true
harness_confidence: {observation.get('confidence', 0.7)}
---

# <Skill Name>

## When to Use
...

## How To
...

## Evolution Log
- {datetime.now().strftime('%Y-%m-%d')} v1: Auto-created from session {observation.get('session_id', '')}
```

Focus on the reusable workflow — what pattern did the assistant discover that can be reused?
""",
        "update_preference": f"""Extract the user's stated preference from this session context.

{_truncate(session_content, 2000)}

The preference is about: {observation.get('summary', '')}

Output ONLY the preference statement in declarative form (2-3 sentences max). Format:
"User prefers X. When doing Y, always Z."

Examples of good preference statements:
- "User prefers contract liability caps at 1x contract value, not 2x."
- "User wants all legal analysis in Chinese, with citations labeled as [需验证] when unverified."
- "User prefers concise responses with no trailing summaries."
"""
    }

    return prompts.get(action, prompts["create_skill"])


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... [truncated]"


def _call_llm(prompt: str, base_url: str, token: str, model: str = "deepseek-v4-pro[1m]") -> Optional[str]:
    """Call the LLM API (Anthropic-compatible endpoint)."""
    import requests

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }

    payload = {
        "model": model,
        "max_tokens": 2000,
        "temperature": 0.3,
        "messages": [
            {"role": "user", "content": prompt}
        ],
    }

    try:
        resp = requests.post(
            f"{base_url}/v1/messages",
            headers=headers,
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        # Anthropic format: content[0].text
        if "content" in data and isinstance(data["content"], list):
            return data["content"][0].get("text", "")
        # OpenAI-compatible format: choices[0].message.content
        elif "choices" in data:
            return data["choices"][0]["message"]["content"]
        return str(data)
    except Exception as e:
        print(f"[refiner] LLM call failed: {e}")
        return None


def refine(
    observation_report,
    session_content: str,
    config_path: Optional[Path] = None,
    auto_activate: bool = False,
) -> Optional[Path]:
    """Generate a skill file from an observation.

    Args:
        observation_report: ObservationReport from observer
        session_content: Raw transcript/session content used for analysis
        config_path: Path to harness_config.yaml
        auto_activate: If True, write directly to .claude/skills/harness/
                      If False, write to harness/skills/ for review

    Returns:
        Path to generated skill file, or None if generation failed.
    """
    config = load_config(config_path)
    if not config.get("refiner", {}).get("enabled", False):
        print("[refiner] Refiner is disabled in config. Skipping.")
        return None

    base_url, token = _get_api_credentials()
    if not token:
        print("[refiner] No API token found. Skipping.")
        return None

    # Build observation dict for prompt
    obs = {
        "session_id": observation_report.session_id,
        "action": observation_report.action,
        "confidence": observation_report.confidence,
        "reason": observation_report.reason,
        "summary": observation_report.summary,
        "tags": observation_report.tags,
        "skill_name": observation_report.skill_name,
    }

    prompt = _build_skill_prompt(session_content, obs, observation_report.action)
    print(f"[refiner] Calling LLM for action={observation_report.action}...")

    model = config.get("refiner", {}).get("model", "deepseek-v4-pro[1m]")
    result = _call_llm(prompt, base_url, token, model)

    if not result:
        print("[refiner] LLM returned no content.")
        return None

    # Determine output path
    harness_dir = Path(__file__).resolve().parent
    if auto_activate:
        skills_dir = Path.home() / ".claude" / "skills" / "harness"
    else:
        skills_dir = harness_dir / "skills"

    skills_dir.mkdir(parents=True, exist_ok=True)

    # Generate filename from tags + timestamp
    tag_part = observation_report.tags[0] if observation_report.tags else "general"
    safe_name = re.sub(r'[^a-z0-9-]', '-', tag_part.lower())[:30]
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    skill_path = skills_dir / f"{safe_name}-{timestamp}.md"

    skill_path.write_text(result, encoding="utf-8")
    print(f"[refiner] Skill written to: {skill_path}")

    return skill_path


def generate_preference(observation_report, session_content: str) -> Optional[str]:
    """Generate a preference statement from an update_preference observation."""
    base_url, token = _get_api_credentials()
    if not token:
        return None

    obs = {
        "summary": observation_report.summary,
        "session_id": observation_report.session_id,
        "confidence": observation_report.confidence,
        "tags": observation_report.tags,
    }

    prompt = _build_skill_prompt(session_content, obs, "update_preference")
    result = _call_llm(prompt, base_url, token, "deepseek-v4-pro[1m]")

    if result:
        return result.strip()
    return None
