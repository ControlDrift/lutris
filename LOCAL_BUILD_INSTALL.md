# Local Build Installation

This describes how to build and install this checkout as a local user build while
continuing to use the existing Flatpak Lutris data directory.

## Assumptions

- Repository path: `/var/home/bazzite/workspace/lutris`
- Existing Flatpak app id: `net.lutris.Lutris`
- Existing Flatpak data path: `/var/home/bazzite/.var/app/net.lutris.Lutris`
- The Flatpak installation remains installed.

## Build And Install

```bash
cd /var/home/bazzite/workspace/lutris
python3 -m pip install --user --no-deps --force-reinstall --no-build-isolation .
```

Verify the installed package:

```bash
python3 -m pip show lutris
```

Expected result:

```text
Name: lutris
Version: 0.5.23
Location: /home/bazzite/.local/lib/python3.14/site-packages
```

## Test The Local Build

The installed Lutris launcher removes local Python package paths from
`sys.path` unless `LUTRIS_ALLOW_LOCAL_PYTHON_PACKAGES=1` is set.

Run the local build with the existing Flatpak config, data, and cache:

```bash
env LUTRIS_ALLOW_LOCAL_PYTHON_PACKAGES=1 \
XDG_CONFIG_HOME=/var/home/bazzite/.var/app/net.lutris.Lutris/data \
XDG_DATA_HOME=/var/home/bazzite/.var/app/net.lutris.Lutris/data \
XDG_CACHE_HOME=/var/home/bazzite/.var/app/net.lutris.Lutris/cache \
/var/home/bazzite/.local/bin/lutris -d
```

## Desktop Launcher Override

Edit the user desktop launcher:

```bash
~/.local/share/applications/net.lutris.Lutris.desktop
```

Set its `Exec=` line to:

```ini
Exec=env LUTRIS_ALLOW_LOCAL_PYTHON_PACKAGES=1 XDG_CONFIG_HOME=/var/home/bazzite/.var/app/net.lutris.Lutris/data XDG_DATA_HOME=/var/home/bazzite/.var/app/net.lutris.Lutris/data XDG_CACHE_HOME=/var/home/bazzite/.var/app/net.lutris.Lutris/cache /var/home/bazzite/.local/bin/lutris %U
```

For `.lutris` installer files, edit:

```bash
~/.local/share/applications/net.lutris.Lutris1.desktop
```

Set its `Exec=` line to:

```ini
Exec=env LUTRIS_ALLOW_LOCAL_PYTHON_PACKAGES=1 XDG_CONFIG_HOME=/var/home/bazzite/.var/app/net.lutris.Lutris/data XDG_DATA_HOME=/var/home/bazzite/.var/app/net.lutris.Lutris/data XDG_CACHE_HOME=/var/home/bazzite/.var/app/net.lutris.Lutris/cache /var/home/bazzite/.local/bin/lutris --install %f
```

Refresh the user desktop database:

```bash
update-desktop-database ~/.local/share/applications
```

## Rollback

Remove the local Python package:

```bash
python3 -m pip uninstall lutris
```

Remove the user desktop launcher overrides:

```bash
rm ~/.local/share/applications/net.lutris.Lutris.desktop
rm ~/.local/share/applications/net.lutris.Lutris1.desktop
update-desktop-database ~/.local/share/applications
```

After rollback, the normal app launcher should use the Flatpak installation
again.
