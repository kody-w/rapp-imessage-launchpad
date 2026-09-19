class LaunchpadError(Exception):
    code = "launchpad_error"


class ConfigurationError(LaunchpadError):
    code = "configuration_error"


class IntegrityError(LaunchpadError):
    code = "integrity_error"


class TransportError(LaunchpadError):
    code = "transport_error"


class PluginError(LaunchpadError):
    code = "plugin_error"


class BusyError(LaunchpadError):
    code = "busy"
