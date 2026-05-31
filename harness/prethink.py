"""PreThink — PocketFlow-powered situational model for harness decision support.

Runs at session start (inject) and session end (observe) to produce a
structured assessment of "what kind of situation is this?"

HARD CONSTRAINTS:
  - Max graph depth: 4 (Node0 → Node1 → Node2 → Node3)
  - Zero LLM calls — pure keyword + DB query + statistics
  - No self-recursion, no Flow nesting, no cycles
  - "routine" exits at Node1 (skip Node2+Node3 = zero overhead)
"""

import sys
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from pocketflow import Node, Flow


def _load_prethink_config() -> dict:
    """Load prethink thresholds from harness_config.yaml, with hardcoded defaults."""
    defaults = {
        "signal_weights": {
            "blocking": 15, "correction": 10, "preference": 8,
            "recent_errors_bonus": 20, "last_action_bonus": 10,
            "multi_tool_bonus": 5, "recent_errors_threshold": 3,
            "multi_tool_threshold": 5,
        },
        "risk_tiers": {"high": 25, "medium": 12, "exploration": 5},
        "budget": {
            "recurring_failure_blocking": 38, "correction_blocking": 35,
            "recurring_failure_efficiency": 25, "correction_efficiency": 20,
            "exploration_efficiency": 18, "exploration_enhancement": 10,
            "preference_enhancement": 8, "routine_enhancement": 5, "default": 10,
        },
    }
    try:
        config_path = Path(__file__).resolve().parent / "harness_config.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        return config.get("prethink", defaults)
    except Exception:
        return defaults


_prethink_cfg = _load_prethink_config()


@dataclass
class SituationalModel:
    """Output of the PreThink graph — passed to observer and injector."""
    situation: str       # exploration | correction | recurring_failure | preference | routine
    continuity: str      # continuation | correction_of_prior | cold_start
    anchor_session: str | None
    anchor_fragments: list[str]
    severity: str        # blocking | efficiency | enhancement
    injection_budget: int
    confidence: float
    reasoning_path: str  # trace of nodes and their decisions for audit


# ══════════════════════════════════════════════════════════════════════════════
# Node0: Prejudge — 30ms keyword scan → risk_tier
# ══════════════════════════════════════════════════════════════════════════════

class PrejudgeNode(Node):
    """Scan user message + error history → risk_tier + trigger signals.

    Budget: <30ms. Pure keyword scan on pre-extracted data.
    DB data (recent errors, last action) passed via shared dict.
    """

    SIGNALS: dict[str, dict] = {
        "blocking": {
            "keywords": [
                "挂了", "失败", "不行", "又错了", "还是不行", "一直失败",
                "error", "failed", "broken", "not working", "dead",
                "崩了", "报错", "异常",
            ],
            "weight": 15,
        },
        "correction": {
            "keywords": [
                "不对", "错了", "不是这样", "改一下", "纠正", "重新",
                "那个不对", "你忘了", "应该是", "修一下", "搞错了",
            ],
            "weight": 10,
        },
        "preference": {
            "keywords": [
                "记住", "以后都", "默认", "习惯", "偏好", "下次",
                "总是", "永远", "从今往后", "帮我记", "保存",
            ],
            "weight": 8,
        },
    }

    def prep(self, shared):
        return {
            "msg": (shared.get("user_message", "") or "").lower(),
            "recent_error_count": shared.get("recent_error_count", 0),
            "last_action": shared.get("last_action", ""),
            "tool_count": shared.get("tool_count", 0),
        }

    def exec(self, prep_res):
        msg = prep_res["msg"]
        triggers: list[str] = []
        score = 0
        sw = _prethink_cfg["signal_weights"]
        rt = _prethink_cfg["risk_tiers"]

        for signal_type, cfg in self.SIGNALS.items():
            for kw in cfg["keywords"]:
                if kw.lower() in msg:
                    score += sw.get(signal_type, cfg["weight"])
                    triggers.append(f"{signal_type}:{kw}")
                    break

        if prep_res["recent_error_count"] >= sw.get("recent_errors_threshold", 3):
            score += sw.get("recent_errors_bonus", 20)
            triggers.append(f"recent_errors:{prep_res['recent_error_count']}")

        if prep_res["last_action"] in ("create_skill", "patch_skill"):
            score += sw.get("last_action_bonus", 10)
            triggers.append(f"last_action:{prep_res['last_action']}")

        tool_count = prep_res.get("tool_count", 0)
        if score == 0 and tool_count >= sw.get("multi_tool_threshold", 5):
            score = sw.get("multi_tool_bonus", 5)
            triggers.append("multi_tool:exploration")

        if score == 0:
            tier = "routine"
        elif score >= rt.get("high", 25):
            tier = "high"
        elif score >= rt.get("medium", 12):
            tier = "medium"
        elif score >= rt.get("exploration", 5):
            tier = "low"
        else:
            tier = "low"

        return {"risk_tier": tier, "risk_score": score, "triggers": triggers}

    def post(self, shared, prep_res, exec_res):
        shared["risk_tier"] = exec_res["risk_tier"]
        shared["risk_score"] = exec_res["risk_score"]
        shared["triggers"] = exec_res["triggers"]
        return "default"


