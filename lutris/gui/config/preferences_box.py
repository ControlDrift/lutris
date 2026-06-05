from gettext import gettext as _
from typing import Any

from gi.repository import Gio, Gtk  # type: ignore

from lutris import settings
from lutris.gui.config.base_config_box import BaseConfigBox
from lutris.gui.config.widget_generator import WidgetGenerator
from lutris.gui.dialogs import ErrorDialog, FileDialog
from lutris.gui.widgets.status_icon import supports_status_icon
from lutris.gui.widgets.utils import open_uri
from lutris.settings import read_setting
from lutris.util.llm_auth import DEFAULT_LLM_PROVIDER, LLMAuthUnavailable
from lutris.util.log import logger
from lutris.util.recommendations import invalidate_recommendation_cache

GEMINI_OAUTH_CONSOLE_URL = "https://console.cloud.google.com/apis/credentials"


def _is_system_dark_by_default():
    app = Gio.Application.get_default()
    return app.style_manager.is_dark_by_default


class InterfacePreferencesBox(BaseConfigBox):
    settings_options = [
        {
            "option": "hide_client_on_game_start",
            "label": _("Minimize client when a game is launched"),
            "type": "bool",
            "help": _("Minimize the Lutris window while playing a game; it will return when the game exits."),
        },
        {
            "option": "hide_text_under_icons",
            "label": _("Hide text under icons"),
            "type": "bool",
            "help": _("Removes the names from the Lutris window when in grid view, but not list view."),
        },
        {
            "option": "hide_badges_on_icons",
            "label": _("Hide badges on icons (Ctrl+p to toggle)"),
            "type": "bool",
            "accelerator": "<Primary>p",
            "help": _("Removes the platform and missing-game badges from icons in the Lutris window."),
        },
        {
            "option": "show_tray_icon",
            "label": _("Show Tray Icon"),
            "type": "bool",
            "available": supports_status_icon,
            "help": _(
                "Adds a Lutris icon to the tray, and prevents Lutris from exiting when the Lutris window is closed. "
                "You can still exit using the menu of the tray icon."
            ),
        },
        {
            "option": "discord_rpc",
            "label": _("Enable Discord Rich Presence for Available Games"),
            "type": "bool",
        },
        {
            "option": "preferred_theme",
            "type": "choice",
            "label": _("Theme"),
            "choices": [
                (_("System Default"), "default"),
                (_("Light"), "light"),
                (_("Dark"), "dark"),
            ],
            "default": "default",
            "help": _("Overrides Lutris's appearance to be light or dark."),
        },
    ]

    def __init__(self, accelerators):
        super().__init__()
        self.accelerators = accelerators

        self.add(self.get_section_label(_("Interface options")))
        frame = Gtk.Frame(visible=True, shadow_type=Gtk.ShadowType.ETCHED_IN)
        listbox = Gtk.ListBox(visible=True)
        frame.add(listbox)
        self.pack_start(frame, False, False, 0)

        gen = PreferencesWidgetGenerator(listbox)
        gen.changed.register(self.on_setting_changed)
        self.widget_generator = gen

        for option in self.settings_options:
            gen.generate_container(option)

            if gen.option_container:
                list_box_row = Gtk.ListBoxRow(visible=True)
                list_box_row.set_selectable(False)
                list_box_row.set_activatable(False)
                list_box_row.add(gen.option_container)
                listbox.add(list_box_row)

        listbox.add(self._get_llm_provider_row())
        gen.update_widgets()

    def on_setting_changed(self, option_key, new_value):
        settings.write_setting(option_key, new_value)

    def _get_llm_provider_row(self):
        row = Gtk.ListBoxRow(visible=True)
        row.set_selectable(False)
        row.set_activatable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, visible=True)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_right(12)
        box.set_margin_left(12)

        label = Gtk.Label(_("LLM provider"), visible=True)
        label.set_alignment(0, 0.5)
        box.pack_start(label, True, True, 0)

        self.llm_google_button = Gtk.Button(_("Open Google Cloud"), visible=True)
        self.llm_google_button.connect("clicked", self.on_llm_google_clicked)
        box.pack_end(self.llm_google_button, False, False, 0)

        self.llm_connect_button = Gtk.Button(_("Connect LLM provider"), visible=True)
        self.llm_connect_button.connect("clicked", self.on_llm_connect_clicked)
        box.pack_end(self.llm_connect_button, False, False, 0)

        self.llm_disconnect_button = Gtk.Button(_("Disconnect"), visible=True)
        self.llm_disconnect_button.connect("clicked", self.on_llm_disconnect_clicked)
        box.pack_end(self.llm_disconnect_button, False, False, 0)

        row.add(box)
        self.update_llm_buttons()
        return row

    def on_llm_google_clicked(self, _button):
        open_uri(GEMINI_OAUTH_CONSOLE_URL)

    def update_llm_buttons(self):
        connected = DEFAULT_LLM_PROVIDER.is_authenticated()
        self.llm_connect_button.set_sensitive(not connected)
        self.llm_disconnect_button.set_sensitive(connected)

    def on_llm_connect_clicked(self, _button):
        try:
            default_path = None
            if DEFAULT_LLM_PROVIDER.client_secret_path.exists():
                default_path = str(DEFAULT_LLM_PROVIDER.client_secret_path.parent)
            file_dialog = FileDialog(
                _("Choose the Google OAuth client JSON downloaded from Google Cloud"),
                default_path=default_path,
                parent=self.get_toplevel(),
            )
            if not file_dialog.filename:
                self.update_llm_buttons()
                return
            DEFAULT_LLM_PROVIDER.import_client_secret(file_dialog.filename)
            DEFAULT_LLM_PROVIDER.connect()
            self._refresh_recommendations()
        except LLMAuthUnavailable as ex:
            ErrorDialog(str(ex), parent=self.get_toplevel())
        except Exception as ex:  # noqa: BLE001 - auth is optional, keep preferences usable
            logger.exception("Failed to connect LLM provider: %s", ex)
            ErrorDialog(_("Unable to connect LLM provider: %s") % ex, parent=self.get_toplevel())
        self.update_llm_buttons()

    def on_llm_disconnect_clicked(self, _button):
        DEFAULT_LLM_PROVIDER.disconnect()
        self._refresh_recommendations()
        self.update_llm_buttons()

    def _refresh_recommendations(self):
        """Drop the cached LLM ordering and re-sort the game store with the new auth state."""
        invalidate_recommendation_cache()
        application = Gio.Application.get_default()
        window = getattr(application, "window", None)
        if window:
            window.update_store()


class PreferencesWidgetGenerator(WidgetGenerator):
    """This generator adjusts the spacing of the wrappers and packs widgets on the
    right to get the interface preferences layout instead of the configuration one."""

    def get_setting(self, option_key: str, default: Any) -> Any:
        return read_setting(option_key, default=default)

    def create_wrapper_box(self, option: dict[str, Any], value: Any, default: Any) -> Gtk.Box | None:
        box = super().create_wrapper_box(option, value, default)
        if box:
            box.set_margin_top(12)
            box.set_margin_bottom(12)
            box.set_margin_right(12)
            box.set_margin_left(12)
        return box

    def build_option_widget(
        self, option: dict[str, Any], widget: Gtk.Widget | None, no_label: bool = False, expand: bool = False
    ) -> Gtk.Widget | None:
        if no_label:
            return super().build_option_widget(option, widget, no_label=no_label, expand=expand)

        label = Gtk.Label(option["label"], visible=True, wrap=True)
        label.set_alignment(0, 0.5)
        if self.wrapper and widget:
            self.wrapper.pack_start(label, True, True, 0)
            self.wrapper.pack_end(widget, expand, expand, 0)
        return widget
