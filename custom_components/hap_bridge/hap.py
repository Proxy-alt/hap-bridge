"""HomeKit service, characteristic and value names, so profiles needn't
spell raw HAP strings or remember what 0 and 1 mean.

Generated from HAP-python's catalogue by scripts/gen_hap.py; don't edit.
Profiles get this module as `ctx.hap` without importing it:

    svc = acc.service(ctx.hap.Service.AIR_PURIFIER, primary=True)
    svc.char(ctx.hap.Char.ACTIVE, get=lambda: ctx.hap.Active.ACTIVE)
"""

from enum import IntEnum
from typing import Final


class Service:
    """Service names, for `acc.service(...)`."""

    ACCESSORY_INFORMATION: Final = "AccessoryInformation"
    AIR_PURIFIER: Final = "AirPurifier"
    AIR_QUALITY_SENSOR: Final = "AirQualitySensor"
    BATTERY_SERVICE: Final = "BatteryService"
    CAMERA_OPERATING_MODE: Final = "CameraOperatingMode"
    CAMERA_RTPSTREAM_MANAGEMENT: Final = "CameraRTPStreamManagement"
    CAMERA_RECORDING_MANAGEMENT: Final = "CameraRecordingManagement"
    CARBON_DIOXIDE_SENSOR: Final = "CarbonDioxideSensor"
    CARBON_MONOXIDE_SENSOR: Final = "CarbonMonoxideSensor"
    CONTACT_SENSOR: Final = "ContactSensor"
    DATA_STREAM_TRANSPORT_MANAGEMENT: Final = "DataStreamTransportManagement"
    DOOR: Final = "Door"
    DOORBELL: Final = "Doorbell"
    FAN: Final = "Fan"
    FANV2: Final = "Fanv2"
    FAUCET: Final = "Faucet"
    FILTER_MAINTENANCE: Final = "FilterMaintenance"
    GARAGE_DOOR_OPENER: Final = "GarageDoorOpener"
    HEATER_COOLER: Final = "HeaterCooler"
    HUMIDIFIER_DEHUMIDIFIER: Final = "HumidifierDehumidifier"
    HUMIDITY_SENSOR: Final = "HumiditySensor"
    INPUT_SOURCE: Final = "InputSource"
    IRRIGATION_SYSTEM: Final = "IrrigationSystem"
    LEAK_SENSOR: Final = "LeakSensor"
    LIGHT_SENSOR: Final = "LightSensor"
    LIGHTBULB: Final = "Lightbulb"
    LOCK_MANAGEMENT: Final = "LockManagement"
    LOCK_MECHANISM: Final = "LockMechanism"
    MICROPHONE: Final = "Microphone"
    MOTION_SENSOR: Final = "MotionSensor"
    NFCACCESS: Final = "NFCAccess"
    OCCUPANCY_SENSOR: Final = "OccupancySensor"
    OUTLET: Final = "Outlet"
    SECURITY_SYSTEM: Final = "SecuritySystem"
    SERVICE_LABEL: Final = "ServiceLabel"
    SLAT: Final = "Slat"
    SMOKE_SENSOR: Final = "SmokeSensor"
    SPEAKER: Final = "Speaker"
    STATELESS_PROGRAMMABLE_SWITCH: Final = "StatelessProgrammableSwitch"
    SWITCH: Final = "Switch"
    TELEVISION: Final = "Television"
    TELEVISION_SPEAKER: Final = "TelevisionSpeaker"
    TEMPERATURE_SENSOR: Final = "TemperatureSensor"
    THERMOSTAT: Final = "Thermostat"
    VALVE: Final = "Valve"
    WINDOW: Final = "Window"
    WINDOW_COVERING: Final = "WindowCovering"


