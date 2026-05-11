# Infrascope Skill TODO

Status: architectural split planned; detailed projection work pending.

## Done

- [x] Existing skill already has compact/stream concepts for inspector data.
- [x] Current architecture recognizes overview/detail separation as the target.

## Next

- [ ] Audit which branches are required for the initial shell versus Details.
- [ ] Move large inspector payloads to active stream/detail demand.
- [ ] Align object inspectors with the shared projection lifecycle states:
  `pending`, `ready`, `refreshing`, `stale`, `error`.
- [ ] Add byte-size diagnostics for each inspector projection fragment.
- [ ] Ensure node-aware addressing uses shared helpers rather than skill-local
  path conventions.

## Target

- [ ] Infrascope loads a minimal health/overview shell first.
- [ ] Object details are refreshed independently.
- [ ] Large topology/log/task-packet data never rides in the always-on widget
  projection.
