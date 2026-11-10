"""Select GTK 4 / libadwaita before anything imports them, or exit with a hint."""

import sys
from typing import NoReturn

MIN_GTK_VERSION = (4, 10, 0)
MIN_ADW_VERSION = (1, 5)

INSTALL_HINT = """\
{line}
Gnomecast needs GTK >= 4.10 and libadwaita >= 1.5, with their Python
bindings (PyGObject).

{reason}

On Debian (13+) or Ubuntu (24.04+), please run:
$ sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

On Fedora, please run:
$ sudo dnf install python3-gobject gtk4 libadwaita

For other distributions please look up the equivalent packages.
{line}"""


def _exit_with_hint(reason: str) -> NoReturn:
    line = "-" * 70
    print(INSTALL_HINT.format(line=line, reason=reason), file=sys.stderr)
    sys.exit(1)


def require_gtk() -> None:
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk
    except (ImportError, ValueError) as e:
        _exit_with_hint(f"Error: {e}")

    adw_version = (Adw.get_major_version(), Adw.get_minor_version())
    if Gtk.check_version(*MIN_GTK_VERSION) is not None or adw_version < MIN_ADW_VERSION:
        gtk_version = (
            Gtk.get_major_version(),
            Gtk.get_minor_version(),
            Gtk.get_micro_version(),
        )
        _exit_with_hint(
            "Found GTK {} and libadwaita {}.".format(
                ".".join(map(str, gtk_version)), ".".join(map(str, adw_version))
            )
        )
