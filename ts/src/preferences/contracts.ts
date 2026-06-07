export interface PrefsGetCommand {
  command: 'prefs.get';
  key: string;
}

export interface PrefsSetCommand {
  command: 'prefs.set';
  key: string;
  value: unknown;
}
