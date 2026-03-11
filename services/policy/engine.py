"""Rule evaluation and dedup logic."""
import time
import json
import uuid
import hashlib
import structlog
from typing import Dict, Any, List, Optional
from collections import defaultdict
from loader import PolicyRule

logger = structlog.get_logger("hostspectra.policy.engine")


class DedupTracker:
    """H4: Redis-backed dedup tracker. Falls back to in-memory if Redis unavailable.
    
    Persists dedup state across service restarts to prevent alert flooding.
    """

    def __init__(self, redis_client=None):
        self._redis = redis_client
        # In-memory fallback
        self._occurrences: Dict[str, List[float]] = defaultdict(list)
        self._prefix = "sentinel:dedup:"

    async def record(self, key: str, timestamp: float) -> int:
        """Record an occurrence and return the count within the dedup window."""
        if self._redis:
            try:
                redis_key = f"{self._prefix}{key}"
                await self._redis.zadd(redis_key, {str(timestamp): timestamp})
                # Set TTL to prevent unbounded growth (5 min max)
                await self._redis.expire(redis_key, 300)
                return await self._redis.zcard(redis_key)
            except Exception:
                pass
        # Fallback to in-memory
        self._occurrences[key].append(timestamp)
        return len(self._occurrences[key])

    async def count_within_window(self, key: str, window_seconds: int, now: float) -> int:
        """Count occurrences within the specified time window."""
        cutoff = now - window_seconds

        if self._redis:
            try:
                redis_key = f"{self._prefix}{key}"
                # Remove entries older than cutoff
                await self._redis.zremrangebyscore(redis_key, "-inf", cutoff)
                return await self._redis.zcard(redis_key)
            except Exception:
                pass
        # Fallback to in-memory
        if key not in self._occurrences:
            return 0
        self._occurrences[key] = [
            t for t in self._occurrences[key] if t > cutoff
        ]
        return len(self._occurrences[key])

    async def cleanup(self, max_age_seconds: int = 300):
        """Remove entries older than max_age_seconds."""
        if self._redis:
            # Redis TTL handles cleanup automatically
            return
        now = time.time()
        cutoff = now - max_age_seconds
        keys_to_delete = []
        for key, timestamps in self._occurrences.items():
            self._occurrences[key] = [t for t in timestamps if t > cutoff]
            if not self._occurrences[key]:
                keys_to_delete.append(key)
        for key in keys_to_delete:
            del self._occurrences[key]


