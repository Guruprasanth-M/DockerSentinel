"""Isolation Forest model loader and scoring."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import joblib
import numpy as np
import structlog
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

log = structlog.get_logger()

# Feature names (must match FeatureVector order)
FEATURE_NAMES = [
    "log_total_events",
    "log_auth_failures",
    "log_sudo_attempts",
    "log_service_restarts",
    "net_new_connections",
    "net_unique_ips",
    "net_port_scan_candidates",
    "net_bytes_rate",
    "proc_new_spawns",
    "proc_cpu_spikes",
    "proc_memory_spikes",
    "proc_unusual_children",
]


class Scorer:
    """Anomaly detection scorer using pre-trained Isolation Forest + z-score + EMA."""

    def __init__(self, model_dir: str = "/data/models") -> None:
        self.model_dir = model_dir
        self.model: Optional[IsolationForest] = None
        self.scaler: Optional[StandardScaler] = None
        self.baseline_stats: Dict[str, Dict[str, float]] = {}
        self.metadata: Dict = {}

        # EMA state
        self._ema_scores: List[float] = []
        self._ema_alpha = 0.3  # Smoothing factor

        self._load_model()

    def _load_model(self) -> None:
        """Load the pre-trained model, scaler, and baseline stats."""
        # Check pre-trained model directory first, then mounted volume
        search_dirs = [self.model_dir, "/app/pretrained"]

        for search_dir in search_dirs:
            model_path = os.path.join(search_dir, "isolation_forest_v1.pkl")
            scaler_path = os.path.join(search_dir, "scaler_v1.pkl")
            stats_path = os.path.join(search_dir, "baseline_stats.json")
            meta_path = os.path.join(search_dir, "model_metadata.json")

            if os.path.exists(model_path):
                try:
                    self.model = joblib.load(model_path)
                    log.info("model_loaded", path=model_path)
                except Exception as e:
                    log.error("model_load_error", path=model_path, error=str(e))
                    continue

                if os.path.exists(scaler_path):
                    try:
                        self.scaler = joblib.load(scaler_path)
                        log.info("scaler_loaded", path=scaler_path)
                    except Exception as e:
                        log.error("scaler_load_error", error=str(e))

                if os.path.exists(stats_path):
                    try:
                        with open(stats_path, "r") as f:
                            self.baseline_stats = json.load(f)
                        log.info("baseline_stats_loaded")
                    except Exception as e:
                        log.error("stats_load_error", error=str(e))

                if os.path.exists(meta_path):
                    try:
                        with open(meta_path, "r") as f:
                            self.metadata = json.load(f)
                        log.info("metadata_loaded", version=self.metadata.get("model_version"))
                    except Exception as e:
                        log.error("metadata_load_error", error=str(e))

                # Copy model files to persistent volume if not already there
                if search_dir != self.model_dir and os.path.exists(self.model_dir):
                    try:
                        for src_name in ["isolation_forest_v1.pkl", "scaler_v1.pkl",
                                         "baseline_stats.json", "model_metadata.json"]:
                            src = os.path.join(search_dir, src_name)
                            dst = os.path.join(self.model_dir, src_name)
                            if os.path.exists(src) and not os.path.exists(dst):
                                import shutil
                                shutil.copy2(src, dst)
                        log.info("model_copied_to_volume", dest=self.model_dir)
                    except Exception as e:
                        log.warning("model_copy_error", error=str(e))

                return

        log.warning("no_model_found", search_dirs=search_dirs)

    @property
    def is_ready(self) -> bool:
        return self.model is not None

    def _feature_vector_to_array(self, features: Dict) -> np.ndarray:
        """Convert a feature dictionary to a numpy array in the correct order."""
        values = [features.get(name, 0.0) for name in FEATURE_NAMES]
        return np.array(values).reshape(1, -1)

    def _isolation_forest_score(self, X: np.ndarray) -> float:
        """Score using Isolation Forest. Returns 0.0 (normal) to 1.0 (anomalous)."""
        if self.model is None:
            return 0.0

        if self.scaler is not None:
            X = self.scaler.transform(X)

        # decision_function returns negative for anomalies, positive for normal
        raw_score = self.model.decision_function(X)[0]

        # Normalize to 0.0 - 1.0 (higher = more anomalous)
        # Typical raw scores range from -0.5 (anomaly) to 0.5 (normal)
        normalized = max(0.0, min(1.0, 0.5 - raw_score))
        return float(normalized)

    def _zscore_score(self, features: Dict) -> float:
        """Score using z-score deviation from baseline. Returns 0.0-1.0."""
        if not self.baseline_stats:
            return 0.0

        z_scores = []
        for name in FEATURE_NAMES:
            value = features.get(name, 0.0)
            stats = self.baseline_stats.get(name, {})
            mean = stats.get("mean", 0.0)
            std = stats.get("std", 1.0)

            if std > 0:
                z = abs(value - mean) / std
                z_scores.append(z)

        if not z_scores:
            return 0.0

        # Max z-score, capped and normalized
        max_z = max(z_scores)
        # z=3 maps to ~0.5, z=6 maps to ~1.0
        normalized = min(1.0, max_z / 6.0)
        return float(normalized)

    def _ema_score(self, current_score: float) -> float:
        """Apply EMA smoothing to detect drift. Returns 0.0-1.0."""
        self._ema_scores.append(current_score)

        # Keep last 60 scores (5 minutes at 5-second windows)
        if len(self._ema_scores) > 60:
            self._ema_scores = self._ema_scores[-60:]

        if len(self._ema_scores) < 2:
            return current_score

        # Calculate EMA
        ema = self._ema_scores[0]
        for score in self._ema_scores[1:]:
            ema = self._ema_alpha * score + (1 - self._ema_alpha) * ema

        return float(ema)

    def score(self, features: Dict) -> Dict:
        """Score a feature vector using ensemble of methods.

        Returns a scoring result dict with individual and combined scores.
        """
        X = self._feature_vector_to_array(features)

        # Individual scores
        if_score = self._isolation_forest_score(X)
        z_score = self._zscore_score(features)

        # Ensemble: weighted average
        raw_ensemble = 0.5 * if_score + 0.3 * z_score + 0.2 * max(if_score, z_score)

        # Apply EMA smoothing
        ema_score = self._ema_score(raw_ensemble)

        # Final score: blend of instant and smoothed
        final_score = 0.7 * raw_ensemble + 0.3 * ema_score

        # Determine risk level
        if final_score >= 0.8:
            risk_level = "critical"
        elif final_score >= 0.6:
            risk_level = "suspicious"
        elif final_score >= 0.4:
            risk_level = "elevated"
        else:
            risk_level = "normal"

        return {
            "score": round(final_score, 4),
            "risk_level": risk_level,
            "isolation_forest_score": round(if_score, 4),
            "zscore_score": round(z_score, 4),
            "ema_score": round(ema_score, 4),
            "model_version": self.metadata.get("model_version", "unknown"),
        }
