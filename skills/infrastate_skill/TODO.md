# Infrastate Skill TODO

Status: compact snapshot and stream split started; still a pressure test skill.

## Done

- [x] Snapshot projection is compacted before Yjs write.
- [x] Projection is rate-limited and pressure-aware.
- [x] Synchronous projection path waits for completion.
- [x] Temporary fallback `data_projections` are embedded in the handler.

## Next

- [ ] Continue shrinking the minimal snapshot used by widgets.
- [ ] Move logs, skill lists, scenario lists, and long diagnostics behind
  explicit Details demand.
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
