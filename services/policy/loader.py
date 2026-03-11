"""YAML policy file loader with hot-reload."""
import os
import time
import threading
import structlog
import yaml
from typing import List, Dict, Any, Optional, Callable

logger = structlog.get_logger("hostspectra.policy.loader")


class PolicyRule:
    """Represents a single policy rule with conditions and actions."""

    def __init__(self, data: Dict[str, Any]):
        self.name: str = data.get("name", "unnamed")
        self.enabled: bool = data.get("enabled", True)
        self.conditions: Dict[str, Any] = data.get("conditions", {})
        self.action: str = data.get("action", "alert_only")
        self.severity: str = data.get("severity", "medium")
        self.notify: bool = data.get("notify", False)

    @property
    def score_threshold(self) -> float:
        return self.conditions.get("score_above", 0.0)

    @property
    def anomaly_type(self) -> Optional[str]:
        return self.conditions.get("anomaly_type")

    @property
    def dedup_window(self) -> int:
        return self.conditions.get("repeated_within_seconds", 0)

    @property
    def min_occurrences(self) -> int:
        return self.conditions.get("min_occurrences", 1)

    def __repr__(self) -> str:
        return f"PolicyRule(name={self.name}, enabled={self.enabled}, action={self.action})"


class PolicyLoader:
    """
    Loads and watches policy configuration file for changes.
    
    Supports hot-reload: file modifications are detected via polling
    (every 2 seconds) and rules are reloaded automatically.
    """

    def __init__(self, config_path: str = "/config/policies.yml"):
        self.config_path = config_path
        self.rules: List[PolicyRule] = []
        self._last_modified: float = 0
        self._lock = threading.Lock()
        self._watchers: List[Callable] = []
        self._running = False
        self._watch_thread: Optional[threading.Thread] = None

    def load(self) -> List[PolicyRule]:
        """Load policies from YAML file."""
        try:
            if not os.path.exists(self.config_path):
                logger.warning("policy_config_not_found", path=self.config_path)
                self.rules = self._default_rules()
                return self.rules

            with open(self.config_path, "r") as f:
                data = yaml.safe_load(f) or {}

            raw_policies = data.get("policies", [])
            new_rules = []
            for raw in raw_policies:
                try:
                    rule = PolicyRule(raw)
                    new_rules.append(rule)
                except Exception as e:
                    logger.error("invalid_policy_rule", rule=raw, error=str(e))

            with self._lock:
                self.rules = new_rules
                self._last_modified = os.path.getmtime(self.config_path)

            logger.info(
                "policies_loaded",
                count=len(new_rules),
                enabled=sum(1 for r in new_rules if r.enabled),
                path=self.config_path,
            )
            return new_rules

        except yaml.YAMLError as e:
            logger.error("policy_yaml_parse_error", error=str(e))
            return self.rules
        except Exception as e:
            logger.error("policy_load_error", error=str(e))
            return self.rules

    def _default_rules(self) -> List[PolicyRule]:
        """Return sensible default policy rules when no config file exists."""
        return [
            PolicyRule({
                "name": "high_score_alert",
                "enabled": True,
                "conditions": {"score_above": 0.75},
                "action": "alert_only",
                "severity": "high",
                "notify": True,
            }),
            PolicyRule({
                "name": "critical_score_alert",
                "enabled": True,
                "conditions": {"score_above": 0.90},
                "action": "alert_only",
                "severity": "critical",
                "notify": True,
            }),
        ]

    def get_active_rules(self) -> List[PolicyRule]:
        """Return only enabled policy rules."""
        with self._lock:
            return [r for r in self.rules if r.enabled]

    def on_reload(self, callback: Callable):
        """Register a callback for when policies are reloaded."""
        self._watchers.append(callback)

    def start_watching(self):
        """Start background thread to watch for config file changes."""
        self._running = True
        self._watch_thread = threading.Thread(
            target=self._watch_loop, daemon=True, name="policy-watcher"
        )
        self._watch_thread.start()
        logger.info("policy_watcher_started", path=self.config_path)

    def stop_watching(self):
        """Stop the file watcher."""
        self._running = False
        if self._watch_thread and self._watch_thread.is_alive():
            self._watch_thread.join(timeout=5)

    def _watch_loop(self):
        """Poll for file changes every 2 seconds."""
        while self._running:
            try:
                if os.path.exists(self.config_path):
                    current_mtime = os.path.getmtime(self.config_path)
                    if current_mtime > self._last_modified:
                        logger.info("policy_config_changed", path=self.config_path)
                        old_count = len(self.rules)
                        self.load()
                        new_count = len(self.rules)
                        logger.info(
                            "policies_reloaded",
                            old_count=old_count,
                            new_count=new_count,
                        )
                        for callback in self._watchers:
                            try:
                                callback(self.rules)
                            except Exception as e:
                                logger.error("policy_reload_callback_error", error=str(e))
            except Exception as e:
                logger.error("policy_watch_error", error=str(e))

            time.sleep(2)
