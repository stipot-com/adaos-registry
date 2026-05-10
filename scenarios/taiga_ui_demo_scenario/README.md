## taiga_ui_demo_scenario

`taiga_ui_demo_scenario` is the control scenario for the first Taiga-oriented
semantic browser UI slice.

It is intentionally neutral and operational rather than product-specific.

The scenario exists to validate:

- one table-oriented semantic view
- one chart-oriented semantic view
- one chat-oriented semantic view
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
- the semantic surface now declares workspace class, lifecycle stage, and
  capability-aware view availability
- table row selection now updates shared local `view:` state
- the semantic table surface now renders through the first Taiga-backed grid
  renderer
- the semantic chart surface now renders through a Taiga-backed chart renderer
- the semantic chat surface now renders through the shared chat widget path
- desktop and modal actions now drive live demo events through both host and
  skill action paths
- the desktop launch surface now exercises typed `open_workspace` through the
  workspace-manager bridge
- the demo package now includes a second `operations`-class modal surface so
  workspace and operations composition can be compared on the same stand
- semantic workspace metadata now projects into `runtime.surface.*` state so
  typed actions can carry stable surface context
- the scenario now also ships an explicit top-level `workspace` shell surface
  under `ui.application.workspace.pageSchema`

Stand verification checklist:

- load `taiga_ui_demo_scenario`
- verify the desktop surface shows summary, table, chart, event, and chat areas
- confirm the main table is rendered by the Taiga-backed semantic grid path
- confirm the chart is rendered by the Taiga-backed semantic chart path
- confirm the operator notes chat is rendered by the semantic chat path
- click `Skill event` and confirm the event log receives a live update
- click `Host event` and confirm the event log receives a live update
- click `Workspace` and confirm the workspace manager opens through the typed
  `open_workspace` path
- click `Workspace shell` and confirm the browser navigates to `/workspace`
  and renders the dedicated workspace shell surface
- click `Operations` and confirm the operations modal opens with event, chart,
  and chat surfaces but without the workspace-only grid
- click `Open Demo Metrics` and confirm the modal opens through the typed action
  path
- click different table rows and confirm the chart changes accordingly
- use the sidebar action to open the modal and repeat the same check there
