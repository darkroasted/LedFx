import asyncio
import logging
import socket
from abc import abstractmethod
from functools import cached_property

import numpy as np
import voluptuous as vol
import zeroconf

from ledfx.config import save_config
from ledfx.events import DeviceUpdateEvent, Event
from ledfx.utils import (
    WLED,
    BaseRegistry,
    RegistryLoader,
    async_fire_and_return,
    generate_id,
)

_LOGGER = logging.getLogger(__name__)


@BaseRegistry.no_registration
class Device(BaseRegistry):

    CONFIG_SCHEMA = vol.Schema(
        {
            vol.Optional(
                "rgbw_led",
                description="RGBW LEDs",
                default=False,
            ): bool,
            vol.Optional(
                "icon_name",
                description="https://material-ui.com/components/material-icons/",
                default="mdi:led-strip",
            ): str,
            vol.Optional(
                "center_offset",
                description="Number of pixels from the perceived center of the device",
                default=0,
            ): int,
            vol.Optional(
                "refresh_rate",
                description="Maximum rate that pixels are sent to the device",
                default=60,
            ): int,
        }
    )

    _active = False

    def __init__(self, ledfx, config):
        self._ledfx = ledfx
        self._config = config
        self._segments = []
        self._pixels = None

    def __del__(self):
        if self._active:
            self.deactivate()

    @property
    def pixel_count(self):
        pass

    def is_active(self):
        return self._active

    def update_pixels(self, display_id, data):
        # update each segment from this display
        if not self._active:
            _LOGGER.warning(
                f"Cannot update pixels of inactive device {self.name}"
            )
            return

        for pixels, start, end in data:
            self._pixels[start : end + 1] = pixels

        if display_id == self.priority_display.id:
            frame = self.assemble_frame()
            self.flush(frame)
            # _LOGGER.debug(f"Device {self.id} flushed by Display {display_id}")

            def trigger_device_update_event():
                self._ledfx.events.fire_event(
                    DeviceUpdateEvent(self.id, frame)
                )

            self._ledfx.loop.call_soon_threadsafe(trigger_device_update_event)

    def assemble_frame(self):
        """
        Assembles the frame to be flushed. Currently this will just return
        the active channels pixels, but will eventually handle things like
        merging multiple segments segments and alpha blending channels
        """
        frame = self._pixels

        if self._config["center_offset"]:
            frame = np.roll(frame, self._config["center_offset"], axis=0)
        return frame

    def activate(self):
        self._pixels = np.zeros((self.pixel_count, 3))
        self._active = True

    def deactivate(self):
        self._pixels = None
        self._active = False
        # self.flush(np.zeros((self.pixel_count, 3)))

    @abstractmethod
    def flush(self, data):
        """
        Flushes the provided data to the device. This abstract method must be
        overwritten by the device implementation.
        """

    @property
    def name(self):
        return self._config["name"]

    @property
    def max_refresh_rate(self):
        return self._config["refresh_rate"]

    @property
    def refresh_rate(self):
        return self.priority_display.refresh_rate

    @cached_property
    def priority_display(self):
        """
        Returns the first display that has the highest refresh rate of all displays
        associated with this device
        """
        if not any(display.active for display in self._displays_objs):
            return None

        refresh_rate = max(
            display.refresh_rate
            for display in self._displays_objs
            if display.active
        )
        return next(
            display
            for display in self._displays_objs
            if display.refresh_rate == refresh_rate
        )

    @cached_property
    def _displays_objs(self):
        return list(
            self._ledfx.displays.get(display_id)
            for display_id in self.displays
        )

    @cached_property
    def displays(self):
        return list(segment[0] for segment in self._segments)

    def add_segment(self, display_id, start_pixel, end_pixel):
        # make sure this segment doesn't overlap with any others
        for _display_id, segment_start, segment_end in self._segments:
            overlap = (
                min(segment_end, end_pixel)
                - max(segment_start, start_pixel)
                + 1
            )
            if overlap > 0:
                display_name = self._ledfx.displays.get(display_id).name
                blocking_display_name = self._ledfx.displays.get(
                    _display_id
                ).name
                msg = f"Failed to activate effect! '{display_name}' overlaps with active device '{blocking_display_name}'"
                _LOGGER.warning(msg)
                raise ValueError(msg)

        # if the segment is from a new device, we need to recheck our priority display
        if display_id not in (segment[0] for segment in self._segments):
            self.invalidate_cached_props()
        self._segments.append((display_id, start_pixel, end_pixel))

    def clear_display_segments(self, display_id):
        self._segments = [
            segment for segment in self._segments if segment[0] != display_id
        ]
        if display_id == self.priority_display:
            self.invalidate_cached_props()

    def clear_segments(self):
        self._segments = []

    def invalidate_cached_props(self):
        # invalidate cached properties
        for prop in ["priority_display", "_displays_objs", "displays"]:
            if hasattr(self, prop):
                delattr(self, prop)


