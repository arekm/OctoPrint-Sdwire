import datetime
import logging
import os
import subprocess
import tempfile
import threading
import time

import octoprint.plugin
from octoprint.events import Events

from . import _vfatdir


class SdwirePlugin(
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.EventHandlerPlugin,
):
    def __init__(self):
        super().__init__()
        self._logger = logging.getLogger("octoprint.plugins.sdwire")

    ##~~ StartupPlugin mixin

    def on_startup(self, host, port):
        self._logger.info(
            "OctoPrint-Sdwire (sdwire_serial={}, disk_uuid={})".format(
                self._settings.get(["sdwire_serial"]), self._settings.get(["disk_uuid"])
            )
        )

    ##~~ EventHandlerPlugin mixin

    def on_event(self, event, payload):
        if event == Events.CONNECTING:
            self.sdwire_low_switch(mode="sd")

    ##~~ SettingsPlugin mixin

    def get_settings_defaults(self):
        return dict(
            sd_mux_ctrl="/usr/local/bin/sd-mux-ctrl",
            sdwire_serial="sd-wire_11",
            disk_uuid="",
        )

    ##~~ TemplatePlugin mixin

    def get_template_configs(self):
        return [{"type": "settings", "custom_bindings": False}]

    def is_template_autoescaped(self):
        True

    ##~~ AssetPlugin mixin

    def get_assets(self):
        # Define your plugin's asset files to automatically include in the
        # core UI here.
        return {"js": ["js/sdwire.js"]}

    def _run_cmd(self, cmd):
        try:
            output = subprocess.check_output(cmd, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as e:
            self._logger.debug(
                "running command ({}) failed ({}): output: {}, stderr: {}".format(
                    cmd, e.returncode, e.output, e.stderr
                )
            )
            return False
        self._logger.debug(f"running command ({cmd}) succeeded: {output}")
        return True

    def _check_printer_state(self, notify=False):
        if not self._printer.is_ready():
            self._logger.info("Printer is not ready or sd card is in use")
            if notify:
                self.sdwrite_notify_error("Printer is not ready or sd card is in use")
            return False
        return True

    # wait for sd card being available or unavailable
    def _wait_for_sdcard_state(self, timeout, wait_for_notavailable):
        sdready = wait_for_notavailable
        # wait up to timeout for sd card to appear
        for _i in range(timeout * 10):
            sdready = self._printer.is_sd_ready()
            if sdready != wait_for_notavailable:
                break
            time.sleep(0.1)
        return sdready != wait_for_notavailable

    def _wait_for_sdcard(self, timeout):
        return self._wait_for_sdcard_state(timeout, False)

    def _wait_for_nosdcard(self, timeout):
        return self._wait_for_sdcard_state(timeout, True)

    def _get_vfat_remote_filename(self, vfatdir, filename):
        try:
            short_name = _vfatdir.get_short_name(vfatdir, filename)
        except Exception as e:
            self._logger.exception(f"Getting vfat remote filename failed: {e}")
            return None

        if short_name:
            short_name = short_name.decode().lower()
            self._logger.debug(
                "Found short filename {} for {} using vfat ioctl".format(
                    short_name, filename
                )
            )
            return short_name

        return None

    def sdwrite_notify_error(self, message):
        self._plugin_manager.send_plugin_message(self._identifier, dict(error=message))

    def sdwire_low_switch(self, mode):
        mode = mode.lower()
        if mode not in ["sd", "usb"]:
            self._logger.error(f"sdwire_low_switch(): unknown mode: {mode}")
            return False

        if mode == "sd":
            # switching to SD is unreliable on some printers, so try harder
            actions = ["--ts", "--dut", "--ts", "--dut"]
        elif mode == "usb":
            actions = ["--dut", "--ts"]

        self._logger.debug(f"Switching sdwire to {mode.upper()}.")

        for a in actions:
            if not self._run_cmd(
                [
                    "/usr/bin/sudo",
                    self._settings.get(["sd_mux_ctrl"]),
                    "--device-serial={}".format(self._settings.get(["sdwire_serial"])),
                    a,
                ]
            ):
                self._logger.debug(f"Switching sdwire to {mode.upper()} failed.")
                return False
            time.sleep(0.3)

        self._logger.debug(f"Sdwire switched to {mode.upper()}.")
        return True

    def sdwire_switch(self, mode):
        mode = mode.lower()
        if mode not in ["usb", "sd"]:
            self._logger.error(f"sdwire_switch(): unknown mode: {mode}")
            return False

        if mode == "usb":
            self._printer.commands("M22", force=True)
            self._wait_for_nosdcard(timeout=2)

        if not self.sdwire_low_switch(mode):
            return False

        if mode == "sd":
            self._printer.commands("M21", force=True)
            self._wait_for_sdcard(timeout=2)
            self._printer.refresh_sd_files()

        return True

    def sdwire_upload(
        self, printer, filename, path, start_cb, success_cb, failure_cb, *args, **kwargs
    ):
        if not self._settings.get(["disk_uuid"]):
            self.sdwrite_notify_error("SD card UUID was not configured!")
            failure_cb(filename, filename, 0)
            return False

        if not self._settings.get(["sdwire_serial"]):
            self.sdwrite_notify_error("Sdwire serial was not configured!")
            failure_cb(filename, filename, 0)
            return False

        if not self._check_printer_state(notify=True):
            failure_cb(filename, filename, 0)
            return False

        self._logger.info(f"Uploading {filename} to sdwire sd card.")
        start_cb(filename, filename)

        def sdwire_set_progress(progress):
            self._plugin_manager.send_plugin_message(
                self._identifier, dict(progress=int(progress))
            )

        def sdwire_copyfile(src, dst, progress_cb):
            with open(src, "rb") as fsrc:
                file_size = os.stat(fsrc.fileno()).st_size

                with open(dst, "wb") as fdst:
                    bufsize = 1024 * 1024

                    fsrc_read = fsrc.read
                    fdst_write = fdst.write

                    copied = 0
                    while True:
                        buf = fsrc_read(bufsize)
                        if not buf:
                            break
                        fdst_write(buf)
                        copied += len(buf)
                        progress_cb(100 * copied / file_size)
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
            for _i in range(0, 50):
                disk = self._run_cmd(["/usr/sbin/blkid", "-U", uuid])
                if disk:
                    break
                time.sleep(0.1)

            if disk:
                self._logger.debug(f"Disk found for UUID: {uuid}")
            else:
                self._logger.info(f"SD card UUID {uuid} was not found in the system!")
                self.sdwrite_notify_error(
                    f"SD card UUID {uuid} was not found in the system!"
                )
                return False

            self._logger.debug("Mounting sdwire SD Card")
            # keep file creation dates compatible with windows/macos
            time_offset = round(
                (
                    datetime.datetime.now().timestamp()
                    - datetime.datetime.utcnow().timestamp()
                )
                / 60
            )
            if not self._run_cmd(
                [
                    "/usr/bin/sudo",
                    "/usr/bin/mount",
                    f"UUID={uuid}",
                    self.mdir_name,
                    "-o",
                    f"uid={os.getuid()},time_offset={time_offset}",
                ]
            ):
                if not self._run_cmd(
                    [
                        "/usr/bin/sudo",
                        "/usr/bin/mount",
                        f"UUID={uuid}",
                        self.mdir_name,
                        "-o",
                        f"uid={os.getuid()}",
                    ]
                ):
                    self.sdwrite_notify_error(
                        f"Mounting SD card with UUID {uuid} failed."
                    )
                    return False
            self._logger.debug("Sdwire mounted")
            return True

        def sdwire_umount(uuid):
            self._logger.debug("Umounting sdwire")
            if not self._run_cmd(["/usr/bin/sudo", "/usr/bin/umount", f"UUID={uuid}"]):
                self._run_cmd(["/usr/bin/sudo", "/usr/bin/umount", self.mdir_name])
            self.sdwire_switch(mode="sd")
            self.mdir.cleanup()
            return True

        def sdwire_run_upload():
            try:
                start_time = time.time()
                short_filename = None

                try:
                    uuid = self._settings.get(["disk_uuid"])
                    sdwire_set_progress(0)
                    if sdwire_mount(uuid):
                        sdwire_copyfile(
                            path,
                            os.path.join(self.mdir.name, filename),
                            sdwire_set_progress,
                        )

                        # Try to find short filename using vfat ioctl
                        short_filename = self._get_vfat_remote_filename(
                            self.mdir.name, filename
                        )

                        sdwire_umount(uuid)

                except Exception as e:
                    failure_cb(filename, filename, int(time.time() - start_time))
                    self._logger.exception(f"Uploading to sdwire failed: {e}")
                    self.sdwrite_notify_error(f"Uploading to sdwire failed: {e}")
                else:
                    self._logger.info(
                        "Upload of {} done in {:.2f}s".format(
                            filename, time.time() - start_time
                        )
                    )
                    success_cb(
                        filename,
                        short_filename if short_filename else filename,
                        int(time.time() - start_time),
                    )

            except Exception as e:
                failure_cb(filename, filename, int(time.time() - start_time))
                self._logger.exception(f"Unknown problem: {e}")
                self.sdwrite_notify_error(f"Unknown problem: {e}")

        thread = threading.Thread(target=sdwire_run_upload)
        thread.daemon = True
        thread.start()

        # doesn't really matter as filename from success callback takes precedence
        return filename

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
        "octoprint.printer.sdcardupload": __plugin_implementation__.sdwire_upload,
    }
