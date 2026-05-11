# Subnet Environment TODO

Status: missing projection root diagnosed; stabilization bridge in place.

## Done

- [x] Snapshot builder remains local and side-effect-light.
- [x] Projection now defaults to `desktop` when activation metadata is absent.
- [x] Temporary fallback `data_projections` are embedded in the handler for
  runtime artifacts without `skill.yaml`.

## Next

- [ ] Replace `desktop` fallback with core-supplied active webspace metadata.
- [ ] Move fallback projection declaration back to core-managed `skill.yaml`
  loading after runtime packaging preserves manifests.
- [ ] Add explicit projection diagnostics when `subnet_env.snapshot` has no
  registered target.
- [ ] Split minimal summary from details so the modal can open with a compact
  Yjs projection and fetch large env/drift sections on demand.

## Target

- [ ] Skill exposes a minimal shell projection under
  `data/nodes/<node_id>/subnet_env`.
- [ ] Details are requested only by active UI demand.
- [ ] Skill never hardcodes browser routing or node path layout.
