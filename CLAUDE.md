# CLAUDE.md

# Project

ubuntu_infra_service

Primary Target:

lab_ccp

---

# Vision

Transform CCP from:

ClusterShell + Ansible Web UI

into:

AI / HPC Cluster Lifecycle Management Platform

The goal is not to become another AWX.

The goal is to become:

PXE + Discovery + Inventory + Cluster + Slurm + Monitoring

for AI/HPC Labs.

---

# Architecture Strategy

Backend Capability Preservation.

Frontend and Workflow Redesign Allowed.

The current UI is not a constraint.

If a better workflow requires redesigning pages,
navigation, forms, inventories or dashboards,
redesign them.

Focus on operator productivity.

---

# Must Preserve

The following capabilities are valuable and should survive redesign:

- Authentication
- RBAC
- Audit Logging
- Job History
- ClusterShell Execution
- Ansible Execution

These are platform foundations.

Reuse these capabilities whenever practical.

---

# Can Be Redesigned

The following may be redesigned if necessary:

- UI Layout
- Navigation
- Node Inventory
- Database Schema
- Cluster Models
- Discovery Workflow
- Host Management
- Dashboard Structure
- API Layout

Backward compatibility is preferred but not mandatory.

Migration scripts are acceptable.

---

# Primary Problem

Current CCP behaves like:

Node List
+
Run Command
+
Run Playbook

This provides limited value once large numbers of nodes exist.

The new focus should be lifecycle management.

---

# Node Lifecycle

Target flow:

Discover
→ Import
→ Authenticate
→ Bootstrap
→ Discover Hardware
→ Organize
→ Cluster Assignment
→ Operate

---

# P0 Critical Bug

Current Node onboarding allows nodes to exist without valid credentials.

This creates unusable inventory records.

A node should never be considered managed until:

1. Authentication succeeds.
2. SSH key deployment succeeds.
3. Command execution succeeds.

This issue has highest priority.

---

# Discovery First

Discovery should drive inventory creation.

Primary source:

DHCP leases

Examples:

- dnsmasq lease database
- DHCP lease records

Discovery should identify:

- IP
- MAC
- Hostname

before import.

Manual node creation should become optional.

---

# Authentication Model

Node onboarding only requires:

- Username
- Password

Rack information should not be entered manually.

---

# Hostname Driven Topology

Hostnames become topology metadata.

Examples:

rack0_sled1_gpu
rack0_sled2_gpu

rack1_sled1_cpu
rack1_sled2_cpu

Automatically derive:

rack
sled
role

from hostname.

No separate rack database should be required.

---

# Hostname Management

One-click hostname update.

CCP executes:

hostnamectl set-hostname

and updates:

- /etc/hostname
- /etc/hosts

Changes should be visible immediately.

---

# Hardware Discovery

Collect:

CPU
Memory
Storage
Network
GPU
Operating System

Optional:

Infiniband

Store discovery metadata for future cluster generation.

---

# Cluster Model

Introduce first-class Cluster objects.

Hierarchy:

Cluster
    Nodes

Examples:

AI Training Cluster

CPU Cluster

Inference Cluster

Validation Cluster

---

# Rack Visualization

Generate automatically from hostname parsing.

No manual rack drawing.

Support:

Rack View
Node Health
Node Status

---

# Ansible Philosophy

CCP is not a development environment.

Do NOT implement:

- Playbook Editors
- Role Editors

Development occurs outside CCP.

Examples:

VSCode
Local Filesystem

---

# Ansible Sources

Support local filesystem paths.

Examples:

/root/rex/ansible_db

/opt/slurm_ansible

/root/playbooks

Scan:

- playbooks
- roles
- inventories
- host_vars
- group_vars

and execute using existing Ansible Runner.

---

# Slurm Is Strategic

Slurm support is a primary platform feature.

Target users:

AI Labs
Research Labs
Universities
GPU Clusters
HPC Environments

---

# Slurm Cluster Builder

Generate clusters from discovered nodes.

Auto-generate:

- slurm.conf
- gres.conf

using discovered hardware.

Minimize manual configuration.

---

# Slurm Lifecycle

Support:

INIT
DISCOVER
DEPLOY
VALIDATE
BENCHMARK
REPORT
MONITOR
CLEANUP

through guided workflows.

Reuse existing Ansible execution framework.

---

# Benchmark Framework

Support automation of:

CPU
Memory
Disk
GPU
Network

Node-to-node network testing must be supported.

Loopback-only tests are insufficient.

---

# Monitoring

Provide:

Node Monitoring

Cluster Monitoring

Slurm Monitoring

GPU Monitoring

Health Dashboards

Capacity Dashboards

Cluster Status Views

---

# Recommended New Navigation

Dashboard

Discovery

Nodes

Clusters

Rack View

Ansible

ClusterShell

Slurm

Benchmark

Monitoring

Reports

Administration

---

# UX Goals

Discovery First

Automation First

Minimal Configuration

Operator Friendly

Lab Friendly

GPU Cluster Friendly

Avoid AWX complexity.

Avoid unnecessary configuration pages.

---

# Development Priorities

P0

- Node Credential Fix
- Discovery
- SSH Bootstrap
- Hostname Management
- Hardware Discovery

P1

- Cluster Model
- Rack View
- Local Path Ansible Sources

P2

- Slurm Builder
- Slurm Lifecycle

P3

- Benchmark
- Monitoring
- Reporting

Always deliver value in small incremental steps.
