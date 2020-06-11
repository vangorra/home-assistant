"""Support for TPLink lights."""
from datetime import timedelta
import logging
import time
from typing import Any, Dict, NamedTuple, Tuple, cast

from kasa import SmartBulb, SmartDeviceException

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP,
    ATTR_HS_COLOR,
    SUPPORT_BRIGHTNESS,
    SUPPORT_COLOR,
    SUPPORT_COLOR_TEMP,
    LightEntity,
)
import homeassistant.helpers.device_registry as dr
from homeassistant.helpers.typing import HomeAssistantType
from homeassistant.util.color import (
    color_temperature_kelvin_to_mired as kelvin_to_mired,
    color_temperature_mired_to_kelvin as mired_to_kelvin,
)

from . import CONF_LIGHT, DOMAIN as TPLINK_DOMAIN
from .common import async_add_entities_retry

PARALLEL_UPDATES = 0
SCAN_INTERVAL = timedelta(seconds=5)

_LOGGER = logging.getLogger(__name__)

ATTR_CURRENT_POWER_W = "current_power_w"
ATTR_DAILY_ENERGY_KWH = "daily_energy_kwh"
ATTR_MONTHLY_ENERGY_KWH = "monthly_energy_kwh"


async def async_setup_entry(hass: HomeAssistantType, config_entry, async_add_entities):
    """Set up switches."""
    await async_add_entities_retry(
        hass, async_add_entities, hass.data[TPLINK_DOMAIN][CONF_LIGHT], add_entity
    )

    return True


async def add_entity(device: SmartBulb, async_add_entities):
    """Check if device is online and add the entity."""
    # Attempt to get the sysinfo. If it fails, it will raise an
    # exception that is caught by async_add_entities_retry which
    # will try again later.
    await device.update()

    async_add_entities([TPLinkSmartBulb(device)], update_before_add=True)


def brightness_to_percentage(byt):
    """Convert brightness from absolute 0..255 to percentage."""
    return round((byt * 100.0) / 255.0)


def brightness_from_percentage(percent):
    """Convert percentage to absolute value 0..255."""
    return round((percent * 255.0) / 100.0)


LightState = NamedTuple(
    "LightState",
    (
        ("state", bool),
        ("brightness", int),
        ("color_temp", float),
        ("hs", Tuple[int, int]),
        ("emeter_params", dict),
    ),
)


LightFeatures = NamedTuple(
    "LightFeatures",
    (
        ("sysinfo", Dict[str, Any]),
        ("mac", str),
        ("alias", str),
        ("model", str),
        ("supported_features", int),
        ("min_mireds", float),
        ("max_mireds", float),
    ),
)