class Char:
    """Characteristic names, for `svc.char(...)`."""

    ACCESSORY_FLAGS: Final = "AccessoryFlags"
    ACTIVE: Final = "Active"
    ACTIVE_IDENTIFIER: Final = "ActiveIdentifier"
    ACTIVE_TRANSITION_COUNT: Final = "ActiveTransitionCount"
    ADMINISTRATOR_ONLY_ACCESS: Final = "AdministratorOnlyAccess"
    AIR_PARTICULATE_DENSITY: Final = "AirParticulateDensity"
    AIR_PARTICULATE_SIZE: Final = "AirParticulateSize"
    AIR_QUALITY: Final = "AirQuality"
    AUDIO_FEEDBACK: Final = "AudioFeedback"
    BATTERY_LEVEL: Final = "BatteryLevel"
    BRIGHTNESS: Final = "Brightness"
    CAMERA_OPERATING_MODE_INDICATOR: Final = "CameraOperatingModeIndicator"
    CARBON_DIOXIDE_DETECTED: Final = "CarbonDioxideDetected"
    CARBON_DIOXIDE_LEVEL: Final = "CarbonDioxideLevel"
    CARBON_DIOXIDE_PEAK_LEVEL: Final = "CarbonDioxidePeakLevel"
    CARBON_MONOXIDE_DETECTED: Final = "CarbonMonoxideDetected"
    CARBON_MONOXIDE_LEVEL: Final = "CarbonMonoxideLevel"
    CARBON_MONOXIDE_PEAK_LEVEL: Final = "CarbonMonoxidePeakLevel"
    CHARGING_STATE: Final = "ChargingState"
    CLOSED_CAPTIONS: Final = "ClosedCaptions"
    COLOR_TEMPERATURE: Final = "ColorTemperature"
    CONFIGURATION_STATE: Final = "ConfigurationState"
    CONFIGURED_NAME: Final = "ConfiguredName"
    CONTACT_SENSOR_STATE: Final = "ContactSensorState"
    COOLING_THRESHOLD_TEMPERATURE: Final = "CoolingThresholdTemperature"
    CURRENT_AIR_PURIFIER_STATE: Final = "CurrentAirPurifierState"
    CURRENT_AMBIENT_LIGHT_LEVEL: Final = "CurrentAmbientLightLevel"
    CURRENT_DOOR_STATE: Final = "CurrentDoorState"
    CURRENT_FAN_STATE: Final = "CurrentFanState"
    CURRENT_HEATER_COOLER_STATE: Final = "CurrentHeaterCoolerState"
    CURRENT_HEATING_COOLING_STATE: Final = "CurrentHeatingCoolingState"
    CURRENT_HORIZONTAL_TILT_ANGLE: Final = "CurrentHorizontalTiltAngle"
    CURRENT_HUMIDIFIER_DEHUMIDIFIER_STATE: Final = "CurrentHumidifierDehumidifierState"
    CURRENT_MEDIA_STATE: Final = "CurrentMediaState"
    CURRENT_POSITION: Final = "CurrentPosition"
    CURRENT_RELATIVE_HUMIDITY: Final = "CurrentRelativeHumidity"
    CURRENT_SLAT_STATE: Final = "CurrentSlatState"
    CURRENT_TEMPERATURE: Final = "CurrentTemperature"
    CURRENT_TILT_ANGLE: Final = "CurrentTiltAngle"
    CURRENT_VERTICAL_TILT_ANGLE: Final = "CurrentVerticalTiltAngle"
    CURRENT_VISIBILITY_STATE: Final = "CurrentVisibilityState"
    DIGITAL_ZOOM: Final = "DigitalZoom"
    DISPLAY_ORDER: Final = "DisplayOrder"
    EVENT_SNAPSHOTS_ACTIVE: Final = "EventSnapshotsActive"
    FILTER_CHANGE_INDICATION: Final = "FilterChangeIndication"
    FILTER_LIFE_LEVEL: Final = "FilterLifeLevel"
    FIRMWARE_REVISION: Final = "FirmwareRevision"
    HARDWARE_FINISH: Final = "HardwareFinish"
    HARDWARE_REVISION: Final = "HardwareRevision"
    HEATING_THRESHOLD_TEMPERATURE: Final = "HeatingThresholdTemperature"
    HOLD_POSITION: Final = "HoldPosition"
    HOME_KIT_CAMERA_ACTIVE: Final = "HomeKitCameraActive"
    HUE: Final = "Hue"
    IDENTIFIER: Final = "Identifier"
    IDENTIFY: Final = "Identify"
    IMAGE_MIRRORING: Final = "ImageMirroring"
    IMAGE_ROTATION: Final = "ImageRotation"
    IN_USE: Final = "InUse"
    INPUT_DEVICE_TYPE: Final = "InputDeviceType"
    INPUT_SOURCE_TYPE: Final = "InputSourceType"
    IS_CONFIGURED: Final = "IsConfigured"
    LEAK_DETECTED: Final = "LeakDetected"
    LOCK_CONTROL_POINT: Final = "LockControlPoint"
    LOCK_CURRENT_STATE: Final = "LockCurrentState"
    LOCK_LAST_KNOWN_ACTION: Final = "LockLastKnownAction"
    LOCK_MANAGEMENT_AUTO_SECURITY_TIMEOUT: Final = "LockManagementAutoSecurityTimeout"
    LOCK_PHYSICAL_CONTROLS: Final = "LockPhysicalControls"
    LOCK_TARGET_STATE: Final = "LockTargetState"
    LOGS: Final = "Logs"
    MANUALLY_DISABLED: Final = "ManuallyDisabled"
    MANUFACTURER: Final = "Manufacturer"
    MODEL: Final = "Model"
    MOTION_DETECTED: Final = "MotionDetected"
    MUTE: Final = "Mute"
    NFCACCESS_CONTROL_POINT: Final = "NFCAccessControlPoint"
    NFCACCESS_SUPPORTED_CONFIGURATION: Final = "NFCAccessSupportedConfiguration"
    NAME: Final = "Name"
    NIGHT_VISION: Final = "NightVision"
    NITROGEN_DIOXIDE_DENSITY: Final = "NitrogenDioxideDensity"
    OBSTRUCTION_DETECTED: Final = "ObstructionDetected"
    OCCUPANCY_DETECTED: Final = "OccupancyDetected"
    ON: Final = "On"
    OPTICAL_ZOOM: Final = "OpticalZoom"
    OUTLET_IN_USE: Final = "OutletInUse"
    OZONE_DENSITY: Final = "OzoneDensity"
    PM10_DENSITY: Final = "PM10Density"
    PM2_5_DENSITY: Final = "PM2.5Density"
    PAIR_SETUP: Final = "PairSetup"
    PAIR_VERIFY: Final = "PairVerify"
    PAIRING_FEATURES: Final = "PairingFeatures"
    PAIRING_PAIRINGS: Final = "PairingPairings"
    PERIODIC_SNAPSHOTS_ACTIVE: Final = "PeriodicSnapshotsActive"
    PICTURE_MODE: Final = "PictureMode"
    POSITION_STATE: Final = "PositionState"
    POWER_MODE_SELECTION: Final = "PowerModeSelection"
    PROGRAM_MODE: Final = "ProgramMode"
    PROGRAMMABLE_SWITCH_EVENT: Final = "ProgrammableSwitchEvent"
    RECORDING_AUDIO_ACTIVE: Final = "RecordingAudioActive"
    RELATIVE_HUMIDITY_DEHUMIDIFIER_THRESHOLD: Final = "RelativeHumidityDehumidifierThreshold"
    RELATIVE_HUMIDITY_HUMIDIFIER_THRESHOLD: Final = "RelativeHumidityHumidifierThreshold"
    REMAINING_DURATION: Final = "RemainingDuration"
    REMOTE_KEY: Final = "RemoteKey"
    RESET_FILTER_INDICATION: Final = "ResetFilterIndication"
    ROTATION_DIRECTION: Final = "RotationDirection"
    ROTATION_SPEED: Final = "RotationSpeed"
    SATURATION: Final = "Saturation"
    SECURITY_SYSTEM_ALARM_TYPE: Final = "SecuritySystemAlarmType"
    SECURITY_SYSTEM_CURRENT_STATE: Final = "SecuritySystemCurrentState"
    SECURITY_SYSTEM_TARGET_STATE: Final = "SecuritySystemTargetState"
    SELECTED_CAMERA_RECORDING_CONFIGURATION: Final = "SelectedCameraRecordingConfiguration"
    SELECTED_RTPSTREAM_CONFIGURATION: Final = "SelectedRTPStreamConfiguration"
    SERIAL_NUMBER: Final = "SerialNumber"
    SERVICE_LABEL_INDEX: Final = "ServiceLabelIndex"
    SERVICE_LABEL_NAMESPACE: Final = "ServiceLabelNamespace"
    SET_DURATION: Final = "SetDuration"
    SETUP_DATA_STREAM_TRANSPORT: Final = "SetupDataStreamTransport"
    SETUP_ENDPOINTS: Final = "SetupEndpoints"
    SLAT_TYPE: Final = "SlatType"
    SLEEP_DISCOVERY_MODE: Final = "SleepDiscoveryMode"
    SMOKE_DETECTED: Final = "SmokeDetected"
    STATUS_ACTIVE: Final = "StatusActive"
    STATUS_FAULT: Final = "StatusFault"
    STATUS_JAMMED: Final = "StatusJammed"
    STATUS_LOW_BATTERY: Final = "StatusLowBattery"
    STATUS_TAMPERED: Final = "StatusTampered"
    STREAMING_STATUS: Final = "StreamingStatus"
    SULPHUR_DIOXIDE_DENSITY: Final = "SulphurDioxideDensity"
    SUPPORTED_AUDIO_RECORDING_CONFIGURATION: Final = "SupportedAudioRecordingConfiguration"
    SUPPORTED_AUDIO_STREAM_CONFIGURATION: Final = "SupportedAudioStreamConfiguration"
    SUPPORTED_CAMERA_RECORDING_CONFIGURATION: Final = "SupportedCameraRecordingConfiguration"
    SUPPORTED_DATA_STREAM_TRANSPORT_CONFIGURATION: Final = "SupportedDataStreamTransportConfiguration"
    SUPPORTED_RTPCONFIGURATION: Final = "SupportedRTPConfiguration"
    SUPPORTED_TRANSITION_CONFIGURATION: Final = "SupportedTransitionConfiguration"
    SUPPORTED_VIDEO_RECORDING_CONFIGURATION: Final = "SupportedVideoRecordingConfiguration"
    SUPPORTED_VIDEO_STREAM_CONFIGURATION: Final = "SupportedVideoStreamConfiguration"
    SWING_MODE: Final = "SwingMode"
    TARGET_AIR_PURIFIER_STATE: Final = "TargetAirPurifierState"
    TARGET_AIR_QUALITY: Final = "TargetAirQuality"
    TARGET_DOOR_STATE: Final = "TargetDoorState"
    TARGET_FAN_STATE: Final = "TargetFanState"
    TARGET_HEATER_COOLER_STATE: Final = "TargetHeaterCoolerState"
    TARGET_HEATING_COOLING_STATE: Final = "TargetHeatingCoolingState"
    TARGET_HORIZONTAL_TILT_ANGLE: Final = "TargetHorizontalTiltAngle"
    TARGET_HUMIDIFIER_DEHUMIDIFIER_STATE: Final = "TargetHumidifierDehumidifierState"
    TARGET_MEDIA_STATE: Final = "TargetMediaState"
    TARGET_POSITION: Final = "TargetPosition"
    TARGET_RELATIVE_HUMIDITY: Final = "TargetRelativeHumidity"
    TARGET_SLAT_STATE: Final = "TargetSlatState"
    TARGET_TEMPERATURE: Final = "TargetTemperature"
    TARGET_TILT_ANGLE: Final = "TargetTiltAngle"
    TARGET_VERTICAL_TILT_ANGLE: Final = "TargetVerticalTiltAngle"
    TARGET_VISIBILITY_STATE: Final = "TargetVisibilityState"
    TEMPERATURE_DISPLAY_UNITS: Final = "TemperatureDisplayUnits"
    TRANSITION_CONTROL: Final = "TransitionControl"
    VOCDENSITY: Final = "VOCDensity"
    VALVE_TYPE: Final = "ValveType"
    VERSION: Final = "Version"
    VOLUME: Final = "Volume"
    VOLUME_CONTROL_TYPE: Final = "VolumeControlType"
    VOLUME_SELECTOR: Final = "VolumeSelector"
    WATER_LEVEL: Final = "WaterLevel"


