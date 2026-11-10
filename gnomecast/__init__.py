from gnomecast.gtk_check import require_gtk

# must run before any module imports Gtk/Adw from gi.repository
require_gtk()

from gnomecast.main import main as main
from gnomecast.version import __version__ as __version__

if __name__ == "__main__":
    main()
