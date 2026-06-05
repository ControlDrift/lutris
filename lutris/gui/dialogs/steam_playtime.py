"""Dialog offering to import the playtimes recorded by the Steam client"""

from gettext import gettext as _
from gettext import ngettext

from gi.repository import Gtk  # type: ignore

from lutris.gui.dialogs import ModalDialog
from lutris.util.steam.playtime import PlaytimeImportCandidates
from lutris.util.strings import get_formatted_playtime


class SteamPlaytimeImportDialog(ModalDialog):
    """Ask the user whether to copy the playtimes recorded by Steam into the library.
    After run() returns, 'confirmed' tells whether the import was accepted and
    'add_uninstalled' whether non-installed games should be added to the library."""

    def __init__(self, candidates: PlaytimeImportCandidates, parent: Gtk.Widget | None = None):
        super().__init__(title=_("Import Steam playtime"), parent=parent, border_width=10)
        self.add_uninstalled = bool(candidates.additions)

        self.add_button(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL)
        self.add_default_button(_("Import"), Gtk.ResponseType.OK)

        vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 12)
        self.get_content_area().add(vbox)

        intro_label = Gtk.Label(_("Steam has recorded playtime that is missing from your Lutris library."))
        intro_label.set_alignment(0, 0.5)
        vbox.pack_start(intro_label, False, False, 0)

        if candidates.updates:
            updates_label = Gtk.Label(
                ngettext(
                    "%d game in your library will be updated to %s of playtime.",
                    "%d games in your library will be updated to %s of playtime.",
                    len(candidates.updates),
                )
                % (len(candidates.updates), get_formatted_playtime(candidates.updated_hours))
            )
            updates_label.set_alignment(0, 0.5)
            vbox.pack_start(updates_label, False, False, 0)

        if candidates.additions:
            additions_checkbutton = Gtk.CheckButton(
                label=ngettext(
                    "Also add %d non-installed Steam game with %s of playtime to the library.",
                    "Also add %d non-installed Steam games with %s of playtime to the library.",
                    len(candidates.additions),
                )
                % (len(candidates.additions), get_formatted_playtime(candidates.added_hours))
            )
            additions_checkbutton.set_active(True)
            additions_checkbutton.connect("toggled", self.on_additions_checkbutton_toggled)
            vbox.pack_start(additions_checkbutton, False, False, 0)

        self.show_all()
        self.run()  # type: ignore

    def on_additions_checkbutton_toggled(self, button: Gtk.CheckButton) -> None:
        self.add_uninstalled = button.get_active()