class Active(IntEnum):
    """Values of Active."""

    INACTIVE = 0
    ACTIVE = 1


class AirParticulateSize(IntEnum):
    """Values of AirParticulateSize."""

    _2_5_M = 0
    _10_M = 1


class AirQuality(IntEnum):
    """Values of AirQuality."""

    UNKNOWN = 0
    EXCELLENT = 1
    GOOD = 2
    FAIR = 3
    INFERIOR = 4
    POOR = 5


class CarbonDioxideDetected(IntEnum):
    """Values of CarbonDioxideDetected."""

    CO2_LEVELS_NORMAL = 0
    CO2_LEVELS_ABNORMAL = 1


class CarbonMonoxideDetected(IntEnum):
    """Values of CarbonMonoxideDetected."""

    COLEVELS_NORMAL = 0
    COLEVELS_ABNORMAL = 1


class ChargingState(IntEnum):
    """Values of ChargingState."""

    NOT_CHARGING = 0
    CHARGING = 1
    NOT_CHARGEABLE = 2


class ClosedCaptions(IntEnum):
    """Values of ClosedCaptions."""

    DISABLED = 0
    ENABLED = 1


class ContactSensorState(IntEnum):
    """Values of ContactSensorState."""

    CONTACT_DETECTED = 0
    CONTACT_NOT_DETECTED = 1


