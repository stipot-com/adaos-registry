# Mediaserver Skill TODO

Status: target alignment planned; not yet fully converted to projected/stream
contract.

## Done

- [x] Skill is identified as a future example for Yjs-projected minimal state
  plus detailed stream data.

## Next

- [ ] Define the minimal durable media summary for Yjs:
  availability, file count, live peers, active tracks, and last error.
- [ ] Move file lists, upload diagnostics, playback details, and live-session
  internals behind details/stream demand.
- [ ] Publish media readiness in the same projection lifecycle vocabulary as
  other skills.
- [ ] Add byte and event counters for stream payloads.

## Target

- [ ] Browser shell can show media health from compact Yjs state.
- [ ] Detailed media panels use streams or explicit API demand.
- [ ] Skill does not duplicate transport health logic owned by core.
