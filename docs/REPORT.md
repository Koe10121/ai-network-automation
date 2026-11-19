# AI-Driven Network Configuration and Automation Using OSPF

Practical lab report. Student name: Thant Zin Soe  Student ID: 6631501147  Date: 17.11.2026

## 1. Summary

This project reads a network diagram (PNG/JPG), uses an image-capable AI model to extract the
topology and addressing as JSON, generates Cisco IOS interface and OSPFv2 configuration for two routers,
validates it, waits for an explicit administrator approval, queues one job per router in Kafka, deploys
with a Python worker over SSH (Paramiko), verifies that OSPF really works, and shows every job and its
state in a web page. The lab ended with PC1 (10.0.0.10) successfully pinging PC2 (10.2.0.10) through R1 and R2.

## 2. Topology and addressing

```
PC1 10.0.0.10/24 ---- R1 (1.1.1.1) ---- R2 (2.2.2.2) ---- PC2 10.2.0.10/24
     gw 10.0.0.1     f0/0      f1/0   f0/0      f1/0     gw 10.2.0.2
                       10.0.0.1  10.1.0.1  10.1.0.2  10.2.0.2
```

| Network      | Between        | R1 interface        | R2 interface        |
|--------------|----------------|---------------------|---------------------|
| 10.0.0.0/24  | PC1 and R1     | FastEthernet0/0 .1  |                     |
| 10.1.0.0/24  | R1 and R2      | FastEthernet1/0 .1  | FastEthernet0/0 .2  |
| 10.2.0.0/24  | R2 and PC2     |                     | FastEthernet1/0 .2  |

OSPFv2 process 1, area 0. Router IDs 1.1.1.1 (R1) and 2.2.2.2 (R2). The two LAN-facing interfaces
(R1 f0/0, R2 f1/0) are passive. No static routes are used.

Management access (outside the OSPF design): both routers have FastEthernet1/1 connected to a GNS3 switch and Cloud
node, on 192.168.217.51 (R1) and 192.168.217.52 (R2), so the worker can reach them over SSH.

## 3. Architecture

```
 Browser (frontend/index.html)
        |  upload, Generate, Review, Approve, Deploy
        v
 FastAPI backend (backend/app.py)
   - analysis.py   image -> JSON plan (Gemini, no router access)
   - planning.py   cleans up the plan (port labels on link ends)
   - validator.py  checks addresses, IDs, links, management hosts
   - generator.py  Cisco IOS config from the plan + configs/devices.yaml
   - dispatcher.py approved job -> Kafka topic network-config-jobs
        |
        v
 Kafka (docker-compose.yml, topic network-config-jobs, one message per device)
        |
        v
 Worker (worker/worker.py)
   1. claims the device job from the backend (blocks duplicates)
   2. re-checks that the job is approved and fetches the config
   3. deployer.py: backup, push config over SSH (Paramiko), write memory
   4. reports SUCCESS or FAILED with output to the backend
   5. verifier.py: after the last router, runs the OSPF checks
        |
        v
 R1 and R2 (GNS3, Cisco 7200, IOS 15.2)
```

| Part | File |
|------|------|
| Web UI | `frontend/index.html` |
| API, job store, approval, state machine | `backend/app.py` |
| AI image analysis | `backend/analysis.py` |
| Config generation | `backend/generator.py` |
| Validation | `backend/validator.py` |
| Kafka producer | `backend/dispatcher.py` |
| Kafka worker | `worker/worker.py` |
| SSH deployment | `worker/deployer.py` |
| OSPF verification | `worker/verifier.py` |
| Router specifics (interfaces, IDs, management IPs) | `configs/devices.yaml` |

## 4. Workflow and job states

1. Upload a PNG/JPG (other types are rejected).
2. **Generate**: the AI extracts the topology as JSON. It must flag anything unclear in an `ambiguities`
   list instead of guessing. The backend validates the plan and generates the configs. Status is `GENERATED`,
   or `INVALID` if there are errors (invalid jobs cannot be approved).
