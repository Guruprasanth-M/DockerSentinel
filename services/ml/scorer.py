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
        """Score using z-score deviation from baseline with significance filtering."""
        if not self.baseline_stats:
            return 0.0

        MIN_RELATIVE_CHANGE = 0.10
        MIN_ABSOLUTE_CHANGE = 0.5
        STD_FLOOR = 0.5

        z_scores = []
        for name in FEATURE_NAMES:
            value = features.get(name, 0.0)
            stats = self.baseline_stats.get(name, {})
            mean = stats.get("mean", 0.0)
            std = stats.get("std", 1.0)

            delta = abs(value - mean)

            # Skip insignificant changes (< 10% of mean or below absolute floor)
            if delta < max(abs(mean) * MIN_RELATIVE_CHANGE, MIN_ABSOLUTE_CHANGE):
                z_scores.append(0.0)
                continue

            effective_std = max(std, STD_FLOOR)
            z_scores.append(delta / effective_std)

        if not z_scores:
            return 0.0

        # Average top-3 z-scores instead of raw max to reduce single-feature noise
        sorted_z = sorted(z_scores, reverse=True)
        top_n = min(3, len(sorted_z))
        avg_top_z = sum(sorted_z[:top_n]) / top_n

        normalized = min(1.0, avg_top_z / 6.0)
        return float(normalized)

    def _ema_score(self, current_score: float) -> float:
        """Apply Exponential Moving Average (EMA) smoothing to detect score drift.
        
        EMA weights recent scores more heavily than older ones, creating a
        smoothed trend line. This prevents single-frame spikes from
        dominating the final score while still catching sustained anomalies.
        
        Formula: ema_t = alpha * score_t + (1 - alpha) * ema_{t-1}
        Alpha = 0.3 means ~30% weight on current score, ~70% on history.
        Window = last 60 scores (~5 minutes at 5-second intervals).
        
        Returns:
            float: Smoothed score clamped to [0.0, 1.0].
        """
        self._ema_scores.append(current_score)

        # Keep last 60 scores (5 minutes at 5-second windows)
        if len(self._ema_scores) > 60:
            self._ema_scores = self._ema_scores[-60:]

        if len(self._ema_scores) < 2:
            return min(1.0, max(0.0, current_score))

        # Calculate EMA iteratively over the window
        ema = self._ema_scores[0]
        for score in self._ema_scores[1:]:
            ema = self._ema_alpha * score + (1 - self._ema_alpha) * ema

        return float(min(1.0, max(0.0, ema)))

    def score(self, features: Dict) -> Dict:
        """Score a feature vector using ensemble of methods.

        Ensemble formula (BUG-M12 documented):
          raw = 0.5 * IF + 0.3 * Z + 0.2 * max(IF, Z)
        
        The max() term is intentionally asymmetric — it amplifies whichever
        detector sees the strongest anomaly. This means a strong IF signal
        or a strong z-score signal alone can push the ensemble higher than
        a simple weighted average would. The 0.2 weight prevents it from
        dominating, but ensures single-method detections aren't diluted.
        
        Final blend: 0.7 * raw + 0.3 * EMA (smoothed trend).
        All intermediate and final scores are clamped to [0.0, 1.0].

        Returns a scoring result dict with individual and combined scores.
        """
        X = self._feature_vector_to_array(features)

        # Individual scores (each returns 0.0–1.0)
        if_score = self._isolation_forest_score(X)
        z_score = self._zscore_score(features)

        # Ensemble: weighted average with max() amplifier for strongest signal
        raw_ensemble = 0.5 * if_score + 0.3 * z_score + 0.2 * max(if_score, z_score)

        # Apply EMA smoothing
        ema_score = self._ema_score(raw_ensemble)

        # Final score: blend of instant and smoothed, clamped to [0.0, 1.0]
        final_score = min(1.0, max(0.0, 0.7 * raw_ensemble + 0.3 * ema_score))

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
