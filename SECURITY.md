# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security-sensitive reports.
Use GitHub's private vulnerability reporting on this repository
(Security → Report a vulnerability). You'll get an acknowledgment within
72 hours. Coordinated disclosure is appreciated; we'll credit reporters in
the fix's release notes unless you ask otherwise.

## Threat model (what this project does and doesn't defend)

ZugaMind is a sidecar that watches untrusted internet sources and, on a
gated decision, invokes an agent harness **you configured** with a briefing
file. The security-relevant properties, and their limits:

- **Fail-closed action gate.** `zugamind/gates/action_gate.py` is the single
  doorway to paid model calls: any erroring check (budget resolution, the
  content screen, model routing) refuses the action. The regex content
  screen catches clear-cut attack phrasings only — it is an acute safety
  net, not an alignment solution.
- **Untrusted input reaches your harness.** Scanner content (GitHub issue
  titles, HN/Reddit titles) is attacker-writable and flows into the wake
  briefing. Treat briefings as untrusted input. The load-bearing defense is
  your harness's own permission model — the shipped Claude Code config
  deliberately does not bypass permission prompts, and every shipped
  harness config is `enabled: false` until you flip it.
- **The actuator only runs commands you wrote.** It substitutes a briefing
  file path into a configured argv; it cannot invent commands. Rate limits
  (per-hour and per-day) are counted from a durable journal and refuse the
  wake if that journal is unreadable.
- **Hard budget cap.** A monthly USD ceiling bounds paid-call spend; the
  free local tier is never budget-gated.

Out of scope: sandboxing the harness you configure, the security of the
harness itself, and prompt-injection-proofing an LLM — see the Safety
design and Limitations sections of the README for the honest boundary.

## Supported versions

Pre-1.0: only the latest release / `main` receives security fixes.
