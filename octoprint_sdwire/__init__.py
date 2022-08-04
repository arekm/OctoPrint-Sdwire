# coding=utf-8
from __future__ import absolute_import

import logging
import os
import subprocess
import tempfile
import threading
import time

import octoprint.plugin
from octoprint.events import Events

class SdwirePlugin(octoprint.plugin.SettingsPlugin,
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.EventHandlerPlugin,
):

    def __init__(self):
        super(SdwirePlugin, self).__init__()
        self._logger = logging.getLogger("octoprint.plugins.sdwire")
        self.started = False
        self.lfn = False

    def on_after_startup(self):
        self._logger.info("OctoPrint-Sdwire loaded (sdwire_serial={}, disk_uuid={})".format(self._settings.get(["sdwire_serial"]), self._settings.get(["disk_uuid"])))

        if self._check_printer_state():
            self.sdwire_switch(mode="usb")
            self.sdwire_switch(mode="sd")

        self.started = True

    def on_event(self, event, payload):
        if event == Events.CONNECTED:
            if self.started and self._check_printer_state():
                self.sdwire_switch(mode="usb")
                self.sdwire_switch(mode="sd")

    ##~~ SettingsPlugin mixin

    def get_settings_defaults(self):
        return dict(sd_mux_ctrl="/usr/local/bin/sd-mux-ctrl",
                sdwire_serial="sd-wire_11",
                disk_uuid=""
                )

    def get_template_configs(self):
        return [{"type": "settings", "custom_bindings": False}]

    ##~~ AssetPlugin mixin

    def get_assets(self):
        # Define your plugin's asset files to automatically include in the
        # core UI here.
        return { "js": ["js/sdwire.js"] }


    def _run_cmd(self, cmd):
        try:
            output = subprocess.check_output(cmd, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as e:
            self._logger.debug("running command ({}) failed ({}): output: {}, stderr: {}".format(cmd, e.returncode, e.output, e.stderr))
            return False
        self._logger.debug("running command ({}) succeeded: {}".format(cmd, output))
        return True

    def _check_printer_state(self, notify=False):
        if not self._printer.is_ready():
            self._logger.info("Printer is not ready or sd card is in use")
            if notify:
                self.sdwrite_notify_error("Printer is not ready or sd card is in use")
            return False
        return True

    def _get_remote_filename(self, filename):
        files = self._printer.get_sd_files(refresh=True)
        return next((item['name'] for item in files if item['display'] == filename and item['name']), filename)

    def sdwrite_notify_error(self, message):
        self._plugin_manager.send_plugin_message(self._identifier, dict(error=message))

    def sdwire_switch(self, mode):
        if mode.lower() == "sd":
            mode_opt = "--dut"
        elif mode.lower() == "usb":
            mode_opt = "--ts"
            self._printer.commands("M22")
        else:
            self._logger.error("sdwire_switch(): unknown mode: {}".format(mode))
            return False

        self._logger.debug("Switching sdwire to {}.".format(mode.upper()))
        if self._run_cmd(["/usr/bin/sudo", self._settings.get(["sd_mux_ctrl"]), "--device-serial={}".format(self._settings.get(["sdwire_serial"])), mode_opt]):
            self._logger.debug("Sdwire switched to {}.".format(mode.upper()))
            if mode.lower() == "sd":
                self._printer.commands("M21")
                self._printer.refresh_sd_files()
            return True
        self._logger.debug("Switching sdwire to {} failed.".format(mode.upper()))
        return False

    def sdwire_upload(self, printer, filename, path,
            start_cb, success_cb, failure_cb,
            *args, **kwargs):

        # Assume long file names support.
        if printer._comm._capability_supported(printer._comm.CAPABILITY_EXTENDED_M20):
            remote_filename = return_filename = filename
            self.lfn = True
        else:
            remote_filename = return_filename = printer._get_free_remote_name(filename)
            self.lfn = False

        if not self._settings.get(["disk_uuid"]):
            self.sdwrite_notify_error("SD card UUID was not configured!")
            failure_cb(filename, remote_filename, 0)
            return False

        if not self._settings.get(["sdwire_serial"]):
            self.sdwrite_notify_error("Sdwire serial was not configured!")
            failure_cb(filename, remote_filename, 0)
            return False

        if not self._check_printer_state(notify=True):
            failure_cb(filename, remote_filename, 0)
            return False

        self._logger.info("Uploading {} to sdwire sd card.".format(remote_filename))
        start_cb(filename, remote_filename)

        def sdwire_set_progress(progress):
            self._plugin_manager.send_plugin_message(self._identifier, dict(progress=int(progress)))

        def sdwire_copyfile(src, dst, progress_cb):
            with open(src, 'rb') as fsrc:
                file_size = os.stat(fsrc.fileno()).st_size

                with open(dst, 'wb') as fdst:

                    bufsize = 1024*1024

                    fsrc_read = fsrc.read
                    fdst_write = fdst.write

                    copied = 0
                    while True:
                        buf = fsrc_read(bufsize)
                        if not buf:
                            break
                        fdst_write(buf)
                        copied += len(buf)
                        progress_cb(100*copied/file_size)
                    return copied
            return False

        def sdwire_mount(uuid):
            self.mdir = tempfile.TemporaryDirectory()
            self.mdir_name = self.mdir.name
            if not self.sdwire_switch(mode="usb"):
                self.sdwrite_notify_error("Failed to switch sdwire to USB mode.")
                return False

            # wait for device for 5s
            disk = None
            for i in range(0, 50):
                disk = self._run_cmd(["/usr/sbin/blkid", "-U", uuid])
                if disk:
                    break
                time.sleep(0.1)

            if disk:
                self._logger.debug("Disk found for UUID: {}".format(uuid))
            else:
                self._logger.info("SD card UUID {} was not found in the system!".format(uuid))
                self.sdwrite_notify_error("SD card UUID {} was not found in the system!".format(uuid))
                return False

            self._logger.debug("Mounting sdwire SD Card")
            if not self._run_cmd(["/usr/bin/sudo",  "/usr/bin/mount", "UUID={}".format(uuid), self.mdir_name, "-o", "uid={}".format(os.getuid())]):
                self.sdwrite_notify_error("Mounting SD card with UUID {} failed.".format(uuid))
                return False
            self._logger.debug("Sdwire mounted")

        def sdwire_umount(uuid):
            self._logger.debug("Umounting sdwire")
            if not self._run_cmd(["/usr/bin/sudo", "/usr/bin/umount", "UUID={}".format(uuid)]):
                    self._run_cmd(["/usr/bin/sudo", "/usr/bin/umount", self.mdir_name])
            self.sdwire_switch(mode="sd")
            self.mdir.cleanup()

        def sdwire_run_upload():
            try:
                start_time = time.time()
                try:

                    uuid = self._settings.get(["disk_uuid"])
                    sdwire_set_progress(0)
                    sdwire_mount(uuid)
                    sdwire_copyfile(path, os.path.join(self.mdir.name, remote_filename), sdwire_set_progress)
                    sdwire_umount(uuid)

                    # We try to return short file name to octoprint anyway because
                    # most firmwares don't support things like M23 with long filename.
                    if self.lfn:
                        return_filename = self._get_remote_filename(remote_filename)

                except Exception as e:
                    failure_cb(filename, remote_filename, int(time.time() - start_time))
                    self._logger.exception("Uploading to sdwire failed: {}".format(e))
                    self.sdwrite_notify_error("Uploading to sdwire failed: {}".format(e))
                else:
                    self._logger.info("Upload of {} as {} done in {:.2f}s".format(filename, remote_filename, time.time() - start_time))
                    success_cb(filename, return_filename, int(time.time() - start_time))
            except Exception as e:
                failure_cb(filename, remote_filename, int(time.time() - start_time))
                self._logger.exception("Unknown problem: {}".format(e))
                self.sdwrite_notify_error("Unknown problem: {}".format(e))

        thread = threading.Thread(target = sdwire_run_upload)
        thread.daemon = True
        thread.start()

        # doesn't really matter as filename from success callback takes precedence
        return return_filename

    ##~~ Softwareupdate hook

    def get_update_information(self):
        # Define the configuration for your plugin to use with the Software Update
        # Plugin here. See https://docs.octoprint.org/en/master/bundledplugins/softwareupdate.html
        # for details.
        return {
                "sdwire": {
                    "displayName": "Sdwire",
                    "displayVersion": self._plugin_version,

                    # version check: github repository
                    "type": "github_release",
                    "user": "arekm",
                    "repo": "OctoPrint-Sdwire",
                    "current": self._plugin_version,

                    # update method: pip
                    "pip": "https://github.com/arekm/OctoPrint-Sdwire/archive/{target_version}.zip",
                    }
                }


__plugin_name__ = "Sdwire"


# Set the Python version your plugin is compatible with below. Recommended is Python 3 only for all new plugins.
# OctoPrint 1.8.0 onwards only supports Python 3.
__plugin_pythoncompat__ = ">=3,<4"  # Only Python 3

def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = SdwirePlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
            "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
            "octoprint.printer.sdcardupload": __plugin_implementation__.sdwire_upload
            }
