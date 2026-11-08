"""TwinTray — a system tray app for the Twingate client on Linux.

The display name lives here and nowhere else: every window title, tray title,
menu label and generated autostart entry reads these constants. The app-id is a
different thing from the display name — it is baked into the desktop file name,
the `Icon=` name, the SNI bus name and the autostart entry, so renaming the app
does not change it.
"""

APP_NAME = "TwinTray"
APP_ID = "io.github.panas78.Twintray"
ICON_NAME = "twintray"
VERSION = "0.1.0"