class CurrentAirPurifierState(IntEnum):
    """Values of CurrentAirPurifierState."""

    INACTIVE = 0
    IDLE = 1
    PURIFYING_AIR = 2


class CurrentDoorState(IntEnum):
    """Values of CurrentDoorState."""

    OPEN = 0
    CLOSED = 1
    OPENING = 2
    CLOSING = 3
    STOPPED = 4


class CurrentFanState(IntEnum):
    """Values of CurrentFanState."""

    INACTIVE = 0
    IDLE = 1
    BLOWING_AIR = 2


class CurrentHeaterCoolerState(IntEnum):
    """Values of CurrentHeaterCoolerState."""

    INACTIVE = 0
    IDLE = 1
    HEATING = 2
    COOLING = 3


class CurrentHeatingCoolingState(IntEnum):
    """Values of CurrentHeatingCoolingState."""

    OFF = 0
    HEAT = 1
    COOL = 2


class CurrentHumidifierDehumidifierState(IntEnum):
    """Values of CurrentHumidifierDehumidifierState."""

    INACTIVE = 0
    IDLE = 1
    HUMIDIFYING = 2
    DEHUMIDIFYING = 3


class CurrentMediaState(IntEnum):
    """Values of CurrentMediaState."""

    PLAY = 0
    PAUSE = 1
    STOP = 2
    UNKNOWN = 3


