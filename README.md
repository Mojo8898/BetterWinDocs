# Better WinDocs Sidebar (Binary Ninja)

A Binary Ninja sidebar widget that shows Microsoft Learn Win32 API documentation for imported WinAPI functions when you click them.

## Features

- Updates only when you click a reference that resolves to an imported WinAPI function
- No UI changes when clicking non-WinAPI tokens
- Pulls Syntax, Description, and Return value from Microsoft Learn
- Local cache to avoid repeated network requests

## Install

1. Copy this folder to your Binary Ninja plugins directory:
   - Windows: `%APPDATA%\Binary Ninja\plugins\`
   - macOS: `~/Library/Application Support/Binary Ninja/plugins/`
   - Linux: `~/.binaryninja/plugins/`
2. Restart Binary Ninja.

## Usage

Open the sidebar and select the `Docs` panel. Click an imported WinAPI call site or import symbol. The panel updates only when documentation is found.

## Notes

- Requires network access to `learn.microsoft.com`
- Docs are cached in `cache.json` in the plugin directory
