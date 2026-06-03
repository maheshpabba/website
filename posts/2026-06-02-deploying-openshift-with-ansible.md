---
title: "Deploying OpenShift with Ansible: A Production-Grade Automation Playbook"
date: 2026-06-02
tags: OpenShift, Ansible, Automation, Kubernetes, Red Hat, Bare Metal, DevOps, Infrastructure
excerpt: Deploying OpenShift on bare-metal servers is a multi-step orchestration problem — authentication, cluster creation, static networking, ISO booting via Redfish, host discovery, and post-install operators. Here's a complete walkthrough of an Ansible automation framework that handles all of it, including idempotent state management and Day-2 operations.
image: assets/images/blog/covers/InfrastrucreAsCode.png
---

# Deploying OpenShift with Ansible: A Production-Grade Automation Playbook

Deploying OpenShift on bare-metal is not a one-click operation.

When I started writing automation for OpenShift deployments on physical servers — Cisco UCS, standalone rack servers, edge nodes — I quickly discovered that the documentation gives you the ingredients but not the recipe. Authentication, cluster object creation, static network configuration generation, ISO mounting over Redfish BMC, polling for host discovery, role assignment, installation triggering, and post-install operator configuration are all separate concerns that have to be choreographed correctly and in the right order, every single time.

The result of that work is a public Ansible playbook framework: [**maheshpabba/ansible-openshift**](https://github.com/maheshpabba/ansible-openshift).

This post walks through the architecture, the design decisions behind it, and how to use it.

---

## The Problem with Manual OpenShift Deployment

The Red Hat Assisted Installer makes OpenShift accessible. But "accessible" means you can do it manually through the web console — it doesn't mean you can do it reliably, repeatedly, and at scale.

The honest picture of a manual deployment looks like this:

- **Authentication has a shelf life.** You exchange an offline token for a short-lived access token at the start. Spend too long on any one step and the token expires — silent failure, cryptic 401 errors, and you're back to step one.
- **NMState configs are per-server, hand-crafted YAML.** One wrong MAC address or subnet prefix and the discovery ISO boots with no network. You won't know until you're 20 minutes into waiting for a host that will never appear.
- **ISO mounting via Redfish is vendor-dependent.** The URL paths differ between Cisco IMC, Dell iDRAC, and HPE iLO. One wrong endpoint and the server boots from its local disk instead of the discovery image. Silent failure again.
- **Host discovery is a waiting game with no feedback.** You poll the Assisted Installer API manually — or stare at the web UI — hoping hosts appear. If one doesn't, you have no immediate way to know whether it's a network issue, a boot order issue, or an ISO mount issue.
- **Role assignment is stateless.** You assign hostnames, roles, and installation disks through API calls or the UI. None of that is saved anywhere portable. Do a fresh deployment next week and you're re-entering it all.
- **Failure at step 8 of 14 means manual triage.** There's no checkpoint system. If installation fails mid-way, you figure out where it broke, clean up orphaned cluster objects, and decide what to redo — from memory.

Do it once and you've learned the path. Do it wrong halfway through and you've burned hours. Need to stand up a second cluster, or rebuild a failed one three weeks later? You're starting from scratch, relying on notes that are probably already stale.

Ansible, with proper state management, solves this.

---

## Repository Structure

The [ansible-openshift](https://github.com/maheshpabba/ansible-openshift) repository is organized as a single-playbook framework with modular task files:

```
ansible-openshift/
├── oshift_role.yml              # Main entry point — switch-controlled workflow
├── oshift_add_role.yml          # Day-2: add worker nodes to existing cluster
├── config.json                  # Your deployment configuration (not committed)
├── config.json.example          # Template to copy and fill in
├── vars/
│   └── defaults.yml             # Default variable values and API endpoints
├── tasks/                       # Modular task files — each owns one concern
│   ├── initialize_state.yml     # Load config, restore saved state
│   ├── authenticate.yml         # Red Hat SSO token exchange
│   ├── create_cluster.yml       # Cluster object creation via API
│   ├── generate_nmstate.yml     # Static NMState config generation per server
│   ├── create_infrastructure_env.yml  # InfraEnv + ISO creation
│   ├── get_iso_url.yml          # Resolve discovery ISO download URL
│   ├── mount_iso.yml            # 3-strategy Redfish ISO mounting
│   ├── discover_hosts.yml       # Poll until hosts appear in Assisted Installer
│   ├── configure_hosts.yml      # Assign roles, hostnames, disks
│   ├── start_installation.yml   # Trigger cluster install
│   ├── configure_operators.yml  # OLM operator post-install config
│   ├── deploy_helm_charts.yml   # Helm chart deployment
│   ├── remove_nodes.yml         # Day-2 node removal
│   └── approve_csrs.yml         # Certificate signing request approval
└── inventory/                   # Generated output directory (gitignored)
    ├── cluster_state.yml        # Saved deployment state for resume
    ├── nmstate.json             # Generated network config
    └── discovery.iso            # Downloaded ISO (if local strategy used)
```

Everything that runs is controlled by switches in `config.json` — boolean flags that tell the playbook which steps to execute or skip. This is the core of what makes the framework resumable and reusable.

---

## Step 1: Configuration

The entire deployment is driven by a single `config.json` file. Copy the example template and fill in your environment:

```bash
cp config.json.example config.json
```

**Minimum required fields:**

```json
{
  "offline_token": "YOUR_RED_HAT_SSO_OFFLINE_TOKEN",
  "pull_secret": "YOUR_OPENSHIFT_PULL_SECRET_JSON",
  "ssh_public_key": "ssh-rsa AAAA... your-key",

  "cluster_name": "ocp-prod-01",
  "openshift_version": "4.20.16",
  "base_dns_domain": "yourdomain.com",
  "cpu_architecture": "x86_64",
  "high_availability_mode": "Full",
  "network_type": "OVNKubernetes",
  "image_type": "full-iso",

  "api_vip": "10.201.9.100",
  "ingress_vip": "10.201.9.101",

  "machine_networks": [{"cidr": "10.201.9.128/25"}],
  "cluster_networks": [{"cidr": "10.128.0.0/14", "host_prefix": 23}],
  "service_networks": [{"cidr": "172.30.0.0/16"}],
  "dns_servers": ["10.201.0.1"]
}
```

**Server definitions** — one entry per physical node:

```json
"servers": [
  {
    "name": "master-0",
    "hostname": "master-0.ocp-prod-01.yourdomain.com",
    "role": "master",
    "management_ip": "10.201.9.152",
    "username": "admin",
    "password": "REDACTED",
    "mac_address": "00:be:75:72:2f:62",
    "installation_disk": "/dev/sda",
    "ip_address": "10.201.9.153",
    "subnet_prefix": 25,
    "gateway": "10.201.9.154",
    "interface_config": {
      "name": "eno1",
      "mac_address": "00:be:75:72:2f:62"
    },
    "redfish": {
      "iso_mount_url": "/redfish/v1/Managers/bmc/VirtualMedia/Slot_2/Actions/VirtualMedia.InsertMedia",
      "iso_status_url": "/redfish/v1/Managers/bmc/VirtualMedia/Slot_2",
      "iso_eject_url": "/redfish/v1/Managers/bmc/VirtualMedia/Slot_2/Actions/VirtualMedia.EjectMedia",
      "power_reset_url": "/redfish/v1/Systems/system/Actions/ComputerSystem.Reset",
      "boot_override_url": "/redfish/v1/Systems/system"
    }
  }
]
```

The `redfish` block tells the playbook how to reach each server's BMC for virtual media operations. The paths vary by vendor and server model — Cisco UCS, Dell iDRAC, HPE iLO, and Supermicro each use slightly different URL structures.

---

## Step 2: Workflow Switches

Every workflow step is individually toggleable. The full switch table from `config.json`:

| Switch | Default | What it does |
|--------|---------|--------------|
| `authenticate` | `true` | Exchange offline token for API access token |
| `calculate_cluster_config` | `false` | Auto-derive HA mode from server count |
| `create_cluster` | `true` | Create cluster object via Assisted Installer API |
| `patch_operators` | `true` | Pre-configure OLM operators during creation |
| `generate_nmstate_config` | `true` | Generate static NMState network configs |
| `create_infrastructure_env` | `true` | Create InfraEnv and generate discovery ISO |
| `create_manifests` | `true` | Apply MachineConfigPool and MachineConfig manifests |
| `prepare_iso` | `true` | Resolve ISO download URL |
| `mount_iso` | `true` | Mount ISO via Redfish on each server |
| `discover_hosts` | `true` | Poll until all hosts appear in Assisted Installer |
| `configure_hosts` | `true` | Assign roles, hostnames, installation disks |
| `install_cluster` | `true` | Trigger installation |
| `post_install_operations` | `false` | Post-install cleanup and operator config |
| `save_state` | `true` | Write state to `inventory/cluster_state.yml` |

This design means you can run the playbook repeatedly without side effects. Already have a cluster created? Set `create_cluster: false`. ISO already mounted? Set `mount_iso: false`. The playbook only runs what you tell it to.

---

## Step 3: Authentication — The Foundation

Every API call to the Red Hat Assisted Installer requires a short-lived bearer token. The `authenticate.yml` task handles the SSO token exchange:

```yaml
- name: "Authenticate with Red Hat SSO"
  uri:
    url: "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token"
    method: POST
    body_format: form-urlencoded
    body:
      grant_type: refresh_token
      client_id: cloud-services
      refresh_token: "{{ cluster_config.offline_token }}"
    timeout: 300
  register: auth_response
```

The `offline_token` from console.redhat.com is long-lived. The access token returned here is valid for ~15 minutes and is passed to every subsequent API call as `Authorization: Bearer {{ access_token }}`.

**Where to get your offline token:** Log in to [console.redhat.com](https://console.redhat.com), go to your profile → Offline Tokens → Generate Token.

---

## Step 4: Cluster Creation

`create_cluster.yml` calls the Assisted Installer API to create the cluster object. Before doing so, it handles a subtle but important concern: if a cluster with the same name already exists, should it be deleted first?

```json
"delete_existing_cluster": true
```

This is controlled by the `delete_existing_cluster` switch. During development and testing you almost always want this set to `true` — otherwise re-runs pile up orphan cluster objects. In production, you likely want it set to `false` so a misconfigured re-run doesn't destroy a running cluster.

The HA mode calculation is also smart. Instead of hardcoding `"Full"` or `"None"` (SNO), you can set `calculate_cluster_config: true` and the playbook derives the mode from your server count:

- 1 server → Single Node OpenShift (`None`)
- 3+ masters → Full HA (`Full`)

---

## Step 5: MachineConfigPool and MachineConfig Manifests

This is a step that most OpenShift deployment guides skip entirely — and then you spend two days figuring out why your GPU nodes are behaving differently from your CPU workers, or why hugepages aren't configured after install.

The `create_manifests.yml` task generates and uploads `MachineConfigPool` (MCP) and `MachineConfig` (MC) manifests to the cluster *before installation begins*, via the Assisted Installer manifests API. Doing this pre-install means the configuration is baked in from first boot — no post-install Day-2 machineconfig rollouts, no node reboots after the fact.

### MachineConfigPool — Node Type Grouping

The playbook reads the `node_labels` from each server in your config and looks for a label key called `aipod.server.profile`. If your servers have different profiles (e.g., `gpu-worker`, `storage-worker`, `standard-worker`), the task generates a separate MCP for each unique type:

```yaml
apiVersion: machineconfiguration.openshift.io/v1
kind: MachineConfigPool
metadata:
  name: gpu-worker
  labels:
    aipod.generated: "true"
    worker.type: "gpu-worker"
spec:
  machineConfigSelector:
    matchExpressions:
      - key: machineconfiguration.openshift.io/role
        operator: In
        values: [worker, gpu-worker]
  nodeSelector:
    matchLabels:
      node-role.kubernetes.io/gpu-worker: ""
  paused: false
  maxUnavailable: 1
```

This lets you apply different `MachineConfig` objects to different node classes independently — rolling kernel config changes to GPU workers without touching storage nodes, for example.

### MachineConfig — Kernel Arguments for AI Workloads

For each worker type, the task generates a `MachineConfig` that sets kernel arguments at boot time:

```yaml
apiVersion: machineconfiguration.openshift.io/v1
kind: MachineConfig
metadata:
  name: 99-gpu-worker-kernel-args
  labels:
    machineconfiguration.openshift.io/role: gpu-worker
spec:
  kernelArguments:
    - module_blacklist=irdma
    - intel_iommu=off
    - amd_iommu=off
    - iommu=off
    - default_hugepagesz=1G
    - hugepagesz=1G
    - hugepages=16
```

Each of these arguments matters for AI and high-performance workloads:

- **`module_blacklist=irdma`** — The iRDMA kernel module conflicts with NVIDIA's RDMA stack on some hardware. Blacklisting it prevents driver conflicts on GPU nodes that use RoCE or InfiniBand for GPU-to-GPU communication.
- **`intel_iommu=off` / `amd_iommu=off` / `iommu=off`** — IOMMU can introduce latency on memory-intensive operations and cause issues with certain GPU direct memory access paths. Disabling it is standard practice for bare-metal GPU nodes where the security tradeoff is acceptable.
- **`hugepages=16` at `1G` size** — Reserves 16 GiB of 1G hugepages at boot. Large language model inference engines and high-throughput data pipelines benefit significantly from hugepages — they reduce TLB pressure and eliminate the overhead of managing thousands of 4K pages for large tensor buffers.

### Pre-Install Upload via Assisted Installer API

Both the MCP and MC manifests are uploaded to the Assisted Installer before installation is triggered, base64-encoded via the cluster manifests API:

```http
POST /api/assisted-install/v2/clusters/{cluster_id}/manifests
{
  "file_name": "machine-config-pool-gpu-worker.yaml",
  "content": "<base64-encoded YAML>"
}
```

The Assisted Installer applies these manifests during bootstrap, so by the time worker nodes complete installation and join the cluster, their MCPs and kernel arguments are already in place. No reboot cycle required after install.

If you don't need differentiated worker pools, the task falls back gracefully — if no servers have an `aipod.server.profile` label, it generates a single MC targeting the default `worker` MCP.

---

## Step 6: Static Networking with NMState

This is where most bare-metal deployments hit their first wall: the discovery ISO boots with DHCP by default, but enterprise environments almost always require static IPs.

The `generate_nmstate.yml` task builds an NMState configuration for each server in your `servers` array and serializes it into the format expected by the Assisted Installer InfraEnv API:

```json
{
  "mac_interface_map": [
    {
      "logical_nic_name": "eth0",
      "mac_address": "00:be:75:72:2f:62"
    }
  ],
  "network_yaml": "interfaces:\n- name: eth0\n  type: ethernet\n  state: up\n  mac-address: 00:be:75:72:2f:62\n  ipv4:\n    enabled: true\n    dhcp: false\n    address:\n    - ip: 10.201.9.153\n      prefix-length: 25\nroutes:\n  config:\n  - destination: 0.0.0.0/0\n    next-hop-address: 10.201.9.154\n    next-hop-interface: eth0\ndns-resolver:\n  config:\n    server:\n    - 10.201.0.1\n"
}
```

This gets embedded into the InfraEnv when it's created, so the discovery ISO that boots on each server already has the correct static IP configuration for that specific machine.

Set `use_static_network: false` in `config.json` to fall back to DHCP if your environment allows it.

---

## Step 7: ISO Mounting via Redfish — 3-Strategy Approach

This is the most operationally interesting part of the playbook. Mounting a virtual media ISO via Redfish is theoretically standardized, but in practice every vendor implements the endpoint slightly differently, and network constraints (proxies, firewalls) can block direct access to Red Hat's CDN URLs.

The `mount_iso.yml` task implements **three progressive strategies**:

### Strategy 1: Direct Red Hat URL

Try mounting the Red Hat-hosted ISO URL directly to the server's BMC virtual media slot. This is the fastest path — no local download required:

```yaml
- name: "Try mounting ISO from Red Hat direct URL"
  uri:
    url: "https://{{ item.management_ip }}{{ item.redfish.iso_mount_url }}"
    method: POST
    body:
      Image: "{{ iso_url }}"
      Inserted: true
    body_format: json
    validate_certs: no
    status_code: [200, 204, 202]
```

### Strategy 2: Locally Hosted ISO

If Strategy 1 fails (BMC can't reach the Red Hat CDN — common in air-gapped or proxy-restricted environments), the playbook falls back to downloading the ISO locally and serving it from a temporary HTTP server that the BMC can reach.

### Strategy 3: Pre-downloaded ISO

If both remote strategies fail, the playbook looks for a locally cached ISO file (`inventory/discovery.iso`) and uses that.

The playbook tracks mount successes and failures per-server and only moves on when all servers (or a configurable minimum) have successfully mounted the ISO.

After mounting, servers are power-cycled via Redfish:

```yaml
- name: "Power cycle server to boot from ISO"
  uri:
    url: "https://{{ item.management_ip }}{{ item.redfish.power_reset_url }}"
    method: POST
    body:
      ResetType: "GracefulRestart"
    body_format: json
    validate_certs: no
```

---

## Step 8: Host Discovery

Once servers boot from the discovery ISO, they register themselves with the Assisted Installer API. The `discover_hosts.yml` task polls the API, checking for hosts reaching the `known` state:

```
Polling discovery status...
  master-0 (10.201.9.153): known ✅
  master-1 (10.201.9.155): discovering...
  master-2 (10.201.9.157): discovering...
  worker-0 (10.201.9.159): known ✅
```

The discovery timeout is configurable (`discovery_timeout: 30` minutes by default). The playbook also implements smart auto-prerequisite detection — if you resume a deployment and the InfraEnv is missing from saved state, it automatically re-enables `create_infrastructure_env` before proceeding to discovery.

---

## Step 9: Host Configuration

Once all hosts are discovered, `configure_hosts.yml` assigns:

- **Hostnames** — from the `hostname` field in your server config
- **Roles** — `master` or `worker`
- **Installation disk** — the device path (`/dev/sda`, `/dev/nvme0n1`, etc.)
- **Node labels** — arbitrary Kubernetes labels applied at install time

```json
"node_labels": {
  "node-type": "master",
  "storage-node": "true"
}
```

The API calls patch each discovered host object with the correct assignments before installation is triggered.

---

## Step 10: Cluster Installation

`install_cluster.yml` sends the `POST /clusters/{cluster_id}/actions/install` request, which kicks off the actual OpenShift installation. At this point the Assisted Installer coordinates bootstrapping across all nodes — the bootstrap node comes up first, the control plane forms, workers join, and operators initialize.

The playbook monitors installation progress via the cluster and host status APIs, logging per-host progress until the cluster reaches `installed` state (typically 60–90 minutes).

---

## State Management — The Resume Architecture

The most operationally significant feature of this framework is **state-aware resume**. Every major step writes its output to `inventory/cluster_state.yml`:

```yaml
cluster_id: "abc123-def456-..."
infra_env_id: "ghi789-jkl012-..."
iso_download_url: "https://api.openshift.com/api/assisted-images/..."
install_status: "installing"
workflow_phase: "installation"
timestamp: "2026-06-02T14:32:00+00:00"
```

When you re-run the playbook, `initialize_state.yml` loads this file first. If `cluster_id` is already set, `create_cluster.yml` skips creation. If `infra_env_id` is set, `create_infrastructure_env.yml` skips. The playbook automatically picks up from where it left off.

**Full retry (state-aware):**
```bash
ansible-playbook oshift_role.yml
```

**Force fresh start:**
```bash
rm inventory/cluster_state.yml inventory/*.json inventory/*.iso
ansible-playbook oshift_role.yml
```

This resume capability is what makes the automation production-viable. A network glitch during host discovery at minute 45 doesn't mean starting over — re-run the playbook and it continues from the discovery polling phase.

---

## Day-2 Operations

The framework includes full Day-2 support for adding and removing worker nodes.

### Adding Worker Nodes

Set `add_nodes: true` in `config.json` and add new server entries with `"operation": "add"`:

```json
{
  "add_nodes": true,
  "servers": [
    {
      "name": "worker-3",
      "operation": "add",
      "hostname": "worker-3.ocp-prod-01.yourdomain.com",
      "role": "worker",
      ...
    }
  ]
}
```

Then run the add-nodes playbook:

```bash
ansible-playbook oshift_add_role.yml
```

This creates a new InfraEnv for the add-nodes operation, mounts the ISO on the new servers, waits for them to be discovered, configures them, and joins them to the existing cluster. The existing cluster is untouched.

### Removing Worker Nodes

Set `remove_nodes: true` and mark servers with `"operation": "remove"`. The `remove_nodes.yml` task gracefully cordons, drains, and deletes the node before removing it from the Assisted Installer.

### CSR Approval

When new nodes join the cluster, Kubernetes issues certificate signing requests that need to be approved. The `approve_csrs.yml` task polls for pending CSRs and approves them automatically:

```yaml
- name: "Approve pending certificate signing requests"
  command: >
    oc adm certificate approve {{ item.metadata.name }}
  loop: "{{ pending_csrs.stdout_lines }}"
```

### Helm Chart Deployment

Set `helm_deploy: true` to trigger post-install Helm chart deployment. Define your charts in `config.json` under `helm_charts` and the `deploy_helm_charts.yml` task handles namespace creation, values templating, and chart installation.

---

## OLM Operator Configuration

The `patch_operators` switch, when enabled during cluster creation, configures which OpenShift Lifecycle Manager operators are installed as part of the cluster installation process:

```json
"olm_operators": [
  {"name": "node-feature-discovery"},
  {"name": "nvidia-gpu"},
  {"name": "lvm"}
]
```

These are passed to the Assisted Installer cluster creation API so the operators are available from day one — no post-install `oc apply` required.

For GPU-bearing clusters (NVIDIA H100, A100), `node-feature-discovery` and `nvidia-gpu` together give you the full GPU Operator stack: device plugin, driver containers, DCGM exporter, and MIG management.

---

## Proxy Support

Enterprise environments often require all external API traffic to go through a corporate proxy. The framework handles this at the variable level:

```json
"proxy": {
  "http_proxy": "http://proxy.company.com:8080",
  "https_proxy": "http://proxy.company.com:8080",
  "no_proxy": "localhost,127.0.0.1,.cluster.local,10.0.0.0/8"
}
```

The playbook uses separate `api_environment` and `redfish_environment` variable dictionaries — proxy is applied to Red Hat API calls but explicitly cleared for Redfish BMC calls, since BMC management IPs are typically on isolated management networks that should not go through the proxy.

---

## Requirements

### Control Node
- Ansible 2.9+
- Python `requests` library

```bash
pip install ansible requests
```

### Red Hat Account
- Valid Red Hat subscription
- Offline token from [console.redhat.com](https://console.redhat.com/openshift/token)
- OpenShift pull secret from [console.redhat.com/openshift/install/pull-secret](https://console.redhat.com/openshift/install/pull-secret)

### Physical Servers
- BMC with Redfish API support (Cisco IMC, Dell iDRAC, HPE iLO, Supermicro IPMI with Redfish)
- Management network access from the Ansible control node to BMC IPs
- Connectivity from BMC network to Red Hat API endpoints (or local ISO hosting)

### Network
- DNS resolution for cluster VIPs and node hostnames
- `api_vip` and `ingress_vip` addresses reserved (not in DHCP pool)

---

## Running It

```bash
# Clone the repo
git clone https://github.com/maheshpabba/ansible-openshift.git
cd ansible-openshift

# Create your config
cp config.json.example config.json
# Edit config.json with your environment details

# Deploy
ansible-playbook oshift_role.yml
```

The playbook runs entirely on `localhost` — there's no Ansible inventory of hosts to manage. All server interaction goes through the Red Hat Assisted Installer API and direct Redfish calls.

---

## What This Looks Like in Practice

A typical fresh deployment run produces output like:

```
TASK [Authenticate with Red Hat SSO]
ok: [localhost] => Red Hat SSO Authentication: SUCCESS

TASK [Create cluster]
ok: [localhost] => Cluster created: ocp-prod-01 (id: abc123...)

TASK [Generate NMState configuration]
ok: [localhost] => Generated static network config for 5 servers

TASK [Create infrastructure environment]
ok: [localhost] => InfraEnv created: ocp-prod-01-infraenv (id: def456...)

TASK [Mount ISO on servers]
ok: [localhost] =>
  STRATEGY 1 - Direct Red Hat URL Results:
  ✅ Successful mounts: 5/5

TASK [Wait for host discovery]
ok: [localhost] =>
  master-0: known ✅
  master-1: known ✅
  master-2: known ✅
  worker-0: known ✅
  worker-1: known ✅

TASK [Configure hosts]
ok: [localhost] => Assigned roles and hostnames to 5 hosts

TASK [Install cluster]
ok: [localhost] => Installation triggered. Monitoring progress...
  [60 minutes later]
  Cluster status: installed ✅
```

---

## Source

The complete playbook framework is open source:

**GitHub:** [maheshpabba/ansible-openshift](https://github.com/maheshpabba/ansible-openshift)

The repo includes `config.json.example` with all fields documented, and the `vars/defaults.yml` file has the full set of tunable parameters (timeouts, retry counts, API endpoints) with their defaults.

If you're deploying OpenShift on Cisco UCS, the Redfish URL patterns in the server config match Cisco IMC's virtual media implementation. For other vendors, adjust the `redfish` block paths for your BMC's specific URL structure.