class CurrentSlatState(IntEnum):
    """Values of CurrentSlatState."""

    FIXED = 0
    JAMMED = 1
    SWINGING = 2


class CurrentVisibilityState(IntEnum):
    """Values of CurrentVisibilityState."""

    SHOWN = 0
    HIDDEN = 1
    STATE2 = 2
    STATE3 = 3


class FilterChangeIndication(IntEnum):
    """Values of FilterChangeIndication."""

    FILTER_OK = 0
    CHANGE_FILTER = 1


class InUse(IntEnum):
    """Values of InUse."""

    NOTINUSE = 0
    INUSE = 1


class InputDeviceType(IntEnum):
    """Values of InputDeviceType."""

    OTHER = 0
    TV = 1
    RECORDING = 2
    TUNER = 3
    PLAYBACK = 4
    AUDIO_SYSTEM = 5


class InputSourceType(IntEnum):
    """Values of InputSourceType."""

    OTHER = 0
    HOME_SCREEN = 1
    TUNER = 2
    HDMI = 3
    COMPOSITE_VIDEO = 4
    SVIDEO = 5
    COMPONENT_VIDEO = 6
    DVI = 7
    AIR_PLAY = 8
    USB = 9
    APPLICATION = 10


class IsConfigured(IntEnum):
    """Values of IsConfigured."""

    NOT_CONFIGURED = 0
    CONFIGURED = 1


