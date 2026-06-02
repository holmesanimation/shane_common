# Shared App Preferences

`shane_common.preferences` provides a generic typed preferences backend for desktop apps that want app-owned schemas with shared persistence and editor plumbing.

The boundary is strict:
- `shane_common` owns the schema primitives, settings manager, and path resolution.
- each app owns its `SettingsCategory` registrations and default values.
- strategy configuration remains a separate system even if it reuses the same schema primitives later.

Lifecycle:
- construct `SettingsManager` with either `app_id="your_app"` or an explicit `path=...`
- register every category before calling `load()`
- read effective values through `get(category_id, key)` or dot access such as `settings.app.general.data_root`
- call `save()` to persist only overrides that differ from registered defaults

Disk layout on Windows defaults to `%LOCALAPPDATA%/<app_id>/settings.yaml`.

Persistence semantics:
- defaults live in memory and are not rewritten unless overridden
- unknown categories and keys on disk are ignored during load
- invalid persisted values fail loudly
- unknown on-disk categories are preserved when a known app saves its own overrides