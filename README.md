# DockerSentinel

**Lightweight, self-hosted SIEM for Linux servers.**

One command. Real-time threat detection. Automated response. No vendor lock-in.

```bash
git clone https://github.com/Guruprasanth-M/DockerSentinel.git
cd DockerSentinel && chmod +x main.sh && ./main.sh
```

Open `http://your-server:8080` — done.

**[Full Documentation →](https://dockersentinel.selfmade.one)**

---

## Why This Exists

I run a simple Linux server — just hosting my site, nothing enterprise.

I wanted to know if someone was brute-forcing my SSH, 
if something weird was running, if an IP was scanning my ports.
Basic stuff. Should be simple.

First I tried Splunk. It is built for companies with dedicated 
security teams. The setup alone requires reading documentation 
for days. Just to monitor one small server — not worth it.

Then I tried Wazuh. Same story. A separate indexer, a separate 
manager, agents to install, Elasticsearch underneath it all, 
4 GB RAM minimum just to start. My server runs my actual site — 
I cannot dedicate all of that just to watch it.

Every tool I found was the same pattern — enterprise software 
built for enterprise teams. Heavy. Complex. Needs to be studied 
before it does anything useful.

But Docker is already on my server. Redis runs in Docker. 
My app runs in Docker. Why can't security monitoring just be 
another `docker compose up`?

So I built it. Everything I needed — logs, processes, network 
connections, ML anomaly scoring, auto IP blocking, real-time 
dashboard — all of it, in containers, one command, on the same 
cheap server that runs my site without it even noticing.

## What it does

- **Monitors** — CPU, memory, disk, network, containers, processes in real-time
- **Detects** — SSH brute force, port scans, privilege escalation, anomalous behavior using ML
- **Responds** — blocks IPs, kills processes, auto-reverses after cooldown
- **Alerts** — Slack, Discord, PagerDuty, any webhook endpoint
- **Zero config** — generates secrets, builds images, starts monitoring

---

## Pipeline

```
Host Logs ──► Collectors ──► Redis Streams ──► ML Engine ──► Policy Engine ──► Actions
                                                   │                              │
                                               Risk Score                   IP Block / Kill
                                                   │                              │
                                            WebSocket (2s)              Webhook Notify
                                                   │
                                              Dashboard
```

Detection to response: **5–12 seconds**

---

## Quick Start

```bash
git clone https://github.com/Guruprasanth-M/DockerSentinel.git
cd DockerSentinel
chmod +x main.sh
./main.sh
```

Requirements: Linux, Docker 24+, 512 MB RAM, port 8080.

---

## Resources

[Documentation](https://dockersentinel.selfmade.one)

---

**DockerSentinel** — because every server deserves a guard that never sleeps.