class LeakDetected(IntEnum):
    """Values of LeakDetected."""

    LEAK_NOT_DETECTED = 0
    LEAK_DETECTED = 1


class LockCurrentState(IntEnum):
    """Values of LockCurrentState."""

    UNSECURED = 0
    SECURED = 1
    JAMMED = 2
    UNKNOWN = 3


class LockLastKnownAction(IntEnum):
    """Values of LockLastKnownAction."""

    SECURED_PHYSICALLY_INTERIOR = 0
    UNSECURED_PHYSICALLY_INTERIOR = 1
    SECURED_PHYSICALLY_EXTERIOR = 2
    UNSECURED_PHYSICALLY_EXTERIOR = 3
    SECUREDBY_KEYPAD = 4
    UNSECUREDBY_KEYPAD = 5
    SECURED_REMOTELY = 6
    UNSECURED_REMOTELY = 7
    SECUREDBY_AUTO_SECURE_TIMEOUT = 8


class LockPhysicalControls(IntEnum):
    """Values of LockPhysicalControls."""

    CONTROL_LOCK_DISABLED = 0
    CONTROL_LOCK_ENABLED = 1


class LockTargetState(IntEnum):
    """Values of LockTargetState."""

    UNSECURED = 0
    SECURED = 1


class OccupancyDetected(IntEnum):
    """Values of OccupancyDetected."""

    OCCUPANCY_NOT_DETECTED = 0
    OCCUPANCY_DETECTED = 1


class PictureMode(IntEnum):
    """Values of PictureMode."""

    OTHER = 0
    STANDARD = 1
    CALIBRATED = 2
    CALIBRATED_DARK = 3
    VIVID = 4
    GAME = 5
    COMPUTER = 6
    CUSTOM = 7


class PositionState(IntEnum):
    """Values of PositionState."""

    DECREASING = 0
    INCREASING = 1
    STOPPED = 2


class PowerModeSelection(IntEnum):
    """Values of PowerModeSelection."""

    SHOW = 0
    HIDE = 1


class ProgramMode(IntEnum):
    """Values of ProgramMode."""

    NOPROGRAMSCHEDULED = 0
    PROGRAMSCHEDULED = 1
    PROGRAMSCHEDULED_MANUAL_MODE = 2


class ProgrammableSwitchEvent(IntEnum):
    """Values of ProgrammableSwitchEvent."""

    SINGLE_PRESS = 0
    DOUBLE_PRESS = 1
    LONG_PRESS = 2


class RemoteKey(IntEnum):
    """Values of RemoteKey."""

    REWIND = 0
    FAST_FORWARD = 1
    NEXT_TRACK = 2
    PREVIOUS_TRACK = 3
    ARROW_UP = 4
    ARROW_DOWN = 5
    ARROW_LEFT = 6
    ARROW_RIGHT = 7
    SELECT = 8
    BACK = 9
    EXIT = 10
    PLAY_PAUSE = 11
    INFORMATION = 15


class RotationDirection(IntEnum):
    """Values of RotationDirection."""

    CLOCKWISE = 0
    COUNTER_CLOCKWISE = 1


class SecuritySystemCurrentState(IntEnum):
    """Values of SecuritySystemCurrentState."""

    STAY_ARM = 0
    AWAY_ARM = 1
    NIGHT_ARM = 2
    DISARMED = 3
    ALARM_TRIGGERED = 4


