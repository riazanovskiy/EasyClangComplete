"""EasyClangComplete plugin for Sublime Text 3.

Provides completion suggestions for C/C++ languages based on clang

Attributes:
    log (logging.Logger): logger for this module
"""

import sublime
import sublime_plugin
import imp
import logging

from threading import Thread

from .plugin import tools
from .plugin import error_vis
from .plugin import plugin_settings
from .plugin.completion import lib_complete
from .plugin.completion import bin_complete
from .plugin.completion import flags_manager

# reload the modules
imp.reload(tools)
imp.reload(flags_manager)
imp.reload(plugin_settings)
imp.reload(error_vis)
imp.reload(lib_complete)
imp.reload(bin_complete)

# some useful aliases
SublBridge = tools.SublBridge
Tools = tools.Tools
PosStatus = tools.PosStatus

log = logging.getLogger(__name__)

handle_plugin_loaded_function = None


def plugin_loaded():
    """called right after sublime api is ready to use. We need it to initialize
    all the different classes that encapsulate functionality. We can only
    properly init them after sublime api is available."""
    handle_plugin_loaded_function()


class EasyClangComplete(sublime_plugin.EventListener):
    """Base class for this plugin. Most of the functionality is delegated

    Attributes:
        settings (plugin_settings.Settings): class that encapsulates settings
        completer (plugin.completion.base_completion.BaseCompleter):
            This object handles auto completion. It can be one of the following:
            - bin_complete.Completer
            - lib_complete.Completer
    """
    settings = None
    completer = None

    def __init__(self):
        """Initializes the object."""
        super().__init__()
        global handle_plugin_loaded_function
        handle_plugin_loaded_function = lambda: self.on_plugin_loaded()
        # By default be verbose and limit on settings change if verbose flag is
        # not set.
        logging.basicConfig(level=logging.DEBUG)

    def on_plugin_loaded(self):
        """Called upon plugin load event."""
        self.settings = plugin_settings.Settings()
        self.on_settings_changed()
        self.settings.add_change_listener(lambda: self.on_settings_changed())
        # As the plugin have just loaded, we might have missed an activation
        # event for the active view so completion will not work for it until
        # re-activated. Force active view initialization in that case.
        self.on_activated_async(sublime.active_window().active_view())

    def on_settings_changed(self):
        """Called when any of the settings changes."""
        # If verbose flag is set then respect default DEBUG level.
        # Otherwise disable level DEBUG and allow INFO and higher levels.
        off_level = logging.NOTSET if self.settings.verbose else logging.DEBUG
        logging.disable(level=off_level)

        # init everything else
        self.completer = None
        if self.settings.use_libclang:
            log.info(" init completer based on libclang")
            self.completer = lib_complete.Completer(self.settings.clang_binary)
            if not self.completer.valid:
                log.error(" cannot initialize completer with libclang.")
                log.info(" falling back to using clang in a subprocess.")
                self.completer = None
        if not self.completer:
            log.info(" init completer based on clang from cmd")
            self.completer = bin_complete.Completer(self.settings.clang_binary)

    def on_activated_async(self, view):
        """Called upon activating a view. Execution in a worker thread.

        Args:
            view (sublime.View): current view

        """
        log.debug(" on_activated_async view id %s", view.buffer_id())
        if Tools.is_valid_view(view):
            if not self.completer:
                return
            if not self.completer.needs_init(view):
                return
            log.debug("init completer for view id: %s", view.buffer_id())
            self.completer.init(view, self.settings)

    def on_selection_modified(self, view):
        """Called when selection is modified. Executed in gui thread.

        Args:
            view (sublime.View): current view
        """
        if Tools.is_valid_view(view):
            (row, _) = SublBridge.cursor_pos(view)
            if not self.completer:
                return
            self.completer.error_vis.show_popup_if_needed(view, row)

    def on_modified_async(self, view):
        """Called in a worker thread when view is modified

        Args:
            view (sublime.View): current view
        """
        if Tools.is_valid_view(view):
            log.debug(" on_modified_async view id %s", view.buffer_id())
            if not self.completer:
                return
            self.completer.error_vis.clear(view)

    def on_post_save_async(self, view):
        """On save. Executed in a worker thread.

        Args:
            view (sublime.View): current view

        """
        if Tools.is_valid_view(view):
            log.debug(" saving view: %s", view.buffer_id())
            if not self.completer:
                return
            self.completer.error_vis.erase_regions(view)
            self.completer.update(view, self.settings.errors_on_save)

    def on_close(self, view):
        """Called on closing the view.

        Args:
            view (sublime.View): current view

        """
        if Tools.is_valid_view(view):
            log.debug(" closing view %s", view.buffer_id())
            if not self.completer:
                return
            self.completer.remove(view.buffer_id())

    def on_query_completions(self, view, prefix, locations):
        """Function that is called when user queries completions in the code

        Args:
            view (sublime.View): current view
            prefix (TYPE): Description
            locations (list[int]): positions of the cursor (first if many).

        Returns:
            sublime.Completions: completions with a flag
        """
        log.debug(" on_query_completions view id %s", view.buffer_id())
        log.debug(" prefix: %s, locations: %s" % (prefix, locations))

        if not Tools.is_valid_view(view):
            log.debug(" not a valid view")
            return Tools.SHOW_DEFAULT_COMPLETIONS

        if not self.completer:
            log.debug(" no completer")
            return Tools.SHOW_DEFAULT_COMPLETIONS

        if self.completer.async_completions_ready:
            self.completer.async_completions_ready = False
            log.debug(" returning existing completions")
            return self.completer.get_completions(
                self.settings.hide_default_completions)

        # Verify that character under the cursor is one allowed trigger
        pos_status = Tools.get_position_status(
            locations[0], view, self.settings)
        if pos_status == PosStatus.WRONG_TRIGGER:
            # we are at a wrong trigger, remove all completions from the list
            log.debug(" wrong trigger")
            log.debug(" hiding default completions")
            return Tools.HIDE_DEFAULT_COMPLETIONS
        if pos_status == PosStatus.COMPLETION_NOT_NEEDED:
            log.debug(" completion not needed")
            # show default completions for now if allowed
            if self.settings.hide_default_completions:
                log.debug(" hiding default completions")
                return Tools.HIDE_DEFAULT_COMPLETIONS
            log.debug(" showing default completions")
            return Tools.SHOW_DEFAULT_COMPLETIONS

        # create a daemon thread to update the completions
        log.debug(" starting async auto_complete at pos: %s", locations[0])
        completion_thread = Thread(
            target=self.completer.complete,
            args=[view, locations[0], self.settings.errors_on_save])
        completion_thread.deamon = True
        completion_thread.start()

        # show default completions for now if allowed
        if self.settings.hide_default_completions:
            log.debug(" hiding default completions")
            return Tools.HIDE_DEFAULT_COMPLETIONS
        log.debug(" showing default completions")
        return Tools.SHOW_DEFAULT_COMPLETIONS