3. **Review**: the page shows validation errors, the AI warnings, the extracted JSON and the configs.
4. **Approve**: an administrator confirms. Nothing is sent to Kafka before this.
5. **Deploy**: one Kafka message per router (job ID, device ID, config reference).
6. The worker deploys and reports. Each device goes `PENDING` -> `RUNNING` -> `SUCCESS` or `FAILED`,
   with timestamps and the router output. The job is `SUCCESS` only if every device succeeded.
7. OSPF verification runs and the job gets `verification: PASSED`, `FAILED` or `SKIPPED`
   (skipped when a deployment failed).

## 5. Security controls

- No passwords, API keys or private keys in code or configs. They come from `.env` (git-ignored); the repository holds only `.env.example`.
- The AI never connects to a router. It only reads the image. Only the worker uses SSH.
- Nothing is deployed without approval. The backend checks approval when sending the job and again when the worker fetches the config; configs of an unapproved job return 403.
- Duplicate protection: a device job can be claimed only once; a second worker or a replayed Kafka message gets 409 and is skipped.
- Safety filter before any line is sent: the deployer refuses lines that could lock out management SSH or wipe the router:
  `no ip ssh`, `crypto key ...`, `line ...`, `transport ...`, `username ...`, `enable ...`, `no ip address`, `shutdown`,
  `no interface`, `no router ospf`, and `reload`, `erase`, `delete`, `format`, `write erase`.
  It also refuses any `ip route` (the exam forbids static routes), and refuses to touch the interface named in
  `MGMT_INTERFACES` (FastEthernet1/1). The whole config is checked before the first line is sent.
- SSH host keys are remembered on first use (`worker/known_hosts`) and checked afterwards. SSH agent and key search are off.
- Upload limits: only PNG/JPEG is accepted, and the file is stored under a random name.

## 6. Failure handling and recovery

- Before changing a router, the worker saves its running-config to `backups/` (name includes device and job).
- If the router rejects a line (`%` error), the worker leaves configuration mode, marks the device `FAILED`, and the
  message says which earlier lines are active but NOT saved to startup-config, and where the backup is.
  `write memory` runs only after every line was accepted.
- Wrong password, unreachable or hung router, and SSH timeouts become `FAILED` with a plain message. The device is never left in `RUNNING`.
- If the backend is down, the worker retries instead of dropping the result.
- If Kafka is unavailable when deploying, the job returns to `APPROVED` and the page shows an error.
- Recovery: re-run Deploy after fixing the cause, or restore from the backup file in `backups/`. If a router is reachable only through
  SSH and a bad line was sent, the console in GNS3 remains available as a recovery path.
- `DRY_RUN=1` runs the whole flow without touching the routers.

## 7. OSPF verification

After the last router succeeds, the worker logs in to each router again and runs `show ip ospf neighbor`, `show ip route ospf` and
`show ip protocols`, retrying every 10 seconds for up to 2 minutes. For each router it checks:

1. OSPF process 1 is running.
2. The router ID is correct.
3. Both networks are advertised in area 0.
4. The LAN interface is passive and the router-to-router interface is not.
5. A FULL adjacency exists with the neighbor router ID.
6. The remote LAN is learned through OSPF.

The raw show output is stored with the job as evidence.

## 8. Test evidence

### 8.1 Real lab run (GNS3, 14 November 2026)

| Item | Result |
|------|--------|
| Job ID | 105e86bdd20a4afca35a6b14e1cd1b9b |
| Validation | 0 errors (4 AI warnings: interface names, gateways, router IDs, mask note) |
| Approved | 2026-11-14 17:45:37 UTC |
| R1 | SUCCESS, Kafka partition 0 offset 2, started 17:46:17, finished 17:46:23 |
| R2 | SUCCESS, Kafka partition 0 offset 3, started 17:46:23, finished 17:46:28 |
| OSPF verification | PASSED. R1: 8 of 8 checks (after 6 attempts while adjacency formed); R2: 8 of 8 checks |
| Ping | PC1 to 10.2.0.10: 5 of 5 replies, TTL 62 (two router hops) |