class SecuritySystemTargetState(IntEnum):
    """Values of SecuritySystemTargetState."""

    STAY_ARM = 0
    AWAY_ARM = 1
    NIGHT_ARM = 2
    DISARM = 3


class ServiceLabelNamespace(IntEnum):
    """Values of ServiceLabelNamespace."""

    DOTS = 0
    ARABIC_NUMERALS = 1


class SlatType(IntEnum):
    """Values of SlatType."""

    HORIZONTAL = 0
    VERTICAL = 1


class SleepDiscoveryMode(IntEnum):
    """Values of SleepDiscoveryMode."""

    NOT_DISCOVERABLE = 0
    ALWAYS_DISCOVERABLE = 1


class SmokeDetected(IntEnum):
    """Values of SmokeDetected."""

    SMOKE_NOT_DETECTED = 0
    SMOKE_DETECTED = 1


class StatusFault(IntEnum):
    """Values of StatusFault."""

    NO_FAULT = 0
    GENERAL_FAULT = 1


class StatusJammed(IntEnum):
    """Values of StatusJammed."""

    NOT_JAMMED = 0
    JAMMED = 1


class StatusLowBattery(IntEnum):
    """Values of StatusLowBattery."""

    BATTERY_LEVEL_NORMAL = 0
    BATTERY_LEVEL_LOW = 1


class StatusTampered(IntEnum):
    """Values of StatusTampered."""

    NOT_TAMPERED = 0
    TAMPERED = 1


class SwingMode(IntEnum):
    """Values of SwingMode."""

    SWING_DISABLED = 0
    SWING_ENABLED = 1


class TargetAirPurifierState(IntEnum):
    """Values of TargetAirPurifierState."""

    MANUAL = 0
    AUTO = 1


class TargetAirQuality(IntEnum):
    """Values of TargetAirQuality."""

    EXCELLENT = 0
    GOOD = 1
    FAIR = 2


class TargetDoorState(IntEnum):
    """Values of TargetDoorState."""

    OPEN = 0
    CLOSED = 1


class TargetFanState(IntEnum):
    """Values of TargetFanState."""

    MANUAL = 0
    AUTO = 1


class TargetHeaterCoolerState(IntEnum):
    """Values of TargetHeaterCoolerState."""

    AUTO = 0
    HEAT = 1
    COOL = 2


class TargetHeatingCoolingState(IntEnum):
    """Values of TargetHeatingCoolingState."""

    OFF = 0
    HEAT = 1
    COOL = 2
    AUTO = 3


class TargetHumidifierDehumidifierState(IntEnum):
    """Values of TargetHumidifierDehumidifierState."""

    HUMIDIFIEROR_DEHUMIDIFIER = 0
    HUMIDIFIER = 1
    DEHUMIDIFIER = 2


class TargetMediaState(IntEnum):
    """Values of TargetMediaState."""

    PLAY = 0
    PAUSE = 1
    STOP = 2


class TargetSlatState(IntEnum):
    """Values of TargetSlatState."""

    MANUAL = 0
    AUTO = 1


class TargetVisibilityState(IntEnum):
    """Values of TargetVisibilityState."""

    SHOWN = 0
    HIDDEN = 1


class TemperatureDisplayUnits(IntEnum):
    """Values of TemperatureDisplayUnits."""

    CELSIUS = 0
    FAHRENHEIT = 1


class ValveType(IntEnum):
    """Values of ValveType."""

    GENERICVALVE = 0
    IRRIGATION = 1
    SHOWERHEAD = 2
    WATERFAUCET = 3


class VolumeControlType(IntEnum):
    """Values of VolumeControlType."""

    NONE = 0
    RELATIVE = 1
    RELATIVE_WITH_CURRENT = 2
    ABSOLUTE = 3


class VolumeSelector(IntEnum):
    """Values of VolumeSelector."""

    INCREMENT = 0
    DECREMENT = 1
