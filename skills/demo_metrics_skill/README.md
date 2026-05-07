## demo_metrics_skill

`demo_metrics_skill` is the first neutral browser-demo skill for the semantic
Web UI migration.

Its role is not to be a product feature.
Its role is to provide one compact, reusable validation surface for:

- table-like semantic views
- chart-like semantic views
- shared selection state
- Yjs-backed shared browser state
- stream-backed live event tails
- typed semantic browser actions

The current `webui.json` keeps one compatibility-era browser modal so the skill
can coexist with `webui.v1`.
Alongside that compatibility surface, the same file also carries a semantic
draft block intended for the next browser/runtime migration step.

Recommended paired scenario:

- `taiga_ui_demo_scenario`

Recommended semantic kinds exercised here:

- `collection_grid`
- `metric_chart`
- `event_log`

