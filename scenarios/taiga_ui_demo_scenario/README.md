## taiga_ui_demo_scenario

`taiga_ui_demo_scenario` is the control scenario for the first Taiga-oriented
semantic browser UI slice.

It is intentionally neutral and operational rather than product-specific.

The scenario exists to validate:

- one table-oriented semantic view
- one chart-oriented semantic view
- one shared selection model
- one live event stream
- one staged loading contract
- one compatibility bridge from current `webui.v1` widgets to future semantic
  renderers

Primary paired skill:

- `demo_metrics_skill`

Current implementation status:

- the semantic schema is already consumed through the runtime compatibility
  bridge
- table row selection now updates shared local `view:` state
- the semantic table surface now renders through the first Taiga-backed grid
  renderer
- the chart surface is no longer a JSON placeholder and now renders as a simple
  temporary trend widget

Stand verification checklist:

- load `taiga_ui_demo_scenario`
- verify the desktop surface shows summary, table, chart, and event areas
- confirm the main table is rendered by the Taiga-backed semantic grid path
- click different table rows and confirm the chart changes accordingly
- use the sidebar action to open the modal and repeat the same check there
