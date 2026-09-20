# Web/PWA delivery matrix

This document contains only public implementation details and synthetic evidence. Deployment addresses, authorized identities, real documents and measurements belong in private records.

| Milestone | Scope | Required evidence | Status |
|---|---|---|---|
| A | Protected baseline, explicit paths, content/family identity, compatible migration | stable identities, immutable provenance, online backup and isolated restore checks | Implemented; protected-file evidence remains private |
| B | Multiple documents and lessons, library, immutable clue preparation | synthetic merged lessons, same-page boundaries, continuations, positioned heading spaces, independent failures, duplicate imports | Automated checks passed; a real whole book still requires material |
| C | Arbitrary counts, historical uniqueness, projected enumeration, exports | requests for 1, 2, 3 and 5; independent Cartesian enumeration of tiny zero/one/multiple-solution domains; interrupted publication and zero-compute restore | Automated checks passed; no claim of exhaustive enumeration for a real lesson |
| D | SQLite queue, fenced attempts, resource admission and yielding | finite-work fairness, stale/busy admission, reservations, owned-process yielding, explicit hold with a small synthetic CPU load, scheduler termination and restart | Automated, isolated process and deployed-service recovery checks passed; effective limits and measurements remain in private release evidence |
| E | English React PWA, authenticated API, local PDF.js | 12 Chromium/Firefox/WebKit checks, desktop/mobile screenshots, authentication and CSRF, private cache exclusion, PDF preview/download and mocked share cancellation/fallback | Browser automation passed; native installation and sharing on physical devices remain unverified |
| F | Approved local systemd and private HTTPS deployment | effective limits, service recovery, final full tests and real acceptance | Approved local deployment verified, including real HTTPS, effective limits, process isolation, browser-close persistence and service recovery. Final suite and deployed-state evidence are release-specific private records; whole-machine reboot and physical-device tests remain unverified |

Production pages and all application messages are English. Communication with the owner remains Chinese. System deployment requires an exact, separately approved local change plan. Existing remote endpoints and unrelated applications must remain untouched.

The full final-source suite is a release gate; focused checks do not replace it.
Run the guarded commands documented in the root README. The browser harness
starts a temporary loopback fixture using invented materials and stops it on
exit. WebKit automation is not physical iOS acceptance. The isolated process
check uses an explicit external hold and a small owned CPU process; it does not
claim to prove zero interference on shared hardware or GPU preemption.

Public tests cover synthetic inputs only. Real document fingerprints, historical
migration details, screenshots containing real data, resource measurements,
output locations, deployment configuration and rollback records remain in the
ignored private release directory. A missing real book, unapproved deployment,
unperformed whole-machine reboot and untested physical devices must be reported
as pending rather than inferred from synthetic tests or configuration files.
