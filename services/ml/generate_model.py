"""Generates the bundled Isolation Forest model from synthetic data."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

# Model output directory (inside container during build, mounted volume at runtime)
MODEL_DIR = os.environ.get("MODEL_DIR", "/app/pretrained")

# Feature names (must match FeatureVector in collectors/models.py)
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


def generate_normal_data(n_samples: int = 10000) -> np.ndarray:
    """Generate synthetic data representing normal server behavior."""
    rng = np.random.RandomState(42)

    data = np.zeros((n_samples, len(FEATURE_NAMES)))

    # log_total_events: 0-20 per 5 seconds (normal)
    data[:, 0] = rng.poisson(5, n_samples)

    # log_auth_failures: 0-2 per 5 seconds (occasional login attempts)
    data[:, 1] = rng.poisson(0.3, n_samples)

    # log_sudo_attempts: 0-1 per 5 seconds
    data[:, 2] = rng.poisson(0.1, n_samples)

    # log_service_restarts: very rare
    data[:, 3] = rng.poisson(0.01, n_samples)

    # net_new_connections: 0-10 per 5 seconds
    data[:, 4] = rng.poisson(3, n_samples)

    # net_unique_ips: 0-5 per 5 seconds
    data[:, 5] = rng.poisson(2, n_samples)

    # net_port_scan_candidates: 0 normally
    data[:, 6] = np.zeros(n_samples)

    # net_bytes_rate: 0-100000 bytes/s
    data[:, 7] = rng.exponential(10000, n_samples)

    # proc_new_spawns: 0-5 per 5 seconds
    data[:, 8] = rng.poisson(1, n_samples)

    # proc_cpu_spikes: 0 normally
    data[:, 9] = rng.poisson(0.05, n_samples)

    # proc_memory_spikes: 0 normally
    data[:, 10] = rng.poisson(0.02, n_samples)

    # proc_unusual_children: 0 normally
    data[:, 11] = np.zeros(n_samples)

    return data


def main() -> None:
    """Generate and save the pre-trained model."""
    os.makedirs(MODEL_DIR, exist_ok=True)

    print("Generating synthetic training data...")
    X_train = generate_normal_data(10000)

    print("Fitting StandardScaler...")
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X_train)

    print("Training Isolation Forest...")
    model = IsolationForest(
        n_estimators=100,
        contamination=0.05,  # Expect ~5% anomalies
        max_samples="auto",
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X_scaled)

    # Compute baseline statistics
    means = X_train.mean(axis=0).tolist()
    stds = X_train.std(axis=0).tolist()
    baseline_stats = {
        FEATURE_NAMES[i]: {"mean": means[i], "std": stds[i]}
        for i in range(len(FEATURE_NAMES))
    }

    # Save model
    model_path = os.path.join(MODEL_DIR, "isolation_forest_v1.pkl")
    joblib.dump(model, model_path)
    print(f"Model saved: {model_path}")

    # Save scaler
    scaler_path = os.path.join(MODEL_DIR, "scaler_v1.pkl")
    joblib.dump(scaler, scaler_path)
    print(f"Scaler saved: {scaler_path}")

    # Save metadata
    metadata = {
        "model_version": "v1_pretrained",
        "algorithm": "IsolationForest",
        "n_estimators": 100,
        "contamination": 0.05,
        "training_samples": 10000,
        "features": FEATURE_NAMES,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "author": "Guruprasanth M",
    }
    metadata_path = os.path.join(MODEL_DIR, "model_metadata.json")
    with open(metadata_path, "w") as f:
        json.dump(metadata, f, indent=2)
    print(f"Metadata saved: {metadata_path}")

    # Save baseline stats
    stats_path = os.path.join(MODEL_DIR, "baseline_stats.json")
    with open(stats_path, "w") as f:
        json.dump(baseline_stats, f, indent=2)
    print(f"Baseline stats saved: {stats_path}")

    print("Pre-trained model generation complete.")


if __name__ == "__main__":
    main()
