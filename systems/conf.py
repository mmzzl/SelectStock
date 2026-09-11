import os

import logging
try:
    import configparser as ConfigParser
except ImportError:
    import ConfigParser

"""
A Conf Load Files.
	Will load the default conf and the local conf, and combine it.

Take for example.

	default/conf/httpd.conf
		[httpd]
		port=80
		www=www

	local/conf/httpd.conf
		[httpd]
		port=8080

	1) the local value will override the default value.

	2) the update will override the default conf
		but never replace the local conf

	3) the modified part will only stored in the local
		The programmer can only store the modified part in the local.
"""


class FantomConfigParser(ConfigParser.ConfigParser):
    def __init__(self, defaults=None):
        ConfigParser.ConfigParser.__init__(self, defaults)

    def optionxform(self, optionstr):
        return optionstr


class Conf(object):
    def __init__(self, home, filename):
        self.home = home
        self.filename = filename

        self.config = self._load()

    def _load_raw(self, filepath):
        config = {}

        if os.path.exists(filepath):
            try:
                parser = FantomConfigParser()
                parser.read(filepath)

                for section in parser.sections():
                    config[section] = {}
                    for key, value in parser.items(section):
                        config[section][key] = value
            except Exception as e:
                logging.error(str(e))

        return config

    def _load(self):
        local = self._load_raw(os.path.join(self.home,
                                              self.filename))
        result = dict()
        for section, section_values in local.items():
            if section not in result:
                result[section] = {}
            for key, value in section_values.items():
                result[section][key] = value
        return result

    def load_value(self, section, option, default=None):
        s = self.config.get(section)

        if s:
            o = s.get(option)

            if o:
                return o

        return default