class TPLinkSmartBulb(LightEntity):
    """Representation of a TPLink Smart Bulb."""

    def __init__(self, smartbulb: SmartBulb) -> None:
        """Initialize the bulb."""
        self.smartbulb = smartbulb
        self._light_features = cast(LightFeatures, None)
        self._light_state = cast(LightState, None)
        self._is_available = True
        self._is_setting_light_state = False

    @property
    def unique_id(self):
        """Return a unique ID."""
        return self._light_features.mac

    @property
    def name(self):
        """Return the name of the Smart Bulb."""
        return self._light_features.alias

    @property
    def device_info(self):
        """Return information about the device."""
        return {
            "name": self._light_features.alias,
            "model": self._light_features.model,
            "manufacturer": "TP-Link",
            "connections": {(dr.CONNECTION_NETWORK_MAC, self._light_features.mac)},
            "sw_version": self._light_features.sysinfo["sw_ver"],
        }

    @property
    def available(self) -> bool:
        """Return if bulb is available."""
        return self._is_available

    @property
    def device_state_attributes(self):
        """Return the state attributes of the device."""
        return self._light_state.emeter_params

    async def async_turn_on(self, **kwargs):
        """Turn the light on."""
        brightness = (
            int(kwargs[ATTR_BRIGHTNESS])
            if ATTR_BRIGHTNESS in kwargs
            else self._light_state.brightness
            if self._light_state.brightness is not None
            else 255
        )
        color_tmp = (
            int(kwargs[ATTR_COLOR_TEMP])
            if ATTR_COLOR_TEMP in kwargs
            else self._light_state.color_temp
        )

        await self.async_set_light_state_retry(
            self._light_state,
            LightState(
                state=True,
                brightness=brightness,
                color_temp=color_tmp,
                hs=tuple(kwargs.get(ATTR_HS_COLOR, self._light_state.hs or ())),
                emeter_params=self._light_state.emeter_params,
            ),
        )

    async def async_turn_off(self, **kwargs):
        """Turn the light off."""
        await self.async_set_light_state_retry(
            self._light_state,
            LightState(
                state=False,
                brightness=self._light_state.brightness,
                color_temp=self._light_state.color_temp,
                hs=self._light_state.hs,
                emeter_params=self._light_state.emeter_params,
            ),
        )

    @property
    def min_mireds(self):
        """Return minimum supported color temperature."""
        return self._light_features.min_mireds

    @property
    def max_mireds(self):
        """Return maximum supported color temperature."""
        return self._light_features.max_mireds

    @property
    def color_temp(self):
        """Return the color temperature of this light in mireds for HA."""
        return self._light_state.color_temp

    @property
    def brightness(self):
        """Return the brightness of this light between 0..255."""
        return self._light_state.brightness

    @property
    def hs_color(self):
        """Return the color."""
        return self._light_state.hs

    @property
    def is_on(self):
        """Return True if device is on."""
        return self._light_state.state

    async def async_update(self):
        """Update the TP-Link Bulb's state."""
        # State is currently being set, ignore.
        if self._is_setting_light_state:
            return

        try:
            # Update light features only once.
            self._light_features = self._light_features or self.get_light_features()
            self._light_state = await self.get_light_state()
            self._is_available = True
        except (SmartDeviceException, OSError) as ex:
            if self._is_available:
                _LOGGER.warning(
                    "Could not read data for %s: %s", self.smartbulb.host, ex
                )
            self._is_available = False

    @property
    def supported_features(self):
        """Flag supported features."""
        return self._light_features.supported_features

    def get_light_features(self):
        """Determine all supported features in one go."""
        sysinfo = self.smartbulb.sys_info
        supported_features = 0
        mac = self.smartbulb.mac
        alias = self.smartbulb.alias
        model = self.smartbulb.model
        min_mireds = None
        max_mireds = None

        if self.smartbulb.is_dimmable:
            supported_features += SUPPORT_BRIGHTNESS
        if self.smartbulb.is_variable_color_temp:
            supported_features += SUPPORT_COLOR_TEMP
            min_mireds = kelvin_to_mired(self.smartbulb.valid_temperature_range[1])
            max_mireds = kelvin_to_mired(self.smartbulb.valid_temperature_range[0])
        if self.smartbulb.is_color:
            supported_features += SUPPORT_COLOR

        return LightFeatures(
            sysinfo=sysinfo,
            mac=mac,
            alias=alias,
            model=model,
            supported_features=supported_features,
            min_mireds=min_mireds,
            max_mireds=max_mireds,
        )

    async def get_light_state(self) -> LightState:
        """Get the light state."""
        emeter_params = {}
        brightness = None
        color_temp = None
        hue_saturation = None

        await self.smartbulb.update()

        state = self.smartbulb.is_on

        if self._light_features.supported_features & SUPPORT_BRIGHTNESS:
            brightness = brightness_from_percentage(self.smartbulb.brightness)

        if self._light_features.supported_features & SUPPORT_COLOR_TEMP:
            if self.smartbulb.color_temp is not None and self.smartbulb.color_temp != 0:
                color_temp = kelvin_to_mired(self.smartbulb.color_temp)

        if self._light_features.supported_features & SUPPORT_COLOR:
            hue, sat, _ = self.smartbulb.hsv
            hue_saturation = (hue, sat)

        if self.smartbulb.has_emeter:
            emeter_params[ATTR_CURRENT_POWER_W] = "{:.1f}".format(
                await self.smartbulb.current_consumption()
            )
            daily_statistics = await self.smartbulb.get_emeter_daily()
            monthly_statistics = await self.smartbulb.get_emeter_monthly()
            try:
                emeter_params[ATTR_DAILY_ENERGY_KWH] = "{:.3f}".format(
                    daily_statistics[int(time.strftime("%d"))]
                )
                emeter_params[ATTR_MONTHLY_ENERGY_KWH] = "{:.3f}".format(
                    monthly_statistics[int(time.strftime("%m"))]
                )
            except KeyError:
                # device returned no daily/monthly history
                pass

        return LightState(
            state=state,
            brightness=brightness,
            color_temp=color_temp,
            hs=hue_saturation,
            emeter_params=emeter_params,
        )

    async def async_set_light_state_retry(
        self, old_light_state: LightState, new_light_state: LightState
    ) -> None:
        """Set the light state with retry."""
        # Optimistically setting the light state.
        self._light_state = new_light_state

        # Tell the device to set the states.
        self._is_setting_light_state = True

        # TODO: this used to be two separate calls to set_light_state and it was simply confusing..
        for t in range(5):
            try:
                await self.set_light_state(old_light_state, new_light_state)
                self._is_available = True
                self._is_setting_light_state = False
                return
            except (SmartDeviceException, OSError) as ex:
                _LOGGER.debug("Got error while setting the state, retrying: %s", ex)

        _LOGGER.warning("Could not set state for %s", self.smartbulb.host)
        self._is_setting_light_state = False

    async def set_light_state(
        self, old_light_state: LightState, new_light_state: LightState
    ) -> None:
        """Set the light state."""
        # Calling the API with the new state information.
        if new_light_state.state != old_light_state.state:
            if new_light_state.state:
                await self.smartbulb.turn_on()
            else:
                await self.smartbulb.turn_off()
                return

        if new_light_state.color_temp != old_light_state.color_temp:
            await self.smartbulb.set_color_temp(
                int(mired_to_kelvin(new_light_state.color_temp))
            )

        brightness_pct = brightness_to_percentage(new_light_state.brightness)
        if new_light_state.hs != old_light_state.hs and len(new_light_state.hs) > 1:
            hue, sat = new_light_state.hs
            await self.smartbulb.set_hsv(int(hue), int(sat), brightness_pct)
        elif new_light_state.brightness != old_light_state.brightness:
            await self.smartbulb.set_brightness(brightness_pct)
