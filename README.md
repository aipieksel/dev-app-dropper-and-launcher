# App Dropper

Drop a project folder and get a double-clickable Mac app. The app launches that folder's own start script. If the folder does not have one, App Dropper scans the project, writes a start file for the kind of project it finds, and launches that. Web apps open in the default browser. If the app is already running, a second open goes to the same URL and does not start another server. If the preferred port is taken by something else, startup uses the next free port.

Each app is written to `~/App Dropper Apps` and to the Apps folder (`/Applications`, or `~/Applications` when `/Applications` is not writable). Drag `App Dropper Apps` to the Dock to open it the way you open the Applications stack. This app does not change Dock preferences.

## Requirements

- Python 3
- macOS tools used to build a launcher: `swiftc`, `iconutil`, `sips`, `codesign`

## Run

```sh
./scripts/run.sh
```

The shell prefers [http://127.0.0.1:8777](http://127.0.0.1:8777). If that port is already taken, it uses the next free port and prints the URL. Set `CONVERTO_PORT` to change the preferred port.

Paste a folder path, or drop the folder on the App Dropper window. A folder that is not a recognized project shows an error and does not create an app.

## What the folder can contain

Discovery uses the folder's own files, in this order:

1. `app-dropper.json` (or `.app-dropper.json`) with `command`, optional `port`, `port_env`, `health_path`, `identity`, `url_path`, and `web`.
2. `package.json` `dev`, `start`, or `serve`, using the lockfile's package manager. A port is used only when the script, `.env`, or config file writes one.
3. `scripts/run.sh`, `scripts/dev.sh`, `scripts/start.sh`, `run.sh`, `dev.sh`, or `start.sh`.
4. Otherwise the folder is scanned for a known project (Vite, Next, Astro, Angular, React, Nuxt, SvelteKit, Django, FastAPI, Flask, Streamlit, Rails, Laravel, Rust, Go, Make, Python, or a static site). App Dropper writes `app-dropper.json` and, when a server is required, `app-dropper-start.sh`.

A folder with a start script and no port becomes a script launcher. It does not get a URL or a port. A web launcher sets `PORT` (and `port_env`) to the port it actually bound. The browser is opened with `/usr/bin/open` and no named browser.

## macOS shell

```sh
./scripts/build_and_run.sh
```

`--build-only` writes `build/Dev App Dropper and Launcher.app` and the same app under `dist/` without launching it.

## Checks and distribution

Run `python3 -m unittest discover -s tests -v` for project discovery,
launcher, and packaging checks. `build/`, `dist/`, Python caches, and created
apps are local outputs; the repository contains source and tests. App Dropper
does not include the projects that users drop onto it.

Original code in this repository is licensed under the [MIT License](LICENSE).
Projects dropped into App Dropper retain their own licenses and are not
included in this repository.
