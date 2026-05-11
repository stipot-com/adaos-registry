# Infra Access Skill TODO

Status: projection completion bridge in place.

## Done

- [x] Snapshot projection uses `ctx_subnet.set_async`.
- [x] Synchronous refresh path waits for projection completion.
- [x] Temporary fallback `data_projections` are embedded in the handler.

## Next

- [ ] Split minimal connection/status summary from detailed token/session/log
  panels.
- [ ] Move high-cardinality or sensitive details behind explicit active demand.
- [ ] Remove local projection executor after the SDK/core sync projection bridge
  exists.
- [ ] Remove fallback projection declaration after runtime packaging preserves
  `skill.yaml`.
- [ ] Add projection miss/error diagnostics visible in the modal.

## Target

- [ ] Modal opens from compact Yjs state.
- [ ] Details are streamed or fetched on explicit demand.
- [ ] Skill never decides hub/member/browser routing.
