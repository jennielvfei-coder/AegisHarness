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

Generate an updated SKILL.md file. FIRST classify the skill:

**Skill Type (REQUIRED — output on first line):**
- `env-fix` — environment-level fix that applies across all tasks (e.g., WebFetch preflight, API config)
- `task-workflow` — steps/templates specific to one task (e.g., "how to generate daily news report")
- `mental-model` — general reasoning/classification pattern (e.g., "how to downgrade claims when challenged")

**Quality Score (REQUIRED — output on second line):** 0.0-1.0. 0.8+ = concrete, repeatable workflow usable by a fresh session.

Output format:
```
SKILL_TYPE: <type>
QUALITY_SCORE: <0.0-1.0>
```markdown
---

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

Generate a new SKILL.md file. FIRST classify the skill:

**Skill Type (REQUIRED — output on first line):**
- `env-fix` — environment-level fix that applies across all tasks (e.g., WebFetch preflight, API config)
- `task-workflow` — steps/templates specific to one task (e.g., "how to generate daily news report")
- `mental-model` — general reasoning/classification pattern (e.g., "how to downgrade claims when challenged")

**Quality Score (REQUIRED — output on second line):** 0.0-1.0. 0.8+ = concrete, repeatable workflow usable by a fresh session.

Output format:
```
SKILL_TYPE: <type>
QUALITY_SCORE: <0.0-1.0>
```markdown
---

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
        "max_tokens": 4000,
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
        # Anthropic format: content is an array, find text blocks
        if "content" in data and isinstance(data["content"], list):
            text_blocks = [
                item.get("text", "")
                for item in data["content"]
                if item.get("type") == "text" and item.get("text")
            ]
            if text_blocks:
                return "\n".join(text_blocks)
        # OpenAI-compatible format: choices[0].message.content
        if "choices" in data:
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

    # Parse SKILL_TYPE and QUALITY_SCORE from LLM response
    skill_type = "mental-model"  # default
    quality_score = observation_report.confidence  # default: observer's estimate

    type_match = re.search(r'SKILL_TYPE:\s*(\S+)', result)
    if type_match:
        skill_type = type_match.group(1).strip()
        if skill_type not in ("env-fix", "task-workflow", "mental-model"):
            skill_type = "mental-model"
        print(f"[refiner] Parsed skill_type={skill_type}")

    qs_match = re.search(r'QUALITY_SCORE:\s*([\d.]+)', result)
    if qs_match:
        try:
            quality_score = round(float(qs_match.group(1)), 2)
            quality_score = max(0.0, min(1.0, quality_score))
            print(f"[refiner] Parsed quality_score={quality_score}")
        except ValueError:
            pass

    # Strip the SKILL_TYPE/QUALITY_SCORE lines from the markdown output
    skill_content = re.sub(r'^SKILL_TYPE:\s*\S+\s*\n?', '', result, flags=re.MULTILINE)
    skill_content = re.sub(r'^QUALITY_SCORE:\s*[\d.]+\s*\n?', '', skill_content, flags=re.MULTILINE)

    # Add quality_score to frontmatter if markdown has frontmatter
    skill_content = re.sub(
        r'(harness_confidence:\s*)[\d.]+',
        rf'\g<1>{quality_score}',
        skill_content,
    )

    # Store quality_score back to observations table
    _update_observation_confidence(observation_report.session_id, quality_score)

    # Route output based on skill_type (P₂ classification)
    harness_dir = Path(__file__).resolve().parent

    if skill_type == "task-workflow":
        # Store as fragment, not a skill file
        return _store_fragment(observation_report, skill_content, quality_score)

    # env-fix or mental-model → write skill file
    if auto_activate:
        skills_dir = Path.home() / ".claude" / "skills"
    else:
        skills_dir = harness_dir / "skills"

    skills_dir.mkdir(parents=True, exist_ok=True)

    # Generate filename: harness_<type>_<name>.md (P₅ unified path)
    tag_part = observation_report.tags[0] if observation_report.tags else "general"
    safe_name = re.sub(r'[^a-z0-9-]', '-', tag_part.lower())[:30]
    filename = f"harness_{skill_type}_{safe_name}.md"
    skill_path = skills_dir / filename

    skill_path.write_text(skill_content, encoding="utf-8")
    print(f"[refiner] Skill ({skill_type}, qs={quality_score}) written to: {skill_path}")

    _index_skill(filename, skill_type, quality_score, observation_report.tags)
    return skill_path


def _update_observation_confidence(session_id: str, quality_score: float):
    """Write quality_score back to observations.confidence in state.db."""
    try:
        from indexer import HarnessDB
        harness_dir = Path(__file__).resolve().parent
        db = HarnessDB(harness_dir / "state.db")
        with db._lock:
            db._conn.execute(
                "UPDATE observations SET confidence = ? WHERE session_id = ?",
                (quality_score, session_id),
            )
            db._conn.commit()
        db.close()
    except Exception as e:
        print(f"[refiner] Failed to update observation confidence: {e}")


def _store_fragment(report, content: str, quality_score: float) -> Optional[Path]:
    """Store a task-workflow as a fragment in the fragments table."""
    try:
        from indexer import HarnessDB
        harness_dir = Path(__file__).resolve().parent
        db = HarnessDB(harness_dir / "state.db")
        tag = report.tags[0] if report.tags else "task-workflow"
        with db._lock:
            db._conn.execute(
                """INSERT INTO fragments (tag, trigger_phrases, content, source_session,
                   confidence, created_at) VALUES (?, ?, ?, ?, ?, unixepoch())""",
                (tag, json.dumps(report.tags), content[:2000], report.session_id, quality_score),
            )
            db._conn.commit()
        db.close()
        print(f"[refiner] Fragment stored: tag={tag}, qs={quality_score}")
        return Path(f"fragments://{tag}")
    except Exception as e:
        print(f"[refiner] Failed to store fragment: {e}")
        return None


def _index_skill(filename: str, skill_type: str, quality_score: float, tags: list):
    """Insert or merge into skill_index; log evolution."""
    try:
        from indexer import HarnessDB
        harness_dir = Path(__file__).resolve().parent
        db = HarnessDB(harness_dir / "state.db")
        with db._lock:
            existing = db._conn.execute(
                "SELECT version FROM skill_index WHERE name = ?", (filename,)
            ).fetchone()
            if existing:
                nv = existing[0] + 1
                db._conn.execute(
                    "UPDATE skill_index SET tags=?, version=?, harness_confidence=? WHERE name=?",
                    (json.dumps(tags, ensure_ascii=False), nv, quality_score, filename),
                )
                db._conn.execute(
                    "INSERT INTO evolution_log(skill_name,action,change_summary,old_version,new_version) VALUES(?,?,?,?,?)",
                    (filename, "merge", f"Auto-merged v{nv}", existing[0], nv),
                )
                print(f"[refiner] Skill merged: {filename} v{existing[0]} → v{nv}")
            else:
                db._conn.execute(
                    "INSERT INTO skill_index(name,file_path,tags,version,harness_confidence,created_at) VALUES(?,?,?,1,?,unixepoch())",
                    (filename, f".claude/skills/{filename}", json.dumps(tags, ensure_ascii=False), quality_score),
                )
            db._conn.commit()
        db.close()
    except Exception as e:
        print(f"[refiner] Failed to index skill: {e}")


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
