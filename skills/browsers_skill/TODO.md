# Browsers Skill TODO

Status: stabilization bridge in place.

## Done

- [x] Action response is lightweight and returns a short `ok`/summary envelope.
- [x] Browser inventory can be delivered through stream receivers.
- [x] Desktop Browsers button opens the node-aware `browsers_modal`.
- [x] Projection publish path waits for completion instead of fire-and-forget.
- [x] Temporary fallback `data_projections` are embedded in the handler.

## Next

- [ ] Keep selected device/group UI state stable while stream fragments update.
- [ ] Split group list, selected device details, current browser summary, and
  actions into independently refreshed stream fragments.
- [ ] Move fallback projection declaration back to core-managed `skill.yaml`
  loading after runtime packaging preserves manifests.
- [ ] Remove local projection executor after the SDK/core sync projection bridge
  exists.
- [ ] Add stream diagnostics: receiver, webspace, node_id, payload size,
  publish count, and last error.

## Target

- [ ] Skill publishes compact durable Yjs summaries for reconnect.
- [ ] Skill uses streams for detailed active panels.
- [ ] Skill does not know whether the consumer is hub-local, member-routed, or
  browser-attached.
