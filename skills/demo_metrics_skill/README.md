## demo_metrics_skill

`demo_metrics_skill` is the first neutral browser-demo skill for the semantic
Web UI migration.

Its role is not to be a product feature.
Its role is to provide one compact, reusable validation surface for:

- table-like semantic views
- chart-like semantic views
- chat-oriented semantic views
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
- `collection_grid` now renders through a first Taiga-backed grid surface
- `metric_chart` now renders through a Taiga-backed chart surface
- `event_log` already maps to the current list/event surface
- `chat_panel` now maps to the current chat surface through the semantic adapter
- desktop and modal action buttons now exercise `open_modal`, `call_host`, and
  `invoke_skill_action` paths against the live demo surface

Stand verification checklist:

- open `Demo Metrics` from the desktop or the scenario sidebar
- confirm the metrics table, chart, event log, and operator notes chat all render
- confirm the table uses the Taiga-styled grid surface rather than the old
  compatibility table
- confirm the chart uses the Taiga-styled semantic chart surface
- confirm the operator notes block is rendered through the semantic chat path
- click `Skill event` and confirm a new live event appears in the event log
- click `Host event` and confirm a new live event appears in the event log
- click `Open modal` and confirm the modal opens through the typed `open_modal`
  path
- select `CPU Load`, `Memory Pressure`, and `Queue Depth` in the table
- confirm the chart title/value/line change with the selected row
- confirm the same behavior works in the modal demo surface

Recommended paired scenario:

- `taiga_ui_demo_scenario`

Recommended semantic kinds exercised here:

- `collection_grid`
- `metric_chart`
- `event_log`
- `chat_panel`