Screenshots in `docs/screenshots/`:

| File | Evidence |
|------|----------|
| `01-ai-plan-json.png` | **AI analysis**: the extracted plan as JSON (devices, links, networks, addresses) and the AI's warnings |
| `02-ai-ambiguities-and-configs.png` | The `ambiguities` the AI flagged instead of guessing, and the proposed IOS/OSPF configs for R1 and R2 |
| `03-approve-confirmation.png` | **Explicit administrator approval** before deployment |
| `04-job-approved.png` | The job recorded as APPROVED |
| `05-kafka-container.png` | Kafka running in Docker (`netauto-kafka`, port 9092) |
| `06-kafka-messages.png` | **Kafka jobs**: one message per router with job ID, device ID and config reference |
| `07-job-running-pending.png` | **Job monitoring**: R1 RUNNING and R2 PENDING, with queue and start timestamps |
| `08-job-success-ospf-passed.png` | **Deployment status**: job SUCCESS with OSPF verification PASSED |
| `09-r1-ospf.png` | R1: neighbor 2.2.2.2 FULL, route to 10.2.0.0/24 learned via OSPF, `show ip protocols` |
| `10-r2-ospf.png` | R2: neighbor 1.1.1.1 FULL, route to 10.0.0.0/24 learned via OSPF, `show ip protocols` |
| `11-ping-pc1-pc2.png` | **PC1 pings 10.2.0.10** with replies (TTL 62: two router hops) |
| `12-gns3-topology.png` | The GNS3 lab, including the management switch and Cloud used for SSH access |

Notes: the lab was deployed twice from the same diagram with the same result. Job `105e86bd...` is the first run
(screenshots 06 and 08) and job `e87e0470...` is the second run (screenshots 03, 04 and 07); both ended SUCCESS with OSPF
verification PASSED. Screenshots 01 and 02 come from a third job that was generated and reviewed but deliberately not deployed.

### 8.2 Automated checks (`tests/`)

These run against fake Cisco SSH servers (`tests/fake_ios.py`) and a throwaway Kafka topic, so they never touch the real routers.
Run them from the project folder, for example `python tests\check_plan.py`.

| Script | What it covers | Result |
|--------|----------------|--------|
| `check_plan.py` | Plan cleanup and validation, including diagrams without IP addresses | 15 passed |
| `check_ssh_deploy.py` | SSH deploy: wrong password, hung router, rejected line, blocked lines, backups, legacy SSH algorithms | 32 passed |
| `check_ospf_verify.py` | OSPF checks: stuck INIT/2WAY, wrong router ID, wrong area, missing route, passive mistakes, timeouts | 32 passed |
| `check_worker.py` | Worker: duplicates, unapproved jobs, malformed messages, backend outages, restart without double deployment | 14 passed |
| `check_end_to_end.py` | Backend + Kafka + worker + SSH + verification with two fake routers | 18 passed |

## 9. How to run

See `README.md`. In short: start Kafka (`docker compose up -d`), start the backend
(`python -m uvicorn backend.app:app --port 8000`), start the worker (`python -m worker.worker`), open `http://127.0.0.1:8000`.

## 10. Limitations

- The AI can misread a diagram. That is why the plan is validated, ambiguities are shown, and a human approves before anything is deployed.
- Interface names, router IDs and management IPs are not on the diagram, so they come from `configs/devices.yaml`.
- Verification and deployment were tested on a two-router GNS3 lab with old Cisco IOS 15.2. Paramiko is pinned to 3.x because
  newer releases dropped the SHA-1 algorithms that this IOS needs.
- The job store is a JSON file (`jobs.json`), which is enough for one backend process, not for a multi-server production system.

## 11. Use of AI assistance

I used Claude Code (Anthropic) to help build and test this project, and the Gemini API for the diagram analysis. I reviewed, ran and tested the system on my own GNS3 lab.