"""
Support for monitoring a Sense energy sensor.

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/sensor.sense/
"""
import logging
from datetime import timedelta
import json

import voluptuous as vol

from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import (CONF_EMAIL, CONF_PASSWORD,
                                 CONF_MONITORED_CONDITIONS,
                                 CONF_DEVICES)
from homeassistant.helpers.entity import Entity
from homeassistant.util import Throttle
from homeassistant.util import slugify
import homeassistant.helpers.config_validation as cv

REQUIREMENTS = ['sense_energy==0.4.2']

_LOGGER = logging.getLogger(__name__)

ACTIVE_NAME = "Energy"
PRODUCTION_NAME = "Production"
CONSUMPTION_NAME = "Usage"

ACTIVE_TYPE = 'active'
DEVICE_TYPE = 'device'


class SensorConfig:
    """Data structure holding sensor config."""

    def __init__(self, name, sensor_type):
        """Sensor name and type to pass to API."""
        self.name = name
        self.sensor_type = sensor_type


# Sensor types/ranges
SENSOR_TYPES = {'active': SensorConfig(ACTIVE_NAME, ACTIVE_TYPE),
                'daily': SensorConfig('Daily', 'DAY'),
                'weekly': SensorConfig('Weekly', 'WEEK'),
                'monthly': SensorConfig('Monthly', 'MONTH'),
                'yearly': SensorConfig('Yearly', 'YEAR')}

# Production/consumption variants
SENSOR_VARIANTS = [PRODUCTION_NAME.lower(), CONSUMPTION_NAME.lower()]

# Valid sensors for configuration
VALID_SENSORS = ['%s_%s' % (typ, var)
                 for typ in SENSOR_TYPES
                 for var in SENSOR_VARIANTS]

ICON = 'mdi:flash'

MIN_TIME_BETWEEN_DAILY_UPDATES = timedelta(seconds=300)
MIN_TIME_BETWEEN_ACTIVE_UPDATES = timedelta(seconds=60)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_EMAIL): cv.string,
    vol.Required(CONF_PASSWORD): cv.string,
    vol.Required(CONF_MONITORED_CONDITIONS):
        vol.All(cv.ensure_list, vol.Length(min=1), [vol.In(VALID_SENSORS)]),
    vol.Optional(CONF_DEVICES): cv.boolean
})


def setup_platform(hass, config, add_entities, discovery_info=None):
    """Set up the Sense sensor."""
    from sense_energy import Senseable

    username = config.get(CONF_EMAIL)
    password = config.get(CONF_PASSWORD)

    data = Senseable(username, password)

    @Throttle(MIN_TIME_BETWEEN_DAILY_UPDATES)
    def update_trends():
        """Update the daily power usage."""
        data.update_trend_data()

    @Throttle(MIN_TIME_BETWEEN_ACTIVE_UPDATES)
    def update_active():
        """Update the active power usage."""
        data.get_realtime()

    @Throttle(MIN_TIME_BETWEEN_ACTIVE_UPDATES)
    def update_devices():
        """Update the active power usage."""
        global realtime_devices
        realtime_devices = data.get_realtime().get('devices', {})

    devices = []
    for sensor in config.get(CONF_MONITORED_CONDITIONS):
        config_name, prod = sensor.rsplit('_', 1)
        name = SENSOR_TYPES[config_name].name
        sensor_type = SENSOR_TYPES[config_name].sensor_type
        is_production = prod == PRODUCTION_NAME.lower()
        if sensor_type == ACTIVE_TYPE:
            update_call = update_active
        else:
            update_call = update_trends
        devices.append(Sense(data, name, sensor_type,
                             is_production, update_call))

    if config.get(CONF_DEVICES):
        for device in data.get_discovered_device_data():
            name = device['name']
            device_id = device['id']
            sensor_type = DEVICE_TYPE
            update_call = update_devices
            devices.append(Sense(data, name, sensor_type,
                                 None, update_call, device_id))

    add_entities(devices)


class Sense(Entity):
    """Implementation of a Sense energy sensor."""

    def __init__(self, data, name, sensor_type, is_production, update_call, device_id=None):
        """Initialize the sensor."""
        self._device_id = device_id
        self._data = data
        self._sensor_type = sensor_type
        self.update_sensor = update_call
        self._is_production = is_production
        self._state = None
        self._current = None

        if sensor_type == DEVICE_TYPE:
            self._name = name
            self._entity_id = "sensor." + slugify("{} {}".format('sense', device_id))
        else:
            name_type = PRODUCTION_NAME if is_production else CONSUMPTION_NAME
            self._name = "%s %s" % (name, name_type)
            self._entity_id = "sensor." + slugify("{} {}".format('sense', self._name))

        if sensor_type == ACTIVE_TYPE or sensor_type == DEVICE_TYPE:
            self._unit_of_measurement = 'W'
            self._force_update = True
        else:
            self._unit_of_measurement = 'kWh'
            self._force_update = None

    @property
    def entity_id(self):
        """Return the entity ID of the sensor."""
        return self._entity_id

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._state

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement of this entity, if any."""
        return self._unit_of_measurement

    @property
    def icon(self):
        """Icon to use in the frontend, if any."""
        return ICON

    @property
    def device_state_attributes(self):
        """Return the state attributes of the sensor."""
        if self._current:
            return {
                'current': self._current
            }

    @property
    def force_update(self):
        """Return force_update."""
        return self._force_update


    def update(self):
        """Get the latest data, update state."""
        from sense_energy import SenseAPITimeoutException
        try:
            self.update_sensor()
        except SenseAPITimeoutException:
            _LOGGER.error("Timeout retrieving data")
            return

        if self._sensor_type == DEVICE_TYPE:
            global realtime_devices
            on = None
            for device in realtime_devices:
                if device['id'] == self._device_id:
                    self._state = round(device['w'])
                    self._current = round(device['c'])
                    on = True
            if not on:
                self._state = 0
                self._current = 0
        elif self._sensor_type == ACTIVE_TYPE:
            if self._is_production:
                self._state = round(self._data.active_solar_power)
            else:
                self._state = round(self._data.active_power)
        else:
            state = self._data.get_trend(self._sensor_type,
                                         self._is_production)
            self._state = round(state, 1)