# ══════════════════════════════════════════════════════════════════════════════
# Node1: Classify — risk_tier + signals → situation type
# ══════════════════════════════════════════════════════════════════════════════

class ClassifyNode(Node):
    """Map risk_tier + observed signals → situation classification.

    Routing contract:
      return "routine"  → Flow stops (no anchor or budget needed)
      return "continue" → Flow continues to AnchorNode → BudgetNode
    """

    def prep(self, shared):
        return {
            "risk_tier": shared.get("risk_tier", "routine"),
            "triggers": shared.get("triggers", []),
            "risk_score": shared.get("risk_score", 0),
            "tool_count": shared.get("tool_count", 0),
            "failure_count": shared.get("failure_count", 0),
            "data_quality_failures": shared.get("data_quality_failures", 0),
            "has_interruption": shared.get("has_interruption", False),
        }

    def exec(self, prep_res):
        triggers_str = " ".join(prep_res["triggers"])
        tier = prep_res["risk_tier"]

        if tier == "routine":
            return {"situation": "routine", "severity": "enhancement",
                    "confidence": 0.90,
                    "reason": "no risk signals, no error history"}

        if tier == "high":
            if "blocking" in triggers_str:
                if prep_res["failure_count"] >= 3:
                    return {"situation": "recurring_failure", "severity": "blocking",
                            "confidence": 0.85,
                            "reason": "blocking signals + repeated failures (>=3)"}
                return {"situation": "correction", "severity": "blocking",
                        "confidence": 0.80,
                        "reason": "blocking signals, isolated incident"}
            if "correction" in triggers_str:
                return {"situation": "correction", "severity": "efficiency",
                        "confidence": 0.80,
                        "reason": "explicit correction at high risk"}
            return {"situation": "exploration", "severity": "efficiency",
                    "confidence": 0.70,
                    "reason": "high risk, unclear signal — assume exploration with failures"}

        if tier == "medium":
            if "correction" in triggers_str:
                return {"situation": "correction", "severity": "efficiency",
                        "confidence": 0.75,
                        "reason": "correction signal at medium risk"}
            if prep_res["has_interruption"]:
                return {"situation": "correction", "severity": "efficiency",
                        "confidence": 0.70,
                        "reason": "user interruption detected"}
            if prep_res["data_quality_failures"] > 0:
                return {"situation": "recurring_failure", "severity": "efficiency",
                        "confidence": 0.70,
                        "reason": f"data quality failures: {prep_res['data_quality_failures']}"}
            return {"situation": "exploration", "severity": "enhancement",
                    "confidence": 0.65,
                    "reason": "medium risk without clear correction signal"}

        if tier == "low":
            if "correction" in triggers_str:
                return {"situation": "correction", "severity": "enhancement",
                        "confidence": 0.65,
                        "reason": "mild correction signal at low risk"}
            if "preference" in triggers_str:
                return {"situation": "preference", "severity": "enhancement",
                        "confidence": 0.80,
                        "reason": "preference statement detected"}
            if prep_res["tool_count"] >= 3:
                return {"situation": "exploration", "severity": "enhancement",
                        "confidence": 0.60,
                        "reason": "multi-tool session, no failures"}
            return {"situation": "routine", "severity": "enhancement",
                    "confidence": 0.70,
                    "reason": "low risk, minimal activity — treated as routine"}

        return {"situation": "routine", "severity": "enhancement",
                "confidence": 0.50, "reason": "fallback"}

    def post(self, shared, prep_res, exec_res):
        shared["situation"] = exec_res["situation"]
        shared["severity"] = exec_res["severity"]
        shared["prethink_confidence"] = exec_res["confidence"]
        shared["classification_reason"] = exec_res["reason"]

        if exec_res["situation"] == "routine":
            return "routine"

        tier = shared.get("risk_tier", "low")
        shared["anchor_mode"] = "deep" if tier in ("medium", "high") else "light"
        return "continue"


