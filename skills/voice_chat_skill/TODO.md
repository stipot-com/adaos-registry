# Voice Chat Skill TODO

Status: projected history path stabilized; core/router ownership still needs
cleanup.

## Done

- [x] Voice history is projected through `voice_chat.state`.
- [x] Temporary fallback `data_projections` are embedded in the handler.
- [x] Handler ensures projection rules before publishing local projected state.

## Next

- [ ] Decide final ownership between RouterService appends and skill appends so
  history is not duplicated or split.
- [ ] Remove handler-side fallback projection declaration after core projection
  loading is guaranteed.
- [ ] Add visible diagnostics when user text is accepted but no projected
  history update is observed.
- [ ] Keep chat history compact and bounded by message count and byte budget.

## Target

- [ ] Router owns transport and event normalization.
- [ ] Skill owns domain response generation.
- [ ] ProjectionService owns Yjs publication and node/webspace addressing.
