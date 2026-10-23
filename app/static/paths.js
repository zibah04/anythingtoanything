// Keep assets and API calls inside the app's mount point, including proxy prefixes.
export function applicationBase(moduleUrl) {
  const directory = new URL(".", moduleUrl);
  // The /static/ entry point is retained for existing links and integrations.
  return directory.pathname.endsWith("/static/")
    ? new URL("../", directory)
    : directory;
}