class Devices(RegistryLoader):
    """Thin wrapper around the device registry that manages devices"""

    PACKAGE_NAME = "ledfx.devices"

    def __init__(self, ledfx):
        super().__init__(ledfx, Device, self.PACKAGE_NAME)

        def on_shutdown(e):
            self._zeroconf.close()
            self.deactivate_devices()

        self._ledfx.events.add_listener(on_shutdown, Event.LEDFX_SHUTDOWN)
        self._zeroconf = zeroconf.Zeroconf()

    def create_from_config(self, config):
        for device in config:
            _LOGGER.info("Loading device from config: {}".format(device))
            self._ledfx.devices.create(
                id=device["id"],
                type=device["type"],
                config=device["config"],
                ledfx=self._ledfx,
            )

    def deactivate_devices(self):
        for device in self.values():
            device.deactivate()

    def get_device(self, device_id):
        for device in self.values():
            if device_id == device.id:
                return device
        return None

    def update_wled_configs(self):
        for device in self.values():
            if device.type == "wled":
                async_fire_and_return(
                    WLED.get_config(device.config["ip_address"]),
                    device.update_config,
                    timeout=2,
                )

    async def find_wled_devices(self):
        # Scan the LAN network that match WLED using zeroconf - Multicast DNS
        # Service Discovery Library
        _LOGGER.info("Scanning for new WLED devices...")
        wled_listener = WLEDListener(self._ledfx)
        wledbrowser = self._zeroconf.add_service_listener(
            "_wled._tcp.local.", wled_listener
        )
        try:
            await asyncio.sleep(10)
        finally:
            _LOGGER.info("Scan Finished")
            self._zeroconf.remove_service_listener(wled_listener)


class WLEDListener(zeroconf.ServiceBrowser):
    def __init__(self, _ledfx):
        self._ledfx = _ledfx

    def remove_service(self, zeroconf_obj, type, name):
        _LOGGER.info(f"Service {name} removed")

    def add_service(self, zeroconf_obj, type, name):

        _LOGGER.info("Found Device!")

        info = zeroconf_obj.get_service_info(type, name)

        if info:
            address = socket.inet_ntoa(info.addresses[0])
            hostname = str(info.server)
            try:
                wled_config = WLED.get_config(address)
            except ValueError as msg:
                _LOGGER.warning(msg)
                return
            led_info = wled_config["leds"]
            wled_name = wled_config["name"]

            wled_count = led_info["count"]
            wled_rgbmode = led_info["rgbw"]
            device_id = generate_id(wled_name)
            device_type = "wled"
            wled_config = {
                "name": wled_name,
                "pixel_count": wled_count,
                "icon_name": "wled",
                "rgbw_led": wled_rgbmode,
                "ip_address": hostname.rstrip("."),
            }

            # Check this device doesn't share IP, name or hostname with any current saved device
            for device in self._ledfx.devices.values():
                if (
                    device.config["ip_address"] == hostname.rstrip(".")
                    or device.config["ip_address"] == hostname
                    or device.config["name"] == wled_name
                    or device.config["ip_address"] == address
                ):
                    return

            # Create the device
            _LOGGER.info(
                "Adding device of type {} with config {}".format(
                    device_type, wled_config
                )
            )

            device = self._ledfx.devices.create(
                id=device_id,
                type=device_type,
                config=wled_config,
                ledfx=self._ledfx,
            )

            # Update and save the configuration
            self._ledfx.config["devices"].append(
                {
                    "id": device.id,
                    "type": device.type,
                    "config": device.config,
                }
            )
            display_name = f"{device.name}"
            display_id = generate_id(display_name)
            display_config = {
                "name": display_name,
                "icon_name": wled_config["icon_name"],
            }
            segments = [[device.id, 0, wled_config["pixel_count"] - 1, False]]

            # create the display
            display = self._ledfx.displays.create(
                id=display_id,
                config=display_config,
                ledfx=self._ledfx,
                is_device=device.id,
            )

        # create the device as a single segment on the display
        display.update_segments(segments)

        # Update the configuration
        self._ledfx.config["displays"].append(
            {
                "id": display.id,
                "config": display.config,
                "segments": display.segments,
                "is_device": device.id,
            }
        )
        save_config(
            config=self._ledfx.config,
            config_dir=self._ledfx.config_dir,
        )
