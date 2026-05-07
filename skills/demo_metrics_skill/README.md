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

Current implementation status:

- semantic desktop and modal schemas are adapted into current browser widgets
- `collection_grid` already maps to a compatibility table surface
- `metric_chart` already maps to a dedicated temporary chart widget
- `event_log` already maps to the current list/event surface

Stand verification checklist:

- open `Demo Metrics` from the desktop or the scenario sidebar
- confirm the metrics table, chart, and event log all render
- select `CPU Load`, `Memory Pressure`, and `Queue Depth` in the table
- confirm the chart title/value/line change with the selected row
- confirm the same behavior works in the modal demo surface

Recommended paired scenario:

- `taiga_ui_demo_scenario`

Recommended semantic kinds exercised here:

- `collection_grid`
- `metric_chart`
- `event_log`
