"""Skill Writer Agent — convert ObservationReport → skill file.

Independent agent: own prompt, own LLM session, no observer context.
Orchestrated by harness_daemon.py when confidence > 0.6.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional


def build_prompt(session_content: str, observation: dict, action: str) -> str:
    """Build a focused prompt for the skill writer agent."""

    existing_info = observation.get("existing_skills_text", "")
    existing_block = f"\n{existing_info}\n" if existing_info else ""

    base = f"""You are a specialized Skill Writer. Your ONLY job: convert an observation into a reusable skill file.

Session context:
{_truncate(session_content, 3000)}

Observation: {observation.get('summary', '')}
Tags: {observation.get('tags', [])}
Reason: {observation.get('reason', '')}
{existing_block}
Output format (STRICT):
```
SKILL_TYPE: <env-fix | task-workflow | mental-model>
QUALITY_SCORE: <0.0-1.0, 0.8+=concrete-repeatable>
ACTION: <create | merge | discard>
```markdown
---
name: <kebab-case>
description: <one-line>
tags: [{', '.join(observation.get('tags', ['general']))}]
triggers:
  - <when-to-suggest>
version: 1
harness_confidence: <match QUALITY_SCORE>
---

# <Skill Name>

## 执行逻辑
### When to Use
### Step-by-Step
### How to Verify

## 异常处理
### Edge Cases
### Fallback
```

Focus: extract REUSABLE patterns, not task-specific steps. env-fix = cross-task fix. mental-model = reasoning pattern. task-workflow = specific steps → mark as fragment.
"""

    correction_extra = f"""
This is a CORRECTION. The user corrected the assistant.
Update existing skill: {observation.get('skill_name', 'unknown')}
What changed: {observation.get('reason', '')}
"""

    if action == "patch_skill":
        return base + correction_extra
    return base


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
            json={"model": model, "max_tokens": 4000, "temperature": 0.3,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=120,
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
        print(f"[skill_writer] LLM call failed: {e}")
    return None


def parse_response(result: str, default_confidence: float) -> tuple:
    """Parse SKILL_TYPE, QUALITY_SCORE, ACTION from LLM output."""
    skill_type = "mental-model"
    quality_score = default_confidence
    action = "create"

    tm = re.search(r'SKILL_TYPE:\s*(\S+)', result)
    if tm and tm.group(1).strip() in ("env-fix", "task-workflow", "mental-model"):
        skill_type = tm.group(1).strip()

    qm = re.search(r'QUALITY_SCORE:\s*([\d.]+)', result)
    if qm:
        try:
            quality_score = round(float(qm.group(1)), 2)
        except ValueError:
            pass

    am = re.search(r'ACTION:\s*(\S+)', result)
    if am and am.group(1).strip() in ("create", "merge", "discard"):
        action = am.group(1).strip()

    # Strip metadata lines
    content = re.sub(r'^(SKILL_TYPE|QUALITY_SCORE|ACTION):\s*\S+\s*\n?', '', result,
                     flags=re.MULTILINE)
    content = re.sub(
        r'(harness_confidence:\s*)[\d.]+',
        rf'\g<1>{quality_score}', content,
    )

    return skill_type, quality_score, action, content.strip()


def _update_observation_confidence(session_id: str, score: float):
    """Write quality_score back to observations table."""
    try:
        import sqlite3
        harness_dir = Path(__file__).resolve().parent.parent
        conn = sqlite3.connect(str(harness_dir / "state.db"), timeout=2)
        conn.execute("UPDATE observations SET confidence=? WHERE session_id=?", (score, session_id))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[skill_writer] obs update failed: {e}")


def run(report, session_content: str, config: dict) -> Optional[dict]:
    """Main entry point for the skill writer agent.

    Returns: dict with skill_type, quality_score, action, content, path
             or None if LLM call fails or confidence too low.
    """
    if not config.get("refiner", {}).get("enabled", False):
        return None

    if report.confidence < 0.3:
        print("[skill_writer] Confidence too low, skipping.")
        return None

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
        print("[skill_writer] No API token.")
        return None

    obs = {
        "session_id": report.session_id,
        "action": report.action,
        "confidence": report.confidence,
        "reason": report.reason,
        "summary": report.summary,
        "tags": report.tags,
        "skill_name": report.skill_name,
        "existing_skills_text": getattr(report, "existing_skills_text", ""),
    }

    prompt = build_prompt(session_content, obs, report.action)
    print(f"[skill_writer] Calling LLM...")
    result = _call_llm(prompt, base_url, token,
                       config.get("refiner", {}).get("model", "deepseek-v4-pro"))

    if not result:
        return None

    skill_type, quality_score, action, content = parse_response(result, report.confidence)
    print(f"[skill_writer] action={action} type={skill_type} qs={quality_score}")

    if action == "discard":
        print("[skill_writer] Discarded — similar skill already exists.")
        return {"skill_type": skill_type, "quality_score": quality_score,
                "action": "discard", "content": None, "path": None}

    # Write to review queue
    harness_dir = Path(__file__).resolve().parent.parent
    skills_dir = harness_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    tag_part = report.tags[0] if report.tags else "general"
    safe_name = re.sub(r'[^a-z0-9-]', '-', tag_part.lower())[:30]
    filename = f"harness_{skill_type}_{safe_name}.md"
    skill_path = skills_dir / filename
    skill_path.write_text(content, encoding="utf-8")
    print(f"[skill_writer] Written: {skill_path}")

    return {
        "skill_type": skill_type,
        "quality_score": quality_score,
        "action": action,
        "content": content,
        "path": str(skill_path),
    }