# ══════════════════════════════════════════════════════════════════════════════
# Node2: Anchor — cross-session continuity
# ══════════════════════════════════════════════════════════════════════════════

class AnchorNode(Node):
    """Determine cross-session continuity via recent observation history.

    Light mode: check last session's action only.
    Deep mode:  check last 3 sessions + fetch failure_pattern fragments.
    """

    def prep(self, shared):
        return {
            "db": shared.get("db"),
            "anchor_mode": shared.get("anchor_mode", "light"),
            "situation": shared.get("situation", ""),
        }

    def exec(self, prep_res):
        result = {
            "continuity": "cold_start",
            "anchor_session": None,
            "anchor_fragments": [],
        }
        db = prep_res.get("db")
        if db is None:
            return result

        try:
            cur = db._conn.execute(
                "SELECT session_id, action, confidence "
                "FROM observations ORDER BY processed_at DESC LIMIT 1"
            )
            last = cur.fetchone()
            if last is None:
                return result

            sid, last_action, last_conf = last
            result["anchor_session"] = sid

            if last_action in ("create_skill", "patch_skill") and (last_conf or 0) > 0.5:
                result["continuity"] = "correction_of_prior"
            else:
                result["continuity"] = "continuation"

            if prep_res["anchor_mode"] != "deep":
                return result

            # Deep: fetch failure patterns for this domain
            cur = db._conn.execute(
                "SELECT tag FROM fragments "
                "WHERE fragment_type='failure_pattern' "
                "ORDER BY confidence DESC LIMIT 3"
            )
            result["anchor_fragments"] = [row[0] for row in cur.fetchall()]

        except Exception as e:
            print(f"[harness] ERROR [prethink.AnchorNode] {e}",
                  file=sys.stderr, flush=True)

        return result

    def post(self, shared, prep_res, exec_res):
        shared["continuity"] = exec_res["continuity"]
        shared["anchor_session"] = exec_res["anchor_session"]
        shared["anchor_fragments"] = exec_res["anchor_fragments"]
        return "default"


# ══════════════════════════════════════════════════════════════════════════════
# Node3: Budget — severity → injection line budget
# ══════════════════════════════════════════════════════════════════════════════

class BudgetNode(Node):
    """Compute injection line budget from (situation, severity).

    blocking   → near maximum (30-38 lines): need full context
    efficiency → moderate (15-25): balance context vs. budget
    enhancement → minimal (5-10): don't crowd the session
    """

    BUDGET: dict[tuple[str, str], int] = {
        ("recurring_failure", "blocking"): 38,
        ("correction",        "blocking"): 35,
        ("recurring_failure", "efficiency"): 25,
        ("correction",        "efficiency"): 20,
        ("exploration",       "efficiency"): 18,
        ("exploration",       "enhancement"): 10,
        ("preference",        "enhancement"): 8,
        ("routine",           "enhancement"): 5,
    }

    def prep(self, shared):
        return {
            "situation": shared.get("situation", "routine"),
            "severity": shared.get("severity", "enhancement"),
        }

    def exec(self, prep_res):
        # Read from config with class-level BUDGET as fallback
        cfg = _prethink_cfg["budget"]
        key = f"{prep_res['situation']}_{prep_res['severity']}"
        budget = cfg.get(key, self.BUDGET.get(
            (prep_res["situation"], prep_res["severity"]), cfg.get("default", 10)))
        return {"injection_budget": budget}

    def post(self, shared, prep_res, exec_res):
        shared["injection_budget"] = exec_res["injection_budget"]
        return "default"


