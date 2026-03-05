"""Builds feature vectors from event streams."""

from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, Set

import structlog
from redis.asyncio import Redis

from models import FeatureVector

log = structlog.get_logger()


class FeatureBuilder:
    """Aggregates raw events from all collector streams into feature vectors."""

    def __init__(self, window_seconds: int = 5) -> None:
        self.window_seconds = window_seconds
        self._reset_window()

    def _reset_window(self) -> None:
        """Reset all window counters."""
        self.log_total = 0
        self.log_auth_failures = 0
        self.log_sudo_attempts = 0
        self.log_service_restarts = 0

        self.net_new_connections = 0
        self.net_unique_ips: Set[str] = set()
        self.net_port_scan_candidates = 0

        self.proc_new_spawns = 0
        self.proc_cpu_spikes = 0
        self.proc_memory_spikes = 0
        self.proc_unusual_children = 0

    def process_log_event(self, data: Dict) -> None:
        """Process a log event from sentinel:logs stream."""
        self.log_total += 1
        event_type = data.get("type", "")

        if event_type in ("ssh_failure", "auth_failure"):
            self.log_auth_failures += 1
        elif event_type == "sudo_attempt":
            self.log_sudo_attempts += 1
        elif event_type == "service_restart":
            self.log_service_restarts += 1

    def process_network_event(self, data: Dict) -> None:
        """Process a network event from sentinel:network stream."""
        event_type = data.get("type", "")

        if event_type == "new_connection":
            self.net_new_connections += 1
            source_ip = data.get("source_ip", "")
            if source_ip:
                self.net_unique_ips.add(source_ip)
        elif event_type == "port_scan_candidate":
            self.net_port_scan_candidates += 1

    def process_process_event(self, data: Dict) -> None:
        """Process a process event from sentinel:processes stream."""
        event_type = data.get("type", "")

        if event_type == "new_process":
            self.proc_new_spawns += 1

        risk_reason = data.get("risk_reason", "") or ""
        if "High CPU" in risk_reason:
            self.proc_cpu_spikes += 1
        if "High memory" in risk_reason:
            self.proc_memory_spikes += 1
        if "Unusual child" in risk_reason:
            self.proc_unusual_children += 1

    def build_vector(self) -> FeatureVector:
        """Build a feature vector from the current window and reset."""
        vector = FeatureVector(
            timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            window_seconds=self.window_seconds,
            log_total_events=self.log_total,
            log_auth_failures=self.log_auth_failures,
            log_sudo_attempts=self.log_sudo_attempts,
            log_service_restarts=self.log_service_restarts,
            net_new_connections=self.net_new_connections,
            net_unique_ips=len(self.net_unique_ips),
            net_port_scan_candidates=self.net_port_scan_candidates,
            net_bytes_rate=0.0,  # Placeholder for future byte-level tracking
            proc_new_spawns=self.proc_new_spawns,
            proc_cpu_spikes=self.proc_cpu_spikes,
            proc_memory_spikes=self.proc_memory_spikes,
            proc_unusual_children=self.proc_unusual_children,
        )
        self._reset_window()
        return vector


async def run(redis: Redis, window_seconds: int = 5) -> None:
    """Run the feature builder — reads all streams using consumer groups and emits feature vectors.
    
    Uses XREADGROUP instead of XREAD so messages are acknowledged and
    won't be re-processed on restart (BUG-M07 fix).
    """
    builder = FeatureBuilder(window_seconds=window_seconds)
    output_stream = "sentinel:features"
    maxlen = 10000

    consumer_group = "feature_builder"
    consumer_name = f"fb_{os.getpid()}"

    input_streams = ["sentinel:logs", "sentinel:network", "sentinel:processes"]

    # Create consumer groups for each input stream
    for stream in input_streams:
        try:
            await redis.xgroup_create(stream, consumer_group, id="0", mkstream=True)
            log.info("consumer_group_created", stream=stream, group=consumer_group)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise

    log.info("feature_builder_start", window_seconds=window_seconds, consumer=consumer_name)

    # First drain pending messages, then switch to new messages
    pending_streams = {s: "0" for s in input_streams}
    new_streams = {s: ">" for s in input_streams}
    processed_pending = False

    while True:
        try:
            if not processed_pending:
                results = await redis.xreadgroup(
                    consumer_group, consumer_name,
                    streams=pending_streams,
                    count=100,
                    block=int(window_seconds * 1000),
                )
                has_msgs = results and any(msgs for _, msgs in results)
                if not has_msgs:
                    processed_pending = True
                    continue
            else:
                results = await redis.xreadgroup(
                    consumer_group, consumer_name,
                    streams=new_streams,
                    count=100,
                    block=int(window_seconds * 1000),
                )

            if not results:
                continue
            for stream_key, messages in results:
                stream_name_str = stream_key if isinstance(stream_key, str) else stream_key.decode()
                for msg_id, fields in messages:
                    raw = fields.get("data") or fields.get(b"data", b"")
                    if isinstance(raw, bytes):
                        raw = raw.decode()

                    try:
                        data = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        await redis.xack(stream_name_str, consumer_group, msg_id)
                        continue

                    if stream_name_str == "sentinel:logs":
                        builder.process_log_event(data)
                    elif stream_name_str == "sentinel:network":
                        builder.process_network_event(data)
                    elif stream_name_str == "sentinel:processes":
                        builder.process_process_event(data)

                    await redis.xack(stream_name_str, consumer_group, msg_id)

            # Build and emit feature vector
            vector = builder.build_vector()
            await redis.xadd(
                output_stream,
                {"data": vector.model_dump_json()},
                maxlen=maxlen,
                approximate=True,
            )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("feature_builder_error", error=str(e))
            await asyncio.sleep(2)
