# Infrastate Skill TODO

Status: compact snapshot and stream split started; still a pressure test skill.

## Done

- [x] Snapshot projection is compacted before Yjs write.
- [x] Projection is rate-limited and pressure-aware.
- [x] Synchronous projection path waits for completion.
- [x] Temporary fallback `data_projections` are embedded in the handler.
- [x] Data-route plan is declared in `skill.yaml`.
- [x] Stream receivers carry budget, route, and guard visibility metadata.

## Next

- [ ] Continue shrinking the minimal snapshot used by widgets and modal control
  sections.
- [ ] Publish shared status cards for runtime, route/realtime, Yjs, operations,
  core update, marketplace, and skill/scenario registry.
- [ ] Remove `infrastate.operations.active` from Yjs projection after status-card
  coverage and stream resubscribe regression tests are in place.
- [ ] Split remaining stream builders so one receiver snapshot cannot rebuild
  unrelated sections.
- [ ] Add detail stream receivers with per-section freshness/error state.
- [ ] Add cleanup for stale/nested node-prefixed modal ids.
- [ ] Remove local projection executor after the SDK/core sync projection bridge
  exists.
- [ ] Remove fallback projection declaration after runtime packaging preserves
  `skill.yaml`.

## Target

- [ ] Widget projection is small, stable, and always safe for Yjs.
- [ ] Details use streams or targeted projections.
- [ ] Skill remains useful as a guard/pressure canary without being able to
  exhaust the shared doc.