# ══════════════════════════════════════════════════════════════════════════════
# Graph assembly + entry point
# ══════════════════════════════════════════════════════════════════════════════

def run_prethink(user_message: str = "",
                 fingerprint: dict | None = None,
                 db=None) -> SituationalModel:
    """Run the PreThink graph and return a SituationalModel.

    Args:
        user_message: First user message of the session (lowercased in Node0).
        fingerprint: Dict with tool_count, failure_count, data_quality_failures,
                     recent_error_count, has_interruption, last_action.
                     Partial OK — missing keys default to 0/False.
        db: HarnessDB instance for anchor lookups (optional).
    """
    if fingerprint is None:
        fingerprint = {}

    shared = {
        "user_message": user_message,
        "recent_error_count": fingerprint.get("recent_error_count", 0),
        "last_action": fingerprint.get("last_action", ""),
        "tool_count": fingerprint.get("tool_count", 0),
        "failure_count": fingerprint.get("failure_count", 0),
        "data_quality_failures": fingerprint.get("data_quality_failures", 0),
        "has_interruption": fingerprint.get("has_interruption", False),
        "db": db,
    }

    n0 = PrejudgeNode()
    n1 = ClassifyNode()
    n2 = AnchorNode()
    n3 = BudgetNode()

    n0 >> n1                     # Node0 default → Node1
    n1 - "continue" >> n2        # non-routine → Anchor
    n2 >> n3                     # Anchor default → Budget
    # n3 has no successors → Flow ends after budget
    # "routine" from n1 has no successor → Flow ends (with benign warning)

    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        Flow(start=n0).run(shared)

    path = []
    if shared.get("triggers"):
        path.append(f"n0:{shared['risk_tier']}({shared['risk_score']})")
    path.append(f"n1:{shared.get('situation','?')}")
    if shared.get("anchor_mode"):
        path.append(f"n2:{shared['anchor_mode']}:{shared.get('continuity','?')}")
    if shared.get("injection_budget"):
        path.append(f"n3:budget={shared['injection_budget']}")

    return SituationalModel(
        situation=shared.get("situation", "routine"),
        continuity=shared.get("continuity", "cold_start"),
        anchor_session=shared.get("anchor_session"),
        anchor_fragments=shared.get("anchor_fragments", []),
        severity=shared.get("severity", "enhancement"),
        injection_budget=shared.get("injection_budget", 10),
        confidence=shared.get("prethink_confidence", 0.5),
        reasoning_path=" → ".join(path),
    )


def situational_model_to_dict(model: SituationalModel) -> dict:
    """Serialize SituationalModel to a JSON-safe dict for storage."""
    return {
        "situation": model.situation,
        "continuity": model.continuity,
        "anchor_session": model.anchor_session,
        "anchor_fragments": model.anchor_fragments,
        "severity": model.severity,
        "injection_budget": model.injection_budget,
        "confidence": model.confidence,
        "reasoning_path": model.reasoning_path,
    }


def situational_model_from_dict(data: dict) -> SituationalModel:
    """Deserialize a stored dict back to SituationalModel."""
    return SituationalModel(
        situation=data.get("situation", "routine"),
        continuity=data.get("continuity", "cold_start"),
        anchor_session=data.get("anchor_session"),
        anchor_fragments=data.get("anchor_fragments", []),
        severity=data.get("severity", "enhancement"),
        injection_budget=data.get("injection_budget", 10),
        confidence=data.get("confidence", 0.5),
        reasoning_path=data.get("reasoning_path", ""),
    )
