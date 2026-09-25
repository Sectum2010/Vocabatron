# Private local deployment

The templates are review material, not an installer. Render concrete paths into
the private deployment directory, review the exact changes, and obtain the
owner's explicit approval before installing or changing system services or
Tailscale Serve. Do not reset Serve or enable Funnel.

The web process binds to the configured loopback endpoint. Add only the approved
HTTPS application path to the existing Serve configuration. Preserve other
paths, ports and services. Identity headers are accepted only from the trusted
local proxy boundary; all authorized logins map to one application owner.
Processes with local access to loopback, including the machine administrator,
are outside this remote-identity threat boundary.

The service mount namespace explicitly hides system Docker/containerd sockets
and the selected user's rootless Docker socket. This prevents an existing Docker
group membership from granting the app access to those local control sockets.
Verify the effective inaccessible paths after installation.

Use a root-level `vocabatron.slice`: its idle CPU weight applies relative to
other top-level workloads. Both services also use SCHED_IDLE, Nice 19 and idle
I/O. The slice has no fixed CPU quota or permanent block-device throughput
limit. MemoryHigh is 32 GiB, MemoryMax 48 GiB, and swap is disabled. The worker
uses 30/46 GiB soft/hard limits; application admission stays below 44 GiB and
reserves at least 24 GiB of host available memory. These are ceilings, not
reservations. Document subprocesses separately enforce a 2.5 GiB resident-tree
limit; their 12 GiB virtual-address limit allows CPU model mappings without
reserving that much physical memory. The worker admits work
only after a stable idle window; existing process and cgroup pressure is still
checked during work. Check actual `cpu.idle`, `cpu.max`, `memory.*`, `io.max`,
parent hierarchy and process scheduling after deployment. I/O weights depend on
the active storage scheduler; configured weights alone do not prove isolation.

The dynamic CPU budget retains two spare host cores and admits at most eight
threads. Choose the default per-search count from bounded measurements on the
deployment host. CPU PSI alone is not evidence of an external competitor:
idle-priority application threads also contribute stalls. Stop new work on stale
telemetry or elevated I/O pressure; finish/checkpoint the current bounded unit.
Memory pressure and explicit holds can stop the application's own process tree.
See [offline document setup](../docs/document-recovery.md) before enabling scans.

Frontend builds write to `.cache/frontend-dist` by default. After the final
regression, copy immutable hashed assets first, then atomically replace the
small shell files in the published static root. Retain previous hashed assets
for clients still using the prior service worker. Never use a development build
to overwrite the live directory before release gates pass.

Shared model inference runs outside these application cgroups. Client priority
cannot preempt an in-flight GPU operation. The worker uses conservative memory
and activity admission, checks between bounded calls, closes its own request on
pressure and waits for the shared service to become quiet. The CLI `hold`,
`hold-status` and `release-hold` commands coordinate external inference. Wait for
acknowledgement; a closed socket alone is not proof of released GPU work.

Before first generation, migrate all known real result roots using their source
hashes, snapshots, original model evidence and independent PDF validation.
Keep unresolved migration records visible; never claim complete historical
deduplication while relevant legacy records remain unverified.

Backups use SQLite's online backup API plus immutable referenced objects and
model evidence. Restore into a new private directory and verify hashes, database
integrity and reference closure before any cutover. Same-disk backups protect
against accidental deletion, not device failure.

Rollback: stop and disable only these two units; remove only the added Serve
path; restore the exact prior unit files if any were replaced, then reload
systemd. Keep the new private archive and original legacy records intact. Do
not reset Serve, remove other routes or delete data during rollback. A reboot
requires separate permission.
