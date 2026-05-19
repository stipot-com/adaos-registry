# Infrascope Skill TODO

Status: architectural split planned; detailed projection work pending.

## Done

- [x] Existing skill already has compact/stream concepts for inspector data.
- [x] Current architecture recognizes overview/detail separation as the target.
- [x] Overview rows strip heavy `details` payloads and point to inspector
  streams instead.
- [x] `skill.yaml:data_routes` and `webui.json` declare first-pass stream
  budgets for overview, inventory, operations, and inspector receivers.
- [x] Stream snapshot requests use per-receiver compact builders before falling
  back to the monolithic snapshot cache.

## Next

- [x] Audit which branches are required for the initial shell versus Details.
- [ ] Publish shared status cards for overview, active incidents, inventory,
  runtime/browser state, registry, and operations.
- [x] Replace monolithic cold subscribe snapshot building with per-receiver
  compact builders.
- [ ] Move large inspector payloads to active stream/detail demand.
- [ ] Align object inspectors with the shared projection lifecycle states:
  `pending`, `ready`, `refreshing`, `stale`, `error`.
- [ ] Add byte-size diagnostics for overview sections and each inspector
  projection fragment.
- [ ] Ensure node-aware addressing uses shared helpers rather than skill-local
  path conventions.

## Target

- [x] Infrascope loads a minimal health/overview shell first.
- [ ] Object details are refreshed independently.
- [x] Large topology/log/task-packet data never rides in the always-on widget
  projection.