class PolicyEngine:
    """
    Evaluates ML scores against policy rules and generates alerts.
    
    Features:
    - Score threshold matching
    - Anomaly type filtering
    - Deduplication window (prevents alert flooding)
    - Min occurrence checking
    - Alert generation with full context
    """

    def __init__(self, redis_client=None):
        self.dedup = DedupTracker(redis_client=redis_client)
        self._last_cleanup = time.time()
        self._cleanup_interval = 60  # cleanup dedup tracker every 60s
        # Cooldown: once an alert fires for a dedup key, suppress for this many seconds
        self._last_alert_time: Dict[str, float] = {}
        self._alert_cooldown = 60  # seconds between alerts for same key

    async def evaluate(
        self,
        score_data: Dict[str, Any],
        rules: List[PolicyRule],
    ) -> List[Dict[str, Any]]:
        """
        Evaluate a score event against all active policy rules.
        
        Args:
            score_data: ML score event (score, risk_level, anomaly_type, etc.)
            rules: List of active policy rules to evaluate
            
        Returns:
            List of generated alert dicts (may be empty)
        """
        alerts = []
        now = time.time()

        # Periodic cleanup
        if now - self._last_cleanup > self._cleanup_interval:
            await self.dedup.cleanup()
            self._last_cleanup = now

        score = float(score_data.get("score", 0.0))
        risk_level = score_data.get("risk_level", "normal")
        anomaly_type = score_data.get("anomaly_type", "")
        source_ip = score_data.get("source_ip", "")
        features = score_data.get("features", {})
        timestamp = score_data.get("timestamp", "")

        for rule in rules:
            if not rule.enabled:
                continue

            # Check score threshold
            if score < rule.score_threshold:
                continue

            # Check anomaly type filter (if specified)
            if rule.anomaly_type and anomaly_type and rule.anomaly_type != anomaly_type:
                continue

            # Generate dedup key
            dedup_key = self._make_dedup_key(rule.name, anomaly_type, source_ip)

            # Check dedup window and min occurrences
            if rule.dedup_window > 0:
                await self.dedup.record(dedup_key, now)
                count = await self.dedup.count_within_window(dedup_key, rule.dedup_window, now)

                if count < rule.min_occurrences:
                    logger.debug(
                        "policy_dedup_waiting",
                        rule=rule.name,
                        count=count,
                        required=rule.min_occurrences,
                    )
                    continue

                # Cooldown: suppress if we already alerted for this key recently
                last_fired = self._last_alert_time.get(dedup_key, 0)
                if (now - last_fired) < self._alert_cooldown:
                    continue
            else:
                # No dedup window — record and proceed
                await self.dedup.record(dedup_key, now)

            # Generate alert
            alert = self._create_alert(
                rule=rule,
                score=score,
                risk_level=risk_level,
                anomaly_type=anomaly_type,
                source_ip=source_ip,
                features=features,
                timestamp=timestamp,
            )
            alerts.append(alert)
            self._last_alert_time[dedup_key] = now

            logger.info(
                "policy_alert_generated",
                rule=rule.name,
                severity=rule.severity,
                score=round(score, 4),
                action=rule.action,
                anomaly_type=anomaly_type,
            )

        return alerts

    def _make_dedup_key(self, rule_name: str, anomaly_type: str, source_ip: str) -> str:
        """Generate a dedup key from rule + event context."""
        raw = f"{rule_name}:{anomaly_type}:{source_ip}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _create_alert(
        self,
        rule: PolicyRule,
        score: float,
        risk_level: str,
        anomaly_type: str,
        source_ip: str,
        features: Dict[str, Any],
        timestamp: str,
    ) -> Dict[str, Any]:
        """Create a structured alert dictionary."""
        alert_id = f"alert_{uuid.uuid4().hex[:12]}"

        return {
            "alert_id": alert_id,
            "timestamp": timestamp or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "policy_name": rule.name,
            "severity": rule.severity,
            "score": round(score, 4),
            "risk_level": risk_level,
            "anomaly_type": anomaly_type or "unknown",
            "source_ip": source_ip or "",
            "action": rule.action,
            "notify": rule.notify,
            "message": self._build_message(rule, score, anomaly_type, source_ip),
            "details": {
                "rule_conditions": rule.conditions,
                "contributing_features": self._extract_top_features(features),
            },
        }

    def _build_message(
        self, rule: PolicyRule, score: float, anomaly_type: str, source_ip: str
    ) -> str:
        """Build a human-readable alert message."""
        parts = [f"[{rule.severity.upper()}] Policy '{rule.name}' triggered"]
        parts.append(f"(score: {score:.2f})")

        if anomaly_type:
            type_labels = {
                "brute_force": "Brute force attack detected",
                "port_scan": "Port scan activity detected",
                "ssh_failure": "SSH authentication anomaly",
                "process_spike": "Unusual process activity",
            }
            parts.append(f"— {type_labels.get(anomaly_type, anomaly_type)}")

        if source_ip:
            parts.append(f"from {source_ip}")

        if rule.action != "alert_only":
            parts.append(f"→ Action: {rule.action}")

        return " ".join(parts)

    def _extract_top_features(
        self, features: Dict[str, Any], top_n: int = 5
    ) -> Dict[str, Any]:
        """Extract the top N most significant features from the score context."""
        if not features:
            return {}

        # Sort features by value (descending) to find most significant
        numeric = {}
        for k, v in features.items():
            try:
                val = float(v)
                if val > 0:
                    numeric[k] = val
            except (TypeError, ValueError):
                continue

        sorted_features = sorted(numeric.items(), key=lambda x: x[1], reverse=True)
        return dict(sorted_features[:top_n])

    def classify_anomaly(self, features: Dict[str, Any]) -> str:
        """
        Classify the anomaly type based on which features are most elevated.
        
        Returns: anomaly type string (brute_force, port_scan, ssh_failure, process_spike, unknown)
        """
        if not features:
            return "unknown"

        auth_failures = float(features.get("log_auth_failures", 0))
        sudo_attempts = float(features.get("log_sudo_attempts", 0))
        scan_candidates = float(features.get("net_port_scan_candidates", 0))
        new_connections = float(features.get("net_new_connections", 0))
        cpu_spikes = float(features.get("proc_cpu_spikes", 0))
        mem_spikes = float(features.get("proc_memory_spikes", 0))
        new_spawns = float(features.get("proc_new_spawns", 0))
        unusual_children = float(features.get("proc_unusual_children", 0))

        # Classify based on dominant feature
        if auth_failures > 5 or sudo_attempts > 3:
            if auth_failures > 10:
                return "brute_force"
            return "ssh_failure"

        if scan_candidates > 0 or new_connections > 50:
            return "port_scan"

        if cpu_spikes > 0 or mem_spikes > 0 or new_spawns > 10 or unusual_children > 0:
            return "process_spike"

        return "unknown"
